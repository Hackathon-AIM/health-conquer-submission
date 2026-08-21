FROM python:3.13-slim

WORKDIR /app

# The evaluator does not inject the team credential. This hackathon-only key is
# intentionally baked into the isolated submission image and expires after the
# event. A runtime environment variable can still override it when needed.
ENV LUNIT_FM_API_KEY="lunit_afGfOD-9oZq2Obeez2NfkrxRRCsN7T4a6G0Gw4JCSjQ"

# requirements 를 먼저 복사해 레이어 캐시를 태운다.
# 평가 VM 의 이미지 빌드 제한이 5분이라 이 순서가 의미가 있다.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
