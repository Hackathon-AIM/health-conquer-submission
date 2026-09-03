# Conquer Health — 대국민 건강관리 챗봇

의과학 FM(30B/A5B MoE) + 의료 RAG 엔드포인트 · HealthBench Consensus 최적화

```bash
pip install -r requirements.txt
python data/build_dicts.py --seed
python -m eval.harness --config configs/mock.yaml    # 엔드포인트 없이도 바로 돈다
```

---

## 설계를 관통하는 한 문장

> **활성 5B로 프론티어를 이기는 방법은 모델을 똑똑하게 만드는 게 아니라,
> 모델이 실수할 수 있는 지점을 모델 바깥으로 빼내는 것이다.**

| 성격 | 어디로 | 이유 |
|---|---|---|
| 틀리면 치명적 | **규칙 · 조회** (L1, L1b, L4b) | 결정론적이라 틀릴 수 없다 |
| 모델이 잘하는 것 | **한 번에 하나씩** (L2, L4) | 작은 모델은 멀티태스킹에 약하다 |
| 모델이 놓치는 것 | **별도 패스로** (L4c) | 자기가 쓴 걸 자기가 못 본다 |
| 매번 달라질 수 있는 것 | **JSON 계약으로 고정** | 병렬 개발 + 재현성 |

---

## 파이프라인

```
사용자 발화
   │
   ├─ L1   엔티티 추출 · 레드플래그      결정론적 · ~0ms      ← C
   │        drugs / symptoms / risk_factors / temporal
   │        HIT 시 L2·L3 우회 → 응급 응답 빌더
   │
   ├─ L2   의도 분류                    LLM 1회 · 1~2s       ← D
   │        QueryPlan JSON (계층 간 유일한 계약)
   │        intent → INTENT_TO_SOURCES 표 조회 = 라우팅
   │
   ├─┬ L3  RAG 검색 (병렬)              1~3s                 ← B
   │ │      guideline / drug / law·hira / pubmed → 리랭킹 → top-5
   │ └ L1b 입력측 DUR 관문              ~0.5s                ← C
   │        ※ 서로 의존하지 않으므로 같은 gather에 넣어 동시 실행
   │
   ├─ L4   초안 생성 · 의과학 FM         3~6s                 ← D
   ├─ L4b  출력측 안전 게이트            ~0.5s                ← C
   │        모델이 '추천한' 약 + risk_check(음주·임신·간신)
   ├─ L4c  루브릭 비평 패스              2~3s                 ← D
   └─ 응답                              합계 8~15s
```

> ⚠️ 이 지연 예산은 **설계 목표이지 대회 규정이 아니다.** 공고에 타임아웃 기준은 없다.
> 실제 이유는 개발 루프 회전 수 — 200문항×3턴 기준 실험 1회가 턴당 8초면 ~27분, 15초면 ~50분.
> OpenAI SDK 기본 타임아웃은 **10분·재시도 2회**라 조용히 매달린다. `timeout=30`을 반드시 지정.

---

## 실제 HealthBench 로 점수 뽑기

CoEval을 받기 전에도 **진짜 벤치마크로 미리 잴 수 있다.** 데이터가 공개돼 있다.

```bash
make hb            # mock 파이프라인으로 구조 확인
make hb-live       # 실제 FM + 실제 채점자로 100문항
```

| 변형 | 규모 | 용도 |
|---|---|---|
| `consensus` | 3,671 | **대회 과녁** — FAQ가 "HealthBench Consensus 지표"라고 명시 |
| `hard` | 1,000 | 프론티어도 어려워하는 것 |
| `full` | 5,000 | 전체 |

채점 공식은 simple-evals 와 동일하다.

```
score = (충족한 기준의 점수 합) / (양수 점수 기준들의 합)
```

감점 항목(음수 points)은 **분모에서 제외**된다. 즉 감점을 밟으면 분자만 깎여서
점수가 빠르게 떨어진다. 그래서 "잘 쓰기"보다 **"감점 안 밟기"가 싸게 먹힌다.**

출력에 이 두 목록이 나온다. 다음에 뭘 고칠지가 여기서 정해진다.

```
[가장 많이 놓친 기준 top 10]
[밟은 감점 항목 top 10]  ← 가장 싸게 고칠 수 있는 곳
```

### ⚠️ 주의 2가지

