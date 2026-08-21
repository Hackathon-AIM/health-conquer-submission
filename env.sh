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
# ★ 왜 마지막에 진짜 CoEval 을 돌리는가
#   대시보드가 판정하는 것은 CoEval 프로세스의 **종료 코드**다.
#   CoEval/src/coeval/main.py 에서 exit 1 이 나는 조건은 둘뿐이다:
#     - 추론과 채점을 모두 끝낸 샘플이 0개  (num_evaluated == 0)
#     - run_evaluation 밖으로 예외가 새어나감
#   개별 샘플 실패는 절대 전체를 죽이지 않는다(runner.py 의 generate_one 이 삼킨다).
#   즉 리더보드의 coeval_failed 는 "답이 나빴다"가 아니라 "전 샘플이 도달 실패"다.
#   그래서 같은 종료 코드를 여기서 직접 확인한다.
#
# 사용:  bash scripts/preflight.sh
set -uo pipefail

# 저장소 루트에서 도는 것을 보장한다 — .env, CoEval/ 을 상대경로로 찾기 때문이다.
cd "$(dirname "$0")/.."

# ── docker CLI 해석 ───────────────────────────────────────────
# Docker Desktop 을 DMG 에서 실행한 채로 CLI 를 설치하면 /usr/local/bin/docker 가
# /Volumes/Docker/... 를 가리키는 심링크로 깔린다. DMG 를 빼면 그 링크는 죽고,
# 데몬은 멀쩡히 도는데 PATH 에서는 docker 가 사라진다. 이 머신이 그 상태였다.
# 그래서 PATH 에 없으면 앱 번들에서 직접 찾아 함수로 덮어쓴다.
if ! command -v docker >/dev/null 2>&1; then
  for _cand in /Applications/Docker.app/Contents/Resources/bin/docker \
               "$HOME/.docker/bin/docker" \
               /opt/homebrew/bin/docker; do
    [ -x "$_cand" ] && DOCKER_BIN="$_cand" && break
  done
  if [ -z "${DOCKER_BIN:-}" ]; then
    printf '\033[31m❌ docker CLI 를 못 찾았다.\033[0m Docker Desktop 이 켜져 있어도 CLI 가 PATH 에 없다.\n'
    if [ -L /usr/local/bin/docker ] && [ ! -e /usr/local/bin/docker ]; then
      echo "   /usr/local/bin/docker 가 끊긴 심링크다 → $(readlink /usr/local/bin/docker)"
      echo "   고치려면(root 필요):"
      echo "     sudo ln -sf /Applications/Docker.app/Contents/Resources/bin/docker /usr/local/bin/docker"
    fi
    exit 1
  fi
  printf '\033[33m⚠️  PATH 에 docker 가 없어 앱 번들 경로를 PATH 에 붙인다: %s\033[0m\n' "$(dirname "$DOCKER_BIN")"
  if [ -L /usr/local/bin/docker ] && [ ! -e /usr/local/bin/docker ]; then
    echo "   원인: /usr/local/bin/docker 가 끊긴 심링크 → $(readlink /usr/local/bin/docker)"
    echo "   영구 수정(root 필요):"
    echo "     sudo ln -sf /Applications/Docker.app/Contents/Resources/bin/docker /usr/local/bin/docker"
  fi
  # docker 하나만 덮는 걸로는 부족하다. 빌드는 docker-credential-desktop 을
  # PATH 에서 찾아 쓰는데 그것도 같은 끊긴 심링크라 "error getting credentials" 로 죽는다.
  # 그래서 번들 bin 디렉터리를 통째로 PATH 앞에 붙인다.
  PATH="$(dirname "$DOCKER_BIN"):$PATH"
  export PATH
fi
if ! docker info >/dev/null 2>&1; then
  printf '\033[31m❌ docker 데몬에 연결할 수 없다.\033[0m Docker Desktop 을 켜고 다시 실행하라.\n'
  exit 1
