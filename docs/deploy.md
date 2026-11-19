# Деплой на сервер (Ubuntu 24.04)

Продовая конфигурация: три контейнера Docker Compose.

| Контейнер | Назначение | Образ |
|---|---|---|
| `vk_comments` | Приложение: поиск → фильтры → LLM → Telegram | `vk_comments:latest` (сборка из `Dockerfile`) |
| `Postgres_vk_comm` | База данных | `postgres:17-alpine` |
| `vk_comments_redis` | Блокировки циклов | `redis:7-alpine` |

> **Почему три контейнера, а не два.** Приложению нужен Redis: на нём
> построены распределённые блокировки (`cycle_lock` в
> `app/db/redis_client.py`) — они не дают двум циклам мониторинга работать
> одновременно (ТЗ п.5.1). Без Redis сервис не запустит мониторинг.

## Требования к серверу

| Параметр | Минимум | Проверено на |
|---|---|---|
| ОС | Ubuntu 22.04+ | Ubuntu 24.04.4 LTS |
| RAM | 2 ГБ свободных | 3.3 ГБ свободных из 7.7 ГБ |
| Диск | 8 ГБ свободных | 25 ГБ (образ ~380 МБ) |
| CPU | 2 ядра | 4 ядра |
| Docker | 24+ | 29.6.2 |
| Docker Compose | v2+ | v5.3.1 |

Порты наружу **не публикуются**: приложение не принимает входящих
соединений, база доступна только внутри сети compose.

## 1. Подготовка сервера

```bash
# Docker (если не установлен)
curl -fsSL https://get.docker.com | sh
docker --version && docker compose version

# Каталог проекта
mkdir -p /opt/vk_comments/logs
cd /opt/vk_comments

# Права на логи: контейнер работает под uid 1000 (appuser),
# без этого будет PermissionError на /app/logs/app.log
chown -R 1000:1000 /opt/vk_comments/logs
```

## 2. Загрузка файлов

С локальной машины (Windows PowerShell):

```powershell
# Собрать архив (исключает .venv, __pycache__, тесты, логи)
.\.venv\Scripts\python.exe scripts/pack_deploy.py

scp vk_deploy.tar.gz root@SERVER_IP:/opt/vk_comments/
ssh root@SERVER_IP "cd /opt/vk_comments && tar -xzf vk_deploy.tar.gz && rm vk_deploy.tar.gz"
```


> **Важно:** не используйте Windows-`tar` с `--exclude='_*.py'` — шаблон
> совпадает с `app/__init__.py` и `app/__main__.py` по сегменту пути, и
> контейнер упадёт с `No module named app.__main__`. `scripts/pack_deploy.py`
> применяет исключения только к локальным служебным файлам.

## 3. Файл `.env`

Секреты берутся из локального `.env` **программно**, а не перепечатыванием —
ручной ввод уже приводил к искажению токенов (лишний символ → HTTP 401).

```powershell
.\.venv\Scripts\python.exe scripts/gen_prod_env.py
scp prod.env root@SERVER_IP:/opt/vk_comments/.env
ssh root@SERVER_IP "chmod 600 /opt/vk_comments/.env"
```

Отличия продового `.env` от локального:

| Переменная | Локально | На сервере |
|---|---|---|
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | `redis://vk_comments_redis:6379/0` |
| `POSTGRES_PASSWORD` | `vk_monitor_pass` | сгенерированный (32 символа) |
| `APP_ENV` | `local` | `production` |
| `DATABASE_URL` | не задаётся | формируется в `docker-compose.prod.yml` |

## 4. Первый запуск

```bash
cd /opt/vk_comments
docker compose -f docker-compose.prod.yml up -d --build
```

Сборка образа занимает 2–3 минуты.

### Применение миграций

Миграции **не** выполняются автоматически при старте контейнера — это
отдельный шаг, чтобы не ронять работающий сервис при обновлении кода:

```bash
docker compose -f docker-compose.prod.yml stop vk_comments
docker compose -f docker-compose.prod.yml run --rm --entrypoint python vk_comments -m alembic upgrade head
docker compose -f docker-compose.prod.yml up -d vk_comments
```

### Проверка

```bash
# Статус контейнеров
docker ps -a --format '{{.Names}} {{.Status}}'

# Окружение: БД, Redis
docker exec vk_comments python -m scripts.check_env

# Плюс реальные запросы к LLM и Telegram
docker exec vk_comments python -m scripts.check_env --external

# Проверка комментариев (can_post из wall.getComments)
docker exec vk_comments python -m scripts.check_comments
```

Ожидаемый результат `check_env --external`:
`Environment is ready: postgres, redis, llm, telegram`.

## 5. Эксплуатация

```bash
cd /opt/vk_comments

# Логи (follow)
docker logs -f vk_comments

# Запустить цикл вне расписания (для проверки)
docker exec -d vk_comments python -m app --once

# Перезапуск
docker compose -f docker-compose.prod.yml restart vk_comments

# Остановка
docker compose -f docker-compose.prod.yml down
```

