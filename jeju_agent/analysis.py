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
    """가격 흐름을 세 가지 참고 신호로 요약한다. 판매 권유가 아니라 근거 요약이다."""
    if not st.get("in_season"):
        return {"level": "거래없음", "reason": "최근 2주 거래 기록이 없어 시세 판단을 할 수 없습니다."}
    p, v, y = st.get("price_chg_pct"), st.get("vol_chg_pct"), st.get("vs_5y_pct")
    up = p is not None and p >= 3
    down = p is not None and p <= -3
    flood = v is not None and v >= 20
    high = y is not None and y >= 10
    if up and not flood:
        lv, rs = "유리", "가격이 오르는데 물량은 크게 늘지 않았습니다."
    elif down and flood:
        lv, rs = "주의", "물량이 몰리면서 가격이 내려가고 있습니다."
    else:
        lv, rs = "보통", "가격과 물량에 뚜렷한 쏠림이 없습니다."
    if high:
        rs += " 최근 5년 같은 달 평균보다 높은 편입니다."
    elif y is not None and y <= -10:
        rs += " 최근 5년 같은 달 평균보다 낮은 편입니다."
    return {"level": lv, "reason": rs}
