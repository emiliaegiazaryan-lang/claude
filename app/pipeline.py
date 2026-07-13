"""Оркестрация пайплайна: вход -> мастер-бриф -> 3 текста -> чистка -> редактор."""

import asyncio
import logging
import re
from dataclasses import dataclass, field

from .agents import CHANNEL_LABELS, CHANNEL_ORDER, AgentRunner
from .cleanup import mechanical_cleanup

logger = logging.getLogger(__name__)

THREADS_POST_LIMIT = 500


@dataclass
class ChannelMaterial:
    channel: str
    label: str
    title: str | None
    body: str
    raw: str = ""  # текст после чистки, до разбора - основа для правок
    posts: list[str] = field(default_factory=list)  # только для Threads
    editor_status: str = "OK"
    editor_issues: str = ""

    def overlong_posts(self) -> list[int]:
        """Номера постов Threads, превысивших лимит 500 знаков (нумерация с 1)."""
        return [
            i for i, post in enumerate(self.posts, start=1)
            if len(post) > THREADS_POST_LIMIT
        ]


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


def split_title(channel: str, text: str) -> tuple[str | None, str]:
    """Выносит строку ЗАГОЛОВОК из текста vc.ru."""
    if channel != "vcru":
        return None, text

    title = None
    body_lines = []
    for line in text.splitlines():
        title_match = re.match(r"ЗАГОЛОВОК:\s*(.*)", line.strip())
        if title is None and title_match:
            title = title_match.group(1).strip() or None
            continue
        body_lines.append(line)
    body = "\n".join(body_lines).strip()
    # модель иногда ставит строку-разделитель --- сразу после заголовка
    body = re.sub(r"\A-{3,}\s*\n", "", body).strip()
    return title, body or text


_THREADS_POST_LABEL = re.compile(r"\AПост\s*\d+\s*[-:.–—]?[^\n]*\n+", re.IGNORECASE)


def split_threads_posts(text: str) -> list[str]:
    """Режет вывод Threads-агента на посты по строке-разделителю ---.

    Служебные метки вида "Пост 1 - крючок" в начале поста снимаются:
    это подпись из промпта, в публикацию она идти не должна.
    """
    posts = re.split(r"\n\s*-{3,}\s*\n", "\n" + text + "\n")
    cleaned = []
    for post in posts:
        post = post.strip()
        if not post:
            continue
        cleaned.append(_THREADS_POST_LABEL.sub("", post).strip())
    return cleaned


def make_material(channel: str, cleaned_text: str, verdict) -> ChannelMaterial:
    title, body = split_title(channel, cleaned_text)
    posts = split_threads_posts(body) if channel == "threads" else []
    return ChannelMaterial(
        channel=channel,
        label=CHANNEL_LABELS[channel],
        title=title,
        body=body,
        raw=cleaned_text,
        posts=posts,
        editor_status=verdict.status,
        editor_issues=verdict.issues,
    )


async def generate_channels(
    runner: AgentRunner,
    brief: str,
    channels: tuple[str, ...],
) -> list[ChannelMaterial]:
    """Генерирует тексты выбранных каналов из готового брифа:
    агенты параллельно -> механическая чистка -> редактор."""
    logger.info("Запускаю канальных агентов параллельно: %s", ", ".join(channels))
    drafts = await asyncio.gather(
        *(runner.write_channel(channel, brief) for channel in channels)
    )

    # механическая чистка до редактора: тире, восклицания, эмодзи
    cleaned = [mechanical_cleanup(draft) for draft in drafts]
    logger.info("Механическая чистка выполнена")

    logger.info("Прогоняю тексты через редактора")
    verdicts = await asyncio.gather(
        *(
            runner.review(channel, brief, text)
            for channel, text in zip(channels, cleaned)
        )
    )

    return [
        make_material(channel, text, verdict)
        for channel, text, verdict in zip(channels, cleaned, verdicts)
    ]


async def run_pipeline(
    runner: AgentRunner,
    source_text: str,
    channels: tuple[str, ...] | None = None,
) -> PipelineResult:
    selected = tuple(channels) if channels else CHANNEL_ORDER

    brief = await runner.build_brief(source_text)
    materials = await generate_channels(runner, brief, selected)

    logger.info("Пайплайн завершён: %d текст(а) готовы", len(materials))
    return PipelineResult(brief=brief, gaps=extract_gaps(brief), materials=materials)


async def revise_material(
    runner: AgentRunner,
    channel: str,
    brief: str,
    current_text: str,
    feedback: str,
) -> ChannelMaterial:
    """Переделывает один материал по правкам автора: агент -> чистка -> редактор."""
    revised = await runner.revise_channel(channel, brief, current_text, feedback)
    cleaned = mechanical_cleanup(revised)
    verdict = await runner.review(channel, brief, cleaned)
    return make_material(channel, cleaned, verdict)
