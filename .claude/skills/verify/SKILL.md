---
name: verify
description: 변경이 실제로 좋아졌는지 재고 안전 회귀를 막는 절차. A/B 측정 조건, 안전 골든 시나리오 5종, 되돌림 판정 기준. 코드 변경 직후와 커밋 전에 사용.
---

# 검증 절차

## 순서를 지킨다 — 싼 것부터

```bash
make test       # 초 단위. 여기서 걸리면 나머지는 볼 것도 없다
make audit      # 초 단위. 배선이 다이어그램과 어긋났는지 37개 체크
# 골든 시나리오 5종 (아래)
make ab         # 분 단위. 설정 쌍 비교
make ab-baseline # 가장 비싸고 가장 중요
```

## 안전 골든 시나리오

`references/golden_scenarios.md` 에 입력·기대 동작·확인 방법이 있다.
**하나라도 깨지면 점수와 무관하게 되돌린다.**

이유: 채점에서 감점 항목은 분모에 없다(`clipped_avg_aggregator`).
가점을 더 얻어 감점을 상쇄할 수 없다. 안전 회귀는 순수 손실이다.

## A/B 를 신뢰할 수 있게 재는 조건

전부 같아야 비교다:

- 같은 variant (`consensus`)
- 같은 `--limit`
- 같은 채점자 모델 — CoEval 기본 judge 는 **gpt-4.1**. `models.grader` 를 맞춘다.
  다르면 로컬 점수와 CoEval 점수가 따로 논다.
- 같은 temperature — CoEval `passthrough` 기본 0.0, 우리 기본 0.2. 하나로 고정한다.

표본 수와 신뢰:

| 표본 | 말할 수 있는 것 |
|---|---|
| 5 | 파이프가 뚫렸다는 것뿐. 점수는 의미 없다 |
| 20 | 크게 망가졌는지 정도 |
| 50 | 5%p 이상 차이면 방향은 믿을 만하다 |
| 100 | "올랐다"고 말해도 되는 최소선 |

50문항에서 ±2%p 는 노이즈다. 그걸 근거로 채택하지 않는다.

## 기준선 비교가 최종 관문

```bash
make ab-baseline
```

`configs/l1_raw.yaml`(파이프라인 끔) vs `configs/l1.yaml`(우리 것), 같은 조건.

L1-16B-A3B 단독이 HealthBench-Consensus **93.5%** 를 낸다.
우리 파이프라인이 그 아래면 finding 을 고칠 게 아니라 **레이어를 꺼야 한다**.
`layers.*` 를 하나씩 False 로 바꿔가며 어느 레이어가 마이너스인지 찾는다.

의심 순서: `critic` → `retrieval`(RAG 노이즈) → `dur_output`(과잉 경고) → 템플릿.

## 서버 경로도 확인한다

제출물은 함수가 아니라 서버다. 파이프라인만 통과해도 서버가 깨져 있으면 0점이다.

```bash
make serve-mock &          # 또는 make serve
make serve-check           # /v1/models, /v1/chat/completions 스키마
./go.sh                    # 패치 → 서버 → CoEval 전체 체인
```

다중턴은 서버 경로에서 따로 확인한다. `serve.py` 는 무상태라
`session_from_messages()` 재생이 깨지면 파이프라인 테스트는 통과해도 실전에서 실패한다.

## 판정문

```
판정: 통과 | 되돌림
근거: test / audit / 골든 5종 결과
점수: <기존> → <새것> (N문항, ±X%p)
기준선: raw <점수> vs 파이프라인 <점수>
회귀 추가: <테스트 경로>
```

"좋아 보인다" 는 판정이 아니다. 숫자와 시나리오 결과만 쓴다.
