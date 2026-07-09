"""Telegram-бот: принимает серию голосовых/текстов, склеивает и запускает пайплайн.

Запуск: python -m app.bot
"""

import asyncio
import logging
from dataclasses import dataclass, field
from io import BytesIO

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .agents import AgentRunner
from .config import Settings, load_settings, require
from .pipeline import ChannelMaterial, PipelineResult, run_pipeline
from .transcription import Transcriber

logger = logging.getLogger(__name__)

router = Router()

COLLECT_CALLBACK = "collect_brief"
MAX_MESSAGE_LENGTH = 4000

STATUS_TEXT = "Слушаю, шлите ещё или нажмите Собрать бриф"
PROCESSING_TEXT = "Принял. Собираю бриф и тексты, обычно это занимает 1-2 минуты..."

collect_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="Собрать бриф", callback_data=COLLECT_CALLBACK)]]
)


@dataclass
class BufferedItem:
    index: int
    kind: str  # voice / audio / text
    text: str | None = None
    error: str | None = None
    task: asyncio.Task | None = None


@dataclass
class Series:
    chat_id: int
    items: list[BufferedItem] = field(default_factory=list)
    timer: asyncio.Task | None = None
    status_message: Message | None = None


class AppContext:
    def __init__(self, settings: Settings, bot: Bot):
        self.settings = settings
        self.bot = bot
        self.transcriber = Transcriber(settings)
        self.runner = AgentRunner(settings)
        self.series: dict[int, Series] = {}

    def is_allowed(self, user_id: int | None) -> bool:
        allowed = self.settings.allowed_user_ids
        return not allowed or (user_id is not None and user_id in allowed)


def split_message(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Режет длинный текст на части по границам абзацев."""
    if len(text) <= limit:
        return [text]
    chunks = []
    current = ""
    for paragraph in text.split("\n\n"):
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(paragraph) > limit:
            chunks.append(paragraph[:limit])
            paragraph = paragraph[limit:]
        current = paragraph
    if current:
        chunks.append(current)
    return chunks


async def send_long(bot: Bot, chat_id: int, text: str) -> None:
    for chunk in split_message(text):
        await bot.send_message(chat_id, chunk)


async def _transcribe_item(ctx: AppContext, item: BufferedItem, file_id: str, filename: str) -> None:
    try:
        buffer = BytesIO()
        await ctx.bot.download(file_id, destination=buffer)
        item.text = await ctx.transcriber.transcribe(buffer.getvalue(), filename)
    except Exception as exc:  # noqa: BLE001 - причина уходит пользователю
        logger.exception("Транскрибация сообщения %d не удалась", item.index)
        item.error = str(exc)


def _schedule_run(ctx: AppContext, series: Series) -> None:
    if series.timer:
        series.timer.cancel()

    async def _wait() -> None:
        try:
            await asyncio.sleep(ctx.settings.debounce_seconds)
        except asyncio.CancelledError:
            return
        await _run_series(ctx, series.chat_id)

    series.timer = asyncio.create_task(_wait())


async def _run_series(ctx: AppContext, chat_id: int) -> None:
    series = ctx.series.pop(chat_id, None)
    if series is None:
        return
    if series.timer:
        series.timer.cancel()

    if series.status_message:
        try:
            await series.status_message.edit_text(PROCESSING_TEXT)
        except TelegramBadRequest:
            pass

    # дожидаемся транскрибаций
    tasks = [item.task for item in series.items if item.task]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    parts = []
    for item in series.items:
        if item.error is not None:
            await ctx.bot.send_message(
                chat_id,
                f"Сообщение {item.index} не распозналось, продолжаю без него. Ошибка: {item.error}",
            )
        elif item.text:
            parts.append(item.text)

    if not parts:
        await ctx.bot.send_message(
            chat_id, "Не удалось получить ни одной расшифровки - пайплайн не запущен."
        )
        return

    source_text = "\n\n".join(parts)
    logger.info(
        "Серия из %d сообщений склеена, %d знаков. Запускаю пайплайн",
        len(series.items),
        len(source_text),
    )

    try:
        result = await run_pipeline(ctx.runner, source_text)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Пайплайн упал")
        await ctx.bot.send_message(
            chat_id, f"Пайплайн не отработал: {exc}. Попробуйте ещё раз."
        )
        return

    await _send_result(ctx, chat_id, result)


def _format_material(material: ChannelMaterial) -> str:
    lines = [material.label]
    if material.editor_status == "НА ПРАВКУ":
        lines[0] += " (после правки редактора)"
    lines.append("")
    if material.title:
        lines.append(f"Заголовок: {material.title}")
    if material.preview:
        lines.append(f"Описание для превью: {material.preview}")
    if material.title or material.preview:
        lines.append("")
    lines.append(material.body)
    return "\n".join(lines)


async def _send_result(ctx: AppContext, chat_id: int, result: PipelineResult) -> None:
    # Мастер-бриф - внутренний рабочий документ, в чат не отправляется.
    for material in result.materials:
        await send_long(ctx.bot, chat_id, _format_material(material))

    if result.gaps:
        await send_long(ctx.bot, chat_id, "Проверь перед публикацией:\n\n" + result.gaps)


@router.message(CommandStart())
async def handle_start(message: Message, ctx: AppContext) -> None:
    if not ctx.is_allowed(message.from_user.id if message.from_user else None):
        return
    await message.answer(
        "Пришлите голосовое (можно несколько подряд) или текст с идеей.\n"
        "Я подожду 20 секунд после последнего сообщения и пришлю четыре текста - "
        "Telegram, VK, Дзен, vc.ru.\n"
        "Чтобы не ждать, нажмите кнопку Собрать бриф."
    )


@router.message(F.voice | F.audio | F.text)
async def handle_content(message: Message, ctx: AppContext) -> None:
    if not ctx.is_allowed(message.from_user.id if message.from_user else None):
        return

    chat_id = message.chat.id
    series = ctx.series.get(chat_id)
    is_new_series = series is None
    if series is None:
        series = Series(chat_id=chat_id)
        ctx.series[chat_id] = series

    index = len(series.items) + 1
    if message.voice:
        item = BufferedItem(index=index, kind="voice")
        item.task = asyncio.create_task(
            _transcribe_item(ctx, item, message.voice.file_id, "voice.ogg")
        )
        logger.info("Чат %d: голосовое %d принято", chat_id, index)
    elif message.audio:
        filename = message.audio.file_name or "audio.mp3"
        item = BufferedItem(index=index, kind="audio")
        item.task = asyncio.create_task(
            _transcribe_item(ctx, item, message.audio.file_id, filename)
        )
        logger.info("Чат %d: аудио %d принято", chat_id, index)
    else:
        item = BufferedItem(index=index, kind="text", text=message.text)
        logger.info("Чат %d: текст %d принят", chat_id, index)

    series.items.append(item)

    if is_new_series:
        series.status_message = await message.answer(
            STATUS_TEXT, reply_markup=collect_keyboard
        )

    _schedule_run(ctx, series)


@router.callback_query(F.data == COLLECT_CALLBACK)
async def handle_collect(callback: CallbackQuery, ctx: AppContext) -> None:
    if not ctx.is_allowed(callback.from_user.id):
        await callback.answer()
        return
    await callback.answer("Собираю")
    if callback.message:
        await _run_series(ctx, callback.message.chat.id)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = load_settings()
    require(settings, "telegram_bot_token", "anthropic_api_key", "openai_api_key")

    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()
    dp.include_router(router)
    dp["ctx"] = AppContext(settings, bot)

    logger.info("Бот запущен, жду сообщения")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
