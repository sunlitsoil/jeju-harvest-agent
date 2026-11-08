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
    "forecast": ["기다리", "기다릴", "더 오를", "오를까", "오르까", "떨어질", "내릴까", "전망", "다음주", "다음 주",
                 "앞으로", "버틸", "몇 주", "다음달", "다음 달"],
    "timing": ["언제", "시기", "타이밍", "몇월", "몇 월", "때가", "출하 시기", "시즌"],
    "market": ["어느 시장", "어디", "시장별", "도매시장", "어느 도매"],
    "status": ["지금", "요즘", "시세", "가격", "얼마", "오늘", "이번주", "이번 주", "작년", "비교", "내도", "팔아도"],
}
# 「지금 내도 될까」처럼 출하 결정을 묻는 말은 현재 시세에 허브 전망까지 붙인다.
DECIDE_WORDS = ["내도", "팔아도", "출하해도", "낼까", "팔까"]

# 질문 속 시장 이름(「가락시장」「엄궁」) → 허브 도매시장명
MARKET_ALIASES = {"가락": "서울가락도매", "강서": "서울강서도매", "구리": "구리도매", "엄궁": "부산엄궁도매",
                  "반여": "부산반여도매", "대구": "대구북부도매", "북부": "대구북부도매", "각화": "광주각화도매",
                  "광주서부": "광주서부도매", "오정": "대전오정도매", "노은": "대전노은도매", "남촌": "인천남촌도매",
                  "인천": "인천남촌도매", "울산": "울산도매", "청주": "청주도매"}


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
    lines: list[str] = field(default_factory=list)  # 화면에서 한 줄씩 보여 줄 답변 문장
    summary: str = ""  # AI가 고른 핵심 문장(본문 문장 그대로). 본문 answer는 항상 코드가 만든 문장이다
    picked: list[int] = field(default_factory=list)
    llm_used: bool = False
    llm_rejected: str = ""
    market: str | None = None
    planner: str = "rule"  # rule | llm

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
    if any(w in q for w in DECIDE_WORDS):
        intents += [i for i in ("status", "forecast") if i not in intents]
    if not intents:
        intents = ["status"]
    return target, intents


def parse_market(question: str) -> str | None:
    q = question.replace(" ", "")
    for word, name in MARKET_ALIASES.items():
        if word in q:
            return name
    return None


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


PLAN_TOOLS = {  # LLM에게 보여 주는 도구 이름 → 내부 의도
    "recent_status": ("status", "최근 7거래일 도매가·거래량을 직전 주, 작년 같은 시기, 최근 5년 같은 달 평균과 비교한다. 지금 시세를 물을 때."),
    "season_profile": ("timing", "지난 5개 시즌을 반달 단위로 나눠 언제 값이 좋았는지 본다. 출하 시기를 물을 때."),
    "market_compare": ("market", "도매시장별 거래 물량 비중과 최근 4주 평균 도매가를 비교한다. 어느 시장에 낼지 물을 때."),
    "price_forecast": ("forecast", "제주데이터허브 「품종별 가격 전망」의 앞으로 4주 예측가를 가져온다. 기다리면 오를지, 지금 낼지 물을 때."),
}


def _llm_tools(messages: list[dict], tool_specs: list[dict]) -> dict | None:
    base, model = os.environ.get("LLM_BASE_URL"), os.environ.get("LLM_MODEL")
    if not base or not model:
        return None
    body = json.dumps({"model": model, "messages": messages, "tools": tool_specs, "temperature": 0}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if os.environ.get("LLM_API_KEY"):
        headers["Authorization"] = "Bearer " + os.environ["LLM_API_KEY"]
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=int(os.environ.get("LLM_TIMEOUT", "60"))) as r:
            return json.loads(r.read())["choices"][0]["message"]
    except Exception:
        return None


