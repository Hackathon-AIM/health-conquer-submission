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

## 구조

```
app.py             OpenAI 호환 FastAPI 서버
submission/        L2 2단계 오케스트레이터, Model/MCP client, 응급 게이트
tests/             외부 API 없이 실행되는 단위 테스트
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

## 구현된 하네스

1. **Generation** — L2에는 `retrieve_relevant_content` 하나만 노출한다. 일반 의료 질문은
   바로 답하고 최신 가이드라인·법률·급여·허가 근거가 필요한 경우에만 검색한다.
2. **Retrieval** — 별도 L2 호출에 MCP 도구와 로컬 `finalize_retrieval`만 노출한다.
   최대 6단계·5회 도구 호출로 제한하며 실제 MCP 결과에서 본 `cite_uid`만 통과시킨다.
3. **Multi-turn** — 최근 user/assistant 대화 최대 20개를 보존하고, retrieval query가
   대화 밖에서도 완결되도록 retrieval prompt에서 지시한다.
4. **Fail-soft** — 모델·MCP 장애가 대화 API의 5xx나 빈 문자열로 번지지 않게 안전한 응답을 반환한다.
5. **Safety** — 명확한 응급 신호는 외부 모델을 호출하기 전에 119/응급실 행동 지침으로 단락한다.

테스트:

```bash
python -m unittest discover -s tests -v
```

## 터미널에서 평가 흐름 확인

API key를 파일에 저장하지 않고 숨김 프롬프트로 입력할 수 있다. 스크립트가 로컬 서버를
자동으로 띄우고 종료할 때 함께 내린다.

직접 환자 역할을 하며 멀티턴 대화:

```bash
python scripts/lunit_demo.py manual
```

터미널 안에서는 `/history`, `/json`, `/reset`, `/quit`을 사용할 수 있고, 여러 줄 질문은
`/paste` 입력 후 마지막 줄에 `.`을 입력해 전송한다.

공식 Patient Simulator와 자동 3턴 대화:

```bash
python scripts/lunit_demo.py patient
```

대화 결과를 JSON으로 보존하려면 경로를 명시한다. `runs/`는 Git과 Docker에서 제외된다.

```bash
python scripts/lunit_demo.py patient --turns 3 --save runs/patient-3turn.json
```

## Docker 웹 채팅

API key를 숨김 입력으로 받아 이미지를 빌드하고, 컨테이너 기동 후 브라우저까지 자동으로 연다.

```bash
python scripts/run_web_demo.py
```

웹 화면에서는 멀티턴 채팅, 응답 시간, evaluator history 원본 JSON, 대화 JSON 다운로드를
확인할 수 있다. 기본 주소는 `http://127.0.0.1:18080`이다. 종료할 때 실행 터미널에서
`Ctrl+C`를 누르면 컨테이너도 제거된다.

## 고정 프로브 회귀 테스트

웹 Docker가 실행 중일 때 별도 터미널에서 8개 고정 프로브를 동일한 순서로 실행한다.
각 프로브는 질문뿐 아니라 불합격 조건도 `evals/probes.json`에 고정되어 있다.

```bash
python scripts/run_probes.py
```

결과는 기본적으로 `runs/probes-YYYYMMDD-HHMMSS.json`에 전체 대화와 응답시간을 포함해
저장된다. 이전 실행과 답변 diff를 보려면 다음처럼 지정한다.

```bash
python scripts/run_probes.py --baseline runs/probes-이전시각.json
```
