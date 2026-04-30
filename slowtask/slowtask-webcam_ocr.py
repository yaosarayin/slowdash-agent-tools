# slowtask-webcam_ocr.py — webcam → Claude vision → SlowDash time-series.
#
# Drop this file into your project's `config/` directory (or symlink it from
# slowdash-agent-tools/slowtask/) and reference it as a task in
# SlowdashProject.yaml:
#
#   task:
#     - name: webcam_ocr
#       auto_load: true
#       parameters:
#         layout_file: slowagent-Omega.json
#
# The task reads its run-time settings from the slowagent layout JSON in the
# config directory, so non-developers can tweak prompts, channel lists, image
# rate, and buffer length without touching Python.

import os
import sys
import json
import time
import hashlib
import asyncio
import logging
import urllib.parse

import slowpy.control
import slowpy.store

# When slowdash runs in a venv, pip's editable install for `slowpy` registers
# only the `slowpy` name on sys.meta_path — `lib/slowpy` itself is NOT on
# sys.path, so the `lib/slowpy/slowagent` symlink is invisible.  Resolve the
# real submodule directory via this file's realpath and add it to sys.path
# before importing.  Works whether the slowtask was symlinked or copied into
# the project's config/ directory.
_AGENT_LIB = os.path.normpath(os.path.join(
    os.path.dirname(os.path.realpath(__file__)), '..', 'lib'
))
if os.path.isdir(os.path.join(_AGENT_LIB, 'slowagent')) and _AGENT_LIB not in sys.path:
    sys.path.insert(0, _AGENT_LIB)

import slowagent
from slowagent import open_webcam, ClaudeVisionExtractor, LLMError


ctrl = slowpy.control.ControlSystem()


# ── State ───────────────────────────────────────────────────────────────── #

config        = None     # parsed slowagent-NAME.json
config_path   = None     # absolute path on disk
config_mtime  = 0        # so we can hot-reload when the JSON file changes

webcam        = None
extractor     = None
datastore_ts  = None     # numeric channels -> time-series datastore
datastore_obj = None     # blob-id channels -> object time-series
blob_storage  = None     # disk store for the LatestImage frame
blob_basedir  = 'data/blob'  # base dir of the blob storage (for sweep)

frame_buffer  = []       # rolling list of (sha256_hex, bytes) tuples
last_capture  = 0        # monotonic time of last frame
last_extract  = 0        # monotonic time of last LLM call

# Dedup: skip the LLM call when the captured frames are identical to the
# ones we extracted last time.  Cheap insurance against burning API tokens
# on a static webcam pointed at an idle instrument.  Cleared whenever the
# layout JSON is reloaded so prompt changes always re-extract.
last_extracted_hashes = set()


# ── Slowdash slowtask entry points ──────────────────────────────────────── #

async def _initialize(params):
    """Called once at task start.  `params` come from SlowdashProject.yaml."""
    global config_path, datastore_ts, datastore_obj, blob_storage
    global last_extract

    # Seed last_extract to "now" so the first extraction waits a full
    # buffer_minutes window before firing.  Otherwise the first cycle would
    # extract with a 1-frame buffer, the dedup cache would get seeded with
    # a partial hash set, and cycle 2 would have to make a wasted API call
    # to discover the steady-state set.  last_capture stays at 0 so the
    # first frame is captured right away — no point waiting.
    last_extract = time.monotonic()

    layout_file = params.get('layout_file')
    if not layout_file:
        logging.error("slowagent: missing `layout_file` parameter")
        return

    # Slowdash chdirs to the project directory before running tasks, so the
    # config dir is always at './config' from the slowtask's POV.  Allow an
    # explicit override for the standalone runner below.
    project_dir = params.get('project_dir') or os.environ.get('SLOWDASH_PROJECT') or os.getcwd()
    config_path = os.path.join(project_dir, 'config', layout_file)

    db_url = params.get('db_url') or os.environ.get('DB_URL', 'sqlite:///WebcamOCR.db')
    global blob_basedir
    blob_basedir = params.get('blob_directory', 'data/blob')

    # Two datastores: one for numeric values (the extracted channels), one for
    # blob-id strings (the LatestImage pointer). This mirrors the existing
    # Applications/Camera example.
    datastore_ts  = slowpy.store.create_datastore_from_url(db_url, 'data')
    datastore_obj = slowpy.store.create_datastore_from_url(db_url, 'photo')
    blob_storage  = slowpy.store.BlobStorage_File(
        basedir=blob_basedir,
        names=['%Y-%m', '%y%m%d-%H%M%S-%Z'],
        ext='.jpg',
    )

    _reload_config()
    logging.info("slowagent: initialized with layout %s", layout_file)

    # Run a first extraction immediately at startup so the dashboard is
    # populated right away without waiting a full buffer_minutes.  Capture
    # one frame per file the source currently holds (HTTP webcams report
    # frame_count() == 1, so they get one snapshot).
    if webcam is not None and extractor is not None:
        n = webcam.frame_count()
        logging.info("slowagent: priming initial buffer with %d frame(s)", n)
        for _ in range(n):
            _capture_one()
        if frame_buffer:
            await _extract_and_store()


