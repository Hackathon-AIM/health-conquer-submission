# MCP 21종 — 상황별 사용법

> 코드의 진실은 `src/medai/l2.py` 의 `TOOLSETS` / `EXPLORATORY` 다.
> 이 문서와 코드가 어긋나면 **코드를 보고 이 문서를 고친다.**
> 도구 이름·출처·설명 원문은 `docs/spec.md §2.2`.

## 1. 인용 가능 여부 — 가장 중요한 구분

설계 의도가 분명하다: **목록·요약은 근거가 아니고, 본문 조회만 인용 가능하다.**

| `cite_uid` **있음** — 조회 = 근거 | `cite_uid` **없음** — 탐색 = 근거 아님 |
|---|---|
| `index_get_page_content` | `index_list_documents` |
| `hira_updates_search` | `index_get_relevant_nodes` |
| `kcd_search_codes` · `kcd_get_name` | `index_keyword_search` |
| `openapi_mfds_check_drug_permission` | `index_get_document_structure` |
| `openapi_mfds_get_drug_indication` | `openapi_law_search` |
| `adr_retrieve_drug_info` | `openapi_law_list_articles` |
| `openapi_hira_get_drug_price` | `rag_get_all_data_sources` |
| `rag_vector_query` | `rag_get_data_source_detail` |

→ `l2.py: EXPLORATORY` 에는 **합성 cite_uid 를 붙이지 않는다.** 붙이면 "문서 제목 목록"을
근거로 인용하게 된다. 인용이 필요한 답이면 반드시 본문 조회로 이어져야 한다.

## 2. 도구 5분류

**① 계층 인덱스** (hira 249 + guideline 120) — 본선 근거의 주력
```
index_get_relevant_nodes  질의→관련 섹션   ★ 시작점으로 최적
index_get_page_content    페이지 원문      ★ 여기만 인용 가능
index_keyword_search      정확 키워드+빈도
index_list_documents      목록/관련도 정렬
index_get_document_structure  섹션 트리 — 기본 제외 (relevant_nodes 와 중복)
```
정석 체인: `relevant_nodes → page_content(좁은 범위)`. list 부터 시작하면 한 호출 손해.

**② 약물**
```
openapi_mfds_get_drug_indication      허가 효능·용법·경고  ★ 한국 약 질문 1순위
openapi_mfds_check_drug_permission    허가/취하 여부
openapi_mfds_find_drugs_by_ingredient 동일성분 대체약
adr_retrieve_drug_info                DailyMed 영문 라벨 — 상호작용·경고
rag_sql_query (faers)                 부작용 신고 통계
openapi_hira_get_drug_price           급여 등재·상한가
```
⚠️ `adr_*` 는 **영문 성분명**을 원한다. 한국 제품명 → MFDS 로 성분 확인 → 영문 변환.

**③ 급여·제도**: `hira_updates_search` · `openapi_hira_disease_check_code` · hira 인덱스 · `rag_vector_query(hira_faq)`

**④ 법령 3단 체인**: `openapi_law_search → openapi_law_list_articles → openapi_law_get_article`
— **항상 3호출 세트**다. POLICY intent 예산이 최소 5 이상이어야 하는 이유.

**⑤ 코드·문헌**: `kcd_search_codes` / `kcd_get_name` · `rag_vector_query(pubmed_abstracts)`

**개발 전용**: `rag_get_all_data_sources`
— 단, `rag_get_data_source_detail` 은 **런타임에 필요하다**. `rag_sql_query` 를 쓰려면
모델이 컬럼명을 알아야 하기 때문이다. `_DRUG_DEEP` 에 포함돼 있다.

## 3. intent → 서브셋 (`l2.py: TOOLSETS`)

| intent | 서브셋 | 도구 수 | ≈토큰 | 근거 |
|---|---|---|---|---|
| `EMERGENCY` | (없음) | 0 | 0 | 즉시 안내 우선 — **가설**, `ab-emergency` 로 측정 |
| `DRUG_SAFETY` | `_DRUG \| _DRUG_DEEP` | 6 | 2,570 | 허가사항이 근거의 왕 |
| `SYMPTOM_CONSULT` | `_INDEX \| _VECTOR` | 5 | 2,789 | 가이드라인 중심 |
| `INFO_REQUEST` | `_INDEX \| _VECTOR \| _KCD` | 7 | 3,202 | |
| `DRUG_RECOMMEND` | `_DRUG \| _INDEX` | 8 | 3,583 | 약 + 비약물 대처·내원 기준 |
| `POLICY` | `_INDEX \| _LAW \| _HIRA \| _VECTOR \| _KCD` | 12 | 5,435 | 법령 체인 3호출 필요 |
| (전체) | — | 21 | 8,687 | 32K 컨텍스트에 들어가긴 한다 |