def llm_plan(question: str) -> dict | None:
    """LLM이 질문을 읽고 부를 도구와 품종·시장을 고른다. 숫자는 만들지 않는다.
    허용 목록 밖의 품종·시장·도구를 고르면 그 선택은 버린다. 실패하면 None(규칙 계획기로)."""
    if os.environ.get("AGENT_PLANNER", "auto") == "rule" or not os.environ.get("LLM_MODEL"):
        return None
    items = tools.list_items()
    cultivars = sorted({i["cultivar"] for i in items})
    by_cultivar = {i["cultivar"]: i["item"] for i in items}
    markets = sorted(set(MARKET_ALIASES.values()))
    specs = [{"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": {
            "cultivar": {"type": "string", "enum": cultivars, "description": "품종. 질문에 품종이 없으면 그 품목의 일반 품종(예: 무→월동무, 당근→당근(일반), 양배추→양배추(일반))."},
            "market": {"type": "string", "enum": markets, "description": "질문에 도매시장이 나올 때만."}},
            "required": ["cultivar"]}}} for name, (_, desc) in PLAN_TOOLS.items()]
    t0 = time.time()
    msg = _llm_tools([
        {"role": "system", "content": "너는 제주 농가의 출하 질문에 답하기 위해 도구를 고르는 계획 담당이다. "
                                      "질문에 답하는 데 필요한 도구를 모두 한 번에 호출하라. 지금 출하할지 묻는 질문이면 recent_status와 price_forecast를 함께 불러라. "
                                      "작물이 목록에 없으면 도구를 부르지 말고 그렇다고만 답하라."},
        {"role": "user", "content": question}], specs)
    ms = int((time.time() - t0) * 1000)
    calls = (msg or {}).get("tool_calls") or []
    intents, target, market, chosen = [], None, None, []
    for c in calls:
        fn = c.get("function", {})
        name = fn.get("name")
        if name not in PLAN_TOOLS:
            continue
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            continue
        cv = args.get("cultivar")
        if cv not in by_cultivar:
            continue
        target = target or (by_cultivar[cv], cv)
        if args.get("market") in markets:
            market = market or args["market"]
        if PLAN_TOOLS[name][0] not in intents:
            intents.append(PLAN_TOOLS[name][0])
        chosen.append(name)
    if not intents:
        return None
    return {"target": target, "intents": intents, "market": market, "tools": chosen, "ms": ms}


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
def _run(res: Result, name: str, fn, optional: bool = False, why: str = "", **kw):
    t0 = time.time()
    try:
        out = fn(**kw)
    except tools.ToolError as e:
        if not optional:
            raise
        res.steps.append(Step(tool=name, args={}, ms=int((time.time() - t0) * 1000), note=f"{why} → 실패: {e}".lstrip(" →")))
        return None
    st = Step(tool=name, args={k: (v.isoformat() if hasattr(v, "isoformat") else round(v) if isinstance(v, float) else v)
                               for k, v in kw.items() if v is not None},
              ms=int((time.time() - t0) * 1000), note=why)
    if isinstance(out, dict):
        st.rows = len(out.get("daily") or out.get("profile") or out.get("markets") or out.get("ranked")
                      or out.get("weeks") or [])
    res.steps.append(st)
    return out


def _won(x):
    return "-" if x is None else f"{x:,.0f}원"


def _pc(x):
    return "-" if x is None else f"{x:+.1f}%"


def _josa(word: str, pair: str) -> str:
    """받침이 있으면 pair[0](은·을), 없으면 pair[1](는·를). 괄호로 끝나면 괄호 앞 글자로 본다."""
    ch = word.rstrip(")").split("(")[0][-1:] if word.endswith(")") else word[-1:]
    has = bool(ch) and "가" <= ch <= "힣" and (ord(ch) - 0xAC00) % 28 != 0
    return word + (pair[0] if has else pair[1])


def _ymd(s: str) -> str:
    s = s.replace("-", "")
    return f"{int(s[4:6])}/{int(s[6:8])}"


