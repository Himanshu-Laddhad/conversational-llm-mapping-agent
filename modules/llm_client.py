"""
llm_client.py
─────────────
Groq-only LLM client used across the app.
"""

import time
from typing import List, Dict, Any

from modules.usage_tracker import log_usage

# ── Provider registry ──────────────────────────────────────────────────────────

PROVIDERS: Dict[str, Dict[str, Any]] = {
    "groq": {
        "label": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "env_key": "GROQ_API_KEY",
    }
}

DEFAULT_MODELS: Dict[str, str] = {
    "groq": "llama-3.3-70b-versatile",
}


# ── Public function ────────────────────────────────────────────────────────────

def chat_complete(
    messages: List[Dict[str, str]],
    api_key: str,
    model: str,
    provider: str = "groq",
    temperature: float = 0.1,
    max_tokens: int = 1_000,
    caller: str = "",
) -> str:
    """
    Call the specified LLM provider and return the assistant reply as a string.

    Args:
        messages:    OpenAI-style message list:
                     [{"role": "system"|"user"|"assistant", "content": "..."}]
                     For Anthropic the system message is extracted automatically.
        api_key:     Provider API key.
        model:       Model identifier (provider-specific).
        provider:    One of "groq", "openai", "nvidia_nim", "anthropic".
        temperature: Sampling temperature (ignored for Anthropic).
        max_tokens:  Maximum tokens in the response.
        caller:      Optional label identifying which module made this call
                     (recorded in the usage log).

    Returns:
        The assistant reply as a stripped string.  Never returns None.

    Raises:
        ValueError: If provider is unknown.
        Any exception raised by the underlying SDK is propagated as-is.
    """
    if provider not in PROVIDERS:
        raise ValueError(
            f"Unknown provider {provider!r}. "
            f"Valid options: {list(PROVIDERS)}"
        )

    if provider != "groq":
        raise ValueError("Only provider='groq' is supported in this build.")
    return _groq_complete(messages, api_key, model, temperature, max_tokens, caller=caller)


# ── Internal helpers ───────────────────────────────────────────────────────────

def _groq_complete(
    messages: List[Dict[str, str]],
    api_key: str,
    model: str,
    temperature: float,
    max_tokens: int,
    caller: str = "",
) -> str:
    """Use Groq SDK only."""
    from groq import Groq  # type: ignore
    client = Groq(api_key=api_key)
    t0 = time.perf_counter()
    response = client.chat.completions.create(
        model=model,
        messages=messages,          # type: ignore[arg-type]
        temperature=temperature,
        max_tokens=max_tokens,
    )
    latency_ms = (time.perf_counter() - t0) * 1000

    usage = response.usage
    if usage is not None:
        log_usage(
            provider="groq",
            model=model,
            caller=caller,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            max_tokens=max_tokens,
            temperature=temperature,
            latency_ms=latency_ms,
        )

    return (response.choices[0].message.content or "").strip()
