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
# Runtime behaviour, all driven by the layout JSON:
#
#   - For HTTP sources (`source: http://…`), every `cycle_seconds` we wipe
#     `batch_dir` (default `last_images`), capture `frames_per_cycle` frames
#     pacing them by `frame_interval` seconds, save each one with a
#     timestamped filename, and run the LLM on the whole batch.
#   - For file:// sources (testing / demo), every cycle we re-read whatever
#     image files are currently in the directory and run the LLM on them
#     (the directory is never modified — those files are user-supplied
#     test data).
#
# Dedup: if every frame's SHA-256 in the new batch matches the previous
# successful batch, the LLM call is skipped — same input, same output, no
# API tokens spent.  Useful when a static camera or a frozen instrument
# returns identical frames cycle after cycle.
#
# Persistence: the SQLite (or other) datastore is opened in append-only
# mode.  History survives slowdash restarts.  The user is the only one
# who deletes the DB.

import os
import io
import sys
import json
import time
import hashlib
import asyncio
import logging
import urllib.request

try:
    from PIL import Image
    _have_pil = True
except ImportError:
    _have_pil = False

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
config_mtime  = 0        # for hot-reload

webcam        = None
extractor     = None
datastore_ts  = None     # numeric channels -> time-series datastore
project_dir   = None     # for resolving relative paths

# Pacing.
last_cycle_start = 0     # monotonic time the last cycle began

# Dedup: skip the LLM call when this batch's frame hashes match the previous
# successful batch.  Cleared on config reload so prompt changes always
# trigger a fresh extraction.
last_extracted_hashes = set()
# Cached values from the last successful LLM call.  When dedup fires we
# re-write these into the datastore with the *current* timestamp so the
# plot keeps a continuous trace even though we didn't burn an API call.
# Faithful to reality: "as of time T, the temperature was still X".
last_extracted_values = {}

# Set of channel names we have ever seen non-null data for.  Used to keep
# slowplot-NAME.json populated only with channels that are actually
# connected, and to auto-add new ones when they first appear.  Seeded
# from the existing DB at startup so a slowdash restart doesn't lose the
# list.
seen_channels = set()

_IMAGE_EXTS = ('.jpg', '.jpeg', '.png')


# ── Slowdash slowtask entry points ──────────────────────────────────────── #

async def _initialize(params):
    """Called once at task start.  `params` come from SlowdashProject.yaml."""
    global config_path, datastore_ts, project_dir

    layout_file = params.get('layout_file')
    if not layout_file:
        logging.error("slowagent: missing `layout_file` parameter")
        return

    project_dir = params.get('project_dir') or os.environ.get('SLOWDASH_PROJECT') or os.getcwd()
    config_path = os.path.join(project_dir, 'config', layout_file)

    db_url = params.get('db_url') or os.environ.get('DB_URL', 'sqlite:///WebcamOCR.db')

    # Open the datastore in APPEND mode (slowpy default).  Existing rows
    # survive restarts.  Only the user can wipe the DB by deleting the file.
    datastore_ts = slowpy.store.create_datastore_from_url(db_url, 'data')

    # Seed the seen-channels set from rows that already exist in the DB —
    # so the auto-generated slowplot reflects the full history, not just
    # what we capture in this session.
    seen_channels.update(_db_existing_channels(db_url))
    if seen_channels:
        logging.info("slowagent: %d channel(s) already in DB: %s",
                     len(seen_channels), ', '.join(sorted(seen_channels)))

    _reload_config()
    _maybe_update_slowplot()
    logging.info("slowagent: initialized with layout %s", layout_file)

    # Run a first cycle right now so the dashboard has data immediately.
    await _run_cycle()


async def _finalize():
    if datastore_ts: datastore_ts.close()
    if webcam:       webcam.close()


async def _loop():
    """Drive cycles every `cycle_seconds` (start-to-start)."""
    if config is None:
        await ctrl.aio_sleep(1.0)
        return

    _maybe_reload_config()

    cycle_seconds = float(config.get('capture', {}).get('cycle_seconds', 30))
    if time.monotonic() - last_cycle_start >= cycle_seconds:
        await _run_cycle()

    await ctrl.aio_sleep(0.5)


# ── User-callable command (POST /api/control {"force_refresh": true}) ───── #

