# 레이어 지도 — 증상에서 파일까지

## 실행 순서 (`src/medai/pipeline.py`)

```
L1   레드플래그 + 엔티티 규칙 추출     결정론    ~0ms
L2   의도 분류 + 소스 라우팅           LLM 1회   1~2s
L3   RAG 검색  ┐ 같은 asyncio.gather   병렬      1~3s
L1b  입력측 DUR ┘                                ~0.5s
L4   초안 생성 + 응급 prefix 강제      LLM       3~6s
L4b  출력측 안전 게이트 (재작성 포함)  결정론+LLM ~0.5s
L4c  루브릭 비평 패스                  LLM       2~3s
```

지연 예산은 설계 목표이지 대회 규정이 아니다.
CoEval client 기본 `timeout: 360.0`, `max_retries: 3` 이 발견된 유일한 실제 시간 제약이다.
레이턴시가 중요한 이유는 점수가 아니라 **하루에 실험을 몇 번 돌리느냐**다.

## 파일별 책임

| 파일 | 책임 | 건드릴 때 주의 |
|---|---|---|
| `contracts.py` | 레이어 간 pydantic 계약 | Optional 로만 추가. Py3.9 호환 |
| `config.py` | DEFAULTS + yaml 병합 + .env 로딩 | 새 동작은 False 기본값 |
| `llm.py` | OpenAI 호환 클라이언트, 역할별 모델 라우팅, `strip_think`, JSON 재시도 | timeout 명시 필수 |
| `redflag.py` | L1 응급 탐지 | 정규식 부분문자열 |
| `entities.py` | L1 약물/위험인자 추출, `key()` 정규화, 규칙∪LLM 병합 | 제형 접미사 제거하되 제품 구분자는 보존 |
| `classify.py` | L2 의도 분류 | 프롬프트는 `prompts/classifier.txt` |
| `router.py` | 의도 → 소스 선택 | `routing.max_sources` |
| `sources/*.py` | L3 검색 어댑터 (guideline/drug/law/pubmed/mock) | `ENDPOINT` 미설정이면 mock |
| `rerank.py` | L3 리랭킹 + 컨텍스트 조립 | `preload()` 를 init 에서 |
| `gates/dur.py` | L1b/L4b DUR 판정, dedupe, severity 분리 | 8종 점검 항목. 술·음식·장기기능은 DUR 밖 |
| `gates/risk.py` | 성분별 주의사항 규칙 | 출처는 허가사항 |
| `generate.py` | L4 초안, `emergency_prefix`, `output_gate`, `rewrite`, `strip_drug_recommendation` | critical 은 삽입이 아니라 재작성 |
| `critic.py` | L4c 루브릭 비평 루프 | `strict_items` 만 강제, 스타일은 confidence 필터 |
| `session.py` | 세션 상태 갱신 | `update()` 는 턴 **끝**에 돈다 |
| `graph.py` | LangGraph 래퍼 (선택) | `pipeline.py` 만으로 완전 동작 |
| `serve.py` | 제출물. OpenAI 호환 서버, 무상태 세션 재생 | stdlib 만 사용 |

## 세션 상태의 두 필드를 헷갈리지 않는다

```python
medication_names   # 실제 복용 중인 약    → DUR 점검 대상
asked_about        # 물어보기만 한 약     → 점검 대상 아님
```

"타이레놀 먹어도 되나요" 는 `asked_about` 이다. 이걸 `medication_names` 에 넣으면
복용 중이 아닌 약으로 병용금기 경고가 나간다.

## 응급 처리는 우리 가설이다

`emergency.sources: []` (검색 우회)는 HealthBench 가 규정한 게 아니다.
근거가 있는 건 배치 점수뿐이다: 응급 안내가 앞이면 가점, 뒤에 묻히면 감점.
검색 우회가 이득인지는 `make ab-emergency` 로 측정한다.

## 데이터 파일 확장 경로

| 파일 | 현재 | 확장 방법 |
|---|---|---|
| `data/drug_map.json` | 시드 수준 | `make dicts` — 식약처 의약품목록 CSV |
| `data/redflags.yaml` | 시드 수준 (WARN) | `make redflags` — HealthBench emergency 테마 역추출 + 검증 |
| `data/risk_rules.yaml` | 6개 성분 | 허가사항 '사용상의 주의사항' 파싱 |

`make redflags-validate` 가 corpus_recall / korean_emergency_recall /
benign_false_positives 를 잰다. 오탐률이 오르면 채택하지 않는다.
