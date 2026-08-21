"""설정 로딩.

프롬프트와 설정을 코드 밖으로 뺀 이유:
  python -m eval.harness --config configs/v2_critic.yaml
한 줄로 설정별 점수를 뽑고 git으로 어느 버전이 몇 점이었는지 추적하기 위함.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
PROMPTS = Path(__file__).resolve().parent / "prompts"
DATA = ROOT / "data"
LOGS = ROOT / "logs"


DEFAULTS: dict[str, Any] = {
    "name": "baseline",

    # ── 모델 배치 ────────────────────────────────────────────
    # ⚠️ 보조 작업(classifier/rewriter/critic/extractor)에 FM이 아닌 모델을
    #    써도 되는지는 대회 규정 확인 필요. 기본값은 전 구간 FM(리스크 0).
    #    drafter는 절대 바꾸지 말 것 — 심사 대상 텍스트다.
    "models": {
        "classifier": "medical-fm",
        "rewriter": "medical-fm",
        "drafter": "medical-fm",     # ★ 고정
        "critic": "medical-fm",
        "extractor": "medical-fm",
        # 로컬 HealthBench 채점자 (eval/healthbench.py).
        # CoEval 의 healthbench_judge 기본값은 gpt-4.1 이다.
        # 같은 모델로 맞추면 로컬 점수와 CoEval 점수의 상관계수가 올라간다.
        # ⚠️ 채점자는 우리 챗봇이 아니므로 규정과 무관하다 (심사 대상은 drafter 뿐).
        "grader": "medical-fm",
    },

    # ── LLM 클라이언트 ───────────────────────────────────────
    "llm": {
        # TODO(현장): 오프닝에서 받은 값으로 교체
        "base_url": "",              # 예: https://.../v1
        "api_key_env": "MEDAI_API_KEY",
        "timeout": 30.0,             # ⚠️ OpenAI SDK 기본은 600초(10분) — 반드시 지정
        "max_retries": 1,
        "temperature": 0.2,
        "max_tokens": 2048,   # think 블록 때문에 넉넉히 (모델 카드 권장)
        # 분류는 짧은 JSON 하나면 된다. 2048 을 주면 모델이 장황해지고 지연이 붙는다.
        # (실측: L2 분류가 33.7초 — 파이프라인 전체 82초의 최대 병목이었다)
        "classifier_max_tokens": 600,
        "extractor_max_tokens": 400,   # 약 이름 목록 JSON 하나 (실측: L4b 11.8초 병목)
        "context_window": 32768,     # L1-16B-A3B 는 32K. 30B 도 같을 가능성이 높음
        # L1 계열은 <think>...</think> 로 추론한다. 반드시 벗겨야 한다.
        "strip_think": True,
    },

    # ── L2 2단계 하네스 (대회 FM 권장 사용법 — src/medai/l2.py) ──
    "l2": {
        "mcp_url": "https://mcp.hackathon.lunit.io/mcp",
        "max_tool_calls": 8,             # 검색 단계 도구 호출 예산 (가이드: 제한하라)
        "max_retrievals_per_turn": 2,    # 생성 단계가 retrieve 를 부를 수 있는 횟수
        # ★ 실측(data/probe/page_stats.json): 가이드라인 1페이지 = 3,929자.
        #   1800 이면 페이지 하나도 절반이 잘리고, 잘리는 쪽이 뒷부분이라
        #   정작 필요한 권고 문장이 날아간다. 페이지 1장이 통째로 들어가게 잡는다.
        "evidence_chars_per_item": 4000,
        "max_evidence_items": 6,         # 6 × 4000 ≈ 24,000자 ≈ 7,200토큰
        # ★ 두 예산은 별개다 — 이걸 헷갈려서 input_limit_exceeded 를 냈다.
        #
        #   tool_result_chars      검색 '대화'에 남기는 도구 결과 (8회 누적된다!)
        #   evidence_chars_per_item 생성 단계로 넘기는 근거 (cite_store 원본에서 뽑는다)
        #
        #   대화 복사본은 작아도 된다 — 모델은 "관련 있나 / 다음에 뭘 열까"만 판단하면
        #   되고, 인용 본문은 cite_store 가 원본 전체를 들고 있다가 마지막에 뽑는다.
        #   12000 으로 뒀더니 8회 × 12000 = 96,000자 ≈ 38K토큰으로 컨텍스트가 터졌다.
        "tool_result_chars": 3000,
        "retrieval_char_budget": 24000,  # 대화에 누적되는 도구 결과 총량 상한
        "tool_timeout": 60.0,            # Codex 권장 tool_timeout_sec 과 동일
    },

    # ── 제출물 서버 (serve.py) ──────────────────────────────
    "server": {
        # 한 턴에 허용하는 벽시계 시간. 넘으면 파이프라인을 취소하고 폴백 문구를 낸다.
        #
        # ★ 이 값이 짧으면 '느린 답'이 '날아간 답'이 된다.
        #   폴백 문구는 그 문항의 가점을 전부 놓치고 감점을 밟는다. 그리고 채점 공식상
        #   감점은 분모에 없어서(docs/spec.md §5.2) 회복이 불가능하다.
        #   → 느린 답 하나가 폴백 하나보다 항상 낫다. l2_live.yaml 의 max_retries=4
        #     주석과 같은 판단이다.
        #
        # 165 인 이유: 대회 채점 split(conquer_val/conquer_test)이 클라이언트 timeout 을
        #   **180초**로 덮어쓴다(docs/spec.md §5.2 — CoEval 실물 config 로 확인).
        #   passthrough 기본값 360 이 아니다.
        #
        #   그래서 상한은 180 바로 아래여야 한다:
        #     · 너무 짧으면 — 150-180초 사이에 끝났을 진짜 답이 폴백으로 바뀐다 (순손실)
        #     · 180 이상이면 — 클라이언트가 먼저 끊어 폴백조차 전달되지 않는다.
        #       그 문항은 runner.score_inference_failures_as_zero=true 로 **0점**이 된다.
        #   165 = 응답 왕복 여유 15초를 남기고 진짜 답 구간을 최대로 가져간다.
        #
        # 실측: 1턴 24.0초(2026-08-21, l2_live + 실제 L2/MCP).
        #       예전 실측 81.9초(docs/l2_playbook.md §6-d)는 분류 최적화 이전 값이다.
        "turn_timeout": 165.0,
    },

    # ── 계층 스위치 (A/B 실험용) ─────────────────────────────
    "layers": {
        # ★ l2_native: L4 초안을 L2 2단계(검색→생성)로 생성한다.
        #   대회 규칙: 최종 출력물은 반드시 L2 로 생성. 이 스위치가 켜지면
        #   L3(우리 검색)·rerank 는 건너뛰고 검색 주체가 L2 모델이 된다.
        "l2_native": False,
        # ★ passthrough: 파이프라인 전체를 끄고 모델에 그대로 물어본다.
        #   L1-16B-A3B 는 HealthBench-Consensus 93.5% 를 이미 낸다.
        #   우리 파이프라인이 그 숫자를 넘는지 반드시 재야 한다.
        #   넘지 못하면 파이프라인이 노이즈를 넣고 있다는 뜻이다.
        "passthrough": False,
        "redflag": True,
        "entity_llm_merge": True,    # L1규칙 ∪ L2LLM 합집합
        "dur_input": True,           # L1b
        "dur_output": True,          # L4b
        "risk_check": True,          # L4b
        "critic": True,              # L4c
    },

    # ── L2 라우팅 ────────────────────────────────────────────
    "routing": {
        "max_sources": 3,
        "allow_pubmed": True,
        "fallback_source": "guideline",
    },

    # ── L3 검색 ─────────────────────────────────────────────
    "retrieval": {
        "mode": "mock",              # mock | live
        "top_k_per_source": 20,      # 넓게 뽑고
        "rerank_top_k": 5,           # 정밀하게 거르고
        # none = 검색 점수 기준 정렬 (기본. 빠르고 의존성 없음)
        # bge  = BGE 크로스인코더. 정확하지만 모델 로딩에 수십 초 + 메모리 수 GB
        #        후보가 많을 때만 값어치를 한다. live 모드에서만 켤 것.
        "reranker": "none",
        "reranker_model": "BAAI/bge-reranker-v2-m3",
        "context_token_budget": 2400,  # 8K 모델이면 3000→2400 권장
        "timeout": 8.0,
    },

    # ── 응급 처리 (A/B 대상: HealthBench가 규정한 게 아니라 우리 가설) ──
    "emergency": {
        "sources": [],               # [] = 검색 우회 / ["guideline"] = top-1만
        "top_k": 1,
    },

    # ── L4 ─────────────────────────────────────────────────
    "generation": {
        "max_rewrite": 2,
        "total_budget_sec": 20.0,    # 초과 시 비평 건너뛰고 초안 출력
    },
    "critic": {
        "strict_items": ["safety_first", "dur_warning_present", "no_hedging_overload"],
        "min_confidence": 0.7,       # 스타일 항목은 확신 낮으면 무시
    },

    # ── 평가 ────────────────────────────────────────────────
    "eval": {
        "max_turns": 3,
        "concurrency": 8,
        "holdout_ratio": 0.2,
        "seed": 20260821,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config(dict):
    """dict + 점 표기 접근."""

    def __getattr__(self, item):
        try:
            v = self[item]
        except KeyError as e:
            raise AttributeError(item) from e
        return Config(v) if isinstance(v, dict) else v

    def get_path(self, dotted: str, default=None):
        cur: Any = self
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur


def _load_dotenv() -> None:
    """.env 를 환경변수로 올린다 (python-dotenv 의존성 없이).

    API 키를 코드나 yaml 에 쓰지 않기 위한 장치.
    이미 설정된 환경변수는 덮어쓰지 않는다.
    """
    f = ROOT / ".env"
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v and k not in os.environ:
            os.environ[k] = v


def load(path: str | Path | None = None) -> Config:
    _load_dotenv()
    cfg = dict(DEFAULTS)
    if path:
        with open(path, encoding="utf-8") as f:
            cfg = _deep_merge(cfg, yaml.safe_load(f) or {})
    # 환경변수 오버라이드 (현장에서 빠르게 바꾸기 위함)
    if os.getenv("MEDAI_BASE_URL"):
        cfg["llm"]["base_url"] = os.environ["MEDAI_BASE_URL"]
    if os.getenv("MEDAI_RETRIEVAL_MODE"):
        cfg["retrieval"]["mode"] = os.environ["MEDAI_RETRIEVAL_MODE"]
    return Config(cfg)


def prompt(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")
