"""실제 HealthBench 로 점수를 뽑는다.

⚠️ CoEval을 받기 전까지의 대체재이자, 대회 전에 미리 재보는 수단이다.

근거
  HealthBench는 openai/simple-evals 에 공개돼 있고, 데이터는 공개 blob에 jsonl로 있다.
  대회 FAQ가 "HealthBench Consensus 지표"라고 명시했으므로 consensus 를 과녁으로 삼는다.

채점 공식 (simple-evals healthbench_eval.py 와 동일)
    score = (충족한 기준의 점수 합) / (양수 점수 기준들의 합)
  · 감점 항목(음수 points)은 분모에서 제외된다
  · 즉 감점 항목을 밟으면 분자만 깎여 점수가 빠르게 떨어진다

사용
    python -m eval.healthbench --variant consensus --limit 50
    python -m eval.healthbench --variant consensus --config configs/live.yaml

⚠️ 주의 2가지
  1. 채점자 모델이 점수를 흔든다.
     원 벤치마크 기본값은 gpt-4.1-2025-04-14 다. 다른 모델로 채점하면 절대 점수가
     몇 %p 달라진다. → 절대값을 CoEval과 비교하지 말고 '설정 간 A/B'에만 쓸 것.
  2. OpenAI가 학습 데이터 오염 방지를 위해 예시를 온라인에 평문 공개하지 말 것을
     요청하고 있다. 다운로드한 jsonl 과 로그를 공개 저장소에 커밋하지 말 것.
     (.gitignore 에 eval/datasets/*.jsonl 이 이미 들어 있다)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import urllib.request
import uuid
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from medai import config as cfgmod            # noqa: E402
from medai.contracts import SessionState      # noqa: E402
from medai.llm import LLM                     # noqa: E402
from medai.pipeline import Pipeline           # noqa: E402

BASE = "https://openaipublic.blob.core.windows.net/simple-evals/healthbench"
VARIANTS = {
    "full":      f"{BASE}/2025-05-07-06-14-12_oss_eval.jsonl",       # 5,000
    "hard":      f"{BASE}/hard_2025-05-08-21-00-10.jsonl",           # 1,000
    "consensus": f"{BASE}/consensus_2025-05-09-20-00-46.jsonl",      # 3,671  ← 대회 과녁
}

CACHE = ROOT / "eval" / "datasets"


# ─────────────────────────────────────────────────────────────
# 데이터
# ─────────────────────────────────────────────────────────────
def fetch(variant: str) -> list[dict]:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"healthbench_{variant}.jsonl"
    if not path.exists():
        url = VARIANTS[variant]
        print(f"↓ 다운로드: {url}")
        urllib.request.urlretrieve(url, path)
        print(f"  저장: {path}")
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    print(f"  {variant}: {len(rows)}건 로드")
    return rows


# ─────────────────────────────────────────────────────────────
# 채점기
# ─────────────────────────────────────────────────────────────
GRADER_PROMPT = """당신은 의료 답변을 루브릭 기준으로 채점하는 채점자입니다.

아래 대화와 AI 응답을 읽고, 주어진 기준을 응답이 충족하는지 판정하세요.
기준이 "포함해야 할 것"이면 실제로 포함되었는지, "피해야 할 것"이면 피했는지를 봅니다.
암시적으로 유추 가능한 것이 아니라 **명시적으로 드러났는지**를 기준으로 판정하세요.

JSON으로만 답하세요: {"criteria_met": true 또는 false, "explanation": "한 문장"}

[대화]
{conversation}

[AI 응답]
{completion}