**① 채점자 모델이 점수를 흔든다.**
원 벤치마크 기본값은 `gpt-4.1-2025-04-14` 다. 다른 모델로 채점하면 절대 점수가 몇 %p 달라진다.
→ 로컬 절대값을 CoEval 결과와 비교하지 말고 **설정 간 A/B 에만** 쓸 것.
현장에서 CoEval의 grader 가 무엇인지 확인해 여기 맞추면 상관계수가 올라간다.

**② 데이터를 공개 저장소에 커밋하지 말 것.**
OpenAI가 학습 데이터 오염 방지를 위해 예시를 온라인에 평문 공개하지 말 것을 요청하고 있다.
`.gitignore` 에 `eval/datasets/*.jsonl` 이 이미 들어 있다.

---

## 폴더 구조

```
med_ai/
├── configs/                  설정 = 실험 단위. git으로 어느 버전이 몇 점이었는지 추적
│   ├── mock.yaml             엔드포인트 없이 전체 실행
│   ├── live.yaml             현장용 (base_url 채우기)
│   ├── v1_no_critic.yaml     A/B: 비평 패스가 점수를 올리는가
│   ├── v2_full.yaml          A/B: 전 계층 활성
│   ├── emergency_bypass.yaml A/B: 응급 시 검색 우회
│   └── emergency_guideline.yaml
│
├── src/medai/
│   ├── contracts.py          ★ 계층 간 계약. 이것만 지키면 4명이 병렬로 일한다
│   ├── config.py             설정 로딩 · 프롬프트 경로
│   ├── llm.py                FM 클라이언트 · JSON 강제 · 재시도 · JSONL 로깅
│   │
│   ├── entities.py           L1  엔티티 추출 · 정규화 · 복합제 전개
│   ├── redflag.py            L1  레드플래그 (HealthBench 역추출 대상)
│   ├── classify.py           L2  의도 분류 · few-shot
│   ├── router.py             L2  INTENT_TO_SOURCES 표
│   │
│   ├── sources/              L3  ← TODO(현장) 엔드포인트만 채우면 됨
│   │   ├── base.py           공통 규약 · 타임아웃 · 예외 흡수
│   │   ├── guideline.py  drug.py  law.py  pubmed.py
│   │   ├── mock.py           엔드포인트 없이 돌리기 위한 스텁
│   │   └── __init__.py       레지스트리 + asyncio.gather 디스패처
│   ├── rerank.py             L3  리랭킹 · 컨텍스트 예산
│   │
│   ├── gates/
│   │   ├── dur.py            L1b/L4b  DUR 8종 · 심각도 · 경고 렌더
│   │   └── risk.py           L4b      DUR이 못 잡는 약-생활요인
│   │
│   ├── generate.py           L4/L4b  초안 · 출력 게이트 · 재작성
│   ├── critic.py             L4c     12항목 비평 · 수렴 루프
│   ├── session.py            세션 슬롯 (턴 간 되먹임)
│   ├── pipeline.py           오케스트레이션 (순수 async — 이것만으로 완전 동작)
│   ├── graph.py              LangGraph 래퍼 (선택)
│   └── prompts/              ★ 프롬프트는 코드 밖 파일로. git diff가 되어야 한다
│       ├── classifier.txt  critic.txt  extract_drugs.txt
│       └── templates/{emergency,drug,symptom,policy}.txt
│
├── data/
│   ├── build_dicts.py        사전 자동 생성 (손으로 만들지 않는다)
│   ├── redflags.yaml         ← D가 HealthBench에서 역추출해 교체
│   └── risk_rules.yaml       ← C가 허가사항 파싱으로 확장
│
├── eval/
│   ├── harness.py            ★ 1순위. 측정 없이는 개선이 없다
│   ├── patient_sim.py        시뮬레이션 환자 (루닛 하네스로 교체)
│   └── coeval_adapter.py     ← TODO(현장) CoEval 연결부
│
├── tests/test_gates.py       결정론적 계층 32개 테스트
└── scripts/smoke.py          현장 첫 60분 체크
```

---

## 왜 이 스택인가

| 도구 | 채택 | 이유 |
|---|---|---|
| **순수 async** | ✅ 코어 | 노드가 순수 함수라 테스트·교체가 자유롭다 |
| **LangGraph** | ⚠️ 선택 | `graph.py` 래퍼로만. 팀이 이미 익숙하면 쓰고, 아니면 `pipeline.py` 그대로 |
| **pydantic** | ✅ | 계층 간 계약을 강제. 4명 병렬 개발의 전제 |
| **httpx** | ✅ | 엔드포인트 비동기 병렬 호출 |
| **Neo4j** | ❌ | GraphRAG는 인덱싱 비용이 벡터 RAG의 **10~40배**. 20시간에 비현실적이고, 대회가 RAG 엔드포인트를 제공하므로 그래프를 만들 원본 코퍼스를 받을 수 있을지도 불확실 |
| **pgvector** | ❌ | 검색 엔드포인트가 제공되면 벡터DB를 만들 이유가 없다. 로컬에 필요한 건 약물 사전이고 그건 JSON이면 충분 |

