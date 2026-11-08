"""계획기 평가: 질문 20개(작성자가 정답 품종·의도를 붙임)에 대해 규칙 계획기와 LLM 계획기를 비교한다.

실행: python3 eval/eval_planner.py            # 규칙만
      LLM_BASE_URL=... LLM_MODEL=... python3 eval/eval_planner.py   # 규칙 + LLM
품종 일치와 의도 집합 일치를 따로 센다. 목록 밖 작물(감귤 등)은 '도구를 안 부름'이 정답이다.
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from jeju_agent import agent  # noqa: E402

QS = json.loads((ROOT / "eval" / os.environ.get("EVAL_QS", "planner_questions.json")).read_text(encoding="utf-8"))


def rule(q):
    saved = os.environ.pop("LLM_MODEL", None)
    try:
        p = agent.plan(q)
    finally:
        if saved:
            os.environ["LLM_MODEL"] = saved
    return (p["target"][1] if p["target"] else None), sorted(p["intents"]), 0


def llm(q):
    t0 = time.time()
    p = agent.plan(q)
    ms = int((time.time() - t0) * 1000)
    if p["planner"] != "llm":
        return (p["target"][1] if p["target"] else None), sorted(p["intents"]), ms, "rule-fallback"
    return (p["target"][1] if p["target"] else None), sorted(p["intents"]), ms, "llm"


def score(name, fn):
    ok_c = ok_i = 0
    rows, total_ms = [], 0
    for x in QS:
        out = fn(x["q"])
        cv, it = out[0], out[1]
        total_ms += out[2] if len(out) > 2 else 0
        c = cv == x["cultivar"]
        i = it == sorted(x["intents"])
        ok_c += c
        ok_i += i
        rows.append({"q": x["q"], "want": [x["cultivar"], sorted(x["intents"])], "got": [cv, it], "cultivar_ok": c,
                     "intent_ok": i, "via": out[3] if len(out) > 3 else "rule"})
    n = len(QS)
    summary = {"planner": name, "n": n, "cultivar_acc": ok_c / n, "intent_acc": ok_i / n,
               "avg_ms": round(total_ms / n) if total_ms else None,
               "rule_fallbacks": sum(r["via"] == "rule-fallback" for r in rows)}
    return summary, rows


if __name__ == "__main__":
    out = {"model": os.environ.get("LLM_MODEL"), "date": time.strftime("%Y-%m-%d %H:%M")}
    out["rule"], out["rule_rows"] = score("rule", rule)
    if os.environ.get("LLM_MODEL"):
        out["llm"], out["llm_rows"] = score("llm", llm)
    print(json.dumps({k: v for k, v in out.items() if not k.endswith("rows")}, ensure_ascii=False, indent=1))
    for k in ("rule_rows", "llm_rows"):
        for r in out.get(k, []):
            if not (r["cultivar_ok"] and r["intent_ok"]):
                print(k, "틀림:", r["q"], "정답", r["want"], "결과", r["got"])
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
