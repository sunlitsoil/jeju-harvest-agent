"""LLM 한 줄 요약 검사: 평가 질문 20개 전체 경로를 돌려 요약 채택·폐기 건수와 원문을 남긴다.
채택된 요약의 의미 귀속(어느 숫자가 어느 시장·기간 값인지)은 사람이 결과 파일을 읽고 판정한다."""
import json, os, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from jeju_agent import agent  # noqa: E402
QS = json.loads((ROOT / "eval" / os.environ.get("EVAL_QS", "planner_questions.json")).read_text(encoding="utf-8"))
rows = []
for x in QS:
    t0 = time.time()
    r = agent.answer(x["q"])
    rows.append({"q": x["q"], "planner": r.planner, "summary": r.summary, "picked": r.picked, "lines": r.lines, "rejected": r.llm_rejected,
                 "answer": r.answer, "sec": round(time.time() - t0, 2)})
out = {"model": os.environ.get("LLM_MODEL"), "date": time.strftime("%Y-%m-%d %H:%M"),
       "accepted": sum(bool(r["summary"]) for r in rows), "rejected": sum(bool(r["rejected"]) for r in rows),
       "rows": rows}
print(json.dumps({k: v for k, v in out.items() if k != "rows"}, ensure_ascii=False))
if len(sys.argv) > 1:
    Path(sys.argv[1]).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
