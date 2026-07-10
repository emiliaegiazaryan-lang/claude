"""Telegram-бот: принимает серию голосовых/текстов, склеивает и запускает пайплайн.

Возможности:
- серия сообщений = одна идея (дебаунс 20 секунд + кнопка "Собрать бриф");
- выбор каналов в начале: Telegram / vc.ru / Threads / Telegram + vc.ru / все три;
- правки: ответьте (реплаем) на присланный материал текстом или голосом -
  бот переделает этот материал с учётом правок.

Запуск: python -m app.bot
"""

import asyncio
import logging
import re
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
    ReplyParameters,
)

from .agents import CHANNEL_LABELS, CHANNEL_ORDER, AgentRunner
from .config import Settings, load_settings, require
from .pipeline import ChannelMaterial, PipelineResult, revise_material, run_pipeline
from .transcription import Transcriber

logger = logging.getLogger(__name__)

router = Router()

COLLECT_CALLBACK = "collect_brief"

# варианты выбора каналов на клавиатуре
CHANNEL_CHOICES: dict[str, tuple[str, ...]] = {
    "ch_telegram": ("telegram",),
    "ch_vcru": ("vcru",),
    "ch_threads": ("threads",),
    "ch_tg_vc": ("telegram", "vcru"),
    "ch_all": CHANNEL_ORDER,
}

MAX_MESSAGE_LENGTH = 4000

STATUS_TEXT = (
    "Слушаю, шлите ещё или нажмите Собрать бриф.\n"
    "Каналы: все три. Можно выбрать другие кнопками ниже."
)
PROCESSING_TEXT = "Принял. Собираю бриф и тексты, обычно это занимает 1-2 минуты..."
FEEDBACK_HINT = (
    "Если что-то не нравится - ответьте (реплаем) на нужный материал "
    "и напишите или наговорите правки."
)


def series_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Telegram", callback_data="ch_telegram"),
                InlineKeyboardButton(text="vc.ru", callback_data="ch_vcru"),
                InlineKeyboardButton(text="Threads", callback_data="ch_threads"),
            ],
            [
                InlineKeyboardButton(text="Telegram + vc.ru", callback_data="ch_tg_vc"),
                InlineKeyboardButton(text="Все три", callback_data="ch_all"),
            ],
            [InlineKeyboardButton(text="Собрать бриф", callback_data=COLLECT_CALLBACK)],
        ]
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
    channels: tuple[str, ...] = CHANNEL_ORDER


@dataclass
class SessionState:
    """Последняя выдача по чату - основа для правок реплаем."""

    brief: str
    materials: dict[str, ChannelMaterial] = field(default_factory=dict)
    message_map: dict[int, str] = field(default_factory=dict)  # message_id -> канал


class AppContext:
    def __init__(self, settings: Settings, bot: Bot):
        self.settings = settings
        self.bot = bot
        self.transcriber = Transcriber(settings)
        self.runner = AgentRunner(settings)
        self.series: dict[int, Series] = {}
        self.sessions: dict[int, SessionState] = {}

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


async def send_long(bot: Bot, chat_id: int, text: str) -> list[Message]:
    return [await bot.send_message(chat_id, chunk) for chunk in split_message(text)]


# разрешённые теги каналов Telegram и vc.ru; всё остальное экранируется
_TG_TAG = re.compile(r"(</?(?:b|blockquote)>)")
_BARE_AMP = re.compile(r"&(?!(?:amp|lt|gt|quot|#\d+);)")


def sanitize_telegram_html(text: str) -> str:
    """Экранирует голые <, >, & вне тегов <b> и <blockquote>."""
    parts = _TG_TAG.split(text)
    result = []
    for i, part in enumerate(parts):
        if i % 2 == 1:  # сам тег - не трогаем
            result.append(part)
        else:
            part = _BARE_AMP.sub("&amp;", part)
            result.append(part.replace("<", "&lt;").replace(">", "&gt;"))
    return "".join(result)


