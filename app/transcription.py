"""Транскрибация голосовых через OpenAI Whisper API (whisper-1, язык ru)."""

import logging

import httpx
from openai import AsyncOpenAI

from .config import Settings

logger = logging.getLogger(__name__)


class Transcriber:
    def __init__(self, settings: Settings):
        # SDK сам ретраит 429/5xx и сетевые ошибки с экспоненциальным бэкоффом.
        # connect=10: при недоступном API ошибка всплывает быстро, а не висит.
        self._client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            max_retries=3,
            timeout=httpx.Timeout(120.0, connect=10.0),
        )

    async def transcribe(self, audio_bytes: bytes, filename: str = "voice.ogg") -> str:
        result = await self._client.audio.transcriptions.create(
            model="whisper-1",
            file=(filename, audio_bytes),
            language="ru",
        )
        text = result.text.strip()
        logger.info("Транскрибация ок: %d знаков", len(text))
        return text
