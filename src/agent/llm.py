"""
The single seam between this codebase and the Anthropic API.

Everything that talks to a model goes through here: the agent loop, the
reranker, the validator, ingest enrichment, vision OCR, and the eval judge.
Keeping it in one place buys uniform usage accounting (which the eval reports
as tokens and dollars per question), one retry policy, and one place to set
adaptive thinking, effort and cache breakpoints.

We use the raw SDK rather than a framework wrapper because those settings —
`thinking`, `output_config.effort`, per-block `cache_control`, `strict` tool
schemas, the Batches API — are exactly what a passthrough layer tends to lose.
"""

from __future__ import annotations

import base64
import logging
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

import anthropic
import pydantic
from pydantic import BaseModel

from .. import config

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_CLIENT: anthropic.Anthropic | None = None

# Adaptive thinking and `output_config.effort` are rejected with a 400 on
# older models — Haiku 4.5 answers "adaptive thinking is not supported on this
# model" and "This model does not support the effort parameter." Sending them
# blindly turns a cheap auxiliary model into a silent no-op, so capability is
# checked rather than assumed.
_MODERN_CONTROLS = (
    "claude-opus-5",
    "claude-opus-4-6",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-fable-5",
    "claude-mythos-5",
)


def supports_modern_controls(model: str) -> bool:
    return model.startswith(_MODERN_CONTROLS)


def client() -> anthropic.Anthropic:
    """Lazily construct the shared client so importing this module is cheap."""
    global _CLIENT
    if _CLIENT is None:
        # 180s is comfortably above a slow Opus turn at high effort, and
        # low enough that a hung connection costs three minutes rather
        # than ten. The SDK retries timeouts, so a stall self-heals.
        _CLIENT = anthropic.Anthropic(max_retries=3, timeout=180.0)
    return _CLIENT


