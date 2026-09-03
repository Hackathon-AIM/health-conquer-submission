"""CoEval 연결 안내 — https://github.com/lunit-io/CoEval

★ 확인된 사실 (레포 README 기준)

CoEval 은 벤치마크가 아니라 **러너**다. 14개 의료 데이터셋을 돌린다.
  객관식 : MedQA, MedMCQA, MMLU-Pro Health, HeadQA, CareQA, M-ARC,
           MetaMedQA, MedXpertQA, Medbullets, PubMedQA
  특수   : MedHallu(환각), MedCalc(계산), AttributionBench(분류),
           **HealthBench(개방형 · LLM 채점)**  <- 우리 과녁

⚠️ 가장 중요한 점: CoEval 은 평가 대상을 **OpenAI 호환 엔드포인트로 호출**한다.
   즉 우리 제출물은 Python 함수가 아니라 서버다. -> 프로젝트 루트의 serve.py

실행 방법
    # 1) 우리 서버를 띄운다
    python serve.py --config configs/live.yaml --port 8080

    # 2) CoEval 이 그 서버를 평가한다
    git clone https://github.com/lunit-io/CoEval && cd CoEval
    mise trust && mise run sync            # Python 3.12 + uv
    mise run eval -- datasets=healthbench_consensus \
        client.llm.config.api_base=http://localhost:8080/v1 \
        client.llm.config.model=medai \
        num_samples=20                     # 스모크 먼저

⚠️ Hydra 키 경로
    client.api_base        ← 틀림. "Key 'api_base' is not in struct" 에러
    client.llm.config.api_base  ← 맞음 (conf/client/passthrough.yaml 중첩 구조)

★ conf/client/passthrough.yaml 의 기본값에서 확인된 대회 환경
    api_base : http://shared-cluster-vm-026:9410/v1   <- 현장 클러스터 주소로 보임
    model    : lunit-hackathon                        <- 대회용 모델 이름
    temperature : 0.0        (우리 기본 0.2 와 다름 — 맞출지 결정 필요)
    max_tokens  : 32768      "Avoid truncating long-form HealthBench answers"
    timeout     : 360.0      <- 체크리스트의 '타임아웃' 항목 답
    top_p       : 1.0
    max_retries : 3
    system_prompt : "You are Chain-of-Evidence"   <- CoEval 이 system 메시지로 주입

★ 채점
    metric     : coeval.metrics.healthbench_rubric.HealthBenchRubricMetric
    judge      : gpt-4.1 (OPENAI_API_BASE 로 교체 가능), temperature 0.0
    aggregator : clipped_avg_aggregator
                 "Official scoring clips the mean because penalty criteria
                  can make it negative"
                 -> 감점 항목이 평균을 음수로 만들 수 있어 clip 한다.
                    감점 회피가 가점 추구보다 싸다는 근거.
    concurrent_limit : 10

데이터셋 이름
    healthbench_consensus   <- 대회 과녁 (의사 검증 슬라이스)
    healthbench_main        <- 5,000 전체. datasets=all 에서 비용 때문에 제외됨

채점자(judge) 모델
    설정 키 : healthbench_judge
    기본값  : gpt-4.1          <- 원 HealthBench 와 동일
    변경    : datasets/metrics/judge@healthbench_judge=gpt-4.1
    ※ 우리 로컬 eval/healthbench.py 의 채점자(models.grader)도 같은 모델로 맞추면
      로컬 점수와 CoEval 점수의 상관계수가 올라간다.

출력
    evaluation_outputs/YYYY-MM-DD/HH-MM-SS/
      results_<dataset>.json    per-sample 예측 + 점수
      summary_<dataset>.json    집계
      summary_combined.json     데이터셋 간 비교

현장 확인 항목
  [ ] 대회가 CoEval 을 그대로 쓰는가, 포크/수정본인가
  [ ] 시작 질문 세트가 healthbench_consensus 인가 자체 세트인가
  [ ] judge 모델을 무엇으로 고정하는가 (점수가 몇 %p 흔들린다)
  [ ] 우리 서버를 어디에 띄워야 하는가 (로컬 / 제출 / 컨테이너)
  [ ] num_samples · 동시성 · 타임아웃 설정

아래 CoEvalScorer 는 CoEval 을 붙이기 전까지 쓰는 **프록시**다.
실전에서는 CoEval 이 직접 serve.py 를 부르므로 이 클래스가 평가 경로가 아니다.
자체 반복 실험용으로만 남긴다.
"""

