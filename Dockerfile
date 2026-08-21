FROM python:3.13-slim

WORKDIR /app

# requirements 를 먼저 복사해 레이어 캐시를 태운다.
# 평가 VM 의 이미지 빌드 제한이 5분이라 이 순서가 의미가 있다.
COPY requirements-serve.txt .
RUN pip install --no-cache-dir -r requirements-serve.txt

COPY . .

EXPOSE 8000

# slim 이미지에는 curl 이 없다. 파이썬으로 자기 /health 를 찔러 본다.
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status==200 else 1)"]

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
