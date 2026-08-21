#!/usr/bin/env bash
# 어느 디렉토리에서 실행해도 동작한다. cd 할 필요 없음.
#
#   bash ~/Documents/med_ai/go.sh
#
# 하는 일: CoEval 패치·설치 -> 우리 서버 기동 -> CoEval 평가까지 한 번에.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
echo "작업 위치: $HERE"

PORT="${PORT:-8080}"
CFG=configs/openai.yaml
N=5

# CoEval 찾기
CO=""
for c in "$HERE/CoEval" "$HERE/../CoEval" "$HOME/Documents/CoEval"; do
  [ -f "$c/pyproject.toml" ] && CO="$(cd "$c" && pwd)" && break
done

echo
echo "=============================================="
echo " 1단계 · CoEval 설치"
echo "=============================================="
if [ -z "$CO" ]; then
  echo "CoEval 이 없습니다. 먼저:"
  echo "  git clone https://github.com/lunit-io/CoEval $HERE/CoEval"
  exit 1
fi
echo "CoEval: $CO"

if [ -d "$CO/.venv" ] && "$CO/.venv/bin/python" -c "import hydra" 2>/dev/null; then
  echo "이미 설치됨. 건너뜁니다."
else
  bash "$HERE/scripts/patch_coeval_mac.sh" "$CO" || {
    echo
    echo "CoEval 설치 실패. 맥에서는 여기까지가 한계일 수 있습니다."
    echo "대안: bash $HERE/go.sh local   <- 우리 자체 러너로 같은 데이터를 채점"
    exit 1
  }
fi

# 자체 러너만 돌리는 모드
if [ "${1:-}" = "local" ]; then
  echo
  echo "자체 러너로 HealthBench 채점"
  exec ./.venv/bin/python -m eval.healthbench --variant consensus --config "$CFG" --limit 20
fi

echo
echo "=============================================="
echo " 2단계 · 우리 서버 기동 (제출물)"
echo "=============================================="
LOG=/tmp/medai_serve.log

# 이미 우리 서버가 떠 있는지 확인 (포트가 쓰인다고 우리 것이라는 보장은 없다)
ours() { curl -sf "localhost:$1/v1/models" 2>/dev/null | grep -q '"medai"'; }

if ours "$PORT"; then
  echo "이미 우리 서버가 $PORT 에 떠 있습니다."
else
  # 비어 있는 포트를 찾는다 (다른 앱이 8080 을 쓰고 있을 수 있다)
  for p in 8080 8081 8082 8090 9100 9200; do
    if ours "$p"; then PORT=$p; echo "기존 서버 발견: $PORT"; break; fi
    if ! lsof -ti:$p >/dev/null 2>&1; then PORT=$p; break; fi
  done

  if ! ours "$PORT"; then
    echo "포트 $PORT 에서 기동합니다..."
    ./.venv/bin/python serve.py --config "$CFG" --port "$PORT" > "$LOG" 2>&1 &
    SRV=$!
    trap 'kill $SRV 2>/dev/null' EXIT
    for i in 1 2 3 4 5 6 7 8 9 10; do
      sleep 1
      ours "$PORT" && break
    done
  fi
fi

if ! ours "$PORT"; then
  echo
  echo "서버 기동 실패. 로그:"
  [ -f "$LOG" ] && tail -30 "$LOG" || echo "  (로그 없음 — 포트 $PORT 를 다른 앱이 점유 중일 수 있습니다)"
  echo
  echo "직접 확인:  lsof -i:$PORT"
  echo "다른 포트로: PORT=9100 bash $HERE/go.sh"
  exit 1
fi
echo "OK  http://localhost:$PORT/v1"

echo
echo "=============================================="
echo " 3단계 · CoEval 평가"
echo "=============================================="
# API 키 확보
if [ -z "${OPENAI_API_KEY:-}" ] && [ -f "$HERE/.env" ]; then
  export OPENAI_API_KEY=$(grep '^OPENAI_API_KEY=' "$HERE/.env" | cut -d= -f2- | tr -d '"'"'"' ')
fi
if [ -z "${OPENAI_API_KEY:-}" ]; then
  echo "OPENAI_API_KEY 가 없습니다. $HERE/.env 에 넣으세요:"
  echo "  OPENAI_API_KEY=sk-..."
  exit 1
fi
echo "judge 용 키 확인됨"

cd "$CO"
# ⚠️ 키 경로 주의: client.api_base 가 아니라 client.llm.config.api_base 다.
#    (conf/client/passthrough.yaml 의 중첩 구조)
mise run eval -- datasets=healthbench_consensus \
    client.llm.config.api_base="http://localhost:$PORT/v1" \
    client.llm.config.model=medai \
    num_samples=$N

echo
echo "결과: $CO/evaluation_outputs/ 아래 최신 폴더"
