"""허브 전망 제공 품종 전부를 시장 지정 없이 돌려 결과를 센다(실음 / 시장 바꿈 / 전부 제외).
이상값 배수(기본 3)를 2·3·5로 바꿨을 때 결과도 함께 남긴다. 저장 응답(캐시)만 써도 된다."""
import json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from jeju_agent import analysis, tools  # noqa: E402
asof = analysis.latest_date()
from datetime import timedelta
avail = tools.forecast_items(analysis._s(asof - timedelta(days=7)), analysis._s(asof))
orig = analysis._forecast_ok
out = {"asof": asof.isoformat(), "by_factor": {}}
for k in (2, 3, 5):
    def ok(last, weeks, ref_price=None, k=k):
        if not last or not last.get("price") or last["price"] <= 0 or not weeks:
            return False
        if ref_price and not (ref_price / k <= last["price"] <= ref_price * k):
            return False
        return all(last["price"] / k <= w["price"] <= last["price"] * k for w in weeks)
    analysis._forecast_ok = ok
    rows = []
    for a in avail:
        st = analysis.recent_status(a["item"], a["cultivar"], asof)
        fc = analysis.forecast(a["item"], a["cultivar"], asof=asof, ref_price=st.get("cur_price"))
        kind = "실음" if fc["available"] and not fc["skipped"] else "시장 바꿈" if fc["available"] else "전부 제외"
        rows.append({"cultivar": a["cultivar"], "result": kind, "market": fc.get("market"), "skipped": fc.get("skipped")})
    out["by_factor"][k] = rows
analysis._forecast_ok = orig
for k, rows in out["by_factor"].items():
    c = {}
    for r in rows:
        c[r["result"]] = c.get(r["result"], 0) + 1
    print(k, c, [(r["cultivar"], r["result"], r["market"]) for r in rows])
if len(sys.argv) > 1:
    Path(sys.argv[1]).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