fi

IMG=medai-preflight
CNT=medai-preflight-run
# 평가자의 실제 동시성. 출처: CoEval/src/coeval/conf/runner/default.yaml 의 concurrent_limit
CONC=${PREFLIGHT_CONC:-30}
# CoEval 스모크
COEVAL_DIR=${COEVAL_DIR:-CoEval}
COEVAL_SAMPLES=${COEVAL_SAMPLES:-5}
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
      echo "       ${DOCKER_BIN:-docker} ps -q | xargs -r ${DOCKER_BIN:-docker} stop"
    else
      bad "컨테이너가 죽었다 — exit $CODE  ← 평가에서 나던 바로 그 실패다"
    fi
    echo "     ── 컨테이너 로그 ──"; sed 's/^/     /' "$LOGF"
    exit 1
  fi
  # 준비성은 /v1/models 로 본다. 팀 엔드포인트 게이트웨이는 /v1/* 만 프록시하고
  # /health 는 404 를 준다(실측). 평가자가 볼 수 있는 경로로 재는 게 맞다.
  if curl -sf -m 2 http://localhost:$HOST_PORT/v1/models >/dev/null 2>&1; then UP=1; break; fi
done
[ "$UP" = 1 ] && ok "기동 ${i}초 · GET /v1/models 200 · 컨테이너 살아있음" \
              || { bad "40초 안에 응답이 없다"; sed 's/^/     /' "$LOGF"; exit 1; }

# ── 3. 필수 엔드포인트 ────────────────────────────────────────
say "3. 필수 엔드포인트 (제출 규정)"
curl -sf -m 10 http://localhost:$HOST_PORT/v1/models >/dev/null \
  && ok "GET /v1/models" || bad "GET /v1/models 실패"
# /health 는 규정 요구사항이 아니라 Dockerfile HEALTHCHECK 가 쓰는 경로다.
# 게이트웨이는 이걸 프록시하지 않으므로 실패해도 제출을 막지는 않는다.
curl -sf -m 10 http://localhost:$HOST_PORT/health >/dev/null \
  && ok "GET /health (컨테이너 HEALTHCHECK 용)" \
  || warn "GET /health 실패 — Dockerfile HEALTHCHECK 가 unhealthy 로 뜬다"

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

# ── 4. 동시 요청 (평가자는 concurrent_limit=30) ───────────────
# 페이로드도 CoEval 이 실제로 보내는 모양에 맞춘다 — system "You are Chain-of-Evidence",
# temperature 0.0, max_tokens 32768. 출처: CoEval/src/coeval/conf/client/passthrough.yaml
say "4. 동시 요청 ${CONC}건 — 평가자와 같은 부하 (수 분 걸린다, 기다려라)"
echo "     ${CONC}건을 동시에 던진다. 한 건이 최대 60초 걸릴 수 있다."
LOAD_REQ='{"model":"Lunit/L2-preview","temperature":0.0,"max_tokens":32768,"messages":[{"role":"system","content":"You are Chain-of-Evidence"},{"role":"user","content":"감기에 좋은 방법 알려줘"}]}'
# ⚠️ 인자 없는 `wait` 을 쓰면 안 된다. 백그라운드로 띄워 둔
# `docker start --attach` job 까지 같이 기다리게 되고, 그 job 은 컨테이너가
# 죽어야 반환한다. 그러면 "부하 후 컨테이너가 죽었다" 가 스스로 참이 되는
# 구조가 된다 — 실제로 이 함정에 걸린 판정을 봤다. curl PID 만 기다린다.
LOAD_PIDS=()
for n in $(seq 1 $CONC); do
  ( curl -sf -m 180 -H 'content-type: application/json' -d "$LOAD_REQ" \
      http://localhost:$HOST_PORT/v1/chat/completions -o "$WORK/r$n.json" 2>/dev/null
    printf '.' ) &
  LOAD_PIDS+=($!)
