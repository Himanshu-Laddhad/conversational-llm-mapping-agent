"""
llm_client.py
─────────────
Unified LLM client (Groq, OpenAI, NVIDIA NIM, Anthropic).
Accepts both caller= and engine= for backward compatibility.
"""

import json
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

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
    "openai":     "gpt-4.1-mini",
    "nvidia_nim": "meta/llama-3.3-70b-instruct",
    "anthropic":  "claude-3-5-haiku-20241022",
}

# Per-provider env vars that override DEFAULT_MODELS at runtime.
_MODEL_ENV_VARS: Dict[str, str] = {
    "groq":       "GROQ_MODEL",
    "openai":     "OPENAI_MODEL",
    "nvidia_nim": "NVIDIA_MODEL",
    "anthropic":  "ANTHROPIC_MODEL",
}


def get_default_model(provider: str, engine: Optional[str] = None) -> str:
    """
    Return the model to use for the given provider and (optionally) engine/intent.

    Resolution order:
      1. <ENGINE>_MODEL env var     e.g. EXPLAIN_MODEL, MODIFY_MODEL (engine-specific)
      2. <PROVIDER>_MODEL env var   e.g. OPENAI_MODEL, GROQ_MODEL    (provider-wide)
      3. DEFAULT_MODELS[provider]   hardcoded fallback

    Engine names match intent names: explain, modify, simulate, audit, generate.
    """
    import os as _os
    if engine:
        engine_val = _os.getenv(f"{engine.upper()}_MODEL", "")
        if engine_val:
            return engine_val
    provider_val = _os.getenv(_MODEL_ENV_VARS.get(provider, ""), "")
    return provider_val or DEFAULT_MODELS.get(provider, "gpt-4.1-mini")


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


def chat_complete_with_tools(
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    tool_executor: Callable[[str, dict], dict],
    api_key: str,
    model: str,
    provider: str = "openai",
    temperature: float = 0.1,
    max_tokens: int = 4096,
    max_tool_rounds: int = 10,
    engine: str = "explain",
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Run an OpenAI-compatible function-calling loop until the model stops
    issuing tool calls or max_tool_rounds is reached.

    Supported providers: openai, groq, nvidia_nim.
    Anthropic uses a different tool_use format and raises NotImplementedError.

    Args:
        messages:       Initial message list (system + user turns).
        tools:          OpenAI-schema tool definitions list.
        tool_executor:  Callable(tool_name: str, args: dict) -> dict.
                        Should be a pure Python function — no LLM calls.
        api_key:        Provider API key.
        model:          Model identifier.
        provider:       One of openai / groq / nvidia_nim.
        temperature:    Sampling temperature.
        max_tokens:     Max completion tokens per round.
        max_tool_rounds: Hard limit on tool-call iterations.
        engine:         Label for token tracking.

    Returns:
        (response_text, final_messages) where final_messages includes all
        intermediate tool call / tool result messages appended in order.
        Callers can store final_messages directly into conversation history.
    """
    if provider == "anthropic":
        raise NotImplementedError(
            "chat_complete_with_tools does not support Anthropic — "
            "use chat_complete() which falls back to the truncated-JSON path."
        )
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown provider {provider!r}. Valid: {list(PROVIDERS)}")

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

    client = OpenAI(**kwargs)

    # Work on a mutable copy so the caller's original list is not mutated.
    msgs = list(messages)
    last_text = ""

    for _round in range(max_tool_rounds):
        t0 = time.perf_counter()
        response = client.chat.completions.create(
            model=model,
            messages=msgs,          # type: ignore[arg-type]
            tools=tools,            # type: ignore[arg-type]
            tool_choice="auto",
            temperature=temperature,
            max_tokens=max_tokens,
        )
        latency_ms = (time.perf_counter() - t0) * 1000

        choice = response.choices[0]
        usage  = getattr(response, "usage", None)

        # Record token usage
        if get_tracker is not None and usage is not None:
            try:
                get_tracker().record(engine=engine, model=model, usage=usage)
            except Exception:
                pass
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

        # No tool calls → model is done
        if choice.finish_reason != "tool_calls":
            last_text = (choice.message.content or "").strip()
            # Append the assistant message to the working list
            msgs.append({"role": "assistant", "content": last_text})
            break

        # The model issued one or more tool calls — execute them all
        assistant_msg = choice.message
        # Build the assistant message dict preserving tool_calls
        tc_list = assistant_msg.tool_calls or []
        msgs.append({
            "role":       "assistant",
            "content":    assistant_msg.content,  # may be None
            "tool_calls": [
                {
                    "id":       tc.id,
                    "type":     "function",
                    "function": {
                        "name":      tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in tc_list
            ],
        })

        for tc in tc_list:
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                args = {}
            result = tool_executor(tc.function.name, args)
            result_str = json.dumps(result, ensure_ascii=False)
            msgs.append({
                "role":         "tool",
                "tool_call_id": tc.id,
                "content":      result_str,
            })

    else:
        # Exceeded max_tool_rounds — return whatever the last assistant text was
        pass

    return last_text, msgs


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
