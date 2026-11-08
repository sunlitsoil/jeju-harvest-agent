"""도구가 가져온 원자료로 판단 근거를 계산한다. LLM 없이 결정적으로 동작한다."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from statistics import mean, median

from . import tools


def _d(s: str) -> date:
    return datetime.strptime(s, "%Y%m%d").date()


def _s(d: date) -> str:
    return d.strftime("%Y%m%d")


def _wavg(rows: list[dict]) -> float | None:
    """거래물량 가중 평균 가격. 물량이 0이면 단순 평균."""
    rows = [r for r in rows if r.get("wholesalePrice") is not None]
    if not rows:
        return None
    vol = sum(r.get("transactionVolume") or 0 for r in rows)
    if vol <= 0:
        return mean(r["wholesalePrice"] for r in rows)
    return sum(r["wholesalePrice"] * (r.get("transactionVolume") or 0) for r in rows) / vol


def _pct(a: float | None, b: float | None) -> float | None:
    if a is None or b in (None, 0):
        return None
    return (a - b) / b * 100


def latest_date() -> date:
    return _d(tools.data_period()["endDate"])


def recent_status(item: str, cultivar: str, asof: date | None = None, window: int = 7) -> dict:
    """최근 거래일 window일과 그 직전 window일, 작년 같은 시기를 비교한다."""
    asof = asof or latest_date()
    rows = tools.daily_stats(item, cultivar, _s(asof - timedelta(days=60)), _s(asof))
    rows = sorted(rows, key=lambda r: r["baseDate"])
    out = {"item": item, "cultivar": cultivar, "asof": asof.isoformat(), "window": window,
           "daily": rows[-30:]}
    if len(rows) < 3 or _d(rows[-1]["baseDate"]) < asof - timedelta(days=14):
        out["in_season"] = False
        out["last_trade"] = rows[-1]["baseDate"] if rows else None
        return out
    out["in_season"] = True
    cur, prev = rows[-window:], rows[-2 * window:-window]
    out["cur_from"], out["cur_to"] = cur[0]["baseDate"], cur[-1]["baseDate"]
    out["cur_price"] = _wavg(cur)
    out["prev_price"] = _wavg(prev) if prev else None
    out["price_chg_pct"] = _pct(out["cur_price"], out["prev_price"])
    out["cur_vol_per_day"] = mean(r["transactionVolume"] for r in cur)
    out["prev_vol_per_day"] = mean(r["transactionVolume"] for r in prev) if prev else None
    out["vol_chg_pct"] = _pct(out["cur_vol_per_day"], out["prev_vol_per_day"])
    # 작년 같은 날짜 구간
    ly_from = _d(cur[0]["baseDate"]) - timedelta(days=364)
    ly_to = _d(cur[-1]["baseDate"]) - timedelta(days=364)
    ly = tools.daily_stats(item, cultivar, _s(ly_from), _s(ly_to))
    out["ly_from"], out["ly_to"] = ly_from.isoformat(), ly_to.isoformat()
    out["ly_price"] = _wavg(ly)
    out["ly_days"] = len(ly)
    out["yoy_pct"] = _pct(out["cur_price"], out["ly_price"])
    # 최근 5년 같은 달 평균(월평균 가격의 평균)
    m = _d(cur[-1]["baseDate"]).month
    hist = tools.monthly_price(item, cultivar, f"{asof.year - 5}0101", _s(asof))
    same_month = [p["y"] for p in hist if int(p["x"][4:6]) == m and int(p["x"][:4]) < _d(cur[-1]["baseDate"]).year]
    out["month_avg_5y"] = mean(same_month) if same_month else None
    out["month_avg_n"] = len(same_month)
    out["vs_5y_pct"] = _pct(out["cur_price"], out["month_avg_5y"])
    return out


HALF_MONTH_LABEL = lambda m, h: f"{m}월 {'전반' if h == 0 else '후반'}"


def season_profile(item: str, cultivar: str, asof: date | None = None, years: int = 5,
                   season_start_month: int = 10) -> dict:
    """지난 시즌들을 반달 단위로 나눠 평균 가격·물량을 본다(언제 내면 값이 좋았나)."""
    asof = asof or latest_date()
    last_start_year = asof.year if asof.month >= season_start_month else asof.year - 1
    seasons = []
    for y in range(last_start_year - years, last_start_year):
        start = date(y, season_start_month, 1)
        end = date(y + 1, season_start_month, 1) - timedelta(days=1)
        rows = tools.daily_stats(item, cultivar, _s(start), _s(end))
        if rows:
            seasons.append((y, rows))
    buckets: dict[tuple[int, int, int], dict] = {}
    for y, rows in seasons:
        for r in rows:
            d = _d(r["baseDate"])
            # 시즌 내 순서를 유지하기 위한 정렬 키
            order = (d.year - y) * 12 + d.month
            k = (order, d.month, 0 if d.day <= 15 else 1)
            b = buckets.setdefault(k, {"by_year": {}})
            b["by_year"].setdefault(y, []).append(r)
    profile = []
    for (order, mth, half), b in sorted(buckets.items()):
        year_prices = {y: _wavg(rs) for y, rs in b["by_year"].items()}
        year_vols = {y: sum(r["transactionVolume"] for r in rs) / max(len(rs), 1) for y, rs in b["by_year"].items()}
        vals = [v for v in year_prices.values() if v is not None]
        profile.append({
            "label": HALF_MONTH_LABEL(mth, half),
            "years": len(vals),
            "avg_price": median(vals) if vals else None,  # 이상 시즌(예: 흉작 해) 영향을 줄이려 중앙값
            "avg_vol_per_day": mean(year_vols.values()) if year_vols else None,
            "by_year": {str(y): round(v) for y, v in year_prices.items() if v is not None},
        })
    # 3개 시즌 이상 거래가 있고, 물량이 본격 출하기(하루 거래량 중앙값의 25% 이상)인 구간만 순위 비교에 쓴다.
    # 끝물처럼 물량이 아주 적은 때는 값이 높아도 농가 전체가 그때 낼 수는 없기 때문이다.
    vols = [p["avg_vol_per_day"] for p in profile if p["avg_vol_per_day"]]
    vol_floor = median(vols) * 0.25 if vols else 0
    for p in profile:
        p["main_season"] = bool(p["avg_vol_per_day"] and p["avg_vol_per_day"] >= vol_floor)
    solid = [p for p in profile if p["years"] >= min(3, len(seasons)) and p["avg_price"] is not None
             and p["main_season"]]
    best = sorted(solid, key=lambda p: -p["avg_price"])[:3]
    season_mean = mean(p["avg_price"] for p in solid) if solid else None
    tail = [p for p in profile if p["avg_price"] is not None and not p["main_season"]]
    return {"item": item, "cultivar": cultivar, "seasons": [y for y, _ in seasons],
            "profile": profile, "best": best, "season_mean": season_mean,
            "vol_floor": vol_floor, "thin_periods": [p["label"] for p in tail]}


def market_share(item: str, cultivar: str, asof: date | None = None, days: int = 365) -> dict:
    asof = asof or latest_date()
    rows = tools.market_volume(item, cultivar, _s(asof - timedelta(days=days)), _s(asof))
    total = sum(r["volume_t"] for r in rows) or 1
    for r in rows:
        r["share_pct"] = r["volume_t"] / total * 100
    return {"item": item, "cultivar": cultivar, "from": (asof - timedelta(days=days)).isoformat(),
            "to": asof.isoformat(), "total_t": total, "markets": rows[:8]}


def signal(st: dict) -> dict:
    """지난 7거래일 흐름을 세 가지 참고 신호로 요약한다. 판매 권유가 아니라 계산식을 그대로 말로 옮긴 것이다.
    오름세: 가격 +3% 이상이고 거래량 +20% 미만 / 내림세: 가격 -3% 이하이고 거래량 +20% 이상 / 그 밖: 보합"""
    if not st.get("in_season"):
        return {"level": "거래없음", "reason": "최근 2주 거래 기록이 없어 시세 판단을 할 수 없습니다."}
    p, v, y = st.get("price_chg_pct"), st.get("vol_chg_pct"), st.get("vs_5y_pct")
    up = p is not None and p >= 3
    down = p is not None and p <= -3
    flood = v is not None and v >= 20
    high = y is not None and y >= 10
    if up and not flood:
        lv, rs = "오름세", "가격이 직전보다 3% 이상 올랐고 거래량 증가는 20% 미만입니다."
    elif down and flood:
        lv, rs = "내림세", "가격이 직전보다 3% 이상 내렸고 거래량은 20% 이상 늘었습니다."
    else:
        lv, rs = "보합", "가격과 물량이 위 두 조건(±3%, 거래량 20%)에 걸리지 않습니다."
    if high:
        rs += " 최근 5년 같은 달 평균보다 높은 편입니다."
    elif y is not None and y <= -10:
        rs += " 최근 5년 같은 달 평균보다 낮은 편입니다."
    return {"level": lv, "reason": rs}


def market_compare(item: str, cultivar: str, asof: date | None = None, days: int = 28,
                   min_share_pct: float = 3.0) -> dict:
    """최근 days일 도매시장별 평균 도매가와 물량. 물량 비중이 min_share_pct 미만인 시장은
    몇 건 거래로 값이 튈 수 있어 가격 순위에서 뺀다."""
    asof = asof or latest_date()
    start = asof - timedelta(days=days)
    rows = tools.market_price(item, cultivar, _s(start), _s(asof))
    total = next((r for r in rows if r["market"] == "전체"), None)
    mk = [r for r in rows if r["market"] != "전체" and r.get("totalVolume")]
    vol = sum(r["totalVolume"] for r in mk) or 1
    out = []
    for r in mk:
        out.append({"market": r["market"], "avg_price": r["averagePrice"], "volume_t": r["totalVolume"],
                    "share_pct": r["totalVolume"] / vol * 100})
    ranked = sorted([m for m in out if m["share_pct"] >= min_share_pct], key=lambda m: -m["avg_price"])
    return {"item": item, "cultivar": cultivar, "from": start.isoformat(), "to": asof.isoformat(),
            "all_avg_price": total["averagePrice"] if total else None, "ranked": ranked,
            "excluded": [m["market"] for m in out if m["share_pct"] < min_share_pct],
            "min_share_pct": min_share_pct}


def _forecast_ok(last: dict | None, weeks: list[dict], ref_price: float | None = None) -> bool:
    """허브 전망이 마지막 실제 가격과 동떨어지면(0원, 1/3 미만, 3배 초과) 쓰지 않는다.
    ref_price(전국 최근 평균)를 주면 그 시장의 마지막 실제 가격도 전국 평균의 1/3∼3배 안이어야 한다.
    거래가 드문 시장에서 허브 모델이 0원 가까운 값을 내거나(2026-11-08 세척무·대구북부),
    시장 가격 자체가 전국과 동떨어진 경우가 있다(같은 날 세척당근·안산도매 kg당 100원 대 전국 3,406원)."""
    if not last or not last.get("price") or last["price"] <= 0 or not weeks:
        return False
    if ref_price and not (ref_price / 3 <= last["price"] <= ref_price * 3):
        return False
    return all(last["price"] / 3 <= w["price"] <= last["price"] * 3 for w in weeks)


def forecast(item: str, cultivar: str, market: str | None = None, asof: date | None = None,
             weeks: int = 4, tries: int = 3, ref_price: float | None = None) -> dict:
    """허브 「품종별 가격 전망」을 요약한다. 시장을 안 주면 최근 4주 물량이 많은 시장부터
    tries곳까지 물어 값이 정상인 첫 시장을 쓴다. 예측값은 허브가 낸 값이며 여기서는 인용·비교만 한다."""
    asof = asof or latest_date()
    avail = tools.forecast_items(_s(asof - timedelta(days=7)), _s(asof))
    if (item, cultivar) not in {(a["item"], a["cultivar"]) for a in avail}:
        return {"available": False, "reason": "not_offered", "item": item, "cultivar": cultivar,
                "forecast_cultivars": sorted({a["cultivar"] for a in avail if a["item"] == item})}
    if market:
        candidates = [market]
    else:
        ms = tools.market_volume(item, cultivar, _s(asof - timedelta(days=28)), _s(asof))
        candidates = [m["market"] for m in ms[:tries]] or ["서울가락도매"]
    skipped = []
    for mk in candidates:
        try:
            d = tools.price_forecast(item, cultivar, mk)
        except tools.ToolError:
            skipped.append(mk)
            continue
        actual = d.get("actual_data") or []
        last = actual[-1] if actual else None
        pred = [p for p in d.get("prediction_data") or [] if not last or p["date"] > last["date"]][:weeks]
        weeks_out = []
        for p in pred:
            ci = p.get("confidence_interval") or {}
            weeks_out.append({"week": p["date"], "price": p["price"], "lo": ci.get("lower_bound"),
                              "hi": ci.get("upper_bound"), "vs_last_pct": _pct(p["price"], last and last["price"])})
        if not _forecast_ok(last, weeks_out, ref_price):
            skipped.append(mk)
            continue
        return {"available": True, "item": item, "cultivar": cultivar, "market": d.get("market", mk),
                "base_date": d.get("base_date"), "last_week": last["date"], "last_price": last["price"],
                "weeks": weeks_out, "actual_tail": [a for a in actual if a.get("price")][-8:],  # 휴장 주(0원)는 뺀다
                "skipped": skipped}
    return {"available": False, "reason": "implausible", "item": item, "cultivar": cultivar,
            "skipped": skipped, "forecast_cultivars": []}
