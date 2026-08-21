#!/usr/bin/env bash
# CoEval 을 macOS(Apple Silicon)에서 설치 가능하게 패치한다.
#
# 문제
#   CoEval 의 핵심 의존성에 sglang[all] (트릴리온랩스 포크)이 들어 있다.
#   sglang 은 nvidia-cutlass-dsl 등 NVIDIA 전용 휠을 끌고 오는데,
#   그 패키지는 manylinux 휠만 있어서 macOS 에서 uv sync 가 통째로 실패한다.
#
#     error: Distribution `nvidia-cutlass-dsl==4.3.5` can't be installed
#            because it doesn't have a source distribution or wheel for the current platform
#
# 해법
#   sglang 은 '모델을 로컬 GPU 로 서빙'하는 용도(`mise run serve`)에만 필요하다.
#   README 도 그 단계를 optional 이라고 명시한다.
#   우리는 이미 OpenAI 호환 엔드포인트(med_ai/serve.py)가 있으므로 sglang 이 필요 없다.
#   → 로컬 클론의 pyproject.toml 에서 sglang 줄만 제거하고 설치한다.
#
# ⚠️ 부작용: 이 클론에서는 `mise run serve` 가 동작하지 않는다. (원래 안 쓴다)
# ⚠️ 현장 Linux 머신이나 GPU 인스턴스에서는 패치 없이 원본 그대로 쓸 것.
#
#   bash scripts/patch_coeval_mac.sh [CoEval경로]
set -euo pipefail

DIR="${1:-}"
if [ -z "$DIR" ]; then
  for c in ../CoEval ./CoEval ~/Documents/CoEval ~/Documents/med_ai/CoEval; do
    [ -f "$c/pyproject.toml" ] && DIR="$c" && break
  done
fi
[ -z "${DIR:-}" ] && { echo "CoEval 경로를 못 찾았습니다. 인자로 주세요."; exit 1; }
DIR="$(cd "$DIR" && pwd)"
echo "CoEval: $DIR"

cd "$DIR"
[ -f pyproject.toml.orig ] || cp pyproject.toml pyproject.toml.orig
echo "백업: pyproject.toml.orig"

python3 - <<'PY'
import re, pathlib
p = pathlib.Path("pyproject.toml")
s = p.read_text(encoding="utf-8")
before = s

# sglang 의존성 줄 제거 (git URL 포함)
s = re.sub(r'^\s*"sglang\[.*?\].*?",?\s*\n', "", s, flags=re.M)

# macOS 에서 해석 불가한 환경을 명시적으로 배제해 두면 재현이 쉽다
if "[tool.uv]" not in s:
    s += '\n[tool.uv]\n# macOS 로컬 평가용 패치 — sglang(GPU 서빙)은 제거됨\n'

p.write_text(s, encoding="utf-8")
print("  sglang 줄 제거:", "OK" if s != before else "이미 없음")
PY

echo
echo "설치 중 (uv sync)..."
if command -v mise >/dev/null 2>&1; then
  mise run sync || uv sync --dev
else
  uv sync --dev
fi

echo
echo "확인:"
./.venv/bin/python -c "import hydra, deepeval, datasets; print('  ✅ hydra / deepeval / datasets OK')" \
  || { echo "  ❌ 임포트 실패"; exit 1; }
./.venv/bin/python -c "
import importlib.util as u
print('  sglang:', '설치됨' if u.find_spec('sglang') else '없음 (정상 — serve 는 안 씁니다)')"

cat <<MSG

=============================================================
 완료. 이제 평가만 돌리면 됩니다 (GPU 불필요)

   # 터미널 1 — 우리 서버
   cd ~/Documents/med_ai && make serve-mock

   # 터미널 2 — CoEval
   cd $DIR
   export OPENAI_API_KEY=sk-...
   mise run eval -- datasets=healthbench_consensus \\
       client.api_base=http://localhost:8080/v1 \\
       client.model=medai num_samples=5

 되돌리기:  cp pyproject.toml.orig pyproject.toml && mise run sync
=============================================================
MSG
