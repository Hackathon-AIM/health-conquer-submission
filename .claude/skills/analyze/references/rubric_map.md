# 루브릭 실패 → 레이어 대응표

analyst 가 원인을 지목할 때 쓰는 표. 확정된 매핑이 아니라 **첫 번째 가설**이다.
반드시 `logs/*.jsonl` 의 trace 로 확인한 뒤에 결론으로 쓴다.

## 감점 항목 (negative) — 우선순위 높음

| 루브릭 문구 계열 | 증상 | 1순위 레이어 | 확인할 trace | 고칠 파일 |
|---|---|---|---|---|
| emergency guidance not prominent / buried | 응급 안내가 뒤에 묻힘 | L4 | `red_flag` 있는데 답 앞 250자에 prefix 없음 | `generate.py: emergency_prefix`, `pipeline.py: node_l4` |
| fails to recognize emergency | 응급을 못 알아봄 | L1 | `red_flag = None` | `redflag.py`, `data/redflags.yaml` |
| unnecessary differential diagnosis list | 감별진단을 줄줄이 나열 | L4 | — | `prompts/templates/symptom.txt` |
| excessive hedging / over-qualification | 과잉 유보 표현 | L4 / L4c | `violations` 에 항목 없음 | 템플릿 + `prompts/critic.txt` |
| recommends unsafe medication | 위험한 약을 추천 | L4b | `drugs_out` 에 있는데 hit 없음 | `generate.py: output_gate`, `gates/risk.py` |
| contradicts itself | 자기모순 (추천+금지 동시) | L4b | rewrite 가 안 돌고 warning 만 삽입됨 | `generate.py: rewrite` 경로 |
| irrelevant information | 무관한 내용 | L3 | `ctx_docs` 높은데 무관한 제목 | `sources/*.py`, `rerank.py` |
| false alarm / unwarranted warning | 필요 없는 경고 | L1 / L1b | `red_flag` 오탐, DUR hit 오탐 | 정규식 lookbehind, `gates/dur.py` |

## 가점 미획득 (positive miss)

| 루브릭 문구 계열 | 증상 | 1순위 레이어 | 고칠 파일 |
|---|---|---|---|
| asks clarifying question | 되묻지 않음 | L2 / L4 | `classify.py` 의도, 템플릿 |
| states uncertainty appropriately | 불확실성 언급 없음 | L4c | `critic.py` strict_items 에 추가 |
| cites source / evidence | 근거 출처 없음 | L3 → L4 | `Context.docs` 의 locator 를 템플릿에서 노출 |
| tailors to context (나이/임신/신장) | 맥락 무시 | L1 → L4 | `entities.py` 위험인자 추출, 세션 union |
| mentions when to seek care | 내원 기준 미제시 | L4 | 템플릿 |
| bolds key action | 핵심 행동 강조 없음 | L4 | 템플릿 |
| considers drug interactions | 상호작용 미고려 | L1b | `gates/dur.py`, `medication_names` 채워졌는지 |

## 자주 틀리는 지목 세 가지

1. **"RAG 가 나쁘다"** — 대개 L3 가 아니라 L2 라우팅이 틀린 것이다.
   `trace.sources` 를 먼저 본다. 소스 선택이 맞았는데 문서가 무관하면 그때 L3.

2. **"모델이 못한다"** — `configs/l2_raw.yaml` 로 같은 질문을 돌려본다.
   raw 가 잘하면 모델 문제가 아니라 우리 레이어 문제다.

3. **"세션이 안 된다"** — `sess.update()` 는 턴 **끝**에 돈다.
   현재 턴에서 추출한 위험인자를 L4b 가 보려면 세션이 아니라 `plan` 에서 읽어야 한다.
   이 순서 버그로 "어제 술 먹고" 시나리오가 1턴에서 조용히 실패한 적이 있다.
