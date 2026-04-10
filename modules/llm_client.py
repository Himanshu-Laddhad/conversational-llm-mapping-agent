"""
llm_client.py
─────────────
Unified LLM client for all four supported providers.

Supported providers
───────────────────
  groq       — Groq (llama-3.3-70b-versatile default)
  openai     — OpenAI (gpt-4o-mini default)
  nvidia_nim — NVIDIA NIM (meta/llama-3.3-70b-instruct default)
  anthropic  — Anthropic (claude-3-5-haiku-20241022 default)

All three of Groq, OpenAI, and NVIDIA NIM expose an OpenAI-compatible REST
API.  We use the ``openai`` SDK for all three, just swapping the base_url.
Anthropic uses its own SDK and response format; this module normalises the
difference so every caller receives a plain string.

Public API
──────────
  chat_complete(messages, api_key, model, provider, temperature, max_tokens)
      → str   — the assistant reply as a plain string

  PROVIDERS       dict[str, dict]  — metadata per provider
  DEFAULT_MODELS  dict[str, str]   — default model name per provider
"""

from typing import List, Dict, Any

# ── Provider registry ──────────────────────────────────────────────────────────

PROVIDERS: Dict[str, Dict[str, Any]] = {
    "groq": {
        "label":    "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "env_key":  "GROQ_API_KEY",
    },
    "openai": {
        "label":    "OpenAI",
        "base_url": None,   # openai SDK default
        "env_key":  "OPENAI_API_KEY",
    },
    "nvidia_nim": {
        "label":    "NVIDIA NIM",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "env_key":  "NVIDIA_API_KEY",
    },
    "anthropic": {
        "label":    "Anthropic",
        "base_url": None,   # anthropic SDK default
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

    if provider == "anthropic":
        return _anthropic_complete(messages, api_key, model, max_tokens)
    return _openai_compat_complete(messages, api_key, model, provider, temperature, max_tokens)


# ── Internal helpers ───────────────────────────────────────────────────────────

def _openai_compat_complete(
    messages: List[Dict[str, str]],
    api_key: str,
    model: str,
    provider: str,
    temperature: float,
    max_tokens: int,
) -> str:
    """Use the openai SDK (OpenAI-compatible endpoint) for Groq, OpenAI, NIM."""
    from openai import OpenAI  # type: ignore

    kwargs: Dict[str, Any] = {"api_key": api_key}
    base_url = PROVIDERS[provider]["base_url"]
    if base_url:
        kwargs["base_url"] = base_url

    client = OpenAI(**kwargs)
    response = client.chat.completions.create(
        model=model,
        messages=messages,          # type: ignore[arg-type]
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return (response.choices[0].message.content or "").strip()


def _anthropic_complete(
    messages: List[Dict[str, str]],
    api_key: str,
    model: str,
    max_tokens: int,
) -> str:
    """Use the anthropic SDK.  System message is extracted from the list."""
    import anthropic  # type: ignore

    # Anthropic expects system as a top-level param, not a message
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
    return (response.content[0].text or "").strip()
