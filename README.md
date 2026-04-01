# billmanager_parser

Автономный Python-сервис парсинга тарифных планов от BILLmanager-провайдеров. Работает независимо от `cloudsell_api`, самостоятельно определяет изменения и отправляет данные через `POST /v1/pricing-plans/sync`.

## Архитектура

```
BILLmanager API
      │
      ▼
parser/           — получает и парсит прайслист и список ОС
      │
      ▼
snapshot/         — сравнивает SHA-256 от raw JSON, пропускает неизменившиеся планы
      │
      ▼
mapper/           — маппит features (быстрый парсер → LLM-фолбэк), цены, локацию
      │
      ▼
api_client/       — отправляет данные в cloudsell_api
      │
      ▼
cloudsell_api     POST /v1/pricing-plans/sync
```

## Компоненты

### `parser/`
- `client.py` — `BillManagerClient`: async HTTP-клиент к BILLmanager, методы `fetch_pricelist()` и `fetch_os_list()`. Retry через tenacity (3 попытки, exponential backoff).
- `plans.py` — `parse_pricelist(raw_bytes)`: парсит JSON-ответ BILLmanager, возвращает `list[ParsedPlan]`. Пропускает планы с нулевой ценой.
- `os_list.py` — `parse_os_list(raw_bytes, plan_id)`: парсит список ОС для плана. Автоопределяет семейство ОС из названия если `$valuegroup` пустое.
- `models.py` — Pydantic-модели для raw BILLmanager JSON: `ParsedPlan`, `ParsedOS`, `ParsedPrice`.

### `mapper/`
- `features.py` — `map_features()`: сначала пробует быстрый детерминированный парсер (regex по полям detail). Если cores/ram/disk не найдены — вызывает LLM.
- `llm.py` — `extract_features_with_llm()`: Gemini через OpenAI-совместимый эндпоинт Google (`https://generativelanguage.googleapis.com/v1beta/openai`). `temperature=0`, HTML очищается через BeautifulSoup. Возвращает `ServerFeatures`.
- `prices.py` — `map_prices()`: фильтрует периоды `{1,3,6,12}`, дедупликация, дропает нулевые цены.
- `location.py` — `resolve_location()`: 1060+ ключей маппинга локаций (перенесено из `cloudsell_api/gateways/providers/mapings.py`). Сначала точное совпадение, затем поиск по подстроке.

### `snapshot/`
- `store.py` — `SnapshotStore`: хранит SHA-256 хэш от canonical JSON `{plan_id, prices, detail}` в файлах `{snapshot_dir}/{provider_host}/{plan_id}.json`. `has_changed()` сравнивает хэш, `save()` обновляет.

### `api_client/`
- `cloudsell.py` — `CloudsellClient`: async HTTP-клиент к cloudsell_api. Заголовок `X-Service-Key`. Методы:
  - `ensure_os_family(family_name)` — POST /v1/os/families, принимает 200/201/409
  - `ensure_os(...)` — POST /v1/os, при 409 fallback на GET /v1/os/providers/{provider_id}
  - `sync_plans(...)` — POST /v1/pricing-plans/sync

### `orchestrator.py`
Основная логика обработки на провайдер:
1. Получить прайслист из BILLmanager
2. Распарсить → `list[ParsedPlan]`
3. Сравнить снапшоты, отобрать изменившиеся планы
4. Для каждого изменившегося: получить ОС, обеспечить их существование в API, замапить features и цены
5. Отправить `POST /pricing-plans/sync` с полным списком активных external_id + payload изменившихся планов
6. Сохранить снапшоты (только после успешного sync)

### `main.py`
APScheduler `AsyncIOScheduler` с cron-триггером. При старте выполняет парсинг немедленно, затем по расписанию. Graceful shutdown по SIGTERM/SIGINT.

## Конфигурация

Все настройки через `.env` (pydantic-settings):

```env
# Gemini (OpenAI-compatible endpoint)
GEMINI_API_KEY=AIza...
GEMINI_MODEL=gemini-2.5-flash

# Cloudsell API
CLOUDSELL_API_URL=http://localhost:8000
CLOUDSELL_SERVICE_KEY=your-secret-service-key

# Провайдеры (вложенные переменные)
PROVIDERS__0__PROVIDER_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
PROVIDERS__0__BASE_URL=https://billing.firstbyte.ru
PROVIDERS__0__USERNAME=your_user
PROVIDERS__0__PASSWORD=your_password

PROVIDERS__1__PROVIDER_ID=yyyyyyyy-yyyy-yyyy-yyyy-yyyyyyyyyyyy
PROVIDERS__1__BASE_URL=https://billing.datacheap.ru
PROVIDERS__1__USERNAME=your_user
PROVIDERS__1__PASSWORD=your_password

# Планировщик
PARSE_CRON=0 2 * * *

# Хранилище снапшотов
SNAPSHOT_DIR=snapshots

# Таймауты (секунды)
PROVIDER_TIMEOUT=60.0
API_TIMEOUT=30.0
```

`PROVIDER_ID` — UUID провайдера из БД `cloudsell_api`, берётся напрямую из конфига.

## Запуск

### Docker Compose (рекомендуется)

```bash
cp .env.example .env
# Заполнить .env

docker compose up -d
docker compose logs -f
```

Снапшоты сохраняются в `./snapshots/` (volume mount, переживают перезапуск контейнера).

### Локально

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
python main.py
```

## Логирование

Структурированные JSON-логи через structlog. Контекстные переменные:
- `provider` — base_url текущего провайдера
- `plan_id` — external_id текущего плана

Пример:
```json
{"event": "Plans change summary", "total": 42, "changed": 3, "unchanged": 39, "provider": "https://billing.firstbyte.ru"}
{"event": "Plan payload built", "plan_id": 12345, "cores": 4, "ram": "8", "disk": "100", "prices_count": 3, "os_count": 5}
{"event": "Plans synced", "provider_id": "uuid", "created": 2, "deactivated": 1}
```

## Взаимодействие с cloudsell_api

Сервис использует три эндпоинта API:

| Метод | Путь | Авторизация | Назначение |
|-------|------|-------------|------------|
| POST | `/v1/os/families` | `X-Service-Key` | Создать семейство ОС |
| POST | `/v1/os` | `X-Service-Key` | Создать ОС провайдера |
| POST | `/v1/pricing-plans/sync` | `X-Service-Key` | Синхронизировать тарифы |

Синхронизация тарифов (`/sync`) атомарна: деактивирует устаревшие планы и создаёт новые в одной транзакции.

## Изменения в cloudsell_api

В рамках этого проекта в `cloudsell_api` были сделаны:
- Добавлен `POST /v1/pricing-plans/sync` с авторизацией через `X-Service-Key`
- Добавлен `ServiceKeyDep` в `deps.py` (читает `PARSER_SERVICE_KEY` из env)
- Добавлена `PricingPlanSyncRequest` схема
- Добавлен метод `PricingPlanService.sync()`
- Удалена cron-задача `parse_pricing_plans` из worker
- Удалён `PricingPlansParser` из DI-контейнера