async def force_refresh():
    """Force the LLM to run on the currently-available frames right away,
    bypassing the dedup cache.  Exposed automatically by slowdash's task
    machinery as a /api/control action."""
    global last_extracted_hashes
    last_extracted_hashes = set()    # clear cache so a re-run isn't deduped
    logging.info("slowagent: force_refresh triggered")
    await _run_cycle()
    return True


# ── Cycle: capture a batch of frames, then extract ──────────────────────── #

async def _run_cycle():
    """One full pass: capture (or re-read) a batch, run LLM, write data."""
    global last_cycle_start
    last_cycle_start = time.monotonic()

    if webcam is None:
        logging.warning("slowagent: webcam not configured; skipping cycle")
        return

    src = config['capture']['source']
    if src.startswith('http://') or src.startswith('https://'):
        frames = await _capture_batch_http()
    else:
        frames = _read_dir_frames(webcam.display_dir)

    if not frames:
        logging.warning("slowagent: no frames captured this cycle")
        return

    await _extract_and_store(frames)


async def _capture_batch_http():
    """Capture `frames_per_cycle` frames from the HTTP webcam, pacing by
    `frame_interval` seconds.  Wipes `batch_dir` first, then writes each
    captured frame with a timestamped name so the dashboard cycler shows
    the latest batch."""
    cap = config['capture']
    n        = int(cap.get('frames_per_cycle', 14))
    interval = float(cap.get('frame_interval', 1.5))
    batch_dir = webcam.display_dir   # set by _resolve_capture_paths

    if batch_dir:
        _wipe_image_dir(batch_dir)
        try:
            os.makedirs(batch_dir, exist_ok=True)
        except OSError as e:
            logging.warning("slowagent: cannot create %s: %s", batch_dir, e)

    max_dim    = int(cap.get('max_image_dim', 1280))   # fits Anthropic per-image limit
    quality    = int(cap.get('jpeg_quality', 85))
    retries    = int(cap.get('capture_retries', 3))     # retry intermittent camera locks
    retry_wait = float(cap.get('capture_retry_wait', 1.0))

    ts_prefix = time.strftime('%Y%m%d-%H%M%S')
    frames = []
    for i in range(n):
        # Some HTTP cameras (notably the picamera2 photo.cgi bundled with
        # slowdash) intermittently fail to acquire the device when another
        # process is holding it — they return a 200 OK with a Python
        # traceback as the body.  Retry a few times with a short wait
        # before giving up on this frame.
        blob = None
        ext  = None
        for attempt in range(1, retries + 1):
            try:
                blob = webcam.get()
            except Exception as e:
                logging.warning("slowagent: capture %d/%d attempt %d/%d failed: %s",
                                i + 1, n, attempt, retries, e)
                blob = None
            if blob is not None:
                ext = _image_ext(blob)
                if ext is not None:
                    break
                if attempt == 1:
                    preview = blob[:80].decode('latin-1', errors='replace').replace('\n', ' ')
                    logging.debug("slowagent: capture %d/%d non-image (%d bytes) — "
                                  "will retry (%r…)", i + 1, n, len(blob), preview)
            if attempt < retries:
                await ctrl.aio_sleep(retry_wait)

        if ext is None:
            logging.warning("slowagent: capture %d/%d gave up after %d retries "
                            "(camera busy / non-image response)",
                            i + 1, n, retries)
            if i < n - 1:
                await ctrl.aio_sleep(interval)
            continue

        # Downsize to keep the LLM request under Anthropic's 5 MB / image
        # limit and to keep the dashboard cycler responsive.  4056x3040 from
        # a Pi HQ camera is ~2 MB; 1280-longest-edge JPEG is ~100-200 KB
        # and still plenty for 7-segment OCR.
        blob_small = _downsize(blob, max_dim, quality)
        if blob_small is None:
            blob_small = blob
        else:
            ext = '.jpg'

        fname = f"{ts_prefix}-{i + 1:03d}{ext}"
        if batch_dir:
            try:
                with open(os.path.join(batch_dir, fname), 'wb') as f:
                    f.write(blob_small)
            except OSError as e:
                logging.warning("slowagent: cannot save %s: %s", fname, e)
        frames.append(blob_small)

        if i < n - 1:
            await ctrl.aio_sleep(interval)

    total_kb = sum(len(b) for b in frames) // 1024
    logging.info("slowagent: captured %d/%d frame(s) (%d KB total) into %s",
                 len(frames), n, total_kb, batch_dir or '(memory only)')
    return frames


