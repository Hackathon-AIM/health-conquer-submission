# Lunit Dashboard Access Check

**확인일:** 2026-08-21  
**대상 URL:** <https://dashboard.hackathon.lunit.io/leaderboard>  
**목적:** 리더보드 접근 가능 여부와 dashboard에서 확인 가능한 운영 정보를 정리한다.

## 접근 결과

| 확인 항목 | 결과 |
| --- | --- |
| HTTP HEAD | `200 OK` |
| HTTP GET | `200 OK`, `text/html; charset=utf-8`, HTML 약 14 KB |
| 브라우저 렌더링 후 URL | `/sign-in?returnTo=%2Fleaderboard` |
| 페이지 제목 | `Lunit Hackathon Dashboard` |
| 인증 전 리더보드 데이터 | 노출되지 않음 |
| 브라우저 콘솔 오류 | 확인된 오류 없음 |

서버는 리더보드 경로의 HTML shell을 반환하지만, 실제 브라우저 렌더링 후에는 로그인 페이지로
리다이렉트된다. 따라서 리더보드 순위, 점수, 제출 결과 같은 데이터는 인증 없이 확인할 수 없다.

## 로그인 화면에서 확인한 정보

- 언어 전환: `KO`, `EN`
- 서비스명: `Lunit Hackathon`
- 화면명: `팀 대시보드`
- 최초 로그인 안내: 처음 로그인하면 새 비밀번호를 설정해야 한다.
- 초기 비밀번호: 관리자에게 문의해야 한다.
- 로그인 입력값: 팀 이름, 비밀번호
- 옵션: 로그인 상태 유지
- 도움말 패널 항목: Wi-Fi, Promotion code 받기, 팀을 찾고 있나요?, 질문하기

## 개발 및 제출 운영 메모

- 리더보드 URL은 공개 HTML 접근은 가능하지만, 실제 데이터 조회는 팀 계정 인증이 필요하다.
- 자동화나 운영 스크립트에 팀 이름, 비밀번호, 세션 쿠키를 하드코딩하지 않는다.
- 리더보드 확인은 Lunit 네트워크와 로그인된 브라우저 세션에서 수동 확인하는 경로를 기본으로 둔다.
- `/leaderboard`에 직접 접근하면 로그인 후 원래 목적지로 돌아가도록 `returnTo=%2Fleaderboard`가 붙는다.
- 인증 전 상태에서는 점수나 순위 기반 의사결정을 할 수 없으므로, 제출 상태 판단 문서에는 로그인 후 확인한 값만 별도로 기록한다.

## 인증 후 확인 결과

인증 후 dashboard 접근은 가능했다. 현재 팀은 `AIM`으로 표시된다.

### 메뉴와 실제 경로

| 메뉴 | 경로 | 확인 결과 |
| --- | --- | --- |
| 리더보드 | `/leaderboard` | 접근 가능 |
| API key | `/api-keys` | 접근 가능, 팀 설정 전이라 key 없음 |
| 팀 사용량 | `/team-usage` | 접근 가능 |
| 제출 | `/submission` | 접근 가능, 팀 설정 전이라 제출 불가 |
| 규칙 | `/rules` | 접근 가능 |
| 일정 | `/schedule` | 접근 가능 |
| 팀 설정 | `/setup` | 접근 가능 |
| Lunit FM 가이드 | `/quick-start/lunit-fm` | 접근 가능 |
| Model API | `/quick-start/model` | 접근 가능 |
| MCP tools 가이드 | `/quick-start/mcp-tools` | 접근 가능 |

직접 추정한 `/api-key`, `/usage`, `/quickstart`는 404였다.

### 팀 설정

팀 설정을 저장하기 전에는 API key 생성과 제출이 막힌다.

설정에 필요한 항목:

- GitHub repository
- 팀 nickname
- 변경 불가 확인 checkbox

Nickname 규칙:

- 영문 소문자, 숫자, hyphen, underscore 사용
- 1-40자
- 처음과 끝은 영문자 또는 숫자
- 제출 endpoint 형식: `https://submission.hackathon.lunit.io/t/<nickname>`

Repository 설정:

- 제출용 branch는 정확히 `lunit/hackathon-submission`이어야 한다.
- repository 생성 후 Hackathon 관리자를 collaborator로 초대해야 한다.
- 필요한 권한은 pull 권한이다.
- 관리자 계정: `lghsigma597@gmail.com`

저장한 repository와 nickname은 dashboard에서 변경할 수 없고, 변경이 필요하면 관리자에게 문의해야 한다.

### 리더보드 스냅샷

확인 시점에는 모든 팀의 Benchmark score가 `—`였고 제출 상태는 모두 `제출 안 됨`이었다.

