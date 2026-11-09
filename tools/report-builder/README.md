# Генератор протоколів ЛР2 і ЛР3

Протоколи `lab2/ЛР2_SOA_КН-61мп_Заєць.docx` і `lab3/ЛР3_SOA_КН-61мп_Заєць.docx` збираються скриптами з цієї теки.
Шаблоном слугує протокол ЛР1 (`lab1/ЛР1_SOA_КН-61мп_Заєць.docx`): з нього беруться титульна сторінка, стилі,
колонтитули й оформлення таблиць, а змінюються лише номер роботи та зміст. Для розгортання лабораторних ця тека
не потрібна.

## Вхідні дані

| Звідки | Що |
|---|---|
| `lab2/docs/test-output`, `lab3/docs/test-output` | збережений вивід `scripts/demo.py`, `scaling_demo.py`, `saga_demo.py` і журнали сервісів |
| `lab2/docs/screenshots`, `lab3/docs/screenshots` | знімки Consul, Traefik і RabbitMQ, зроблені `capture_ui.mjs` на запущеному стеку |
| `lab2/services/calendar-service/Dockerfile` | лістинг Dockerfile у протоколі ЛР2 |

## Порядок збирання

```bash
node draw_figures.mjs                                  # схеми -> lab2/docs/diagrams, lab3/docs/diagrams
python3 render_terminals.py && node capture_terminals.mjs   # вивід перевірок -> build/terminals/*.png
python3 crop_screens.py                                # фрагменти скріншотів -> build/screens
python3 lab2_report.py && python3 lab3_report.py       # протоколи -> lab2/, lab3/
```

Потрібні Python 3.10+ з `python-docx` і `Pillow`, Node.js з `playwright` та встановлений Google Chrome
(шляхи до Chrome і модуля playwright задано на початку `.mjs`-файлів). Для перевірки верстки DOCX можна
конвертувати в PDF через LibreOffice; результат кладеться в `render/`, яка до репозиторію не входить.
