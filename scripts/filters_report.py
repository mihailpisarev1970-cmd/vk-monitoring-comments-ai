"""Прогон фильтров по сохранённым публикациям (оценка на реальных данных).

Запуск:  python -m scripts.filters_report
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter

from sqlalchemy import select

from app.core.logging import setup_logging
from app.db.models import Post
from app.db.session import engine, session_scope
from app.filters.base import FilterContext
from app.filters.loader import load_config
from app.filters.pipeline import FilterPipeline
from app.vk.models import NormalizedPost
from app.vk.normalizer import clean_text

EXAMPLES_PER_BUCKET = 3


async def main() -> int:
    setup_logging(log_to_file=False)
    bundle = load_config(force=True)
    pipeline = FilterPipeline(bundle)

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Post.owner_id, Post.post_id, Post.text, Post.ad_flag, Post.marked_as_ads)
            )
        ).all()
    await engine.dispose()

    posts = [
        NormalizedPost(
            owner_id=owner_id,
            post_id=post_id,
            text=text or "",
            published_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
            post_url=f"https://vk.com/wall{owner_id}_{post_id}",
            ad_flag=ad_flag,
            marked_as_ads=marked,
        )
        for owner_id, post_id, text, ad_flag, marked in rows
    ]

    bucket_counter: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    passed: list[NormalizedPost] = []
    seen_texts: list[str] = []

    for post in posts:
        context = FilterContext(seen_texts=list(seen_texts))
        result = pipeline.run(post, context)
        bucket_counter[result.outcome.value] += 1
        if result.rejected:
            reasons[result.reason.split(":")[0]] += 1
        else:
            passed.append(post)
            seen_texts.append(clean_text(post.text))

    total = len(posts)
    print(f"\n=== Фильтрация {total} публикаций ===")
    for outcome, count in bucket_counter.most_common():
        print(f"  {outcome}: {count} ({count / total:.1%})")
    print("\nПричины отклонения:")
    for filter_name, count in reasons.most_common():
        print(f"  {filter_name}: {count}")

    print(f"\nПрошли технические фильтры: {len(passed)}")
    for post in passed[:EXAMPLES_PER_BUCKET * 3]:
        text = clean_text(post.text).replace("\n", " ")
        print(f"  [{post.key}] {text[:130]}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
