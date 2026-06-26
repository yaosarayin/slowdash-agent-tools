# sd_agent.py — slowdash user module for the LLM webcam-OCR agent.
#
# Load this from SlowdashProject.yaml's `module:` block to:
#   1. add slowagent-NAME.json files to the home-page catalog,
#   2. expose /api/agent/* helpers used by site/slowagent/slowagent-app.mjs.
#
# Example:
#   slowdash_project:
#     module:
#       file: ../../slowdash-agent-tools/server/sd_agent.py
#
# All routes are read-only or write-prompt-text-only.  The slowtask is the
# only thing that actually talks to the LLM or writes to the datastore.

import os
import sys
import glob
import json
import logging
import urllib.parse

import slowlette

# slowagent ships in the submodule's `lib/`; if slowdash is running in a
# venv that doesn't expose lib/slowpy on sys.path, fall back to importing
# from the submodule directly via this file's realpath.  Same dance as
# slowtask-webcam_ocr.py.
_AGENT_LIB = os.path.normpath(os.path.join(
    os.path.dirname(os.path.realpath(__file__)), '..', 'lib'
))
if os.path.isdir(os.path.join(_AGENT_LIB, 'slowagent')) and _AGENT_LIB not in sys.path:
    sys.path.insert(0, _AGENT_LIB)

import slowagent


# Slowdash chdirs to the project directory before loading user modules
# (app/server/slowdash.py: `os.chdir(self.project.project_dir)`), so we can
# use cwd as the project root throughout this file.  The slowlette Request
# object doesn't carry an app reference, so this is the cleanest path.
PROJECT_DIR = os.getcwd()


app = slowlette.Slowlette()


# ── Catalog integration ─────────────────────────────────────────────────── #

@app.get('/api/agent/layouts')
async def list_agent_layouts():
    """Return the names of every slowagent-NAME.json layout in the project."""
    config_dir = os.path.join(PROJECT_DIR, 'config')
    layouts = []
    for path in sorted(glob.glob(os.path.join(config_dir, 'slowagent-*.json'))):
        rootname = os.path.splitext(os.path.basename(path))[0]
        try:
            _, label = rootname.split('-', 1)
        except ValueError:
            label = rootname
        layouts.append({
            'name':  label,
            'file':  os.path.basename(path),
            'mtime': int(os.path.getmtime(path)),
        })
    return layouts


# Note: we do NOT register a `/api/config/contentlist` handler.  Slowdash's
# built-in scanner in sd_config.py already enumerates `<kind>-<name>.<ext>`
# files in the project's config dir and emits an entry with `type=<kind>`,
# so `slowagent-Omega.json` already shows up as a `slowagent` catalog entry.
# Adding our own handler caused a duplicate row.
#
# The home-page catalog only shows `kind`s listed in `slowhome.html`'s
# `catalog_type` config (default: `slowdash,slowplot,slowcruise,userhtml`).
# To see slowagent layouts on the home page, override the catalog by saving
# a `slowdash-Home.json` in the project's config/ with the type added.


# ── Prompt update helper used by the front-end ──────────────────────────── #

@app.post('/api/agent/prompt/{layout_file}')
async def update_prompt(layout_file: str, body: slowlette.JSON):
    """Patch only the `llm.prompt` field of a layout file.  Kept as a
    distinct endpoint for backward compat with older front-ends; new code
    should POST to /api/agent/settings/{layout_file}.  Body: {"prompt": "..."}."""
    data = _unwrap_json(body)
    if not isinstance(data, dict) or 'prompt' not in data:
        return slowlette.Response(status_code=400,
                                  content=b'expected JSON {"prompt": "..."}')
    return await _patch_layout(layout_file, {'prompt': data['prompt']})


@app.post('/api/agent/settings/{layout_file}')
async def update_settings(layout_file: str, body: slowlette.JSON):
    """Patch a curated subset of fields in a layout file.  Recognised keys:

      - prompt            (str) -> `llm.prompt`
      - cycle_seconds     (number ≥ frame_interval×frames_per_cycle, ≤86400) -> `capture.cycle_seconds`
      - frames_per_cycle  (int 1..60)       -> `capture.frames_per_cycle`
      - frame_interval    (number 0.1..60)  -> `capture.frame_interval`
      - connected         ({channel_name: bool}) -> sets `connected` on each
                           matching channel object in `channels[]`

    Unknown keys are ignored.  All-or-nothing write — the file is rewritten
    only if the patch is well-formed.  The slowtask polls the file mtime and
    reloads on change, so updates take effect within a couple of seconds.
    """
    data = _unwrap_json(body)
    if not isinstance(data, dict):
        return slowlette.Response(status_code=400, content=b'expected JSON object')
    return await _patch_layout(layout_file, data)


