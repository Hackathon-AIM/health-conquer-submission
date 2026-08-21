# L2 플레이북 — 규칙·모델·MCP·데이터를 우리 아키텍처에 접붙이기

> 근거: 대시보드 규칙 페이지(2026-08-21 확인). 구현: `src/medai/l2.py`, `src/medai/mcp_client.py`

## 0. 규칙에서 곧바로 따라나오는 결정 6개

| 규칙 | 우리 결정 |
|---|---|
| 최종 출력물은 **반드시 L2 로 생성** | drafter = L2 고정. L4b 재작성·L4c 비평도 L2 로 (최종 텍스트를 만지는 모든 호출) |
| Evaluation 은 **외부 접근 차단 격리 환경** | OpenAI 등 외부 API 를 제출물 경로에서 완전 제거. 보조 역할까지 전부 L2. 외부 데이터를 쓰려면 **파일로 구워 제출물에 동봉** (런타임 fetch 금지) |
| 개발 중 harness 자유 구성 | `serve.py` + 우리 안전 레이어 유지 가능. 단, 격리 환경에서 도는지가 관건 → 의존성 최소 원칙 유지 |
| HealthBench **역공학 금지** (code review 로 확인) | `make redflags`(HealthBench 역추출)는 **폐기 대상**. 레드플래그 사전은 응급의학 일반 지식·공개 임상 기준으로만 구성. 코드/데이터에 HealthBench 유래 흔적을 남기지 않는다 |
| 하나의 제출물로 두 상에 공통 사용. **벤치마크 상** = CoEval HealthBench Consensus 자동 채점 최고점. **프론티어 상** = 벤치마크 **상위 10팀만** 대상, 임상의가 우리 챗봇 vs 프론티어 모델의 멀티턴 대화를 블라인드 비교 | 전략 순서가 정해진다: ① 벤치마크 점수로 top 10 진입이 관문 ② 그다음 멀티턴 대화 품질이 승부처. 멀티턴은 L2 의 공식 약점(§4)이므로 재작성·세션 주입이 프론티어 상의 무기다. `scripts/sim_loop.py` 기록을 사람이 읽는 것까지가 실험이다 |
| Codex 를 coding agent 로 | `AGENTS.md` 가 Codex 용 안내. `.claude/` 스킬 내용과 동일 원칙 |

**93.5% 라는 숫자는 잊는다.** 그건 L1-16B-A3B 얘기였다. 이제 기준선은
`configs/l2_raw.yaml` (L2 권장 사용법 그대로, 우리 레이어 없이)이고,
대시보드 검증 세트에서 직접 재야 한다. `make ab-baseline` 이 그 비교다.

## 1. L2 는 2단계 모델이다 — 우리 아키텍처가 이렇게 바뀐다

```
기존:  L1 → L2분류 → [L3 우리가 검색 ‖ L1b] → L4 drafter → L4b → L4c
이후:  L1 → L2분류 → [        L1b        ] → L4 = L2 2단계 → L4b → (L4c)
                                              ├ 생성단계: retrieve_relevant_content 만 노출
                                              └ 검색단계: MCP 도구 + finalize_retrieval
```

- **L3 와 rerank 는 L2 경로에서 은퇴한다.** 검색 주체가 L2 모델 자신이다.
  우리가 통제하는 것은 ① 어떤 도구를 노출할지(서브셋) ② 도구 호출 예산
  ③ 근거 블록 크기 ④ 질의 재작성뿐이다.
- **우리 안전 레이어는 그대로 산다.** 레드플래그(L1), DUR 게이트(L1b/L4b),
  응급 prefix 강제, 무상태 세션 재생 — 이건 L2 가 해주지 않는 우리 몫이다.
- 시스템 프롬프트는 단계별로 분리 (가이드 명시):
  `prompts/l2_retrieval.txt` / `prompts/l2_generation.txt`. 합치지 않는다.

## 2. MCP 21종 — 상황별 사용법 (핵심 질문의 답)

### 왜 서브셋인가
21개를 다 주면 모델이 도구를 고르다 헤매고, 엉뚱한 코퍼스에 예산을 태운다.
intent 는 이미 분류 단계가 정하므로 **판단은 LLM, 매핑은 코드** (router.py 원칙).
매핑표는 `l2.py: TOOLSETS` — 한 줄 고치면 전 케이스 반영, A/B 가능.