| 순위 | 팀 | Benchmark | 제출 |
| --- | --- | --- | --- |
| 1 | AIM | — | 제출 안 됨 |
| 2 | daintlab-A | — | 제출 안 됨 |
| 3 | daintlab-B | — | 제출 안 됨 |
| 4 | daintlab-C | — | 제출 안 됨 |
| 5 | hwiwasoo | — | 제출 안 됨 |
| 6 | Pulse 404 | — | 제출 안 됨 |
| 7 | 끈질긴우루사 | — | 제출 안 됨 |
| 8 | 날렵한위고비 | — | 제출 안 됨 |
| 9 | 두뇌세탁소 | — | 제출 안 됨 |
| 10 | 만능의아스피린 | — | 제출 안 됨 |
| 11 | 메디그라피 | — | 제출 안 됨 |
| 12 | 불사대마왕 | — | 제출 안 됨 |
| 13 | 브라질 원정대 | — | 제출 안 됨 |
| 14 | 승진 | — | 제출 안 됨 |
| 15 | 안녕하세요뉴비입니다 | — | 제출 안 됨 |
| 16 | 알필로그 | — | 제출 안 됨 |
| 17 | 용맹한타이레놀 | — | 제출 안 됨 |
| 18 | 참좋은훈제AI | — | 제출 안 됨 |
| 19 | 창억떡 | — | 제출 안 됨 |
| 20 | Tricare | — | 제출 안 됨 |
| 21 | make-easy | — | 제출 안 됨 |

Dashboard 안내에 따르면 official leaderboard가 생성되기 전까지 Benchmark에는 임시 score가 표시된다.
팀마다 채점 가능한 항목이 다를 수 있어 최종 공동 검토 후 순위가 달라질 수 있다.

### API key와 사용량

- API key 생성 전제조건: repository와 nickname 저장.
- API key 목록은 0개로 표시된다.
- 팀 사용량은 전체 token 0, input 0, output 0, 요청 0, 오류율 0.0%로 표시된다.
- Latency 지표는 아직 값이 없다.
- API key 원문은 기록하지 않는다.

### 제출 페이지

현재 상태:

- 팀 설정 필요
- 제출 기능 비활성
- `Endpoint unavailable`
- 활성 trial 없음
- 제출 이력 없음

제출 대상:

- Containerized multi-turn conversation driver
- Evaluator는 각 conversation turn을 service로 전송한다.
- Driver는 conversation context를 사용해 필요한 model 또는 tool을 orchestrate하고 다음 assistant response를 반환해야 한다.

제출 제약:

- Repository root에 `Dockerfile` 필요
- Evaluation VM에서 image build가 5분 이내 완료되어야 함
- Container는 수동 작업 없이 시작되어야 함
- Service bind: `0.0.0.0:8000`
- 평가 대상 port: `8000` only
- OpenAI-compatible API 필요
- 최소 endpoint: `GET /v1/models`, `POST /v1/chat/completions`
- 제출 SHA는 `lunit/hackathon-submission` branch HEAD의 40자리 전체 SHA
- 이 페이지에서의 마지막 제출이 최종 제출로 간주됨

Local 실행 예시:

```bash
docker build -t my-team-submission:local .
docker run --rm -p 8000:8000 my-team-submission:local
```

### 대시보드 일정

| 시점 | 내용 |
| --- | --- |
| 2026-08-21 13:00-13:30 | 오프닝, 규칙 공유, 사용 방법 안내 |
| 2026-08-21 13:30- | 현장 팀 매칭 & 개발 시작 |
| 2026-08-21 17:30 | 저녁 |
| 2026-08-21 18:30- | 개발 계속 |
| 2026-08-22 10:30 | 제출 마감 |
| 2026-08-22 10:30-11:30 | 발표 준비 |
| 2026-08-22 11:30-13:00 | 발표 & 점심 |
| 2026-08-22 14:00-15:00 | 시상 & 클로징 |

## 빠른 시작 가이드 요약

상세 구현 메모는 기존 문서를 참고한다.

- L2 사용 방식: `docs/architecture/lunit_fm_l2_usage.md`
- Model API와 Patient Simulator: `docs/architecture/lunit_api_integration.md`
- MCP 연결과 tool catalog: `docs/architecture/lunit_mcp_tools.md`

Dashboard에서 확인한 핵심 endpoint와 model:

| 항목 | 값 |
| --- | --- |
| Lunit FM endpoint | `https://model.hackathon.lunit.io/` |
| Patient Simulator endpoint | `https://patient.hackathon.lunit.io/` |
| MCP endpoint | `https://mcp.hackathon.lunit.io/mcp` |
| L2 model | `Lunit/L2-preview` |
| Patient simulator model | `patient-simulator-ko` |

동일한 팀 API key를 Model, Patient Simulator, MCP endpoint에서 모두 사용한다.

## 이번 확인에서 하지 않은 것

- 관리자 초기 비밀번호 요청 또는 로그인 시도
- 인증 세션, 쿠키, 로컬 스토리지 확인
- Promotion code 발급 버튼 클릭
- 팀 설정 저장
- API key 생성 또는 원문 기록
- 리더보드 내부 API 추측 호출
