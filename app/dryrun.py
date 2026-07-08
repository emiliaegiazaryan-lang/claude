"""Тестовый прогон пайплайна без Telegram.

Использование:
    python -m app.dryrun "текст темы"

Нужен только ANTHROPIC_API_KEY в .env - транскрибация и Telegram не задействованы.
"""

import asyncio
import logging
import sys

from .agents import AgentRunner
from .config import load_settings, require
from .pipeline import run_pipeline

SEPARATOR = "=" * 70


async def _main(topic: str) -> None:
    settings = load_settings()
    require(settings, "anthropic_api_key")
    runner = AgentRunner(settings)

    result = await run_pipeline(runner, topic)

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
        if material.preview:
            print(f"Описание для превью: {material.preview}")
        if material.title or material.preview:
            print()
        print(material.body)
        if material.editor_status == "НА ПРАВКУ" and material.editor_issues:
            print()
            print(f"Замечания редактора:\n{material.editor_issues}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    topic = " ".join(sys.argv[1:]).strip()
    if not topic:
        print('Использование: python -m app.dryrun "текст темы"')
        sys.exit(1)
    asyncio.run(_main(topic))


if __name__ == "__main__":
    main()