### 도구를 성격으로 나누면 5가지다

**① 계층 인덱스 (hira 249 + guideline 120 문서)** — 본선 근거의 주력
```
index_list_documents      목록/관련도 정렬  → 훑기
index_get_relevant_nodes  질의→관련 섹션    → 조준        ★ 시작점으로 최적
index_keyword_search      정확 키워드+빈도  → 조문·용어 확인
index_get_page_content    페이지 원문+cite_uid → 인용 재료  ★ 여기만 인용 가능(추정)
index_get_document_structure  섹션 트리    → 기본 제외 (relevant_nodes 와 중복, 예산 절약)
```
정석 체인: `relevant_nodes → page_content(좁은 범위)`. list 부터 시작하면 한 호출 손해.

**② 약물 (한국 허가 + 미국 라벨 + 부작용 DB)**
```
openapi_mfds_get_drug_indication     허가 효능·용법·경고 ★ 한국 약 질문 1순위
openapi_mfds_check_drug_permission   허가/취하 여부
openapi_mfds_find_drugs_by_ingredient 동일성분 대체약
adr_retrieve_drug_info               DailyMed 영문 라벨 — 상호작용·경고 보강
rag_sql_query(faers)                 부작용 신고 통계 — "이 약 부작용 흔한가요"
openapi_hira_get_drug_price          급여 등재·상한가 — 비용 질문
```
주의: adr_* 는 **영문 성분명**을 원한다. 한국 제품명 → MFDS 로 성분 확인 → 영문명 변환.

**③ 급여·제도**: `hira_updates_search`(고시·심의사례), `openapi_hira_disease_check_code`,
hira 코퍼스 인덱스, `rag_vector_query(hira_faq)`

**④ 법령 3단 체인**: `openapi_law_search → openapi_law_list_articles → openapi_law_get_article`
— 항상 3호출 세트다. POLICY intent 예산을 최소 5 이상으로 봐야 하는 이유.

**⑤ 코드·문헌**: `kcd_search_codes/kcd_get_name`, `rag_vector_query(pubmed_abstracts)`

**개발 전용 (런타임 제외)**: `rag_get_all_data_sources`, `rag_get_data_source_detail`
— 스키마는 probe 로 미리 떠서 SQL 사용 프롬프트에 박는다. 런타임에 스키마 탐색 = 예산 낭비.

### intent → 서브셋 (l2.py TOOLSETS, A/B 대상)

| intent | 도구 | 근거 |
|---|---|---|
| emergency | (없음) | 즉시 안내 우선 — 우리 가설, `ab-emergency` 로 측정 |
| drug_safety | 약물② + FAERS/약가 | 허가사항이 근거의 왕 |
| drug_recommend | 약물② + 인덱스① | 약 + 비약물 대처·내원 기준 |
| symptom_consult | 인덱스① + pubmed | 가이드라인 중심 |
| info_request | 인덱스① + pubmed + KCD | |
| policy | 인덱스① + 법령④ + HIRA③ + FAQ + KCD | |

## 2-b. 자주 나오는 질문 — MCP 를 꼭 써야 하나 / description 은 누가 쓰나 / `@tool` 이 필요한가

**Q1. RAG 데이터를 꼭 MCP 로 써야 하나?**
제공 코퍼스(guideline 120 · hira 249 · pubmed · FAERS · DailyMed · KCD)는 **MCP 가
유일한 접근 경로**다. 원본 덤프를 주지 않으므로 파일로 받아 우리가 인덱싱할 수 없다.
게다가 L2 는 애초에 **이 tool set 으로 학습됐다**(가이드 명시) — 다른 검색기를 붙이면
모델이 학습한 검색 행동과 어긋난다. 규칙상 외부 데이터는 라이선스만 확보하면 추가할
수 있는데, 평가가 격리 환경이라 **런타임 fetch 는 불가**하고 파일로 구워 동봉해야 한다
(예: 식약처 DUR 고시 CSV → `data/` → L1b 가 로컬 조회).

**Q2. description 을 잘 적어야 하지 않나?**
**우리가 쓰지 않는다. 서버가 이미 써놨다.** `tools/list` 가 이름·description·
inputSchema 를 통째로 준다. 우리 몫은 잘 쓰는 게 아니라 **훼손하지 않는 것**이다.

