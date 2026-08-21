# Source Layout

`src/`는 설계 문서의 P0 파이프라인을 그대로 코드 모듈로 분해한 영역입니다.

```text
api/             # API entrypoint, route, request/response schema
orchestrator/    # 전체 workflow와 deterministic state graph
clinical/        # clinical state, triage, query analysis, conversation policy
policy/          # response requirement와 retrieval policy
retrieval/       # source-specific query planning, routing, reranking, filtering
sources/         # PubMed, drug, guideline, HIRA, law source adapter
evidence_graph/  # P1 query-time clinical evidence graph
reasoning/       # answer planning과 medical FM generation
verification/    # claim extraction, groundedness, safety, repair
response/        # final response composition과 depth control
observability/   # trace, latency, token, metric logging
config/          # settings와 feature flags
evaluation/      # CoEval 연결, ablation, run comparison
```

P0 구현은 `api -> orchestrator -> clinical -> retrieval -> reasoning -> verification -> response -> observability` 순서로 진행합니다.