### Где что лежит

| Путь | Содержимое |
|---|---|
| `/opt/vk_comments/.env` | секреты (права 600) |
| `/opt/vk_comments/config/` | маркеры и фильтры (монтируется в контейнер, правки без пересборки) |
| `/opt/vk_comments/logs/app.log` | логи приложения (ротация 10 МБ × 5) |
| volume `vk-comments_pgdata` | данные PostgreSQL |
| volume `vk-comments_redisdata` | данные Redis |

## 6. Расписание

`Europe/Moscow`, два окна в сутки:

| Окно | Запуски поиска |
|---|---|
| утреннее 10:00–12:00 | **10:00** и **11:00** |
| вечернее 18:00–20:00 | **18:00** и **19:00** |

Дополнительно **отправка очереди уведомлений — каждые 5 минут** (по всему
диапазону суток, включая ретраи недоставленных).

Вне этих окон поиск не выполняется. Пропущенные интервалы внутри окон
восстанавливаются при старте сервиса; промежуток между окнами
восстановлению не подлежит.

Чтобы изменить расписание, отредактируйте `SCHEDULE_HOURS` в
`app/scheduler/runner.py` (требуется пересборка образа).

## 7. Обновление версии

```bash
cd /opt/vk_comments
# загрузить новый архив (см. п.2), распаковать
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml run --rm --entrypoint python vk_comments -m alembic upgrade head
docker compose -f docker-compose.prod.yml restart vk_comments
```

## Известные проблемы и решения

| Симптом | Причина | Решение |
|---|---|---|
| `permission denied: '/app/logs/app.log'` | каталог `logs` принадлежит root, контейнер работает под uid 1000 | `chown -R 1000:1000 /opt/vk_comments/logs` |
| `No module named app.__main__` | архив собран Windows-`tar` с `--exclude='_*.py'`, файлы `app/__*.py` выпали | собирать через `scripts/pack_deploy.py` |
| `relation "alembic_version" does not exist` | миграции не применены | п.4 «Применение миграций» |
| `password authentication failed for user "vk_monitor"` | пароль в `.env` изменён, но volume уже инициализирован старым | пересоздать volume: `docker compose -f docker-compose.prod.yml down` и `docker volume rm vk-comments_pgdata`, затем миграции |
| `playwright install` — exit code 127, `libatomic.so.1` | устаревшая версия образа с браузерной проверкой | обновиться до текущей версии: Playwright и Chromium в проекте больше не используются |
| Публикации с закрытыми комментариями попадают в Telegram | устаревшая версия образа (браузерная проверка всегда возвращала «открыто») | обновиться до текущей версии; проверка идёт через `wall.getComments` → `can_post` |
| `LLM FAILED: HTTP 401` / `Telegram FAILED: 401` | искажённые секреты в `.env` | пересоздать `.env` через `scripts/gen_prod_env.py` |
| Зависшая блокировка `vkmon:production:lock:monitoring` после убийства процесса | цикл убит без graceful shutdown | блокировка истекает за 15 минут (TTL + heartbeat); принудительно: `docker exec vk_comments_redis redis-cli DEL vkmon:production:lock:monitoring` |
| Цикл не успевает за час, следующий запуск пропускается | слишком много публикаций для LLM | снизить `filters.llm.max_posts_per_cycle` или `monitoring.count_per_query` в `config/filters.json` (подхватывается без перезапуска) |
| Записи в `monitoring_runs` висят в `running` | процесс убит посреди цикла | закрываются автоматически при старте следующего цикла (`reap_stale_runs`) |

## Производительность и лимиты

Цикл запускается раз в час, поэтому все этапы должны укладываться в 60 минут.

| Этап | Ограничение | Значение по умолчанию |
|---|---|---|
| Поиск | `monitoring.count_per_query` × число запросов | 100 × 28 |
| LLM + комментарии | `filters.llm.max_posts_per_cycle` | 150 публикаций |
| Проверка комментариев (браузер) | `filters.comments.max_checks_per_cycle` | 25 публикаций |

Обработка одной публикации в LLM занимает ~5–25 с (классификация + генерация
комментария с регенерациями). Публикации сверх лимита не теряются: они
остаются в статусе `new` и берутся первыми в следующем цикле.

Если цикл регулярно не успевает, в логах будет:
`LLM-этап ограничен N публикациями из M; остальные будут обработаны в следующем цикле`.

## Локальная разработка

Продовый compose не мешает локальной конфигурации:

```powershell
docker compose up -d              # postgres + redis для разработки
.\.venv\Scripts\python.exe -m pytest
```

Конфигурации независимы: `docker-compose.yml` (локально) и
`docker-compose.prod.yml` (сервер), разные имена проектов и volume.
