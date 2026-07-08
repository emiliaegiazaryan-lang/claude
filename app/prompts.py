"""Загрузка системных промптов из папки prompts/.

Промпты лежат отдельно от кода: правка файла в prompts/ не требует правки кода.
Тексты промптов дословно совпадают с skeylo-agenty-kontent-konveyer.md.
"""

from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Не найден промпт {path}")
    return path.read_text(encoding="utf-8").strip()
