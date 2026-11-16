# ЛР4. Інтеграція сервісних API з AI-агентами: function calling, MCP, A2A

Протокол: [ЛР4_SOA_КН-61мп_Заєць.docx](ЛР4_SOA_КН-61мп_Заєць.docx).

Система ЛР3 (три сервіси календаря, Traefik, Consul, RabbitMQ, Outbox/Inbox, SAGA) без змін у механізмах;
додано лише `GET /api/calendar/calendars` — список календарів користувача, без якого агенту нема з чого почати.
Поверх REST API працюють три AI-інтеграції з тек `agents/` і `scripts/`.

| Завдання | Що зроблено | Де |
|---|---|---|
| 1. Function calling | LLM користується REST-клієнтом ЛР3 через 10 інструментів; цикл агента з ланцюжками викликів | `agents/calendar_ai/fc_client.py`, `tools.py`, `agent.py` |
| 1.1 Токени | `max_tokens` на виклик, бюджет сесії з оцінкою до виклику, ліміти кроків і викликів, обрізання й проєкції відповідей, частота запитів | `guardrails.py` (`TokenBudget`, `clip`), `llm.py` |
| 1.2 Ланцюжок | «Онови подію …; якщо її немає — створи»: `list_my_calendars → list_events → update_event / create_event → get_saga_status` | `scripts/fc_demo.py` |
| 1.3 Prompt injection | фільтр запиту, вилучення інструкцій із даних інструментів (spotlighting), політика деструктивних дій, валідація аргументів Pydantic | `guardrails.py`, `tools.py` |
| 2. MCP | сервер REST API: 12 tools з анотаціями, 6 resources, 3 prompts; stdio і Streamable HTTP за шлюзом; MCP-хост з LLM | `agents/calendar_ai/mcp_server.py`, `mcp_host.py` |
| 3. A2A | продюсер ЛР3 як агент-планувальник (навички `schedule_event`, `reschedule_event`); споживач ЛР3 як асистент учасника, що слухає RabbitMQ і домовляється з продюсером через A2A | `producer_agent.py`, `consumer_agent.py`, `a2a_common.py` |

## Запуск

Потрібні Docker з Compose v2 і ключ будь-якого OpenAI-сумісного API (за замовчуванням Google Gemini,
ключ: https://aistudio.google.com/apikey).

```bash
cp .env.example .env                 # вписати LLM_API_KEY; .env не потрапляє в git
docker compose up -d --build         # система ЛР3 + mcp-server, producer-agent, consumer-agent
docker compose run --rm agent-cli python -m pytest -q tests          # 27 тестів guardrails без LLM
docker compose run --rm agent-cli python scripts/mcp_check.py        # MCP без LLM: 24 перевірки
docker compose run --rm agent-cli python scripts/fc_demo.py          # завдання 1: 11 перевірок
docker compose run --rm agent-cli python scripts/a2a_demo.py         # завдання 3
docker compose down -v
```

Окремі запити:

```bash
docker compose run --rm agent-cli python -m calendar_ai.fc_client --user artem@pm.me "Покажи мої календарі"
docker compose run --rm agent-cli python -m calendar_ai.mcp_host --url http://gateway/mcp "Склади порядок денний"
```

Порти на хості на 1000 більші, ніж у ЛР3, тож ЛР2–ЛР4 можна запускати одночасно. Виняток — шлюз: 10088, бо порт
10080 входить до списку заборонених портів стандарту Fetch, і браузер, Node.js та MCP Inspector до нього не підключаються.

| Адреса | Призначення |
|---|---|
| http://localhost:10088/api/calendar/docs | OpenAPI сервісу календаря |
| http://localhost:10088/mcp | MCP-сервер (Streamable HTTP), заголовок `X-User-Id` |
| http://localhost:10088/a2a/producer/.well-known/agent-card.json | Agent Card продюсера |
| http://localhost:10088/a2a/consumer/.well-known/agent-card.json | Agent Card споживача |
| http://localhost:10081 | панель Traefik |
| http://localhost:10500 | Consul: сервіси й агенти з `protocol=mcp/a2a` у метаданих |
| http://localhost:17672 | RabbitMQ (`calendar` / `calendar`) |

## MCP Inspector

```bash
npx @modelcontextprotocol/inspector@2.12.0 --web --transport http \
    --server-url http://localhost:10088/mcp --header "X-User-Id: artem@pm.me" --protocol-era auto
npx @modelcontextprotocol/inspector@2.12.0 --cli http://localhost:10088/mcp --transport http \
    --header "X-User-Id: artem@pm.me" --method tools/list
```

Для stdio (Claude Desktop, Inspector з командою) сервер запускається як підпроцес; користувач задається змінною:

```json
{"mcpServers": {"calendar": {"command": "docker", "args": ["compose", "-f", "/шлях/до/lab4/docker-compose.yml",
  "run", "--rm", "-i", "-e", "CALENDAR_USER_ID=artem@pm.me", "agent-cli",
  "python", "-m", "calendar_ai.mcp_server", "stdio"]}}}
```

## Guardrails

| Ризик | Захист |
|---|---|
| Витрати токенів | `LLM_MAX_OUTPUT_TOKENS`, `LLM_SESSION_TOKEN_BUDGET` (резерв до виклику, облік `usage` після, токени міркувань Gemini — з `total_tokens`), `AGENT_MAX_STEPS`, `AGENT_MAX_TOOL_CALLS`, обрізання відповіді інструмента, `LLM_MAX_RPM` і пауза з `retry in N s` при 429 |
| Пряма prompt injection | `check_user_input`: шаблони EN/UK/RU, нормалізація Unicode і прибирання невидимих символів; відхилення до виклику моделі |
| Непряма prompt injection | відповіді API позначені `trust: untrusted-data`; підозрілі рядки замінюються маркером, шлях поля видно в `guardrail`; назви-інструкції не зберігаються через MCP |
| Деструктивні дії | `cancel_event` лише за явним наміром у запиті користувача і після підтвердження людиною (термінал, MCP elicitation) |
| Аргументи | Pydantic `extra=forbid`: UUID, ISO 8601 зі зміщенням, IANA-пояс, email, довжини, подія в майбутньому і не довша 12 год |
| Ідентичність | користувач — з `X-User-Id`/`CALENDAR_USER_ID`, агент — з `X-Agent-Key`, а не з аргументів моделі; MCP-ресурс чужих календарів недоступний |
| Протокол MCP | rate limit 60 викликів/хв на користувача, аудит кожного `tools/call`, захист від DNS rebinding (Host/Origin) |
| A2A | ключ агента в Agent Card; учасник може просити перенести лише свою подію; ліміт переговорів (1 пропозиція споживача, 2 запити до продюсера на подію) |

## Структура

```
agents/calendar_ai/     REST-клієнт, guardrails, інструменти, цикл агента, MCP-сервер і хост, A2A-агенти
agents/tests/           модульні тести guardrails і циклу агента (сценарна модель замість LLM)
agents/Dockerfile       один образ для mcp-server, producer-agent, consumer-agent і agent-cli
common/, services/      система ЛР3 (+ GET /calendars)
scripts/                fc_demo.py, mcp_check.py, a2a_demo.py, saga_demo.py (регресія ЛР3)
docs/test-output/       вивід перевірок, наведений у протоколі
docs/diagrams/          схеми з протоколу
docs/screenshots/       MCP Inspector
```
