"""질문 → 계획 → 도구 실행 → 근거 계산 → 답변.

기본은 규칙 기반 계획기(LLM 없음)라 센터 PC에서 인터넷만 되면 바로 돈다.
LLM_BASE_URL·LLM_MODEL을 주면 OpenAI 호환 엔드포인트(예: 센터 PC의 로컬 모델 서버)로
①규칙이 품목을 못 찾을 때 질문 해석 ②최종 답변 문장 다듬기를 맡긴다.
LLM 문장에 근거표에 없는 숫자가 들어가면 버리고 규칙 답변을 쓴다(숫자 가드).
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from dataclasses import dataclass, field

from . import analysis, tools

# 질문 속 단어 → (품목, 기본 품종). 품종 이름이 직접 나오면 그 품종을 쓴다.
ITEM_ALIASES = [
    ("월동무", ("무", "월동무")),
    ("세척무", ("무", "세척무")),
    ("흙당근", ("당근", "흙당근")),
    ("세척당근", ("당근", "세척당근")),
    ("당근", ("당근", "당근(일반)")),
    ("저장양배추", ("양배추", "저장양배추")),
    ("양배추", ("양배추", "양배추(일반)")),
    ("깐마늘", ("마늘", "깐마늘")),
    ("난지", ("마늘", "저장형 난지")),
    ("남도", ("마늘", "햇마늘 남도")),
    ("마늘", ("마늘", "마늘")),
    ("조생양파", ("양파", "조생양파")),
    ("만생양파", ("양파", "만생양파")),
    ("저장양파", ("양파", "저장양파")),
    ("양파", ("양파", "조생양파")),
    ("무", ("무", "월동무")),
]

INTENT_WORDS = {
    "timing": ["언제", "시기", "타이밍", "몇월", "몇 월", "때가", "출하 시기", "시즌"],
    "market": ["어느 시장", "어디", "시장별", "도매시장", "어느 도매"],
    "status": ["지금", "요즘", "시세", "가격", "얼마", "오늘", "이번주", "이번 주", "작년", "비교", "내도", "팔아도"],
}


@dataclass
class Step:
    tool: str
    args: dict
    ms: int = 0
    rows: int = 0
    note: str = ""


@dataclass
class Result:
    question: str
    item: str | None = None
    cultivar: str | None = None
    intents: list[str] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    facts: dict = field(default_factory=dict)
    answer: str = ""
    evidence: list[list[str]] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    chart: dict | None = None
    llm_used: bool = False
    llm_rejected: str = ""

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["steps"] = [s.__dict__ for s in self.steps]
        return d


# ---------------------------------------------------------------- 계획
def parse(question: str) -> tuple[tuple[str, str] | None, list[str]]:
    q = question.replace(" ", "").replace("무엇", "")
    target = None
    for word, pair in ITEM_ALIASES:
        if word in q:
            target = pair
            break
    intents = [k for k, ws in INTENT_WORDS.items() if any(w.replace(" ", "") in q for w in ws)]
    if not intents:
        intents = ["status"]
    return target, intents


def _llm(messages: list[dict], max_tokens: int = 500) -> str | None:
    base, model = os.environ.get("LLM_BASE_URL"), os.environ.get("LLM_MODEL")
    if not base or not model:
        return None
    body = json.dumps({"model": model, "messages": messages, "temperature": 0.2,
                       "max_tokens": max_tokens}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if os.environ.get("LLM_API_KEY"):
        headers["Authorization"] = "Bearer " + os.environ["LLM_API_KEY"]
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())["choices"][0]["message"]["content"]
    except Exception:
        return None


def _llm_parse(question: str) -> tuple[str, str] | None:
    items = tools.list_items()
    txt = _llm([
        {"role": "system", "content": "다음 목록에서 질문이 가리키는 품목과 품종 하나를 JSON {\"item\":..,\"cultivar\":..}로만 답하라. 없으면 {}.\n" + json.dumps(items, ensure_ascii=False)},
        {"role": "user", "content": question}], 80)
    if not txt:
        return None
    m = re.search(r"\{.*\}", txt, re.S)
    try:
        d = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        return None
    pair = (d.get("item"), d.get("cultivar"))
    return pair if pair in {(i["item"], i["cultivar"]) for i in items} else None


# ---------------------------------------------------------------- 실행
def _run(res: Result, name: str, fn, **kw):
    t0 = time.time()
    out = fn(**kw)
    st = Step(tool=name, args=kw, ms=int((time.time() - t0) * 1000))
    if isinstance(out, dict):
        st.rows = len(out.get("daily") or out.get("profile") or out.get("markets") or [])
    res.steps.append(st)
    return out


def _won(x):
    return "-" if x is None else f"{x:,.0f}원"


def _pc(x):
    return "-" if x is None else f"{x:+.1f}%"


def _ymd(s: str) -> str:
    s = s.replace("-", "")
    return f"{int(s[4:6])}/{int(s[6:8])}"


def answer(question: str) -> Result:
    res = Result(question=question)
    target, intents = parse(question)
    if target is None:
        target = _llm_parse(question)
        if target:
            res.steps.append(Step(tool="llm_parse", args={}, note="규칙으로 품목을 못 찾아 LLM이 해석"))
    if target is None:
        res.answer = ("어떤 작물인지 찾지 못했습니다. 월동무·당근·양배추·마늘·양파 중 하나를 넣어 다시 물어봐 주세요. "
                      "예: \"월동무 지금 내도 될까?\"")
        return res
    res.item, res.cultivar = target
    res.intents = intents
    asof = _run(res, "data_period", lambda: analysis.latest_date())
    lines: list[str] = []

    if "status" in intents or "timing" not in intents and "market" not in intents:
        st = _run(res, "recent_status", analysis.recent_status, item=res.item, cultivar=res.cultivar, asof=asof)
        sg = analysis.signal(st)
        res.facts["status"] = st
        res.facts["signal"] = sg
        if st["in_season"]:
            lines.append(
                f"{res.cultivar} 최근 {st['window']}거래일({_ymd(st['cur_from'])}~{_ymd(st['cur_to'])}) 평균 도매가는 "
                f"kg당 {_won(st['cur_price'])}으로 직전 {st['window']}거래일보다 {_pc(st['price_chg_pct'])} 움직였고, "
                f"하루 평균 거래량은 {st['cur_vol_per_day']:,.1f}톤({_pc(st['vol_chg_pct'])})입니다.")
            if st.get("ly_price"):
                lines.append(f"작년 같은 시기(kg당 {_won(st['ly_price'])})보다 {_pc(st['yoy_pct'])}, "
                             f"최근 {st['month_avg_n']}년 같은 달 평균(kg당 {_won(st['month_avg_5y'])})보다 {_pc(st['vs_5y_pct'])}입니다.")
            lines.append(f"참고 신호: [{sg['level']}] {sg['reason']}")
            res.evidence += [
                ["최근 평균 도매가(원/kg)", _won(st["cur_price"]), f"{st['cur_from']}~{st['cur_to']}"],
                ["직전 대비", _pc(st["price_chg_pct"]), "거래량 가중 평균"],
                ["하루 평균 거래량(톤)", f"{st['cur_vol_per_day']:,.1f}", _pc(st["vol_chg_pct"])],
                ["작년 같은 시기", _won(st.get("ly_price")), f"{st.get('ly_from')}~{st.get('ly_to')} ({st.get('ly_days')}거래일)"],
                ["5년 같은 달 평균", _won(st.get("month_avg_5y")), f"{st.get('month_avg_n')}개 연도"],
            ]
            res.chart = {"title": f"{res.cultivar} 최근 도매가(원/kg)",
                         "x": [r["baseDate"] for r in st["daily"]],
                         "y": [r["wholesalePrice"] for r in st["daily"]]}
        else:
            lines.append(f"{res.cultivar}는 최근 2주 동안 거래 기록이 없습니다(최근 60일 안 마지막 거래일: {st.get('last_trade') or '없음'}). "
                         f"아직 출하 시즌이 아니거나 시즌이 끝난 시기입니다. 지난 시즌에 언제 값이 좋았는지 함께 보여드립니다.")
            if "timing" not in intents:
                intents.append("timing")

    if "timing" in intents:
        sp = _run(res, "season_profile", analysis.season_profile, item=res.item, cultivar=res.cultivar, asof=asof)
        res.facts["season"] = sp
        if sp["best"]:
            best = ", ".join(f"{b['label']}(kg당 {_won(b['avg_price'])})" for b in sp["best"])
            lines.append(f"지난 {len(sp['seasons'])}개 시즌을 반달 단위로 보면, 물량이 본격적으로 거래되는 기간 중 "
                         f"도매가(연도별 중앙값)가 높았던 때는 {best}입니다. 이 기간 평균은 kg당 {_won(sp['season_mean'])}입니다.")
            if sp["thin_periods"]:
                lines.append(f"{', '.join(sp['thin_periods'])}은 값이 높게 나오기도 하지만 거래량이 적은 시기라 순위에서 뺐습니다.")
            for p in sp["profile"]:
                if p["avg_price"] is not None:
                    res.evidence.append([p["label"] + ("" if p["main_season"] else " (물량 적음)"), _won(p["avg_price"]),
                                         f"{p['years']}개 시즌 · 하루 {p['avg_vol_per_day']:,.0f}톤 · " +
                                         " ".join(f"{y[2:]}시즌 {v:,}" for y, v in p["by_year"].items())])
            if res.chart is None:
                pts = [p for p in sp["profile"] if p["avg_price"] is not None]
                res.chart = {"title": f"{res.cultivar} 시즌 반달별 도매가 중앙값(원/kg)",
                             "x": [p["label"] for p in pts], "y": [round(p["avg_price"]) for p in pts]}
        else:
            lines.append("지난 시즌 거래 기록이 부족해 시기 비교를 할 수 없습니다.")

    if "market" in intents:
        ms = _run(res, "market_share", analysis.market_share, item=res.item, cultivar=res.cultivar, asof=asof)
        res.facts["market"] = ms
        top = ", ".join(f"{m['market']} {m['share_pct']:.0f}%" for m in ms["markets"][:3])
        lines.append(f"최근 1년 {res.cultivar} 거래량은 {top} 순으로 많았습니다.")
        for m in ms["markets"]:
            res.evidence.append([m["market"], f"{m['volume_t']:,.0f}톤", f"{m['share_pct']:.1f}%"])

    res.caveats = [
        f"제주데이터허브 농산물 유통 분석 데이터(수록 최종일 {asof.isoformat()}) 기준입니다. 산지 가격이 아니라 전국 도매시장 경락 가격입니다.",
        "가격 신호는 과거 흐름을 요약한 참고 자료입니다. 날씨·작황·계약 조건은 반영하지 않으니 판매 결정은 농가가 직접 하세요.",
    ]
    base_answer = " ".join(lines)
    res.answer = base_answer
    _polish(res, base_answer)
    return res


# ---------------------------------------------------------------- 숫자 가드
_NUM = re.compile(r"[+-]?\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "").lstrip("+") for n in _NUM.findall(text)}


def _polish(res: Result, base: str) -> None:
    txt = _llm([
        {"role": "system", "content": "너는 제주 농가를 돕는 안내원이다. 아래 근거 문장의 숫자를 바꾸지 말고, 새 숫자를 만들지 말고, "
                                      "쉬운 말로 4문장 안에 다시 써라. 판매를 권유하지 말고 판단은 농가 몫이라고 덧붙여라."},
        {"role": "user", "content": f"질문: {res.question}\n근거: {base}"}])
    if not txt:
        return
    allowed = _numbers(base) | _numbers(" ".join(" ".join(r) for r in res.evidence))
    extra = _numbers(txt) - allowed
    if extra:
        res.llm_rejected = "근거에 없는 숫자 " + ", ".join(sorted(extra)[:5])
        return
    res.answer = txt.strip()
    res.llm_used = True
