"""라우터를 Patient Simulator 실측 질문으로 채점한다.

`data/patient_questions.json` 의 `case_id` 접두사가 곧 도메인 정답 라벨이다.
(pubmed-xxx → domain "pubmed"). 공짜로 얻은 라벨이니 쓴다.

    python3 eval_router.py --data ../health-hackerton/data/patient_questions.json

도메인 정확도 외에 응급·되묻기 판정 분포도 같이 찍는다. 그쪽은 정답 라벨이 없으니
숫자가 아니라 눈으로 보고 판단해야 한다.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from router import DOMAINS, classify  # noqa: E402


def load_env() -> None:
    for base in (os.path.dirname(os.path.abspath(__file__)),
                 os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "health-hackerton")):
        path = os.path.join(base, ".env")
        if not os.path.exists(path):
            continue
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def make_caller(url: str, key: str, model: str, sem: asyncio.Semaphore):
    async def call_fm(messages: list[dict], max_tokens: int, extra: dict | None = None) -> dict:
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": min(max_tokens, 2048),
            "chat_template_kwargs": {"enable_thinking": False},
            **(extra or {}),
        }
        async with sem:
            async with httpx.AsyncClient(timeout=180) as c:
                r = await c.post(
                    f"{url}/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json=payload,
                )
        r.raise_for_status()
        return r.json()

    return call_fm


async def main() -> int:
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="../health-hackerton/data/patient_questions.json")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    rows = json.load(open(args.data, encoding="utf-8"))
    url = os.environ.get("LUNIT_FM_API_URL", "https://model.hackathon.lunit.io").rstrip("/")
    key = os.environ.get("LUNIT_FM_API_KEY", "")
    model = os.environ.get("LUNIT_FM_MODEL", "Lunit/L2-preview")
    if not key:
        print("LUNIT_FM_API_KEY 없음", file=sys.stderr)
        return 1

    call_fm = make_caller(url, key, model, asyncio.Semaphore(args.concurrency))
    routes = await asyncio.gather(
        *(classify([{"role": "user", "content": r["question"]}], call_fm) for r in rows)
    )

    hit = 0
    confusion: dict[tuple[str, str], int] = collections.defaultdict(int)
    per_domain: dict[str, list[int]] = collections.defaultdict(list)
    results = []
    for row, route in zip(rows, routes):
        gold = (row.get("case_id") or "?").split("-")[0]
        got = route.domain
        ok = gold == got
        hit += ok
        confusion[(gold, got)] += 1
        per_domain[gold].append(1 if ok else 0)
        results.append({**row, "gold": gold, "route": route.as_dict(), "ok": ok})

    n = len(rows)
    print(f"\n=== 도메인 정확도: {hit}/{n} = {hit / n:.1%} ===\n")
    print(f"{'gold':<18}{'정답률':>8}   오분류")
    for gold in sorted(per_domain, key=lambda g: -len(per_domain[g])):
        v = per_domain[gold]
        wrong = [f"{got}×{c}" for (g, got), c in confusion.items() if g == gold and got != gold]
        print(f"{gold:<18}{sum(v)}/{len(v):<6}   {', '.join(wrong) or '-'}")

    print("\n=== 응급 판정 ===")
    for k, v in collections.Counter(r.urgency for r in routes).most_common():
        print(f"  {k}: {v}")
    for row, route in zip(rows, routes):
        if route.urgency == "emergency":
            print(f"    [응급] {row['question'][:70]}")

    print("\n=== 되묻기 판정 ===")
    for k, v in collections.Counter(r.context for r in routes).most_common():
        print(f"  {k}: {v}")
    for row, route in zip(rows, routes):
        if route.context == "missing_critical":
            print(f"    [되묻기] {row['question'][:55]}")
            print(f"             → {route.ask_back[:90]}")

    print("\n=== 페르소나 ===")
    for k, v in collections.Counter(r.persona for r in routes).most_common():
        print(f"  {k}: {v}")
    print("\n=== 날짜 민감 ===",
          sum(1 for r in routes if r.date_sensitive), "건")

    if args.out:
        json.dump(results, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print("\n저장:", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
