# slowdash-agent-tools

LLM-driven monitoring tools for [SlowDash](https://github.com/slowproj/slowdash).

This submodule turns a webcam pointed at a screen-only instrument
(an Omega CN1500 controller, a benchtop multimeter, a chiller front panel, …)
into a normal SlowDash time-series. A vision LLM (Claude, invoked via the
headless `claude` CLI — your Claude Code subscription, no API key needed)
reads each captured frame, extracts the displayed numbers as JSON, and
writes them to the SlowDash datastore. Plot, alarm, and analyze them like
any other channel.

## Why

A lot of lab equipment has only a 7-segment or LCD display — no Ethernet,
no USB, no API. Pointing a webcam at the screen and OCR-ing the frames is
the cheapest path to remote monitoring, but classic OCR (tesseract et al.)
struggles with multi-zone instruments that:

- cycle several values through one display in scan mode (the Omega CN1500
  ramps through CTR1..CTR7 with one zone visible at a time),
- use color-coded LED indicators or per-channel unit-tags to disambiguate
  which channel a number refers to,
- have inconsistent lighting, glare, or partial occlusion.

A multi-modal LLM handles all of that from a natural-language prompt — the
user describes what's on the screen and which channel names to extract, and
Claude returns structured JSON that the slowtask writes to the datastore.

Vision/OCR is delegated to whatever LLM the user's local `claude` CLI is
authenticated against (typically Claude.ai via OAuth on a Pro / Max plan).
This module never reads or stores an Anthropic API key.

## File structure

```
slowdash-agent-tools/
├── install.sh                        # Symlink installer
├── requirements.txt                  # Pillow (+ tomli on <3.11) — claude CLI installed separately
├── site/
│   ├── slowagent.html                # Linked into app/site/slowagent.html
│   └── slowagent/                    # Linked into app/site/slowagent/
│       ├── slowagent-app.mjs           Page orchestration (cycler, prompt, plot iframe)
│       ├── slowagent-api.mjs           REST client (wraps /api/agent/*, /api/data/*)
│       └── slowagent.css               Layout & component styles
├── server/
│   └── sd_agent.py                   # slowlette user module: /api/agent/*
├── lib/slowagent/                    # Linked into lib/slowpy/slowagent/
│   ├── __init__.py
│   ├── secrets.py                      Generic TOML-backed secret loader (vestigial)
│   ├── webcam.py                       Frame capture (HTTP camera or local dir)
│   ├── llm.py                          Subprocess wrapper around `claude -p` + JSON parsing
│   └── sandbox.py                      Restricted exec scaffold (not used by default)
└── slowtask/
    └── slowtask-webcam_ocr.py        # Symlink this into your project's config/
```

## Quick start

```bash
# 1. Add as a submodule of your slowdash checkout
cd /path/to/slowdash
git submodule add git@github.com:yaosarayin/slowdash-agent-tools.git slowdash-agent-tools

# 2. Install Python dependencies (Pillow for image resize, tomli on <3.11)
pip install -r slowdash-agent-tools/requirements.txt

# 3. Run install.sh — creates three symlinks under the slowdash tree:
#       app/site/slowagent.html
#       app/site/slowagent/
#       lib/slowpy/slowagent/
bash slowdash-agent-tools/install.sh

# 4. Make sure the headless `claude` CLI is installed and signed in.
#    Vision/OCR is invoked as `claude -p ...`; auth lives in your Claude
#    Code install (typically a Claude.ai subscription via OAuth — no
#    Anthropic API key required).
#       https://claude.com/claude-code
claude auth login    # one-time, only if you haven't already

# 5. Try the bundled WebcamOCR example
cd ExampleProjects/WebcamOCR
slowdash --port=18881
# then open http://localhost:18881/slowagent.html?config=slowagent-Omega.json
```

## Wiring into your own project

Add to your `SlowdashProject.yaml`:

```yaml
slowdash_project:
  data_source:
    url: sqlite:///MyProject
    time_series:        { schema: data[channel]@timestamp(unix)=value }
    object_time_series: { schema: photo[channel]@timestamp(unix)=value }
    blob_storage:       { type: file, base_directory: data/blob }

  module:
    file: ../../slowdash-agent-tools/server/sd_agent.py
    # path is relative to the project directory

  task:
    - name: webcam_ocr
      auto_load: true
      parameters:
        layout_file: slowagent-Foo.json
```

Symlink the slowtask into your `config/` directory:

```bash
ln -s ../../slowdash-agent-tools/slowtask/slowtask-webcam_ocr.py \
      config/slowtask-webcam_ocr.py
```

Then create a `slowagent-Foo.json` (the slowtask will auto-generate the
matching `slowplot-Foo.json` from the channels it actually sees). See
`ExampleProjects/WebcamOCR/config/` for working starting points.

## Layout JSON schema (`slowagent-NAME.json`)

```json
{
  "meta":     { "name": "Foo", "title": "..." },

  "capture":  {
    "source":            "http://10.0.0.91/photo.jpg",
    "cycle_seconds":      30,
    "frames_per_cycle":   14,
    "frame_interval":     1.5,
    "batch_dir":          "last_images",

    "//optional knobs":   "",
    "max_image_dim":      1280,
    "jpeg_quality":       85,
    "capture_retries":    3,
    "capture_retry_wait": 1.0
  },

  "llm": {
    "model":          "claude-opus-4-7",
    "max_tokens":     1024,
    "prompt":         "natural-language extraction prompt",
    "example_prompt": "shown read-only on the page as a hint to the user"
  },

  "channels": [
    { "name": "ctr1", "label": "Zone 1 (°C)", "color": "#009090", "ymin": 0, "ymax": 200 }
  ],

  "plot": { "length": 86400, "reload": 5 }
}
```

| Field                          | Default       | What it controls                                                                                                                       |
|--------------------------------|---------------|----------------------------------------------------------------------------------------------------------------------------------------|
| `capture.source`               | —             | `http(s)://…` for live camera, or `file://./dir` / `/abs/path` for a directory of test images.                                          |
| `capture.cycle_seconds`        | `30`          | Start-to-start interval between batches.                                                                                               |
| `capture.frames_per_cycle`     | `14`          | Number of frames captured per cycle (each goes to the LLM in one call).                                                                |
| `capture.frame_interval`       | `1.5`         | Seconds between consecutive captures within a batch.                                                                                   |
| `capture.batch_dir`            | `last_images` | Directory the slowtask wipes and re-fills with the current batch (HTTP source). For file:// sources, this field is ignored — the source IS the directory. Path is relative to the project directory. |
| `capture.max_image_dim`        | `1280`        | Longest edge after downsize, in pixels. Keeps each LLM request well under the 5 MB / image cap and shortens vision-tool latency.        |
| `capture.jpeg_quality`         | `85`          | JPEG quality after downsize (0–100).                                                                                                    |
| `capture.capture_retries`      | `3`           | Per-frame retry count if the camera returns non-image data (e.g. a busy CGI returning a Python traceback as the body).                  |
| `capture.capture_retry_wait`   | `1.0`         | Seconds between capture retries.                                                                                                        |
| `llm.model`                    | `claude-opus-4-7` | Claude model id (passed verbatim to `claude -p --model …`; aliases like `opus`/`sonnet` also work).                                |
| `llm.max_tokens`               | `1024`        | Soft hint for response length (the headless CLI doesn't expose a hard cap; defaults are fine for a JSON-only response).                |
| `llm.prompt`                   | —             | Free-text prompt sent with each batch. Edit live via the **Save Prompt** button on the dashboard.                                       |
| `llm.example_prompt`           | —             | Read-only hint shown in the page UI for users authoring a prompt.                                                                       |
| `channels[]`                   | —             | List of declared channels; only entries listed here get written to the datastore. The auto-generated slowplot uses these colors/labels. |
| `plot.length`                  | `86400`       | Time window the auto-generated slowplot uses (seconds). Default = 24 hours so the plot still shows history during quiet periods.        |
| `plot.reload`                  | `5`           | Plot refresh interval in seconds.                                                                                                       |

The slowtask polls the file's mtime; saving the prompt from the page (or
editing the JSON directly) takes effect within ~10 seconds without
restarting the task.

## How it works

### Per-cycle batch model

1. The slowtask reads `slowagent-NAME.json` from your project's `config/`.
2. Every `cycle_seconds` (start-to-start), it wipes the `batch_dir`,
   captures `frames_per_cycle` frames spaced by `frame_interval` seconds,
   and saves each one with a timestamped name (`YYYYMMDD-HHMMSS-NNN.jpg`).
3. Each captured frame is **validated** as a real JPEG/PNG (caches
   returning HTML/Python tracebacks are rejected) and **downsized** to
   `max_image_dim` longest-edge — keeps each image well under the 5 MB
   per-image cap and the dashboard cycler responsive.
4. The whole batch is written to a fresh per-call temp directory and a
   single `claude -p` subprocess is spawned with `--tools Read --add-dir
   <tmp>`. Claude reads each image and returns one JSON object covering
   the entire batch. Sending the batch (rather than a single frame) is
   what lets the LLM piece together which value belongs to which channel
   on a multi-zone display.
5. Claude returns JSON like `{"ctr1": 21.0, "ctr2": null, …}`. The slowtask
   writes each non-null value to the datastore via
   `slowpy.store.DataStore.append(value, tag=channel_name)`.

### Dedup with continuous plotting

If every frame's SHA-256 in the new batch matches the previous successful
batch, the LLM call is **skipped** to save tokens. To keep the plot fed
during these idle periods, the slowtask **replays the cached values** with
the current timestamp — so the time-series shows a continuous trace
("temperature was still X at time T") instead of a gap. Cache invalidates
on any `slowagent-NAME.json` edit so prompt changes always re-run.

### Auto-channel discovery

The slowtask maintains a set of channels it has ever seen non-null data for
(seeded from existing DB rows on startup). Whenever a new channel first
appears, it:
- regenerates `slowplot-NAME.json` to show only the seen channels (with
  the colors/labels from `channels[]` in the layout JSON), and
- triggers `GET /api/channels?force_rescan=true` to invalidate slowdash's
  startup channel-list cache, so `/api/data/<channel>` starts returning
  rows immediately rather than after the next slowdash restart.

### DB persistence

The datastore opens in slowpy's append mode (default `recreate=False`).
Rows survive slowdash restarts. The slowtask never deletes the DB file —
the only way to wipe history is for the user to remove `*.db` manually.

## Frontend

The page at `/slowagent.html?config=slowagent-NAME.json`:

- **Recent Frames panel (left, top)** — cycles through every JPEG/PNG
  currently in the batch directory, ~1.5 s per frame, with a small caption
  showing the filename and `frame N/M`. Polls the file list every 5 s so
  newly-captured batches appear automatically.
- **Extraction Prompt panel (left, mid)** — editable textarea pre-filled
  with the current prompt. Two buttons:
  - **Save Prompt** — writes the new prompt to `slowagent-NAME.json` via
    `POST /api/agent/prompt/{file}`. The slowtask reloads within ~10 s.
  - **Force Refresh** — calls `POST /api/control {"force_refresh": true}`,
    which re-runs the LLM right now on the frames currently in
    `batch_dir`, bypassing the dedup cache. A short hint below the buttons
    explains what each does.
- **Example Prompt panel (left, bottom)** — read-only textarea showing
  `llm.example_prompt` from the layout, useful as a starting point when
  authoring a new prompt.
- **Live Plots panel (right)** — iframe of `slowplot.html?config=slowplot-NAME.json`
  with the slowdash header hidden, so it looks like a single integrated
  plot panel.

The header, theme, and styling come from the standard slowdash `Frame`
class, so switching themes (`style.theme: dark` in your project) just works.

`Cmd/Ctrl-S` in the prompt textarea is bound to **Save Prompt**.

The home page (`/`, `slowhome.html`) auto-discovers content types from
`/api/config/contentlist` instead of using a hardcoded list, so any
`slowagent-NAME.json` you drop in `config/` shows up in the catalog
without further configuration.

## REST endpoints

| Route                                          | Method | Purpose                                                                          |
|------------------------------------------------|--------|----------------------------------------------------------------------------------|
| `/api/agent/layouts`                           | GET    | List of `slowagent-*.json` layouts in the project.                                |
| `/api/agent/prompt/{layout_file}`              | POST   | Patch only the `llm.prompt` field of a layout (body: `{"prompt": "..."}`).        |
| `/api/agent/frames/{layout_file}`              | GET    | List frames currently in the batch directory, newest-first.                       |
| `/api/agent/frame/{layout_file}/{filename}`    | GET    | Serve a single frame from the batch directory.                                    |
| `/api/control` (body `{"force_refresh":true}`) | POST   | Trigger an immediate LLM run on the current batch (bypasses dedup).               |
| `/api/config/file/{filename}`                  | GET    | (slowdash) Load any layout JSON.                                                  |
| `/api/data/{channels}?length=&to=`             | GET    | (slowdash) Time-series data.                                                      |
| `/api/channels?force_rescan=true`              | GET    | (slowdash) List channels; `force_rescan` re-reads the schema from the data store. |

All path-traversal attempts on the frame endpoints are rejected via both a
filename-syntax check and a `realpath`-based directory-containment check.

## Security model

- **Authentication**: the slowtask spawns the headless `claude` CLI per
  cycle and lets Claude Code resolve auth (typically a Claude.ai
  subscription via OAuth in the user's keychain). This module reads no
  Anthropic API key. To force OAuth even on machines that have a stale
  `ANTHROPIC_API_KEY` exported, the subprocess env is sanitized — the
  key is dropped before exec.
- **Subprocess sandboxing**: the `claude -p` invocation uses
  `--tools Read` (only the file-Read tool is exposed to the model),
  `--permission-mode bypassPermissions`, `--no-session-persistence`, and
  `--setting-sources ""` (skip user-level plugins, hooks, and agents).
  `--add-dir` grants Read access only to the per-call temp directory
  containing the current batch — Claude cannot reach project files,
  the slowdash DB, or anything else.
- **No code execution from the LLM**: by design the LLM is asked for JSON
  values, not Python. The slowtask never `eval`s the response. The
  `slowagent.sandbox` module is scaffolded for a future advanced mode (an
  AST-validated sandbox that allows only a curated subset of builtins,
  blocks `import`, `__import__`, and `__dunder__` access) but it is **not**
  enabled by the default slowtask.
- **Frame retention**: captured frames live only in process memory between
  capture and extraction, plus the timestamped files inside `batch_dir`,
  plus the per-call temp directory which is auto-deleted when the
  subprocess returns. At the start of every cycle the slowtask **wipes
  every image file in `batch_dir`** before writing the new batch — so on
  disk there is exactly one batch (the most recent one) at any time.
- **Path safety**: the frame-serving endpoints validate filenames (no
  path separators, no leading dots, no `..`) and use `os.path.realpath`
  to confirm the resolved file is inside the configured directory before
  reading it.

## Tested

End-to-end against a live Omega CN1500 controller via a Raspberry Pi
camera (timelapse photo.jpg endpoint):

- **DB persistence across restart**: starting slowdash with an existing
  DB logs `slowagent: N channel(s) already in DB: …` and `/api/channels`
  returns those channels immediately.
- **Auto-rescan on first new channel**: a fresh DB triggers
  `slowagent: triggered slowdash channel rescan` after the first
  successful extract, so `/api/channels` and `/api/data` start returning
  rows in the same session — no slowdash restart required.
- **Auto-generated slowplot**: `slowplot-NAME.json` is rewritten each
  time a new channel first appears, listing only the seen channels.
- **Image validation**: when the camera CGI returns 200 OK with a Python
  traceback as the body, the slowtask rejects it (logged as "non-image
  data") and retries up to `capture_retries` times.
- **Image downsize**: 4056×3040 source JPEGs (≈2 MB) are downsized to
  1280-px JPEGs (≈100–200 KB) before going to the LLM — total request
  size drops from ≈28 MB to ≈2 MB and the 5 MB / image cap is never hit.
- **Force Refresh**: clicking the dashboard button posts
  `{"force_refresh": true}` to `/api/control`; the slowtask logs
  `force_refresh triggered` and runs an extra cycle within seconds.
- **Cycler panel**: lists 14 frames per batch, each fetchable as a real
  JPEG/PNG; spaces in filenames work via URL encoding; path-traversal
  attempts return HTTP 400.
- **Dedup replay**: when frames are byte-identical, the LLM call is
  skipped and the cached values are re-written with the current timestamp
  so the plot keeps a continuous trace.

The only step that depends on the network is the actual LLM call. If
`claude` is missing from PATH, the user isn't authenticated, or the
subprocess errors, the slowtask logs the failure and skips the cycle;
the rest of the pipeline keeps running.

## License

MIT
