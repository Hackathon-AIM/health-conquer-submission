# 2026-08-19 Initial Structure

## Summary

기존 설계 문서를 기준으로 MediBot 개발을 시작할 수 있는 폴더 구조와 진행 기록 체계를 생성했습니다.

## Added

- `src/`: P0/P1 애플리케이션 모듈 구조
- `prompts/`: stage-specific prompt 관리
- `docs/architecture/`: 구조 문서
- `docs/progress/`: 개발 진행 기록
- `storage/`: trace, evaluation run, cache, artifact 저장
- `tests/`: unit/integration 테스트 구조

## Rationale

대회 일정상 먼저 필요한 것은 graph 실험이 아니라 end-to-end P0 pipeline, CoEval 실행, trace 기반 반복 개선입니다. 따라서 설계 문서의 권장 구조를 그대로 반영하되, graph 관련 디렉터리는 P1 실험 영역으로 분리했습니다.

## Next

- P0 feature flags 및 settings 작성
- ClinicalState 모델 작성
- Safety Pre-Triage 최소 구현
- API/harness adapter 작성
- TraceRecord 저장 포맷 확정
