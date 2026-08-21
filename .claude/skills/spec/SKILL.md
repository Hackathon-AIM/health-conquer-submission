---
name: spec
description: 대회 사양(엔드포인트·MCP 도구·2단계 계약·제출 제약·CoEval 설정)을 확인하고 제출 규격 위반을 잡는 절차. 도구 이름·인자·URL·포트·브랜치·예산 상수가 필요할 때, 그리고 커밋·제출 직전에 사용. 기억으로 답하지 말고 이 스킬을 먼저 돌린다.
---

# 사양 확인 · 제출 게이트

## 0. 원칙

**사양에 관한 사실은 `docs/spec.md` 에서 읽는다. 추론하지 않는다.**

해커톤에서 가장 비싼 실수는 점수가 낮은 게 아니라 **규격 위반으로 0점**이 되는 것이다.
그 다음으로 비싼 것은 사양을 잘못 기억해 하루를 태우는 것이다 (예: `client.api_base` 로
CoEval 을 돌리다 `Key 'api_base' is not in struct` 에서 막히는 것 — 맞는 경로는
`client.llm.config.api_base` 다).

| 알고 싶은 것 | 어디 |
|---|---|
| 엔드포인트 URL · API key · 모델 이름 | `docs/spec.md §1` |
| MCP 도구 21종 이름·출처·설명 | `docs/spec.md §2.2` |
| intent → 도구 서브셋, 인용 가능 도구 | `references/mcp_playbook.md` |
| `finalize_retrieval` / `retrieve_relevant_content` 계약 | `docs/spec.md §3` |
| 제출 제약 (Dockerfile·포트·브랜치) | `docs/spec.md §4` |
| CoEval 설정 (judge·temperature·timeout·Hydra 경로) | `docs/spec.md §5.2` |
| 확정 안 된 것 | `docs/spec.md §7` — **여기 있는 항목은 지어내지 않는다** |

설계 근거·실측값(페이지 크기·지연·토큰 예산)은 `docs/l2_playbook.md` 다. 사양이 아니다.

## 1. 절대 규칙 4개 — 위반 여부를 코드에서 확인하는 법

| # | 규칙 | 확인 명령 |
|---|---|---|
| 1 | 최종 출력은 **반드시 L2** | `grep -rn "model" configs/l2_live.yaml` — `models.*` 가 전부 `Lunit/L2-preview` 인가 |
| 2 | **HealthBench 역공학 금지** | `grep -rniE "healthbench" data/ src/` — 사전·규칙이 평가 데이터에서 온 흔적이 있는가 |
| 3 | **격리 환경** — 런타임 외부 호출 금지 | `grep -rnE "api\.openai\.com\|https?://(?!.*hackathon\.lunit\.io)" src/ serve.py` |
| 4 | API 키는 코드·yaml 에 없음 | `git ls-files .env` 가 비어야 한다 |

> 규칙 4 에는 **의도된 예외 1건**이 있다: `Dockerfile` ENV 의 `LUNIT_FM_API_KEY`.
> 평가 환경이 키를 어떻게 주입하는지 사양에 없어서 넣은 것이다 (`fc3ed52`).
> `docs/spec.md §7-2` 가 확인되면 즉시 제거하고 키를 재발급한다.

## 2. 제출 게이트 — 커밋·제출 전에 전부 통과해야 한다

```powershell
python scripts/mk.py test            # 단위 테스트
python scripts/mk.py audit           # 배선 감사 37건
python scripts/mk.py submit-check    # 규격 정적 점검
python scripts/mk.py submit-check-docker   # 빌드 5분 + 기동 + 엔드포인트 (권장)
```

`submit-check-docker` 가 재는 것이 진짜 관문이다:

| 제약 | 통과선 |
|---|---|
| repo root 에 `Dockerfile` | 존재 |
| 빌드 시간 | **5분 이내** |
| 수동 작업 없이 기동 | `CMD` 에 인자 없이 뜬다 |
| 서비스 주소 | `0.0.0.0:8000` (**8000 만 평가된다**) |
| `EXPOSE 8000` | 있음 |
| `GET /v1/models` | 200 |
| `POST /v1/chat/completions` | 200, `choices[0].message.content` 비어 있지 않음 |
| 브랜치 | `lunit/hackathon-submission` |

## 3. 평가 경로를 착각하지 않는다

컨테이너에 들어가는 것은 **`serve.py` + `src/` + `configs/` + `data/`** 뿐이다.

```
Dockerfile → CMD ["python","serve.py"] → src/medai/pipeline.py → src/medai/l2.py
```

**`app.py` 와 `submission/` 는 COPY 되지 않는다 = 평가 경로가 아니다.**
거기를 고치면 점수가 1점도 안 움직인다. 고치기 전에 항상 확인한다:

```bash
grep -n "^COPY" Dockerfile
```

## 4. 사양이 바뀌면

대시보드 공지가 바뀌었으면 **`docs/spec.md` 를 먼저 고치고** 그다음 코드를 고친다.
반대 순서로 하면 사양 문서가 죽고, 죽은 문서는 있는 것보다 나쁘다.

`docs/spec.md §7` 의 미확인 항목이 확정되면 그 행을 지우고 §1~§6 본문에 반영한다.
