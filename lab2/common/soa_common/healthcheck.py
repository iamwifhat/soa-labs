"""Перевірка стану для HEALTHCHECK у Dockerfile: код виходу 0, якщо /health відповідає 200."""
import sys
import urllib.request

try:
    urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=2)
except Exception:  # noqa: BLE001 - будь-яка помилка означає несправний контейнер
    sys.exit(1)