def plan(question: str) -> dict:
    """질문 → 품종·의도·시장·계획기. LLM 계획이 있으면 쓰고, 없거나 실패하면 규칙 계획기로 돌아간다.
    규칙이 찾은 시장 이름은 LLM이 빠뜨려도 보탠다(두 계획기를 겹쳐 쓴다)."""
    target, intents = parse(question)
    market = parse_market(question)
    out = {"planner": "rule", "steps": []}
    mode = os.environ.get("AGENT_PLANNER", "auto")  # llm: LLM 먼저 | auto: 규칙이 품목을 못 찾을 때만 LLM | rule
    rule_found = target is not None
    p = llm_plan(question) if mode == "llm" or (mode == "auto" and target is None) else None
    if p:
        out["planner"] = "llm"
        target = p["target"] or target
        intents = p["intents"] or intents
        market = p["market"] or market
        out["steps"].append(Step(tool="llm_plan", args={"tools": p["tools"]}, ms=p["ms"],
                                 note="LLM이 질문을 읽고 부를 도구를 골랐습니다"))
    elif target is None:
        target = _llm_parse(question)
        if target:
            out["steps"].append(Step(tool="llm_parse", args={}, note="규칙으로 품목을 못 찾아 LLM이 해석"))
    if market and "market" not in intents and "forecast" not in intents:
        intents.append("market")
    out.update(target=target, intents=intents if target else [], market=market,
               llm_target=bool(target) and not rule_found)
    return out