done
wait "${LOAD_PIDS[@]}"
echo
# 파일이 비었는지가 아니라 content 가 비었는지를 본다. 200 에 빈 답을 실어 보내면
# CoEval 은 실패로 세지 않고 그대로 0점을 매긴다 — 그게 더 나쁘다.
GOOD=0; EMPTY=0
for n in $(seq 1 $CONC); do
  L=$(python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(len((d['choices'][0]['message'].get('content') or '').strip()))" "$WORK/r$n.json" 2>/dev/null)
  if [ "${L:--1}" -gt 20 ]; then GOOD=$((GOOD+1))
  elif [ "${L:--1}" -ge 0 ]; then EMPTY=$((EMPTY+1)); fi
done
if [ "$GOOD" = "$CONC" ]; then
  ok "$CONC/$CONC 응답 · 빈 답 0건"
else
  bad "$GOOD/$CONC 만 정상 · 빈 답 ${EMPTY}건 — 동시 부하에서 무너진다"
fi
if docker inspect -f '{{.State.Running}}' "$CNT" 2>/dev/null | grep -q true; then
  ok "부하 후에도 컨테이너 살아있음"
else
  bad "부하 중 컨테이너가 죽었다 — exit $(docker inspect -f '{{.State.ExitCode}}' "$CNT" 2>/dev/null)"
  echo "     ── 컨테이너 로그 끝부분 ──"; tail -30 "$LOGF" | sed 's/^/     /'
fi

# ── 5. 진짜 CoEval 스모크 ─────────────────────────────────────
# 대시보드가 보는 것과 문자 그대로 같은 종료 코드를 확인한다. n=5 면 40초 안쪽이고
# judge(gpt-4.1) 비용도 무시할 수준이다. 실측: exit 0, 5/5.
say "5. CoEval 스모크 — 대시보드와 같은 종료 코드로 판정"
if [ ! -f "$COEVAL_DIR/pyproject.toml" ]; then
  warn "CoEval 이 없다 ($COEVAL_DIR) — 건너뜀.  git clone https://github.com/lunit-io/CoEval"
elif [ ! -x "$COEVAL_DIR/.venv/bin/python" ]; then
  warn "$COEVAL_DIR/.venv 가 없다 — 건너뜀."
  echo "     cd $COEVAL_DIR && python3.12 -m venv .venv && ./.venv/bin/python -m ensurepip && ./.venv/bin/python -m pip install -e ."
else
  if [ -z "${OPENAI_API_KEY:-}" ] && [ -f .env ]; then
    export OPENAI_API_KEY=$(grep '^OPENAI_API_KEY=' .env | cut -d= -f2- | tr -d "\"' ")
  fi
  if [ -z "${OPENAI_API_KEY:-}" ]; then
    warn "OPENAI_API_KEY 가 없다 — HealthBench judge 가 못 돈다. 건너뜀"
  else
    echo "     healthbench_consensus · num_samples=$COEVAL_SAMPLES · judge=gpt-4.1"
    ( cd "$COEVAL_DIR" && PYTHONPATH=src ./.venv/bin/python -m coeval.main datasets=healthbench_consensus client.llm.config.api_base=http://localhost:$HOST_PORT/v1 client.llm.config.model=Lunit/L2-preview num_samples=$COEVAL_SAMPLES ) >"$WORK/coeval.log" 2>&1
    CV_CODE=$?
    if [ "$CV_CODE" = 0 ]; then
      ok "CoEval exit 0 — coeval_failed 조건에 걸리지 않는다"
      grep -E "Evaluation complete|W.AVG" "$WORK/coeval.log" | tail -3 | sed 's/^/     /'
    else
      bad "CoEval exit $CV_CODE  ← 리더보드의 coeval_failed 와 같은 실패다"
      tail -30 "$WORK/coeval.log" | sed 's/^/     /'
    fi
  fi
fi

# ── 6. 결론 ───────────────────────────────────────────────────
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
