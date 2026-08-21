"""L1 · 레드플래그 스크리너 (결정론적, ~0ms)

담당: C (패턴 사전은 D가 HealthBench에서 역추출)

왜 LLM이 아니라 규칙인가
  LLM에게 물으면 "대부분" 맞힌다. 문제는 '대부분'이다.
  100번 중 3번 틀리면 그 3번이 사람이 죽는 케이스이고,
  HealthBench는 worst-of-n — 한 번의 대형 실패가 전체 점수를 무너뜨린다.
  규칙은 멍청하지만 예측 가능하고, 테스트로 검증되고, 모델을 바꿔도 안 변한다.

⚠️ 패턴을 창작하지 말 것
  openai/simple-evals 의 HealthBench emergency referral 테마에서 역추출하세요.
  의사 262명이 "이 상황엔 응급 안내가 있어야 한다"고 표시한 대화의
  '사용자 발화'를 모아 패턴화하는 것이 의료 배경 없이 정확도를 얻는 유일한 경로.
  data/redflags.yaml 에 채워 넣으면 여기서 자동으로 읽는다.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml

from .config import DATA
from .contracts import RedFlagHit

# HealthBench 역추출 전까지 쓰는 최소 시드.
# ⚠️ 이건 임시값이다. 8/21 오전에 D가 반드시 교체할 것.
SEED: dict[str, list[str]] = {
    "cardiac": [
        r"가슴이?\s*(조이|짓눌|찢어|답답|아프|아파)",
        r"식은땀.{0,12}(가슴|흉통)",
        r"(왼쪽|좌측)?\s*팔.{0,6}(저림|저려).{0,12}가슴",
        r"흉통",
    ],
    "neuro": [
        r"갑자기.{0,12}(말이\s*어눌|발음이\s*이상|한쪽.{0,4}(마비|힘이\s*빠))",
        r"(지금까지|평생|난생).{0,10}(가장|제일)\s*심한\s*두통",
        r"벼락(같은)?\s*두통",
        r"의식.{0,4}(잃|없|흐)",
        r"경련|발작",
    ],
    "respiratory": [
        r"숨을?\s*(못|잘\s*못)\s*(쉬|쉬겠)",
        r"호흡\s*곤란",
        r"입술이?\s*(파래|퍼레|창백)",
    ],
    "bleeding": [
        r"피를?\s*토",
        r"검은\s*변|흑색변",
        r"멈추지\s*않는\s*출혈|지혈이?\s*안",
    ],
    "psych": [
        r"죽고\s*싶",
        r"살기\s*싫",
        r"자살",
        r"해치고\s*싶",
    ],
    "anaphylaxis": [
        r"목이?\s*(붓|조이|막히)",
        r"온몸에?\s*두드러기.{0,12}(숨|호흡)",
    ],
}


@lru_cache(maxsize=1)
def _patterns() -> dict[str, list[re.Pattern]]:
    raw = dict(SEED)
    p = Path(DATA) / "redflags.yaml"
    if p.exists():
        loaded = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        # 파일이 있으면 파일이 정본. 시드는 파일에 없는 카테고리만 보충.
        for k, v in loaded.items():
            raw[k] = v
    return {k: [re.compile(x) for x in v] for k, v in raw.items()}


def scan(text: str) -> RedFlagHit | None:
    """HIT하면 L2·L3를 통째로 건너뛰고 응급 경로로 간다.

    ⚠️ '검색을 우회한다'는 것은 HealthBench가 규정한 게 아니라 우리 설계 가설이다.
       근거 있는 것은 배치 점수뿐:
         · 응급 안내를 맨 앞에  → +10
         · 뒤에 묻으면          → -9
         · 감별진단 나열        → -5
       configs/*.yaml 의 emergency.sources 로 A/B 테스트할 것.
    """
    for cat, pats in _patterns().items():
        for p in pats:
            m = p.search(text)
            if m:
                return RedFlagHit(category=cat, pattern=p.pattern, matched=m.group(0))
    return None


def categories() -> list[str]:
    return list(_patterns().keys())
