# Генератор протоколів ЛР2–ЛР4

Протоколи `lab2/ЛР2_SOA_КН-61мп_Заєць.docx`, `lab3/ЛР3_SOA_КН-61мп_Заєць.docx` і `lab4/ЛР4_SOA_КН-61мп_Заєць.docx`
збираються скриптами з цієї теки. Шаблоном слугує протокол ЛР1 (`lab1/ЛР1_SOA_КН-61мп_Заєць.docx`): з нього беруться
титульна сторінка, стилі, колонтитули й оформлення таблиць, а змінюються лише номер роботи та зміст. Для розгортання
лабораторних ця тека не потрібна.

## Вхідні дані

| Звідки | Що |
|---|---|
| `labN/docs/test-output` | збережений вивід перевірочних сценаріїв і журнали сервісів; протокол ЛР4 бере з нього й числа для таблиць |
| `lab2/docs/screenshots`, `lab3/docs/screenshots` | знімки Consul, Traefik і RabbitMQ, зроблені `capture_ui.mjs` на запущеному стеку |
| `lab4/docs/screenshots` | знімок вебінтерфейсу MCP Inspector (`capture_ui.mjs … inspector`) |
| `lab2/services/calendar-service/Dockerfile` | лістинг Dockerfile у протоколі ЛР2 |

## Порядок збирання

```bash
node draw_figures.mjs [lab4]                           # схеми -> labN/docs/diagrams (аргумент — лише одна ЛР)
python3 render_terminals.py && node capture_terminals.mjs   # вивід перевірок -> build/terminals/*.png
python3 crop_screens.py                                # фрагменти скріншотів ЛР2–ЛР3 -> build/screens
INSPECTOR_URL='http://127.0.0.1:6274?MCP_INSPECTOR_API_TOKEN=…' \
    node capture_ui.mjs ../../lab4/docs/screenshots inspector   # потрібен запущений MCP Inspector
python3 lab2_report.py && python3 lab3_report.py && python3 lab4_report.py   # протоколи -> labN/
```

Потрібні Python 3.10+ з `python-docx` і `Pillow`, Node.js з `playwright` та встановлений Google Chrome
(шляхи до Chrome і модуля playwright задано на початку `.mjs`-файлів). Для перевірки верстки DOCX можна
конвертувати в PDF через LibreOffice; результат кладеться в `render/`, яка до репозиторію не входить.