실측(`data/probe/tools.json`)에서 실제로 훼손하고 있었다 — `openai_tools()` 가
description 을 1024자에서 자르고 있었고, 이보다 긴 도구가 4개였다:

| 도구 | description | 잘리면 |
|---|---|---|
| `rag_vector_query` | 2,165자 | **절반 손실** — collection 이름·필터 사용법이 뒷부분에 있어 `collection_name` 을 못 채운다 |
| `openapi_law_get_article` | 1,216자 | article_keys 형식 설명 손실 |
| `rag_sql_query` | 1,108자 | SQL 방언·제약 손실 |
| `openapi_mfds_get_drug_indication` | 1,034자 | 옵션 필드 설명 손실 |

→ 절단 상한을 8,000자로 올려 사실상 무절단으로 바꿨다. 대신 `outputSchema`·`_meta`
는 애초에 안 보낸다(호출에 불필요한데 `rag_sql_query` 것만 15,797자다).

서브셋별 실측 비용 (매 요청에 실리는 양):

| intent | 도구 수 | ≈토큰 |
|---|---|---|
| emergency | 0 | 0 |
| drug_safety | 6 | 2,570 |
| symptom_consult | 5 | 2,789 |
| info_request | 7 | 3,202 |
| drug_recommend | 8 | 3,583 |
| policy | 12 | 5,435 |
| (전체 21개) | 21 | 8,687 |

전체를 실어도 8.7K 토큰이라 32K 컨텍스트에 안 들어가는 건 아니다. 서브셋의 이유는
**용량이 아니라 정확도** — 선택지가 적을수록 모델이 덜 헤매고 예산을 덜 태운다.
이건 가설이므로 `TOOLSETS` 를 넓혀가며 A/B 로 재야 한다.

**Q3. 파이썬에서 `@tool` 데코레이터로 가져와야 하나?**
아니다. `@tool` 은 LangChain 이 **파이썬 함수**를 도구로 만들 때 쓰는 것이다.
MCP 도구는 이미 원격에 있고 스키마도 서버가 주므로, 우리는 **형식 변환만** 한다:

```python
# mcp_client.py — MCP tools/list → OpenAI tools 파라미터
{"type": "function", "function": {
    "name":        t["name"],           # 서버가 준 것
    "description": t["description"],    # 서버가 준 것 (자르지 않는다)
    "parameters":  t["inputSchema"],    # 서버가 준 것
}}
```

우리가 **직접 스키마를 쓰는 도구는 딱 둘**이고, 이것만 `@tool` 에 해당하는 일이다:

| 함수 | 어디에 | 하는 일 |
|---|---|---|
| `finalize_retrieval` | 검색 단계 | 모델이 cite_uid 선택을 제출하고 검색을 끝낸다 |
| `retrieve_relevant_content` | 생성 단계 | 모델이 부르면 우리가 검색 단계를 돌려 근거를 넣어준다 |

둘 다 MCP 도구가 아니라 **우리 하네스가 정의해 주입**하는 것이다(가이드 명시).
`l2.py` 의 `FINALIZE_TOOL` / `RETRIEVE_TOOL` 딕셔너리가 그 스키마이고,
이 둘의 description 은 **우리가 잘 써야 하는 유일한 description** 이다.

LangChain 을 쓰지 않는 이유: 의존성 하나가 격리 평가 환경에서 사고 하나다.
`tools=[...]` 딕셔너리를 직접 만드는 게 더 짧고 디버깅이 쉽다.

## 3. 청킹·파싱 — "우리가 청킹하지 않는다"가 답의 절반

코퍼스는 **서버 쪽에 이미 인덱싱**되어 있다 (섹션 트리·페이지 단위·시맨틱 노드).
우리가 정할 것은 청킹이 아니라 **소비량**이다:

1. **페이지 열람 폭** — `index_get_page_content` 는 호출당 최대 20페이지.
   20을 다 받으면 컨텍스트가 넘치고 노이즈가 는다.
   시작값: relevant_nodes 가 준 page range 그대로, 상한 6페이지. probe 실측 후 조정.
2. **근거 블록 컷** — `l2.evidence_chars_per_item` 기본 1800자(≈1250tok),
   최대 6블록 ≈ 7500tok. L2 컨텍스트 크기를 현장에서 확인하고 재조정.
