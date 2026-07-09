"""Оркестрация пайплайна: вход -> мастер-бриф -> 4 текста -> проверка редактором."""

import asyncio
import logging
import re
from dataclasses import dataclass

from .agents import CHANNEL_LABELS, CHANNEL_ORDER, AgentRunner

logger = logging.getLogger(__name__)


@dataclass
class ChannelMaterial:
    channel: str
    label: str
    title: str | None
    preview: str | None
    body: str
    editor_status: str
    editor_issues: str


@dataclass
class PipelineResult:
    brief: str
    gaps: str | None
    materials: list[ChannelMaterial]


def extract_gaps(brief: str) -> str | None:
    match = re.search(r"ПРОБЕЛЫ[:*]*\s*\n?(.+)\Z", brief, re.DOTALL)
    if not match:
        return None
    # убираем markdown-обрамление вокруг заголовка секции, если модель его добавила
    gaps = match.group(1).strip().lstrip("*: \n").strip()
    return gaps or None


def split_title(channel: str, text: str) -> tuple[str | None, str | None, str]:
    """Выносит ЗАГОЛОВОК (и ОПИСАНИЕ ДЛЯ ПРЕВЬЮ у Дзена) из текста Дзена и vc.ru."""
    if channel not in ("dzen", "vcru"):
        return None, None, text

    title = None
    preview = None
    body_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        title_match = re.match(r"ЗАГОЛОВОК:\s*(.*)", stripped)
        preview_match = re.match(r"ОПИСАНИЕ ДЛЯ ПРЕВЬЮ:\s*(.*)", stripped)
        if title is None and title_match:
            title = title_match.group(1).strip() or None
            continue
        if channel == "dzen" and preview is None and preview_match:
            preview = preview_match.group(1).strip() or None
            continue
        body_lines.append(line)
    body = "\n".join(body_lines).strip()
    return title, preview, body or text


async def run_pipeline(runner: AgentRunner, source_text: str) -> PipelineResult:
    brief = await runner.build_brief(source_text)

    logger.info("Запускаю 4 канальных агента параллельно")
    drafts = await asyncio.gather(
        *(runner.write_channel(channel, brief) for channel in CHANNEL_ORDER)
    )

    logger.info("Прогоняю тексты через редактора")
    verdicts = await asyncio.gather(
        *(
            runner.review(channel, brief, draft)
            for channel, draft in zip(CHANNEL_ORDER, drafts)
        )
    )

    materials = []
    for channel, draft, verdict in zip(CHANNEL_ORDER, drafts, verdicts):
        final_text = draft
        if verdict.status == "НА ПРАВКУ" and verdict.fixed_text:
            final_text = verdict.fixed_text
        title, preview, body = split_title(channel, final_text)
        materials.append(
            ChannelMaterial(
                channel=channel,
                label=CHANNEL_LABELS[channel],
                title=title,
                preview=preview,
                body=body,
                editor_status=verdict.status,
                editor_issues=verdict.issues,
            )
        )

    logger.info("Пайплайн завершён: 4 текста готовы")
    return PipelineResult(brief=brief, gaps=extract_gaps(brief), materials=materials)
