"""LLM provider abstraction.

Two providers are supported:

* ``anthropic``  – Claude via the official Anthropic SDK (default).
* ``openrouter`` – any model on OpenRouter via its OpenAI-compatible HTTP API.

Both expose the same two operations used by the app:

* ``stream_chat``   – token-by-token streaming with tool-use support.
* ``complete_chat`` – single non-streaming completion (opening message, insights, wizard).

Tool calls are surfaced as ``{"type": "tool_use", "name": ..., "input": {...}}`` events
regardless of provider, so the rest of the app never needs to know which backend is in use.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

import anthropic
import httpx

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_CHAT_MODEL = os.environ.get("CLAUDE_CHAT_MODEL", "claude-haiku-4-5-20251001")
CLAUDE_ANALYSIS_MODEL = os.environ.get("CLAUDE_ANALYSIS_MODEL", "claude-sonnet-4-6")

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
OPENROUTER_CHAT_MODEL = os.environ.get("OPENROUTER_CHAT_MODEL", "anthropic/claude-haiku-4.5")
OPENROUTER_ANALYSIS_MODEL = os.environ.get("OPENROUTER_ANALYSIS_MODEL", "anthropic/claude-sonnet-4.5")
APP_URL = os.environ.get("APP_URL", "https://survey-chatbot.app")

PROVIDERS = ("anthropic", "openrouter")


class LLMError(Exception):
    """Raised when a provider call fails. ``message`` is safe to show to users."""

    def __init__(self, message: str, status: int = 500):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass
class LLMConfig:
    provider: str = "anthropic"
    model: str = ""
    analysis_model: str = ""
    api_key: str = ""
    # Visuals
    image_mode: str = "stock"          # none | stock | generate
    image_provider: str = "pollinations"
    image_model: str = ""
    image_base_url: str = ""
    image_api_key: str = ""
    image_style: str = ""
    extra: dict = field(default_factory=dict)

    def describe(self) -> str:
        return f"{self.provider}:{self.model}"


def default_model(provider: str, analysis: bool = False) -> str:
    if provider == "openrouter":
        return OPENROUTER_ANALYSIS_MODEL if analysis else OPENROUTER_CHAT_MODEL
    return CLAUDE_ANALYSIS_MODEL if analysis else CLAUDE_CHAT_MODEL


def default_api_key(provider: str) -> str:
    return OPENROUTER_API_KEY if provider == "openrouter" else ANTHROPIC_API_KEY


def normalise_config(cfg: LLMConfig) -> LLMConfig:
    """Fill in defaults so callers can rely on every field being populated."""
    if cfg.provider not in PROVIDERS:
        cfg.provider = "anthropic"
    if not cfg.model:
        cfg.model = default_model(cfg.provider)
    if not cfg.analysis_model:
        # If the user picked a specific model, use it for analysis too (OpenRouter);
        # for Anthropic keep the dedicated analysis model unless overridden.
        cfg.analysis_model = cfg.model if cfg.provider == "openrouter" else default_model("anthropic", analysis=True)
    if not cfg.api_key:
        cfg.api_key = default_api_key(cfg.provider)
    if not cfg.image_mode:
        cfg.image_mode = "stock"
    if not cfg.image_provider:
        cfg.image_provider = "pollinations"
    return cfg


# ────────────────────────── message helpers ──────────────────────────

def merge_consecutive(messages: list) -> list:
    """Merge consecutive same-role messages (some OpenAI-compatible models reject them)."""
    out = []
    for m in messages:
        if out and out[-1]["role"] == m["role"] and isinstance(out[-1]["content"], str) and isinstance(m["content"], str):
            out[-1] = {"role": m["role"], "content": out[-1]["content"] + "\n\n" + m["content"]}
        else:
            out.append({"role": m["role"], "content": m["content"]})
    return out


def to_openai_tools(tools: Optional[list]) -> Optional[list]:
    if not tools:
        return None
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            },
        }
        for t in tools
    ]


def _friendly_http_error(resp: httpx.Response, provider: str) -> LLMError:
    detail = ""
    try:
        body = resp.json()
        detail = (body.get("error") or {}).get("message") or body.get("message") or ""
    except Exception:
        detail = resp.text[:200]
    if resp.status_code in (401, 403):
        return LLMError(f"{provider} rejected the API key. Please check the key in Settings.", 500)
    if resp.status_code == 402:
        return LLMError(f"{provider} reports insufficient credits. {detail}".strip(), 500)
    if resp.status_code == 404:
        return LLMError(f"{provider} could not find the model. {detail}".strip(), 500)
    if resp.status_code == 429:
        return LLMError(f"{provider} rate limit reached. Please try again shortly.", 503)
    return LLMError(f"{provider} error ({resp.status_code}): {detail or 'unknown error'}", 500)


# ────────────────────────── Anthropic ──────────────────────────

def _anthropic_client(cfg: LLMConfig) -> anthropic.AsyncAnthropic:
    if not cfg.api_key:
        raise LLMError("No Anthropic API key configured. Add one in Settings or set ANTHROPIC_API_KEY.", 500)
    return anthropic.AsyncAnthropic(api_key=cfg.api_key)


def _wrap_anthropic_error(e: Exception) -> LLMError:
    if isinstance(e, anthropic.AuthenticationError):
        return LLMError("Anthropic rejected the API key. Please check the key in Settings.", 500)
    if isinstance(e, anthropic.RateLimitError):
        return LLMError("Anthropic rate limit reached. Please try again shortly.", 503)
    if isinstance(e, anthropic.NotFoundError):
        return LLMError("Anthropic could not find the configured model.", 500)
    if isinstance(e, anthropic.APIStatusError):
        return LLMError(f"Anthropic error ({e.status_code}): {getattr(e, 'message', str(e))}", 500)
    if isinstance(e, anthropic.APIConnectionError):
        return LLMError("Could not reach Anthropic. Please try again.", 503)
    return LLMError(str(e), 500)


async def _anthropic_stream(cfg, model, system, messages, tools, max_tokens) -> AsyncIterator[dict]:
    client = _anthropic_client(cfg)
    kwargs = dict(model=model, max_tokens=max_tokens, system=system, messages=messages)
    if tools:
        kwargs["tools"] = tools
    tool_uses = {}
    try:
        async with client.messages.stream(**kwargs) as stream:
            async for event in stream:
                if event.type == "content_block_delta":
                    delta = event.delta
                    if delta.type == "text_delta":
                        yield {"type": "text", "text": delta.text}
                    elif delta.type == "input_json_delta" and event.index in tool_uses:
                        tool_uses[event.index]["input_json"] += delta.partial_json
                elif event.type == "content_block_start":
                    block = event.content_block
                    if block.type == "tool_use":
                        tool_uses[event.index] = {"name": block.name, "input_json": ""}
                elif event.type == "content_block_stop" and event.index in tool_uses:
                    tool = tool_uses.pop(event.index)
                    try:
                        tool_input = json.loads(tool["input_json"]) if tool["input_json"] else {}
                    except json.JSONDecodeError:
                        tool_input = {}
                    yield {"type": "tool_use", "name": tool["name"], "input": tool_input}
    except anthropic.APIError as e:
        raise _wrap_anthropic_error(e)


async def _anthropic_complete(cfg, model, system, messages, tools, max_tokens) -> dict:
    client = _anthropic_client(cfg)
    kwargs = dict(model=model, max_tokens=max_tokens, system=system, messages=messages)
    if tools:
        kwargs["tools"] = tools
    try:
        response = await client.messages.create(**kwargs)
    except anthropic.APIError as e:
        raise _wrap_anthropic_error(e)
    text_parts, tool_calls = [], []
    for block in response.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            tool_calls.append({"name": block.name, "input": block.input or {}})
    return {"text": "".join(text_parts), "tool_calls": tool_calls, "stop_reason": response.stop_reason}


# ────────────────────────── OpenRouter (OpenAI-compatible) ──────────────────────────

def _openrouter_headers(cfg: LLMConfig) -> dict:
    if not cfg.api_key:
        raise LLMError("No OpenRouter API key configured. Add one in Settings or set OPENROUTER_API_KEY.", 500)
    return {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": APP_URL,
        "X-Title": "Survey Chatbot",
    }


def _openai_messages(system: str, messages: list) -> list:
    out = []
    if system:
        out.append({"role": "system", "content": system})
    out.extend(merge_consecutive(messages))
    return out


async def _openrouter_stream(cfg, model, system, messages, tools, max_tokens) -> AsyncIterator[dict]:
    payload = {
        "model": model,
        "messages": _openai_messages(system, messages),
        "max_tokens": max_tokens,
        "stream": True,
    }
    oa_tools = to_openai_tools(tools)
    if oa_tools:
        payload["tools"] = oa_tools
        payload["tool_choice"] = "auto"
    pending_tools = {}  # index -> {id, name, arguments}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0)) as client:
            async with client.stream("POST", f"{OPENROUTER_BASE_URL}/chat/completions",
                                     headers=_openrouter_headers(cfg), json=payload) as resp:
                if resp.status_code != 200:
                    await resp.aread()
                    raise _friendly_http_error(resp, "OpenRouter")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("error"):
                        raise LLMError(f"OpenRouter error: {chunk['error'].get('message', 'unknown')}", 500)
                    for choice in chunk.get("choices", []):
                        delta = choice.get("delta") or {}
                        content = delta.get("content")
                        if content:
                            yield {"type": "text", "text": content}
                        for tc in delta.get("tool_calls") or []:
                            idx = tc.get("index", 0)
                            slot = pending_tools.setdefault(idx, {"name": "", "arguments": ""})
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] = fn["name"]
                            if fn.get("arguments"):
                                slot["arguments"] += fn["arguments"]
    except httpx.HTTPError as e:
        raise LLMError(f"Could not reach OpenRouter: {e}", 503)
    for idx in sorted(pending_tools):
        slot = pending_tools[idx]
        if not slot["name"]:
            continue
        try:
            args = json.loads(slot["arguments"]) if slot["arguments"] else {}
        except json.JSONDecodeError:
            args = {}
        yield {"type": "tool_use", "name": slot["name"], "input": args}


async def _openrouter_complete(cfg, model, system, messages, tools, max_tokens) -> dict:
    payload = {
        "model": model,
        "messages": _openai_messages(system, messages),
        "max_tokens": max_tokens,
    }
    oa_tools = to_openai_tools(tools)
    if oa_tools:
        payload["tools"] = oa_tools
        payload["tool_choice"] = "auto"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0)) as client:
            resp = await client.post(f"{OPENROUTER_BASE_URL}/chat/completions",
                                     headers=_openrouter_headers(cfg), json=payload)
    except httpx.HTTPError as e:
        raise LLMError(f"Could not reach OpenRouter: {e}", 503)
    if resp.status_code != 200:
        raise _friendly_http_error(resp, "OpenRouter")
    body = resp.json()
    if body.get("error"):
        raise LLMError(f"OpenRouter error: {body['error'].get('message', 'unknown')}", 500)
    choice = (body.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    text = msg.get("content") or ""
    if isinstance(text, list):  # some models return content parts
        text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
    tool_calls = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        tool_calls.append({"name": fn.get("name", ""), "input": args})
    return {"text": text, "tool_calls": tool_calls, "stop_reason": choice.get("finish_reason")}


# ────────────────────────── public API ──────────────────────────

async def stream_chat(cfg: LLMConfig, system: str, messages: list, tools: Optional[list] = None,
                      max_tokens: int = 1024, analysis: bool = False) -> AsyncIterator[dict]:
    """Yield ``{"type": "text"}`` and ``{"type": "tool_use"}`` events."""
    cfg = normalise_config(cfg)
    model = cfg.analysis_model if analysis else cfg.model
    if cfg.provider == "openrouter":
        gen = _openrouter_stream(cfg, model, system, messages, tools, max_tokens)
    else:
        gen = _anthropic_stream(cfg, model, system, messages, tools, max_tokens)
    async for ev in gen:
        yield ev


async def complete_chat(cfg: LLMConfig, system: str, messages: list, tools: Optional[list] = None,
                        max_tokens: int = 1024, analysis: bool = False) -> dict:
    """Return ``{"text": str, "tool_calls": [...], "stop_reason": str}``."""
    cfg = normalise_config(cfg)
    model = cfg.analysis_model if analysis else cfg.model
    if cfg.provider == "openrouter":
        return await _openrouter_complete(cfg, model, system, messages, tools, max_tokens)
    return await _anthropic_complete(cfg, model, system, messages, tools, max_tokens)


async def list_openrouter_models(api_key: str = "") -> dict:
    """Return chat and image-capable models from OpenRouter for the settings dropdowns."""
    headers = {"HTTP-Referer": APP_URL, "X-Title": "Survey Chatbot"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(f"{OPENROUTER_BASE_URL}/models", headers=headers)
    except httpx.HTTPError as e:
        raise LLMError(f"Could not reach OpenRouter: {e}", 503)
    if resp.status_code != 200:
        raise _friendly_http_error(resp, "OpenRouter")
    chat, image = [], []
    for m in resp.json().get("data", []):
        arch = m.get("architecture") or {}
        outputs = arch.get("output_modalities") or []
        pricing = m.get("pricing") or {}
        entry = {
            "id": m.get("id"),
            "name": m.get("name") or m.get("id"),
            "prompt_price": pricing.get("prompt"),
            "completion_price": pricing.get("completion"),
            "context_length": m.get("context_length"),
        }
        if "image" in outputs:
            image.append(entry)
        if "text" in outputs or not outputs:
            chat.append(entry)
    chat.sort(key=lambda x: x["name"].lower())
    image.sort(key=lambda x: x["name"].lower())
    return {"chat": chat, "image": image}


def strip_json_fences(raw: str) -> str:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
    if raw.endswith("```"):
        raw = raw[:-3].strip()
    if raw.startswith("json"):
        raw = raw[4:].strip()
    return raw


def extract_json_object(raw: str) -> Optional[dict]:
    """Best-effort extraction of the first JSON object in a model response."""
    raw = strip_json_fences(raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None