def _downsize(blob: bytes, max_dim: int, quality: int):
    """Resize `blob` so the longest side is at most `max_dim` pixels and
    re-encode as JPEG.  Returns the new bytes, or None if PIL is missing or
    the image can't be decoded (caller falls back to the original)."""
    if not _have_pil:
        return None
    try:
        img = Image.open(io.BytesIO(blob))
        img.load()
    except Exception as e:
        logging.warning("slowagent: cannot decode frame for downsize: %s", e)
        return None
    if max(img.size) > max_dim:
        img.thumbnail((max_dim, max_dim))
    out = io.BytesIO()
    img.convert('RGB').save(out, 'JPEG', quality=quality, optimize=True)
    return out.getvalue()


def _image_ext(blob: bytes):
    """Return '.jpg' / '.png' if `blob` looks like a real JPEG or PNG;
    None for anything else.  Used to refuse saving garbage CGI responses
    (Python tracebacks, HTML error pages, etc.) under a .jpg name."""
    if not blob:
        return None
    if blob.startswith(b'\xff\xd8\xff'):     # JPEG SOI marker
        return '.jpg'
    if blob.startswith(b'\x89PNG\r\n\x1a\n'):
        return '.png'
    return None


def _read_dir_frames(path):
    """Read every image file in `path` into a list of bytes.  Used for the
    file:// (test/demo) source where the directory is curated by hand."""
    if not path or not os.path.isdir(path):
        return []
    frames = []
    for f in sorted(os.listdir(path)):
        if not f.lower().endswith(_IMAGE_EXTS):
            continue
        try:
            with open(os.path.join(path, f), 'rb') as fp:
                frames.append(fp.read())
        except OSError as e:
            logging.warning("slowagent: cannot read %s: %s", f, e)
    return frames


def _wipe_image_dir(path):
    """Delete every image file under `path` (non-recursive — the slowtask
    writes flat into the batch dir).  Logs but does not raise on errors."""
    if not path or not os.path.isdir(path):
        return
    for f in os.listdir(path):
        if not f.lower().endswith(_IMAGE_EXTS):
            continue
        try:
            os.remove(os.path.join(path, f))
        except OSError as e:
            logging.warning("slowagent: cannot remove %s: %s", f, e)


# ── LLM extraction ──────────────────────────────────────────────────────── #

async def _extract_and_store(frames):
    """Send `frames` (list[bytes]) to Claude, parse the JSON response, and
    write each extracted channel value to the datastore.

    On dedup hit (same frames as last cycle), we don't call the LLM — but
    we DO re-write the cached values with the current timestamp so the
    plot keeps a continuous trace.  The new rows are labelled the same as
    the originals; their fresh timestamps just reflect "as of now, the
    temperature was still X"."""
    global last_extracted_hashes, last_extracted_values

    if extractor is None:
        logging.warning("slowagent: extractor unavailable; skipping LLM call")
        return

    declared = {c['name'] for c in config.get('channels', [])}

    # Dedup: same set of frames as the last successful extraction → skip
    # the LLM but still feed the plot from the cache.
    current_hashes = {hashlib.sha256(b).hexdigest() for b in frames}
    if current_hashes and current_hashes == last_extracted_hashes and last_extracted_values:
        replayed = 0
        for name, value in last_extracted_values.items():
            if name not in declared or value is None:
                continue
            datastore_ts.append(value, tag=name)
            replayed += 1
        logging.info("slowagent: %d frame(s) unchanged — skipped LLM, "
                     "replayed %d cached channel(s) into the plot",
                     len(frames), replayed)
        return

    prompt   = config.get('llm', {}).get('prompt', '')
    channels = config.get('channels', [])

    channel_block = '\n'.join(
        f"- \"{c['name']}\": {c.get('label') or c.get('description') or c['name']}"
        for c in channels
    )
    full_prompt = (
        f"{prompt.strip()}\n\n"
        f"Return a JSON object with exactly these keys (use null for any "
        f"channel you cannot read in any frame):\n{channel_block}"
    )

    try:
        result = await extractor.extract(frames, full_prompt)
    except LLMError as e:
        logging.warning("slowagent: extraction failed: %s", e)
        return

    n_read = sum(1 for v in result.values.values() if v is not None)
    logging.info("slowagent: extracted %d/%d channels (model=%s)",
                 n_read, len(channels), result.model)

    # Cache only on success — failures retry the same content next cycle.
    last_extracted_hashes = current_hashes
    last_extracted_values = {n: v for n, v in result.values.items()
                             if n in declared and v is not None}

    new_channel_seen = False
    for name, value in result.values.items():
        if value is None:
            continue
        if name not in declared:
            logging.warning("slowagent: LLM returned undeclared channel %s", name)
            continue
        datastore_ts.append(value, tag=name)
        if name not in seen_channels:
            seen_channels.add(name)
            new_channel_seen = True

    # If a previously-unseen channel just showed up, regenerate the
    # slowplot config so the new line appears on the plot after a refresh,
    # AND tell slowdash to re-scan its channel cache (it locks the cache
    # at startup, so a `data` table created after that scan would otherwise
    # be invisible to /api/channels and /api/data until the next restart).
    if new_channel_seen:
        _maybe_update_slowplot()
        _trigger_slowdash_rescan()


