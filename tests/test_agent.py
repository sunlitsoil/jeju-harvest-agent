"""오프라인 재현 테스트: tests/fixtures 의 저장 응답(2026-11-02 수집)만 쓴다.

실행: python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["JEJU_AGENT_FIXTURES"] = str(ROOT / "tests" / "fixtures")
os.environ.pop("LLM_BASE_URL", None)
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
        self.assertIn(r.facts["signal"]["level"], ("유리", "보통", "주의"))

    def test_market_share_sums_to_100(self):
        ms = analysis.market_share("양배추", "양배추(일반)", asof=analysis.latest_date())
        total = sum(m["share_pct"] for m in ms["markets"])
        self.assertLessEqual(total, 100.01)

    def test_number_guard_rejects_new_numbers(self):
        base = "kg당 2,068원으로 +1.2% 움직였습니다."
        self.assertEqual(agent._numbers("2,068원 +1.2%") - agent._numbers(base), set())
        self.assertEqual(agent._numbers("3,000원") - agent._numbers(base), {"3000"})


if __name__ == "__main__":
    unittest.main()
