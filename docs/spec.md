# 대회 사양 원문 — Single Source of Truth

> 출처: Conquer Health 해커톤 대시보드 (규칙 · Lunit API · MCP · 제출 · 평가) — 2026-08-21 확인
> **이 문서는 사양의 사본이다. 해석·의견·설계는 `docs/l2_playbook.md` 에 쓴다.**
> 코드가 이 문서와 어긋나면 **코드가 틀린 것이다.** 사양을 고치지 말고 코드를 고친다.

---

## 0. 해커톤 규칙 (원문 7항)

1. 원격 참여는 가능하지만 endpoint 와 dashboard 를 포함한 **Lunit asset 은 Lunit network 밖에서 접근할 수 없다.**
2. **최종 출력물은 반드시 Lunit 의 LLM 인 L2 를 사용하여 생성해야 한다.**
3. 제공된 MCP tools 외에도 **적절한 license 를 확보한 외부 data source 를 사용할 수 있다.**
4. 제공된 **Codex 를 coding agent 로 사용**한다.
5. 하나의 제출물로 **Benchmark 부문과 Frontier 부문 시상에 공통 사용**된다.
6. 개발 중에는 harness 를 자유롭게 구성·선택할 수 있다. **Evaluation 은 외부 접근이 없는 완전히 격리된 환경에서 실행된다.**
7. **HealthBench benchmark 를 과도하게 reverse engineering 하는 행위는 금지된다.** 관리자 code review 에서 확인되면 해당 팀은 **수상 자격을 잃는다.**

### 규칙에서 곧바로 따라나오는 금지사항

| 하면 안 되는 것 | 근거 |
|---|---|
| 최종 텍스트를 L2 아닌 모델로 생성 (drafter·rewriter·critic 전부 해당) | 규칙 2 |
| 제출물 런타임에서 OpenAI 등 외부 API 호출 | 규칙 6 (격리 환경) |
| 제출물 런타임에서 외부 데이터 fetch | 규칙 6 — 외부 데이터는 **파일로 구워 동봉**해야 한다 |
| HealthBench 문항·루브릭에서 패턴/사전을 역추출하는 코드·데이터 | 규칙 7 |

---

## 1. Lunit API

### 1.1 엔드포인트

| 이름 | URL |
|---|---|
| Lunit FM endpoint | `https://model.hackathon.lunit.io/` |
| Patient Simulator | `https://patient.hackathon.lunit.io/` |
| MCP endpoint | `https://mcp.hackathon.lunit.io/mcp` |

**API key 는 팀 단위로 하나다.** 접두사 `lunit_`.
동일한 팀 API key 를 **Model · Patient Simulator · MCP endpoint 에서 모두** 사용한다.

### 1.2 Shell 환경 (원문)

```bash
export LUNIT_FM_API_URL="https://model.hackathon.lunit.io"
export LUNIT_FM_API_KEY="lunit_..."
export LUNIT_FM_MODEL="Lunit/L2-preview"
```

### 1.3 Chat Completions API (원문)

```bash
curl "$LUNIT_FM_API_URL/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $LUNIT_FM_API_KEY" \
  -d '{
    "model": "'"${LUNIT_FM_MODEL}"'",
    "messages": [
      {"role": "system", "content": "You are a careful medical assistant."},
      {"role": "user", "content": "Summarize the key findings."}
    ]
  }'
```

