"""레드플래그 사전을 HealthBench 에서 역추출한다.

담당: D

⚠️ 패턴을 창작하지 말 것.
   의사 262명이 "이 상황엔 응급 안내가 있어야 한다"고 표시해둔 실제 케이스에서
   사용자 발화를 뽑아 패턴화하는 것이, 의료 배경 없이 정확도를 얻는 유일한 경로다.

⚠️ 언어 문제 (중요)
   HealthBench 는 49개 언어 멀티링구얼이지만 응급 예시의 대부분은 영어다.
   우리 챗봇은 한국어이므로 영어 패턴을 그대로 쓸 수 없다.
   → 이 스크립트는 3단계로 처리한다.
     1) 기계적 추출  : emergency 루브릭이 걸린 예시의 사용자 발화를 모은다 (LLM 불필요)
     2) 한국어 변환  : LLM 에게 그 발화들을 보여주고 한국어 정규식을 쓰게 한다 (선택)
     3) 검증        : 만든 패턴을 원본 코퍼스에 되돌려 재현율·오탐률을 측정한다 ★

   3단계가 핵심이다. 이게 없으면 "그럴듯한 패턴"이지 "검증된 패턴"이 아니다.

사용:
    # 1단계만 — 어떤 발화들이 응급으로 표시됐는지 눈으로 본다
    python data/extract_redflags.py --dump

    # 전체 — LLM 으로 한국어 패턴 생성 + 검증
    python data/extract_redflags.py --config configs/openai.yaml --out data/redflags.yaml

    # 기존 패턴만 검증 (LLM 불필요)
    python data/extract_redflags.py --validate-only

⚠️ 산출물과 다운로드한 jsonl 을 공개 저장소에 커밋하지 말 것 (학습 데이터 오염 방지).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from medai import config as cfgmod   # noqa: E402
from medai.llm import LLM            # noqa: E402

sys.path.insert(0, str(ROOT))
from eval.healthbench import fetch   # noqa: E402

# 루브릭 텍스트에서 '응급 의뢰' 항목을 식별하는 신호.
# example_tags 에 theme:emergency_referral 이 있으면 그것도 쓴다.
_EMERGENCY_RUBRIC = re.compile(
    r"(emergency|911|119|urgent(ly)?\s+(care|medical)|ambulance|ER\b|"
    r"emergency\s+(room|department|services)|immediate(ly)?\s+(medical|seek)|"
    r"call\s+(emergency|911)|응급|즉시\s*(내원|병원)|구급)",
    re.I,
)

CATEGORIES = [
    "cardiac", "neuro", "respiratory", "bleeding",
    "psych", "anaphylaxis", "abdominal", "obstetric", "infection",
]


# ─────────────────────────────────────────────────────────────
# 1단계 · 기계적 추출 (LLM 불필요)
# ─────────────────────────────────────────────────────────────
def collect(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """응급 안내가 요구된 예시 / 그렇지 않은 예시로 가른다.

    후자가 중요하다. 오탐률(false positive)을 재려면 '응급이 아닌' 집합이 필요하다.
    """
    emerg, normal = [], []
    for r in rows:
        tags = " ".join(r.get("example_tags") or [])
        rubrics = r.get("rubrics") or []

        # 가점 항목(points > 0) 중 응급 의뢰를 요구하는 게 있는가
        hit = any(
            rb.get("points", 0) > 0 and _EMERGENCY_RUBRIC.search(rb.get("criterion", ""))
            for rb in rubrics
        ) or "emergency" in tags.lower()

        users = [m["content"] for m in (r.get("prompt") or []) if m.get("role") == "user"]
        if not users:
            continue
        item = {
            "prompt_id": r.get("prompt_id"),
            "tags": r.get("example_tags") or [],
            "user_text": "\n".join(users),
            "first_user": users[0],
            "emergency_rubrics": [
                rb["criterion"] for rb in rubrics
                if rb.get("points", 0) > 0 and _EMERGENCY_RUBRIC.search(rb.get("criterion", ""))
            ][:3],
        }
        (emerg if hit else normal).append(item)
    return emerg, normal


def surface_phrases(items: list[dict], n: int = 40) -> list[tuple[str, int]]:
    """발화에서 자주 나오는 표현을 노출시킨다.

    패턴을 자동으로 만들지는 않는다. 사람이 보고 고르는 게 낫다.
    """
    cnt: Counter = Counter()
    for it in items:
        t = it["first_user"].lower()
        for m in re.finditer(r"[a-z가-힣]+(?:\s+[a-z가-힣]+){1,3}", t):
            g = m.group(0).strip()
            if 6 <= len(g) <= 40:
                cnt[g] += 1
    return cnt.most_common(n)


# ─────────────────────────────────────────────────────────────
# 2단계 · 한국어 패턴 생성 (LLM)
# ─────────────────────────────────────────────────────────────
PROMPT = """당신은 한국어 의료 챗봇의 응급 신호 탐지 규칙을 만드는 엔지니어입니다.

