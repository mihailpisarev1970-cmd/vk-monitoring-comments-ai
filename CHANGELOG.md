# История изменений — VK Monitor Assistant

Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/),
версии соответствуют [Semantic Versioning](https://semver.org/lang/ru/).

## [0.1.0] — 2026-11-19

Первая публичная версия: полный пайплайн мониторинга ВКонтакте.

### Добавлено

* **VK API** (`app/vk/`): клиент `newsfeed.search` / `wall.getById` с
  троттлингом, экспоненциальным backoff и классификацией ошибок;
  rolling window, нормализация публикаций, определение рекламных меток,
  дедупликация по `(owner_id, post_id)` и `text_hash`.
* **Фильтры** (`app/filters/` + `config/*.json`): официальная реклама,
  курсы и инфопродукты, спам/вакансии, коммерческие предложения,
  качество текста, дубликаты.
* **LLM** (`app/llm/`): классификация релевантности, генерация комментария
  (200–300 символов) и его валидация через AiDostup (OpenAI-compatible,
  `claude-haiku-4-5`), устойчивость к prompt-injection.
* **Проверка комментариев** (`app/vk/comments_check.py`): доступность
  комментирования через `wall.getComments` → `can_post`.
* **Telegram** (`app/telegram/`): aiogram 3, HTML-сообщение со ссылкой
  и inline-кнопкой, очередь доставки и повторные попытки.
* **Планировщик** (`app/scheduler/`): APScheduler, расписание по МСК
  (10–12, 17–19), watermark и восстановление пропущенных окон,
  суточный лимит LLM в Redis, очистка БД от отработанных записей.
* **Инфраструктура**: PostgreSQL 17 + SQLAlchemy 2.x + asyncpg, Alembic
  (обратимая миграция), Redis, Docker/Docker Compose (dev и prod),
  ротация логов, healthcheck, `scripts/check_env.py`.
* **Тесты и линтер**: 29 файлов pytest (включая интеграционные),
  ruff, mypy-совместимая аннотация типов.
* **Документация**: README, PROJECT_STATUS, `docs/architecture.md`,
  `docs/deploy.md`, `docs/vk_api_check.md`, `docs/llm_model_choice.md`.

### Безопасность

* Все секреты читаются из `.env` (`pydantic-settings`, `SecretStr`) и не
  попадают в логи и `repr`.
* `.env` исключён из git и Docker-контекста; в репозитории только
  `.env.example` с пустыми значениями.