async def _finalize():
    if datastore_ts:  datastore_ts.close()
    if datastore_obj: datastore_obj.close()
    if webcam:        webcam.close()


async def _loop():
    """Single iteration of the slowdash event loop."""
    if config is None:
        await ctrl.aio_sleep(1.0)
        return

    _maybe_reload_config()

    capture_interval = 60.0 / float(config['capture'].get('rate_per_min', 12))
    buffer_seconds   = 60.0 * float(config['capture'].get('buffer_minutes', 1))

    now = time.monotonic()

    # Capture a frame at the configured rate.
    if now - last_capture >= capture_interval:
        _capture_one()

    # Extract once we've buffered a full window — or every buffer_seconds, in
    # case the rate is high enough that we keep collecting forever.
    if frame_buffer and (now - last_extract >= buffer_seconds):
        await _extract_and_store()

    await ctrl.aio_sleep(0.5)


# ── Implementation ──────────────────────────────────────────────────────── #

def _reload_config():
    """Read slowagent-NAME.json and re-create webcam / LLM client if needed."""
    global config, config_mtime, webcam, extractor, last_extracted_hashes

    if not os.path.isfile(config_path):
        logging.warning("slowagent: config not found at %s", config_path)
        config = None
        return

    config_mtime = os.path.getmtime(config_path)
    with open(config_path) as f:
        config = json.load(f)

    # Force re-extraction on the next cycle — the prompt or channel list may
    # have changed even if the camera frames have not.
    last_extracted_hashes = set()

    src = config['capture']['source']
    src = _resolve_relative_source(src)

    # Rolling-disk options for HTTP webcams.  Ignored by directory sources
    # (they ARE the display dir already).
    cap = config.get('capture', {})
    rolling_dir  = cap.get('rolling_dir')
    rolling_keep = int(cap.get('rolling_keep', 10))
    if rolling_dir and not os.path.isabs(rolling_dir):
        rolling_dir = os.path.normpath(os.path.join(
            os.path.dirname(os.path.dirname(config_path)), rolling_dir))

    # Re-open the webcam if the source URL changed.
    if webcam is None or webcam.source != src:
        if webcam is not None:
            webcam.close()
        webcam = open_webcam(src, display_dir=rolling_dir, keep=rolling_keep)

    # Re-create the extractor if the model changed.
    llm_cfg = config.get('llm', {})
    model   = llm_cfg.get('model', 'claude-opus-4-7')
    max_tok = int(llm_cfg.get('max_tokens', 1024))
    if extractor is None or extractor._model != model or extractor._max_tokens != max_tok:
        try:
            extractor = ClaudeVisionExtractor(model=model, max_tokens=max_tok)
        except slowagent.SecretError as e:
            logging.error("slowagent: %s", e)
            extractor = None

    logging.info("slowagent: config reloaded — %d channel(s)",
                 len(config.get('channels', [])))


def _maybe_reload_config():
    if not config_path or not os.path.isfile(config_path):
        return
    if os.path.getmtime(config_path) > config_mtime:
        logging.info("slowagent: detected config change, reloading")
        _reload_config()


def _resolve_relative_source(src: str) -> str:
    """Allow `file://./last_images` style sources relative to the project dir.
    Absolute http(s):// and `file:///` URLs are passed through unchanged."""
    if src.startswith('http://') or src.startswith('https://'):
        return src
    if src.startswith('file:///'):
        return src

    # Anything else is treated as a path, possibly with a `file:` or `file://`
    # prefix.  We don't use urllib.parse here because `file://./foo` parses
    # with netloc='.' and path='/foo', losing the relative-ness.
    if src.startswith('file://'):
        rel = src[len('file://'):]
    elif src.startswith('file:'):
        rel = src[len('file:'):]
    else:
        rel = src

    if os.path.isabs(rel):
        return 'file://' + rel

    base = os.path.dirname(os.path.dirname(config_path))   # one above config/
    return 'file://' + os.path.normpath(os.path.join(base, rel))


def _capture_one():
    global last_capture
    if webcam is None:
        return
    try:
        frame = webcam.get()
    except Exception as e:
        logging.warning("slowagent: webcam capture failed: %s", e)
        return
    digest = hashlib.sha256(frame).hexdigest()
    frame_buffer.append((digest, frame))
    last_capture = time.monotonic()
    logging.debug("slowagent: captured frame %s, buffer=%d", digest[:8], len(frame_buffer))


