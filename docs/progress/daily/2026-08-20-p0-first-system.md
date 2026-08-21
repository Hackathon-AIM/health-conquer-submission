# 2026-08-20 P0 First System

## Summary

P0 로컬 스모크 가능한 MediBot end-to-end 파이프라인을 구현했습니다.

## Added

- 루트 `pyproject.toml`
- `src/medibot` 독립 패키지
- FastAPI `POST /chat`
- CLI smoke runner: `python -m medibot.evaluation.smoke --message "..."`
- rule-first clinical state, triage, query analysis
- deterministic MiniEvidenceSource 기반 fallback RAG
- response generation fallback, claim extraction, groundedness/safety verifier
- JSONL trace writer
- CoEval-compatible `MediBotCoEvalClient`
- unit/integration tests
- GPT API remote generation 연결
- CoEval `client=medibot` Hydra config
- CoEval / DeepEval 호환 패치
- Windows 결과 저장 UTF-8 인코딩 패치

## Evaluation

- `healthbench_consensus`, `num_samples=1`: HealthBench Rubric `0.500`
- `healthbench_consensus`, `num_samples=5`: HealthBench Rubric `0.433`
- 5-sample run: `4/5` passed, inference failed `0`, scoring failed `0`, total time `44.44s`
- Output: `evaluation_outputs/2026-08-20/12-05-45/summary_combined.json`

## Remaining

- 실제 RAG source endpoint adapter 추가
- prompt placeholder를 운영 prompt로 작성
- verifier fallback 정책 완화
- context seeking 및 language matching 개선
