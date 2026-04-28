"""
llm_client.py
─────────────
Unified LLM client (Groq, OpenAI, NVIDIA NIM, Anthropic).
Accepts both caller= and engine= for backward compatibility.
"""

import time
from typing import List, Dict, Any, Optional

try:
    from modules.usage_tracker import log_usage as _log_usage
except Exception:
    _log_usage = None  # usage_tracker is optional

# ── Provider registry ──────────────────────────────────────────────────────────

PROVIDERS: Dict[str, Dict[str, Any]] = {
    "groq": {
        "label":    "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "env_key":  "GROQ_API_KEY",
    },
    "openai": {
        "label":    "OpenAI",
        "base_url": None,
        "env_key":  "OPENAI_API_KEY",
    },
    "nvidia_nim": {
        "label":    "NVIDIA NIM",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "env_key":  "NVIDIA_API_KEY",
    },
    "anthropic": {
        "label":    "Anthropic",
        "base_url": None,
        "env_key":  "ANTHROPIC_API_KEY",
    },
}

DEFAULT_MODELS: Dict[str, str] = {
    "groq":       "llama-3.3-70b-versatile",
    "openai":     "gpt-4o-mini",
    "nvidia_nim": "meta/llama-3.3-70b-instruct",
    "anthropic":  "claude-3-5-haiku-20241022",
}


# ── Public function ────────────────────────────────────────────────────────────

def chat_complete(
    messages: List[Dict[str, str]],
    api_key: str,
    model: str,
    provider: str = "groq",
    temperature: float = 0.1,
    max_tokens: int = 1_000,
    engine: str = "unknown",   # token tracking label
    caller: str = "",          # alias for engine (backward compat)
) -> str:
    """
    Call the specified LLM provider and return the assistant reply as a string.
    Accepts both engine= and caller= (caller takes precedence if both are set).
    """
    _label = caller or engine or "unknown"

    if provider not in PROVIDERS:
        raise ValueError(
            f"Unknown provider {provider!r}. "
            f"Valid options: {list(PROVIDERS)}"
        )

    if provider == "anthropic":
        return _anthropic_complete(messages, api_key, model, max_tokens, engine=_label)
    return _openai_compat_complete(messages, api_key, model, provider, temperature, max_tokens, engine=_label)


# ── Internal helpers ───────────────────────────────────────────────────────────

def _openai_compat_complete(
    messages: List[Dict[str, str]],
    api_key: str,
    model: str,
    provider: str,
    temperature: float,
    max_tokens: int,
    engine: str = "unknown",
) -> str:
    """Use the openai SDK (OpenAI-compatible endpoint) for Groq, OpenAI, NIM."""
    from openai import OpenAI  # type: ignore

    try:
        from .token_tracker import get_tracker
    except ImportError:
        try:
            from token_tracker import get_tracker  # type: ignore
        except ImportError:
            get_tracker = None

    kwargs: Dict[str, Any] = {"api_key": api_key}
    base_url = PROVIDERS[provider]["base_url"]
    if base_url:
        kwargs["base_url"] = base_url

    t0 = time.perf_counter()
    client = OpenAI(**kwargs)
    response = client.chat.completions.create(
        model=model,
        messages=messages,          # type: ignore[arg-type]
        temperature=temperature,
        max_tokens=max_tokens,
    )
    latency_ms = (time.perf_counter() - t0) * 1000

    usage = getattr(response, "usage", None)

    # Record in token_tracker (sidebar stats)
    if get_tracker is not None and usage is not None:
        try:
            get_tracker().record(engine=engine, model=model, usage=usage)
        except Exception:
            pass

    # Record in usage_tracker (JSONL log file)
    if _log_usage is not None and usage is not None:
        try:
            _log_usage(
                provider=provider,
                model=model,
                caller=engine,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
                max_tokens=max_tokens,
                temperature=temperature,
                latency_ms=latency_ms,
            )
        except Exception:
            pass

    return (response.choices[0].message.content or "").strip()


def _anthropic_complete(
    messages: List[Dict[str, str]],
    api_key: str,
    model: str,
    max_tokens: int,
    engine: str = "unknown",
) -> str:
    """Use the anthropic SDK."""
    import anthropic  # type: ignore

    try:
        from .token_tracker import get_tracker
    except ImportError:
        try:
            from token_tracker import get_tracker  # type: ignore
        except ImportError:
            get_tracker = None

    system = ""
    chat_messages: List[Dict[str, str]] = []
    for m in messages:
        if m.get("role") == "system":
            system = m.get("content", "")
        else:
            chat_messages.append(m)

    client = anthropic.Anthropic(api_key=api_key)
    kwargs: Dict[str, Any] = {
        "model":      model,
        "messages":   chat_messages,
        "max_tokens": max_tokens,
    }
    if system:
        kwargs["system"] = system

    response = client.messages.create(**kwargs)

    if get_tracker is not None:
        try:
            class _UsageAdapter:
                def __init__(self, r):
                    self.prompt_tokens     = getattr(r.usage, "input_tokens", 0)
                    self.completion_tokens = getattr(r.usage, "output_tokens", 0)
                    self.total_tokens      = self.prompt_tokens + self.completion_tokens
            get_tracker().record(engine=engine, model=model, usage=_UsageAdapter(response))
        except Exception:
            pass

    return (response.content[0].text or "").strip()
