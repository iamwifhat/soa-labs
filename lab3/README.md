# ЛР3. Транзакційність: Transactional Outbox/Inbox і SAGA

Протокол: [ЛР3_SOA_КН-61мп_Заєць.docx](ЛР3_SOA_КН-61мп_Заєць.docx).

Розвиток системи з ЛР2 (ті самі три сервіси, шлюз Traefik, Consul, RabbitMQ, окремі PostgreSQL).
Синхронну оркестрацію перенесення з ЛР2 замінено асинхронною SAGA з компенсаціями.

## Що додано

| Механізм | Де | Суть |
|---|---|---|
| Transactional Outbox | `common/soa_common/outbox.py`, усі сервіси-продюсери | повідомлення записується в таблицю `outbox` у тій самій транзакції, що й зміна агрегату; фоновий ретранслятор публікує його в RabbitMQ |
| Transactional Inbox | `participation-service`, `notification-service`, відповіді в `calendar-service` | `messageId` записується в таблицю `inbox` у транзакції обробки; повтор відсіюється |
| SAGA (оркестрація) | `services/calendar-service/app/saga.py` | `CreateEvent` і `RescheduleEvent`; журнал кроків у `sagas` / `saga_log` |
| Відкат по DLQ | `MessageBus.consume_dead_letters` | календар слухає `participation.calendar-events.dlq` і компенсує свою транзакцію |
| Відкат по таймауту | `SagaCoordinator.expire_loop` | `SAGA_TIMEOUT_SECONDS` (30 с) без відповіді учасника — компенсація |
| Семантичне блокування | `Event.saga_id`, статус `Pending` | поки SAGA не завершена, подію не можна змінити (HTTP 409) |
| Config service | Consul KV `config/participation-service/fault-mode` | керована відмова учасника: `none`, `error`, `delay` |

## Запуск

```bash
docker compose up -d --build
python3 scripts/saga_demo.py     # 7 сценаріїв, 12 автоматичних перевірок (~2 хв)
docker compose down -v
```

ЛР2 і ЛР3 використовують ті самі порти (8080, 8081, 8500, 15672), тому одночасно запускається лише одна з них.

Режим збою вмикається без перезапуску сервісів:

```bash
curl -X PUT localhost:8500/v1/kv/config/participation-service/fault-mode -d error
```

## Потік SAGA CreateEvent

1. `POST /api/calendar/events` → подія `Pending`, рядок `sagas`, `EventCreated.v1` в outbox (одна транзакція) → HTTP 202.
2. `participation-service` створює запрошення і записує `InvitationsCreated.v1` у свій outbox.
3. Календар підтверджує подію (`Scheduled`) і публікує `EventConfirmed.v1`; лише тоді сервіс сповіщень надсилає листи.
4. Збій учасника: 4 спроби → DLQ → календар скасовує подію (`EventCancelled.v1`, reason `SagaCompensation`).
   Немає відповіді 30 с → та сама компенсація; запізнілу відповідь координатор ігнорує.

Стан SAGA та її журнал: `GET /api/calendar/sagas/{sagaId}`.

## Структура

```
common/soa_common/      механізми ЛР2 + outbox.py (Outbox, Inbox, ретранслятор) і config.py (Consul KV)
services/<name>/        Dockerfile та код сервісу; координатор SAGA — services/calendar-service/app/saga.py
scripts/saga_demo.py    сценарій перевірки
docs/openapi/           експортовані контракти OpenAPI
docs/diagrams/          схеми з протоколу (PNG і SVG)
docs/test-output/       вивід saga_demo.py і журнали сервісів, наведені в протоколі
docs/screenshots/       знімок черг RabbitMQ
```