3. **도구 결과 컷** — 검색 단계 대화에 넣는 결과는 8000자 컷 (l2.py).
   FAERS SQL 이 수만 행을 뱉으면 LIMIT 를 프롬프트로 강제.

**결정에 필요한 실측 4가지 — `python scripts/probe_mcp.py` 한 번이면 나온다:**
- 페이지당 문자 수 분포 → ①②의 상수 확정
- cite_uid 실제 형식 → `mcp_client.find_cite_uids` 패턴 검증 (지금은 `cite-hex` + JSON 필드 가정)
- **cite_uid 가 없는 도구 목록** → 그 도구는 인용 불가 = 탐색용. 인용이 필요한 답이면
  반드시 page_content 류로 마무리하도록 검색 프롬프트에 명시
- 도구별 지연 → `max_tool_calls=8` 이 시간으로 몇 초인지 환산

우리가 직접 파싱·청킹하는 유일한 경우: **외부 데이터 동봉** (허용됨, 라이선스 확보 시).
예: 식약처 DUR 고시 CSV → `data/` 에 구워 L1b 가 로컬 조회. 격리 환경에서도 동작.

### 실측 결과 1차 (2026-08-21, data/probe/)

서버가 이미 해놓은 것이 확인됐다 — 문서마다 **섹션 트리 + 노드별 영문 요약 +
페이지 범위 + 시맨틱 검색 점수**까지 있다 (예: `doc_id=xvoqs, node xvoqs.5.5.2,
range [47,47], summary, score 0.627`). 우리가 청킹할 것이 없다는 판단 확정.

| 실측 | 값 | 결정 |
|---|---|---|
| 도구 지연 | 중앙값 ~85ms, 최대 727ms | 병목은 모델 왕복이지 도구가 아님. max_tool_calls 8 유지 (필요시 10~12 여유 있음) |
| 응답 크기 | 목록류 7~20K자, OpenAPI 류 소형 | 도구 결과 8000자 컷 유지 — 목록 하위권은 어차피 저관련 |
| cite_uid 형식 | `cite-` + 16 hex — 우리 정규식과 일치 | find_cite_uids 유지 |
| 인자 이름 | query 가 아니라 `drug_name`/`name`/`collection_name`/`source_name`/`mst` | probe 수정 완료. 실전 L2 는 tools/list 스키마를 보므로 무관 |
| 법령 체인 | law_search 가 mst 를 주며 "list_articles(mst) 로 여세요" 라고 직접 안내 | 서버가 체인을 유도 — 프롬프트에서 따로 강제할 필요 낮음 |

### 실측 2차 — 인자를 고치고 나서 그림이 바뀌었다

1차에서 "cite_uid 는 hira_updates_search 에만 있다"고 판단했는데 **틀렸다.**
그건 인자 이름이 틀려 호출이 전부 실패한 탓이었다. 제대로 부르니:

| cite_uid **있음** (조회 = 근거) | cite_uid **없음** (탐색 = 근거 아님) |
|---|---|
| `index_get_page_content` · `hira_updates_search` · `kcd_search_codes` · `kcd_get_name` · `openapi_mfds_check_drug_permission` · `openapi_mfds_get_drug_indication` · `adr_retrieve_drug_info` · `openapi_hira_get_drug_price` · `rag_vector_query` | `index_list_documents` · `index_get_relevant_nodes` · `index_keyword_search` · `openapi_law_search` · `openapi_law_list_articles` |

설계 의도가 분명하다 — **목록·요약은 근거가 아니고, 본문 조회만 인용 가능하다.**
그래서 합성 cite_uid 는 `EXPLORATORY` 도구에는 **붙이지 않는다**(l2.py).
붙이면 "문서 제목 목록"을 근거로 인용하게 된다.

지연도 1차보다 현실적이다: `openapi_hira_get_drug_price` 6.1초,
`rag_vector_query` 3.2초, `openapi_mfds_get_drug_indication` 2.9초.
도구 8회면 최악 10~25초. 예산을 6으로 줄일지는 A/B 로 결정한다.

### ★ 청킹은 서버가 했지만 **파싱은 우리가 해야 한다**

`index_get_page_content` 실측 (`page_stats.json`):

