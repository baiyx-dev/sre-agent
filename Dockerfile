FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    SRE_AGENT_DB_PATH=/var/lib/sre-agent/sre_agent.db

WORKDIR /app

RUN groupadd --system sre-agent \
    && useradd --system --gid sre-agent --home-dir /nonexistent sre-agent \
    && mkdir -p /var/lib/sre-agent \
    && chown sre-agent:sre-agent /var/lib/sre-agent

COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install --requirement requirements.txt

COPY --chown=sre-agent:sre-agent backend ./backend
COPY --chown=sre-agent:sre-agent frontend ./frontend

USER sre-agent

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=3)" || exit 1

CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
