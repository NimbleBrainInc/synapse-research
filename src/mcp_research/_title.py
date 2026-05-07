"""Title generation for research runs.

Separates the short, human-meaningful label (`title`) from the full
research brief (`query`). The brief is what gets handed to GPT-Researcher;
the title is what goes in list rows and detail headings.

This module is deliberately small and self-contained:
  * one async public entry point — ``generate_title(query)``
  * a single fast Anthropic call against ``FAST_LLM`` (Haiku in production)
  * a hard 5-second wall-clock cap so a slow provider can never delay the
    research run itself — the worker fires this in the background, and a
    title that never arrives just leaves the field null (the UI falls back
    to a truncated query)

The model is read from the ``FAST_LLM`` env var (set in manifest.json), with
the ``anthropic:`` prefix stripped. The Anthropic SDK is the only client we
talk to here — no langchain wrapper, no provider abstraction. The bundle
already pins the SDK so cost is one import.
"""

from __future__ import annotations

import asyncio
import os
import sys

# 5 seconds is generous for a 50-token completion against Haiku and well
# under the worker's per-phase liveness budget. If the provider ever slows
# this much the run still proceeds with a null title — preferable to
# blocking research on a label.
_TITLE_TIMEOUT_S = 5.0

# Cap is intentionally tight: titles are scannable labels, not summaries.
# Schema enforces 80 chars total; this caps the model's output budget so a
# rambling completion never bloats up to the schema limit.
_MAX_OUTPUT_TOKENS = 60

_PROMPT = (
    "Summarize this research request as a concise 3-8 word title. "
    "Use title case. No quotes, no trailing punctuation, no leading "
    "phrases like 'Title:'. Output only the title.\n\n"
    "Request: {query}"
)


def _resolve_model() -> str:
    """Read ``FAST_LLM`` from env and strip the ``anthropic:`` provider prefix.

    The manifest sets ``FAST_LLM=anthropic:claude-haiku-4-5`` to match
    GPT-Researcher's provider:model convention. The Anthropic SDK takes
    just the model id, so we slice off the prefix here. Falls back to a
    sensible Haiku default if the var is missing — keeps unit tests
    workable without forcing them to set env.
    """
    raw = os.environ.get("FAST_LLM", "claude-haiku-4-5")
    if ":" in raw:
        _, _, model = raw.partition(":")
        return model or "claude-haiku-4-5"
    return raw


async def generate_title(query: str) -> str | None:
    """Generate a short title for a research query.

    Returns the title on success, or ``None`` on any failure (timeout,
    missing API key, provider error, empty completion). Never raises —
    the caller fires this in the background and a missing title is a
    benign no-op.
    """
    if not query or not query.strip():
        return None

    try:
        # Lazy import so the bundle still starts (and tests still run)
        # when the SDK isn't installed in the test environment.
        from anthropic import AsyncAnthropic
    except ImportError:
        print(
            "[synapse-research] title: anthropic SDK not available; skipping",
            file=sys.stderr,
        )
        return None

    if not os.environ.get("ANTHROPIC_API_KEY"):
        # No key → no title. Don't log loudly; this is a normal state
        # during local dev runs without secrets configured.
        return None

    model = _resolve_model()
    client = AsyncAnthropic()

    async def _call() -> str | None:
        message = await client.messages.create(
            model=model,
            max_tokens=_MAX_OUTPUT_TOKENS,
            messages=[{"role": "user", "content": _PROMPT.format(query=query)}],
        )
        # The SDK returns a list of content blocks; the first text block
        # is what we want. Defensive against odd shapes — return None
        # rather than crash.
        for block in message.content:
            text = getattr(block, "text", None)
            if text:
                return _clean(text)
        return None

    try:
        return await asyncio.wait_for(_call(), timeout=_TITLE_TIMEOUT_S)
    except asyncio.CancelledError:
        # Caller cancelled (e.g., run cancelled). Propagate so the task
        # exits cleanly; the worker swallows it at the gather site.
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"[synapse-research] title generation failed: {exc}", file=sys.stderr)
        return None


def _clean(raw: str) -> str | None:
    """Strip whitespace and quote artefacts from the model's output.

    Even with a tight prompt, Haiku occasionally wraps the title in
    quotes or appends a period. Snip those off so the entity stores a
    clean label. Returns None if the cleaned string is empty.
    """
    text = raw.strip()
    # Drop matched leading/trailing quote pairs — both single and double.
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'", "“", "”"}:
        text = text[1:-1].strip()
    # Drop a single trailing period; leave question marks / exclamations
    # alone since those can be intentional in a title.
    if text.endswith("."):
        text = text[:-1].rstrip()
    if not text:
        return None
    # Schema cap is 80 chars. If the model overran, hard-truncate at a
    # word boundary so we never store something the entity store would
    # reject.
    if len(text) > 80:
        text = text[:80].rsplit(" ", 1)[0]
    return text or None
