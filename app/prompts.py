"""Загрузка системных промптов из папки prompts/.

Промпты лежат отдельно от кода: правка файла в prompts/ не требует правки кода.
Тексты промптов дословно совпадают с skeylo-agenty-kontent-konveyer.md.
"""

from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Из каких блоков собирается системный промпт каждого агента.
# Стилевые блоки (карта, эталоны) стоят перед форматными правилами агента.
# Источник стиля: skeylo-stilevaya-karta.md, разложен по файлам в prompts/.
PROMPT_COMPOSITION: dict[str, tuple[str, ...]] = {
    "parser": ("parser",),
    "editor": ("editor",),
    "telegram": ("style_card", "etalons_telegram", "telegram"),
    "vcru": ("style_card", "format_vcru", "etalons_vcru", "vcru"),
    "threads": ("style_card", "threads"),
}

BLOCK_SEPARATOR = "\n\n---\n\n"


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Не найден промпт {path}")
    return path.read_text(encoding="utf-8").strip()


@lru_cache(maxsize=None)
def build_system_prompt(agent: str) -> str:
    """Собирает системный промпт агента из блоков в prompts/."""
    parts = PROMPT_COMPOSITION.get(agent, (agent,))
    return BLOCK_SEPARATOR.join(load_prompt(part) for part in parts)
