"""Реальная классификация публикаций через LLM (ТЗ п.12).

Запуск:  python -m scripts.llm_report [--limit N]
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
from app.filters.base import FilterContext
from app.filters.loader import load_config
from app.filters.pipeline import FilterPipeline
from app.llm.classifier import PostClassifier
from app.llm.client import LLMClient
from app.vk.models import NormalizedPost
from app.vk.normalizer import clean_text


async def main(limit: int) -> int:
    setup_logging(log_to_file=False)
    bundle = load_config(force=True)
    pipeline = FilterPipeline(bundle)
    threshold = bundle.filters.llm.confidence_threshold

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Post).where(Post.status == PostStatus.NEW).order_by(Post.id).limit(limit)
            )
        ).scalars().all()

        # Отбираем прошедшие технические фильтры
        candidates = []
        seen: list[str] = []
        for post in rows:
            normalized = NormalizedPost(
                owner_id=post.owner_id,
                post_id=post.post_id,
                text=post.text or "",
                published_at=post.published_at,
                post_url=post.post_url,
                ad_flag=post.ad_flag,
                marked_as_ads=post.marked_as_ads,
            )
            result = pipeline.run(normalized, FilterContext(seen_texts=list(seen)))
            if result.rejected:
                post.status = result.status
                post.status_reason = result.reason
                continue
            candidates.append(post)
            seen.append(clean_text(post.text))

        await session.flush()
        print(f"\nКандидатов на LLM: {len(candidates)} из {len(rows)} (порог {threshold})")

        outcomes = Counter()
        examples: list[tuple[str, str]] = []
        async with LLMClient() as client:
            classifier = PostClassifier(client, confidence_threshold=threshold)
            for index, post in enumerate(candidates, start=1):
                outcome = await classifier.classify_and_save(session, post)
                if not outcome.analyzed:
                    outcomes["error"] += 1
                    print(f"  [{index}] ошибка: {outcome.error}")
                    continue
                label = "ACCEPTED" if outcome.accepted else "rejected"
                outcomes[label] += 1
                text = clean_text(post.text).replace("\n", " ")[:90]
                reasons = ",".join(outcome.decision.rejection_reasons) or "-"
                topics = ",".join(outcome.result.matched_topics[:2]) or "-"
                conf = outcome.result.confidence
                print(
                    f"  [{index}] {label:9} conf={conf:.2f} "
                    f"topics={topics[:22]:22} reasons={reasons[:40]:40} | {text}"
                )
                if outcome.accepted and len(examples) < 5:
                    examples.append((post.post_url, post.text or ""))

    await engine.dispose()

    print("\n=== Итоги LLM ===")
    for key, value in outcomes.items():
        print(f"  {key}: {value}")

    async with session_scope() as session:
        total = len((await session.execute(select(PostAnalysis.id))).all())
        accepted = len(
            (await session.execute(select(PostAnalysis.id).where(PostAnalysis.accepted.is_(True)))).all()
        )
    print(f"  post_analysis: {total} записей, принято {accepted}")

    if examples:
        print("\nПримеры принятых публикаций:")
        for url, text in examples:
            print(f"  {url} — {clean_text(text)[:110]}")

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=25)
    sys.exit(asyncio.run(main(parser.parse_args().limit)))
