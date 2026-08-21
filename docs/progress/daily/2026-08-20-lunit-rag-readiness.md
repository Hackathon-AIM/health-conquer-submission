# 2026-08-20 Lunit RAG Readiness

## Summary

Conquer Health 해커톤 현장에서 Lunit L2 endpoint와 MCP tool 명세를 받은 뒤 바로
연결할 수 있도록 retrieval, final generation guard, trace, prompt, 문서 준비를 완료했다.

## Added

- `SourceRegistry` 기반 retrieval 경로
- `MiniEvidenceSource` direct call 제거
- `mini`, `lunit_mcp`, `hybrid` RAG backend 설정
- Lunit MCP source adapter stub
- L2 final answer generation guard
- retrieval diagnostics trace field
- final model provider/name 및 L2 guard trace field
- 대회용 `system.md` prompt
- source-specific `retrieval_query_planner.md` prompt
- HealthBench 지향이지만 reverse engineering 없는 `answer_generator.md` 보강
- Lunit RAG 제출 준비 문서: `docs/architecture/lunit_rag_submission_readiness.md`

## Source Registry

기존 workflow는 `MiniEvidenceSource`를 직접 호출했다. 이제 workflow는
`SourceRegistry.search()`를 통해 logical source를 concrete adapter로 라우팅한다.

지원 backend:

- `mini`: 로컬 deterministic smoke test
- `lunit_mcp`: Lunit 제공 MCP/tool source만 사용
- `hybrid`: MCP source 우선, 로컬 개발에서는 mini fallback 허용

현장 전환 설정:

```text
MEDIBOT_RAG_BACKEND=lunit_mcp
MEDIBOT_ENABLED_MCP_SOURCES=drug_label,drug_pricing,disease_code,law,guideline,pubmed,faers,hira_coverage
```

## MCP Adapter Stubs

다음 source stub을 준비했다.

- `drug_label`: 의약품 라벨, 허가, 용량, 금기, 상호작용, 안전성
- `drug_pricing`: 의약품 급여, 약가
- `disease_code`: 질병 분류 코드
- `law`: 건강보험 관련 법령 조문
- `guideline`: 진료 가이드라인
- `pubmed`: 최신 의과학 논문
- `faers`: 의약품 부작용/이상사례
- `hira_coverage`: 심평원 급여 기준 및 질의응답

현재 stub은 명세 수령 전이라 `stub_unconfigured` diagnostic을 남기고 빈 evidence를 반환한다.
내일 할 일은 각 source의 `search()`에서 실제 MCP tool을 호출해 `RetrievalEvidence`로 normalize하는 것이다.

## L2 Guard

최종 제출 모드에서 L2 외 모델이나 fallback이 final answer를 생성하지 못하도록 guard를 추가했다.

제출 리허설 설정:

```text
MEDIBOT_FINAL_MODEL_PROVIDER=lunit_l2
MEDIBOT_REQUIRE_L2_FINAL=1
MEDIBOT_ALLOW_FALLBACK=0
```

`MEDIBOT_REQUIRE_L2_FINAL=1`이면 provider mismatch, missing endpoint, remote generation failure가
deterministic fallback으로 전환되지 않고 실패한다.

## Trace

Trace에 다음 항목을 추가했다.

- `retrieval_diagnostics`: source, adapter, query, top_k, status, result_count, latency
- `final_model_provider`
- `final_model_name`
- `l2_required`

이 값은 validation dashboard 결과와 source별 ablation을 연결하는 데 사용한다.

## Prompt Policy

프롬프트는 HealthBench hidden set을 추정하지 않고 일반 의료 품질 원칙에 맞춘다.

- 결론 또는 응급 행동 지침 먼저
- 고위험 claim은 retrieved evidence 우선
- 근거 부족 시 불확실성과 안전한 next step 제시
- 추가 질문은 답변이 실제로 바뀔 때만 사용
- 마지막 턴은 best-effort answer 우선
- benchmark reverse engineering 금지

## Evaluation

```text
pytest
20 passed
```

추가 테스트:

- mini backend가 source registry를 통해 evidence를 반환하는지 확인
- `lunit_mcp` backend에서 미구현 MCP stub이 `stub_unconfigured`를 trace하는지 확인
- L2 필수 모드에서 non-L2 provider가 차단되는지 확인

## Remaining

1. 현장 MCP tool schema 수령 후 `LunitMCPSource.search()` 구현
2. L2 endpoint/base URL/model/API key 설정
3. `MEDIBOT_RAG_BACKEND=lunit_mcp`로 전환 후 smoke test
4. `MEDIBOT_REQUIRE_L2_FINAL=1`, `MEDIBOT_ALLOW_FALLBACK=0`으로 submission rehearsal
5. validation dashboard 결과와 `retrieval_diagnostics`를 연결해 source routing ablation
