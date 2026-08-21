# health-conquer-submission

Conquer Health 2026 · 팀 **AIM** 제출물 — 컨테이너화된 멀티턴 대화 드라이버.

> 준비 저장소(`conquer-health-2026-juhee`)와 별개다. 여기는 **제출되는 것만** 둔다.
> 실험·벤치마크·문서는 준비 저장소에 남긴다.

## 이 저장소가 지켜야 하는 계약

대시보드 `/submission` 이 못박은 것들이다. 하나라도 어기면 평가가 실패한다.

| 항목 | 값 |
|---|---|
| 제출 브랜치 | **`lunit/hackathon-submission`** (이 브랜치에서만 받는다) |
| 서버 | **`0.0.0.0:8000`** — 포트 8000만 평가한다 |
| 필수 엔드포인트 | `GET /v1/models`, `POST /v1/chat/completions` |
| Dockerfile | 루트에 있어야 하고, **빌드 5분 이내** |
| 기동 | 별도 수동 작업 없이 떠야 한다 |

제출은 이 브랜치 HEAD 의 **40자리 전체 SHA** 를 대시보드에 입력하는 방식이다.
**마지막 제출이 최종 제출**이다.

## 실행 모드

기본은 **기준선(baseline)** 이다. 받은 대화를 손대지 않고 L2 에 그대로 넘기고
그 출력만 돌려준다 — 라우터도 retrieval 도 타지 않는다.
하네스 기여분을 재는 대조군이자, 하네스가 깨졌을 때 되돌아올 바닥이다.

```bash
BASELINE=1   # 기본 — L2 출력 그대로
BASELINE=0   # 2단계 하네스 (라우터 → retrieval → generation)
```

## 구조

```
app.py             OpenAI 호환 서버. run_baseline() / run_harness() 로 갈린다.
Dockerfile         python:3.13-slim + uvicorn
requirements.txt   fastapi / uvicorn / httpx
.dockerignore      빌드 컨텍스트 부풀지 않게. 5분 제한 때문에 중요하다.
```

## 로컬에서 돌리기

```bash
cp .env.example .env    # LUNIT_FM_API_KEY 를 채운다
```

```bash
docker build -t aim-submission:local .
```

```bash
docker run --rm -p 8000:8000 --env-file .env aim-submission:local
```

```bash
curl -s localhost:8000/v1/models
```

```bash
curl -s localhost:8000/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"고혈압 환자가 주의할 생활습관은?"}]}'
```

## 알아둘 것 — 실측에서 나온 함정

- **`max_tokens` 상한이 2048 이다.** 넘기면 `400 output_limit_exceeded`. 늘릴 방법이 없다.
- **L2 는 사고과정을 별도 `reasoning` 필드로 뱉는다** (`<think>` 태그가 아니다).
  그리고 그게 2048 예산을 통째로 먹어서 답변이 잘리고, 심하면 **`content` 가 빈 문자열로 온다.**
  같은 질문 실측:

  | | reasoning | content | finish_reason | completion_tokens |
  |---|---|---|---|---|
  | thinking on | 2,492자 | 881자 | `length` (잘림) | 2048 |
  | **thinking off** | 0자 | 576자 | **`stop`** (완결) | 395 |

  그래서 기본값을 **thinking off** 로 뒀다 (`chat_template_kwargs.enable_thinking=false`).
  켜보려면 `FM_THINKING=1`. `app.py` 는 빈 응답을 감지해 한 번 더 시도한다.
- **L2 는 single-turn 최적화인데 평가는 멀티턴이다.** 히스토리를 날것으로 넘기면 안 된다.
  query rewriting / context summarization 이 `generate_reply()` 안에 들어가야 한다.
- **평가는 외부 접근이 차단된 격리 환경**에서 돈다. 제공 엔드포인트 외에는 실행 시점에 아무것도 부르면 안 된다.
- **미확인**: 격리 환경에서 `LUNIT_FM_API_KEY` 를 어떻게 주입받는지 운영진 확인 필요.
  키를 이미지에 넣을 수는 없다.

## 다음 작업

`generate_reply()` 를 2단계 하네스로 교체한다.

1. **retrieval** — MCP 도구(`https://mcp.hackathon.lunit.io/mcp`, 21개) + 직접 정의한
   `finalize_retrieval` 만 주고, 답변이 아니라 `cite_uid` 를 수집시킨다.
   `index_get_relevant_nodes` 로 위치를 찾고 `index_get_page_content` 로 본문+`cite_uid` 를 받는 2단 구조.
2. **generation** — `retrieve_relevant_content` 하나만 주고 최종 답변을 만든다.

배경과 근거는 준비 저장소의 `14_당일_대시보드_수집.md` 에 있다.
