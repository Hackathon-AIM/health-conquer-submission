# ADR 2026-08-19: Project Layout

## Status

Accepted

## Context

설계 문서는 P0에서 안전한 RAG pipeline과 평가 루프를 먼저 완성하고, graph reasoning은 P1 실험으로 미루도록 권장합니다. 기존 저장소에는 설계 문서와 CoEval 평가 프레임워크가 먼저 존재했습니다.

## Decision

MediBot 애플리케이션 코드는 `src/` 아래에 pipeline stage별로 분리합니다. Prompt는 `prompts/`에서 별도 관리하고, 개발 진행 상황은 `docs/progress/`에 저장합니다. 실행 중 생성되는 trace와 평가 결과는 문서 디렉터리가 아닌 `storage/`에 저장합니다.

## Consequences

- P0 구현 범위와 P1 graph 실험 범위가 디렉터리 수준에서 분리됩니다.
- CoEval 결과와 개발 로그를 문서로 연결하기 쉽습니다.
- Prompt 변경을 코드 변경과 분리해 ablation할 수 있습니다.
