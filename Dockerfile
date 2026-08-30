FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TELCONET_INTENT=/app/lab/intent.yml

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY lab/intent.yml ./lab/intent.yml

RUN pip install --no-cache-dir .

USER 65532:65532

EXPOSE 8000

CMD ["uvicorn", "telconet_sentinel.main:app", "--host", "0.0.0.0", "--port", "8000"]

