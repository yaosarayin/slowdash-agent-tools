# llm.py — vision extractor backed by the headless `claude` CLI.
#
# We hand a batch of webcam frames plus a natural-language prompt to a
# `claude -p` subprocess.  Claude reads each image file via its Read tool
# and returns a single-line JSON object whose keys are channel names and
# values are numbers (or null).  Authentication is delegated to whatever
# the user's Claude Code install has — typically a Claude.ai subscription
# via OAuth — so this module no longer needs an Anthropic API key.

import os
import re
import json
import shutil
import asyncio
import logging
import tempfile
from dataclasses import dataclass, field
from typing import Iterable


DEFAULT_MODEL = 'claude-opus-4-7'
DEFAULT_MAX_TOKENS = 1024
DEFAULT_TIMEOUT = 180.0


SYSTEM_PROMPT = (
    "You are an industrial-instrument display reader.  Look at the attached "
    "webcam frames of a screen-only laboratory instrument and extract the "
    "numeric readings displayed on it.\n"
    "\n"
    "Respond with ONLY a JSON object on a single line, no prose, no markdown. "
    "Keys must match the channel names the user specifies in their prompt. "
    "Values must be JSON numbers, not strings.  Use null (literal JSON `null`, "
    "not the string 'null') for any channel whose value you cannot read with "
    "complete confidence.  Do not invent channels the user did not ask for.\n"
    "\n"
    "CRITICAL — UNCERTAINTY RULE:\n"
    "Returning null is ALWAYS preferred over returning a wrong number.  Do "
    "NOT report a value unless you are COMPLETELY SURE of BOTH (a) every "
    "digit of the temperature, AND (b) which channel that temperature "
    "belongs to.  If even one digit is partially obscured, blurry, or "
    "ambiguous, return null.  If the channel-tag (the per-channel suffix "
    "or label that disambiguates the value) is not visible IN THE SAME "
    "FRAME as the digits, return null — do not infer the channel from the "
    "previous or next frame, do not assume the display is still on the "
    "same channel.  Do not guess, do not approximate, do not interpolate, "
    "do not extrapolate, do not invent values for channels that are not "
    "currently shown.  When in doubt, null.\n"
    "\n"
    "If the user's prompt lists which channels are CONNECTED, only ever "
    "report values for those channels — return null (or omit) for any "
    "channel marked as not connected, even if you think you can read its "
    "value.\n"
    "\n"
    "If the instrument cycles through several channels on a single display, "
    "use the indicator lights / labels in each frame to figure out which "
    "channel each reading belongs to — and only report a value when the "
    "channel-tag and the digits are unambiguous in the same frame."
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
    """Extracts numeric values from webcam frames by spawning `claude -p`."""

    def __init__(self, *,
                 model: str = DEFAULT_MODEL,
                 max_tokens: int = DEFAULT_MAX_TOKENS,
                 timeout: float = DEFAULT_TIMEOUT):
        claude_bin = shutil.which('claude')
        if claude_bin is None:
            raise LLMError(
                "claude CLI not found on PATH — install Claude Code "
                "(https://claude.com/claude-code) and run `claude auth login`"
            )
        self._claude = claude_bin
        self._model = model
        self._max_tokens = max_tokens
        self._timeout = float(timeout)

    async def extract(self, frames: Iterable[bytes], prompt: str,
                      *, mime_type: str = 'image/jpeg') -> ExtractionResult:
        """Send `frames` and `prompt` to claude, parse the JSON response.

        - frames: iterable of raw image bytes (JPEG or PNG)
        - prompt: the user-supplied extraction instructions
        - mime_type: ignored; kept for API compatibility (claude reads files
          and detects format itself).
        """
        frames = list(frames)
        if not frames:
            raise LLMError("no frames to extract from")

        with tempfile.TemporaryDirectory(prefix='slowagent-') as tmpdir:
            # macOS /var → /private/var: realpath both --add-dir and the
            # paths-in-prompt so claude's containment check matches.
            real_tmpdir = os.path.realpath(tmpdir)

            paths = []
            for i, blob in enumerate(frames, start=1):
                ext = _image_ext(blob) or '.jpg'
                path = os.path.join(real_tmpdir, f"frame-{i:03d}{ext}")
                with open(path, 'wb') as f:
                    f.write(blob)
                paths.append(path)

            paths_block = '\n'.join(f"- {p}" for p in paths)
            full_prompt = (
                f"Read each of the following image files using the Read tool, "
                f"in order, then extract the requested values.\n\n"
                f"Files:\n{paths_block}\n\n"
                f"{prompt}"
            )

            argv = [
                self._claude, '-p',
                '--model', self._model,
                '--output-format', 'json',
                '--tools', 'Read',
                '--permission-mode', 'bypassPermissions',
                '--no-session-persistence',
                '--setting-sources', '',
                '--exclude-dynamic-system-prompt-sections',
                '--add-dir', real_tmpdir,
                '--append-system-prompt', SYSTEM_PROMPT,
                full_prompt,
            ]

            # Force OAuth/keychain auth: a stale ANTHROPIC_API_KEY in env
            # would otherwise win over the user's Claude Code subscription.
            env = os.environ.copy()
            env.pop('ANTHROPIC_API_KEY', None)

            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=real_tmpdir,
                    env=env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as e:
                raise LLMError(f"failed to spawn claude CLI: {e}") from e

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self._timeout
                )
            except asyncio.TimeoutError:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                raise LLMError(
                    f"claude CLI timed out after {self._timeout:.0f}s"
                )

            if proc.returncode != 0:
                err = stderr.decode('utf-8', errors='replace').strip()
                raise LLMError(
                    f"claude CLI exit {proc.returncode}: {err[:500] or '(no stderr)'}"
                )

            try:
                envelope = json.loads(stdout)
            except json.JSONDecodeError as e:
                preview = stdout[:300].decode('utf-8', errors='replace')
                raise LLMError(
                    f"claude CLI returned non-JSON output: {e} — got {preview!r}"
                ) from e

            if envelope.get('is_error'):
                msg = envelope.get('result') or envelope.get('error') or str(envelope)
                raise LLMError(f"claude CLI reported error: {msg}")

            text = envelope.get('result', '') or ''
            values = _parse_json_response(text)

            return ExtractionResult(
                values=values,
                raw_response=text,
                model=envelope.get('model') or self._model,
                usage={
                    'input_tokens':  envelope.get('usage', {}).get('input_tokens', 0),
                    'output_tokens': envelope.get('usage', {}).get('output_tokens', 0),
                },
            )


# ── Helpers ────────────────────────────────────────────────────────────── #

def _image_ext(blob: bytes):
    """Return '.jpg' / '.png' if `blob` looks like a real JPEG or PNG, else
    None.  Used so the temp-file extension matches the actual format."""
    if not blob:
        return None
    if blob.startswith(b'\xff\xd8\xff'):       # JPEG SOI
        return '.jpg'
    if blob.startswith(b'\x89PNG\r\n\x1a\n'):
        return '.png'
    return None


def _parse_json_response(text: str) -> dict:
    """Pull a JSON object out of Claude's reply.  Tolerates the model
    occasionally wrapping its answer in a fenced code block or adding a
    leading/trailing sentence."""
    text = text.strip()

    fence = re.match(r'^```(?:json)?\s*(.*?)\s*```$', text, flags=re.DOTALL)
    if fence:
        text = fence.group(1).strip()

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
