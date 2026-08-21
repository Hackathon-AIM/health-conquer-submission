"""Patient Simulator ↔ 우리 서버 대화 루프.

전문가 평가(chat 품질)가 같은 제출물로 이뤄지므로, 벤치마크 점수와 별개로
시뮬레이터 상대 대화 기록을 눈으로 검토해야 한다. 이 스크립트가 그 기록을 만든다.

    export LUNIT_FM_API_KEY=lunit_...
    python serve.py --config configs/l2_live.yaml --port 8080 &
    python scripts/sim_loop.py --n 5 --turns 3 --server http://localhost:8080

규칙 (대시보드 가이드):
  · 첫 질문: 빈 messages 로 POST. 매번 새 질문이 나온다 (~14s)
  · 후속: 받은 질문 + 우리 답변을 그대로 쌓아 전체 history 재전송 (~8s)
  · 첫 질문을 절대 수정하지 말 것 — continuation 이 깨진다
  · 3턴 정도에서 중단 — 길어지면 같은 질문을 반복한다
  · 404 → 빈 messages 로 새 대화 / 502 → 재시도
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

import httpx

SIM_URL = "https://patient.hackathon.lunit.io/v1/chat/completions"
SIM_MODEL = "patient-simulator-ko"
OUT = Path(__file__).resolve().parents[1] / "logs" / "sim"


async def sim_next(client: httpx.AsyncClient, messages: list[dict]) -> str:
    """시뮬레이터에서 다음 user 발화를 받는다. 502 는 재시도, 404 는 예외."""
    headers = {"Authorization": f"Bearer {os.environ['LUNIT_FM_API_KEY']}"}
    for attempt in range(3):
        r = await client.post(SIM_URL, headers=headers, timeout=60.0,
                              json={"model": SIM_MODEL, "messages": messages})
        if r.status_code == 502:
            await asyncio.sleep(2 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    raise RuntimeError("simulator 502 3회")


async def our_answer(client: httpx.AsyncClient, server: str,
                     messages: list[dict]) -> str:
    r = await client.post(f"{server}/v1/chat/completions", timeout=180.0,
                          json={"model": "medai", "messages": messages})
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


async def one_conversation(server: str, turns: int, idx: int) -> dict:
    convo: list[dict] = []      # 시뮬레이터 관점 history (user=환자, assistant=우리)
    async with httpx.AsyncClient() as client:
        for t in range(turns):
            q = await sim_next(client, convo)
            convo.append({"role": "user", "content": q})   # ⚠️ 받은 그대로 보존
            a = await our_answer(client, server, convo)
            convo.append({"role": "assistant", "content": a})
            print(f"  [{idx}] 턴{t + 1} 환자: {q[:60]}…")
            print(f"  [{idx}]       우리: {a[:60]}…")
    return {"idx": idx, "turns": turns, "messages": convo}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--turns", type=int, default=3)
    ap.add_argument("--server", default="http://localhost:8080")
    a = ap.parse_args()

    if not os.getenv("LUNIT_FM_API_KEY"):
        raise SystemExit("LUNIT_FM_API_KEY 를 설정하세요 (.env 또는 export)")

    OUT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%m%d_%H%M%S")
    results = []
    for i in range(a.n):        # 순차 실행 — 동시성은 지원되지만 기록 읽기가 우선
        try:
            results.append(await one_conversation(a.server, a.turns, i))
        except Exception as e:
            print(f"  [{i}] 실패: {e!r}")
            results.append({"idx": i, "error": repr(e)})

    path = OUT / f"sim_{stamp}.json"
    path.write_text(json.dumps(results, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(f"\n대화 {len(results)}건 저장 → {path}")
    print("이 파일을 사람이 읽고 어색한 턴을 찾는 것까지가 이 실험이다.")


if __name__ == "__main__":
    asyncio.run(main())
