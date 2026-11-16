# syntax=docker/dockerfile:1
# Один образ для MCP-сервера, A2A-агентів і консольних клієнтів; роль задає команда в docker-compose.yml.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

COPY agents/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

# soa_common: реєстрація в Consul і шина RabbitMQ — ті самі, що в сервісах ЛР2–ЛР3.
COPY common/soa_common ./soa_common
COPY agents/calendar_ai ./calendar_ai
COPY agents/tests ./tests

RUN useradd --system --uid 10001 --no-create-home app
USER app

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-m", "soa_common.healthcheck"]

CMD ["python", "-m", "calendar_ai.mcp_server", "http"]