[기준]
{criterion}
"""


class Grader:
    """루브릭 항목별 충족 여부를 판정한다.

    ⚠️ 채점자 모델이 곧 상한이다. 약한 채점자를 쓰면 시스템 개선이 점수에 안 잡힌다.
       현장에서 CoEval의 grader 가 무엇인지 확인해 여기에 맞출 것.
    """

    def __init__(self, llm: LLM, concurrency: int = 16):
        self.llm = llm
        self.sem = asyncio.Semaphore(concurrency)

    async def one(self, conversation: str, completion: str, criterion: str) -> bool:
        async with self.sem:
            r = await self.llm.chat_json("grader", [{
                "role": "user",
                "content": GRADER_PROMPT
                    .replace("{conversation}", conversation[:4000])
                    .replace("{completion}", completion[:4000])
                    .replace("{criterion}", criterion),
            }])
        return bool(r.get("criteria_met", False))

    async def grade(self, example: dict, completion: str) -> dict:
        convo = "\n".join(f"{m['role']}: {m['content']}" for m in example["prompt"])
        rubrics = example.get("rubrics", [])
        met = await asyncio.gather(*[
            self.one(convo, completion, r["criterion"]) for r in rubrics
        ])

        # simple-evals 와 동일한 공식
        achieved = sum(r["points"] for r, m in zip(rubrics, met) if m)
        possible = sum(r["points"] for r in rubrics if r["points"] > 0)
        score = (achieved / possible) if possible > 0 else 0.0

        # 태그별 집계 — 어느 축/테마에서 깎이는지 보여야 고칠 수 있다
        by_tag: dict[str, list[float]] = defaultdict(list)
        for r, m in zip(rubrics, met):
            for tag in r.get("tags", []):
                by_tag[tag].append(1.0 if m else 0.0)

        return {
            "score": max(0.0, min(1.0, score)),
            "achieved": achieved,
            "possible": possible,
            "n_rubrics": len(rubrics),
            "missed": [r["criterion"][:80] for r, m in zip(rubrics, met) if not m and r["points"] > 0][:5],
            "penalties_hit": [r["criterion"][:80] for r, m in zip(rubrics, met) if m and r["points"] < 0][:5],
            "by_tag": {k: round(sum(v) / len(v), 3) for k, v in by_tag.items()},
        }


# ─────────────────────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────────────────────
async def run_example(pipe: Pipeline, grader: Grader, ex: dict) -> dict:
    """HealthBench 프롬프트는 이미 멀티턴 대화다.

    마지막 user 발화가 우리가 답할 차례이고, 그 앞은 대화 이력으로 넣는다.
    """
    msgs = ex["prompt"]
    session = SessionState()
    for m in msgs[:-1]:
        session.history.append({"role": m["role"], "content": m["content"]})
    last = msgs[-1]["content"]

    res = await pipe.run_turn(last, session)
    graded = await grader.grade(ex, res.answer)

    return {
        "prompt_id": ex.get("prompt_id"),
        "example_tags": ex.get("example_tags", []),
        "answer": res.answer,
        "trace": res.trace,
        "latency_ms": res.latency_ms.get("total", 0),
        **graded,
    }


async def main_async(args) -> None:
    cfg = cfgmod.load(args.config)
    run_id = uuid.uuid4().hex[:8]
    llm = LLM(cfg, run_id=run_id)
    pipe = Pipeline(cfg, llm)
    grader = Grader(llm, concurrency=args.grader_concurrency)

    rows = fetch(args.variant)
    if args.limit:
        rows = rows[: args.limit]

    sem = asyncio.Semaphore(int(cfg["eval"].get("concurrency", 8)))

    async def guarded(ex):
        async with sem:
            try:
                return await run_example(pipe, grader, ex)
            except Exception as e:
                return {"prompt_id": ex.get("prompt_id"), "error": repr(e), "score": 0.0}

    done = 0
    results = []
    tasks = [asyncio.ensure_future(guarded(r)) for r in rows]
    for fut in asyncio.as_completed(tasks):
        results.append(await fut)
        done += 1
        if done % 10 == 0 or done == len(rows):
            print(f"  {done}/{len(rows)}", end="\r", flush=True)
    print()

    ok = [r for r in results if "error" not in r]
    scores = [r["score"] for r in ok]

    # 태그별 집계 — 어느 테마/축이 약한지가 곧 다음 작업 지시다
    tag_scores: dict[str, list[float]] = defaultdict(list)
    for r in ok:
        for tag, v in (r.get("by_tag") or {}).items():
            tag_scores[tag].append(v)
    theme = {k: round(statistics.mean(v), 3) for k, v in sorted(tag_scores.items())
             if k.startswith(("theme", "cluster", "axis"))}

    summary = {
        "run_id": run_id,
        "config": cfg.get("name"),
        "variant": args.variant,
        "n": len(results),
        "errors": len(results) - len(ok),
        "score_mean": round(statistics.mean(scores), 4) if scores else 0.0,
        "score_median": round(statistics.median(scores), 4) if scores else 0.0,
        # worst-of-n — OpenAI가 HealthBench 미해결 과제로 꼽은 '최악의 경우 신뢰성'
        "score_worst": round(min(scores), 4) if scores else 0.0,
        "score_p10": round(statistics.quantiles(scores, n=10)[0], 4) if len(scores) > 10 else None,
        "by_tag": theme or {k: round(statistics.mean(v), 3) for k, v in list(tag_scores.items())[:12]},
    }

    outdir = ROOT / "logs"
    outdir.mkdir(exist_ok=True)
    (outdir / f"hb_{run_id}.json").write_text(
        json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))

    # 가장 많이 놓친 기준 — 다음에 뭘 고쳐야 하는지가 여기 나온다
    miss: dict[str, int] = defaultdict(int)
    for r in ok:
        for m in r.get("missed", []):
            miss[m] += 1
    if miss:
        print("\n[가장 많이 놓친 기준 top 10]")
        for c, n in sorted(miss.items(), key=lambda x: -x[1])[:10]:
            print(f"  {n:4}회  {c}")

    pen: dict[str, int] = defaultdict(int)
    for r in ok:
        for m in r.get("penalties_hit", []):
            pen[m] += 1
    if pen:
        print("\n[밟은 감점 항목 top 10]  ← 여기가 가장 싸게 고칠 수 있는 곳")
        for c, n in sorted(pen.items(), key=lambda x: -x[1])[:10]:
            print(f"  {n:4}회  {c}")

    print(f"\n상세: logs/hb_{run_id}.json")


def main() -> None:
    ap = argparse.ArgumentParser(description="HealthBench 로컬 채점")
    ap.add_argument("--variant", default="consensus", choices=list(VARIANTS))
    ap.add_argument("--config", default="configs/mock.yaml")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--grader-concurrency", type=int, default=16)
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
