"""Execute validated shots with mode isolation and conservative recovery.

A persisted submission intent blocks resubmission after an uncertain outcome.
This is not an exactly-once billing guarantee or protection from state deletion.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from agent.planner import VideoPlan, parse_plan
from tools._http import MINIMAX_BASE_URL
from tools.video_generator import VIDEO_ASPECT_RATIO, VIDEO_DURATION_SECONDS, VIDEO_RESOLUTION

STATE_VERSION = 2
INTENT = "submission_intent"
SUBMITTED = "submitted"
SUCCEEDED = "succeeded"
DOWNLOADED = "downloaded"
FAILED = "failed"
UNCERTAIN = "submission_uncertain"
STAGES = {INTENT, SUBMITTED, SUCCEEDED, DOWNLOADED, FAILED, UNCERTAIN}


class ExecutionError(RuntimeError):
    """Execution cannot continue safely; no automatic resubmission."""


@dataclass(frozen=True)
class ExecutionIdentity:
    mode: str
    provider: str
    model_id: str
    api_base_url: str

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "provider": self.provider, "model_id": self.model_id,
                "api_base_url": self.api_base_url,
                "request_parameters": {"duration": VIDEO_DURATION_SECONDS,
                                       "resolution": VIDEO_RESOLUTION, "ratio": VIDEO_ASPECT_RATIO}}


@dataclass
class ShotRecord:
    shot_id: int
    status: str
    task_id: str | None = None
    video_url: str | None = None
    download_path: str | None = None


@dataclass
class RunState:
    plan_hash: str
    execution: dict[str, Any]
    records: dict[int, ShotRecord] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": STATE_VERSION, "plan_hash": self.plan_hash,
                "execution": self.execution,
                "records": {str(k): asdict(v) for k, v in sorted(self.records.items())}}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunState:
        if (type(data.get("schema_version")) is not int or data["schema_version"] != STATE_VERSION
                or not isinstance(data.get("execution"), dict)):
            raise ExecutionError("旧版或未知状态缺少可信执行模式，拒绝自动恢复；请保留原文件并人工核实任务。")
        if set(data) != {"schema_version", "plan_hash", "execution", "records"}:
            raise ExecutionError("任务状态字段异常，拒绝恢复。")
        fingerprint = data["plan_hash"]
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ExecutionError("任务状态缺少有效计划哈希，拒绝恢复。")
        raw_records = data["records"]
        if not isinstance(raw_records, dict):
            raise ExecutionError("任务状态 records 必须是对象。")
        records = {}
        for key, value in raw_records.items():
            if not isinstance(key, str) or not re.fullmatch(r"[1-9][0-9]*", key):
                raise ExecutionError("任务状态镜头编号异常。")
            shot_id = int(key)
            if (not isinstance(value, dict) or set(value) != set(ShotRecord.__dataclass_fields__)
                    or type(value["shot_id"]) is not int or value["shot_id"] != shot_id
                    or not isinstance(value["status"], str) or value["status"] not in STAGES):
                raise ExecutionError("任务状态镜头记录异常，拒绝恢复。")
            record = ShotRecord(**value)
            for item in (record.task_id, record.video_url, record.download_path):
                if item is not None and (not isinstance(item, str) or not item.strip()):
                    raise ExecutionError("任务状态 ID 或路径格式异常。")
            if record.status in {INTENT, UNCERTAIN}:
                if any(item is not None for item in (record.task_id, record.video_url, record.download_path)):
                    raise ExecutionError("未确认的提交状态包含冲突数据。")
            elif not record.task_id:
                raise ExecutionError("已提交状态缺少任务 ID，禁止重新提交。")
            if record.status in {SUCCEEDED, DOWNLOADED}:
                if not record.video_url:
                    raise ExecutionError("成功状态缺少下载地址。")
            elif record.video_url is not None:
                raise ExecutionError("任务状态与下载地址不一致。")
            if (record.status == DOWNLOADED) != (record.download_path is not None):
                raise ExecutionError("任务状态与下载路径不一致。")
            records[shot_id] = record
        return cls(fingerprint, data["execution"], records)


def plan_hash(plan: VideoPlan) -> str:
    payload = json.dumps(plan.to_dict(), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate state field")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError("invalid state constant")


def _atomic_write_text(path: Path, text: str) -> None:
    """Flush a unique owned temporary file before atomic replacement."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as output:
            tmp = Path(output.name)
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, path)
    finally:
        if tmp is not None and tmp.exists():
            tmp.unlink()  # Only this call's newly created temporary file.


class MockBackend:
    """Offline backend writing text placeholders, not playable video."""

    def __init__(self) -> None:
        self.submit_count = 0
        self.query_count = 0

    def execution_identity(self) -> ExecutionIdentity:
        return ExecutionIdentity("mock", "local-mock", "mock-v1", "local")

    def submit_video(self, prompt, *, duration_seconds, aspect_ratio, shot_id) -> str:
        self.submit_count += 1
        return f"mock-task-{shot_id}"

    def query_video(self, task_id: str) -> dict[str, Any]:
        self.query_count += 1
        return {"status": "succeeded", "content": {"url": f"mock://video/{task_id}.mp4"}}

    def download_video(self, url: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"mock video clip\n")