def _unwrap_json(body):
    """slowlette.JSON is a wrapper class that proxies dict/list operations
    via __contains__/__getitem__/get(), but it is NOT itself a `dict` —
    so `isinstance(body, dict)` is always False even for a perfectly good
    JSON object.  Reach through to the underlying parsed structure."""
    if body is None:
        return None
    if hasattr(body, 'value'):
        return body.value()
    return body


async def _patch_layout(layout_file: str, patch: dict):
    if not _safe_layout_filename(layout_file):
        return slowlette.Response(status_code=400, content=b'bad layout file name')

    path = os.path.join(PROJECT_DIR, 'config', layout_file)
    if not os.path.isfile(path):
        return slowlette.Response(status_code=404, content=b'layout not found')

    try:
        with open(path) as f:
            doc = json.load(f)
    except Exception as e:
        return slowlette.Response(status_code=500,
                                  content=f'cannot read layout: {e}'.encode())

    applied = []

    if 'prompt' in patch:
        if not isinstance(patch['prompt'], str):
            return slowlette.Response(status_code=400, content=b'prompt must be a string')
        doc.setdefault('llm', {})['prompt'] = patch['prompt']
        applied.append('prompt')

    if 'cycle_seconds' in patch:
        try:
            cs = float(patch['cycle_seconds'])
        except (TypeError, ValueError):
            return slowlette.Response(status_code=400, content=b'cycle_seconds must be a number')
        cap = doc.get('capture', {})
        fi      = float(cap.get('frame_interval',  1.5))
        fpc     = int(cap.get('frames_per_cycle', 5))
        min_cs  = round(fi * fpc, 6)
        if not (min_cs <= cs <= 86400.0):
            return slowlette.Response(
                status_code=400,
                content=f'cycle_seconds must be between {min_cs} (frame_interval × frames_per_cycle) and 86400'.encode()
            )
        doc.setdefault('capture', {})['cycle_seconds'] = cs
        applied.append('cycle_seconds')

    if 'frames_per_cycle' in patch:
        try:
            fpc = int(patch['frames_per_cycle'])
        except (TypeError, ValueError):
            return slowlette.Response(status_code=400,
                                      content=b'frames_per_cycle must be an integer')
        if not (1 <= fpc <= 60):
            return slowlette.Response(status_code=400,
                                      content=b'frames_per_cycle must be between 1 and 60')
        doc.setdefault('capture', {})['frames_per_cycle'] = fpc
        applied.append('frames_per_cycle')

    if 'frame_interval' in patch:
        try:
            fi = float(patch['frame_interval'])
        except (TypeError, ValueError):
            return slowlette.Response(status_code=400,
                                      content=b'frame_interval must be a number')
        if not (0.1 <= fi <= 60.0):
            return slowlette.Response(status_code=400,
                                      content=b'frame_interval must be between 0.1 and 60 seconds')
        doc.setdefault('capture', {})['frame_interval'] = fi
        applied.append('frame_interval')

    if 'connected' in patch:
        flags = patch['connected']
        if not isinstance(flags, dict):
            return slowlette.Response(status_code=400, content=b'connected must be an object')
        for ch in doc.get('channels', []):
            name = ch.get('name')
            if name in flags:
                ch['connected'] = bool(flags[name])
        applied.append('connected')

    if not applied:
        return slowlette.Response(
            status_code=400,
            content=b'no recognised fields in patch (expected prompt, cycle_seconds, frames_per_cycle, frame_interval, or connected)'
        )

    try:
        with open(path, 'w') as f:
            json.dump(doc, f, indent=2)
    except Exception as e:
        return slowlette.Response(status_code=500,
                                  content=f'cannot write layout: {e}'.encode())

    # If the connected list changed, rewrite slowplot-NAME.json right now
    # so the UI sees the new channel set on the very next iframe load —
    # without waiting for the slowtask's next config-poll cycle (which can
    # be blocked for 20+ seconds by an in-flight LLM call).
    slowplot_changed = False
    if 'connected' in applied:
        try:
            slowplot_changed = slowagent.regenerate_slowplot(path)
        except Exception as e:
            logging.warning("sd_agent: slowplot regen failed: %s", e)

    return {
        'ok':                True,
        'file':              layout_file,
        'applied':           applied,
        'slowplot_changed':  slowplot_changed,
    }


# ── Cycling-frames display ──────────────────────────────────────────────── #
#
# The dashboard left-hand panel cycles through every image currently
# available in the webcam's display directory.  These endpoints:
#   - list the available files (for the cycler to know what to fetch)
#   - serve each file by name (with the same path-safety rules
#     BlobStorage_File.get_blob() uses).

