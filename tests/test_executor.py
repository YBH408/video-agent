import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent.executor as execution

from agent.executor import (
    DOWNLOADED,
    INTENT,
    RealBackend,
    RunState,
    FAILED,
    SUBMITTED,
    SUCCEEDED,
    UNCERTAIN,
    ExecutionError,
    Executor,
    MockBackend,
    plan_hash,
)
from agent.planner import parse_plan

FIXTURE = Path(__file__).resolve().parents[1] / "examples" / "future_city_10s.json"


def make_plan():
    return parse_plan(FIXTURE.read_text(encoding="utf-8"))


class _GuardingBackend(MockBackend):
    """Queries succeed, but any submission is a failure of the test."""

    def __init__(self):
        self.submit_count = 0

    def submit_video(self, prompt, *, duration_seconds, aspect_ratio, shot_id):
        self.submit_count += 1
        raise AssertionError("resubmission must not happen")

    def query_video(self, task_id):
        return {"status": "succeeded", "content": {"url": f"mock://video/{task_id}.mp4"}}

    def download_video(self, url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"clip\n")


class _FailingSubmitBackend(MockBackend):
    """Submission raises; used to prove no auto-resubmit happens."""

    def __init__(self):
        self.submit_count = 0

    def submit_video(self, prompt, *, duration_seconds, aspect_ratio, shot_id):
        self.submit_count += 1
        raise RuntimeError("network lost before reply")

    def query_video(self, task_id):
        return {"status": "succeeded", "content": {"url": "mock://v.mp4"}}

    def download_video(self, url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"clip\n")


