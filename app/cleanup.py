"""Механическая чистка текста до редактора. Детерминированно, без модели.

Правила: тире заменяется на дефис, восклицательные знаки убираются
(в конце предложения превращаются в точку), эмодзи удаляются.
HTML-теги Telegram эти операции не затрагивают: замены не касаются
символов <, > и букв.
"""

import re

# все виды тире: figure dash, en dash, em dash, horizontal bar
_DASHES = re.compile(r"[‒–—―]")

# восклицательные знаки (включая двойной и инвертированный)
_EXCLAMATIONS = re.compile(r"[!‼¡]+")

# эмодзи и пиктограммы; рубль (₽, U+20BD) и стрелки-дефисы не задеваются
_EMOJI = re.compile(
    "["
    "\U0001f000-\U0001faff"   # основные блоки эмодзи
    "\U0001f1e6-\U0001f1ff"   # флаги
    "☀-➿"           # значки, дингбаты
    "⬀-⯿"           # стрелки-значки
    "️"                  # селектор эмодзи-представления
    "‍"                  # zero-width joiner
    "]+"
)

_EXCLAMATION_AT_END = re.compile(r"[!‼¡]+(?=\s|$)")

# двойные пробелы внутри строки (остаются после удаления эмодзи);
# отступы в начале строк не трогаем
_DOUBLE_SPACES = re.compile(r"(?<=\S) {2,}(?=\S)")


def mechanical_cleanup(text: str) -> str:
    text = _DASHES.sub("-", text)
    text = _EMOJI.sub("", text)
    text = _DOUBLE_SPACES.sub(" ", text)
    # в конце предложения "Внимание!" -> "Внимание.", в остальных местах знак просто убирается
    text = _EXCLAMATION_AT_END.sub(".", text)
    text = _EXCLAMATIONS.sub("", text)
    # подчищаем хвостовые пробелы построчно
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()
