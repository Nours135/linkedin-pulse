"""Shared LLM middleware: one Anthropic client, multimodal input, response cache and usage logging.

    from pulse.llm import llm, image, pdf
    text = llm.complete("Summarize this JD", system="...")
    text = llm.complete(["What does this screenshot show?", image("shot.png")])

- Inputs: a str, a content block, or a list mixing both. image()/pdf() accept a path, URL or bytes.
- Cache: identical requests are served from work/llm_cache/ (pass cache=False to skip);
  Anthropic prompt caching is also on, so repeated long prefixes are billed at the cache-read rate.
- Usage: every call appends one JSON line to work/llm_usage.jsonl (tokens, cost, latency, cache hit).
- Config: model and max_tokens come from the llm section of config.yaml; ANTHROPIC_API_KEY from .env.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from pulse.config import settings, work_dir as WORK_DIR

log = logging.getLogger(__name__)

LLM_SETTINGS = settings.get("llm", {})
DEFAULT_MODEL = LLM_SETTINGS.get("model", "claude-haiku-5-5")
DEFAULT_MAX_TOKENS = LLM_SETTINGS.get("max_tokens", 16000)

# USD per 1M tokens: (input, output). Cache writes bill 1.25x input, cache reads 0.1x input.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}


class LLMError(RuntimeError):
    pass


# ---- Content blocks ----

def _load(src: str | Path | bytes) -> tuple[bytes | None, str | None]:
    """Return (raw bytes, None) for a file or bytes, or (None, url) for an http(s) URL."""
    if isinstance(src, bytes):
        return src, None
    if isinstance(src, str) and src.startswith(("http://", "https://")):
        return None, src
    return Path(src).read_bytes(), None


def image(src: str | Path | bytes, media_type: str | None = None) -> dict:
    """Image block from a path, URL or raw bytes (png/jpeg/gif/webp)."""
    data, url = _load(src)
    if url:
        return {"type": "image", "source": {"type": "url", "url": url}}
    media_type = media_type or mimetypes.guess_type(str(src))[0] or "image/png"
    return {"type": "image", "source": {
        "type": "base64", "media_type": media_type,
        "data": base64.standard_b64encode(data).decode(),
    }}


def pdf(src: str | Path | bytes) -> dict:
    """PDF document block from a path, URL or raw bytes."""
    data, url = _load(src)
    if url:
        return {"type": "document", "source": {"type": "url", "url": url}}
    return {"type": "document", "source": {
        "type": "base64", "media_type": "application/pdf",
        "data": base64.standard_b64encode(data).decode(),
    }}


def _content(prompt: str | dict | list) -> list[dict]:
    items = prompt if isinstance(prompt, list) else [prompt]
    return [{"type": "text", "text": p} if isinstance(p, str) else p for p in items]


# ---- Middleware ----

class LLM:
    def __init__(self, model: str = DEFAULT_MODEL, work_dir: Path = WORK_DIR,
                 max_tokens: int = DEFAULT_MAX_TOKENS):
        self._client: anthropic.Anthropic | None = None
        self.model = model
        self.max_tokens = max_tokens
        self.cache_dir = work_dir / "llm_cache"
        self.usage_log = work_dir / "llm_usage.jsonl"

    @property
    def client(self) -> anthropic.Anthropic:
        # Created lazily so importing this module never needs credentials
        if self._client is None:
            api_key = os.getenv("ANTHROPIC_API_KEY")
            if not api_key:
                raise LLMError("ANTHROPIC_API_KEY is not set; add it to .env")
            self._client = anthropic.Anthropic(api_key=api_key)
        return self._client

    def complete(
        self,
        prompt: str | dict | list,
        *,
        system: str | None = None,
        history: list[dict] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        effort: str | None = None,
        cache: bool = True,
        tag: str = "",
    ) -> str:
        """Send one user turn (after optional history) and return the reply text.
        tag labels the call in the usage log, e.g. "resume.select"."""
        params = {
            "model": model or self.model,
            "max_tokens": max_tokens or self.max_tokens,
            "messages": [*(history or []), {"role": "user", "content": _content(prompt)}],
        }
        # Prompt caching: an explicit breakpoint on the stable system prompt, plus automatic
        # caching of the conversation tail only when there is history to reuse. A one-off user
        # message is never read back, so caching it would just pay the write premium.
        # Prefixes below the model minimum (512 tokens on Haiku 5.5) silently don't cache.
        if system:
            params["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        if history:
            params["cache_control"] = {"type": "ephemeral"}
        if effort:
            params["output_config"] = {"effort": effort}

        key = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
        cache_file = self.cache_dir / f"{key}.json"
        if cache and cache_file.exists():
            hit = json.loads(cache_file.read_text())
            self._log(params["model"], tag, usage=None, latency=0.0, cached=True)
            return hit["text"]

        start = time.monotonic()
        resp = self.client.messages.create(**params)
        latency = time.monotonic() - start

        self._log(params["model"], tag, usage=resp.usage, latency=latency, cached=False,
                  stop_reason=resp.stop_reason, request_id=resp._request_id)
        if resp.stop_reason == "refusal":
            raise LLMError(f"model refused: {resp.stop_details}")
        if resp.stop_reason == "max_tokens":
            log.warning("llm %s hit max_tokens=%s, output truncated", tag, params["max_tokens"])

        text = "".join(b.text for b in resp.content if b.type == "text")
        if cache and resp.stop_reason == "end_turn":
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps({"model": params["model"], "text": text}))
        return text

    def complete_json(self, prompt: str | dict | list, **kwargs) -> dict | list:
        """complete() and parse the reply as JSON, tolerating a ```json fence."""
        text = self.complete(prompt, **kwargs).strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        return json.loads(text)

    def _log(self, model: str, tag: str, usage, latency: float, cached: bool, **extra) -> None:
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "model": model, "tag": tag, "cached": cached, "latency_s": round(latency, 2)}
        if usage is not None:
            tokens = {
                "input": usage.input_tokens,
                "output": usage.output_tokens,
                "cache_write": usage.cache_creation_input_tokens or 0,
                "cache_read": usage.cache_read_input_tokens or 0,
            }
            rec.update(tokens=tokens, cost_usd=round(cost(model, tokens), 6), **extra)
        log.info("llm %s %s", tag or model, rec.get("tokens", "cache hit"))
        self.usage_log.parent.mkdir(parents=True, exist_ok=True)
        with self.usage_log.open("a") as f:
            f.write(json.dumps(rec) + "\n")


def cost(model: str, tokens: dict) -> float:
    inp, out = PRICES.get(model, (0.0, 0.0))
    return (tokens["input"] * inp + tokens["cache_write"] * inp * 1.25
            + tokens["cache_read"] * inp * 0.1 + tokens["output"] * out) / 1e6


def usage_summary(path: Path = WORK_DIR / "llm_usage.jsonl") -> dict:
    """Totals over the usage log: calls, cache hits, tokens and cost, per tag."""
    by_tag: dict[str, dict] = {}
    for line in path.read_text().splitlines() if path.exists() else []:
        rec = json.loads(line)
        s = by_tag.setdefault(rec["tag"] or "-", {"calls": 0, "cache_hits": 0, "input": 0,
                                                  "output": 0, "cost_usd": 0.0})
        s["calls"] += 1
        s["cache_hits"] += rec["cached"]
        if "tokens" in rec:
            s["input"] += rec["tokens"]["input"] + rec["tokens"]["cache_read"] + rec["tokens"]["cache_write"]
            s["output"] += rec["tokens"]["output"]
            s["cost_usd"] += rec["cost_usd"]
    return by_tag


llm = LLM()
