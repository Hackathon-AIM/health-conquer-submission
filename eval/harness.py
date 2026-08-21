"""평가 하네스 — 1순위 작업. 담당: A

측정 없이는 개선이 없다. 금요일 14~16시에 이것부터 돌아가게 만든다.

하네스 = 모델을 반복해서 돌리는 자동화 껍데기
  ① 문제 불러오기 → ② 프롬프트 포맷 → ③ 모델 호출 → ④ 채점 → ⑤ 집계
이 5단계가 전부지만, 실제로는 병렬 처리·재시도·체크포인트가 지옥이라 도구를 쓴다.

사용:
    python -m eval.harness --config configs/mock.yaml --limit 5
    python -m eval.harness --config configs/live.yaml --split holdout
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from medai import config as cfgmod            # noqa: E402
from medai.contracts import SessionState      # noqa: E402
from medai.llm import LLM                     # noqa: E402
from medai.pipeline import Pipeline           # noqa: E402

from .coeval_adapter import CoEvalScorer      # noqa: E402
from .patient_sim import PatientAgent         # noqa: E402


# ─────────────────────────────────────────────────────────────
# 데이터셋
# ─────────────────────────────────────────────────────────────
SAMPLE = [
    {"id": "s1", "theme": "symptom",
     "question": "요즘 자꾸 어지럽고 두통이 있어요",
     "persona": "45세 남성, 고혈압으로 혈압약 복용 중, 2주 전부터 증상"},
    {"id": "s2", "theme": "drug",
     "question": "어제 술을 너무 많이 마셨는데 머리가 너무 아파요",
     "persona": "30대 남성, 주 2회 음주, 특이 병력 없음"},
    {"id": "s3", "theme": "emergency",
     "question": "갑자기 가슴이 조이고 식은땀이 나요",
     "persona": "58세 남성, 고혈압·당뇨, 흡연자"},
    {"id": "s4", "theme": "drug",
     "question": "타이레놀 하루에 최대 몇 알까지 먹어도 되나요?",
     "persona": "일반 성인"},
    {"id": "s5", "theme": "policy",
     "question": "건강검진에서 추가로 받은 검사는 보험이 되나요?",
     "persona": "직장가입자"},
]


def load_dataset(path: str | None, limit: int | None, split: str, cfg) -> list[dict]:
    if path and Path(path).exists():
        rows = [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
    else:
        rows = list(SAMPLE)

    # 홀드아웃 격리 — 최종 평가 세트가 다를 때 과적합이 드러난다.
    # 제공 문제 세트를 전부 튜닝에 쓰면 안 된다.
    import random
    rnd = random.Random(int(cfg["eval"].get("seed", 0)))
    idx = list(range(len(rows)))
    rnd.shuffle(idx)
    n_hold = int(len(rows) * float(cfg["eval"].get("holdout_ratio", 0.2)))
    hold = set(idx[:n_hold])
    if split == "holdout":
        rows = [r for i, r in enumerate(rows) if i in hold]
    elif split == "dev":
        rows = [r for i, r in enumerate(rows) if i not in hold]
    return rows[:limit] if limit else rows


# ─────────────────────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────────────────────
async def run_one(pipe: Pipeline, patient: PatientAgent, scorer: CoEvalScorer,
                  row: dict, max_turns: int) -> dict:
    """멀티턴 대화 하나를 끝까지 돌린다.

    루닛 하네스가 제공되면 patient_sim 을 그쪽으로 교체하면 된다.
    시작 질문은 고정, 후속 발화는 시뮬레이션 환자가 생성한다.
    """
    session = SessionState()
    convo: list[dict] = []
    q = row["question"]

    for turn in range(max_turns):
        res = await pipe.run_turn(q, session)
        convo.append({
            "turn": turn + 1,
            "user": q,
            "assistant": res.answer,
            "latency_ms": res.latency_ms,
            "trace": res.trace,
        })
        if turn + 1 >= max_turns:
            break
        q = await patient.next_utterance(row.get("persona", ""), convo)
        if not q:
            break

    score = await scorer.score(row, convo)
    return {"id": row["id"], "theme": row.get("theme"), "conversation": convo, **score}


async def main_async(args) -> None:
    cfg = cfgmod.load(args.config)
    run_id = uuid.uuid4().hex[:8]
    llm = LLM(cfg, run_id=run_id)
    pipe = Pipeline(cfg, llm)
    patient = PatientAgent(llm, cfg)
    scorer = CoEvalScorer(cfg, llm)

    rows = load_dataset(args.data, args.limit, args.split, cfg)
    sem = asyncio.Semaphore(int(cfg["eval"].get("concurrency", 8)))
    max_turns = int(cfg["eval"].get("max_turns", 3))

    async def guarded(r):
        async with sem:
            try:
                return await run_one(pipe, patient, scorer, r, max_turns)
            except Exception as e:
                return {"id": r["id"], "error": repr(e), "score": 0.0}

    t0 = time.perf_counter()
    results = await asyncio.gather(*[guarded(r) for r in rows])
    elapsed = time.perf_counter() - t0

    scores = [r.get("score", 0.0) for r in results if "error" not in r]
    errors = [r for r in results if "error" in r]
    lat = [t["latency_ms"].get("total", 0)
           for r in results for t in r.get("conversation", [])]

    summary = {
        "run_id": run_id,
        "config": cfg.get("name"),
        "config_path": args.config,
        "split": args.split,
        "n": len(results),
        "errors": len(errors),
        "score_mean": round(statistics.mean(scores), 4) if scores else 0.0,
        "score_worst": round(min(scores), 4) if scores else 0.0,   # worst-of-n 방어 확인용
        "latency_p50_ms": int(statistics.median(lat)) if lat else 0,
        "latency_max_ms": max(lat) if lat else 0,
        "wall_sec": round(elapsed, 1),
    }

    outdir = ROOT / "logs"
    outdir.mkdir(exist_ok=True)
    (outdir / f"run_{run_id}.json").write_text(
        json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if errors:
        print(f"\n⚠️ {len(errors)}건 실패:", [e["id"] for e in errors][:10])
    print(f"\n상세: logs/run_{run_id}.json / LLM 호출: logs/llm_{run_id}.jsonl")


def main() -> None:
    ap = argparse.ArgumentParser(description="Conquer Health 평가 하네스")
    ap.add_argument("--config", default="configs/mock.yaml")
    ap.add_argument("--data", default=None, help="jsonl 경로 (없으면 내장 샘플)")
    ap.add_argument("--split", default="all", choices=["all", "dev", "holdout"])
    ap.add_argument("--limit", type=int, default=None)
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