def answer(question: str) -> Result:
    res = Result(question=question)
    pl = plan(question)
    target, intents, market = pl["target"], pl["intents"], pl["market"]
    res.planner = pl["planner"]
    res.steps += pl["steps"]
    res.market = market
    if target is None:
        res.answer = ("어떤 작물인지 찾지 못했습니다. 월동무·당근·양배추·마늘·양파 중 하나를 넣어 다시 물어봐 주세요. "
                      "예: \"월동무 지금 내도 될까?\"")
        return res
    res.item, res.cultivar = target
    res.intents = intents
    asof = _run(res, "data_period", lambda: analysis.latest_date())
    lines: list[str] = []
    if pl.get("llm_target"):  # 규칙이 못 찾은 작물을 AI가 읽은 경우: 잘못 읽었을 수 있으니 먼저 밝힌다
        lines.append(f"질문에서 작물 이름을 바로 찾지 못해 AI가 「{res.cultivar}」로 읽었습니다. 다른 작물이면 작물 이름을 넣어 다시 물어봐 주세요.")

    if "status" in intents or "timing" not in intents and "market" not in intents:
        st = _run(res, "recent_status", analysis.recent_status, why="현재 시세부터 보려고 최근 7거래일을 봅니다", item=res.item, cultivar=res.cultivar, asof=asof)
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
            lines.append(f"{_josa(res.cultivar, '은는')} 최근 2주 동안 거래 기록이 없습니다(최근 60일 안 마지막 거래일: {_ymd(st['last_trade']) if st.get('last_trade') else '없음'}). "
                         f"아직 출하 시즌이 아니거나 시즌이 끝난 시기입니다. 지난 시즌에 언제 값이 좋았는지 함께 보여드립니다.")
            if "timing" not in intents:
                intents.append("timing")
                res.steps[-1].note += " → 최근 2주 거래가 없어 지난 시즌 분석으로 바꿉니다"

    if "timing" in intents:
        sp = _run(res, "season_profile", analysis.season_profile, why="출하 시기를 보려고 지난 시즌을 반달 단위로 나눕니다", item=res.item, cultivar=res.cultivar, asof=asof)
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
        ms = _run(res, "market_share", analysis.market_share, why="어느 시장인지 묻는 질문이라 시장별 물량을 봅니다", item=res.item, cultivar=res.cultivar, asof=asof)
        res.facts["market"] = ms
        top = ", ".join(f"{m['market']} {m['share_pct']:.0f}%" for m in ms["markets"][:3])
        lines.append(f"최근 1년 {res.cultivar} 거래량은 {top} 순으로 많았습니다.")
        for m in ms["markets"]:
            res.evidence.append([m["market"] + " (1년 물량)", f"{m['volume_t']:,.0f}톤", f"{m['share_pct']:.1f}%"])
        mc = _run(res, "market_compare", analysis.market_compare, optional=True,
                  why="물량이 작은 시장을 빼고 최근 4주 평균가를 비교합니다", item=res.item,
                  cultivar=res.cultivar, asof=asof)
        res.facts["market_price"] = mc
        if mc and mc["ranked"]:
            best = ", ".join(f"{m['market']}(kg당 {_won(m['avg_price'])})" for m in mc["ranked"][:3])
            lines.append(f"최근 4주 물량 비중 {mc['min_share_pct']:.0f}% 이상인 시장 가운데 평균 도매가가 높았던 곳은 {best}입니다.")
            if market:
                mine = next((m for m in mc["ranked"] + [] if m["market"] == market), None)
                if mine:
                    lines.append(f"물어보신 시장({market})은 kg당 {_won(mine['avg_price'])}, 물량 비중 {mine['share_pct']:.0f}%입니다.")
                elif market in mc["excluded"]:
                    lines.append(f"물어보신 시장({market})은 최근 4주 물량 비중이 {mc['min_share_pct']:.0f}%보다 작아 가격 비교에서 뺐습니다.")
            for m in mc["ranked"]:
                res.evidence.append([m["market"] + " (4주 평균가)", _won(m["avg_price"]),
                                     f"{m['volume_t']:,.0f}톤 · 비중 {m['share_pct']:.1f}%"])
        else:
            lines.append("최근 4주 시장별 가격은 거래가 적거나 허브 응답이 없어 비교하지 못했습니다.")

    if "forecast" in intents:
        ref = (res.facts.get("status") or {}).get("cur_price")
        fc = _run(res, "price_forecast", analysis.forecast, optional=True,
                  why="앞으로를 묻는 질문이라 제주데이터허브 전망을 가져옵니다", item=res.item,
                  cultivar=res.cultivar, market=market, asof=asof, ref_price=ref)
        res.facts["forecast"] = fc or {}
        if fc and fc.get("skipped"):
            res.steps[-1].note += f" → 이상값 검사로 {', '.join(fc['skipped'])} 전망을 뺐습니다"
        if fc and not fc["available"] and fc.get("reason") == "not_offered":
            res.steps[-1].note += " → 이 품종은 허브 전망 대상이 아니라 싣지 않습니다"
        if fc is None:
            lines.append("제주데이터허브 가격 전망을 지금 불러오지 못했습니다. 잠시 뒤 다시 물어봐 주세요.")
        elif not fc["available"] and fc.get("reason") == "implausible":
            lines.append(f"제주데이터허브 가격 전망({', '.join(fc['skipped'])})은 그 시장 가격이 전국 평균이나 최근 실제 가격과 크게 어긋나 이번 답에서는 싣지 않았습니다.")
        elif not fc["available"]:
            alt = ", ".join(fc["forecast_cultivars"]) or "없음"
            lines.append(f"제주데이터허브의 「품종별 가격 전망」은 {_josa(res.cultivar, '을를')} 제공하지 않습니다"
                         f"(같은 품목의 전망 품종: {alt}).")
        elif fc["weeks"]:
            w = fc["weeks"]
            seq = ", ".join(f"{_ymd(x['week'])} 주 {_won(x['price'])}" for x in w)
            lines.append(f"제주데이터허브 가격 전망은 {fc['market']} 기준(기준일 {_ymd(fc['base_date'])})으로, "
                         f"지난 실제 주({_ymd(fc['last_week'])} 주) kg당 {_won(fc['last_price'])} 다음을 {seq}으로 봅니다. "
                         f"{len(w)}주 뒤 전망은 지난 실제 주보다 {_pc(w[-1]['vs_last_pct'])}이고, "
                         f"전망 범위는 kg당 {_won(w[-1]['lo'])}∼{_won(w[-1]['hi'])}입니다.")
            if fc.get("skipped"):
                lines.append(f"({', '.join(fc['skipped'])} 전망은 가격이 전국 평균이나 실제 가격과 크게 어긋나 건너뛰었습니다.)")
            lv = (res.facts.get("signal") or {}).get("level")
            chg = w[-1]["vs_last_pct"] or 0
            if (lv == "오름세" and chg <= -3) or (lv == "내림세" and chg >= 3):
                lines.append(f"지난 흐름으로 본 참고 신호([{lv}])와 허브 전망의 방향이 서로 다릅니다. 두 근거를 함께 보고 판단하세요.")
            cur = (res.facts.get("status") or {}).get("cur_price")
            if cur and abs(fc["last_price"] / cur - 1) > 0.4:
                lines.append(f"이 전망은 {fc['market']} 한 곳의 값이라 위의 전국 도매시장 평균과 차이가 큽니다. 흐름(오르내림)만 참고하세요.")
            for x in w:
                res.evidence.append([f"허브 전망 {_ymd(x['week'])} 주 ({fc['market']})", _won(x["price"]),
                                     f"범위 {_won(x['lo'])}∼{_won(x['hi'])} · 지난 실제 주 대비 {_pc(x['vs_last_pct'])}"])
            if res.chart is None or "status" not in intents:
                res.chart = {"title": f"{res.cultivar} {fc['market']} 주별 실제 가격과 허브 전망(원/kg)",
                             "x": [a["date"] for a in fc["actual_tail"]] + [x["week"] for x in w],
                             "y": [round(a["price"]) for a in fc["actual_tail"]] + [round(x["price"]) for x in w],
                             "split": len(fc["actual_tail"])}

    res.caveats = [
        f"제주데이터허브 농산물 유통 분석 데이터(수록 최종일 {asof.isoformat()}) 기준입니다. 산지 가격이 아니라 전국 도매시장 경락 가격(운송비 차감 전)이며, 제주산 출하분만 따로 집계했는지는 확인하지 못했습니다.",
        "가격 신호는 과거 흐름을 요약한 참고 자료입니다. 날씨·작황·계약 조건은 반영하지 않으니 판매 결정은 농가가 직접 하세요.",
    ]
    if "forecast" in res.facts and res.facts["forecast"].get("available"):
        res.caveats.append("가격 전망은 제주데이터허브 「품종별 가격 전망」이 낸 값을 그대로 옮긴 것입니다. 이 서비스가 따로 예측하지 않습니다.")
    base_answer = " ".join(lines)
    res.lines = lines
    res.answer = base_answer
    _polish(res, base_answer)  # 본문은 그대로 두고 AI가 고른 핵심 문장만 위에 따로 올린다
    return res