async def send_telegram_html(bot: Bot, chat_id: int, text: str) -> list[Message]:
    """Отправка с parse_mode=HTML; при битой разметке - откат на простой текст."""
    messages = []
    for chunk in split_message(text):
        try:
            messages.append(await bot.send_message(chat_id, chunk, parse_mode="HTML"))
        except TelegramBadRequest:
            logger.warning("HTML-разметка не прошла, отправляю как простой текст")
            messages.append(await bot.send_message(chat_id, chunk))
    return messages


async def _transcribe_item(ctx: AppContext, item: BufferedItem, file_id: str, filename: str) -> None:
    try:
        buffer = BytesIO()
        await ctx.bot.download(file_id, destination=buffer)
        item.text = await ctx.transcriber.transcribe(buffer.getvalue(), filename)
    except Exception as exc:  # noqa: BLE001 - причина уходит пользователю
        logger.exception("Транскрибация сообщения %d не удалась", item.index)
        item.error = str(exc)


async def _extract_feedback_text(ctx: AppContext, message: Message) -> str | None:
    """Достаёт текст правок из сообщения: текст как есть, голос - через Whisper."""
    if message.text:
        return message.text
    file_id = None
    filename = "voice.ogg"
    if message.voice:
        file_id = message.voice.file_id
    elif message.audio:
        file_id = message.audio.file_id
        filename = message.audio.file_name or "audio.mp3"
    if not file_id:
        return None
    try:
        buffer = BytesIO()
        await ctx.bot.download(file_id, destination=buffer)
        return await ctx.transcriber.transcribe(buffer.getvalue(), filename)
    except Exception:  # noqa: BLE001
        logger.exception("Транскрибация правок не удалась")
        return None


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
        "Серия из %d сообщений склеена, %d знаков. Каналы: %s",
        len(series.items),
        len(source_text),
        ", ".join(series.channels),
    )

    try:
        result = await run_pipeline(ctx.runner, source_text, series.channels)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Пайплайн упал")
        await ctx.bot.send_message(
            chat_id, f"Пайплайн не отработал: {exc}. Попробуйте ещё раз."
        )
        return

    await _send_result(ctx, chat_id, result)


async def _send_telegram_material(ctx: AppContext, chat_id: int, material: ChannelMaterial) -> list[Message]:
    text = f"{material.label}\n\n{sanitize_telegram_html(material.body)}"
    return await send_telegram_html(ctx.bot, chat_id, text)


async def _send_vcru_material(ctx: AppContext, chat_id: int, material: ChannelMaterial) -> list[Message]:
    lines = [material.label, ""]
    if material.title:
        lines += [f"Заголовок: {sanitize_telegram_html(material.title)}", ""]
    lines.append(sanitize_telegram_html(material.body))
    return await send_telegram_html(ctx.bot, chat_id, "\n".join(lines))


async def _send_threads_material(ctx: AppContext, chat_id: int, material: ChannelMaterial) -> list[Message]:
    """Постит цепочкой: первый пост, остальные - ответами в тред."""
    posts = material.posts or [material.body]
    messages = [
        await ctx.bot.send_message(
            chat_id, f"{material.label} - цепочка из {len(posts)}\n\n{posts[0]}"
        )
    ]
    for post in posts[1:]:
        messages.append(
            await ctx.bot.send_message(
                chat_id,
                post,
                reply_parameters=ReplyParameters(message_id=messages[-1].message_id),
            )
        )

    overlong = material.overlong_posts()
    if overlong:
        numbers = ", ".join(str(n) for n in overlong)
        await ctx.bot.send_message(
            chat_id,
            f"Внимание: в Threads посты {numbers} длиннее 500 знаков - сократи перед публикацией.",
        )
    return messages


_SENDERS = {
    "telegram": _send_telegram_material,
    "vcru": _send_vcru_material,
    "threads": _send_threads_material,
}


