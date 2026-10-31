"""Validated operational settings; no silent fallback for malformed values."""
import math,os


def llm_timeout_seconds():
    try:value=float(os.getenv('LLM_TIMEOUT_S','30'))
    except ValueError:raise RuntimeError('INVALID_LLM_TIMEOUT_S') from None
    if not math.isfinite(value) or not 30<=value<=90:raise RuntimeError('INVALID_LLM_TIMEOUT_S')
    return value
