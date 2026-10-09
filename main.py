"""Plan, preview and (opt-in) execute a storyboard.

This entry point plans by default and only submits video-generation tasks when
``--run`` is passed. Without ``--real`` the executor uses a fully offline mock,
so nothing is billed and no network request is sent.
"""
import argparse
import json
import sys
from pathlib import Path

from agent.executor import ExecutionError, Executor, MockBackend, RealBackend
from agent.planner import Planner, PlanValidationError, VideoPlan, parse_plan
from config import ConfigurationError, PROJECT_DIR, get_setting
from tools._http import APIError

RUN_DIR = PROJECT_DIR / "runs"


def planned_tool_calls(plan: VideoPlan) -> list[dict]:
    # Revalidate before producing an execution preview, including programmatic plans.
    checked = parse_plan(json.dumps(plan.to_dict(), ensure_ascii=False))
    return [
        {"tool": "tools.video_generator.generate_video", "shot_id": shot.id,
         "arguments": {"prompt": shot.video_prompt,
                       "duration_seconds": shot.duration_seconds,
                       "aspect_ratio": checked.aspect_ratio}}
        for shot in checked.shots
    ]


def _load_plan(args) -> VideoPlan:
    """Produce a validated plan from --input or from the LLM planner."""
    if args.input:
        try:
            raw = args.input.read_text(encoding="utf-8-sig")
        except OSError:
            raise OSError("无法读取 --input 指定的分镜文件。")
        return parse_plan(raw, expected_total_seconds=args.duration)
    requirement = args.prompt if args.prompt is not None else input("请输入视频需求：")
    if args.mock_response:
        raw = args.mock_response.read_text(encoding="utf-8-sig")
        planner = Planner(llm=lambda _system, _user: raw)
    else:
        planner = Planner()
    return planner.plan(requirement, expected_total_seconds=args.duration)


def _run_executor(plan: VideoPlan, args) -> int:
    if args.real:
        print("警告：即将使用真实 MiniMax 视频生成，可能产生费用。", file=sys.stderr)
        try:
            answer = input('输入 "yes" 确认启动真实生成：').strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("已取消。", file=sys.stderr)
            return 1
        if answer != "yes":
            print("已取消，未启动真实生成。", file=sys.stderr)
            return 1
        backend = RealBackend(get_setting("MINIMAX_VIDEO_MODEL_ID"))
        poll_interval = args.poll_interval
    else:
        backend = MockBackend()
        poll_interval = 0.0
        print("[mock] 使用离线模拟后端，不调用 MiniMax，不产生费用。")
    executor = Executor(
        plan,
        state_path=args.state,
        output_dir=args.output_dir,
        backend=backend,
        poll_interval=poll_interval,
        max_polls=args.max_polls,
    )
    records = executor.run()
    mode = "真实 MiniMax 生成（已确认）" if args.real else "模拟生成（零计费）"
    print(f"\n执行完成（{mode}）。")
    for record in records:
        print(f"镜头 {record.shot_id}: {record.status} task_id={record.task_id or '-'} "
              f"文件={record.download_path or '-'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="视频 Agent：分镜规划与可选执行（默认 Mock）")
    parser.add_argument("--prompt", help="自然语言视频需求；未提供时交互输入")
    parser.add_argument("--dry-run", action="store_true", help="仅检查本地计划或 Mock 响应；不执行、不调用 LLM")
    parser.add_argument("--mock-response", type=Path, help="读取模拟 LLM 文本，全程离线、无需 Key")
    parser.add_argument("--duration", type=int, help="明确总时长（秒），用于复杂时长需求校验")
    parser.add_argument("--output", type=Path, help="保存校验后的分镜 JSON 到新文件")
    parser.add_argument("--run", action="store_true", help="执行分镜生成（默认使用离线 Mock）")
    parser.add_argument("--input", type=Path, help="读取已校验的分镜 JSON，跳过 LLM 规划")
    parser.add_argument("--real", action="store_true", help="使用真实 MiniMax（需 --run，且需交互确认）")
    parser.add_argument("--state", type=Path,
                        help="状态路径；默认 runs/mock/state.json 或 runs/real/state.json")
    parser.add_argument("--output-dir", type=Path,
                        help="片段目录；默认 runs/mock/videos 或 runs/real/videos")
    parser.add_argument("--poll-interval", type=float, default=5.0, help="查询任务状态的间隔秒数")
    parser.add_argument("--max-polls", type=int, default=60, help="单个任务最多查询次数")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Reject conflicts before plan loading, credentials, state changes or requests.
    if args.run and args.dry_run:
        print("错误：--run 与 --dry-run 不能同时使用。", file=sys.stderr)
        return 1
    if args.real and not args.run:
        print("错误：--real 必须与 --run 一起使用。", file=sys.stderr)
        return 1
    if (args.dry_run or (args.run and not args.real)) and not (args.input or args.mock_response):
        print("错误：离线检查或 Mock 执行必须提供 --input 或 --mock-response。", file=sys.stderr)
        return 1
    mode = "real" if args.real else "mock"
    args.state = args.state or RUN_DIR / mode / "state.json"
    args.output_dir = args.output_dir or RUN_DIR / mode / "videos"
    try:
        plan = _load_plan(args)
        plan_json = json.dumps(plan.to_dict(), ensure_ascii=False, indent=2)
        if not args.run:
            calls = planned_tool_calls(plan)
            if args.output:
                # Exclusive creation avoids accidentally replacing configuration/source files.
                with args.output.open("x", encoding="utf-8") as output:
                    output.write(plan_json + "\n")
            print(plan_json)
            mode = ("本地分镜校验，零网络请求" if args.input else
                    "离线模拟 LLM，零网络请求" if args.mock_response else
                    "真实 LLM 规划；LLM 调用可能计费")
            print(f"\n[dry-run] {mode}；不提交图片或视频生成请求。")
            print(f"预计视频生成 API 提交次数：{len(calls)}；实际提交次数：0。")
            print("以下仅打印工具调用计划，不执行：")
            print(json.dumps(calls, ensure_ascii=False, indent=2))
            return 0
        print(plan_json)
        return _run_executor(plan, args)
    except (PlanValidationError, ConfigurationError, APIError, ExecutionError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1
    except OSError:
        print("错误：无法读取模拟文件或创建输出文件（已有文件不会覆盖）。", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("已取消。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())