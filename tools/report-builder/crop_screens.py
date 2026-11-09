"""Обрізає скріншоти з docs/screenshots до фрагментів, що вставляються у звіти (build/screens)."""
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = HERE / "build" / "screens"

# Рамки в пікселях знімка (масштаб 2×): прибрано бічне меню, порожні поля та колонки швидкостей.
CROPS = {
    "lab2-consul-instances.png": ("lab2", "consul-instances.png", (640, 20, 2470, 920)),
    "lab2-traefik-service.png": ("lab2", "traefik-service.png", (0, 0, 1720, 1420)),
    "lab2-rabbitmq-queues.png": ("lab2", "rabbitmq-queues.png", (50, 595, 1720, 930)),
    "lab3-rabbitmq-queues.png": ("lab3", "rabbitmq-queues.png", (50, 595, 1770, 1035)),
}

if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for target, (lab, source, box) in CROPS.items():
        with Image.open(ROOT / lab / "docs" / "screenshots" / source) as image:
            image.crop(box).save(OUT / target)
        print(target)
