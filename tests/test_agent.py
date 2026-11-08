"""오프라인 재현 테스트: tests/fixtures 의 저장 응답(2026-11-02 수집, 전망·시장별 가격은 2026-11-08 수집)만 쓴다.

실행: python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["JEJU_AGENT_FIXTURES"] = str(ROOT / "tests" / "fixtures")
for k in ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "AGENT_PLANNER"):
    os.environ.pop(k, None)
sys.path.insert(0, str(ROOT))

from jeju_agent import agent, analysis  # noqa: E402


class ParseTest(unittest.TestCase):
    def test_item_and_intent(self):
        self.assertEqual(agent.parse("월동무 언제 내면 좋아?"), (("무", "월동무"), ["timing"]))
        self.assertEqual(agent.parse("당근 요즘 시세")[0], ("당근", "당근(일반)"))
        self.assertIn("market", agent.parse("양배추 어느 시장에 많이 가?")[1])

    def test_no_false_radish(self):
        self.assertIsNone(agent.parse("무엇을 심을까")[0])


class AnswerTest(unittest.TestCase):
    def test_out_of_season_falls_back_to_timing(self):
        r = agent.answer("월동무 지금 내도 될까?")
        self.assertIn("season", r.facts)
        self.assertTrue(r.facts["season"]["best"])
        # 물량 적은 끝물은 순위에서 빠져야 한다
        self.assertTrue(all(b["main_season"] for b in r.facts["season"]["best"]))

    def test_in_season_status_numbers_match_evidence(self):
        r = agent.answer("당근 요즘 시세 어때?")
        st = r.facts["status"]
        self.assertTrue(st["in_season"])
        self.assertIn(f"{st['cur_price']:,.0f}원", r.answer)
        self.assertIn(r.facts["signal"]["level"], ("오름세", "보합", "내림세"))

    def test_market_share_sums_to_100(self):
        ms = analysis.market_share("양배추", "양배추(일반)", asof=analysis.latest_date())
        total = sum(m["share_pct"] for m in ms["markets"])
        self.assertLessEqual(total, 100.01)

    def test_number_guard_rejects_new_numbers(self):
        base = "kg당 2,068원으로 +1.2% 움직였습니다."
        self.assertEqual(agent._numbers("2,068원 +1.2%") - agent._numbers(base), set())
        self.assertEqual(agent._numbers("3,000원") - agent._numbers(base), {"3000"})


class ForecastTest(unittest.TestCase):
    def test_forecast_quoted_with_source(self):
        r = agent.answer("당근 지금 내도 될까?")
        fc = r.facts["forecast"]
        self.assertTrue(fc["available"])
        self.assertEqual(len(fc["weeks"]), 4)
        self.assertIn("제주데이터허브 가격 전망", r.answer)
        self.assertIn(f"{fc['weeks'][-1]['price']:,.0f}원", r.answer)

    def test_forecast_not_offered(self):
        r = agent.answer("월동무 다음 주 전망은?")
        self.assertFalse(r.facts["forecast"]["available"])
        self.assertIn("제공하지 않습니다", r.answer)

    def test_implausible_forecast_guard(self):
        last = {"price": 1000.0}
        ok = [{"price": 950.0}, {"price": 1100.0}]
        self.assertTrue(analysis._forecast_ok(last, ok))
        self.assertFalse(analysis._forecast_ok({"price": 0.0}, ok))
        self.assertFalse(analysis._forecast_ok(last, [{"price": 20.0}]))
        self.assertFalse(analysis._forecast_ok({"price": 100.0}, [{"price": 110.0}], ref_price=3406.0))


class MarketTest(unittest.TestCase):
    def test_small_markets_not_ranked(self):
        mc = analysis.market_compare("양배추", "양배추(일반)", asof=analysis.latest_date())
        self.assertTrue(all(m["share_pct"] >= mc["min_share_pct"] for m in mc["ranked"]))
        prices = [m["avg_price"] for m in mc["ranked"]]
        self.assertEqual(prices, sorted(prices, reverse=True))

    def test_market_named_in_question(self):
        r = agent.answer("양배추 가락시장 시세 어때?")
        self.assertEqual(r.market, "서울가락도매")
        self.assertIn("market", r.intents)


def _fake_tool_calls(calls):
    import json as _j
    return lambda messages, specs: {"tool_calls": [
        {"function": {"name": n, "arguments": _j.dumps(a, ensure_ascii=False)}} for n, a in calls]}


class LlmPlannerTest(unittest.TestCase):
    def setUp(self):
        os.environ["LLM_MODEL"] = "fake"
        self._orig = agent._llm_tools

    def tearDown(self):
        agent._llm_tools = self._orig
        for k in ("LLM_MODEL", "AGENT_PLANNER"):
            os.environ.pop(k, None)

    def test_llm_mode_uses_llm_choice(self):
        os.environ["AGENT_PLANNER"] = "llm"
        agent._llm_tools = _fake_tool_calls([("recent_status", {"cultivar": "월동무"}), ("price_forecast", {"cultivar": "월동무"})])
        p = agent.plan("놈삐 이번 주에 내불카?")
        self.assertEqual(p["planner"], "llm")
        self.assertEqual(p["target"], ("무", "월동무"))
        self.assertEqual(sorted(p["intents"]), ["forecast", "status"])

    def test_unknown_cultivar_and_tool_dropped(self):
        os.environ["AGENT_PLANNER"] = "llm"
        agent._llm_tools = _fake_tool_calls([("recent_status", {"cultivar": "감귤"}), ("rm_rf", {"cultivar": "월동무"})])
        p = agent.plan("감귤 시세")
        self.assertEqual(p["planner"], "rule")
        self.assertIsNone(p["target"])

    def test_auto_mode_skips_llm_when_rule_finds_item(self):
        called = []
        agent._llm_tools = lambda m, s: called.append(1) or None
        p = agent.plan("당근 요즘 시세 어때?")
        self.assertEqual(p["planner"], "rule")
        self.assertEqual(called, [])


class PickGuardTest(unittest.TestCase):
    """AI 핵심 문장 고르기. 2026-11-08 자유 요약 평가에서 나온 두 오류(①2,068원을 서울가락도매 값으로 잘못 이음
    ②숫자는 맞고 이유를 잘못 이음)는 LLM이 문장을 새로 쓸 수 있어서 생겼다. 번호만 받으면 둘 다 생길 수 없다."""
    def setUp(self):
        self._orig = agent._llm

    def tearDown(self):
        agent._llm = self._orig

    def _res(self):
        r = agent.Result(question="당근 기다리면 오를까", cultivar="당근(일반)")
        r.lines = ["당근(일반) 최근 7거래일 평균 도매가는 kg당 2,068원입니다.",
                   "제주데이터허브 가격 전망은 서울가락도매 기준 1,794원입니다."]
        return r

    def test_picked_lines_are_verbatim(self):
        r = self._res()
        agent._llm = lambda *a, **k: "2"
        agent._polish(r, "")
        self.assertEqual(r.summary, r.lines[1])

    def test_rewritten_sentence_rejected(self):
        # 사례①: 숫자를 다른 시장에 붙인 문장을 돌려주면 버린다
        r = self._res()
        agent._llm = lambda *a, **k: "서울가락도매 당근은 kg당 2,068원입니다."
        agent._polish(r, "")
        self.assertEqual(r.summary, "")
        self.assertIn("번호가 아닌", r.llm_rejected)

    def test_out_of_range_rejected(self):
        r = self._res()
        agent._llm = lambda *a, **k: "1,7"
        agent._polish(r, "")
        self.assertEqual(r.summary, "")


class LlmTargetNoticeTest(unittest.TestCase):
    def test_llm_read_crop_is_disclosed(self):
        orig = agent.plan
        agent.plan = lambda q: {"planner": "llm", "steps": [], "target": ("무", "월동무"), "intents": ["status"],
                                "market": None, "llm_target": True}
        try:
            r = agent.answer("놈삐 시세 어떵?")
        finally:
            agent.plan = orig
        self.assertIn("AI가 「월동무」로 읽었습니다", r.lines[0])


if __name__ == "__main__":
    unittest.main()
