# The arena builds this file with the repository root as the build context:
#   docker build -t mech-bot .
# It runs with no network, a read-only filesystem, 1 CPU, 512 MB and uid 65534.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY bot/ /app/

USER 65534:65534

ENTRYPOINT ["python", "-u", "/app/main.py"]