| 열람 | 지연 | 크기 | 페이지당 |
|---|---|---|---|
| 1장 | 51ms | 3,929자 | **3,929자** |
| 3장 | 49ms | 11,845자 | 3,948자 |
| 5장 | 44ms | 16,432자 | 3,286자 |

여기서 두 가지 버그가 드러났다:

1. **`evidence_chars_per_item = 1800` 이면 페이지 한 장도 절반이 잘린다.**
   그것도 뒤가 잘리는데, 권고 문장은 보통 문단 뒤쪽에 있다. → **4000 으로 상향**
   (실측 손실량: 페이지당 1,889자가 사라지고 있었다)
2. **근거 블록에 JSON 원문을 그대로 넣고 있었다.**
   응답은 `{"cite_uid":..,"url":..,"title":..,"pages":[{"page":47,"text":".."}]}`
   구조라 껍데기가 예산을 먹고 모델이 읽기도 나쁘다.

→ `l2.py: render_evidence()` 추가. cite_uid 로 해당 객체를 찾아 제목·URL·
   `pages[].text` 를 뽑아 사람이 읽는 형태로 만든다. 이것이 우리가 하는 파싱의 전부다.

```
[1]
source_type: guideline/hira
Prevention of cardiovascular disease : guidelines for ...
url: https://www.who.int/publications/i/item/9789241547178
[p.47] ence of other cardiovascular risk factors (264, 268). Observational data ...
```

예산: 6블록 × 4,000자 ≈ 24,000자 ≈ 7,200토큰 + 도구 정의 2.5~5.4K + 프롬프트.
32K 컨텍스트에 여유가 있다. `tool_result_chars` 는 12,000 (3페이지 열람이 들어간다).

### SQL 을 쓰려면 스키마를 볼 수 있어야 한다

