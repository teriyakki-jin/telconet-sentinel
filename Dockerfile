FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TELCONET_INTENT=/app/lab/intent.yml \
    TELCONET_EXPERIMENT=/app/evidence/bfd-comparison.json \
    TELCONET_REPEATED_EXPERIMENT=/app/evidence/bfd-repeated-trials.json

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY lab/intent.yml ./lab/intent.yml
COPY evidence/bfd-comparison.json ./evidence/bfd-comparison.json
COPY evidence/bfd-repeated-trials.json ./evidence/bfd-repeated-trials.json

RUN pip install --no-cache-dir .

USER 65532:65532

EXPOSE 8000

CMD ["uvicorn", "telconet_sentinel.main:app", "--host", "0.0.0.0", "--port", "8000"]
