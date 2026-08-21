# 제출용 컨테이너 — Conquer Health 해커톤
#
# 규정
#   · repository root 에 Dockerfile 이 있어야 하고, build 가 5분 이내
#   · 수동 작업 없이 시작되어 0.0.0.0:8000 에서 서비스
#   · OpenAI 호환: GET /v1/models, POST /v1/chat/completions
#
# 로컬 확인
#   docker build -t medai:local .
#   docker run --rm -p 8000:8000 -e LUNIT_FM_API_KEY=lunit_... medai:local
#   curl localhost:8000/v1/models

FROM python:3.12-slim

WORKDIR /app

# ① 의존성만 먼저 — 소스가 바뀌어도 이 레이어는 캐시된다 (빌드 5분 제한 대비)
#    requirements.txt 전체를 쓰지 않는다: FlagEmbedding(리랭커)·pandas·langgraph 는
#    L2 네이티브 경로에서 쓰지 않으면서 빌드만 수 분 늘린다.
COPY requirements-serve.txt ./
RUN pip install --no-cache-dir -r requirements-serve.txt

# ② 소스
COPY serve.py ./
COPY src/ ./src/
COPY configs/ ./configs/
COPY data/ ./data/

ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    MEDAI_CONFIG=configs/l2_live.yaml \
    HOST=0.0.0.0 \
    PORT=8000

EXPOSE 8000

# 인자 없이 뜬다. 포트·설정은 위 ENV 가 결정한다.
CMD ["python", "serve.py"]
