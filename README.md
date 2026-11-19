# VK Monitor Assistant — мониторинг публикаций и генерация комментариев для ВКонтакте

> Сервис мониторинга публикаций ВКонтакте: поиск по маркерам, многоступенчатая
> фильтрация (реклама, курсы, спам), семантическая проверка через LLM
> (AiDostup / claude-haiku-4-5), генерация естественного комментария на
> русском языке и доставка уведомления в Telegram.

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-composed-blue.svg)](https://www.docker.com/)
[![PostgreSQL 17](https://img.shields.io/badge/postgres-17-blue.svg)](https://www.postgresql.org/)
[![Redis 7](https://img.shields.io/badge/redis-7-red.svg)](https://redis.io/)
[![ruff](https://img.shields.io/badge/linter-ruff-ff69b4.svg)](https://docs.astral.sh/ruff/)
[![pytest](https://img.shields.io/badge/tests-pytest-green.svg)](https://docs.pytest.org/)
[![LLM: claude-haiku-4-5](https://img.shields.io/badge/LLM-claude--haiku--4--5-purple.svg)](https://aidostup.site/)

## О проекте (SEO-описание)

**VK Monitor Assistant** — это автоматизированный сервис мониторинга
публикаций в социальной сети **ВКонтакте (VK)**. Решение
позволяет отслеживать новые посты по заданным тематическим маркерам,
автоматически фильтровать рекламу, спам и коммерческие предложения,
проверять релевантность через крупную языковую модель (LLM) и генерировать
естественные комментарии на русском языке для последующей ручной публикации.

### Ключевые возможности

* **Мониторинг ВКонтакте** — поиск публикаций по маркерам через VK API
  (`newsfeed.search`) с гибким расписанием по MSK.
* **Многоступенчатая фильтрация** — отсев официальной рекламы VK, курсов,
  спама, коммерческих предложений и нетекстового контента.
* **LLM-анализ (AiDostup / claude-haiku-4-5)** — семантическая проверка
  релевантности публикации тематике проекта и принятие решения об одобрении.
* **Генерация комментариев** — создание естественного русскоязычного
  комментария под постом с контролем длины (200–300 символов) и падежных
  форм слов.
* **Проверка доступности комментариев** — через VK API `wall.getComments`
  (`can_post`) перед отправкой уведомления.
* **Доставка в Telegram** — готовый результат (ссылка на пост + сгенерированный
  комментарий) присылается в Telegram-чат для ручной публикации пользователем.
* **База данных PostgreSQL + Redis** — хранение публикаций, истории циклов,
  блокировка параллельных запусков и суточный лимит LLM.
* **Очистка БД** — удаление отработанных публикаций (старше 24 ч), чтобы база
  не раздувалась.

> **Важно:** комментарии во ВКонтакте публикуются **только вручную**
> пользователем. Автоматическая отправка не реализуется (ТЗ п.1, п.13).

### Стек технологий

| Компонент | Назначение |
|---|---|
| Python 3.11+ (3.13) | Основной язык разработки |
| PostgreSQL 17 | Хранение публикаций и истории |
| Redis 7 | Блокировки циклов и суточный лимит LLM |
| Docker / Docker Compose | Развёртывание на сервере |
| AiDostup (claude-haiku-4-5) | LLM-классификация и генерация |
| Telegram Bot API | Уведомления о готовых комментариях |
| VK API | Поиск публикаций и проверка `can_post` |

## Техническое задание

* `VK.docx` (v5.0) — техническое задание.
* Прогресс разработки: [PROJECT_STATUS.md](PROJECT_STATUS.md).
* Результаты проверки VK API: [docs/vk_api_check.md](docs/vk_api_check.md).
* Выбор и проверка LLM: [docs/llm_model_choice.md](docs/llm_model_choice.md).
* Архитектура: [docs/architecture.md](docs/architecture.md).

## Требования

| Компонент | Версия |
|---|---|
| Python | 3.11+ (проверено на 3.13) |
| Docker Desktop | с поддержкой `docker compose` |
| PostgreSQL | 17 (в Docker-контейнере) |
| Redis | 7 (в Docker-контейнере) |

## Быстрый старт (локальная разработка)

```powershell
# 1. Запустить Docker Desktop и подождать готовности демона
# 2. Подготовить окружение (venv + зависимости + контейнеры + миграции)
powershell -ExecutionPolicy Bypass -File scripts/bootstrap.ps1

# 3. Проверить окружение (включая реальные запросы к LLM и Telegram)
.\.venv\Scripts\python.exe -m scripts.check_env --external

# 4. Запустить приложение (пока только preflight-проверки)
.\.venv\Scripts\python.exe -m app
```

### Ручная последовательность

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# Отключить конфликтующую локальную службу PostgreSQL (Windows), если она есть
Stop-Service postgresql-x64-17 -Force
Set-Service postgresql-x64-17 -StartupType Manual

docker compose up -d postgres redis
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m scripts.check_env --external
```



## Настройка `.env`

Скопируйте `.env.example` в `.env` и заполните реальными значениями. **Файл
`.env` не попадает в репозиторий (добавлен в `.gitignore`) — никогда не
коммитьте его.**

| Переменная | Описание |
|---|---|
| `VK_APP_ID` | ID standalone-приложения ВКонтакте |
| `VK_ACCESS_TOKEN` | **Сервисный** ключ доступа (не токен личной страницы) |
| `VK_API_VERSION` | Версия VK API (5.199) |
| `AIDOSTUP_BASE_URL` | `https://aidostup.site/v1` |
| `AIDOSTUP_API_KEY` | Ключ AiDostup |
| `AIDOSTUP_MODEL` | Модель для классификации (`claude-haiku-4-5`) |
| `AIDOSTUP_COMMENT_MODEL` | Модель для генерации комментариев (опционально) |
| `TELEGRAM_BOT_TOKEN` | Токен Telegram-бота |
| `TELEGRAM_CHAT_ID` | ID чата для уведомлений |
| `DATABASE_URL` | DSN `postgresql+asyncpg://…` (хост `127.0.0.1` на Windows) |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` |
| `TIMEZONE` | `Europe/Moscow` |

## Постфильтры отбора публикаций

Помимо официальной рекламы и коммерческих предложений, публикация
отклоняется в следующих случаях:

| Правило | Файл | Настройки в `config/filters.json` |
|---|---|---|
| Предложение работы / вакансия | `app/filters/spam_filter.py` | `spam.job_offer_min_signals` (3) |
| Комментарии к публикации закрыты | `app/vk/comments_check.py` | `comments.*` |
| Нет текстовой информации | `app/filters/quality_filter.py` | `quality.reject_non_text` |
| Одно предложение и меньше | `app/filters/quality_filter.py` | `quality.min_sentences` (2) |
| Рекламный характер | `app/filters/commercial_filter.py` | `commercial.min_signals_for_reject` |

**Проверка комментариев** выполняется через VK API, метод `wall.getComments`:
в ответе есть поле `can_post`, и оно равно `False`, если автор ограничил
возможность комментирования.

```powershell
python -m scripts.check_comments
```

Один запрос на публикацию (~0.3 с), браузер не нужен. Проверяются только
публикации, уже одобренные LLM, — перед постановкой в очередь Telegram.

> **История решения.** Изначально проверка делалась headless-браузером по
> классу `closed_comments` в HTML. Этот подход оказался ошибочным: класс
> присутствует у **всех** публикаций. Playwright и Chromium удалены из
> зависимостей (образ уменьшен до ~381 МБ).

## Маркеры поиска

Тематики и поисковые запросы — в `config/markers.json`. Сейчас **15 тематик,
28 запросов**; изменения подхватываются на следующем цикле без перезапуска.

```json
{
  "name": "Нейросети",
  "queries": ["нейросети", "#нейросети"],
  "enabled": true
}
```

**Важно:** при добавлении новой тематики нужно дописать её термины в
`config/spam_words.json` → `topics.terms`. Иначе `quality_filter` отклонит
все публикации по этому маркеру.

## Суточный лимит LLM и очистка БД

* **LLM обрабатывает не более 25 публикаций в сутки** — счётчик в Redis
  (`llm:daily_usage:<дата МСК>`) сбрасывается в полночь.
* **В базу попадают только свежие посты** — опубликованные не более 4 часов
  назад (`monitoring.max_age_hours`).
* **Очистка БД** — отработанные публикации (`sent`, `rejected_*`, `failed`)
  старше 24 часов удаляются вместе со связанными записями (`post_analysis`,
  `post_markers`, `notifications`), чтобы база не раздувалась.

## Разработка

```powershell
# Тесты
.\.venv\Scripts\python.exe -m pytest tests -v

# Только быстрые тесты без БД/Redis
.\.venv\Scripts\python.exe -m pytest tests -m "not integration"

# Линтер
.\.venv\Scripts\python.exe -m ruff check .

# Миграции
.\.venv\Scripts\python.exe -m alembic revision --autogenerate -m "описание"
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic check
```

## Docker (сервер)

```bash
docker compose --profile prod up -d --build
docker compose exec app python -m alembic upgrade head
docker compose logs -f app
```

`postgres` и `redis` всегда запускаются как сервисы; `app` — в профиле
`prod`.

## Расписание запуска (Мoscow)

Поиск публикаций выполняется по часам: **10:00, 11:00, 12:00, 17:00, 18:00,
19:00** (Europe/Moscow). Каждый цикл обрабатывает только публикации,
опубликованные не более 4 часов назад.

## Структура проекта

```
app/
├── core/      конфигурация и логирование
├── db/        модели, сессии, Redis
├── vk/        клиент VK API
├── filters/   фильтрация публикаций
├── llm/       AiDostup: классификация и генерация комментариев
├── telegram/  aiogram 3: доставка уведомлений
└── scheduler/ планировщик и watermark
config/        markers.json, filters.json, spam_words.json
migrations/    Alembic
tests/         pytest
docs/          документация
scripts/       утилиты (check_env, bootstrap)
```

## Лицензия

Частный проект, разработан по техническому заданию заказчика.