**서브셋의 이유는 용량이 아니라 정확도다.** 선택지가 적을수록 모델이 덜 헤매고 예산을
덜 태운다. 이건 가설이므로 `TOOLSETS` 를 넓혀가며 A/B 로 잰다 — 한 줄 고치면 전 케이스 반영.

## 4. description 은 우리가 쓰지 않는다

`tools/list` 가 이름·description·inputSchema 를 통째로 준다. 우리 몫은 잘 쓰는 게 아니라
**훼손하지 않는 것**이다. 실제로 훼손하고 있었다 — `openai_tools()` 가 1024자에서 자르고
있었고 이보다 긴 도구가 4개였다 (`rag_vector_query` 2,165자 · `openapi_law_get_article`
1,216 · `rag_sql_query` 1,108 · `openapi_mfds_get_drug_indication` 1,034).
→ 상한을 8,000자로 올려 사실상 무절단. `outputSchema`·`_meta` 는 애초에 안 보낸다.

**우리가 직접 쓰는 description 은 딱 둘**이고, 이것만 잘 써야 한다:
`l2.py: FINALIZE_TOOL` · `RETRIEVE_TOOL`.

LangChain `@tool` 은 필요 없다. MCP 도구는 이미 원격에 있고 스키마도 서버가 준다.
우리는 형식 변환만 한다 (`mcp_client.py: openai_tools()`).

## 5. 예산 상수 (실측 기반 — `docs/l2_playbook.md §3`)

| 상수 | 값 | 근거 |
|---|---|---|
| `max_tool_calls` | 8 | 도구 지연 중앙값 85ms, 최악 6.1초(`hira_get_drug_price`). 8회면 최악 10~25초 |
| `max_retrievals_per_turn` | 2 | 3회째부터 "가진 것으로 답하되 불확실성 명시" 강제 |
| `evidence_chars_per_item` | 4000 | 가이드라인 1페이지 = 3,929자. 1800이면 페이지 절반이 잘렸다 |
| `max_evidence_items` | 6 | 6 × 4000 ≈ 24,000자 ≈ 7,200토큰 |
| `tool_result_chars` | 3000 | **검색 대화에 누적**되는 양. 12000 으로 뒀다가 8회 누적 96,000자로 `input_limit_exceeded` |
| `retrieval_char_budget` | 24000 | 도구 결과 총량. 넘으면 오래된 것부터 비운다 |
| `index_get_page_content` 열람 폭 | 상한 6페이지 | 호출당 최대 20페이지지만 다 받으면 노이즈 |

⚠️ **근거 예산과 검색 대화 예산을 헷갈리지 말 것.** 앞의 둘은 생성 단계에 들어가는 양,
뒤의 둘은 검색 단계 대화에 쌓이는 양이다. 후자를 키우면 컨텍스트 초과로 터진다.

## 6. 이 모델의 알려진 실패 모드

**보수적으로 자기 결과를 버린다.** 실측에서 L2 가 목표 문장을 찾아놓고
`status="partial", items=[]` 로 종료했다 — "완벽하지 않다"는 이유로 전량 폐기.
생성 단계는 빈손으로 답을 썼다.

두 겹으로 막아뒀다:
- 프롬프트(`prompts/l2_retrieval.txt`): "items 를 비운 채 partial 을 내지 마라.
  완벽하지 않으면 `relevance_score` 를 낮게 주어 표현하라"
- 하네스(`l2.py`): items 가 비었는데 수집한 uid 가 있으면 **수집분으로 자동 복구**.
  `note` 는 살려서 생성 단계로 넘긴다 (무엇을 못 찾았는지가 유용한 정보다).

→ 하네스는 "모델이 아무것도 안 냈을 때"뿐 아니라 **"모델이 스스로 버렸을 때"도 복구**해야 한다.
새 단계를 추가할 때 이 원칙을 같이 적용한다.
