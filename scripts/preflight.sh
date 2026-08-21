#!/usr/bin/env bash
# 제출 전 최종 검증 — 평가자가 하는 것과 같은 순서로 재현한다.
#
# ★ 왜 `docker build .` 로는 부족한가
#   평가자는 **커밋된 SHA** 를 받아 거기서 이미지를 만든다. 우리가 지금까지 쓰던
#   `docker build -t medai:local .` 은 **작업 디렉터리**를 쓴다. 커밋 안 된 파일,
#   .gitignore 된 파일이 로컬 빌드에는 들어가고 평가 빌드에는 안 들어간다.
#   "로컬은 되는데 평가에서 죽는" 현상이 여기서 나온다.
#   그래서 이 스크립트는 `git archive HEAD` 로 **커밋된 것만** 꺼내 빌드한다.
#
# ★ 왜 `docker run` 으로는 부족한가
#   평가자 로그에 찍힌 명령은 `docker start --attach <container>` 다.
#   create → start 경로이고, 컨테이너가 종료하면 그 종료 코드가 그대로 실패가 된다.
#   그래서 여기서도 create → start 로 띄우고, **기동 후에도 살아 있는지**를 본다.
#   exit 1 은 바로 이 지점에서 났다.
#
# 사용:  bash scripts/preflight.sh
set -uo pipefail

