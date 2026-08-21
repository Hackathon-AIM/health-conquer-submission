# 제출용 컨테이너 — Conquer Health 해커톤
#
# 규정: repo root 에 Dockerfile / 빌드 5분 이내 / 수동 작업 없이 0.0.0.0:8000
#
# ⚠️ 구조를 리더보드 통과본(4c8cdc6, 29.13점)에 맞춘다. 평가 VM 의 빌더가
#    우리 맥과 다를 수 있으므로 파서 해석이 갈릴 여지를 남기지 않는다.
#    특히 ENV 줄바꿈(\) 안에 주석을 넣지 않는다 — 빌더에 따라 주석이 값에
#    붙으면 PORT 가 "8000 # ..." 이 되고 int() 파싱이 터져
#    배너 한 줄도 못 찍고 exit 1 이 된다.
FROM python:3.13-slim

WORKDIR /app

COPY requirements-serve.txt .
RUN pip install --no-cache-dir -r requirements-serve.txt

COPY . .

ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1
ENV MEDAI_CONFIG=configs/l2_live.yaml
ENV HOST=0.0.0.0
ENV PORT=8000
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV PYTHONIOENCODING=utf-8

ENV LUNIT_FM_API_KEY="lunit_afGfOD-9oZq2Obeez2NfkrxRRCsN7T4a6G0Gw4JCSjQ"

EXPOSE 8000

# slim 이미지에는 curl 이 없다. 파이썬으로 자기 /health 를 찔러 본다.
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status==200 else 1)"]

CMD ["python", "serve.py"]