# ---------------------------------------------------------------- 숫자 가드
_NUM = re.compile(r"[+-]?\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> set[str]:
    # 부호는 문장에서 「낮다·내렸다」로 바뀔 수 있어 값만 비교한다
    return {n.replace(",", "").lstrip("+-") for n in _NUM.findall(text)}


_KEEP_TERMS = ["제주데이터허브", "최근 7거래일", "최근 4주", "최근 1년", "작년 같은 시기", "지난 5개 시즌", "제공하지 않습니다",
               "싣지 않았습니다", "차이가 큽니다"]


def _required_terms(res: Result, base: str) -> list[str]:
    """다듬은 문장에 반드시 남아야 하는 말: 출처·기간·시장 이름·품종·'전망 없음' 같은 한계 문구."""
    terms = [t for t in _KEEP_TERMS if t in base]
    terms += sorted({m for m in MARKET_ALIASES.values() if m in base})
    if res.cultivar and res.cultivar in base:
        terms.append(res.cultivar)
    return terms


def _polish(res: Result, base: str) -> None:
    """AI 핵심 문장 고르기: LLM은 답변 문장 번호만 고른다. 화면에는 코드가 만든 그 문장을 그대로 올린다.
    v0.2의 자유 요약은 숫자 검사를 통과하고도 이유를 잘못 이은 사례가 있어(2026-11-08 평가) 이 방식으로 바꿨다.
    문장을 새로 쓰지 않으므로 숫자·시장·이유를 엇갈리게 이을 수 없다."""
    if len(res.lines) < 2:
        return
    numbered = "\n".join(f"{i + 1}. {l}" for i, l in enumerate(res.lines))
    txt = _llm([
        {"role": "system", "content": "너는 제주 농가를 돕는 안내원이다. 아래 번호 붙은 답변 문장 가운데 질문에 가장 직접 답하는 문장을 "
                                      "1개 또는 2개 골라 번호만 쉼표로 답하라(예: 1,3). 다른 말은 쓰지 마라."},
        {"role": "user", "content": f"질문: {res.question}\n답변 문장:\n{numbered}"}], 20)
    if not txt:
        return
    if re.sub(r"[\d,\s.]", "", txt):
        res.llm_rejected = "번호가 아닌 답: " + txt.strip()[:40]
        return
    ids = [int(n) for n in re.findall(r"\d+", txt)]
    if not ids or len(ids) > 2 or any(not 1 <= i <= len(res.lines) for i in ids):
        res.llm_rejected = "범위를 벗어난 번호: " + txt.strip()[:40]
        return
    res.picked = sorted(set(i - 1 for i in ids))
    res.summary = " ".join(res.lines[i] for i in res.picked)
    res.llm_used = True
