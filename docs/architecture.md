# Архитектура — VK Monitor Assistant

## 1. Назначение

Сервис отслеживает новые публикации ВКонтакте по заданным маркерам,
фильтрует рекламу/курсы/спам, проверяет тематику через LLM, готовит
естественный комментарий и присылает пользователю уведомление в Telegram.
Публикация комментария — **только вручную** пользователем.

## 2. Структура проекта

```
vk_komments/
├── app/
│   ├── __main__.py            # точка входа: preflight + запуск планировщика
│   ├── core/
│   │   ├── config.py          # pydantic-settings, все параметры из .env
│   │   └── logging.py         # единое логирование (консоль + файл)
│   ├── db/
│   │   ├── base.py            # DeclarativeBase, миксины, enum-хелперы
│   │   ├── enums.py           # PostStatus, AdFlag, NotificationStatus, RunStatus
│   │   ├── session.py         # async engine, session_scope()
│   │   ├── redis_client.py    # Redis, распределённая блокировка
│   │   └── models/
│   │       ├── post.py        # posts
│   │       ├── marking.py     # post_markers
│   │       ├── analysis.py    # post_analysis
│   │       ├── notification.py# notifications (очередь доставки)
│   │       └── monitoring.py  # monitoring_runs, scheduler_state
│   ├── vk/                    # клиент VK API (этап 2)
│   │   ├── client.py
│   │   ├── search.py
│   │   ├── normalizer.py
│   │   ├── models.py
│   │   ├── comments_check.py  # can_post из wall.getComments
│   │   └── exceptions.py
│   ├── filters/               # многоступенчатая фильтрация (этап 3)
│   │   ├── ad_filter.py
│   │   ├── course_filter.py
│   │   ├── commercial_filter.py
│   │   ├── spam_filter.py
│   │   ├── duplicate_filter.py
│   │   ├── quality_filter.py
│   │   └── pipeline.py
│   ├── llm/                   # AiDostup (этапы 4–5)
│   │   ├── client.py
│   │   ├── classifier.py
│   │   ├── comment_generator.py
│   │   └── prompts/
│   │       ├── classification.txt
│   │       └── comment_generation.txt
│   ├── telegram/              # aiogram 3 (этап 6)
│   │   ├── bot.py
│   │   ├── sender.py
│   │   └── formatter.py
│   └── scheduler/             # APScheduler (этап 7)
│       ├── runner.py
│       ├── watermark.py
│       └── monitoring.py
├── config/
│   ├── markers.json           # тематики и поисковые запросы
│   ├── filters.json           # включение фильтров, пороги, окна
│   └── spam_words.json        # словари спам-сигналов
├── migrations/                # Alembic
├── tests/                     # pytest
├── docs/                      # документация (этот файл, vk_api_check.md)
├── scripts/                   # check_env, bootstrap
├── docker-compose.yml         # postgres + redis (+ профиль prod для app)
├── Dockerfile
└── alembic.ini
```

## 3. Поток обработки публикации

```
APScheduler (10:00, 11:00, 18:00, 19:00 MSK)
        │
        ▼
[1] VK newsfeed.search по каждому активному запросу из config/markers.json
        │  (rolling window: watermark − overlap … now, count=200)
        ▼
[2] Нормализация → posts (UNIQUE owner_id, post_id) + post_markers
        │  дубликаты ID и текста отсекаются в PostgreSQL
        ▼
[3] Технические фильтры (app/filters/pipeline.py)
        │  ad_filter → rejected_ad (безусловно, ТЗ п.8)
        │  duplicate → точные дубли и SimHash-копии
        │  course → реклама образовательных продуктов
        │  commercial → коммерческие предложения и реклама
        │  spam → вакансии, накрутка активности, схемы
        │  quality → нет текста / <= 1 предложения / кликбейт / офтоп
        │  отказ → rejected_filter
        ▼
[4] LLM-классификация (app/llm) → post_analysis
        │  строгий JSON + валидация; правила решения ТЗ п.12.5
        │  отклонено → rejected_llm
        ▼
[5] Генерация комментария (200–300 символов) + проверка ТЗ п.13.2
        │  не прошёл → регенерация (не более N раз) → rejected_comment
        ▼
[5a] Проверка доступности комментариев (app/vk/comments_check.py)
        │  запрос wall.getComments → can_post
        │  can_post=False → rejected_filter
        ▼
[6] notifications (очередь) → aiogram 3 → Telegram
        │  ретраи с backoff, ограничение попыток, журнал ошибок
        ▼
[7] Отправлено → posts.status = sent, telegram_message_id сохранён
```

### Дополнительные правила отбора

Требования заказчика, реализованные после этапа 7, — публикация отклоняется:

| Правило | Где реализовано | Сигнал |
|---|---|---|
| Предложение работы / вакансия | `spam_filter` (`job_offer`) | `job_offer:*` — нужен порог сигналов (3, либо 2 при явном «ищу/ищем/требуется») |
| Комментарии закрыты | `app/vk/comments_check.py` | `can_post=False` из `wall.getComments` |
| Нет текстовой информации | `quality_filter` | `no_text_info`, `media_only` |
| Одно предложение и меньше | `quality_filter` | `too_few_sentences` |
| Рекламный характер | `commercial_filter` (`promo`) | `promo:*` — не меньше двух промо-признаков |

**Как проверяются комментарии.** Через метод VK API `wall.getComments`: в его
ответе есть поле `can_post`, оно доступно сервисному ключу и равно `False`,
если автор ограничил комментирование. Проверка выполняется только для
публикаций, одобренных LLM, — это один запрос (~0.3 с) на публикацию.
При недоступности проверки (сеть, лимит, неожиданный ответ) публикация не
отбрасывается (fail-open), а ошибка фиксируется в `monitoring_runs.errors`.

> **Отказ от браузерной проверки.** Ранее использовался headless-браузер и
> поиск класса `closed_comments`. Подход оказался неверным: класс есть у
> **всех** публикаций (служебный флаг VK в `class="_post post ...
> closed_comments ..."`), а `m.vk.com` для анонима не рендерит блок
> комментариев совсем. Проверка всегда возвращала «открыто». Playwright и
> Chromium удалены из зависимостей — образ уменьшился с ~1.5 ГБ до 381 МБ.

## 4. Статусы публикации

| Статус | Значение |
|---|---|
| `new` | найдена и сохранена, ещё не обработана |
| `rejected_ad` | официальная реклама VK (ТЗ п.8) |
| `rejected_filter` | отклонена техническими фильтрами |
| `rejected_llm` | отклонена LLM-классификацией |
| `rejected_comment` | комментарий не прошёл проверку |
| `approved` | прошла все проверки |
| `queued` | поставлена в очередь уведомлений |
| `sent` | уведомление доставлено |
| `failed` | исчерпаны попытки обработки/отправки |

## 5. Таблицы

| Таблица | Назначение |
|---|---|
| `posts` | публикации; `UNIQUE (owner_id, post_id)`; `ad_flag`; `text_hash` (уникальный при NOT NULL) |
| `post_markers` | все маркеры/запросы, которыми найдена публикация; `UNIQUE (post_id, marker_name, query)` |
| `post_analysis` | каждая LLM-попытка: поля классификации, `raw_response`, `accepted`, `rejection_reasons` |
| `notifications` | очередь доставки: готовый комментарий и тело сообщения, попытки, `telegram_message_id`, партиальный уникальный индекс «одно sent-уведомление на пост» |
| `monitoring_runs` | история циклов: время, окно, счётчики, ошибки |
| `scheduler_state` | watermark и служебное состояние планировщика |

## 6. Ключевые архитектурные решения

1. **Один процесс-оркестратор.** Планировщик, мониторинг, LLM и отправка в
   Telegram работают в одном asyncio-приложении, чтобы состояние было
   согласованным. Очередь уведомлений хранится в PostgreSQL (а не в памяти),
   поэтому перезапуск не теряет уведомления.
2. **Redis — блокировки.** `cycle_lock()` не допускает параллельного запуска
   двух циклов мониторинга и повторной обработки одной публикации. TTL
   блокировки продлевается фоновой heartbeat-задачей: цикл, идущий дольше
   часа (LLM на сотнях публикаций), не теряет блокировку и не даёт стартовать
   второму процессу.
3. **Отказоустойчивость к обрыву.** Запись `monitoring_runs` создаётся и
   коммитится **до** начала долгих этапов, а прогресс (счётчики + статусы
   публикаций) фиксируется каждые 5 обработанных публикаций. Прерванный
   процесс не теряет оплаченные LLM-запросы; зависший `running`-прогон при
   следующем старте помечается как `failed` (`reap_stale_runs`).
4. **Конфигурация вместо кода.** Маркеры, пороги и включение фильтров — в JSON
   (`config/`), перечитываются на каждом цикле без перезапуска (ТЗ п.4.1).
5. **Fail-closed.** Любая неопределённость (неизвестный рекламный флаг,
   неуверенность LLM, битый JSON) → публикация не отправляется.
6. **Идемпотентность.** Повторная обработка не создаёт дубликатов: ключи VK,
   хеш текста, уникальные индексы, состояние в БД.
7. **Тестируемость.** Внешние вызовы (VK, LLM, Telegram) инкапсулированы в
   клиенты с внедряемым HTTP-транспортом; тесты используют репозиторий-фикстуры
   для БД и Redis.