아래는 의사들이 "이 상황에는 응급 의료 연락 안내가 반드시 있어야 한다"고 표시한
실제 사용자 발화들입니다. (영어가 다수이나 한국 사용자를 대상으로 합니다)

이 발화들이 나타내는 응급 상황을, **한국 사용자가 실제로 쓸 법한 표현**의
Python 정규식으로 바꾸세요.

[매우 중요한 제약]
1. 한국어는 부분문자열 오탐이 잦습니다. 반드시 피하세요.
   - r"술" 은 "수술·기술·예술"에 매칭됩니다 → (?<![가-힣])술 처럼 쓰세요
   - 짧은 한 글자 패턴은 원칙적으로 금지합니다
2. 과탐지는 미탐지만큼 나쁩니다. 모든 답변에 119가 붙으면 관련성 점수가 깎입니다.
   "머리가 아파요", "감기 기운이 있어요" 같은 일상 표현은 절대 매칭되면 안 됩니다.
3. 조사가 붙는 것을 고려하세요 ("가슴이", "가슴을", "가슴 이")
4. 카테고리는 다음 중에서만 고르세요:
   {categories}

[사용자 발화 예시]
{samples}

[의사들이 요구한 응급 안내 문구 예시]
{rubrics}

JSON으로만 답하세요:
{{"patterns": {{"cardiac": ["정규식1", "정규식2"], "neuro": [...]}}}}

