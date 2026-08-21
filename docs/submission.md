# 제출 절차

저장소: https://github.com/hackathon-aim/health-conquer-submission
평가 대상 브랜치: **`lunit/hackathon-submission`** (이 이름이어야 한다. 평가가 이 브랜치의
40자리 SHA 를 찾는다. `jiwoo` 같은 개인 브랜치는 작업용으로만 쓰고 여기로 머지한다.)

## 규정 → 우리 구현

| 규정 | 구현 | 확인 |
|---|---|---|
| repo root 에 `Dockerfile` | 있음 | `make submit-check` |
| build 5분 이내 | `requirements-serve.txt` (4개 패키지) — torch·FlagEmbedding 제외 | 아래 §2 에서 실측 |
| 수동 작업 없이 기동 | `CMD ["python","serve.py"]`, 포트·설정은 ENV | 아래 §2 |
| `0.0.0.0:8000` | `serve.py` 기본값 (`PORT`/`HOST` 환경변수) | 확인됨 |
| `GET /v1/models` | 있음 | 확인됨 |
| `POST /v1/chat/completions` | 있음, 무상태 멀티턴 | 확인됨 |
| 최종 출력은 L2 | 전 역할 `Lunit/L2-preview` | `submit_check.py` 가 외부 모델 문자열까지 검사 |

## 1. 브랜치 세팅 (최초 1회)

```bash
cd /Users/ziuuu/Documents/med_ai
git remote -v                    # origin 이 health-conquer-submission 인지
# 없으면:
git remote add origin https://github.com/hackathon-aim/health-conquer-submission.git

git checkout -b lunit/hackathon-submission
```

⚠️ **`.env` 가 커밋되지 않는지 반드시 확인.** `git ls-files .env` 가 비어야 한다.
`submit_check.py` 가 이것도 검사한다. 키가 올라가면 즉시 폐기하고 재발급해야 한다.

## 2. 제출 전 점검 (매번)

```bash
make submit-check          # 정적 점검
make submit-check-docker   # 도커 빌드 + 기동 + 엔드포인트 (권장, 빌드 시간도 잰다)
```

수동으로 하려면:

```bash
docker build -t medai:local .
docker run --rm -p 8000:8000 -e LUNIT_FM_API_KEY=lunit_... medai:local
curl localhost:8000/v1/models
curl localhost:8000/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"medai","messages":[{"role":"user","content":"머리가 아파요"}]}'
```

## 3. 커밋 · 푸시 · SHA 제출

```bash
git add -A && git commit -m "L2 2단계 하네스 + MCP 서브셋 + 안전 게이트"
git push -u origin lunit/hackathon-submission
git rev-parse HEAD          # ← 이 40자리를 대시보드에 붙여넣는다
```

대시보드 입력값:
- SHA: 위 40자리
- Model 이름: `Lunit/L2-preview`

## 4. 격리 환경에서 확인해야 할 것 ★ 미해결

규정에 "Evaluation 은 완전히 격리된 환경에서 실행되며 외부 접근은 허용되지 않습니다"
라고 되어 있는데, **우리 컨테이너는 두 곳을 호출해야 한다**:

- `https://model.hackathon.lunit.io` (L2)
- `https://mcp.hackathon.lunit.io` (RAG 도구)

이 둘이 "외부"에 해당하면 우리 파이프라인은 평가 환경에서 아무것도 못 한다.
상식적으로는 Lunit 내부망만 열고 그 외 인터넷을 막는다는 뜻이겠지만,
**추측으로 두면 안 된다.** 운영진에게 두 가지를 확인할 것:

1. 컨테이너에서 model / mcp 엔드포인트에 접근 가능한가?
2. `LUNIT_FM_API_KEY` 는 어떻게 주입되는가? (환경변수 이름은? 우리는 `LUNIT_FM_API_KEY`
   를 읽는다. 다른 이름이면 `configs/l2_live.yaml` 의 `api_key_env` 를 바꾼다.)

확인 전까지는 **대시보드 검증 세트로 한 번 돌려보는 것이 유일한 증거**다.
검증 세트에서 점수가 0 이거나 답변이 전부 폴백 문구면 이 문제다.

## 5. 제출 후에도 바뀌는 것

마지막에 대시보드로 보낸 제출물이 최종이다. 그러니 **일단 지금 돌아가는 상태로 한 번
제출해 두고** 개선분을 계속 밀어 넣는 게 안전하다. 빈손으로 마감을 맞는 것이 최악이다.
