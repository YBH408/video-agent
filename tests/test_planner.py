import copy
import json
import unittest
from pathlib import Path
from unittest.mock import Mock

from agent.planner import Planner, PlanValidationError, parse_plan, requested_duration

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "examples" / "future_city_10s.json"
RAW = FIXTURE_PATH.read_text(encoding="utf-8")


class PlannerTests(unittest.TestCase):
    def test_acceptance_two_five_second_shots(self):
        llm = Mock(return_value=RAW)
        plan = Planner(llm).plan("生成一个10秒的未来城市科幻视频")
        self.assertEqual(plan.total_duration_seconds, 10)
        self.assertEqual(plan.shot_count, 2)
        self.assertEqual([shot.duration_seconds for shot in plan.shots], [5, 5])
        self.assertEqual(plan.aspect_ratio, "16:9")
        self.assertEqual(json.loads(RAW), plan.to_dict())
        system, user = llm.call_args.args
        self.assertIn("本次总时长严格为 10 秒", system)
        self.assertEqual(user, "生成一个10秒的未来城市科幻视频")
        llm.assert_called_once()

    def test_markdown_fence(self):
        self.assertEqual(parse_plan("```json\n" + RAW + "\n```").shot_count, 2)

    def test_invalid_text_and_duplicate_keys(self):
        cases = ["", "hello", "[]", "null", "true", "{}", RAW[:-5],
                 "这里是计划：" + RAW, RAW + " trailing",
                 "```json\n" + RAW + "\n```\n额外说明",
                 RAW.replace('"shot_count": 2', '"shot_count": 2, "shot_count": 2'),
                 RAW.replace('"shot_count": 2', '"shot_count": NaN'),
                 RAW.replace('"shot_count": 2', '"shot_count": Infinity')]
        for index, raw in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(PlanValidationError):
                parse_plan(raw)

    def test_invalid_fields(self):
        base = json.loads(RAW)
        edits = [
            ("missing style", lambda p: p.pop("video_style")),
            ("unexpected field", lambda p: p.update(extra="x")),
            ("empty style", lambda p: p.update(video_style="  ")),
            ("invalid style type", lambda p: p.update(video_style=[])),
            ("wrong ratio", lambda p: p.update(aspect_ratio="9:16")),
            ("bool count", lambda p: p.update(shot_count=True)),
            ("float count", lambda p: p.update(shot_count=2.0)),
            ("string count", lambda p: p.update(shot_count="2")),
            ("negative count", lambda p: p.update(shot_count=-1)),
            ("count mismatch", lambda p: p.update(shot_count=3)),
            ("shots not list", lambda p: p.update(shots={})),
            ("shots empty", lambda p: p.update(shots=[])),
            ("total mismatch", lambda p: p.update(total_duration_seconds=15)),
            ("total string", lambda p: p.update(total_duration_seconds="10")),
            ("total bool", lambda p: p.update(total_duration_seconds=True)),
            ("total zero", lambda p: p.update(total_duration_seconds=0)),
            ("nonmultiple total", lambda p: p.update(total_duration_seconds=12)),
            ("shot not object", lambda p: p["shots"].__setitem__(0, [])),
            ("missing prompt", lambda p: p["shots"][0].pop("video_prompt")),
            ("empty prompt", lambda p: p["shots"][0].update(video_prompt=" ")),
            ("nontext prompt", lambda p: p["shots"][0].update(video_prompt=9)),
            ("empty description", lambda p: p["shots"][0].update(description="")),
            ("duplicate ID", lambda p: p["shots"][1].update(id=1)),
            ("nonsequential ID", lambda p: p["shots"][0].update(id=9)),
            ("bool ID", lambda p: p["shots"][0].update(id=True)),
            ("float ID", lambda p: p["shots"][0].update(id=1.0)),
            ("wrong shot duration", lambda p: p["shots"][0].update(duration_seconds=10)),
            ("float shot duration", lambda p: p["shots"][0].update(duration_seconds=5.0)),
        ]
        for label, edit in edits:
            with self.subTest(case=label):
                data = copy.deepcopy(base)
                edit(data)
                with self.assertRaises(PlanValidationError):
                    parse_plan(json.dumps(data))

    def test_requirement_duration_mismatch(self):
        with self.assertRaises(PlanValidationError):
            Planner(Mock(return_value=RAW)).plan("生成一个15秒的视频")

    def test_nonmultiple_duration_fails_before_llm(self):
        for request in ["生成12秒的视频", "生成-10秒的视频", "生成0秒的视频", "生成10.5秒的视频", " "]:
            with self.subTest(request=request):
                llm = Mock(return_value=RAW)
                with self.assertRaises(PlanValidationError):
                    Planner(llm).plan(request)
                llm.assert_not_called()

    def test_explicit_duration_conflict_fails_before_llm(self):
        llm = Mock(return_value=RAW)
        with self.assertRaises(PlanValidationError):
            Planner(llm).plan("生成10秒的视频", expected_total_seconds=15)
        llm.assert_not_called()

    def test_duration_inference_does_not_guess_ambiguous_requests(self):
        self.assertEqual(requested_duration("时长为0.5分钟"), 30)
        self.assertIsNone(requested_duration("生成十秒的视频"))
        self.assertIsNone(requested_duration("总共10秒，每镜头5秒"))
        self.assertIsNone(requested_duration("每镜头5秒的城市视频"))
        self.assertIsNone(requested_duration("生成未来城市视频"))


if __name__ == "__main__":
    unittest.main()