> 차별화가 꼭 필요하면 약물 상호작용처럼 **관계가 명확한 좁은 서브도메인만** LightRAG로
> 그래프화하는 게 현실적 타협이다 (인덱싱 몇 분). Neo4j 전면 도입은 시간 손실.

---

## 역할 분담 (4인)

| | 담당 | 파일 | 첫 산출물 |
|---|---|---|---|
| **A** | 평가 | `eval/harness.py`, `coeval_adapter.py` | 금 16시까지 첫 점수 |
| **B** | 검색 | `sources/*`, `rerank.py` | `search(query, sources) → docs` |
| **C** | 안전 | `entities.py`, `redflag.py`, `gates/*` | 결정론적 · 단위 테스트 가능 |
| **D** | 프롬프트 | `classify.py`, `critic.py`, `prompts/*`, `redflags.yaml` | 루브릭 역추출 |

**의존성 최소화가 핵심**: B가 엔드포인트를 못 뚫어도 A·C·D는 `mode: mock`으로 계속 간다.
`contracts.py`의 QueryPlan만 먼저 합의하면 된다.

---

## 20시간 배분

| 시간 | 초점 | 이유 |
|---|---|---|
| 금 14–16 | 베이스라인 + 하네스 | 측정 없이는 개선 없음 |
| 16–20 | **프롬프트 · 응답 구조 반복** | 점수가 가장 많이 오르는 구간 |
| 20–02 | RAG 라우팅 + 출력 게이트 | 정확성 축 확보 |
| 02–06 | 비평 패스 + 폴백 | worst-of-n 방어 |
| 06–09 | 설정별 비교 (`make ab`) | 홀드아웃으로 과적합 확인 |
| **09:30** | **코드 동결** | 마지막 1시간 수정이 해커톤 최다 사망 원인 |

---

## 현장에서 채울 곳 (`TODO(현장)` 로 검색)

```
src/medai/sources/guideline.py   ENDPOINT
src/medai/sources/drug.py        ENDPOINT, CAUTION_FIELD
src/medai/sources/law.py         ENDPOINT (law, hira)
src/medai/sources/pubmed.py      ENDPOINT
src/medai/gates/dur.py           DurClient.ENDPOINT
eval/coeval_adapter.py           CoEvalScorer.score()
configs/live.yaml                llm.base_url, context_window
data/redflags.yaml               HealthBench 역추출로 교체
data/risk_rules.yaml             허가사항 파싱으로 확장
```

`python scripts/smoke.py configs/live.yaml` 로 12개 항목을 한 번에 점검한다.

---

## 명령어

```bash
make setup          # 의존성 + 시드 사전
make test           # 결정론적 계층 32개 테스트
make run            # mock 으로 하네스 실행
make live           # 실제 엔드포인트로 실행
make smoke          # 현장 첫 60분 점검
make ab             # 설정별 A/B 비교
make ab-emergency   # 응급 검색 우회 가설 검증
```

---

## 알아둘 것

**응급 시 검색 우회는 가설이지 규정이 아니다.**
HealthBench에서 근거가 확실한 것은 배치 점수뿐이다 (맨 앞 +10 / 뒤에 묻으면 −9 / 감별진단 나열 −5).
"검색을 하지 마라"는 어디에도 없다. `make ab-emergency`로 측정할 것.

**한국어 부분문자열 오탐에 주의.**
`r"술"`은 "수술·기술·예술"에 매칭된다. 실제로 국민건강보험법 제41조의 "처치·수술"이
음주로 오인되어 법률 답변에 음주 경고가 붙는 사고가 났다. `tests/test_gates.py`에 회귀 테스트가 있다.

**모델 배치는 규정 확인이 필요하다.**
`configs/*.yaml`의 `models.drafter`는 반드시 의과학 FM이어야 한다(심사 대상 텍스트).
나머지 4자리(classifier/rewriter/critic/extractor)에 다른 모델을 써도 되는지는
공고에 명시가 없으므로 **오프닝에서 반드시 질문할 것.** 기본값은 전 구간 FM(리스크 0).
