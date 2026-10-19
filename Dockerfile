FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TELCONET_PROJECT_ROOT=/app \
    TELCONET_INTENT=/app/lab/intent.yml \
    TELCONET_CANDIDATE_INTENT=/app/lab/intent-dual-homed.yml \
    TELCONET_DESIGN_CATALOG=/app/lab/designs.yml \
    TELCONET_FAILURE_DOMAINS=/app/lab/failure-domains.yml \
    TELCONET_EXPERIMENT=/app/evidence/bfd-comparison.json \
    TELCONET_REPEATED_EXPERIMENT=/app/evidence/bfd-repeated-trials.json \
    TELCONET_RUNTIME_N1_EVIDENCE=/app/evidence/runtime-n1-dashboard.json

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY lab/designs.yml ./lab/designs.yml
COPY lab/failure-domains.yml ./lab/failure-domains.yml
COPY lab/intent.yml ./lab/intent.yml
COPY lab/intent-dual-homed.yml ./lab/intent-dual-homed.yml
COPY lab/telconet-dual-homed.clab.yml ./lab/telconet-dual-homed.clab.yml
COPY lab/frr ./lab/frr
COPY evidence/bfd-comparison.json ./evidence/bfd-comparison.json
COPY evidence/bfd-repeated-trials.json ./evidence/bfd-repeated-trials.json
COPY evidence/runtime-n1-dashboard.json ./evidence/runtime-n1-dashboard.json

RUN pip install --no-cache-dir . \
    && install -d -o 65532 -g 65532 /var/lib/telconet

USER 65532:65532

EXPOSE 8000

CMD ["uvicorn", "telconet_sentinel.main:app", "--host", "0.0.0.0", "--port", "8000"]
