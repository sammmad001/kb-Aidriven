"""Few-shot example loader for LLM prompts.

V1.1: Externalizes few-shot examples from code into config/prompts/ directory
for easier maintenance and iteration without modifying source code.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

# Default prompts directory relative to kb/ project root
# fewshot.py lives in app/ingest/, so parent.parent.parent = kb/
_DEFAULT_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "config" / "prompts"


def _resolve_prompts_dir() -> Path:
    """Resolve the prompts directory, falling back to default."""
    return _DEFAULT_PROMPTS_DIR


@lru_cache(maxsize=8)
def load_fewshot(name: str) -> str:
    """Load a few-shot example file by name.

    Args:
        name: File name without path, e.g. 'analyze_fewshot.txt'

    Returns:
        Content of the few-shot file, or empty string if not found.
    """
    prompts_dir = _resolve_prompts_dir()
    file_path = prompts_dir / name
    try:
        content = file_path.read_text(encoding="utf-8").strip()
        logger.debug("Loaded few-shot examples from %s (%d chars)", file_path, len(content))
        return content
    except FileNotFoundError:
        logger.debug("Few-shot file not found: %s (will use zero-shot)", file_path)
        return ""
    except Exception as exc:
        logger.warning("Failed to load few-shot file %s: %s", file_path, exc)
        return ""
