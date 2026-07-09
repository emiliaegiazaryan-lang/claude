"""Вызовы Claude API: парсер, четыре канальных агента, редактор."""

import logging
import re
from dataclasses import dataclass

import anthropic

from .config import Settings
from .prompts import build_system_prompt

logger = logging.getLogger(__name__)

CHANNEL_ORDER = ("telegram", "vk", "dzen", "vcru")

CHANNEL_LABELS = {
    "telegram": "Telegram",
    "vk": "VK",
    "dzen": "Дзен",
    "vcru": "vc.ru",
}

MAX_OUTPUT_TOKENS = 8000


@dataclass
class EditorVerdict:
    status: str          # "OK" или "НА ПРАВКУ"
    issues: str          # список несоответствий (текстом)
    fixed_text: str | None  # исправленный текст, если был


class AgentRunner:
    def __init__(self, settings: Settings):
        self._settings = settings
        # SDK сам ретраит 429/5xx и сетевые ошибки с экспоненциальным бэкоффом.
        self._client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key,
            max_retries=3,
        )

    async def _call(self, system: str, user_content: str, temperature: float) -> str:
        response = await self._client.messages.create(
            model=self._settings.model,
            max_tokens=MAX_OUTPUT_TOKENS,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user_content}],
        )
        text = "".join(
            block.text for block in response.content if block.type == "text"
        ).strip()
        if response.stop_reason == "max_tokens":
            logger.warning("Ответ агента обрезан по max_tokens")
        return text

    async def build_brief(self, source_text: str) -> str:
        logger.info("Парсер: собираю мастер-бриф")
        brief = await self._call(build_system_prompt("parser"), source_text, self._settings.parser_temperature)
        logger.info("Парсер: бриф собран, %d знаков", len(brief))
        return brief

    async def write_channel(self, channel: str, brief: str) -> str:
        logger.info("Канальный агент %s: пишу текст", channel)
        text = await self._call(build_system_prompt(channel), brief, self._settings.channel_temperature)
        logger.info("Канальный агент %s: готово, %d знаков", channel, len(text))
        return text

    async def review(self, channel: str, brief: str, text: str) -> EditorVerdict:
        logger.info("Редактор: проверяю текст для %s", channel)
        user_content = (
            f"МАСТЕР-БРИФ:\n{brief}\n\n"
            f"КАНАЛ: {CHANNEL_LABELS[channel]}\n\n"
            f"ТЕКСТ КАНАЛА:\n{text}"
        )
        raw = await self._call(build_system_prompt("editor"), user_content, self._settings.editor_temperature)
        verdict = parse_editor_output(raw)
        logger.info("Редактор: %s - статус %s", channel, verdict.status)
        return verdict


def parse_editor_output(raw: str) -> EditorVerdict:
    status = "OK"
    status_match = re.search(r"СТАТУС:\s*(.+)", raw)
    if status_match and "ПРАВК" in status_match.group(1).upper():
        status = "НА ПРАВКУ"

    issues = ""
    issues_match = re.search(
        r"НЕСООТВЕТСТВИЯ:\s*(.*?)(?=ИСПРАВЛЕННЫЙ ТЕКСТ:|\Z)", raw, re.DOTALL
    )
    if issues_match:
        issues = issues_match.group(1).strip()

    fixed_text = None
    fixed_match = re.search(r"ИСПРАВЛЕННЫЙ ТЕКСТ:\s*\n?(.*)", raw, re.DOTALL)
    if fixed_match:
        candidate = fixed_match.group(1).strip()
        # отсекаем ответы вида "не требуется", "-" и т.п.
        if len(candidate) > 200:
            fixed_text = candidate

    return EditorVerdict(status=status, issues=issues, fixed_text=fixed_text)
