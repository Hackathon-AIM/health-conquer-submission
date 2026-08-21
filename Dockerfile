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
    PORT=8000 \
    # 로케일이 POSIX 인 평가 환경에서 한글 print 가 UnicodeEncodeError 로
    # 프로세스를 죽이는 것을 막는다. 배너 한 줄로 0점이 되면 억울하다.
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONIOENCODING=utf-8

# ★ 평가자는 팀 자격증명을 주입하지 않는다 (팀 실측 — nightandweather, fc3ed52).
#   해커톤 전용 키이며 행사 후 만료된다. 런타임 환경변수가 있으면 그쪽이 우선한다.
ENV LUNIT_FM_API_KEY="lunit_afGfOD-9oZq2Obeez2NfkrxRRCsN7T4a6G0Gw4JCSjQ"

EXPOSE 8000

# slim 이미지에는 curl 이 없다. 파이썬으로 자기 /health 를 찔러 본다.
# 평가자가 기동 완료를 기다린다면 이게 신호가 된다 (팀 Dockerfile 과 동일).
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status==200 else 1)"]

# 인자 없이 뜬다. 포트·설정은 위 ENV 가 결정한다.
CMD ["python", "serve.py"]
