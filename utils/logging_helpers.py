"""
Инструменты для подробного логирования.

`log_timing` — async context manager, который пишет в лог:
- начало операции (DEBUG)
- успешное завершение с временем в мс (DEBUG)
- ошибку с временем (WARNING — даже без DEBUG-уровня)
- отмену с временем (DEBUG)

Использование:

    async with log_timing("openai.chat.create", model="gpt-5", msgs=7):
        response = await client.chat.completions.create(...)

    # → 12:34:56.789 | DEBUG | utils.functions:42 | → openai.chat.create model=gpt-5 msgs=7
    # → 12:34:58.123 | DEBUG | utils.functions:42 | ✓ openai.chat.create OK (1334ms) model=gpt-5 msgs=7
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

logger = logging.getLogger("timing")


def _format_kwargs(kwargs: dict[str, Any]) -> str:
    if not kwargs:
        return ""
    parts = []
    for k, v in kwargs.items():
        if isinstance(v, str) and len(v) > 60:
            v = v[:57] + "..."
        parts.append(f"{k}={v}")
    return " " + " ".join(parts)


@asynccontextmanager
async def log_timing(name: str, **kwargs: Any):
    """
    Async context manager для тайминга и логирования операций.

    Все ключевые операции (OpenAI call, Groq call, Whisper, скачивания,
    парсинг документов) обёрнуты в это, чтобы при LOG_LEVEL=DEBUG было видно
    «что именно тормозит».
    """
    start = time.monotonic()
    ctx = _format_kwargs(kwargs)
    logger.debug(f"→ {name}{ctx}")
    try:
        yield
        elapsed_ms = (time.monotonic() - start) * 1000
        logger.debug(f"✓ {name} OK ({elapsed_ms:.0f}ms){ctx}")
    except asyncio.CancelledError:
        elapsed_ms = (time.monotonic() - start) * 1000
        logger.debug(f"⊘ {name} CANCELLED ({elapsed_ms:.0f}ms){ctx}")
        raise
    except Exception as e:
        elapsed_ms = (time.monotonic() - start) * 1000
        # Ошибки логируем даже без DEBUG-уровня — это всегда полезно
        logger.warning(
            f"✗ {name} FAIL ({elapsed_ms:.0f}ms){ctx}: "
            f"{type(e).__name__}: {e}"
        )
        raise


def log_event(name: str, **kwargs: Any) -> None:
    """Точечная запись события без тайминга. Удобно для милстоунов в пайплайне."""
    ctx = _format_kwargs(kwargs)
    logger.debug(f"• {name}{ctx}")
