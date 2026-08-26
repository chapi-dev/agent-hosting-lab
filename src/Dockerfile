# Self-hosted image.
#
# Compare this file with src/hosted/, which has no Dockerfile at all. Under the
# hosted model the platform builds and ships the image; here it is yours to
# write, pin, patch and rebuild every time a CVE lands in the base layer.

FROM mcr.microsoft.com/devcontainers/python:3.12-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    STATE_DIR=/tmp/sessions

WORKDIR /app

COPY selfhosted/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY agent_core ./agent_core
COPY selfhosted ./selfhosted

# Non-root because the platform will not do it for you.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /tmp/sessions \
    && chown -R appuser /app /tmp/sessions
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"

CMD ["uvicorn", "selfhosted.server:app", "--host", "0.0.0.0", "--port", "8000"]