카테고리당 3~8개, 전체 30~50개 정도가 적당합니다."""


async def generate_patterns(emerg: list[dict], llm: LLM, n_samples: int = 60) -> dict:
    samples = "\n".join(f"- {it['first_user'][:220]}" for it in emerg[:n_samples])
    rubrics = "\n".join(
        f"- {c[:160]}" for it in emerg[:40] for c in it["emergency_rubrics"][:1]
    )[:4000]
    raw = await llm.chat_json("classifier", [{
        "role": "user",
        "content": PROMPT.format(
            categories=", ".join(CATEGORIES),
            samples=samples[:12000],
            rubrics=rubrics,
        ),
    }])
    pats = raw.get("patterns") or {}
    # 컴파일 안 되는 패턴은 버린다
    clean: dict[str, list[str]] = {}
    for cat, ps in pats.items():
        if cat not in CATEGORIES:
            continue
        ok = []
        for p in ps:
            try:
                re.compile(p)
                ok.append(p)
            except re.error:
                print(f"  [skip] 컴파일 실패: {cat} / {p}")
        if ok:
            clean[cat] = ok
    return clean


# ─────────────────────────────────────────────────────────────
# 3단계 · 검증 ★ 여기가 핵심
# ─────────────────────────────────────────────────────────────
def validate(patterns: dict[str, list[str]],
             emerg: list[dict], normal: list[dict]) -> dict:
    """만든 패턴을 원본 코퍼스에 되돌려 재현율·오탐률을 잰다.

    이게 없으면 '그럴듯한 패턴'이지 '검증된 패턴'이 아니다.

    ⚠️ HealthBench 응급 예시는 대부분 영어라, 한국어 패턴의 재현율은 낮게 나온다.
       그 숫자를 목표로 삼지 말 것. 대신 아래 둘을 보라.
         · 오탐률(FP) — 일상 표현에 걸리면 안 된다. 이건 언어와 무관하게 유효하다
         · 커버리지 — 각 카테고리에 패턴이 있는가
    """
    compiled = {c: [re.compile(p) for p in ps] for c, ps in patterns.items()}

    def scan(text: str) -> str | None:
        for c, ps in compiled.items():
            for p in ps:
                if p.search(text):
                    return c
        return None

    tp = sum(1 for it in emerg if scan(it["user_text"]))
    fp = sum(1 for it in normal if scan(it["user_text"]))

    # 한국어 일상 표현 오탐 테스트 — 여기서 하나라도 걸리면 과탐지다
    BENIGN = [
        "머리가 조금 아파요", "감기 기운이 있어요", "타이레놀 하루 몇 알까지 먹어도 되나요?",
        "건강검진 보험이 되나요?", "처치·수술 및 그 밖의 치료", "운동 후에 근육통이 있어요",
        "비타민 D 보충제 추천해주세요", "혈압약을 매일 먹고 있어요", "잠이 잘 안 와요",
        "소화가 잘 안 되는데 소화제 먹어도 될까요?", "요즘 스트레스를 많이 받아요",
        "예방접종은 언제 맞아야 하나요?",
    ]
    benign_hits = [(t, scan(t)) for t in BENIGN if scan(t)]

    # 한국어 응급 표현 정탐 테스트 — 여기는 다 걸려야 한다
    KO_EMERGENCY = [
        "갑자기 가슴이 조이고 식은땀이 나요", "숨을 못 쉬겠어요",
        "갑자기 한쪽 팔에 힘이 빠져요", "지금까지 겪어본 것 중 가장 심한 두통이에요",
        "피를 토했어요", "죽고 싶다는 생각이 들어요",
        "목이 붓고 숨쉬기가 힘들어요", "의식을 잃었다가 깨어났어요",
    ]
    ko_miss = [t for t in KO_EMERGENCY if not scan(t)]

    return {
        "n_patterns": sum(len(v) for v in patterns.values()),
        "n_categories": len(patterns),
        "corpus_recall": round(tp / len(emerg), 3) if emerg else 0.0,
        "corpus_fp_rate": round(fp / len(normal), 3) if normal else 0.0,
        "korean_emergency_recall": round(
            (len(KO_EMERGENCY) - len(ko_miss)) / len(KO_EMERGENCY), 3),
        "korean_missed": ko_miss,
        "benign_false_positives": benign_hits,   # ★ 비어 있어야 한다
    }


def to_yaml(patterns: dict[str, list[str]], stats: dict) -> str:
    lines = [
        "# 레드플래그 패턴 — HealthBench emergency 루브릭에서 역추출",
        "#",
        "# 생성: python data/extract_redflags.py",
        f"# 패턴 {stats['n_patterns']}개 / {stats['n_categories']}카테고리",
        f"# 한국어 응급 재현율 {stats['korean_emergency_recall']} / "
        f"일상 표현 오탐 {len(stats['benign_false_positives'])}건",
        "#",
        "# ⚠️ 이 파일을 공개 저장소에 커밋하지 말 것 (HealthBench 오염 방지)",
        "# ⚠️ 사람이 한 번 읽고 과탐지 소지가 있는 패턴을 지울 것",
        "",
    ]
    for cat in CATEGORIES:
        if cat not in patterns:
            continue
        lines.append(f"{cat}:")
        for p in patterns[cat]:
            lines.append(f"  - '{p}'")
        lines.append("")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
async def main_async(a) -> None:
    rows = fetch(a.variant)
    emerg, normal = collect(rows)
    print(f"\n[1단계] 응급 예시 {len(emerg)}건 / 비응급 {len(normal)}건")

    if a.dump:
        print("\n─── 응급으로 표시된 사용자 발화 (상위 25건) ───")
        for it in emerg[:25]:
            print(f"\n· {it['first_user'][:200]}")
            if it["emergency_rubrics"]:
                print(f"    └ 루브릭: {it['emergency_rubrics'][0][:120]}")
        print("\n─── 자주 나오는 표현 ───")
        for g, c in surface_phrases(emerg, 30):
            print(f"  {c:4}  {g}")
        return

    if a.validate_only:
        import yaml
        cur = yaml.safe_load((ROOT / "data" / "redflags.yaml").read_text(encoding="utf-8"))
        stats = validate(cur, emerg, normal)
        print("\n[3단계] 현재 redflags.yaml 검증")
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return

    cfg = cfgmod.load(a.config)
    llm = LLM(cfg)
    if not llm.enabled:
        print("\n⚠️ base_url 이 없어 패턴 생성을 건너뜁니다. --dump 로 발화를 먼저 보세요.")
        return

    print("\n[2단계] 한국어 패턴 생성 중...")
    patterns = await generate_patterns(emerg, llm)
    print(f"  {sum(len(v) for v in patterns.values())}개 / {len(patterns)}카테고리")

    print("\n[3단계] 검증")
    stats = validate(patterns, emerg, normal)
    print(json.dumps(stats, ensure_ascii=False, indent=2))

    if stats["benign_false_positives"]:
        print("\n❌ 일상 표현에 오탐이 있습니다. 해당 패턴을 손보고 다시 돌리세요.")
    if stats["korean_missed"]:
        print(f"\n⚠️ 한국어 응급 표현 {len(stats['korean_missed'])}개를 놓쳤습니다.")

    out = Path(a.out)
    out.write_text(to_yaml(patterns, stats), encoding="utf-8")
    print(f"\n저장: {out}")
    print("→ 반드시 사람이 한 번 읽고 과탐지 소지가 있는 패턴을 지울 것")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="consensus", choices=["consensus", "hard", "full"])
    ap.add_argument("--config", default="configs/openai.yaml")
    ap.add_argument("--out", default="data/redflags.generated.yaml")
    ap.add_argument("--dump", action="store_true", help="발화만 출력 (LLM 불필요)")
    ap.add_argument("--validate-only", action="store_true", help="현재 yaml 검증만")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
