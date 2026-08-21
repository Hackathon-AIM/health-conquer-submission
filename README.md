# MediBot

의료 Foundation Model과 multi-source Medical RAG를 결합한 해커톤용 의료 상담 챗봇 프로젝트입니다.

현재 저장소는 기존 설계 문서(`medical_ai_chatbot_implementation_plan.md`)를 기준으로 P0 end-to-end 파이프라인을 구현하기 위한 폴더 구조와 개발 진행 기록 체계를 먼저 구성했습니다.

## 주요 문서

- `medical_ai_chatbot_implementation_plan.md`: 전체 아키텍처와 구현 계획
- `conquer_health_hackathon_info.md`: 대회 정보와 구현상 요구사항
- `docs/hackathon_rules.md`: 해커톤 규칙과 제출/evaluation 준수 체크리스트
- `benchmark_evaluation_notes.md`: CoEval / HealthBench 평가 전략
- `docs/architecture/project_structure.md`: 현재 저장소 구조와 모듈 책임
- `docs/architecture/lunit_fm_l2_usage.md`: Lunit FM L2의 retrieval/generation 2단계 사용 가이드
- `docs/architecture/lunit_api_integration.md`: Lunit Model API와 Patient Simulator 연결 방법
- `docs/architecture/lunit_mcp_tools.md`: Lunit MCP server 연결 방법과 tool catalog
- `docs/architecture/lunit_rag_submission_readiness.md`: L2/MCP RAG 제출 준비 사항
- `docs/progress/STATUS.md`: 현재 개발 진행 상태
- `docs/progress/checklists/p0-checklist.md`: P0 구현 체크리스트

## 최상위 구조

```text
src/                  # MediBot 애플리케이션 코드
prompts/              # 단계별 LLM prompt와 버전 관리
docs/                 # 아키텍처/진행 상황/결정 기록
storage/              # trace, 평가 결과, cache, 산출물 저장
tests/                # 단위/통합 테스트
CoEval/               # Lunit CoEval 평가 프레임워크
```

## 개발 원칙

- P0에서는 graph보다 end-to-end RAG pipeline과 평가 루프를 우선합니다.
- 응급 상황은 retrieval 전에 fast path로 처리합니다.
- 약물, 용량, 금기, 상호작용, 법률/보험, 최신 근거 질의는 RAG 근거를 우선합니다.
- 모든 request는 trace로 남겨 CoEval 결과와 연결합니다.
- prompt와 feature flag는 실험 단위로 버전 관리합니다.

## 대회 제출 준비

- 최종 제출 모드에서는 `MEDIBOT_FINAL_MODEL_PROVIDER=lunit_l2`,
  `MEDIBOT_REQUIRE_L2_FINAL=1`, `MEDIBOT_ALLOW_FALLBACK=0`을 사용합니다.
- RAG backend는 `MEDIBOT_RAG_BACKEND=mini|lunit_mcp|hybrid`로 선택합니다.
- 현장 MCP 명세를 받으면 `src/medibot/sources/lunit_mcp.py`의 source별
  `search()` 구현만 채우는 구조입니다.
