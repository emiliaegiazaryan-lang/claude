"""Тестовый прогон пайплайна без Telegram.

Использование:
    python -m app.dryrun "текст темы"
    python -m app.dryrun "текст темы" --channels telegram,vcru

Нужен только ANTHROPIC_API_KEY в .env - транскрибация и Telegram не задействованы.
"""

import asyncio
import logging
import sys

from .agents import CHANNEL_ORDER, AgentRunner
from .config import load_settings, require
from .pipeline import run_pipeline

SEPARATOR = "=" * 70


async def _main(topic: str, channels: tuple[str, ...] | None) -> None:
    settings = load_settings()
    require(settings, "anthropic_api_key")
    runner = AgentRunner(settings)

    result = await run_pipeline(runner, topic, channels)

    print(SEPARATOR)
    print("МАСТЕР-БРИФ")
    print(SEPARATOR)
    print(result.brief)

    if result.gaps:
        print()
        print(SEPARATOR)
        print("ПРОБЕЛЫ - проверь перед публикацией")
        print(SEPARATOR)
        print(result.gaps)

    for material in result.materials:
        print()
        print(SEPARATOR)
        print(f"{material.label} (редактор: {material.editor_status})")
        print(SEPARATOR)
        if material.title:
            print(f"Заголовок: {material.title}")
            print()
        if material.posts:
            for i, post in enumerate(material.posts, start=1):
                marker = " - ДЛИННЕЕ 500" if len(post) > 500 else ""
                print(f"--- пост {i} ({len(post)} знаков{marker}) ---")
                print(post)
                print()
        else:
            print(material.body)
        if material.editor_status == "НА ПРАВКУ" and material.editor_issues:
            print()
            print(f"Редактор - что не так:\n{material.editor_issues}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = sys.argv[1:]
    channels: tuple[str, ...] | None = None
    if "--channels" in args:
        i = args.index("--channels")
        raw = args[i + 1] if i + 1 < len(args) else ""
        channels = tuple(ch.strip() for ch in raw.split(",") if ch.strip())
        unknown = [ch for ch in channels if ch not in CHANNEL_ORDER]
        if unknown or not channels:
            print(f"Неизвестные каналы: {', '.join(unknown) or '(пусто)'}. "
                  f"Доступны: {', '.join(CHANNEL_ORDER)}")
            sys.exit(1)
        args = args[:i] + args[i + 2:]
    topic = " ".join(args).strip()
    if not topic:
        print('Использование: python -m app.dryrun "текст темы" [--channels telegram,vcru]')
        sys.exit(1)
    asyncio.run(_main(topic, channels))


if __name__ == "__main__":
    main()
