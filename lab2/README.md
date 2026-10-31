# ЛР2. Оркестрація та хореографія мікросервісів календаря

Реалізація контекстів, спроєктованих у ЛР1 (варіант 4 — хмарний календар користувача):

| Сервіс | Контекст ЛР1 | Власна БД | Що робить |
|---|---|---|---|
| `calendar-service` | Планування календаря | `calendar-db` | календарі, події, версії; координатор перенесення |
| `participation-service` | Участь у подіях | `participation-db` | запрошення та відповіді адресатів |
| `notification-service` | Нагадування та сповіщення | `notification-db` | нагадування, листи, налаштування каналів |

Інфраструктура: **Traefik** (API-шлюз), **Consul** (реєстр сервісів і перевірки стану),
**RabbitMQ** (брокер повідомлень, quorum-черги з DLQ), **PostgreSQL** (окремий екземпляр на кожен сервіс).

## Запуск

```bash
docker compose up -d --build
python3 scripts/demo.py                 # наскрізна перевірка: 15 автоматичних перевірок
python3 scripts/scaling_demo.py all     # масштабування, балансування, відмова екземпляра, Circuit Breaker
docker compose down -v                  # зупинка з видаленням даних
```

| Адреса | Призначення |
|---|---|
| http://localhost:8080/api/calendar/docs | OpenAPI сервісу календаря (так само `/api/participation/docs`, `/api/notification/docs`) |
| http://localhost:8081 | панель Traefik |
| http://localhost:8500 | інтерфейс Consul |
| http://localhost:15672 | RabbitMQ Management (`calendar` / `calendar`) |

Порти мікросервісів назовні не відкриваються: клієнт звертається лише до шлюзу.
Ідентифікація користувача до ЛР5 імітується заголовком `X-User-Id`.

## Взаємодія

* **Синхронно (REST через Consul):** `GET /api/calendar/diagnostics/chain?message=hello` — кожен сервіс дописує свій
  фрагмент і викликає наступний; `GET /api/calendar/events/{id}/overview` — композиція даних трьох сервісів;
  `PATCH /api/calendar/events/{id}` зі зміною часу — **оркестрація** перенесення координатором у `calendar-service`.
* **Асинхронно (RabbitMQ):** `EventCreated.v1`, `EventUpdated.v1`, `EventCancelled.v1` (обмінник `calendar.events`)
  та `InvitationCreated/Responded/Revoked.v1` — **хореографія** створення й скасування події.

## Масштабування

```bash
docker compose up -d --scale calendar-service=3 --scale participation-service=3
```

Кожен новий екземпляр сам реєструється в Consul; Traefik підхоплює його з каталогу (серверне балансування),
сервіси-клієнти обирають екземпляр по колу (клієнтське балансування), а черга RabbitMQ розподіляє
повідомлення між споживачами.

## Структура

```
common/soa_common/      спільні механізми: Consul, балансування + Circuit Breaker, RabbitMQ, PostgreSQL, HTTP
services/<name>/        Dockerfile та код сервісу (domain.py, store.py, main.py, schema.sql)
docs/openapi/           експортовані контракти OpenAPI
scripts/                демонстраційні та перевірочні сценарії
```