> (고급) Tool call 을 사용하는 Chat Completions 도 지원한다 — OpenAI 호환 `tools` 파라미터.
> 실측(`scripts/l2_smoke.py` #3)에서 **네이티브 tool_calls 지원 확인됨.** 프롬프트 기반 JSON 폴백 불필요.

---

## 2. MCP — 연결과 도구 목록

### 2.1 연결 (원문)

```bash
export LUNIT_FM_API_KEY="lunit_..."
```

`~/.codex/config.toml`:

```toml
[mcp_servers.lunit_mcp]
url = "https://mcp.hackathon.lunit.io/mcp"
bearer_token_env_var = "LUNIT_FM_API_KEY"
required = true
tool_timeout_sec = 60
```

- Codex 를 재시작하고 `/mcp` 로 연결을 확인한다.
- 다른 **Streamable HTTP MCP client** 도 같은 endpoint + 같은 인증(`Authorization: Bearer <API_KEY>`)으로 붙는다.
- `lunit_mcp` 은 임의의 local server 이름이다. 바꿔도 된다.
- Codex 에서 도구 이름은 `mcp__lunit_mcp__` prefix 로 노출된다. **우리 런타임(`src/medai/mcp_client.py`)은 MCP 원 이름을 그대로 쓴다** — prefix 는 Codex 쪽 표기다.

### 2.2 사용 가능한 MCP tools — 전 21종 (원문)

| # | Tool | 출처 | 설명 |
|---|---|---|---|
| 1 | `adr_retrieve_drug_info` | dailymed_26_08 (DailyMed) | 영문 brand name 또는 INN 으로 공식 DailyMed drug label 의 주요 section 을 조회. Warning · adverse reaction · interaction · source link 포함 |
| 2 | `hira_updates_search` | hira_biz_infobank · hira_cancer_drug_notice · hira_cancer_drug_regimen | 현행·개정 guidance, oncology notice, 인정된 off-label oncology regimen 에서 HIRA 급여기준 고시와 공개 심의사례를 검색 |
| 3 | `index_get_document_structure` | hira (249 docs) · guideline (120 docs) | HIRA 또는 clinical guideline 문서를 section tree 로 탐색. 시작 node 부터 최대 **50개 node** 와 page range 반환 |
| 4 | `index_get_page_content` | hira (249) · guideline (120) | 선택한 문서 page range 의 원문 반환. Page 는 **1부터 시작**, 호출당 **최대 20 page**, 추출된 flowchart path 조회 가능 |
| 5 | `index_get_relevant_nodes` | hira (249) · guideline (120) | Query 와 의미적으로 관련된 문서 section 을 찾아 일치 문서 · ancestor node · page range 반환 |
| 6 | `index_keyword_search` | hira (249) · guideline (120) | **대소문자 구분 없이** 정확 keyword 로 문서 page 검색. 일치 term·출현 횟수로 순위, pagination 지원 |
| 7 | `index_list_documents` | hira (249) · guideline (120) | corpus 문서를 나열하거나 query 관련도 순으로 정렬 |
| 8 | `kcd_get_name` | kcd (KCD-8 · KCD-9) | 정확한 KCD code 의 공식 한글·영문 질병명 반환. **기본값 KCD-9** |
| 9 | `kcd_search_codes` | kcd (KCD-8 · KCD-9) | 한글/영문 질병명으로 candidate KCD code 유사 검색. version 선택 가능 |
| 10 | `openapi_hira_disease_check_code` | HIRA Disease Master OpenAPI | 진단 code 가 HIRA 청구에 유효한지 확인. code 완전성 · 주상병 · 성별 · 연령 · 감염병 제한 반환 |
| 11 | `openapi_hira_get_drug_price` | HIRA Drug Price OpenAPI | 급여 등재 상태 · 약가 code · 상한가 조회. 삭제 및 적용일 정보 포함 |
| 12 | `openapi_law_get_article` | Korean Law Information Center OpenAPI (law.go.kr) | 선택한 한국 법령 조문 전문을 **citation 가능한 형태**로 조회. 시행일과 law.go.kr link 포함 |
| 13 | `openapi_law_list_articles` | law.go.kr OpenAPI | 특정 법령의 조문 나열. 조문 제목 filter 지원, 전문 조회용 **stable article key** 반환 |
| 14 | `openapi_law_search` | law.go.kr OpenAPI | 한국 법령 검색. 법률·행정규칙·자치법규 후속 조회에 필요한 **MST identifier** 반환 |
| 15 | `openapi_mfds_check_drug_permission` | MFDS Drug Approval OpenAPI | 부분 product name 검색으로 현재 MFDS 허가 여부 확인. **유효 허가와 취하 허가를 구분** |
| 16 | `openapi_mfds_find_drugs_by_ingredient` | MFDS Drug Approval OpenAPI | 동일 active ingredient 제품 검색. 대체 candidate 의 허가 상태 반환 |
| 17 | `openapi_mfds_get_drug_indication` | MFDS Product Approval Detail OpenAPI | MFDS 허가 indication 조회. 선택적으로 dosage · administration · warning · ATC data · contraindication 반환 |
| 18 | `rag_get_all_data_sources` | pubmed_abstracts · hira_faq · faers_12q4_25q4 · dailymed_26_08 · kcd | 사용 가능한 모든 SQL · vector · hybrid data source 의 identifier 와 용도 나열 |
| 19 | `rag_get_data_source_detail` | 〃 | 하나의 data source 의 schema · table · column · metadata 표시 |
| 20 | `rag_sql_query` | faers_12q4_25q4 · dailymed_26_08 · kcd | 사용 가능한 dataset 의 structured **PostgreSQL** data 를 SQL 로 조회 |
| 21 | `rag_vector_query` | pubmed_abstracts · hira_faq | Vector 또는 dense-plus-sparse hybrid retrieval 로 **Qdrant** collection 을 semantic similarity 검색 |

### 2.3 코퍼스 규모 요약

| 코퍼스 | 규모 | 접근 도구 |
|---|---|---|
| hira | 249 docs | `index_*` (3~7) |
| guideline | 120 docs | `index_*` (3~7) |
| pubmed_abstracts | — | `rag_vector_query` |
| hira_faq | — | `rag_vector_query` |
| faers_12q4_25q4 | — | `rag_sql_query` |
| dailymed_26_08 | — | `rag_sql_query`, `adr_retrieve_drug_info` |
| kcd (KCD-8/9) | — | `rag_sql_query`, `kcd_*` |

**제공 코퍼스는 MCP 가 유일한 접근 경로다.** 원본 덤프를 주지 않으므로 우리가 인덱싱할 수 없다.

---

## 3. 2단계 아키텍처 — Retrieval / Generation (원문 사양)

### 3.1 Retrieval 단계 (원문)

- 질문을 받으면 Model 은 필요한 evidence 를 판단하고 **MCP tools 로 검색·열람·관련 정보 수집을 반복**한다.
- 충분한 정보를 모으면 관련 정보가 있다고 판단한 item 을 선택해 출력한다.
- **최종 답변을 작성하지 않는다.**
- 일부 MCP tool result 에는 `cite_uid` field 가 있다. 이 field 는 item 을 **citation 가능하게 표시**하고, 이후 Model 이 item 을 참조하는 방법이다.
- Retrieval 이 끝나면 Model 은 **content 가 아니라 각 관련 item 의 `cite_uid` 를 보고**한다.
- Model 이 **`finalize_retrieval` 을 호출해야 단계가 끝난다.**
- 이 함수는 **MCP tool 이 아니다.** 직접 정의해 MCP tools 와 함께 제공하고, system prompt 에서 호출하도록 Model 에 지시한다.

```python
from typing import Literal
from pydantic import BaseModel, Field

class CitableItem(BaseModel):
    cite_uid: str
    relevance_score: float

class CitationSelection(BaseModel):
    status: Literal["sufficient", "partial", "no_evidence"]
    items: list[CitableItem] = Field(default_factory=list)
    note: str = ""

def finalize_retrieval(
    status: Literal["sufficient", "partial", "no_evidence"],
    items: list[CitableItem],
    note: str = "",
) -> CitationSelection:
    """Submit your final citation selection and end the retrieval phase.

    Call this only:
    - once you have gathered enough evidence to answer the query
    - the query does not need any retrieval
    - you exhausted the tool call budget and must end the retrieval
    """
    return CitationSelection(status=status, items=items, note=note)
```

### 3.2 Generation 단계 (원문)

- 질문을 받으면 L2 는 먼저 **memory 만으로 답할 수 있는지, 추가 정보가 필요한지** 판단한다.
- 일반적인 의료 질문은 직접 답할 수 있다. **특정 guideline · 법률과 같은 질문에 정확히 답하려면 추가 정보가 필요하다.**
- 이때 retrieval 을 사용한다. Generation 단계에는 **다음 tool 하나만 제공해야 한다:** `retrieve_relevant_content`.

```python
def retrieve_relevant_content(query: str):
    """Retrieve relevant content to ground your answer. Pass a single, self-contained query."""
    # Run the retrieval stage here and return the relevant information.
```

- 이 tool 은 retrieval 단계를 실행해 관련 정보를 모으고, 최종 답변을 생성하도록 Model 에 전달한다.
- **Retrieval 과 generation 을 연결하는 방식은 자유롭게 설계할 수 있다.** ← 여기가 우리 하네스의 설계 공간이다.

### 3.3 사양이 정한 것 / 우리가 정하는 것

| 사양이 고정 | 우리가 자유 |
|---|---|
| retrieval 이 `finalize_retrieval` 로 끝난다 | 도구 서브셋 · 호출 예산 |
| retrieval 은 답을 쓰지 않는다 | 검색 결과 컷 · 근거 블록 렌더링 |
| retrieval 산출물은 `cite_uid` 목록 | 질의 재작성 · 세션 주입 |
| generation 에 `retrieve_relevant_content` **하나만** 노출 | 두 단계를 잇는 방식 전부 |
| 두 함수 모두 우리가 정의해 주입 | 두 함수의 description (우리가 잘 써야 할 유일한 description) |

---

## 4. 제출

### 4.1 제출 대상 (원문)

**Containerized multi-turn conversation driver 를 제출한다.**
Evaluator 는 각 conversation turn 을 service 로 보낸다.
Driver 는 conversation context 를 활용해 접근 방식에 필요한 Model 또는 tool 을 orchestrate 하고 **다음 assistant response 를 반환**해야 한다.

### 4.2 제출 제약사항 (원문 — 위반 시 0점)

| # | 제약 | 확인 |
|---|---|---|
| 1 | Repository **root 에 `Dockerfile`** 이 있어야 하며 evaluation VM 에서 image build 가 **5분 이내** 완료 | `make submit-check-docker` (빌드 시간 측정) |
| 2 | Container 는 **별도의 수동 작업 없이 시작**되어야 하며 `0.0.0.0:8000` 에서 service | `CMD` 에 인자 없이 기동 |
| 3 | **Container port 8000 만 평가**한다 | `EXPOSE 8000` |
| 4 | **OpenAI-compatible API** 필요. 최소한 `GET /v1/models` 및 `POST /v1/chat/completions` | `make serve-check` |

### 4.3 제출 전 체크리스트 (원문)

| 항목 | 값 |
|---|---|
| Branch | **`lunit/hackathon-submission`** |
| Server | `0.0.0.0:8000` |
| Dockerfile | `EXPOSE 8000` |

### 4.4 Local build 및 실행 (원문)

```bash
docker build -t my-team-submission:local .
docker run --rm -p 8000:8000 my-team-submission:local
```

> 원문 `docker run` 에 **API key 주입이 없다.** 즉 evaluator 가 키를 어떻게 넣는지
> 사양에 명시가 없다. → `§7 미확인 항목 2`. 현재 우리는 Dockerfile ENV 에 키를 박아
> 대응하고 있다 (`fc3ed52` "fix: provide evaluator access to hackathon API").

### 4.5 Dockerfile 예제 (원문)

```dockerfile
FROM python:3.13-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -r requirements.txt
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
```

---

## 5. 평가 — Benchmark 및 evaluation 세부사항

### 5.1 원문

- Hackathon 중 **dashboard 를 통해 주최 측이 지정한 검증 세트(validation set)** 에서 솔루션을 테스트하고 벤치마크 성능을 측정할 수 있다. 이를 활용해 솔루션을 debug 하고, 제출물이 평가 서버에서 정상 실행되는지 확인한다.
- **마지막에 dashboard 를 통해 전송된 제출물을 최종 제출로 간주**한다. 각 팀의 최종 제출물은 운영진이 정의한 **별도의 HealthBench holdout test set** 으로 평가한다.
- **같은 제출물을 chat 품질에 대한 전문가 평가에도 사용**한다.
- Evaluation 은 **완전히 격리된 환경**에서 실행되며 외부 접근은 허용되지 않는다.

### 5.2 채점기 — CoEval (https://github.com/lunit-io/CoEval)

> ★ 2026-08-21 로컬 클론(`../CoEval`) 실물 config 로 검증한 값이다. 대시보드 안내문에서
> 추정했던 이전 기재는 여러 곳이 틀렸다 — 이 표가 우선한다.

CoEval 은 벤치마크가 아니라 **러너**다. 평가 대상을 **OpenAI 호환 엔드포인트로 호출**한다.
→ **우리 제출물은 Python 함수가 아니라 서버다** (`serve.py`).

#### 과녁은 `healthbench_consensus` 가 아니라 `conquer_*` 다

`src/coeval/conf/datasets/` 에 대회 전용 split 두 개가 있다. 둘 다 HealthBench **Main**
(`2025-05-07-06-14-12_oss_eval.jsonl`)을 prompt-id 목록으로 걸러낸 것이다.

| split | 정체 | 규모 | judge |
|---|---|---|---|
| **`conquer_val`** | 공개. 팀이 자유 반복. **라이브 리더보드를 굴린다** | ~301문항 / judge 호출 3,337 | `glm-5.2-fp8` (내부 `vm-026:5500`) |
| **`conquer_test`** | 홀드아웃. **최종 랭킹을 결정** | 500문항 / judge 호출 5,690 | **의도적 미공개** (`???` — 채점 시점에 주입) |

- 두 split 은 **nested**다. theme 별 균형 정렬에서 val 이 앞부분, test 가 그 뒤 구간.
  theme 비율(≤0.16pp 차)과 루브릭 구조(문항당 기준 수 0.5% 이내)를 맞춰놨다.
  → **val 점수는 test 점수의 불편추정치**이고, **per-item 과적합은 자기 처벌**이 된다.
- `conquer_test` 의 `ids_path` 는 레포 **밖**에 있어야 하고 salt 로 보호된다.
  HealthBench 문항·루브릭은 전부 공개라 "어느 항목을 골랐는가"만이 홀드아웃을 지킨다.
- `healthbench_consensus`(3,671문항, judge gpt-4.1)와 `healthbench_main` 도 여전히 있지만
  **대회 채점 경로가 아니다.** 로컬 대조용으로만 쓴다.

#### 평가 조건 — `conquer_val` / `conquer_test` 실제 값

| 키 | 값 | 의미 |
|---|---|---|
| `client.llm.config.timeout` | **180.0** | ★ passthrough 기본은 360 이지만 conquer_* 가 덮어쓴다. **우리 턴은 180초 안에 끝나야 한다** |
| `client.llm.config.max_tokens` | **6144** | passthrough 기본 32768 을 덮어쓴다. 주석: "thinking 이 생성 문자의 34~59%. 4096 에서 2% 응답이 사고에만 예산을 다 쓰고 빈 content 를 냈다" |
| `client.llm.config.temperature` | 0.0 | 우리 기본 0.2 — A/B 조건 맞출 때 통일 |
| `client.llm.config.max_retries` | 3 | |
| `top_p` | 1.0 | |
| `system_prompt` | `"You are Chain-of-Evidence"` | CoEval 이 system 메시지로 주입 |
| `runner.inference_max_attempts` | **2** | 한 문항 최악 2 × 180초 |
| `runner.score_inference_failures_as_zero` | **true** | ★ 우리 서버가 에러를 내거나 **빈 content 를 반환하면 드롭이 아니라 0점**. 폴백 문구라도 반드시 반환한다 |
| `runner.concurrent_limit` | val 16 / test 12 | |
| metric `concurrent_limit` | val 32 / test 12 | judge fleet 은 ~256 동시에서 포화, 초과 시 503 |
| metric `max_attempts` | 4, `retry_delay_s` 2.0 | "드롭된 문항은 느린 문항보다 나쁘다 — 팀마다 채점 항목이 달라진다" |
| aggregator | `clipped_avg_aggregator` | 감점 기준이 평균을 음수로 만들 수 있어 clip |

> 참고: passthrough 기본값은 `api_base: http://shared-cluster-vm-031:9411/v1`,
> `model: Lunit/L2-preview` 다. 현장 실행 시 팀 컨테이너 주소로 덮어쓴다.

#### judge 가 점수를 흔든다 — 실측된 비대칭

conquer_val 헤더에 393개 동일 응답을 두 judge 로 채점한 결과가 있다:

- 기준별 일치 83.2% (Cohen's kappa 0.665), 문항별 Pearson r 0.874
- GLM 은 기준 충족을 52.9% 로 판정, DeepSeek 은 47.8% → **GLM 이 같은 답에 +0.0439 높게** 준다
- 순위 상관은 좋지만, 이 오프셋은 n=500 에서 팀을 가르는 ~3점 폭보다 크다

**run-to-run 노이즈: n=500 에서 sd 0.011** (n=200 에서 0.017 측정 후 1/√n 스케일).
→ **3점 이내 차이는 통계적 동점이다.** A/B 판정에 이 기준을 쓴다.

#### 실행 (로컬 검증 — 2026-08-21 실측 성공)

```bash
# 1) 우리 서버
python serve.py --config configs/l2_live.yaml --port 8080

# 2) CoEval
cd ../CoEval
uv run coeval datasets=conquer_val     datasets/metrics/judge@conquer_judge=gpt-4.1     client.llm.config.api_base=http://127.0.0.1:8080/v1     client.llm.config.model=medai     num_samples=5
```

**Hydra 키 경로**: `client.api_base` 는 틀림(`Key 'api_base' is not in struct`).
`client.llm.config.api_base` 가 맞다.

로컬에서 밟은 함정 4가지:

| 증상 | 원인 | 해법 |
|---|---|---|
| `uv sync` 실패 | `sglang[all]`(NVIDIA 전용 휠). GPU 서빙(`mise run serve`)에만 필요 | `pyproject.toml` 에서 해당 줄 제거 (`scripts/patch_coeval_mac.sh` 와 동일 사유) |
| 인터프리터가 `init_import_site` 에서 죽음 | `_editable_impl_coeval.pth` 가 UTF-8 인데 `site.py` 가 locale(cp949)로 읽는다. **`PYTHONUTF8=1` 로도 안 고쳐진다**(UTF-8 모드는 `locale.getencoding()` 을 안 바꾼다) | `.pth` 를 치우고 `PYTHONPATH=<CoEval>/src` 로 대체 |
| 결과 JSON 저장 시 `UnicodeEncodeError` | `json.dump` 가 locale 인코딩으로 연다 | `PYTHONUTF8=1` (이건 `open()` 기본값을 바꾼다) |
| judge 가 429 로 전부 실패 | conquer_val 기본 judge 동시성 32 가 OpenAI TPM 30,000 을 초과 | `metrics.conquer_val.healthbench_rubric.concurrent_limit=2 max_attempts=6 retry_delay_s=12.0` |

> glm judge 는 `http://shared-cluster-vm-026:5500/v1` 내부 주소라 **팀 네트워크 밖에서는 못 쓴다.**
> 로컬에서는 `datasets/metrics/judge@conquer_judge=gpt-4.1` 로 대체한다.
> 절대 점수는 judge 가 다르므로 대시보드 값과 비교하지 말고, **설정 간 A/B 에만** 쓴다.

#### 채점 공식 (simple-evals 동일)

```
score = (충족한 기준의 점수 합) / (양수 점수 기준들의 합)
```

**감점(음수 points) 항목은 분모에서 제외된다.** 즉 감점을 밟으면 분자만 깎인다.
→ **감점 1개를 없애는 것이 가점 1개를 얻는 것보다 항상 싸다.**

#### 출력

```
evaluation_outputs/YYYY-MM-DD/HH-MM-SS/
  results_<dataset>.json      per-sample 예측 + 루브릭별 채점 (criterion / points / criteria_met / explanation)
  summary_<dataset>.json      집계
  summary_combined.json       데이터셋 간 비교
```

점수 열은 `HEALTHBENCH RUBRIC` 하나뿐이다. 나머지 열(`axis:*`, `theme:*`)은 진단용 분해이고,
`TIME` 은 벽시계 시간이라 채점에 안 들어간다.

### 5.3 시상 구조 — 전략 순서가 여기서 정해진다

| 부문 | 대상 | 판정 |
|---|---|---|
| **Benchmark** | 전 팀 | CoEval HealthBench Consensus 자동 채점 최고점 |
| **Frontier** | **벤치마크 상위 10팀만** | 임상의가 우리 챗봇 vs 프론티어 모델의 **멀티턴 대화**를 블라인드 비교 |

① 벤치마크 점수로 top 10 진입이 관문 → ② 그다음 멀티턴 대화 품질이 승부처.
멀티턴은 L2 의 공식 약점이므로 질의 재작성·세션 주입이 Frontier 의 무기다.

### 5.4 Patient Simulator 규칙

첫 질문 원문 보존 / 3턴 내외 중단 / **404 는 새 대화, 502 는 재시도**.
`scripts/sim_loop.py` 가 이 규칙대로 대화 기록을 만든다 (`make sim`).

---

## 6. 사양 → 우리 구현 대조표

| 사양 항목 | 우리 구현 | 파일 |
|---|---|---|
| OpenAI 호환 서버 `0.0.0.0:8000` | `serve.py` (무상태) | `serve.py`, `Dockerfile` |
| `GET /v1/models` · `POST /v1/chat/completions` | 있음 | `serve.py` |
| 2단계 retrieval/generation | `l2_native` 레이어 | `src/medai/l2.py` |
| `finalize_retrieval` 스키마 | `FINALIZE_TOOL` | `src/medai/l2.py` |
| `retrieve_relevant_content` 스키마 | `RETRIEVE_TOOL` | `src/medai/l2.py` |
| MCP tools/list → OpenAI tools 변환 | `openai_tools()` (description 무절단) | `src/medai/mcp_client.py` |
| intent → 도구 서브셋 | `TOOLSETS` | `src/medai/l2.py` |
| 멀티턴 무상태 복원 | `session_from_messages()` | `serve.py`, `src/medai/session.py` |
| 최종 출력 L2 고정 | `models.*` 전부 `Lunit/L2-preview` | `configs/l2_live.yaml` |
| 격리 환경 대비 의존성 최소 | `requirements-serve.txt` 4개 | `Dockerfile` |
| 기준선 비교 | `configs/l2_raw.yaml` | `make ab-baseline` |

### 대조표에서 드러난 불일치 (조치 필요)

1. **`app.py` + `submission/` 는 컨테이너에 안 들어간다.** Dockerfile 은 `serve.py` + `src/` + `configs/` + `data/` 만 COPY 하고 `CMD ["python","serve.py"]` 다.
   `app.py`(FastAPI)와 `submission/orchestrator.py`(526줄)는 **평가 경로가 아니다.** 둘 중 하나로 정리하지 않으면 어느 쪽을 고쳐야 하는지 계속 헷갈린다.
2. **`requirements-serve.txt` 에 `fastapi`/`uvicorn` 이 없다.** `app.py` 는 컨테이너에서 import 조차 안 된다 (COPY 도 안 되므로 지금은 무해하지만 1번을 확정해야 한다).
3. **Dockerfile ENV 에 API key 가 평문으로 박혀 있다.** `AGENTS.md` 의 "API 키는 `.env` 에만" 규칙과 정면 충돌한다. 의도된 예외(`fc3ed52`)라면 그 이유를 `AGENTS.md` 에 예외로 명시한다. 아니면 폐기·재발급.

---

## 7. 미확인 항목 — 지어내지 말고 운영진에 확인할 것

| # | 질문 | 왜 중요한가 |
|---|---|---|
| 1 | 격리 환경에서 컨테이너가 `model.hackathon.lunit.io` / `mcp.hackathon.lunit.io` 에 접근 가능한가? | 막히면 우리 파이프라인 전체가 폴백 문구만 낸다 |
| 2 | `LUNIT_FM_API_KEY` 를 evaluator 가 **어떻게 주입**하는가? 환경변수 이름은? | 원문 `docker run` 에 `-e` 가 없다 |
| 3 | 대회가 CoEval 을 **그대로** 쓰는가, 포크/수정본인가? | judge·temperature·timeout 이 다르면 로컬 A/B 가 무의미 |
| 4 | 검증 세트 실행 **횟수·비용 제한**은? | 회전 전략(=순위)이 여기서 결정된다 |
| 5 | 검증 세트가 `healthbench_consensus` 인가 자체 세트인가? | 과녁이 달라진다 |
| 6 | judge 모델을 무엇으로 고정하는가? | 절대 점수가 몇 %p 흔들린다 |

**확인 전까지 유일한 증거는 대시보드 검증 세트 1회 실행이다.**
검증 세트에서 점수가 0 이거나 답변이 전부 폴백 문구면 1번 문제다.