class RealBackend:
    """Use the same explicit model ID for state identity and submission."""

    def __init__(self, model_id: str) -> None:
        if not isinstance(model_id, str) or not model_id.strip():
            raise ExecutionError("真实后端需要明确的视频模型 ID。")
        self.model_id = model_id.strip()

    def execution_identity(self) -> ExecutionIdentity:
        return ExecutionIdentity("real", "minimax", self.model_id, MINIMAX_BASE_URL)

    def submit_video(self, prompt, *, duration_seconds, aspect_ratio, shot_id) -> str:
        from tools.video_generator import generate_video
        return generate_video(prompt, duration_seconds=duration_seconds, aspect_ratio=aspect_ratio,
                              model_id=self.model_id)

    def query_video(self, task_id: str) -> dict[str, Any]:
        from tools.video_generator import query_video
        return query_video(task_id)

    def download_video(self, url: str, dest: Path) -> None:
        _download_bytes(url, dest)


def _download_bytes(url: str, dest: Path) -> None:
    try:
        import requests
    except ImportError:
        raise ExecutionError("下载视频需要安装 requests：pip install -r requirements.txt") from None
    try:
        response = requests.get(url, timeout=180)
        response.raise_for_status()
        content = response.content
    except requests.RequestException:
        raise ExecutionError("视频下载失败，保留成功任务状态，可人工核实后恢复下载。") from None
    if not content:
        raise ExecutionError("视频下载内容为空。")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)


