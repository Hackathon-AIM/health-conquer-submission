# Current Development Status

**Date:** 2026-08-21  
**Phase:** Lunit L2 MCP harness live wiring  
**Target:** Stabilize cite_uid-based retrieval phase and run live CoEval smoke

## Current State

- [x] 설계 문서 정리 완료
- [x] 대회 정보 문서 정리 완료
- [x] 벤치마크/평가 전략 문서 정리 완료
- [x] CoEval repository 추가
- [x] MediBot 애플리케이션 폴더 구조 생성
- [x] Prompt 디렉터리 생성
- [x] 개발 진행 기록 디렉터리 생성
- [x] P0 코드 구현 시작
- [x] 루트 `src/medibot` 패키지 생성
- [x] FastAPI `/chat` endpoint 구현
- [x] CLI smoke runner 구현
- [x] Deterministic fallback 모델/RAG 구현
- [x] GPT API remote generation 연결 확인
- [x] Trace JSONL 저장 구현
- [x] CoEval-compatible client wrapper 구현
- [x] CoEval `client=medibot` config 추가
- [x] CoEval HealthBench smoke test 실행
- [x] CoEval 5-sample baseline 실행
- [x] Verifier fallback 정책 완화
- [x] HealthBench용 answer generation prompt 작성
- [x] 영어 medical intent rule 보강
- [x] CoEval `healthbench_consensus` 10-sample 재실행
- [x] CoEval 10-sample 결과 및 요인 분석 문서화
- [x] Source registry 기반 retrieval 준비
- [x] L2 final generator guard 추가
- [x] Lunit MCP source adapter stub 추가
- [x] 대회용 system / retrieval planner prompt 작성
- [x] Lunit RAG 제출 준비 작업 문서화
- [x] Lunit Model API key 및 endpoint 연결 확인
- [x] Lunit MCP `tools/list` live 연결 확인
- [x] Lunit MCP protocol version `2025-11-25`로 조정
- [x] L2 generation/retrieval 2-phase harness 구현
- [x] MCP Streamable HTTP client 구현
- [x] L2/MCP fake integration tests 추가
- [x] live MediBot pipeline smoke 구동 확인
- [ ] `cite_uid` 기반 retrieval finalization 안정화

## Latest Notes

- `docs/progress/daily/2026-08-21-lunit-l2-mcp-harness.md`: L2 MCP harness 구현, live smoke 결과, 남은 한계 정리
- `docs/progress/daily/2026-08-20-lunit-rag-readiness.md`: L2 guard, source registry, MCP stub, prompt 준비, 테스트 결과 정리
- `docs/architecture/lunit_rag_submission_readiness.md`: 현장 MCP/L2 연결 runbook

## Next Work

1. Retrieval prompt와 tool subset을 조정해 L2가 `finalize_retrieval`을 안정적으로 호출하도록 개선
2. guideline/HIRA path에서 `index_get_page_content`까지 유도해 `cite_uid` evidence 확보
3. MCP raw result에서 nested `cite_uid` 추출 coverage 확대
4. L2 MCP harness live smoke를 medication, HIRA/law, PubMed, FAERS, KCD, pricing query로 확장
5. CoEval `healthbench_consensus` 소량 live run으로 `mini` baseline과 L2 MCP harness 비교
