# Інтелектуальні сервіс-орієнтовані системи — лабораторні роботи

Заєць Євген Анатолійович, група КН-61мп. Викладач — Гірянський Богдан Петрович.
Варіант 4 — хмарна інформаційна система для календаря користувача.

Кожна лабораторна — окрема тека з протоколом (DOCX), кодом і інструкцією з розгортання. Лабораторні послідовні:
ЛР1 проєктує систему, ЛР2 її реалізує, ЛР3 розвиває код ЛР2, ЛР4 відкриває API системи ЛР3 для AI-агентів.

| № | Тема | Тека | Протокол | Стан |
|---|---|---|---|---|
| 1 | Декомпозиція сервісних систем та Domain Discovery | [lab1](lab1) | [ЛР1](lab1/ЛР1_SOA_КН-61мп_Заєць.docx) | готово; проєктна робота, без коду |
| 2 | Оркестрація та хореографія мікросервісів | [lab2](lab2) | [ЛР2](lab2/ЛР2_SOA_КН-61мп_Заєць.docx) | готово |
| 3 | Забезпечення транзакційності: Transactional Outbox і SAGA | [lab3](lab3) | [ЛР3](lab3/ЛР3_SOA_КН-61мп_Заєць.docx) | готово |
| 4 | Інтеграція сервісних API з AI-агентами: function calling, MCP, A2A | [lab4](lab4) | [ЛР4](lab4/ЛР4_SOA_КН-61мп_Заєць.docx) | готово |
| 5 | Безпека API | lab5 | — | буде додано до здачі |
| 6 | Спостережуваність і AIOps | lab6 | — | буде додано до здачі |

## Розгортання

Потрібні Docker з Compose v2 (Docker Desktop) і Python 3.10+ для перевірочних скриптів ЛР2–ЛР3; для ЛР4 —
ключ OpenAI-сумісного LLM API (за замовчуванням Gemini) у файлі `lab4/.env`. ЛР2 займає на хості порти 8080, 8081,
8500 і 15672, ЛР3 — на 1000 більші (9080, 9081, 9500, 16672), ЛР4 — ще на 1000 більші (10081, 10500, 17672), крім
шлюзу 10088: порт 10080 браузери і Node.js блокують як небезпечний. Усі три лабораторні можна запускати одночасно.

```bash
cd lab2
docker compose up -d --build        # шлюз, Consul, RabbitMQ, три сервіси та їхні БД
python3 scripts/demo.py             # 15 автоматичних перевірок
python3 scripts/scaling_demo.py all # масштабування, балансування, відмова екземпляра, Circuit Breaker
docker compose down -v
```

```bash
cd lab3
docker compose up -d --build
python3 scripts/saga_demo.py        # Outbox, Inbox, SAGA з компенсацією по DLQ і таймауту: 12 перевірок
docker compose down -v
```

```bash
cd lab4
cp .env.example .env                # вписати LLM_API_KEY
docker compose up -d --build        # система ЛР3 + MCP-сервер і два A2A-агенти
docker compose run --rm agent-cli python scripts/mcp_check.py   # MCP без LLM: 24 перевірки
docker compose run --rm agent-cli python scripts/fc_demo.py     # function calling і guardrails: 11 перевірок
docker compose run --rm agent-cli python scripts/a2a_demo.py    # продюсер і споживач як A2A-агенти: 11 перевірок
docker compose down -v
```

| Що | ЛР2 | ЛР3 | ЛР4 |
|---|---|---|---|
| API (Swagger) | http://localhost:8080/api/calendar/docs | http://localhost:9080/api/calendar/docs | http://localhost:10088/api/calendar/docs |
| Consul | http://localhost:8500 | http://localhost:9500 | http://localhost:10500 |
| Traefik | http://localhost:8081 | http://localhost:9081 | http://localhost:10081 |
| RabbitMQ (`calendar` / `calendar`) | http://localhost:15672 | http://localhost:16672 | http://localhost:17672 |
| MCP-сервер | — | — | http://localhost:10088/mcp |
| Agent Cards (A2A) | — | — | http://localhost:10088/a2a/producer/.well-known/agent-card.json |

Докладніше — у README кожної теки.

## Структура

```
lab1/                    протокол ЛР1 і схеми проєктування
lab2/                    код ЛР2, docker-compose.yml, протокол, docs/ (OpenAPI, схеми, вивід перевірок, скріншоти)
lab3/                    код ЛР3 (розвиток ЛР2), docker-compose.yml, протокол, docs/
lab4/                    система ЛР3 + agents/ (function calling, MCP-сервер, A2A-агенти), протокол, docs/
tools/report-builder/    генератор протоколів ЛР2–ЛР4 з DOCX ЛР1 як шаблону; для розгортання не потрібен
```
