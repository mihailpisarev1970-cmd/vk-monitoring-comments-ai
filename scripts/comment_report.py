"""Реальная генерация комментариев для approved-публикаций (ТЗ п.13).

Запуск:  python -m scripts.comment_report [--limit N]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter

from sqlalchemy import select

from app.core.logging import setup_logging
from app.db.enums import PostStatus
from app.db.models import Post, PostAnalysis
from app.db.session import engine, session_scope
from app.filters.loader import load_config
from app.llm.client import LLMClient, LLMError
from app.llm.comment_generator import CommentGenerator


async def main(limit: int) -> int:
    setup_logging(log_to_file=False)
    bundle = load_config(force=True)
    comment_cfg = bundle.filters.comment

    async with LLMClient() as client:
        generator = CommentGenerator(
            client,
            max_attempts=comment_cfg.max_regeneration_attempts + 1,
        )
        print(
            f"Модель: {client.model}, попыток: {generator.max_attempts}, "
            f"длина: {comment_cfg.min_length}–{comment_cfg.max_length}"
        )

        async with session_scope() as session:
            posts = (
                await session.execute(
                    select(Post)
                    .where(Post.status == PostStatus.APPROVED)
                    .order_by(Post.id)
                    .limit(limit)
                )
            ).scalars().all()

            # Уже обработанные на этапе генерации пропускаем
            done = {
                row[0]
                for row in (
                    await session.execute(
                        select(PostAnalysis.post_id).where(
                            PostAnalysis.stage == "comment_generation"
                        )
                    )
                ).all()
            }
            pending = [post for post in posts if post.id not in done]

            print(f"Публикаций approved: {len(posts)}, к обработке: {len(pending)}\n")
            results = Counter()

            for index, post in enumerate(pending, start=1):
                try:
                    outcome = await generator.generate_and_save(session, post)
                except LLMError as exc:
                    results["llm_error"] += 1
                    print(f"  [{index}] ошибка LLM: {exc}")
                    continue

                if outcome.valid:
                    results["valid"] += 1
                    text = post.text.replace("\n", " ")[:70]
                    print(f"  [{index}] OK   {len(outcome.comment or '')} симв, "
                          f"попыток {outcome.attempts} | {text}")
                    print(f"       → {outcome.comment}")
                elif outcome.validation is not None:
                    results["invalid"] += 1
                    print(f"  [{index}] FAIL попыток {outcome.attempts}: "
                          f"{'; '.join(outcome.validation.reasons[:2])}")
                else:
                    results["error"] += 1
                    print(f"  [{index}] ERROR: {outcome.error}")

    await engine.dispose()

    print("\n=== Итоги генерации ===")
    for key, value in results.items():
        print(f"  {key}: {value}")

    async with session_scope() as session:
        total = await session.scalar(
            select(PostAnalysis.id).where(PostAnalysis.stage == "comment_generation").limit(1)
        )
        stats = (
            await session.execute(
                select(PostAnalysis.accepted).where(PostAnalysis.stage == "comment_generation")
            )
        ).all()
    _ = total
    accepted = sum(1 for (flag,) in stats if flag)
    print(f"  post_analysis(comment_generation): {len(stats)} записей, валидных {accepted}")

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=10)
    sys.exit(asyncio.run(main(parser.parse_args().limit)))
