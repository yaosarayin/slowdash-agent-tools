# llm.py — Anthropic Claude vision client for slowagent.
#
# A thin wrapper around the official `anthropic` SDK.  The slowtask hands us a
# list of recently-captured frames plus a natural-language prompt; we send all
# of them to Claude's vision-capable model and parse the structured JSON
# response back into a dict of {channel_name: numeric_value}.
#
# We use an AsyncAnthropic client so the slowtask event loop is never blocked
# while waiting on the API.
#
# Why JSON mode and not free-form text?  A multi-modal LLM is reliable at
# parsing displays but unreliable at unconstrained float formatting.  By
# forcing the response to be a JSON object whose keys are channel names and
# whose values are numbers, we get a clean failure (parse error) instead of
# subtly wrong values.

import os
import re
import json
import base64
import logging
from dataclasses import dataclass, field
from typing import Iterable

from .secrets import get_secret, SecretError


DEFAULT_MODEL = 'claude-opus-4-7'
DEFAULT_MAX_TOKENS = 1024


# System prompt sent on every request.  It tells Claude:
#   1. exactly what shape to return,
#   2. that "I can't read this" should be a real signal, not a hallucinated number,
#   3. that we want raw numbers, not strings or formatted values.
SYSTEM_PROMPT = (
    "You are an industrial-instrument display reader.  Look at the attached "
    "webcam frames of a screen-only laboratory instrument and extract the "
    "numeric readings displayed on it.\n"
    "\n"
    "Respond with ONLY a JSON object on a single line, no prose, no markdown. "
    "Keys must match the channel names the user specifies in their prompt. "
    "Values must be JSON numbers, not strings.  Use null (literal JSON `null`, "
    "not the string 'null') for any channel whose value you cannot read in any "
    "of the frames.  Do not invent channels the user did not ask for.\n"
    "\n"
    "If the instrument cycles through several channels on a single display, "
    "use the indicator lights / labels in each frame to figure out which "
    "channel each reading belongs to."
)


class LLMError(Exception):
    pass


@dataclass
class ExtractionResult:
    """Output of a single extraction call."""
    values: dict          # {channel_name: float | None}
    raw_response: str     # the exact text Claude returned, useful for logs
    model: str
    usage: dict = field(default_factory=dict)


class ClaudeVisionExtractor:
    """Extracts numeric values from webcam frames using Claude vision."""

    def __init__(self, *,
                 model: str = DEFAULT_MODEL,
                 max_tokens: int = DEFAULT_MAX_TOKENS,
                 api_key: str = None):
        try:
            import anthropic
        except ImportError:
            raise LLMError("the `anthropic` package is required: pip install anthropic")
        self._anthropic = anthropic

        self._api_key = api_key or get_secret('anthropic_api_key')
        self._model = model
        self._max_tokens = max_tokens
        self._client = anthropic.AsyncAnthropic(api_key=self._api_key)

    async def extract(self, frames: Iterable[bytes], prompt: str,
                      *, mime_type: str = 'image/jpeg') -> ExtractionResult:
        """Send `frames` and `prompt` to Claude, parse the JSON response.

        - frames: iterable of raw image bytes (JPEG or PNG)
        - prompt: the user-supplied extraction instructions
        - mime_type: image MIME for all frames
        """
        frames = list(frames)
        if not frames:
            raise LLMError("no frames to extract from")

        content = []
        for blob in frames:
            content.append({
                "type": "image",
                "source": {
                    "type":       "base64",
                    "media_type": _detect_mime(blob, mime_type),
                    "data":       base64.b64encode(blob).decode('ascii'),
                },
            })
        content.append({"type": "text", "text": prompt})

        try:
            resp = await self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content}],
            )
        except self._anthropic.APIError as e:
            raise LLMError(f"Anthropic API error: {e}") from e

        # The response is a list of content blocks; we only expect text.
        text = ''.join(b.text for b in resp.content if getattr(b, 'type', None) == 'text')
        values = _parse_json_response(text)

        return ExtractionResult(
            values=values,
            raw_response=text,
            model=resp.model,
            usage={
                'input_tokens':  getattr(resp.usage, 'input_tokens', 0),
                'output_tokens': getattr(resp.usage, 'output_tokens', 0),
            },
        )


# ── Helpers ────────────────────────────────────────────────────────────── #

_PNG_MAGIC = b'\x89PNG\r\n\x1a\n'
_JPG_MAGIC = b'\xff\xd8\xff'

def _detect_mime(blob: bytes, default: str) -> str:
    """Sniff the first few bytes; fall back to `default`.  Useful when the
    DirectoryWebcam returns a mix of JPEG and PNG files."""
    if blob.startswith(_PNG_MAGIC):
        return 'image/png'
    if blob.startswith(_JPG_MAGIC):
        return 'image/jpeg'
    return default


def _parse_json_response(text: str) -> dict:
    """Pull a JSON object out of Claude's reply.  Tolerates the model
    occasionally wrapping its answer in a fenced code block or adding a
    leading/trailing sentence."""
    text = text.strip()

    # Strip Markdown fencing if present.
    fence = re.match(r'^```(?:json)?\s*(.*?)\s*```$', text, flags=re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    # Find the first JSON object in the text.
    start = text.find('{')
    end   = text.rfind('}')
    if start < 0 or end < 0 or end < start:
        raise LLMError(f"no JSON object in LLM response: {text!r}")
    blob = text[start:end + 1]

    try:
        data = json.loads(blob)
    except json.JSONDecodeError as e:
        raise LLMError(f"invalid JSON in LLM response: {e} — raw {blob!r}")
    if not isinstance(data, dict):
        raise LLMError(f"LLM response is not a JSON object: {blob!r}")

    # Coerce values: leave None alone, convert numeric strings to float, drop
    # anything else with a warning.
    out = {}
    for k, v in data.items():
        if v is None:
            out[k] = None
        elif isinstance(v, (int, float)):
            out[k] = float(v)
        elif isinstance(v, str):
            try:
                out[k] = float(v)
            except ValueError:
                logging.warning("slowagent.llm: dropping non-numeric value %s=%r", k, v)
        else:
            logging.warning("slowagent.llm: dropping unsupported value %s=%r", k, v)
    return out
