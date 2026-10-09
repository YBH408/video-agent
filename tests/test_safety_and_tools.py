import contextlib
import importlib
import io
import json
import socket
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import main
from agent.planner import parse_plan
from tools import _http, image_generator, llm, video_generator

FIXTURE = Path(__file__).resolve().parents[1] / "examples" / "future_city_10s.json"


class SafetyTests(unittest.TestCase):
    def test_imports_do_not_read_settings_or_request_network(self):
        with patch("config.get_setting", side_effect=AssertionError("settings access")), \
             patch.object(_http, "_request", side_effect=AssertionError("network access")), \
             patch.object(socket.socket, "connect", side_effect=AssertionError("network access")):
            for name in ["tools.llm", "tools.image_generator", "tools.video_generator",
                         "agent.planner", "agent.executor", "main"]:
                importlib.reload(importlib.import_module(name))

    def test_offline_dry_run_never_calls_apis_or_reads_credentials(self):
        output = io.StringIO()
        with patch.object(_http, "_request", side_effect=AssertionError("network")) as network, \
             patch.object(socket.socket, "connect", side_effect=AssertionError("network")), \
             patch.object(llm, "get_setting", side_effect=AssertionError("credentials")) as settings, \
             patch.object(video_generator, "generate_video", side_effect=AssertionError("video")) as video, \
             patch.object(image_generator, "generate_image", side_effect=AssertionError("image")) as image, \
             contextlib.redirect_stdout(output):
            status = main.main(["--dry-run", "--mock-response", str(FIXTURE),
                                "--prompt", "生成一个10秒的未来城市科幻视频"])
        self.assertEqual(status, 0)
        result = output.getvalue()
        plan, _ = json.JSONDecoder().raw_decode(result)
        self.assertEqual(plan["shot_count"], 2)
        self.assertIn("预计视频生成 API 提交次数：2；实际提交次数：0", result)
        self.assertEqual(result.count('"tool": "tools.video_generator.generate_video"'), 2)
        for mocked in [network, settings, video, image]:
            mocked.assert_not_called()

    def test_default_entry_also_never_generates_video(self):
        with patch.object(llm, "generate_text", return_value=FIXTURE.read_text(encoding="utf-8")), \
             patch("agent.planner.generate_text", return_value=FIXTURE.read_text(encoding="utf-8")) as model, \
             patch.object(video_generator, "generate_video") as video, \
             patch.object(image_generator, "generate_image") as image, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main.main(["--prompt", "生成10秒的视频"]), 0)
        model.assert_called_once()
        video.assert_not_called()
        image.assert_not_called()

    def test_invalid_plan_has_no_execution_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory) / "invalid.json"
            bad.write_text('{"shots": []}', encoding="utf-8")
            output, error = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                self.assertEqual(main.main(["--prompt", "生成10秒视频", "--dry-run", "--mock-response", str(bad)]), 1)
            self.assertNotIn('"tool"', output.getvalue())
            self.assertIn("错误", error.getvalue())

    def test_output_is_valid_json_and_existing_file_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "plan.json"
            arguments = ["--prompt", "生成10秒的视频", "--dry-run", "--mock-response", str(FIXTURE), "--output", str(destination)]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main.main(arguments), 0)
            original = destination.read_text(encoding="utf-8")
            self.assertEqual(parse_plan(original).shot_count, 2)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main.main(arguments), 1)
            self.assertEqual(destination.read_text(encoding="utf-8"), original)

    def test_execution_flag_conflicts_fail_before_plan_or_state_changes(self):
        cases = [["--run", "--dry-run"], ["--run", "--real", "--dry-run"], ["--real"], ["--real", "--dry-run"]]
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            state.write_text("TEST_ONLY_EXISTING_STATE", encoding="utf-8")
            for flags in cases:
                with self.subTest(flags=flags), \
                     patch.object(main, "_load_plan", side_effect=AssertionError("planning must not start")) as load, \
                     patch.object(main, "_run_executor", side_effect=AssertionError("execution must not start")) as execute, \
                     patch.object(main, "get_setting", side_effect=AssertionError("config must not be read")) as settings, \
                     patch.object(_http, "_request", side_effect=AssertionError("no request")) as request, \
                     contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as error:
                    result = main.main([*flags, "--state", str(state), "--prompt", "生成10秒视频"])
                    self.assertEqual(result, 1)
                    self.assertIn("错误", error.getvalue())
                    self.assertEqual(output.getvalue(), "")
                    for mocked in (load, execute, settings, request):
                        mocked.assert_not_called()
                    self.assertEqual(state.read_text(encoding="utf-8"), "TEST_ONLY_EXISTING_STATE")

    def test_dry_run_and_mock_run_require_local_input_before_llm(self):
        for flags in (["--dry-run"], ["--run"]):
            with self.subTest(flags=flags), patch.object(main, "_load_plan") as load, \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main.main([*flags, "--prompt", "生成10秒视频"]), 1)
                load.assert_not_called()

    def test_default_mock_and_real_paths_are_separate(self):
        paths = []
        for real in (False, True):
            flags = ["--run", "--input", str(FIXTURE)] + (["--real"] if real else [])
            with patch("builtins.input", return_value="yes"), \
                 patch.object(main, "get_setting", return_value="TEST_ONLY_MODEL_A"), \
                 patch.object(main, "Executor") as executor, \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                executor.return_value.run.return_value = []
                self.assertEqual(main.main(flags), 0)
                paths.append(executor.call_args.kwargs)
        self.assertEqual(paths[0]["state_path"], main.RUN_DIR / "mock" / "state.json")
        self.assertEqual(paths[1]["state_path"], main.RUN_DIR / "real" / "state.json")
        self.assertNotEqual(paths[0]["output_dir"], paths[1]["output_dir"])



