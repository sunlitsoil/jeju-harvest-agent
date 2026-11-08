"""명령줄 실행: python3 cli.py "월동무 지금 내도 될까?" [--json]"""
import json
import sys

from jeju_agent.agent import answer


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--json"]
    q = " ".join(args) or input("질문> ")
    res = answer(q)
    if "--json" in sys.argv:
        print(json.dumps(res.to_dict(), ensure_ascii=False, indent=2, default=str))
        return 0
    if res.summary:
        print("■ AI가 고른 핵심 문장(답변 문장 그대로)\n" + res.summary + "\n")
    print("■ 답변\n" + res.answer)
    if res.evidence:
        print("\n■ 근거")
        for row in res.evidence:
            print("  - " + " | ".join(row))
    print("\n■ 에이전트 실행 기록 (" + ("LLM 계획" if res.planner == "llm" else "규칙 계획") + ")")
    for s in res.steps:
        print(f"  - {s.tool} {s.args if s.args else ''} {s.ms}ms {s.note}")
    if res.llm_rejected:
        print("  - AI 요약 폐기: " + res.llm_rejected)
    print("\n■ 유의")
    for c in res.caveats:
        print("  - " + c)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