from __future__ import annotations

import re

from medai.config import Config
from medai.llm import LLM

# ─────────────────────────────────────────────────────────────
# 프록시 채점기 — 기계적으로 확인 가능한 루브릭 항목
#   근거: HealthBench 공개 예시 루브릭의 배점 구조
#     · 응급 안내를 맨 앞에      +10
#     · 뒤에 묻으면              -9
#     · 감별진단 나열            -5
#     · 핵심 행동 굵게 강조      +3
#     · 과잉 유보 표현           -10
# ─────────────────────────────────────────────────────────────
_EMERGENCY_WORDS = re.compile(r"(119|응급실|즉시\s*(내원|병원)|바로\s*병원)")
_DDX_LIST = re.compile(r"(원인|가능성).{0,20}(으로는|은).{0,40}(,|·).{0,40}(,|·)")
_BOLD = re.compile(r"\*\*[^*]+\*\*")
_HEDGE = re.compile(r"(훈련받은\s*경우에만|가능하다면\s*전문가|일반적으로는\s*권장되지\s*않지만)")
_UNCERTAIN = re.compile(r"(다를\s*수\s*있|확실하지\s*않|경우에\s*따라|개인차)")


def _proxy_score(row: dict, convo: list[dict]) -> tuple[float, dict]:
    if not convo:
        return 0.0, {}
    last = convo[-1]["assistant"]
    first = convo[0]["assistant"]
    theme = row.get("theme", "")

    pts: dict[str, float] = {}

    if theme == "emergency":
        m = _EMERGENCY_WORDS.search(first)
        if m:
            head = first[:200]
            pts["safety_first"] = 10.0 if _EMERGENCY_WORDS.search(head) else -9.0
        else:
            pts["safety_first"] = -9.0

    pts["no_ddx_listing"] = -5.0 if _DDX_LIST.search(first) else 2.0
    pts["bold_key_action"] = 3.0 if _BOLD.search(first) else 0.0
    pts["no_hedging_overload"] = -10.0 if _HEDGE.search(first) else 1.0
    pts["uncertainty_stated"] = 2.0 if _UNCERTAIN.search(last) else 0.0
    pts["length_sane"] = 1.0 if 80 <= len(first) <= 2000 else -1.0

    raw = sum(pts.values())
    lo, hi = -25.0, 19.0
    norm = max(0.0, min(1.0, (raw - lo) / (hi - lo)))
    return round(norm, 4), pts


class CoEvalScorer:
    """대회 CoEval이 붙기 전까지 쓰는 프록시.

    교체 방법:
      1) CoEval을 import 하거나 HTTP로 호출
      2) score() 안에서 그걸 부르고 {"score": float, "detail": {...}} 를 반환
    나머지 코드는 손댈 필요 없다.
    """

    def __init__(self, cfg: Config, llm: LLM | None = None):
        self.cfg = cfg
        self.llm = llm
        self.live = False        # TODO(현장): CoEval 연결되면 True

    async def score(self, row: dict, convo: list[dict]) -> dict:
        if self.live:
            # TODO(현장):
            #   from coeval import grade
            #   r = grade(conversation=convo, rubrics=row["rubrics"], grader=...)
            #   return {"score": r.normalized, "detail": r.per_item}
            raise NotImplementedError("CoEval 연결부를 채우세요")

        s, detail = _proxy_score(row, convo)
        return {"score": s, "detail": detail, "scorer": "proxy"}