# ── Config loading ──────────────────────────────────────────────────────── #

def _reload_config():
    """Read slowagent-NAME.json and re-create the webcam / LLM client if
    the source URL or model changed.  Always clears the dedup cache so a
    prompt edit re-runs extraction immediately."""
    global config, config_mtime, webcam, extractor, last_extracted_hashes

    if not os.path.isfile(config_path):
        logging.warning("slowagent: config not found at %s", config_path)
        config = None
        return

    config_mtime = os.path.getmtime(config_path)
    with open(config_path) as f:
        config = json.load(f)

    last_extracted_hashes = set()

    src = config['capture']['source']
    src = _resolve_relative_source(src)

    batch_dir = config.get('capture', {}).get('batch_dir', 'last_images')
    if not os.path.isabs(batch_dir):
        batch_dir = os.path.normpath(os.path.join(project_dir, batch_dir))

    # Re-open the webcam if the source URL changed.  For HTTP, `display_dir`
    # = batch_dir (where the slowtask writes captured frames).  For file://,
    # the source directory IS the display dir; the open_webcam factory
    # ignores display_dir for those.
    if webcam is None or webcam.source != src:
        if webcam is not None:
            webcam.close()
        try:
            globals()['webcam'] = open_webcam(src, display_dir=batch_dir)
        except Exception as e:
            logging.error("slowagent: cannot open webcam %s: %s", src, e)
            globals()['webcam'] = None

    # Re-create the extractor if the model changed.
    llm_cfg = config.get('llm', {})
    model   = llm_cfg.get('model', 'claude-opus-4-7')
    max_tok = int(llm_cfg.get('max_tokens', 1024))
    if extractor is None or extractor._model != model or extractor._max_tokens != max_tok:
        try:
            globals()['extractor'] = ClaudeVisionExtractor(model=model, max_tokens=max_tok)
        except slowagent.SecretError as e:
            logging.error("slowagent: %s", e)
            globals()['extractor'] = None

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

    if src.startswith('file://'):
        rel = src[len('file://'):]
    elif src.startswith('file:'):
        rel = src[len('file:'):]
    else:
        rel = src

    if os.path.isabs(rel):
        return 'file://' + rel

    return 'file://' + os.path.normpath(os.path.join(project_dir, rel))


# ── Auto-generated slowplot config ──────────────────────────────────────── #

def _db_existing_channels(db_url):
    """Return the set of channel names that already have rows in the
    datastore.  Used at startup so the auto-generated slowplot reflects
    history, not just what we capture in this session.

    Best-effort: only implemented for the SQLite URL form we ship by
    default.  Other backends fall back to an empty set."""
    if not db_url.startswith('sqlite:///'):
        return set()
    db_path = db_url[len('sqlite:///'):]
    if not db_path.endswith('.db'):
        db_path += '.db'
    if not os.path.isfile(db_path):
        return set()
    try:
        import sqlite3
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT DISTINCT channel FROM data")
            return {row[0] for row in cur.fetchall()}
    except Exception as e:
        logging.warning("slowagent: cannot read channel list from DB: %s", e)
        return set()


