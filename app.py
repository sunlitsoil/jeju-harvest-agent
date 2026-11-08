"""센터 PC용 웹 화면. 외부 패키지 없이 `python3 app.py` 후 브라우저에서 http://127.0.0.1:8765 접속."""
from __future__ import annotations

import html
import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from jeju_agent.agent import answer

PORT = int(os.environ.get("PORT", "8765"))
EXAMPLES = ["월동무 지금 내도 될까?", "월동무 언제 내면 값이 좋아?", "당근 조금 기다리면 값 오를까?",
            "양배추 어느 시장이 값을 잘 쳐줘?", "당근 엄궁시장 시세 어때?"]


def _md(v) -> str:
    """20260912·2026-09-12 → 9/12. 날짜가 아니면(「11월 후반」) 그대로."""
    d = str(v).replace("-", "")
    return f"{int(d[4:6])}/{int(d[6:8])}" if len(d) == 8 and d.isdigit() else str(v)


def svg_chart(ch: dict | None) -> str:
    if not ch or not ch.get("y"):
        return ""
    w, h, pad = 640, 220, 40
    ys = [float(v) for v in ch["y"]]
    lo, hi = min(ys), max(ys)
    span = (hi - lo) or 1
    n = len(ys)
    pts = [(pad + i * (w - 2 * pad) / max(n - 1, 1), h - pad - (v - lo) / span * (h - 2 * pad)) for i, v in enumerate(ys)]
    split = ch.get("split") or n  # split 앞은 실제 값, 뒤는 허브 전망(점선)
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts[:split])
    fpath = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts[max(split - 1, 0):]) if split < n else ""
    labels = "".join(
        f'<text x="{x:.0f}" y="{h - 12}" font-size="11" text-anchor="middle">{html.escape(_md(ch["x"][i]))}</text>'
        for i, (x, _) in enumerate(pts) if i % max(1, n // 6) == 0)
    return (f'<figure><figcaption>{html.escape(ch["title"])}</figcaption>'
            f'<svg viewBox="0 0 {w} {h}" width="100%" role="img">'
            f'<line x1="{pad}" y1="{h - pad}" x2="{w - pad}" y2="{h - pad}" stroke="#999"/>'
            f'<text x="4" y="{pad}" font-size="11">{hi:,.0f}</text><text x="4" y="{h - pad}" font-size="11">{lo:,.0f}</text>'
            f'<polyline fill="none" stroke="#1d5fa8" stroke-width="2" points="{path}"/>'
            + (f'<polyline fill="none" stroke="#c0392b" stroke-width="2" stroke-dasharray="6 4" points="{fpath}"/>'
               f'<text x="{w - pad}" y="14" font-size="12" text-anchor="end" fill="#c0392b">점선 = 제주데이터허브 전망</text>' if fpath else "")
            + f'{labels}</svg></figure>')


PAGE = """<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>밭에서 묻는 시세 비서</title>
<style>body{{font-family:'Noto Sans KR',sans-serif;max-width:760px;margin:24px auto;padding:0 16px;font-size:18px;line-height:1.6;color:#222}}
h1{{font-size:24px}}input{{width:100%;font-size:20px;padding:10px;box-sizing:border-box}}button{{font-size:18px;padding:8px 14px;margin:4px 4px 0 0}}
.ans{{background:#f3f7fb;border-left:4px solid #1d5fa8;padding:4px 16px;margin:16px 0}}.ans p{{margin:8px 0}}
.sum{{background:#fff8e6;border-left:4px solid #d39e00;padding:10px 16px;margin:16px 0 0}}.sum b{{font-size:14px;color:#8a6d00}}table{{border-collapse:collapse;width:100%;font-size:15px}}
td{{border-bottom:1px solid #ddd;padding:4px 6px;vertical-align:top}}details{{margin:12px 0;font-size:15px}}.note{{font-size:14px;color:#555}}</style>
<h1>밭에서 묻는 시세 비서 <small style="font-size:14px">제주 월동채소 출하 판단 도우미</small></h1>
<form method="get"><input name="q" value="{q}" placeholder="예: 월동무 지금 내도 될까?" autofocus><br><button>물어보기</button></form>
<div>{examples}</div>{body}
<p class="note">자료: 제주데이터허브 응용서비스 「농산물 분석」(원자료 aT 도매유통정보시스템)과 「품종별 가격 전망」. 가격은 전국 도매시장 경락가(운송비 차감 전)이며 제주산 출하분만 따로 집계했는지는 확인하지 못했습니다. 판매 결정은 농가가 직접 하세요.</p></html>"""


def render(q: str) -> str:
    ex = "".join(f'<button onclick="location.href=\'?q={urllib.parse.quote(e)}\'" type="button">{html.escape(e)}</button>' for e in EXAMPLES)
    body = ""
    if q:
        try:
            r = answer(q)
            planner = "LLM이 도구를 골랐습니다" if r.planner == "llm" else "규칙으로 도구를 골랐습니다"
            rows = "".join("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in row) + "</tr>" for row in r.evidence)
            steps = "".join(f"<li>{html.escape(s.tool)} · {s.ms}ms {html.escape(s.note)}</li>" for s in r.steps)
            steps = f"<p>{planner}</p>" + steps
            cav = "".join(f"<li>{html.escape(c)}</li>" for c in r.caveats)
            summary = (f'<div class="sum"><b>AI가 고른 핵심 문장 · 아래 답변에서 그대로 옮김</b><br>{html.escape(r.summary)}</div>'
                       if r.summary else "")
            fu = "".join(f'<button onclick="location.href=\'?q={urllib.parse.quote(f)}\'" type="button">{html.escape(f)}</button>' for f in r.followups)
            fu = f'<p class="note">이어서 물어보기</p><div>{fu}</div>' if fu else ""
            body = (f'{summary}<div class="ans">{"".join(f"<p>{html.escape(l)}</p>" for l in (r.lines or [r.answer]))}</div>{fu}{svg_chart(r.chart)}'
                    f'<details open><summary>근거 숫자</summary><table>{rows}</table></details>'
                    f'<details><summary>에이전트가 한 일</summary><ol>{steps}</ol>'
                    f'{"<p>AI 요약 폐기: " + html.escape(r.llm_rejected) + "</p>" if r.llm_rejected else ""}</details>'
                    f'<ul class="note">{cav}</ul>')
        except Exception as e:  # 화면에 오류를 그대로 보여 주고 서버는 계속 돈다
            body = f'<div class="ans">자료를 가져오지 못했습니다. 인터넷 연결을 확인해 주세요. ({html.escape(str(e))})</div>'
    return PAGE.format(q=html.escape(q), examples=ex, body=body)


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        q = (qs.get("q") or [""])[0].strip()
        if u.path == "/api/ask":
            data = json.dumps(answer(q).to_dict(), ensure_ascii=False, default=str).encode("utf-8")
            ctype = "application/json; charset=utf-8"
        elif u.path == "/":
            data, ctype = render(q).encode("utf-8"), "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    print(f"브라우저에서 http://127.0.0.1:{PORT} 을 여세요. 끝내려면 Ctrl+C")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
