# Development Progress

이 디렉터리는 MediBot 개발 진행 상황을 저장하고 문서화하기 위한 공간입니다.

## 구조

```text
docs/progress/
├── STATUS.md
├── daily/
├── decisions/
├── experiments/
└── checklists/
```

## 작성 규칙

- 하루 작업 시작/종료 시 `daily/YYYY-MM-DD-*.md`에 기록합니다.
- 구조나 기술 선택이 바뀌면 `decisions/ADR-YYYY-MM-DD-*.md`에 남깁니다.
- CoEval, prompt, feature flag 실험은 `experiments/`에 저장합니다.
- 구현 완료 여부는 `checklists/`에서 체크합니다.
- 실행 결과 파일은 문서에 붙여넣지 않고 `storage/evaluation_runs/` 또는 `storage/traces/` 경로만 연결합니다.