@dataclass
class Usage:
    """Token and cost accounting, accumulated across a run."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    calls: int = 0
    latency_ms: float = 0.0
    by_model: dict[str, int] = field(default_factory=dict)

    def add(self, model: str, usage: Any, latency_ms: float) -> None:
        self.calls += 1
        self.latency_ms += latency_ms
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0
        self.by_model[model] = self.by_model.get(model, 0) + 1

    def merge(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.calls += other.calls
        self.latency_ms += other.latency_ms
        for model, count in other.by_model.items():
            self.by_model[model] = self.by_model.get(model, 0) + count

    @property
    def cost_usd(self) -> float:
        """Approximate spend. Priced against the dominant model in the run."""
        model = (
            max(self.by_model, key=lambda m: self.by_model[m]) if self.by_model else config.MODEL
        )
        rates = config.PRICING.get(model, config.PRICING[config.MODEL])
        per_million = 1_000_000
        return (
            self.input_tokens * rates["input"]
            + self.cache_read_tokens * rates["input"] * config.CACHE_READ_MULTIPLIER
            + self.cache_write_tokens * rates["input"] * config.CACHE_WRITE_MULTIPLIER
            + self.output_tokens * rates["output"]
        ) / per_million

    @property
    def cache_hit_rate(self) -> float:
        """Share of prompt tokens served from cache. Zero across repeated runs
        means something volatile crept into the prefix."""
        total = self.input_tokens + self.cache_read_tokens + self.cache_write_tokens
        return (self.cache_read_tokens / total) if total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cache_hit_rate": round(self.cache_hit_rate, 4),
            "latency_ms": round(self.latency_ms, 1),
            "cost_usd": round(self.cost_usd, 6),
            "by_model": self.by_model,
        }


def cached(text: str) -> dict[str, Any]:
    """A system block with a cache breakpoint on it.

    Used for the stable prefix (instructions + case brief + timeline
    skeleton), which is ~10k tokens and is re-sent on every turn of a
    conversation. Volatile content must come after this block.
    """
    return {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}


class LLM:
    """Thin, explicit wrapper. One instance per run so usage is scoped."""

    def __init__(self, usage: Usage | None = None) -> None:
        self.usage = usage or Usage()

    # -- core -----------------------------------------------------------

    def message(
        self,
        *,
        system: str | list[dict[str, Any]] | None = None,
        messages: list[dict[str, Any]],
        model: str | None = None,
        effort: str = "high",
        max_tokens: int = 8000,
        tools: Sequence[dict[str, Any]] | None = None,
        thinking: bool = True,
    ) -> Any:
        """One turn. Returns the raw SDK Message so callers can inspect blocks."""
        model = model or config.MODEL
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system is not None:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = list(tools)
        if supports_modern_controls(model):
            kwargs["output_config"] = {"effort": effort}
            if thinking:
                # Adaptive: the model decides depth, and interleaves thinking
                # between tool calls. `budget_tokens` is rejected on Opus 5.
                kwargs["thinking"] = {"type": "adaptive"}

        started = time.perf_counter()
        response = client().messages.create(**kwargs)
        self.usage.add(model, response.usage, (time.perf_counter() - started) * 1000)
        return response

    def text(self, **kwargs: Any) -> str:
        response = self.message(**kwargs)
        return "".join(b.text for b in response.content if b.type == "text").strip()

    def structured(
        self,
        schema: type[T],
        *,
        system: str | list[dict[str, Any]] | None = None,
        messages: list[dict[str, Any]],
        model: str | None = None,
        effort: str = "low",
        max_tokens: int = 4000,
    ) -> T | None:
        """A response validated against a Pydantic model.

        Returns None rather than raising when the model declines or the
        response cannot be parsed — every caller has a sane fallback, and one
        malformed judgement should not abort a run.
        """
        model = model or config.MODEL
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
            "output_format": schema,
        }
        if supports_modern_controls(model):
            kwargs["output_config"] = {"effort": effort}
        if system is not None:
            kwargs["system"] = system

        started = time.perf_counter()
        try:
            response = client().messages.parse(**kwargs)
        except (
            anthropic.APIStatusError,
            anthropic.APIConnectionError,
            pydantic.ValidationError,
            ValueError,
        ) as exc:
            # A refusal, a transport failure, or output truncated mid-JSON.
            # Every caller has a fallback, and one bad structured response
            # must not abort a whole eval run or agent turn.
            logger.warning("structured(%s) failed: %s", schema.__name__, exc)
            return None
        self.usage.add(model, response.usage, (time.perf_counter() - started) * 1000)
        parsed = getattr(response, "parsed_output", None)
        return parsed if isinstance(parsed, schema) else None

    def stream_text(
        self,
        *,
        system: str | list[dict[str, Any]] | None = None,
        messages: list[dict[str, Any]],
        model: str | None = None,
        effort: str = "high",
        max_tokens: int = 8000,
    ) -> Iterator[str]:
        """Stream text deltas. Used by the chat UI so answers appear as they
        are written rather than after a 30-second pause."""
        model = model or config.MODEL
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if supports_modern_controls(model):
            kwargs["output_config"] = {"effort": effort}
            kwargs["thinking"] = {"type": "adaptive"}
        if system is not None:
            kwargs["system"] = system

        started = time.perf_counter()
        with client().messages.stream(**kwargs) as stream:
            yield from stream.text_stream
            final = stream.get_final_message()
        self.usage.add(model, final.usage, (time.perf_counter() - started) * 1000)

    # -- vision ---------------------------------------------------------

    def read_images(
        self,
        images: Sequence[bytes],
        prompt: str,
        *,
        model: str | None = None,
        media_type: str = "image/png",
        max_tokens: int = 8000,
    ) -> str:
        """Read text off rendered pages.

        Used at ingest for the six image-only PDFs in case_001, which have no
        text layer at all and would otherwise be invisible to retrieval.
        """
        content: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.standard_b64encode(image).decode(),
                },
            }
            for image in images
        ]
        content.append({"type": "text", "text": prompt})
        return self.text(
            messages=[{"role": "user", "content": content}],
            model=model,
            effort="low",
            max_tokens=max_tokens,
        )


__all__ = ["LLM", "Usage", "cached", "client", "supports_modern_controls"]
