FROM python:3.13-slim

WORKDIR /app

# requirements 를 먼저 복사해 레이어 캐시를 태운다.
# 평가 VM 의 이미지 빌드 제한이 5분이라 이 순서가 의미가 있다.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
