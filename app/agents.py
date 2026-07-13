"""Вызовы Claude API: парсер, три канальных агента, редактор.

Модели: канальные агенты - sonnet (важен голос), парсер и редактор - haiku
(извлечение и проверка, дешевле). Статичные системные промпты помечены
cache_control - повторные вызовы идут по кэш-цене; меняющийся мастер-бриф
передаётся в user-сообщении и в кэш не попадает.
"""

import logging
import re
from dataclasses import dataclass

import anthropic
import httpx

from .config import Settings
from .prompts import build_system_prompt

logger = logging.getLogger(__name__)

CHANNEL_ORDER = ("telegram", "vcru", "threads")

CHANNEL_LABELS = {
    "telegram": "Telegram",
    "vcru": "vc.ru",
    "threads": "Threads",
}

CHANNEL_MAX_TOKENS = 8000
PARSER_MAX_TOKENS = 4000
EDITOR_MAX_TOKENS = 1500


@dataclass
class EditorVerdict:
    status: str   # "OK" или "НА ПРАВКУ"
    issues: str   # список "ЧТО НЕ ТАК" (текстом, пусто при OK)


class AgentRunner:
    def __init__(self, settings: Settings):
        self._settings = settings
        # SDK сам ретраит 429/5xx и сетевые ошибки с экспоненциальным бэкоффом.
        # connect=10: если API недоступен (например, российский IP без VPN),
        # ошибка всплывает за секунды, а не висит десятки минут.
        self._client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key,
            max_retries=3,
            timeout=httpx.Timeout(180.0, connect=10.0),
        )

    async def _call(
        self,
        model: str,
        system: str,
        user_content: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        response = await self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            # системный промпт статичен между вызовами - кэшируем целиком
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_content}],
        )
        usage = response.usage
        logger.info(
            "Модель %s: вход %d, из кэша %d, в кэш %d, выход %d токенов",
            model,
            usage.input_tokens,
            usage.cache_read_input_tokens or 0,
            usage.cache_creation_input_tokens or 0,
            usage.output_tokens,
        )
        text = "".join(
            block.text for block in response.content if block.type == "text"
        ).strip()
        if response.stop_reason == "max_tokens":
            logger.warning("Ответ агента обрезан по max_tokens")
        return text

    async def build_brief(self, source_text: str) -> str:
        logger.info("Парсер: собираю мастер-бриф")
        brief = await self._call(
            self._settings.fast_model,
            build_system_prompt("parser"),
            source_text,
            self._settings.parser_temperature,
            PARSER_MAX_TOKENS,
        )
        logger.info("Парсер: бриф собран, %d знаков", len(brief))
        return brief

    async def write_channel(self, channel: str, brief: str) -> str:
        logger.info("Канальный агент %s: пишу текст", channel)
        text = await self._call(
            self._settings.model,
            build_system_prompt(channel),
            brief,
            self._settings.channel_temperature,
            CHANNEL_MAX_TOKENS,
        )
        logger.info("Канальный агент %s: готово, %d знаков", channel, len(text))
        return text

    async def revise_channel(
        self, channel: str, brief: str, current_text: str, feedback: str
    ) -> str:
        """Переделывает текст канала по правкам автора. Системный промпт тот же -
        повторный вызов идёт по кэш-цене."""
        logger.info("Канальный агент %s: переделываю по правкам", channel)
        user_content = (
            f"МАСТЕР-БРИФ:\n{brief}\n\n"
            f"ТЕКУЩИЙ ТЕКСТ:\n{current_text}\n\n"
            f"ПРАВКИ ОТ АВТОРА:\n{feedback}\n\n"
            "Автор посмотрел текст и просит правки. Перепиши текст с учётом правок, "
            "сохрани формат канала и все правила выше. Меняй только то, о чём просят, "
            "остальное без необходимости не трогай. Выведи только готовый текст, "
            "без комментариев и пояснений."
        )
        text = await self._call(
            self._settings.model,
            build_system_prompt(channel),
            user_content,
            self._settings.channel_temperature,
            CHANNEL_MAX_TOKENS,
        )
        logger.info("Канальный агент %s: правка готова, %d знаков", channel, len(text))
        return text

    async def review(self, channel: str, brief: str, text: str) -> EditorVerdict:
        logger.info("Редактор: проверяю текст для %s", channel)
        user_content = (
            f"МАСТЕР-БРИФ:\n{brief}\n\n"
            f"КАНАЛ: {CHANNEL_LABELS[channel]}\n\n"
            f"ТЕКСТ КАНАЛА:\n{text}"
        )
        raw = await self._call(
            self._settings.fast_model,
            build_system_prompt("editor"),
            user_content,
            self._settings.editor_temperature,
            EDITOR_MAX_TOKENS,
        )
        verdict = parse_editor_output(raw)
        logger.info("Редактор: %s - статус %s", channel, verdict.status)
        return verdict


def parse_editor_output(raw: str) -> EditorVerdict:
    status = "OK"
    status_match = re.search(r"СТАТУС:\s*(.+)", raw)
    if status_match and "ПРАВК" in status_match.group(1).upper():
        status = "НА ПРАВКУ"

    issues = ""
    issues_match = re.search(r"ЧТО НЕ ТАК:\s*(.*)\Z", raw, re.DOTALL)
    if issues_match:
        issues = issues_match.group(1).strip()

    return EditorVerdict(status=status, issues=issues)
