# med_ai — Claude Code 진입 계약

Conquer Health 해커톤 제출물. 대국민 건강상담 챗봇.
**채점: CoEval(lunit-io/CoEval) → HealthBench Consensus 루브릭, judge = gpt-4.1.**

## 먼저 읽을 것 (순서 고정)

| # | 문서 | 언제 |
|---|---|---|
| 1 | **`docs/spec.md`** | **항상.** 대회 사양 원문 SSOT — 엔드포인트·MCP 21종·2단계 계약·제출 제약·CoEval 설정 |
| 2 | `docs/l2_playbook.md` | 설계 근거·실측값·예산 상수가 필요할 때 |
| 3 | `docs/submission.md` | 제출·브랜치·SHA 절차 |
| 4 | `.claude/harness.md` | 에이전트 팀 구성 근거 |

**규칙: 사양에 관한 사실은 `docs/spec.md` 에서 읽는다. 기억이나 추론으로 답하지 않는다.**
엔드포인트 URL, MCP 도구 이름·인자, 페이지 상한, 포트, 브랜치명, CoEval 키 경로가 여기 해당한다.

## 절대 규칙 (어기면 수상 자격 또는 0점)

1. **최종 출력은 반드시 L2** (`Lunit/L2-preview`). 최종 텍스트를 만지는 모든 호출 — drafter · rewriter · critic — 이 해당한다.
2. **HealthBench 역공학 금지.** 평가 문항·루브릭에서 패턴이나 사전을 역추출하는 코드·데이터를 만들지 않는다. 관리자 code review 로 확인되면 수상 자격 박탈이다.
3. **평가는 격리 환경.** 제출물 런타임에서 외부 API 호출·외부 데이터 fetch 금지. 외부 데이터는 파일로 구워 동봉한다.
4. **제출 규격은 협상 불가**: repo root `Dockerfile` · 빌드 5분 이내 · 수동 작업 없이 기동 · `0.0.0.0:8000` · `GET /v1/models` · `POST /v1/chat/completions` · 브랜치 `lunit/hackathon-submission`.
5. **변경은 한 번에 하나**, config 스위치(기본 `False`)와 함께. A/B 없이 채택하지 않는다.

## 채점 구조가 우선순위를 정한다

```
score = (충족한 기준의 점수 합) / (양수 점수 기준들의 합)
```

**감점 항목은 분모에 없다.** 가점을 더 얻어 감점을 상쇄할 수 없다.
→ **감점 1개 제거 > 가점 1개 획득.** 언제나. 우선순위 표를 이 순서로 만든다.

`clipped_avg_aggregator` 가 평균을 clip 하므로 안전 회귀는 순수 손실이다.

## 평가 경로 — 무엇이 실제로 평가되는가

**제출물은 함수가 아니라 서버다.** CoEval 이 OpenAI 호환 엔드포인트로 호출한다.

```
Dockerfile → CMD ["python","serve.py"] → serve.py (무상태)
  → src/medai/pipeline.py
     L1 레드플래그(결정론) → L2 분류 → L1b DUR
     → L4 = src/medai/l2.py (2단계: MCP 검색 → 근거 기반 생성)
     → L4b 출력 게이트 → L4c 비평(l2_live 에서 off)
```

- `serve.py` 는 **무상태**다. 매 요청에 전체 messages 가 오고 `session_from_messages()` 가 규칙 추출을 재생해 세션을 복원한다. 세션 의존 기능을 추가하면 이 재생 경로도 같이 고쳐야 한다.
- **`app.py` 와 `submission/` 는 컨테이너에 들어가지 않는다** (Dockerfile 이 COPY 하지 않음). 평가 경로가 아니다. 여기를 고치면 점수에 아무 영향이 없다. → `docs/spec.md §6` 참조.
- L2 경로에서는 `src/medai/sources/*` 와 `rerank.py` 를 쓰지 않는다. 검색 주체가 L2 자신이다.

## 이 머신에서 실행하기 (Windows)

`make` 와 `mise` 가 없다. **`python scripts/mk.py <target>`** 이 Makefile 타깃을 대신한다.

```powershell
python scripts/mk.py list          # 타깃 목록
python scripts/mk.py test          # 단위 테스트
python scripts/mk.py audit         # 배선 감사
python scripts/mk.py serve-l2      # 제출물 서버 (configs/l2_live.yaml)
python scripts/mk.py serve-check   # /v1/models · /v1/chat/completions 스키마
python scripts/mk.py probe         # MCP 실측 (현장 1순위)
python scripts/mk.py l2-smoke      # L2 전 구간 스모크
python scripts/mk.py ab-baseline   # l2_raw(기준선) vs l2_live(우리)
python scripts/mk.py submit-check  # 제출 규격 정적 점검
```

가상환경은 `.venv` (uv 로 생성). `mk.py` 가 자동으로 `.venv/Scripts/python.exe` 를 쓴다.
문서에 `make X` 라고 쓰여 있으면 `python scripts/mk.py X` 로 읽는다.

## 에이전트 팀

| 에이전트 | 하는 일 | 절대 안 하는 일 |
|---|---|---|
| `analyst` | 실패 군집화, 원인 레이어 특정, 우선순위 표 | 코드 수정 |
| `builder` | finding 하나를 최소 diff 로 구현 + 스위치 | 자기 변경 채택 결정 |
| `qa` | A/B 측정, 골든 시나리오, 되돌림 판정 | 원인 분석 |

**`qa` 만 "되돌려라" 라고 말할 수 있다.** 한 사이클: analyst → builder(1건) → qa.

스킬: `spec`(사양 조회·제출 게이트) · `analyze`(진단) · `build`(수정) · `verify`(검증).

## 기준선

넘어야 할 선은 **`configs/l2_raw.yaml`** — L2 권장 2단계 사용법 그대로, 우리 레이어 없이.
우리 파이프라인이 이걸 못 넘으면 레이어가 노이즈를 넣고 있는 것이고, finding 을 고칠 게
아니라 **레이어를 꺼야 한다**. `layers.*` 를 하나씩 `False` 로 바꿔 범인을 찾는다.

> 옛 문서에 나오는 "raw 93.5%" 는 **L1-16B-A3B** 이야기이고 지금 기준선이 아니다. 인용하지 않는다.

## 커밋 전

```powershell
python scripts/mk.py test
python scripts/mk.py audit
python scripts/mk.py submit-check
```

`.env` 가 커밋되지 않는지 확인한다 (`git ls-files .env` 가 비어야 한다).
