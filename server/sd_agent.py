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
import glob
import json
import logging
import urllib.parse

import slowlette


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
    """Patch only the `llm.prompt` field of a layout file.  Leaves every
    other setting (channels, capture rate, …) untouched.  The slowtask polls
    the file mtime and reloads on change, so updates take effect within a
    couple of seconds without restarting the task."""
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

    new_prompt = body.get('prompt') if body is not None else None
    if not isinstance(new_prompt, str):
        return slowlette.Response(status_code=400,
                                  content=b'expected JSON {"prompt": "..."}')

    doc.setdefault('llm', {})['prompt'] = new_prompt
    try:
        with open(path, 'w') as f:
            json.dump(doc, f, indent=2)
    except Exception as e:
        return slowlette.Response(status_code=500,
                                  content=f'cannot write layout: {e}'.encode())

    return {'ok': True, 'file': layout_file}


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

      - if `capture.rolling_dir` is set, that directory is the display dir
        (HTTP webcams write each capture there);
      - otherwise, if `capture.source` is a file:// URL or a relative path,
        treat it as a directory of frames;
      - otherwise (an http(s):// camera with no rolling_dir), return None.
    """
    path = os.path.join(PROJECT_DIR, 'config', layout_file)
    try:
        with open(path) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None

    cap = doc.get('capture') or {}

    rolling = cap.get('rolling_dir')
    if rolling:
        if not os.path.isabs(rolling):
            rolling = os.path.normpath(os.path.join(PROJECT_DIR, rolling))
        return rolling

    src = cap.get('source') or ''
    if src.startswith('http://') or src.startswith('https://'):
        return None      # no on-disk view for HTTP without rolling_dir

    # Strip file: scheme prefixes if present.
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