class _TimedOutQueryBackend(MockBackend):
    def __init__(self):
        self.submit_count = 0

    def submit_video(self, prompt, *, duration_seconds, aspect_ratio, shot_id):
        self.submit_count += 1
        return f"mock-{shot_id}"

    def query_video(self, task_id):
        raise RuntimeError("timeout")

    def download_video(self, url, dest):
        raise AssertionError("download must not be reached")


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name) / "state.json"
        self.output_dir = Path(self._tmp.name) / "videos"

    def test_two_shots_complete_in_mock_mode(self):
        backend = MockBackend()
        executor = Executor(make_plan(), state_path=self.state, output_dir=self.output_dir,
                            backend=backend, poll_interval=0.0, max_polls=10)
        records = executor.run()
        self.assertEqual([r.status for r in records], [DOWNLOADED, DOWNLOADED])
        self.assertEqual(backend.submit_count, 2)
        self.assertEqual(backend.query_count, 2)
        for record in records:
            path = Path(record.download_path)
            self.assertTrue(path.is_file())
            self.assertGreater(path.stat().st_size, 0)
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(len(saved["records"]), 2)
        self.assertEqual(saved["plan_hash"], plan_hash(make_plan()))

    def test_completed_run_resumes_without_resubmitting(self):
        backend = MockBackend()
        Executor(make_plan(), state_path=self.state, output_dir=self.output_dir,
                 backend=backend, poll_interval=0.0, max_polls=10).run()
        guard = _GuardingBackend()
        executor = Executor(make_plan(), state_path=self.state, output_dir=self.output_dir,
                            backend=guard, poll_interval=0.0, max_polls=10)
        self.assertEqual([r.status for r in executor.run()], [DOWNLOADED, DOWNLOADED])
        self.assertEqual(guard.submit_count, 0)

    def test_interrupted_after_submit_queries_without_resubmitting(self):
        plan = make_plan()
        first_path = self.output_dir / "shot_1.mp4"
        first_path.parent.mkdir(parents=True, exist_ok=True)
        first_path.write_bytes(b"x")
        self.state.write_text(json.dumps({
            "schema_version": execution.STATE_VERSION,
            "execution": MockBackend().execution_identity().to_dict(),
            "plan_hash": plan_hash(plan),
            "records": {
                "1": {"shot_id": 1, "status": DOWNLOADED, "task_id": "mock-task-1",
                      "video_url": "mock://v1", "download_path": str(first_path)},
                "2": {"shot_id": 2, "status": SUBMITTED, "task_id": "mock-task-2",
                      "video_url": None, "download_path": None},
            },
        }, ensure_ascii=False), encoding="utf-8")
        guard = _GuardingBackend()
        executor = Executor(plan, state_path=self.state, output_dir=self.output_dir,
                            backend=guard, poll_interval=0.0, max_polls=10)
        self.assertEqual([r.status for r in executor.run()], [DOWNLOADED, DOWNLOADED])
        self.assertEqual(guard.submit_count, 0)
        self.assertTrue((self.output_dir / "shot_2.mp4").is_file())

    def test_uncertain_submission_stops_and_never_resubmits(self):
        plan = make_plan()
        backend = _FailingSubmitBackend()
        with self.assertRaises(ExecutionError):
            Executor(plan, state_path=self.state, output_dir=self.output_dir,
                     backend=backend, poll_interval=0.0, max_polls=10).run()
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(saved["records"]["1"]["status"], UNCERTAIN)
        self.assertIsNone(saved["records"]["1"]["task_id"])
        # A second attempt must not call submit again.
        second = _FailingSubmitBackend()
        with self.assertRaises(ExecutionError):
            Executor(plan, state_path=self.state, output_dir=self.output_dir,
                     backend=second, poll_interval=0.0, max_polls=10).run()
        self.assertEqual(second.submit_count, 0)

    def test_failed_generation_is_recorded_and_raises(self):
        class _FailedBackend(MockBackend):
            def submit_video(self, prompt, *, duration_seconds, aspect_ratio, shot_id):
                return "mock-1"

            def query_video(self, task_id):
                return {"status": "failed"}

            def download_video(self, url, dest):
                raise AssertionError("download must not be reached")

        plan = make_plan()
        with self.assertRaises(ExecutionError):
            Executor(plan, state_path=self.state, output_dir=self.output_dir,
                     backend=_FailedBackend(), poll_interval=0.0, max_polls=10).run()
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(saved["records"]["1"]["status"], FAILED)

    def test_query_timeout_keeps_task_id_without_resubmitting(self):
        plan = make_plan()
        backend = _TimedOutQueryBackend()
        with self.assertRaises(ExecutionError):
            Executor(plan, state_path=self.state, output_dir=self.output_dir,
                     backend=backend, poll_interval=0.0, max_polls=2).run()
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(saved["records"]["1"]["status"], SUBMITTED)
        self.assertEqual(saved["records"]["1"]["task_id"], "mock-1")
        self.assertEqual(backend.submit_count, 1)

    def test_state_plan_mismatch_is_rejected(self):
        self.state.write_text(json.dumps({"schema_version": execution.STATE_VERSION,
                                               "execution": MockBackend().execution_identity().to_dict(),
                                               "plan_hash": "0" * 64, "records": {}}), encoding="utf-8")
        with self.assertRaises(ExecutionError):
            Executor(make_plan(), state_path=self.state, output_dir=self.output_dir,
                     backend=MockBackend(), poll_interval=0.0, max_polls=10)

    def test_download_failure_is_resumable(self):
        class _FlakyDownloadBackend(MockBackend):
            def __init__(self):
                self.download_attempts = 0

            def submit_video(self, prompt, *, duration_seconds, aspect_ratio, shot_id):
                return f"mock-{shot_id}"

            def query_video(self, task_id):
                return {"status": "succeeded", "content": {"url": f"mock://{task_id}.mp4"}}

            def download_video(self, url, dest):
                self.download_attempts += 1
                raise RuntimeError("disk full")

        class _RecordingBackend(MockBackend):
            def __init__(self):
                self.submitted = []

            def submit_video(self, prompt, *, duration_seconds, aspect_ratio, shot_id):
                self.submitted.append(shot_id)
                return f"mock-{shot_id}"

            def query_video(self, task_id):
                return {"status": "succeeded", "content": {"url": f"mock://{task_id}.mp4"}}

            def download_video(self, url, dest):
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b"clip\n")

        plan = make_plan()
        with self.assertRaises(ExecutionError):
            Executor(plan, state_path=self.state, output_dir=self.output_dir,
                     backend=_FlakyDownloadBackend(), poll_interval=0.0, max_polls=10).run()
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(saved["records"]["1"]["status"], SUCCEEDED)
        self.assertIsNotNone(saved["records"]["1"]["video_url"])
        # Resume: shot 1 re-downloads without resubmitting; shot 2 submits once.
        recording = _RecordingBackend()
        executor = Executor(plan, state_path=self.state, output_dir=self.output_dir,
                            backend=recording, poll_interval=0.0, max_polls=10)
        self.assertEqual([r.status for r in executor.run()], [DOWNLOADED, DOWNLOADED])
        self.assertEqual(recording.submitted, [2])


class RecoverySafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state.json"
        self.output = Path(self.tmp.name) / "videos"

    def executor(self, backend):
        return Executor(make_plan(), state_path=self.state, output_dir=self.output,
                        backend=backend, poll_interval=0, max_polls=2)

    def write_state(self, backend, records=None):
        state = RunState(plan_hash(make_plan()), backend.execution_identity().to_dict(), records or {})
        self.state.write_text(json.dumps(state.to_dict()), encoding="utf-8")

    def test_mock_state_rejected_by_real_before_backend_calls(self):
        self.executor(MockBackend()).run()
        before = self.state.read_bytes()
        real = RealBackend("TEST_ONLY_MODEL_A")
        with patch.object(real, "submit_video") as submit, patch.object(real, "query_video") as query, \
             patch.object(real, "download_video") as download, self.assertRaises(ExecutionError):
            self.executor(real)
        for call in (submit, query, download):
            call.assert_not_called()
        self.assertEqual(self.state.read_bytes(), before)

    def test_real_state_rejected_by_mock_without_modification(self):
        self.write_state(RealBackend("TEST_ONLY_MODEL_A"))
        before = self.state.read_bytes()
        mock = MockBackend()
        with self.assertRaises(ExecutionError):
            self.executor(mock)
        self.assertEqual(mock.submit_count, 0)
        self.assertEqual(self.state.read_bytes(), before)

    def test_legacy_state_rejected_in_both_modes_and_preserved(self):
        self.state.write_text(json.dumps({"plan_hash": plan_hash(make_plan()), "records": {}}), encoding="utf-8")
        before = self.state.read_bytes()
        for backend in (MockBackend(), RealBackend("TEST_ONLY_MODEL_A")):
            with self.subTest(mode=backend.execution_identity().mode), self.assertRaises(ExecutionError):
                self.executor(backend)
        self.assertEqual(self.state.read_bytes(), before)

    def test_provider_model_endpoint_and_request_parameters_must_match(self):
        real = RealBackend("TEST_ONLY_MODEL_A")
        for field, value in (("provider", "other"), ("model_id", "TEST_ONLY_MODEL_B"),
                             ("api_base_url", "https://example.invalid"),
                             ("request_parameters", {"duration": 10, "resolution": "1080P", "ratio": "9:16"})):
            with self.subTest(field=field):
                self.write_state(real)
                data = json.loads(self.state.read_text(encoding="utf-8"))
                data["execution"][field] = value
                self.state.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(ExecutionError):
                    self.executor(real)

    def test_corrupt_or_incomplete_state_never_becomes_a_new_submission(self):
        backend = MockBackend()
        self.write_state(backend)
        valid = self.state.read_text(encoding="utf-8")
        missing_id = json.loads(valid)
        missing_id["records"] = {"1": {"shot_id": 1, "status": SUBMITTED, "task_id": None,
                                      "video_url": None, "download_path": None}}
        cases = ["{", "[]", valid.replace('"schema_version": 2', '"schema_version": 2, "schema_version": 2'),
                 valid.replace('"records": {}', '"records": NaN'), json.dumps(missing_id)]
        for raw in cases:
            with self.subTest(raw_type=raw[:10]):
                self.state.write_text(raw, encoding="utf-8")
                with self.assertRaises(ExecutionError):
                    self.executor(backend)
                self.assertEqual(self.state.read_text(encoding="utf-8"), raw)
        self.assertEqual(backend.submit_count, 0)

    def test_missing_plan_hash_is_rejected(self):
        self.write_state(MockBackend())
        data = json.loads(self.state.read_text(encoding="utf-8"))
        data["plan_hash"] = ""
        self.state.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ExecutionError):
            self.executor(MockBackend())

    def test_unreadable_state_metadata_is_not_treated_as_missing(self):
        self.write_state(MockBackend())
        before = self.state.read_bytes()
        original = execution.os.lstat

        def inaccessible(path, *args, **kwargs):
            if str(path) == str(self.state):
                raise PermissionError("TEST_ONLY_ACCESS_DENIED")
            return original(path, *args, **kwargs)

        backend = MockBackend()
        with patch.object(execution.os, "lstat", side_effect=inaccessible), self.assertRaises(ExecutionError):
            self.executor(backend)
        self.assertEqual(backend.submit_count, 0)
        self.assertEqual(self.state.read_bytes(), before)

    def test_request_parameter_type_changes_are_rejected(self):
        self.write_state(MockBackend())
        data = json.loads(self.state.read_text(encoding="utf-8"))
        data["execution"]["request_parameters"]["duration"] = 5.0
        self.state.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ExecutionError):
            self.executor(MockBackend())

    def test_real_rejects_mock_id_even_if_metadata_was_relabelled(self):
        real = RealBackend("TEST_ONLY_MODEL_A")
        self.write_state(real, {1: execution.ShotRecord(1, SUBMITTED, task_id="mock-task-1")})
        with self.assertRaises(ExecutionError):
            self.executor(real)

    def test_mock_rejects_real_id_or_url_even_with_mock_metadata(self):
        records = [execution.ShotRecord(1, SUBMITTED, task_id="TEST_ONLY_TASK_1"),
                   execution.ShotRecord(1, SUCCEEDED, task_id="mock-task-1", video_url="https://example.invalid/video.mp4")]
        for record in records:
            with self.subTest(status=record.status):
                self.write_state(MockBackend(), {1: record})
                with self.assertRaises(ExecutionError):
                    self.executor(MockBackend())

    def test_real_rejects_mock_url_even_if_metadata_was_relabelled(self):
        real = RealBackend("TEST_ONLY_MODEL_A")
        self.write_state(real, {1: execution.ShotRecord(1, SUCCEEDED, task_id="TEST_ONLY_TASK_1", video_url="mock://video/1")})
        with self.assertRaises(ExecutionError):
            self.executor(real)

    def test_real_rejects_placeholder_mp4_even_with_real_metadata(self):
        real = RealBackend("TEST_ONLY_MODEL_A")
        self.output.mkdir()
        clip = self.output / "shot_1.mp4"
        clip.write_bytes(b"mock video clip\n")
        self.write_state(real, {1: execution.ShotRecord(1, DOWNLOADED, task_id="TEST_ONLY_TASK_1",
                                                        video_url="https://example.invalid/video.mp4", download_path=str(clip))})
        with self.assertRaises(ExecutionError):
            self.executor(real)
        self.assertEqual(clip.read_bytes(), b"mock video clip\n")

    def test_missing_completed_clip_stops_instead_of_resubmitting(self):
        self.executor(MockBackend()).run()
        (self.output / "shot_1.mp4").unlink()  # Only a newly created test file.
        backend = MockBackend()
        with self.assertRaises(ExecutionError):
            self.executor(backend)
        self.assertEqual(backend.submit_count, 0)

    def test_existing_output_without_matching_state_is_preserved(self):
        self.output.mkdir()
        clip = self.output / "shot_1.mp4"
        clip.write_bytes(b"TEST_ONLY_USER_DATA")
        backend = MockBackend()
        with self.assertRaises(ExecutionError):
            self.executor(backend).run()
        self.assertEqual(backend.submit_count, 0)
        self.assertFalse(self.state.exists())
        self.assertEqual(clip.read_bytes(), b"TEST_ONLY_USER_DATA")

    def test_submission_intent_is_on_disk_before_submit_method(self):
        state_path = self.state
        test = self

        class CheckedBackend(MockBackend):
            def submit_video(self, *args, **kwargs):
                data = json.loads(state_path.read_text(encoding="utf-8"))
                test.assertEqual(data["records"][str(kwargs["shot_id"])]["status"], INTENT)
                test.assertEqual(data["execution"]["mode"], "mock")
                return super().submit_video(*args, **kwargs)

        backend = CheckedBackend()
        self.executor(backend).run()
        self.assertEqual(backend.submit_count, 2)

    def test_intent_write_failure_blocks_real_submit_before_request(self):
        real = RealBackend("TEST_ONLY_MODEL_A")
        executor = self.executor(real)
        with patch.object(execution, "_atomic_write_text", side_effect=OSError("TEST_ONLY_DISK_FAILURE")), \
             patch.object(real, "submit_video") as submit, self.assertRaises(ExecutionError):
            executor.run()
        submit.assert_not_called()
        self.assertFalse(self.state.exists())
        self.assertEqual(executor.state.records, {})

    def test_intent_fsync_failure_blocks_submit(self):
        original = execution.os.fsync
        calls = []

        def fail_state_flush(descriptor):
            calls.append(descriptor)
            if len(calls) == 2:  # First sync is the execution lock; second is the intent file.
                raise OSError("TEST_ONLY_FLUSH_FAILURE")
            return original(descriptor)

        backend = MockBackend()
        with patch.object(execution.os, "fsync", side_effect=fail_state_flush), self.assertRaises(ExecutionError):
            self.executor(backend).run()
        self.assertEqual(backend.submit_count, 0)
        self.assertFalse(self.state.exists())

    def test_intent_replace_failure_preserves_existing_state(self):
        backend = MockBackend()
        self.write_state(backend)
        before = self.state.read_bytes()
        with patch.object(execution.os, "replace", side_effect=OSError("TEST_ONLY_REPLACE_FAILURE")), self.assertRaises(ExecutionError):
            self.executor(backend).run()
        self.assertEqual(backend.submit_count, 0)
        self.assertEqual(self.state.read_bytes(), before)

    def test_persisted_intent_is_not_automatically_resubmitted(self):
        self.write_state(MockBackend(), {1: execution.ShotRecord(1, INTENT)})
        backend = MockBackend()
        with self.assertRaises(ExecutionError):
            self.executor(backend).run()
        self.assertEqual(backend.submit_count, 0)

    def test_simulated_process_loss_after_submit_preserves_intent(self):
        class SimulatedProcessLoss(BaseException):
            pass

        real = RealBackend("TEST_ONLY_MODEL_A")
        with patch.object(real, "submit_video", side_effect=SimulatedProcessLoss) as submit, \
             self.assertRaises(SimulatedProcessLoss):
            self.executor(real).run()
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["records"]["1"]["status"], INTENT)
        with patch.object(real, "submit_video") as again, self.assertRaises(ExecutionError):
            self.executor(real).run()
        again.assert_not_called()

    def test_lost_id_when_post_submit_write_fails_blocks_resume(self):
        original = execution._atomic_write_text

        def fail_saved_id(path, text):
            data = json.loads(text)
            if data["records"]["1"]["status"] == SUBMITTED:
                raise OSError("TEST_ONLY_POST_SUBMIT_WRITE_FAILURE")
            original(path, text)

        real = RealBackend("TEST_ONLY_MODEL_A")
        with patch.object(real, "submit_video", return_value="TEST_ONLY_TASK_1") as submit, \
             patch.object(execution, "_atomic_write_text", side_effect=fail_saved_id), self.assertRaises(ExecutionError):
            self.executor(real).run()
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["records"]["1"]["status"], INTENT)
        with patch.object(real, "submit_video") as again, self.assertRaises(ExecutionError):
            self.executor(real).run()
        again.assert_not_called()

    def test_uncertain_result_write_failure_still_leaves_intent(self):
        original = execution._atomic_write_text

        def fail_uncertain(path, text):
            if json.loads(text)["records"]["1"]["status"] == UNCERTAIN:
                raise OSError("TEST_ONLY_DISK_FAILURE")
            original(path, text)

        backend = _FailingSubmitBackend()
        with patch.object(execution, "_atomic_write_text", side_effect=fail_uncertain), self.assertRaises(ExecutionError):
            self.executor(backend).run()
        self.assertEqual(backend.submit_count, 1)
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["records"]["1"]["status"], INTENT)
        second = MockBackend()
        with self.assertRaises(ExecutionError):
            self.executor(second).run()
        self.assertEqual(second.submit_count, 0)

    def test_missing_task_id_response_is_uncertain_and_not_resubmitted(self):
        backend = MockBackend()
        with patch.object(backend, "submit_video", return_value=None) as submit, self.assertRaises(ExecutionError):
            self.executor(backend).run()
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["records"]["1"]["status"], UNCERTAIN)
        with self.assertRaises(ExecutionError):
            self.executor(backend).run()
        self.assertEqual(backend.submit_count, 0)

    def test_existing_lock_is_not_removed_or_bypassed(self):
        lock = self.state.with_suffix(".json.lock")
        lock.write_text("TEST_ONLY_EXISTING_LOCK", encoding="utf-8")
        backend = MockBackend()
        with self.assertRaises(ExecutionError):
            self.executor(backend).run()
        self.assertEqual(backend.submit_count, 0)
        self.assertEqual(lock.read_text(encoding="utf-8"), "TEST_ONLY_EXISTING_LOCK")

    def test_state_is_reloaded_under_lock_before_resuming(self):
        first = self.executor(MockBackend())
        guard = _GuardingBackend()
        second = self.executor(guard)  # Both initially see no state file.
        first.run()
        self.assertEqual([r.status for r in second.run()], [DOWNLOADED, DOWNLOADED])
        self.assertEqual(guard.submit_count, 0)

    def test_changed_backend_model_stops_before_submit(self):
        real = RealBackend("TEST_ONLY_MODEL_A")
        executor = self.executor(real)
        real.model_id = "TEST_ONLY_MODEL_B"
        with patch.object(real, "submit_video") as submit, self.assertRaises(ExecutionError):
            executor.run()
        submit.assert_not_called()



if __name__ == "__main__":
    unittest.main()