async def _send_material(
    ctx: AppContext, chat_id: int, material: ChannelMaterial, session: SessionState
) -> None:
    """Отправляет материал, запоминает его для правок реплаем."""
    messages = await _SENDERS[material.channel](ctx, chat_id, material)
    session.materials[material.channel] = material
    for message in messages:
        session.message_map[message.message_id] = material.channel

    if material.editor_status == "НА ПРАВКУ" and material.editor_issues:
        await send_long(
            ctx.bot,
            chat_id,
            f"Редактор про {material.label} - НА ПРАВКУ:\n{material.editor_issues}",
        )


async def _send_result(ctx: AppContext, chat_id: int, result: PipelineResult) -> None:
    # Мастер-бриф - внутренний рабочий документ, в чат не отправляется.
    session = SessionState(brief=result.brief)
    ctx.sessions[chat_id] = session

    for material in result.materials:
        await _send_material(ctx, chat_id, material, session)

    if result.gaps:
        await send_long(ctx.bot, chat_id, "Проверь перед публикацией:\n\n" + result.gaps)

    await ctx.bot.send_message(chat_id, FEEDBACK_HINT)


async def _handle_feedback(ctx: AppContext, message: Message, channel: str) -> None:
    session = ctx.sessions.get(message.chat.id)
    if session is None or channel not in session.materials:
        await message.answer("Не нашёл материал для правки - пришлите идею заново.")
        return

    feedback = await _extract_feedback_text(ctx, message)
    if not feedback:
        await message.answer("Не смог разобрать правки - напишите текстом, что поменять.")
        return

    label = CHANNEL_LABELS[channel]
    logger.info("Правки для %s: %d знаков", channel, len(feedback))
    note = await message.answer(f"Переделываю {label}...")
    try:
        material = await revise_material(
            ctx.runner,
            channel,
            session.brief,
            session.materials[channel].raw,
            feedback,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Правка не удалась")
        await message.answer(f"Правка не прошла: {exc}. Попробуйте ещё раз.")
        return
    try:
        await note.delete()
    except TelegramBadRequest:
        pass
    await _send_material(ctx, message.chat.id, material, session)


@router.message(CommandStart())
async def handle_start(message: Message, ctx: AppContext) -> None:
    if not ctx.is_allowed(message.from_user.id if message.from_user else None):
        return
    await message.answer(
        "Пришлите голосовое (можно несколько подряд) или текст с идеей.\n"
        "Кнопками можно выбрать, что генерировать: Telegram, vc.ru, Threads "
        "или комбинацию. По умолчанию - все три.\n"
        "Я подожду 20 секунд после последнего сообщения (или нажмите Собрать бриф) "
        "и пришлю готовые тексты.\n"
        "Правки: ответьте (реплаем) на нужный материал текстом или голосом - переделаю."
    )


@router.message(F.voice | F.audio | F.text)
async def handle_content(message: Message, ctx: AppContext) -> None:
    if not ctx.is_allowed(message.from_user.id if message.from_user else None):
        return

    chat_id = message.chat.id

    # ответ (реплай) на присланный материал = правки к нему
    session = ctx.sessions.get(chat_id)
    reply = message.reply_to_message
    if session and reply and reply.message_id in session.message_map:
        await _handle_feedback(ctx, message, session.message_map[reply.message_id])
        return

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
            STATUS_TEXT, reply_markup=series_keyboard()
        )

    _schedule_run(ctx, series)


@router.callback_query(F.data.in_(CHANNEL_CHOICES))
async def handle_channel_choice(callback: CallbackQuery, ctx: AppContext) -> None:
    if not ctx.is_allowed(callback.from_user.id):
        await callback.answer()
        return
    channels = CHANNEL_CHOICES[callback.data]
    labels = ", ".join(CHANNEL_LABELS[ch] for ch in channels)

    series = ctx.series.get(callback.message.chat.id) if callback.message else None
    if series is None:
        await callback.answer("Серия уже собрана - выбор применится к следующей идее")
        return
    series.channels = channels
    await callback.answer(f"Каналы: {labels}")
    try:
        await callback.message.edit_text(
            f"Слушаю, шлите ещё или нажмите Собрать бриф.\nКаналы: {labels}.",
            reply_markup=series_keyboard(),
        )
    except TelegramBadRequest:
        pass


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
