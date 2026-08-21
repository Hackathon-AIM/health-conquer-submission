# Prompt Directory

Prompt는 Python 코드에 하드코딩하지 않고 이 디렉터리에서 관리합니다.

## 기본 prompt

- `system.md`
- `query_analyzer.md`
- `response_requirements.md`
- `retrieval_query_planner.md`
- `answer_generator.md`
- `claim_extractor.md`
- `groundedness_verifier.md`
- `safety_verifier.md`
- `targeted_repair.md`

## 버전 정책

실험 단위 prompt는 `versions/` 아래에 보관합니다.

```text
versions/
├── v0_baseline/
├── v1_rag_grounded/
├── v2_safety_first/
└── v3_healthbench_tuned/
```

실행 trace에는 사용한 prompt version을 반드시 남깁니다.