class Executor:
    def __init__(self, plan: VideoPlan, *, state_path: Path, output_dir: Path, backend,
                 poll_interval: float = 5.0, max_polls: int = 60) -> None:
        self.plan = parse_plan(json.dumps(plan.to_dict(), ensure_ascii=False))
        self.state_path = Path(state_path)
        self.output_dir = Path(output_dir)
        self.backend = backend
        self.poll_interval = poll_interval
        self.max_polls = max_polls
        identity = backend.execution_identity()
        if (not isinstance(identity, ExecutionIdentity) or identity.mode not in {"mock", "real"}
                or any(not isinstance(v, str) or not v.strip()
                       for v in (identity.provider, identity.model_id, identity.api_base_url))):
            raise ExecutionError("执行后端身份无效。")
        self._execution = identity.to_dict()
        self._hash = plan_hash(self.plan)
        self.state = self._load_state()

    def _validate_record(self, record: ShotRecord) -> None:
        if self._execution["mode"] == "real":
            if record.task_id and record.task_id.lower().startswith("mock"):
                raise ExecutionError("真实模式拒绝 Mock 任务 ID。")
            if record.video_url:
                url = urlsplit(record.video_url)
                if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
                    raise ExecutionError("真实模式下载地址无效或属于 Mock。")
        else:
            if record.task_id and not record.task_id.lower().startswith("mock"):
                raise ExecutionError("Mock 模式拒绝非 Mock 任务 ID。")
            if record.video_url and urlsplit(record.video_url).scheme != "mock":
                raise ExecutionError("Mock 模式拒绝真实下载地址。")
        if record.status == DOWNLOADED:
            dest = self.output_dir / f"shot_{record.shot_id}.mp4"
            if Path(record.download_path).resolve() != dest.resolve() or not dest.is_file() or dest.stat().st_size == 0:
                raise ExecutionError("已下载文件缺失或输出目录不匹配，禁止自动提交替代任务。")
            if self._execution["mode"] == "real":
                with dest.open("rb") as clip:
                    if clip.read(32).startswith(b"mock video clip"):
                        raise ExecutionError("真实模式拒绝 Mock 占位 MP4。")

    def _load_state(self) -> RunState:
        try:
            info = os.lstat(self.state_path)
        except FileNotFoundError:
            return RunState(self._hash, self._execution)
        except OSError:
            raise ExecutionError("无法检查状态文件，禁止当作新任务执行。") from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ExecutionError("拒绝通过链接或重解析点读取或修改任务状态。")
        if not stat.S_ISREG(info.st_mode):
            raise ExecutionError("状态路径不是普通文件。")
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"),
                              object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
            if not isinstance(data, dict):
                raise ValueError("state must be an object")
            state = RunState.from_dict(data)
            if json.dumps(state.execution, sort_keys=True) != json.dumps(self._execution, sort_keys=True):
                raise ExecutionError("状态执行模式、服务商、模型或关键请求参数不匹配，拒绝恢复。")
            if state.plan_hash != self._hash:
                raise ExecutionError("状态属于另一个分镜计划，拒绝恢复。")
            ids = sorted(state.records)
            if ids != list(range(1, len(ids) + 1)) or len(ids) > self.plan.shot_count:
                raise ExecutionError("任务状态镜头序列不完整，拒绝恢复。")
            if any(state.records[i].status != DOWNLOADED for i in ids[:-1]):
                raise ExecutionError("任务状态执行顺序异常，拒绝恢复。")
            for record in state.records.values():
                self._validate_record(record)
            return state
        except (ValueError, OSError, RecursionError):
            raise ExecutionError("任务状态损坏或无法读取，保留原文件并停止执行。") from None

    def _save(self, record: ShotRecord) -> None:
        updated = RunState(self._hash, self._execution, {**self.state.records, record.shot_id: record})
        try:
            _atomic_write_text(self.state_path, json.dumps(updated.to_dict(), ensure_ascii=False, indent=2) + "\n")
        except OSError:
            raise ExecutionError("任务状态写入失败，停止执行；保留已有提交意图并人工核实。") from None
        self.state = updated

    @contextmanager
    def _run_lock(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.state_path.with_suffix(self.state_path.suffix + ".lock")
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise ExecutionError("状态文件已有执行锁；可能仍在运行或曾中断，需人工核实，不能自动删除锁。") from None
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(str(os.getpid()))
                output.flush()
                os.fsync(output.fileno())
            yield
        finally:
            lock.unlink()  # Only a lock exclusively created by this run.

    def run(self) -> list[ShotRecord]:
        with self._run_lock():
            if self.backend.execution_identity().to_dict() != self._execution:
                raise ExecutionError("执行后端身份发生变化，停止执行。")
            self.state = self._load_state()  # Re-read under lock, never use a stale snapshot.
            if any(r.status in {INTENT, UNCERTAIN, FAILED} for r in self.state.records.values()):
                raise ExecutionError("存在提交意图、未知结果或失败状态，请人工核实；禁止自动重提交。")
            for shot in self.plan.shots:
                dest = self.output_dir / f"shot_{shot.id}.mp4"
                record = self.state.records.get(shot.id)
                if dest.exists() and (record is None or record.status != DOWNLOADED):
                    raise ExecutionError("输出文件已存在但缺少匹配的完成状态，停止执行，不覆盖文件。")
            for shot in self.plan.shots:
                self._process_shot(shot)
            return [self.state.records[shot.id] for shot in self.plan.shots]

    def _process_shot(self, shot) -> None:
        record = self.state.records.get(shot.id)
        if record is not None and record.status == DOWNLOADED:
            return
        if record is None:
            self._submit(shot)
            record = self.state.records[shot.id]
        if record.status != SUCCEEDED:
            self._poll(record)
            record = self.state.records[shot.id]
        if record.status == SUCCEEDED:
            self._download(record)

    def _submit(self, shot) -> None:
        self._save(ShotRecord(shot.id, INTENT))  # Must succeed before any submit call.
        try:
            task_id = self.backend.submit_video(shot.video_prompt, duration_seconds=shot.duration_seconds,
                                                aspect_ratio=self.plan.aspect_ratio, shot_id=shot.id)
        except Exception:
            self._save(ShotRecord(shot.id, UNCERTAIN))
            raise ExecutionError("提交结果未知，停止自动重试；请人工核实服务商是否已创建任务。") from None
        # BaseException / process termination leaves the persisted intent intact.
        if not isinstance(task_id, str) or not task_id.strip():
            self._save(ShotRecord(shot.id, UNCERTAIN))
            raise ExecutionError("提交未返回有效 ID，停止自动重试。")
        record = ShotRecord(shot.id, SUBMITTED, task_id=task_id.strip())
        self._validate_record(record)
        self._save(record)  # Failure leaves INTENT on disk, preventing resubmission.

    def _poll(self, record: ShotRecord) -> None:
        for attempt in range(self.max_polls):
            try:
                task = self.backend.query_video(record.task_id)
            except Exception:
                task = None
            if isinstance(task, dict) and isinstance(task.get("status"), str):
                status = task["status"]
                if status == "succeeded":
                    content = task.get("content")
                    url = content.get("url") if isinstance(content, dict) else None
                    if not isinstance(url, str) or not url.strip():
                        raise ExecutionError("任务成功但缺少下载地址，停止执行。")
                    completed = ShotRecord(record.shot_id, SUCCEEDED, task_id=record.task_id, video_url=url.strip())
                    self._validate_record(completed)
                    self._save(completed)
                    return
                if status in {"failed", "cancelled"}:
                    self._save(ShotRecord(record.shot_id, FAILED, task_id=record.task_id))
                    raise ExecutionError("视频任务失败或取消，禁止自动重新提交。")
            if attempt + 1 < self.max_polls:
                time.sleep(self.poll_interval)
        raise ExecutionError("查询次数已耗尽，保留原任务 ID；恢复时继续查询，不重新提交。")

    def _download(self, record: ShotRecord) -> None:
        dest = self.output_dir / f"shot_{record.shot_id}.mp4"
        if dest.exists():
            raise ExecutionError("输出文件已存在，停止下载，不覆盖已有内容。")
        try:
            self.backend.download_video(record.video_url, dest)
            completed = ShotRecord(record.shot_id, DOWNLOADED, task_id=record.task_id,
                                   video_url=record.video_url, download_path=str(dest))
            self._validate_record(completed)
        except ExecutionError:
            raise
        except Exception:
            raise ExecutionError("下载失败，保留成功任务状态，不重新提交。") from None
        self._save(completed)
