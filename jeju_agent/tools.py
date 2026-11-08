"""제주데이터허브 농산물 유통 분석 데이터 도구 (Tool layer).

로그인·API 키 없이 공개된 제주데이터허브 농산물 분석 엔드포인트만 호출한다.
응답은 로컬 캐시(cache/)에 저장해 센터 PC에서 같은 질문을 반복할 때 재호출하지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE_URL = os.environ.get("JEJU_HUB_BASE", "https://jejudatahub.net")
CACHE_DIR = Path(os.environ.get("JEJU_AGENT_CACHE", Path(__file__).resolve().parent.parent / "cache"))
CACHE_TTL_SEC = int(os.environ.get("JEJU_AGENT_CACHE_TTL", str(6 * 3600)))
OFFLINE = os.environ.get("JEJU_AGENT_OFFLINE") == "1"
FIXTURE_DIR = os.environ.get("JEJU_AGENT_FIXTURES")
RECORD = os.environ.get("JEJU_AGENT_RECORD") == "1"  # 테스트 고정 응답을 새로 저장할 때만 1

AGRI = "/data-api/analysis/agricultural"


class ToolError(RuntimeError):
    pass


def _cache_key(path: str, params: dict) -> str:
    raw = path + "?" + urllib.parse.urlencode(sorted(params.items()))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _get(path: str, params: dict | None = None, post: bool = False):
    params = params or {}
    key = _cache_key(("POST " if post else "") + path, params)
    if FIXTURE_DIR:
        f = Path(FIXTURE_DIR) / f"{key}.json"
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))
        if not RECORD:
            raise ToolError(f"fixture 없음: {path} {params} ({key})")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cf = CACHE_DIR / f"{key}.json"
    if cf.exists() and (OFFLINE or time.time() - cf.stat().st_mtime < CACHE_TTL_SEC):
        return json.loads(cf.read_text(encoding="utf-8"))
    if OFFLINE:
        raise ToolError(f"오프라인 모드인데 캐시가 없습니다: {path}")
    headers = {"User-Agent": "jeju-harvest-agent/0.2"}
    if post:  # 허브 가격 전망은 JSON 본문으로 묻는다
        url, data = BASE_URL + path, json.dumps(params, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    else:
        url, data = BASE_URL + path + ("?" + urllib.parse.urlencode(params) if params else ""), None
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            body = r.read().decode("utf-8")
    except Exception as e:  # 네트워크 장애 시 오래된 캐시라도 쓴다
        if cf.exists():
            return json.loads(cf.read_text(encoding="utf-8"))
        raise ToolError(f"제주데이터허브 호출 실패: {path} ({e})") from e
    data = json.loads(body)
    if isinstance(data, dict) and data.get("status") == 500:
        raise ToolError(f"제주데이터허브 오류: {path} ({data.get('detail')})")
    cf.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    if FIXTURE_DIR and RECORD:
        (Path(FIXTURE_DIR) / f"{key}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


# ---- 도구 함수 (에이전트가 고르는 단위) -------------------------------------

def list_items() -> list[dict]:
    """조회 가능한 품목·품종 목록."""
    return _get(f"{AGRI}/item/item-list")


def data_period() -> dict:
    """데이터 수록 기간(시작일·최종일, YYYYMMDD)."""
    return _get(f"{AGRI}/item/period-date")


def daily_stats(item: str, cultivar: str, start: str, end: str) -> list[dict]:
    """일별 도매가격(원/kg)·거래물량(톤)·거래금액(백만원 추정, 원문 단위 미표기)."""
    return _get(f"{AGRI}/item/statistics-daily",
                {"startDate": start, "endDate": end, "item": item, "cultivar": cultivar})


def monthly_price(item: str, cultivar: str, start: str, end: str) -> list[dict]:
    """월평균 도매가격 시계열."""
    data = _get(f"{AGRI}/month/price",
                {"startDate": start, "endDate": end, "item": item, "cultivar": cultivar})
    for s in data:
        if s.get("name") == cultivar:
            return s.get("data", [])
    return data[0].get("data", []) if data else []


def market_volume(item: str, cultivar: str, start: str, end: str) -> list[dict]:
    """도매시장별 거래물량(톤). 원문은 latitude/longitude 이름이 서로 바뀌어 있어 바로잡는다."""
    rows = _get(f"{AGRI}/market/market-volume",
                {"startDate": start, "endDate": end, "item": item, "cultivar": cultivar})
    out = []
    for r in rows:
        out.append({"market": r["market"], "volume_t": r["transactionVolume"],
                    "lon": float(r["latitude"]), "lat": float(r["longitude"])})
    return sorted(out, key=lambda x: -x["volume_t"])


def market_price(item: str, cultivar: str, start: str, end: str) -> list[dict]:
    """도매시장별 평균·최고·최저 도매가격(원/kg)과 거래물량(톤). 첫 행은 '전체'."""
    return _get(f"{AGRI}/market/price",
                {"startDate": start, "endDate": end, "item": item, "cultivar": cultivar})


def forecast_items(start: str, end: str) -> list[dict]:
    """허브 「품종별 가격 전망」이 제공되는 품목·품종 목록."""
    return _get(f"{AGRI}/market/prediction-item-list", {"startDate": start, "endDate": end})


def price_forecast(item: str, cultivar: str, market: str) -> dict:
    """허브 「품종별 가격 전망」: 도매시장 하나의 주별 실제 가격과 앞으로 12주 예측가·신뢰구간.
    예측은 허브가 계산한 값이며 이 서비스는 인용만 한다. market='전체'는 허브가 받지 않는다."""
    return _get("/api/agricultural/price-prediction",
                {"item": item, "cultivar": cultivar, "market": market}, post=True)


TOOLS = {
    "list_items": list_items,
    "data_period": data_period,
    "daily_stats": daily_stats,
    "monthly_price": monthly_price,
    "market_volume": market_volume,
    "market_price": market_price,
    "forecast_items": forecast_items,
    "price_forecast": price_forecast,
}
