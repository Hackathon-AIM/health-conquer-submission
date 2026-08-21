---
name: analyze
description: HealthBench/CoEval 평가 결과에서 실패를 군집화하고 원인 레이어를 특정하는 절차. 점수가 떨어졌거나, CoEval 결과 JSON을 받았거나, "왜 이 점수인지 모르겠다" 할 때 사용.
---

# 평가 결과 분석

## 1. 어떤 숫자를 보고 있는지 먼저 확정한다

CoEval 출력 표에서 **점수는 `HEALTHBENCH RUBRIC` 열 하나뿐**이다.
`TIME` 은 벽시계 시간이고 채점에 들어가지 않는다. `SAMPLES` 는 표본 수다.

```
│ healthbench_conse… │ 0.733 │ 5 │ 29.0s │
                       ↑점수   ↑표본  ↑정보일 뿐
```

표본 5개짜리 숫자로는 아무 결론도 내지 않는다. 최소 50, 비교하려면 100.

집계는 `clipped_avg_aggregator` 다. 감점 항목이 평균을 음수로 끌 수 있어서 clip 한다.
→ **감점을 지우는 게 가점을 얻는 것보다 싸다.**

## 2. 실패를 모은다

```bash
# 로컬 (빠른 회전)
python -m eval.healthbench --variant consensus --config configs/openai.yaml --limit 50

# CoEval 결과가 있으면
ls CoEval/evaluation_outputs/*/*/results_healthbench_consensus.json
```

로컬 러너는 이미 두 가지를 뽑아준다:
- **가장 많이 놓친 기준 top 10** → 가점 미획득
- **밟은 감점 항목 top 10** → 감점

이 둘을 절대 한 표에 섞지 않는다. 우선순위가 다르다.

## 3. 군집화한다

같은 문장으로 요약되는 실패를 하나로 묶는다. 12건이 전부 "응급 안내가 앞에 없음"
이면 그건 12개의 문제가 아니라 **1개의 버그**다. 버그 1개를 고치면 12건이 같이 낫는다.

군집 이름은 루브릭 문구 그대로 쓴다. 의역하면 나중에 매칭이 안 된다.

## 4. 레이어를 특정한다

`logs/*.jsonl` 의 trace 로 대조한다. 추측하지 말고 다음 순서로 좁힌다:

```
trace.red_flag 가 None 인가?           → L1 (탐지 실패)
trace.sources 가 비었거나 엉뚱한가?     → L2 라우팅
trace.ctx_docs = 0 인가?               → L3 검색
trace.drugs_in 에 약이 없나?           → L1 엔티티
trace.drugs_out 에만 있나?             → L4b 출력 게이트 문제
trace.violations 가 비었는데 답이 나쁜가? → L4c 비평 항목 부재
전부 정상인데 답이 나쁜가?              → L4 프롬프트/템플릿
```

자세한 대응표: `references/rubric_map.md`

## 5. 파이프라인 자체를 의심한다

레이어 하나하나가 아니라 **파이프라인이 있는 게 손해**일 수 있다.
raw 모델(L1-16B-A3B)이 HealthBench-Consensus 93.5% 를 이미 낸다.

```bash
make ab-baseline    # raw vs 우리 파이프라인, 같은 50문항
```

파이프라인이 낮으면 그게 최우선 finding 이다. 흔한 원인 세 가지:
- **RAG 노이즈** — 무관한 문서가 컨텍스트에 들어가 답을 흐린다
- **과잉 경고** — 안 필요한 DUR/음주 경고가 붙어 감점
- **템플릿 경직** — 정해진 구조가 루브릭이 원하는 형태를 막는다

각각 `layers.*` 스위치를 하나씩 꺼서 확인한다. 한 번에 하나만.

## 6. 보고

analyst 에이전트의 산출물 형식을 따른다. 근거 샘플 id 없는 행은 쓰지 않는다.
