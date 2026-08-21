FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    LUNIT_FM_API_URL=https://model.hackathon.lunit.io \
    LUNIT_FM_MODEL=Lunit/L2-preview \
    LUNIT_FM_API_KEY= \
    MEDIBOT_FINAL_MODEL_PROVIDER=lunit_l2 \
    MEDIBOT_REQUIRE_L2_FINAL=1 \
    MEDIBOT_ALLOW_FALLBACK=0 \
    MEDIBOT_RAG_BACKEND=lunit_mcp

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY prompts ./prompts

EXPOSE 8000

CMD ["uvicorn", "medibot.api.server:app", "--host", "0.0.0.0", "--port", "8000"]