_IMAGE_EXTS = ('.jpg', '.jpeg', '.png')
_MIME = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png'}


@app.get('/api/agent/frames/{layout_file}')
async def list_source_frames(layout_file: str):
    """List image files available in the display dir for `layout_file`.
    Returns [{name, mtime, size}, ...]  newest-first."""
    if not _safe_layout_filename(layout_file):
        return slowlette.Response(status_code=400, content=b'bad layout file')

    display_dir = _resolve_display_dir(layout_file)
    if display_dir is None or not os.path.isdir(display_dir):
        return []

    entries = []
    for name in os.listdir(display_dir):
        if not name.lower().endswith(_IMAGE_EXTS):
            continue
        if not _safe_image_name(name):
            continue
        path = os.path.join(display_dir, name)
        try:
            entries.append({
                'name':  name,
                'mtime': int(os.path.getmtime(path)),
                'size':  os.path.getsize(path),
            })
        except OSError:
            continue
    entries.sort(key=lambda e: e['mtime'], reverse=True)
    return entries


@app.get('/api/agent/frame/{layout_file}/{filename}')
async def get_source_frame(layout_file: str, filename: str):
    """Serve a single image file from the display dir for `layout_file`.
    Strict path safety — rejects anything that isn't a plain image filename
    in the resolved display dir."""
    if not _safe_layout_filename(layout_file):
        return slowlette.Response(status_code=400, content=b'bad layout file')
    if not _safe_image_name(filename):
        return slowlette.Response(status_code=400, content=b'bad image name')

    display_dir = _resolve_display_dir(layout_file)
    if display_dir is None:
        return slowlette.Response(status_code=404, content=b'no display dir')

    path = os.path.join(display_dir, filename)
    real_path = os.path.realpath(path)
    real_dir  = os.path.realpath(display_dir)
    if not real_path.startswith(real_dir + os.sep) and real_path != real_dir:
        return slowlette.Response(status_code=403, content=b'path traversal')
    if not os.path.isfile(real_path):
        return slowlette.Response(status_code=404, content=b'not found')

    try:
        with open(real_path, 'rb') as f:
            content = f.read()
    except OSError:
        return slowlette.Response(status_code=500, content=b'read error')

    ext = os.path.splitext(filename)[1].lower()
    return slowlette.Response(content_type=_MIME.get(ext, 'application/octet-stream'),
                              content=content)


def _resolve_display_dir(layout_file: str):
    """Read the layout JSON and return the absolute path of the directory
    that holds the cycling-display images.  Mirrors the slowtask's logic:

      - HTTP source: the slowtask writes captured frames into
        `capture.batch_dir` (default `last_images`); that's the display dir.
      - file:// or path source: the source IS the directory.

    `capture.rolling_dir` is accepted as a backward-compat alias for
    `batch_dir`.  Returns an absolute path, or None if no on-disk view
    exists.
    """
    path = os.path.join(PROJECT_DIR, 'config', layout_file)
    try:
        with open(path) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None

    cap = doc.get('capture') or {}
    src = cap.get('source') or ''

    if src.startswith('http://') or src.startswith('https://'):
        # HTTP source uses batch_dir (or rolling_dir) for the on-disk view.
        d = cap.get('batch_dir') or cap.get('rolling_dir') or 'last_images'
        if not os.path.isabs(d):
            d = os.path.normpath(os.path.join(PROJECT_DIR, d))
        return d

    # file:// or relative-path source: the source IS the display dir.
    if src.startswith('file:///'):
        return src[len('file://'):]
    if src.startswith('file://'):
        rel = src[len('file://'):]
    elif src.startswith('file:'):
        rel = src[len('file:'):]
    else:
        rel = src

    if not rel:
        return None
    if os.path.isabs(rel):
        return rel
    return os.path.normpath(os.path.join(PROJECT_DIR, rel))


# ── Helpers ─────────────────────────────────────────────────────────────── #

def _safe_layout_filename(name: str) -> bool:
    """Reject anything that isn't a plain `slowagent-XXX.json` basename."""
    if not name or '/' in name or '\\' in name or name.startswith('.'):
        return False
    if not name.startswith('slowagent-') or not name.endswith('.json'):
        return False
    return True


def _safe_image_name(name: str) -> bool:
    """Reject path-separator and traversal tricks; allow normal filename
    characters (including spaces — real-world `Screenshot 2026-04-30…png`
    files exist).  The realpath check inside get_source_frame is the
    authoritative containment guarantee; this is a quick first filter."""
    if not name or '/' in name or '\\' in name:
        return False
    if name.startswith('.') or '..' in name:
        return False
    return name.lower().endswith(_IMAGE_EXTS)
