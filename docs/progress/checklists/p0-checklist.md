# P0 Checklist

이 체크리스트는 `medical_ai_chatbot_implementation_plan.md`의 P0 항목을 개발 진행용으로 옮긴 것입니다.

## API / Harness

- [x] API entrypoint와 request/response schema 정의
- [x] Multi-turn harness adapter 구현
- [x] OpenAI-compatible endpoint wrapper 검토

## Prompt

- [x] `prompts/system.md` 기본 정책 prompt 작성
- [ ] `prompts/query_analyzer.md` structured output prompt 작성
- [x] `prompts/retrieval_query_planner.md` source-specific query prompt 작성
- [x] `prompts/answer_generator.md` grounded answer prompt 작성
- [ ] `prompts/claim_extractor.md` claim extraction prompt 작성
- [ ] `prompts/groundedness_verifier.md` verifier prompt 작성
- [ ] `prompts/safety_verifier.md` verifier prompt 작성

## Clinical Pipeline

- [x] ClinicalState 및 patient fact 모델 구현
- [x] Safety Pre-Triage 구현
- [x] Emergency Fast Path 구현
- [x] Conditional Emergency Guardrail 구현
- [x] Clinical Query Analyzer structured output 구현
- [x] Retrieval 필요 여부 판단 정책 구현
- [x] Conversation turn policy 최소 구현

## Retrieval

- [x] MedicalSource 공통 protocol 구현
- [x] Source-specific Query Planner 구현
- [x] Dynamic Source Router 기본 구현
- [x] Source별 async retrieval adapter 구현
- [x] Source registry 기반 retrieval 구현
- [x] Lunit MCP source adapter stub 구현
- [x] Candidate pool 및 source-aware metadata 관리
- [x] Reranker 구현
- [x] Evidence Filter 구현
- [x] High-risk evidence coverage check 구현
- [x] Evidence Fusion 기본 구현

## Generation / Verification

- [x] Medical Foundation Model generator 구현
- [x] L2 final generator guard 구현
- [x] Claim Extractor 구현
- [x] Basic Groundedness Verifier 구현
- [x] Basic Clinical Safety Verifier 구현
- [x] Evidence 부족 시 safe fallback 구현
- [x] Response Composer 구현

## Observability / Evaluation

- [x] TraceRecord 및 latency/token logging 구현
- [x] Retrieval diagnostics 및 final model guard trace logging 구현
- [x] Prompt version trace logging 구현
- [x] CoEval 실행 스크립트 연결
- [x] Baseline run 저장 경로 확정