def _layout_basename():
    """slowagent-Omega.json -> Omega"""
    name = os.path.splitext(os.path.basename(config_path))[0]
    if name.startswith('slowagent-'):
        return name[len('slowagent-'):]
    return name


def _maybe_update_slowplot():
    """Rewrite slowplot-{layout_name}.json so it lists ONLY the channels
    we have ever seen non-null data for.  No-op if the file already
    matches the current set — so this can be called every cycle without
    churning the disk."""
    if not config or not config_path or not seen_channels:
        return

    layout_name   = _layout_basename()
    slowplot_path = os.path.join(os.path.dirname(config_path),
                                 f'slowplot-{layout_name}.json')

    declared = config.get('channels', [])
    visible  = [c for c in declared if c['name'] in seen_channels]
    visible_names = [c['name'] for c in visible]

    if not visible:
        return

    # Skip if the existing slowplot already has exactly these channels in
    # this order — avoids touching the file every cycle.
    try:
        with open(slowplot_path) as f:
            existing = json.load(f)
        existing_names = [p.get('channel') for p in
                          (existing.get('panels') or [{}])[0].get('plots', [])]
        if existing_names == visible_names:
            return
    except (OSError, ValueError, KeyError):
        pass

    plot_cfg = config.get('plot', {})
    plots = []
    for c in visible:
        plots.append({
            "type":          "timeseries",
            "channel":       c['name'],
            "color":         c.get('color', '#666'),
            "label":         c.get('label', c['name']),
            "format":        "%.1f",
            "opacity":       1,
            "marker_type":   "circle",
            "marker_size":   3,
            "line_width":    1,
            "line_type":     "connect",
            "fill_opacity":  0,
            "fill_envelope": False,
            "fill_baseline": 1e-100,
        })

    title = config.get('meta', {}).get('title', layout_name)
    doc = {
        "control": {
            "range":  {"length": int(plot_cfg.get('length', 86400)), "to": 0},
            "reload": int(plot_cfg.get('reload', 5)),
            "mode":   "normal",
            "grid":   {"rows": 1, "columns": 1},
        },
        "style":  {},
        "panels": [{
            "type":   "timeseries",
            "plots":  plots,
            "axes":   {
                "xfixed": False, "yfixed": False,
                "xlog":   False, "ylog":   False, "zlog": False,
                "ymin": 0, "ymax": 200,
                "title":  "Zone Temperatures (°C)",
                "ytitle": "Temperature (°C)",
            },
            "legend": {"style": "transparent", "position": "left"},
        }],
        "meta": {
            "name":        layout_name,
            "title":       f"{title} — Live Plot",
            "description": "Auto-generated by slowtask-webcam_ocr.py from the channels seen so far.",
        },
    }

    try:
        with open(slowplot_path, 'w') as f:
            json.dump(doc, f, indent=2)
        logging.info("slowagent: rewrote %s with %d channel(s): %s",
                     os.path.basename(slowplot_path), len(visible),
                     ', '.join(visible_names))
    except OSError as e:
        logging.warning("slowagent: cannot write %s: %s", slowplot_path, e)


def _trigger_slowdash_rescan():
    """GET /api/channels?force_rescan=true on the local slowdash instance.

    Slowdash scans its data source at startup and caches the result.  When
    the slowtask writes the very first row of a fresh DB, the `data` table
    didn't exist during that scan — so /api/channels and /api/data stay
    empty until something invalidates the cache.  This call does that.

    The port comes from the SLOWDASH_PORT env var if set (slowdash
    propagates it), else defaults to 18881."""
    port = os.environ.get('SLOWDASH_PORT', '18881')
    url  = f'http://127.0.0.1:{port}/api/channels?force_rescan=true'
    try:
        req = urllib.request.Request(url, method='GET')
        with urllib.request.urlopen(req, timeout=2) as r:
            r.read()
        logging.info("slowagent: triggered slowdash channel rescan")
    except Exception as e:
        logging.warning("slowagent: could not trigger rescan (%s): %s", url, e)


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