IMG=medai-preflight
CNT=medai-preflight-run
# 호스트 포트는 8000 을 피한다 — 개발용 `docker run -p 8000:8000` 이 떠 있으면
# 바인드가 충돌해 컨테이너가 exit 128 로 죽고, 우리 앱 문제로 오인하게 된다.
# 컨테이너 안쪽은 규정대로 8000 그대로다.
HOST_PORT=${PREFLIGHT_PORT:-18000}
WORK=$(mktemp -d)
LOGF="$WORK/container.log"
FAIL=0

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✅ %s\033[0m\n' "$*"; }
bad()  { printf '  \033[31m❌ %s\033[0m\n' "$*"; FAIL=1; }
warn() { printf '  \033[33m⚠️  %s\033[0m\n' "$*"; }

cleanup() {
  docker rm -f "$CNT" >/dev/null 2>&1
  rm -rf "$WORK"
}
trap cleanup EXIT

# ── 0. 커밋 상태 ──────────────────────────────────────────────
say "0. 커밋 상태 — 평가자는 커밋된 것만 본다"
RUNNING_N=$(docker ps -q | wc -l | tr -d ' ')
[ "$RUNNING_N" != "0" ] && warn "다른 컨테이너 ${RUNNING_N}개가 떠 있다 (호스트 포트 $HOST_PORT 로 피해서 띄운다)"
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  bad "커밋 안 된 수정이 있다. 커밋하고 다시 실행하라:"
  git status --short --untracked-files=no | sed 's/^/     /'
else
  ok "작업 트리 깨끗함"
fi
UNTRACKED=$(git ls-files --others --exclude-standard)
if [ -n "$UNTRACKED" ]; then
  warn "추적되지 않는 파일이 있다 — 이 파일들은 평가 이미지에 '들어가지 않는다':"
  echo "$UNTRACKED" | sed 's/^/     /'
  echo "     서빙에 필요한 파일이 여기 있으면 반드시 git add 하라."
fi
SHA=$(git rev-parse HEAD)
echo "     HEAD = $SHA"

# ── 1. 커밋된 트리만 꺼내서 빌드 ──────────────────────────────
say "1. 커밋된 트리로 이미지 빌드 (평가자와 동일)"
git archive HEAD | tar -x -C "$WORK" || { bad "git archive 실패"; exit 1; }
[ -f "$WORK/Dockerfile" ] && ok "루트에 Dockerfile 있음" || bad "루트에 Dockerfile 이 없다"
grep -q "EXPOSE 8000" "$WORK/Dockerfile" && ok "EXPOSE 8000 있음" || bad "EXPOSE 8000 이 없다"

T0=$(date +%s)
if docker build -q -t "$IMG" "$WORK" >"$WORK/build.log" 2>&1; then
  BT=$(( $(date +%s) - T0 ))
  if [ "$BT" -le 300 ]; then ok "빌드 성공 — ${BT}초 (제한 300초)"
  else bad "빌드가 ${BT}초 걸렸다 — 5분 제한 초과"; fi
else
  bad "빌드 실패:"; tail -25 "$WORK/build.log" | sed 's/^/     /'; exit 1
fi

# ── 2. create → start --attach 로 기동 ────────────────────────
say "2. 기동 — docker create → start --attach (평가자와 동일)"
docker rm -f "$CNT" >/dev/null 2>&1
if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"$HOST_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  bad "호스트 포트 $HOST_PORT 이 이미 사용 중이다. PREFLIGHT_PORT=19000 bash scripts/preflight.sh 로 바꿔 실행하라"
  exit 1
fi
docker create --name "$CNT" -p "$HOST_PORT":8000 "$IMG" >/dev/null || { bad "create 실패"; exit 1; }
docker start --attach "$CNT" >"$LOGF" 2>&1 &
ATTACH_PID=$!

UP=0
for i in $(seq 1 40); do
  sleep 1
  RUNNING=$(docker inspect -f '{{.State.Running}}' "$CNT" 2>/dev/null)
  if [ "$RUNNING" != "true" ]; then
    CODE=$(docker inspect -f '{{.State.ExitCode}}' "$CNT" 2>/dev/null)
    if [ "$CODE" = "128" ]; then
      bad "도커가 컨테이너를 못 띄웠다 (exit 128) — 앱 문제가 아니라 환경 문제다"
      echo "     대개 포트 충돌이다. 아래로 정리하고 다시 실행하라:"
      echo "       docker ps -q | xargs -r docker stop"
    else
      bad "컨테이너가 죽었다 — exit $CODE  ← 평가에서 나던 바로 그 실패다"
    fi
    echo "     ── 컨테이너 로그 ──"; sed 's/^/     /' "$LOGF"
    exit 1
  fi
  if curl -sf -m 2 http://localhost:$HOST_PORT/health >/dev/null 2>&1; then UP=1; break; fi
done
[ "$UP" = 1 ] && ok "기동 ${i}초 · /health 200 · 컨테이너 살아있음" \
              || { bad "40초 안에 응답이 없다"; sed 's/^/     /' "$LOGF"; exit 1; }

# ── 3. 필수 엔드포인트 ────────────────────────────────────────
say "3. 필수 엔드포인트 (제출 규정)"
curl -sf -m 10 http://localhost:$HOST_PORT/v1/models >/dev/null \
  && ok "GET /v1/models" || bad "GET /v1/models 실패"

REQ='{"model":"Lunit/L2-preview","messages":[{"role":"user","content":"타이레놀 성인 1회 최대 용량이 얼마인가요"}]}'
RESP=$(curl -sf -m 120 -H 'content-type: application/json' -d "$REQ" \
        http://localhost:$HOST_PORT/v1/chat/completions 2>/dev/null)
if [ -n "$RESP" ]; then
  LEN=$(printf '%s' "$RESP" | python3 -c 'import json,sys;print(len(json.load(sys.stdin)["choices"][0]["message"]["content"]))' 2>/dev/null)
  if [ "${LEN:-0}" -gt 20 ]; then ok "POST /v1/chat/completions — 응답 ${LEN}자"
  else bad "응답이 비었다 (${LEN:-0}자) — 그 문항은 0점이다"; fi
else
  bad "POST /v1/chat/completions 실패"
fi

# ── 4. 동시 요청 (평가자는 concurrent_limit=10) ───────────────
say "4. 동시 요청 10건 — 평가자와 같은 부하"
for n in $(seq 1 10); do
  curl -sf -m 150 -H 'content-type: application/json' \
    -d '{"model":"Lunit/L2-preview","messages":[{"role":"user","content":"감기에 좋은 방법 알려줘"}]}' \
    http://localhost:$HOST_PORT/v1/chat/completions -o "$WORK/r$n.json" &
done
wait
GOOD=0
for n in $(seq 1 10); do
  [ -s "$WORK/r$n.json" ] && GOOD=$((GOOD+1))
done
[ "$GOOD" = 10 ] && ok "10/10 응답" || bad "$GOOD/10 만 응답 — 동시 부하에서 무너진다"
docker inspect -f '{{.State.Running}}' "$CNT" 2>/dev/null | grep -q true \
  && ok "부하 후에도 컨테이너 살아있음" || bad "부하 중 컨테이너가 죽었다"

# ── 5. 결론 ───────────────────────────────────────────────────
say "결과"
if [ "$FAIL" = 0 ]; then
  printf '  \033[32m통과 — 제출 가능\033[0m\n\n'
  echo "  리더보드에 낼 SHA:"
  echo "    $SHA"
  echo "  Model name: Lunit/L2-preview"
  echo
  echo "  ⚠️ 이 SHA 가 브랜치 HEAD 인지 확인하라 (아니면 sha_not_branch_head):"
  echo "    git push origin HEAD:lunit/hackathon-submission --force"
else
  printf '  \033[31m실패 — 제출하지 말 것\033[0m\n'
  echo "  위 ❌ 항목을 고치고 다시 실행하라."
fi
exit "$FAIL"
