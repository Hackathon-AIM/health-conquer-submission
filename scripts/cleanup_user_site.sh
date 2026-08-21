#!/usr/bin/env bash
# venv 없이 설치해서 ~/Library/Python/3.9 (user site-packages) 에 들어간 패키지를 제거한다.
# 시스템 파이썬을 되돌리는 작업이므로 목록을 확인하고 실행할 것.
set -uo pipefail

PKGS="langgraph langgraph-sdk langgraph-checkpoint langgraph-prebuilt langchain-core langsmith \
FlagEmbedding sentence_transformers peft accelerate ir-datasets \
openai httpx pydantic pydantic-core typing-inspection typing-extensions \
pytest pluggy iniconfig rapidfuzz jiter jsonpatch orjson ormsgpack \
lz4 zstandard uuid-utils tenacity"

echo "다음 패키지를 사용자 site-packages 에서 제거합니다:"
echo "$PKGS" | tr ' ' '\n' | sed '/^$/d' | sed 's/^/  - /'
echo
read -r -p "진행할까요? [y/N] " a
[ "$a" = "y" ] || [ "$a" = "Y" ] || { echo "취소됨"; exit 0; }

python3 -m pip uninstall -y $PKGS || true

echo
echo "완료. 확인:"
python3 -m pip list --user 2>/dev/null | head -30
echo
echo "이제 'make setup' 으로 .venv 안에 다시 설치하세요."
