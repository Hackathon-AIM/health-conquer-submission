#!/usr/bin/env bash
# CoEval 연결 리허설 — 제출 경로를 미리 뚫어둔다.
#
# CoEval(https://github.com/lunit-io/CoEval)은 평가 대상을 OpenAI 호환 엔드포인트로
# 호출한다. 즉 우리 제출물은 serve.py 다.
#
# ★ GPU 불필요
#   CoEval 의 `mise run serve` 는 SGLang 로컬 서빙용이라 GPU가 필요하지만,
#   README 가 "this step is optional" 이라고 명시한다.
#   우리는 이미 OpenAI 호환 엔드포인트(serve.py)가 있으므로 그 단계를 건너뛴다.
#
#   bash scripts/coeval_rehearsal.sh
set -uo pipefail
cd "$(dirname "$0")/.."

CFG="${CFG:-configs/openai.yaml}"
PORT="${PORT:-8080}"
N="${N:-5}"
# CoEval 위치 자동 탐색 (../CoEval, ./CoEval, ~/Documents/CoEval 등)
COEVAL_DIR="${COEVAL_DIR:-}"
if [ -z "$COEVAL_DIR" ]; then
  for c in ../CoEval ./CoEval ~/Documents/CoEval ~/Documents/med_ai/CoEval; do
    [ -f "$c/pyproject.toml" ] && COEVAL_DIR="$c" && break
  done
fi
COEVAL_DIR="${COEVAL_DIR:-../CoEval}"

echo "=============================================================="
echo "  CoEval 연결 리허설"
echo "  config=$CFG  port=$PORT  num_samples=$N"
echo "=============================================================="

# ── 0) 사전 점검 ────────────────────────────────────────────
echo
echo "[0/4] 사전 점검"
if command -v mise >/dev/null 2>&1; then
  echo "  ✅ mise $(mise --version 2>/dev/null | head -1)"
else
  echo "  ❌ mise 없음. 설치:"
  echo "       curl https://mise.run | sh"
  echo "       echo 'eval \"\$(~/.local/bin/mise activate zsh)\"' >> ~/.zshrc && exec zsh"
  MISE_MISSING=1
fi
if [ -n "${OPENAI_API_KEY:-}" ]; then
  echo "  ✅ OPENAI_API_KEY 설정됨 (HealthBench judge 용)"
else
  # .env 에서 읽어본다
  if [ -f .env ] && grep -q '^OPENAI_API_KEY=.' .env; then
    export OPENAI_API_KEY=$(grep '^OPENAI_API_KEY=' .env | cut -d= -f2-)
    echo "  ✅ OPENAI_API_KEY (.env 에서 로드)"
  else
    echo "  ⚠️  OPENAI_API_KEY 없음 — HealthBench judge 가 안 돕니다"
    echo "       .env 에 넣거나: export OPENAI_API_KEY=sk-..."
  fi
fi

# ── 1) 우리 서버 기동 ────────────────────────────────────────
echo
echo "[1/4] serve.py 기동 (제출물)"
if lsof -ti:"$PORT" >/dev/null 2>&1; then
  echo "  ⚠️  포트 $PORT 사용 중. 기존 프로세스를 쓰겠습니다."
else
  ./.venv/bin/python serve.py --config "$CFG" --port "$PORT" > /tmp/medai_serve.log 2>&1 &
  SRV=$!
  trap 'kill $SRV 2>/dev/null' EXIT
  sleep 4
fi

if ! curl -sf "localhost:$PORT/v1/models" >/dev/null; then
  echo "  ❌ 서버 응답 없음. 로그:"; tail -25 /tmp/medai_serve.log; exit 1
fi
echo "  ✅ /v1/models 응답"

# ── 2) OpenAI SDK 호환성 (CoEval 이 쓰는 방식) ───────────────
echo
echo "[2/4] OpenAI SDK 로 멀티턴 호출"
./.venv/bin/python - <<PY || { echo "  ❌ SDK 호출 실패"; exit 1; }
from openai import OpenAI
c = OpenAI(base_url="http://localhost:$PORT/v1", api_key="none", timeout=180)
r = c.chat.completions.create(model="medai", messages=[
    {"role":"user","content":"어제 술을 너무 많이 마셨어요"},
    {"role":"assistant","content":"수분 섭취와 휴식이 도움이 됩니다."},
    {"role":"user","content":"두통약은 뭘 먹으면 될까요?"},
])
t = r.choices[0].message.content
assert r.choices[0].finish_reason == "stop", "finish_reason 이상"
assert r.usage.total_tokens > 0, "usage 없음"
assert t and len(t) > 20, "응답이 너무 짧음"
print("  ✅ 스키마 정상 (finish_reason / usage / content)")
print("  ✅ 무상태 세션 복원 확인 — 1턴 음주가 3턴 약 추천에 반영되는지 아래 확인")
print("  ---")
print("  " + t[:350].replace("\n", "\n  "))
PY

# ── 3) CoEval 준비 ──────────────────────────────────────────
echo
echo "[3/4] CoEval 준비"
if [ ! -d "$COEVAL_DIR" ]; then
  echo "  CoEval 이 없습니다. 다른 터미널에서:"
  echo
  echo "    cd $(dirname "$(pwd)") && git clone https://github.com/lunit-io/CoEval"
  echo "    cd CoEval && mise trust && mise run sync"
  echo
  echo "  ※ mise run sync 가 Python 3.12 를 알아서 설치합니다 (시스템 3.9 와 무관)"
  echo "  ※ mise run serve 는 GPU 가 필요하지만 건너뜁니다 — 우리 serve.py 를 씁니다"
else
  echo "  ✅ $COEVAL_DIR 존재"
  [ -d "$COEVAL_DIR/.venv" ] && echo "  ✅ 의존성 설치됨" \
    || echo "  ⚠️  mise run sync 미실행 — cd $COEVAL_DIR && mise trust && mise run sync"
fi

# ── 4) 실행 명령 ────────────────────────────────────────────
echo
echo "[4/4] CoEval 실행 (다른 터미널에서)"
cat <<CMD

    cd $COEVAL_DIR
    export OPENAI_API_KEY=\${OPENAI_API_KEY:-sk-...}

    # 스모크 — 5문항으로 연결부터 확인
    mise run eval -- datasets=healthbench_consensus \\
        client.llm.config.api_base=http://localhost:$PORT/v1 \\
        client.llm.config.model=medai \\
        num_samples=$N

  ⚠️ judge 비용은 문항당 루브릭 수에 비례합니다. 반드시 작게 시작하세요.
  결과: $COEVAL_DIR/evaluation_outputs/<날짜>/<시각>/summary_healthbench_consensus.json

CMD
echo "서버는 계속 떠 있습니다. 종료하려면 Ctrl+C"
[ -n "${SRV:-}" ] && wait $SRV || read -r -d '' _ </dev/null