class ToolTests(unittest.TestCase):
    def settings(self, name):
        # Synthetic test data only; no environment or .env is read.
        return "TEST_ONLY_" + name

    def test_seed_request_reuses_existing_protocol(self):
        with patch.object(llm, "get_setting", side_effect=self.settings), \
             patch.object(llm, "post", return_value={"choices": [{"message": {"content": "{}"}}]}) as request:
            self.assertEqual(llm.generate_text("system", "user"), "{}")
        endpoint, _, payload = request.call_args.args
        self.assertEqual(endpoint, "https://ark.cn-beijing.volces.com/api/v3/chat/completions")
        self.assertEqual(payload["model"], "TEST_ONLY_SEED_MODEL_ID")
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["temperature"], 0)
        self.assertEqual(payload["max_tokens"], 4096)

    def test_seed_rejects_bad_response(self):
        for response in [{}, {"choices": []}, {"choices": [{"message": {"content": None}}]},
                         {"choices": [{"message": {"content": " "}}]}]:
            with self.subTest(response=response), patch.object(llm, "get_setting", side_effect=self.settings), \
                 patch.object(llm, "post", return_value=response), self.assertRaises(_http.APIError):
                llm.generate_text("system", "user")

    def test_seedream_request_reuses_existing_protocol(self):
        with patch.object(image_generator, "get_setting", side_effect=self.settings), \
             patch.object(image_generator, "post", return_value={"data": [{"url": "https://example.invalid/image"}]}) as request:
            self.assertEqual(image_generator.generate_image("scene"), "https://example.invalid/image")
        endpoint, _, payload = request.call_args.args
        self.assertEqual(endpoint, "https://ark.cn-beijing.volces.com/api/v3/images/generations")
        self.assertEqual(payload, {"model": "TEST_ONLY_SEEDREAM_MODEL_ID", "prompt": "scene", "size": "2K",
                                  "response_format": "url", "sequential_image_generation": "disabled", "stream": False})

    def test_minimax_request_and_both_existing_task_id_shapes(self):
        for response in [{"task_id": "mock-task"}, {"task": {"id": "mock-task"}}]:
            with self.subTest(response=response), patch.object(video_generator, "get_setting", side_effect=self.settings), \
                 patch.object(video_generator, "post", return_value=response) as request:
                self.assertEqual(video_generator.generate_video("scene"), "mock-task")
            endpoint, _, payload = request.call_args.args
            self.assertEqual(endpoint, "https://api.minimax.cn/v2/video_generation")
            self.assertEqual(payload, {"model": "TEST_ONLY_MINIMAX_VIDEO_MODEL_ID", "content": [{"type": "text", "text": "scene"}],
                                      "duration": 5, "resolution": "768P", "ratio": "16:9"})

    def test_bad_video_arguments_fail_before_api(self):
        cases = [{"prompt": ""}, {"prompt": "scene", "duration_seconds": 10},
                 {"prompt": "scene", "duration_seconds": True}, {"prompt": "scene", "aspect_ratio": "9:16"}]
        with patch.object(video_generator, "post") as request, patch.object(video_generator, "get_setting") as setting:
            for arguments in cases:
                with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                    video_generator.generate_video(**arguments)
            request.assert_not_called()
            setting.assert_not_called()

    def test_query_reuses_existing_fields_and_does_not_resubmit(self):
        task = {"status": "succeeded", "content": {"url": "https://example.invalid/video"}}
        with patch.object(video_generator, "get_setting", side_effect=self.settings), \
             patch.object(video_generator, "get", return_value={"task": task}) as request, \
             patch.object(video_generator, "post") as submit:
            self.assertEqual(video_generator.query_video("mock-task"), task)
        self.assertEqual(request.call_args.args[0], "https://api.minimax.cn/v2/query/video_generation/mock-task")
        submit.assert_not_called()

    def test_bad_tool_responses(self):
        for module, function, responses in [
            (image_generator, image_generator.generate_image, [{}, {"data": []}, {"data": [{"url": None}]}]),
            (video_generator, video_generator.generate_video, [{}, {"task": {}}, {"task_id": []}]),
        ]:
            for response in responses:
                with self.subTest(function=function.__name__, response=response), \
                     patch.object(module, "get_setting", side_effect=self.settings), \
                     patch.object(module, "post", return_value=response), self.assertRaises(_http.APIError):
                    function("scene")
        for response in [{}, {"task": {}}, {"task": {"status": "succeeded"}}]:
            with self.subTest(query=response), patch.object(video_generator, "get_setting", side_effect=self.settings), \
                 patch.object(video_generator, "get", return_value=response), self.assertRaises(_http.APIError):
                video_generator.query_video("mock-task")

    def test_http_timeout_authorization_and_sanitized_error(self):
        class FakeRequestError(Exception):
            pass

        response = Mock()
        response.status_code = 200
        response.json.return_value = {"ok": True}
        fake_requests = types.SimpleNamespace(post=Mock(return_value=response), get=Mock(return_value=response), RequestException=FakeRequestError)
        with patch.dict(sys.modules, {"requests": fake_requests}):
            self.assertEqual(_http.post("https://example.invalid", "TEST_ONLY", {"x": 1}), {"ok": True})
            self.assertEqual(fake_requests.post.call_args.kwargs["timeout"], 180)
            self.assertEqual(fake_requests.post.call_args.kwargs["headers"], {"Authorization": "Bearer TEST_ONLY"})
            _http.get("https://example.invalid", "TEST_ONLY")
            self.assertEqual(fake_requests.get.call_args.kwargs["timeout"], 90)
            fake_requests.post.side_effect = FakeRequestError("sensitive-response-content")
            with self.assertRaises(_http.APIError) as caught:
                _http.post("https://example.invalid", "TEST_ONLY", {})
            self.assertNotIn("sensitive-response-content", str(caught.exception))

    def test_explicit_model_id_matches_request_without_model_setting_lookup(self):
        with patch.object(video_generator, "get_setting", side_effect=self.settings) as settings, \
             patch.object(video_generator, "post", return_value={"task_id": "TEST_ONLY_TASK_1"}) as request:
            video_generator.generate_video("scene", model_id="TEST_ONLY_PINNED_MODEL")
        self.assertEqual(request.call_args.args[2]["model"], "TEST_ONLY_PINNED_MODEL")
        self.assertEqual([call.args[0] for call in settings.call_args_list], ["MINIMAX_API_KEY"])

    def test_billable_post_redirect_is_not_followed_or_retried(self):
        response = Mock(status_code=307)
        fake = types.SimpleNamespace(post=Mock(return_value=response), RequestException=RuntimeError)
        with patch.dict(sys.modules, {"requests": fake}), self.assertRaises(_http.APIError):
            _http.post("https://example.invalid", "TEST_ONLY", {})
        fake.post.assert_called_once()
        self.assertFalse(fake.post.call_args.kwargs["allow_redirects"])
        response.json.assert_not_called()

    def test_billable_post_exception_is_not_retried(self):
        fake = types.SimpleNamespace(post=Mock(side_effect=RuntimeError("TEST_ONLY_LOST_RESPONSE")), RequestException=RuntimeError)
        with patch.dict(sys.modules, {"requests": fake}), self.assertRaises(_http.APIError):
            _http.post("https://example.invalid", "TEST_ONLY", {})
        fake.post.assert_called_once()



if __name__ == "__main__":
    unittest.main()
