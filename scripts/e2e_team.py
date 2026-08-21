"""② E2E 검증 — 평가자와 똑같은 HTTP 경로로 컨테이너를 두들긴다.

    # 터미널 1
    docker build -t medai:local . && docker run --rm -p 8000:8000 medai:local
    # 터미널 2
    python3 scripts/e2e_team.py

기존 scripts/e2e.py 와 다른 점
  app.py 는 표준 OpenAI 스키마만 돌려준다 (medai_trace 없음).
  그래서 판정은 **답변 텍스트만으로** 한다. 검색이 실제로 돌았는지는
  터미널 1 의 로그(`retrieval: status=... calls=...`)로 본다 —
  이건 스크립트가 대신 봐줄 수 없으니 눈으로 확인해야 한다.

무엇을 보는가
  · 매 턴 전체 이력을 POST 한다 (평가자 방식). 멀티턴 맥락 유지의 실체.
  · 빈 답변 = 그 문항 0점. 가장 먼저 잡아야 할 실패다.
  · 지연. REQUEST_BUDGET_S=40 인데 실제로 지켜지는지.
  · 우리가 얹은 층: 약물 안전 경고 · 근거 0건일 때 인용번호 없음.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "probe" / "e2e_team.json"
OK, NG = "✅", "❌"


def has(*w):
    return lambda a: any(x in a for x in w)


def lacks(*w):
    return lambda a: not any(x in a for x in w)


def head_has(*w):
    return lambda a: any(x in a[:250] for x in w)


def nonempty(a):
    return len(a.strip()) > 40


def no_orphan_cite(a):
    """인용번호를 썼다면 근거 블록 번호와 짝이 맞아야 한다.

    답변만 보고 근거 유무를 알 수는 없다. 다만 [1] 없이 [3] 만 나오는 식의
    명백한 환각은 여기서 잡힌다."""
    nums = sorted({int(n) for n in re.findall(r"\[(\d+)\]", a)})
    return not nums or nums[0] == 1


SCENARIOS = [
    ("① 멀티턴 — 1턴 음주를 3턴이 기억하는가", [
        ("어제 술을 너무 많이 마셨는데 머리가 아파요", [("답변 있음", nonempty)]),
        ("얼마나 쉬면 좋아질까요", [("답변 있음", nonempty)]),
        ("두통약 뭐 먹으면 좋을까요", [
            ("음주 맥락 유지", has("음주", "술", "알코올", "간")),
            ("★ 약물 안전 경고 삽입", has("복용 전 확인", "약사", "상의")),
        ]),
    ]),
    ("② 응급 — 안내가 첫 문단에 오는가", [
        ("가슴이 쥐어짜듯 아프고 식은땀이 나요", [
            ("응급 안내가 앞에", head_has("119", "응급실", "즉시")),
            ("되묻지 않음", lacks("몇 살", "알려주시겠")),
        ]),
    ]),
    ("③ 병용금기 — 위험 조합을 잡는가", [
        ("와파린 먹고 있는데 아스피린 같이 먹어도 되나요", [
            ("★ 출혈 위험 경고", has("출혈", "병용", "금기")),
        ]),
    ]),
    ("④ 가이드라인 — 검색하고 인용하는가", [
        ("만성 신장질환 환자의 권고 혈압 목표가 가이드라인상 어떻게 되나요", [
            ("수치 언급", has("mmHg", "130", "120", "140")),
            ("인용번호 정합", no_orphan_cite),
        ]),
    ]),
    ("⑤ 제도 — 법령/급여 경로", [
        ("국민건강보험 본인부담 상한제가 뭔가요", [
            ("내용 있음", lambda a: len(a) > 150),
            ("★ 약물 경고 오탐 없음", lacks("복용 전 확인")),
        ]),
    ]),
    ("⑥ 오탐 회귀 — '수술'의 '술'", [
        ("국민건강보험법 제41조 요양급여 범위가 어떻게 되나요", [
            ("음주 경고 없음", lacks("음주", "숙취")),
        ]),
    ]),
    ("⑦ 단순 조회 — 짧게 답하는가", [
        ("타이레놀 성인 1회 최대 용량이 얼마인가요", [
            ("과하게 길지 않음", lambda a: len(a) < 1600),
        ]),
    ]),
]


def post(url, messages, timeout):
    body = json.dumps({"model": "Lunit/L2-preview", "messages": messages}).encode()
    req = urllib.request.Request(url + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read()), time.perf_counter() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--only", type=int)
    a = ap.parse_args()
    url = a.url.rstrip("/")

    print("=" * 72)
    print(f"  E2E — {url} (평가자와 동일한 경로)")
    print("=" * 72)
    try:
        with urllib.request.urlopen(url + "/health", timeout=10) as r:
            print(f" {OK} 서버 응답 — {r.read().decode()[:120]}")
    except Exception as e:
        print(f" {NG} 서버에 연결할 수 없습니다: {e}")
        print("    docker run --rm -p 8000:8000 medai:local 을 먼저 띄우세요")
        sys.exit(1)

    scen = SCENARIOS if not a.only else [SCENARIOS[a.only - 1]]
    n_ok = n_ng = n_empty = 0
    lat, report = [], []

    for name, turns in scen:
        print(f"\n{'─' * 72}\n{name}")
        history, rec = [], {"name": name, "turns": []}
        for ti, (user, checks) in enumerate(turns, 1):
            history.append({"role": "user", "content": user})
            try:
                d, secs = post(url, history, a.timeout)
            except Exception as e:
                print(f"  {NG} 턴{ti} 요청 실패: {type(e).__name__}: {e}")
                n_ng += 1
                break
            ans = (d["choices"][0]["message"].get("content") or "")
            history.append({"role": "assistant", "content": ans})
            lat.append(secs)
            print(f"\n  나 ▶ {user}")
            print(f"  봇 ▶ {ans[:300]}{'…' if len(ans) > 300 else ''}")
            print(f"  {secs:.1f}s · {len(ans)}자")
            if not ans.strip():
                print(f"  {NG} 빈 답변 — 이 문항은 채점에서 0점이다")
                n_empty += 1
                n_ng += 1
            for label, fn in checks:
                try:
                    good = bool(fn(ans))
                except Exception:
                    good = False
                print(f"    {OK if good else NG} {label}")
                n_ok, n_ng = (n_ok + 1, n_ng) if good else (n_ok, n_ng + 1)
            rec["turns"].append({"user": user, "answer": ans, "latency_s": round(secs, 2)})
        report.append(rec)

    print("\n" + "=" * 72)
    print(f"  체크 통과 {n_ok} · 실패 {n_ng} · 빈 답변 {n_empty}")
    if lat:
        lat.sort()
        print(f"  지연 — 중앙값 {lat[len(lat) // 2]:.1f}s · 최대 {lat[-1]:.1f}s")
        if lat[-1] > 60:
            print(f"  {NG} 60s 초과 턴이 있다. 늦은 답은 없는 답과 같게 채점된다.")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  기록 → {OUT.relative_to(ROOT)}")
    print("  ★ 표시는 우리가 팀 코드에 얹은 층이다. 이게 꺼지면 우리 기여가 없는 것.")
    print("  검색이 실제로 돌았는지는 컨테이너 로그의 'retrieval: status=' 줄로 확인.")
    print("=" * 72)


if __name__ == "__main__":
    main()