`rag_sql_query` 는 FAERS/DailyMed/KCD 를 SQL 로 조회하는데, 스키마(FAERS 14KB)를
프롬프트에 박기엔 너무 크다. 런타임 서브셋에서 `rag_get_data_source_detail` 을
빼놨던 것은 실수 — 모델이 컬럼명을 모른 채 SQL 을 쓸 수 없다. `_DRUG_DEEP` 에 되돌렸다.
(FAERS 스키마에 "개별 레코드를 반환하는 쿼리에는 `safetyreportid` 컬럼을 반드시
포함하라"는 지시가 있는데, 이런 것은 모델이 직접 읽어야 지킨다.)

## 4. 멀티턴 — L2 의 공식 약점, 우리 하네스의 주전장

가이드: "L2 는 single-turn 최적화. multi-turn 완화가 challenge 의 일부."

우리 대응 (이미 구현):
1. **질의 재작성** (`pipeline._l2_query`) — 멀티턴이면 지시어를 해소한 자기완결
   질의로 재작성 후 L2 에 투입. 1턴이면 재작성 없이 그대로 (지연·왜곡 방지).
2. **세션 요약 주입** (`pipeline._session_note`) — 나이·임신·위험인자·복용약을
   생성 단계 시스템 컨텍스트로. 규칙 추출 기반이라 무상태 serve.py 에서도 재생된다.
3. **retrieval query 는 항상 자기완결** — 생성 프롬프트에 명시.

Patient Simulator 규칙: 첫 질문 원문 보존 / 3턴 내외 중단 / 404 는 새 대화, 502 는 재시도.
`scripts/sim_loop.py` 가 이 규칙대로 대화 기록을 만든다. **전문가 평가 대비는
이 기록을 사람이 읽는 것**이다.

## 5. 예산 산수

검색 1회 = 도구 8회 이하 + 모델 왕복 ≤9회. 도구 60s 타임아웃 최악이면 수 분.
실전 중앙값은 probe 로 재고, `llm.timeout=90` / CoEval 쪽 `timeout: 360` 안에
들어오는지 확인. 생성 단계 retrieve 는 턴당 2회 상한 — 3회째부터는
"가진 것으로 답하되 불확실성을 명시"로 강제 (l2.py).

## 6. 현장 체크리스트 (기존 smoke 체크리스트에 추가)

```
[ ] probe_mcp.py 실행 → 페이지 크기·cite_uid 형식·지연 실측 (§3의 상수 4개 확정)
[ ] L2 가 tool_calls 를 OpenAI 형식으로 내는지 (vLLM tool parser 동작 확인)
    → 안 되면: 프롬프트 기반 JSON 도구 호출로 폴백 (smoke 의 JSON 5/5 가 분기점)
[ ] L2 응답에 <think> 있는지 → strip_think 유지 여부
[ ] 대시보드 제출 형식 — serve.py 를 부르는가, 다른 인터페이스인가
[ ] 검증 세트 실행 비용/횟수 제한 — 회전 전략 결정
[ ] 격리 환경 스펙 — 우리 의존성(httpx·openai·pydantic·yaml)이 다 있는가
[ ] make redflags 경로 폐기 확인 (역공학 금지 규칙) — data/redflags.yaml 을
    일반 임상 기준 유래로 재작성했는가
[ ] ab-baseline: l2_raw vs l2_live 를 대시보드 검증 세트에서 비교
```

## 6-b. 1차 스모크 실측 (2026-08-21, data/probe/l2_smoke.json) — 4/6 통과

| # | 항목 | 결과 | 조치 |
|---|---|---|---|
| 1 | 엔드포인트 | ✅ 502ms, `Lunit/L2-preview` 확인 | — |
| 2 | 일반 chat | ❌ **빈 응답** (5.3초, think 블록 없음) | L2 는 의료 질의가 아니면 답을 안 낼 수 있다. 도구 없는 호출을 피하도록 l2.py 최종 폴백 수정 |
| 3 | **tool_calls** | ✅ 네이티브 지원 확인 | ★ 최대 리스크 해소. 프롬프트 기반 폴백 불필요 |
| 4 | 검색 단계 | ❌ 근거 0블록 | 아래 §6-c |
| 5 | 2단계 generate | ✅ 910자, 근거 없이도 답변 생성 | 인용번호 없음 — 근거가 안 왔으니 당연 |
| 6 | 파이프라인 전체 | ✅ 동작, 그러나 **81.9초** | 아래 §6-d |

### 6-c. 검색 단계가 찾은 근거를 버린 사건 (가장 중요한 발견)

L2 는 목표 문장을 **실제로 찾았다**. finalize 의 note 원문:

> "tyfna.1.6.3.6(페이지 50)에 'systolic blood pressure goal below 130 mm Hg' 가 있다.
> 그러나 이는 Synopsis 요약이고 본문 CKD 섹션을 아직 찾지 못했다."

그리고 `status="partial", items=[]` 로 종료했다. **완벽하지 않다는 이유로 찾은 근거를
전량 폐기**한 것이다. 생성 단계는 빈손으로 답을 썼다(그래서 인용번호가 없었다).

두 겹으로 막았다:
- 프롬프트(`l2_retrieval.txt`): "items 를 비운 채 partial 을 내지 마라. 완벽하지 않으면
  relevance_score 를 낮게 주어 표현하라"
- 하네스(`l2.py`): items 가 비었는데 수집한 uid 가 있으면 **수집분으로 자동 복구**.
  note 는 살려서 생성 단계로 넘긴다(무엇을 못 찾았는지가 유용한 정보다).

교훈: 이 모델은 **보수적으로 자기 결과를 버리는 쪽**으로 실패한다. 하네스는
"모델이 아무것도 안 냈을 때"뿐 아니라 **"모델이 스스로 버렸을 때"도 복구**해야 한다.

### 6-d. 81.9초 — 어디서 샜나

```
l1      2ms      레드플래그 (결정론)
l2  33,708ms  ← 분류. 최대 병목
l3_l1b   0ms      (L2 네이티브라 우리 검색 없음)
l4  36,361ms  ← 검색 8회 + 생성
l4b 11,822ms  ← 출력 게이트의 약물 추출 LLM 1회
l4c      0ms      (l2_live 에서 비평 꺼둠)
```

도구는 범인이 아니다(중앙값 85ms). 전부 **L2 추론 시간**이다. 조치:

| 원인 | 조치 |
|---|---|
| 분류 JSON 에 불필요한 `queries`(검색어 4종)·`extra_sources` 가 있었다. l2_native 에서는 L2 가 검색어를 스스로 만들므로 **완전히 낭비** | `classifier_l2.txt` 신설 — 해당 필드 제거, 대신 응답 설계 필드 추가 |
| `max_tokens: 2048` 이라 모델이 장황해짐 | `classifier_max_tokens: 600`, `extractor_max_tokens: 400` |
| 검색 8회를 다 씀 | 예산은 유지하되 관측. 2차 측정 후 6으로 내릴지 결정 |

CoEval `timeout: 360` 안에는 들어가므로 실패는 아니다. 문제는 **처리량**
(200문항×3턴 = 13.6시간)과 **프론티어 상**(임상의가 82초를 기다린다).

## 6-e. 채점 축과 우리 분류의 정렬 — 원래 안 맞았다

**intent 6종은 "무엇을 검색할까"(라우팅)를 정할 뿐, "어떻게 답할까"를 정하지 않는다.**
루브릭이 채점하는 것은 후자다. HealthBench 논문이 공개한 축(테마 7 / 축 5)에 대면
우리가 다루던 것은 emergency 하나뿐이었다.

> ※ 공개된 축 분류를 참고하는 것은 규칙이 금지한 "과도한 역공학"(문항·루브릭 추출)과
>   다르다. 우리는 **무엇을 잘해야 하는지**만 참고하고, 문항은 건드리지 않는다.

| 채점 축 | 이전 | 지금 |
|---|---|---|
| 응급 안내 배치 | ✅ 레드플래그 + prefix 강제 | 유지 + 생성 지시문에 명시 |
| **맥락 탐색(되묻기)** | ⚠️ `need_followup` 을 계산만 하고 **L2 경로에서 버렸다** | `clarifying_question` 신설 — 실제 질문 문장을 만들어 생성 지시문에 주입 |
| **응답 깊이** | ❌ 없음 | `depth: brief/standard/thorough` — 단순 질문에 장문 = 감점 |
| **불확실성 표현** | ❌ 없음 | `uncertainty` — 있을 때만 명시, 없으면 유보 표현 금지 |
| 상대 수준별 소통 | ⚠️ `persona` 있었으나 L2 경로에서 미사용 | 지시문에 반영 |
| 약물 안전 | ✅ L1b/L4b 게이트 | 유지 + 지시문에 금기 안내 요구 |

**핵심 버그였다**: `node_l4` 가 `l2.generate(query, intent, context_note)` 만 넘겨서
분류가 33초를 들여 계산한 `need_followup`·`persona`·`missing_info` 가 **전부 버려지고
있었다**. `_l2_instructions()` 를 추가해 축마다 지시 한 줄로 변환해 주입한다.

### 티키타카 설계 (프론티어 상의 실체)

3턴뿐이라 한 턴을 질문에만 쓰면 손해다. 그래서 규칙을 셋으로 고정했다:

1. **답을 먼저 하고 마지막에 질문 하나** — 질문만 던지고 끝내지 않는다
2. **질문은 한 개만** — 나열하면 상대가 답하기 어렵다
3. **근거 없는 되묻기 금지** — `why_it_changes_answer` 가 비면 코드가 `need_followup`
   을 강제로 끈다. `clarifying_question` 이 비어도 끈다 (두 필드는 한 상태다)

## 7. 남는 리스크 (해결 못 한 것 — 지어내지 않고 남긴다)

- ~~tool calling 신뢰도~~ → **해소** (1차 스모크 #3 통과, 네이티브 지원 확인)
- ~~cite_uid 형식 가정~~ → **확인** (`cite-`+16hex, 정규식 일치)
- **속도**: 조치는 했으나 재측정 전이다. 82초 → 목표 40초 이하.
  못 내려가면 분류를 규칙 기반으로 더 밀어내야 한다(entities 는 이미 규칙이 있다).
- **되묻기가 점수를 올리는지 미검증**: 되묻기는 맥락 탐색 축에서 가점이지만
  불필요하면 감점이다. `layers` 스위치로 켜고 끄며 A/B 로 재야 한다.
- **`depth` 판정 품질 미검증**: 모델이 brief 를 제대로 고르는지 실측 필요.
  전부 standard 로 몰리면 규칙(질문 길이·물음표 수)으로 보정한다.
- **L2 가 빈 응답을 내는 조건 불명**: 스모크 #2 에서 인사말에 빈 content.
  의료 도메인 밖이라서인지, 도구가 없어서인지 미분리. 폴백은 넣어뒀다.