async def _extract_and_store():
    """Send the buffered frames to Claude and write the results to the
    datastore.  Drops every frame except the most recent one (which becomes
    the LatestImage blob).

    Skips the LLM call entirely when the set of frame hashes in the buffer
    is identical to the set we extracted last time — same images in, same
    answer out, no API tokens spent.  Cache invalidates on config reload so
    prompt edits always trigger a fresh extraction."""
    global last_extract, frame_buffer, last_extracted_hashes

    last_extract = time.monotonic()

    if extractor is None:
        logging.warning("slowagent: extractor unavailable; dropping buffer")
        frame_buffer.clear()
        return

    # Dedup: same set of frames as last successful extraction → skip.
    current_hashes = {h for h, _ in frame_buffer}
    if current_hashes and current_hashes == last_extracted_hashes:
        logging.info("slowagent: %d frame(s) unchanged since last extract — "
                     "skipping LLM call", len(frame_buffer))
        frame_buffer.clear()
        return

    prompt   = config.get('llm', {}).get('prompt', '')
    channels = config.get('channels', [])

    # Build a channel-aware prompt so the LLM knows exactly which keys to use
    # in its JSON response.
    channel_block = '\n'.join(
        f"- \"{c['name']}\": {c.get('label') or c.get('description') or c['name']}"
        for c in channels
    )
    full_prompt = (
        f"{prompt.strip()}\n\n"
        f"Return a JSON object with exactly these keys (use null for any "
        f"channel you can't read):\n{channel_block}"
    )

    try:
        result = await extractor.extract([f for _, f in frame_buffer], full_prompt)
    except LLMError as e:
        logging.warning("slowagent: extraction failed: %s — clearing buffer", e)
        frame_buffer.clear()
        return

    logging.info("slowagent: extracted %d/%d channels (model=%s)",
                 sum(1 for v in result.values.values() if v is not None),
                 len(channels), result.model)

    # Cache only on success — failures should retry the same content next cycle.
    last_extracted_hashes = current_hashes

    # Write each channel value to the datastore.  Channels missing from the
    # response are silently skipped — the LLM has already explicitly said
    # "I can't read this" by setting null.
    declared = {c['name'] for c in channels}
    for name, value in result.values.items():
        if value is None:
            continue
        if name not in declared:
            logging.warning("slowagent: LLM returned undeclared channel %s", name)
            continue
        datastore_ts.append(value, tag=name)

    # Keep only the most recent frame, save it as the LatestImage blob, and
    # delete every other file under blob_basedir.  Sweeping the whole tree
    # (rather than tracking just the previous blob_id) handles three cases
    # naturally: (a) cross-restart leftovers, (b) crash-recovery, and
    # (c) someone dropping junk into the directory by hand.
    latest = frame_buffer[-1][1]     # (hash, bytes) → bytes
    frame_buffer = []                # release the in-memory buffer

    try:
        blob_id = blob_storage.write(latest)
        if blob_id:
            datastore_obj.append(blob_id, tag='LatestImage')
            _sweep_blob_dir(_blob_id_to_path(blob_id))
    except Exception as e:
        logging.warning("slowagent: failed to store latest frame: %s", e)


def _blob_id_to_path(blob_id_json):
    """Extract the relative path from a BlobStorage_File.write() result."""
    try:
        rec = json.loads(blob_id_json)
        bid = rec.get('id', '')
    except (ValueError, AttributeError):
        return None
    if not bid.startswith('file:'):
        return None
    return bid[len('file:'):]


def _sweep_blob_dir(keep_relpath):
    """Delete every image file under blob_basedir except the one at
    `keep_relpath`.  Also prunes empty subdirectories left behind."""
    if not blob_basedir or not os.path.isdir(blob_basedir):
        return
    keep_abs = os.path.normpath(os.path.join(blob_basedir, keep_relpath)) \
               if keep_relpath else None

    for root, dirs, files in os.walk(blob_basedir, topdown=False):
        for f in files:
            if not f.lower().endswith(('.jpg', '.jpeg', '.png')):
                continue
            path = os.path.join(root, f)
            if keep_abs and os.path.normpath(path) == keep_abs:
                continue
            try:
                os.remove(path)
            except OSError as e:
                logging.warning("slowagent: cannot delete stale blob %s: %s", path, e)
        # rmdir is a no-op for non-empty dirs; empty dirs get pruned.
        if root != blob_basedir:
            try:
                os.rmdir(root)
            except OSError:
                pass


# ── Standalone runner (handy for development) ───────────────────────────── #

if __name__ == '__main__':
    async def main():
        logging.basicConfig(level=logging.INFO)
        params = {
            'layout_file': os.environ.get('SLOWAGENT_LAYOUT', 'slowagent-Omega.json'),
            'project_dir': os.environ.get('SLOWAGENT_PROJECT', os.getcwd()),
            'db_url':      os.environ.get('DB_URL', 'sqlite:///WebcamOCR.db'),
        }
        await _initialize(params)
        ctrl.stop_by_signal()
        while not ctrl.is_stop_requested():
            await _loop()
        await _finalize()

    asyncio.run(main())
