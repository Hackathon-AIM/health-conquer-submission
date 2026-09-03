#!/usr/bin/env bash
# venv 생성 + 의존성 설치. 전역 파이썬을 오염시키지 않는다.
set -euo pipefail
cd "$(dirname "$0")/.."

# 3.10+ 를 우선 찾는다. 없으면 3.9로도 동작하도록 코드를 맞춰 두었다.
PYBIN=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1; then
    v=$("$c" -c 'import sys;print("%d%02d"%sys.version_info[:2])')
    if [ "$v" -ge 310 ]; then PYBIN=$c; break; fi
    [ -z "$PYBIN" ] && PYBIN=$c
  fi
done
[ -z "$PYBIN" ] && { echo "python3 을 찾을 수 없습니다."; exit 1; }

VER=$("$PYBIN" -c 'import sys;print("%d.%d"%sys.version_info[:2])')
echo "→ 사용할 파이썬: $PYBIN ($VER)"
if [ "$($PYBIN -c 'import sys;print("%d%02d"%sys.version_info[:2])')" -lt 310 ]; then
  cat <<'WARN'

⚠️  Python 3.9 입니다. 코드는 3.9에서도 돌아가지만 3.11 권장.
    설치하려면 (택1):
      brew install python@3.11
      curl -LsSf https://astral.sh/uv/install.sh | sh && uv python install 3.11
    설치 후 `make setup` 을 다시 실행하세요.

WARN
fi

[ -d .venv ] || "$PYBIN" -m venv .venv
./.venv/bin/python -m pip install --quiet --upgrade pip
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python data/build_dicts.py --seed

cat <<'DONE'

✅ 완료. 앞으로는 아래 중 하나로 쓰세요.

   source .venv/bin/activate     # 셸에서 직접
   make test / make run          # Makefile 이 알아서 .venv 사용

DONE
