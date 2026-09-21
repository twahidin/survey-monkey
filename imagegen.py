"""Image generation providers for the survey media panel.

Each provider returns raw image bytes plus a MIME type so the image can be persisted
in the ``media_assets`` table and served from ``/api/assets/{id}``. Persisting the bytes
keeps transcripts and reports stable even after provider URLs expire.

Providers:

* ``pollinations`` – free, no key required (https://pollinations.ai). Optional token.
* ``openrouter``   – image-output models on OpenRouter (e.g. Gemini image models),
                     via the chat completions endpoint with ``modalities: ["image", "text"]``.
* ``openai``       – OpenAI Images API (``gpt-image-1``, ``dall-e-3``) or any
                     OpenAI-compatible endpoint via ``image_base_url``.
"""

import base64
import logging
import random
import re
from urllib.parse import quote

import httpx

from llm import LLMConfig, LLMError, OPENROUTER_BASE_URL, APP_URL

logger = logging.getLogger(__name__)

IMAGE_PROVIDERS = {
    "pollinations": {
        "label": "Pollinations.ai (free)",
        "needs_key": False,
        "default_model": "flux",
        "help": "Free community service, no key required. Quality varies; good for quick illustrations.",
    },
    "openrouter": {
        "label": "OpenRouter (image models)",
        "needs_key": True,
        "default_model": "google/gemini-2.5-flash-image-preview",
        "help": "Uses your OpenRouter key. Pick any model that supports image output.",
    },
    "openai": {
        "label": "OpenAI Images (or compatible)",
        "needs_key": True,
        "default_model": "gpt-image-1",
        "help": "Uses the OpenAI Images API. Set a base URL to point at a compatible provider.",
    },
}

MAX_PROMPT_CHARS = 900


def build_prompt(prompt: str, style: str = "") -> str:
    prompt = re.sub(r"\s+", " ", (prompt or "")).strip()
    style = re.sub(r"\s+", " ", (style or "")).strip()
    full = f"{prompt}. Style: {style}" if style else prompt
    return full[:MAX_PROMPT_CHARS]


def _decode_data_url(url: str):
    m = re.match(r"^data:([^;]+);base64,(.+)$", url, re.S)
    if not m:
        return None, None
    return base64.b64decode(m.group(2)), m.group(1)


async def _fetch_bytes(client: httpx.AsyncClient, url: str):
    resp = await client.get(url, follow_redirects=True)
    if resp.status_code != 200:
        raise LLMError(f"Image download failed ({resp.status_code}).", 502)
    mime = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
    if not mime.startswith("image/"):
        raise LLMError("Image provider returned a non-image response.", 502)
    return resp.content, mime


async def _pollinations(cfg: LLMConfig, prompt: str):
    model = cfg.image_model or "flux"
    seed = random.randint(1, 10_000_000)
    url = (
        f"https://image.pollinations.ai/prompt/{quote(prompt)}"
        f"?width=1024&height=640&nologo=true&seed={seed}&model={quote(model)}"
    )
    headers = {}
    if cfg.image_api_key:
        headers["Authorization"] = f"Bearer {cfg.image_api_key}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=15.0), headers=headers) as client:
        return await _fetch_bytes(client, url)


async def _openrouter(cfg: LLMConfig, prompt: str):
    if not cfg.image_api_key:
        raise LLMError("No OpenRouter key configured for image generation.", 500)
    model = cfg.image_model or IMAGE_PROVIDERS["openrouter"]["default_model"]
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": f"Generate a single image: {prompt}"}],
        "modalities": ["image", "text"],
    }
    headers = {
        "Authorization": f"Bearer {cfg.image_api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": APP_URL,
        "X-Title": "Survey Chatbot",
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0)) as client:
        resp = await client.post(f"{OPENROUTER_BASE_URL}/chat/completions", headers=headers, json=payload)
        if resp.status_code != 200:
            detail = ""
            try:
                detail = (resp.json().get("error") or {}).get("message", "")
            except Exception:
                pass
            raise LLMError(f"OpenRouter image error ({resp.status_code}): {detail}", 502)
        body = resp.json()
        msg = ((body.get("choices") or [{}])[0].get("message")) or {}
        images = msg.get("images") or []
        for im in images:
            url = ((im.get("image_url") or {}).get("url")) or im.get("url")
            if not url:
                continue
            if url.startswith("data:"):
                data, mime = _decode_data_url(url)
                if data:
                    return data, mime
            else:
                return await _fetch_bytes(client, url)
    raise LLMError("The selected OpenRouter model did not return an image. Choose an image-output model.", 502)


async def _openai(cfg: LLMConfig, prompt: str):
    if not cfg.image_api_key:
        raise LLMError("No API key configured for image generation.", 500)
    base = (cfg.image_base_url or "https://api.openai.com/v1").rstrip("/")
    model = cfg.image_model or IMAGE_PROVIDERS["openai"]["default_model"]
    payload = {"model": model, "prompt": prompt, "n": 1, "size": "1024x1024"}
    if model.startswith("gpt-image"):
        payload["quality"] = "low"
    else:
        payload["response_format"] = "b64_json"
    headers = {"Authorization": f"Bearer {cfg.image_api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0)) as client:
        resp = await client.post(f"{base}/images/generations", headers=headers, json=payload)
        if resp.status_code != 200:
            detail = ""
            try:
                detail = (resp.json().get("error") or {}).get("message", "")
            except Exception:
                pass
            raise LLMError(f"Image API error ({resp.status_code}): {detail}", 502)
        data = (resp.json().get("data") or [{}])[0]
        if data.get("b64_json"):
            return base64.b64decode(data["b64_json"]), "image/png"
        if data.get("url"):
            return await _fetch_bytes(client, data["url"])
    raise LLMError("Image API returned no image.", 502)


async def generate_image(cfg: LLMConfig, prompt: str):
    """Generate an image; returns ``(bytes, mime_type)``. Raises ``LLMError`` on failure."""
    full_prompt = build_prompt(prompt, cfg.image_style)
    provider = cfg.image_provider or "pollinations"
    if provider == "openrouter":
        return await _openrouter(cfg, full_prompt)
    if provider == "openai":
        return await _openai(cfg, full_prompt)
    return await _pollinations(cfg, full_prompt)
