# slowdash-agent-tools

LLM-driven monitoring tools for [SlowDash](https://github.com/slowproj/slowdash).

This submodule turns a webcam pointed at a screen-only instrument
(an Omega CN1500 controller, a benchtop multimeter, a chiller front panel, …)
into a normal SlowDash time-series. A vision LLM (Anthropic Claude) reads
each captured frame, extracts the displayed numbers as JSON, and writes them
to the SlowDash datastore. Plot, alarm, and analyze them like any other
channel.

## Why

A lot of lab equipment has only a 7-segment or LCD display — no Ethernet,
no USB, no API. Pointing a webcam at the screen and OCR-ing the frames is
the cheapest path to remote monitoring, but classic OCR (tesseract et al.)
struggles with multi-zone instruments that:

- cycle several values through one display in scan mode (the Omega CN1500
  ramps through CTR1..CTR7 with one zone visible at a time),
- use color-coded LED indicators that disambiguate which channel a number
  refers to,
- have inconsistent lighting, glare, or partial occlusion.

A multi-modal LLM handles all of that from a natural-language prompt — the
user describes what's on the screen and which channel names to extract, and
Claude returns structured JSON that the slowtask writes to the datastore.

## File structure

```
slowdash-agent-tools/
├── install.sh                        # Symlink installer
├── requirements.txt                  # anthropic>=0.40.0 (and tomli on <3.11)
├── site/
│   ├── slowagent.html                # Linked into app/site/slowagent.html
│   └── slowagent/                    # Linked into app/site/slowagent/
│       ├── slowagent-app.mjs           Page orchestration (header, image, prompt, plots)
│       ├── slowagent-api.mjs           REST client (wraps /api/agent/*, /api/data/*)
│       └── slowagent.css               Layout & component styles
├── server/
│   └── sd_agent.py                   # slowlette user module: /api/agent/*
├── lib/slowagent/                    # Linked into lib/slowpy/slowagent/
│   ├── __init__.py
│   ├── secrets.py                      Secure API-key loader (mode 0600 enforced)
│   ├── webcam.py                       Frame capture (HTTP camera or local dir)
│   ├── llm.py                          Anthropic Claude client + JSON parsing
│   └── sandbox.py                      Restricted exec scaffold (not used by default)
└── slowtask/
    └── slowtask-webcam_ocr.py        # Symlink this into your project's config/
```

## Quick start

```bash
# 1. Add as a submodule of your slowdash checkout
cd /path/to/slowdash
git submodule add <this-repo-url> slowdash-agent-tools

# 2. Install dependencies (anthropic SDK, plus tomli on Python < 3.11)
pip install -r slowdash-agent-tools/requirements.txt

# 3. Run install.sh — creates three symlinks under the slowdash tree:
#       app/site/slowagent.html
#       app/site/slowagent/
#       lib/slowpy/slowagent/
bash slowdash-agent-tools/install.sh

# 4. Save your Anthropic API key (mode 0600 is enforced by the loader)
mkdir -p ~/.config/slowdash
cat > ~/.config/slowdash/secrets.toml <<'EOF'
anthropic_api_key = "sk-ant-api03-..."
EOF
chmod 600 ~/.config/slowdash/secrets.toml

#    Or set the environment variable instead:
#    export ANTHROPIC_API_KEY=sk-ant-api03-...

# 5. Try the bundled example (no real camera required — it cycles through
#    sample images of an Omega CN1500 controller)
cd ExampleProjects/WebcamOCR
slowdash --port=18881
# then open http://localhost:18881/slowagent.html?config=slowagent-Omega.json
```

## Wiring into your own project

Add to your `SlowdashProject.yaml`:

```yaml
slowdash_project:
  # … your existing data_source / style / etc. …

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

Then create a `slowagent-Foo.json` (and matching `slowplot-Foo.json` if you
want a custom right-hand plot panel) in the same `config/` directory. See
`ExampleProjects/WebcamOCR/config/` for working starting points.

## Layout JSON schema (`slowagent-NAME.json`)

```json
{
  "meta":     {"name": "Foo", "title": "..."},
  "capture":  {
    "source": "file://./last_images" | "http://192.168.1.43/photo.cgi",
    "rate_per_min":   12,            // frames per minute
    "buffer_minutes":  1             // window of frames sent to the LLM per call
  },
  "llm": {
    "model":      "claude-opus-4-7",
    "max_tokens": 1024,
    "prompt":         "natural-language extraction prompt",
    "example_prompt": "shown read-only on the page as a hint to the user"
  },
  "channels": [
    {"name": "ctr1", "label": "Zone 1 (°C)", "color": "#009090", "ymin": 0, "ymax": 200},
    …
  ],
  "plot": {"length": 600, "reload": 5}
}
```

The slowtask polls the file's mtime; saving the prompt from the page (or
editing the JSON directly) takes effect within ~10 seconds without
restarting the task.

## How it works

1. The slowtask reads `slowagent-NAME.json` from your project's `config/`.
2. Every `60/rate_per_min` seconds it grabs a frame from the webcam and
   appends it to a rolling in-memory buffer.
3. After every `buffer_minutes` worth of frames, the slowtask sends the
   *whole* buffer plus the prompt to Claude. Sending the buffer (rather
   than a single frame) is what lets the LLM piece together which value
   belongs to which channel on a multi-zone display that cycles through
   them.
4. Claude returns JSON like `{"ctr1": 21.0, "ctr2": 25.5, "ctr3": null, …}`.
5. The slowtask:
   - writes each non-null value to the datastore via
     `slowpy.store.DataStore.append(value, tag=channel_name)`,
   - saves the most recent frame as the `LatestImage` blob channel for the
     dashboard,
   - **deletes the previous LatestImage file from disk** so only one frame
     is ever on disk at a time,
   - drops every other in-memory frame.

## Frontend

The page at `/slowagent.html?config=slowagent-NAME.json` shows:

- **Left side** — latest webcam frame (polled every 3 s from
  `/api/blob/LatestImage`), an editable textarea for the LLM prompt with a
  `Save Prompt` button (writes to the layout JSON via
  `POST /api/agent/prompt/{file}`), and a read-only example prompt below it.
- **Right side** — an iframe of `slowplot.html?config=slowplot-NAME.json`
  with the slowdash header hidden, so it looks like a single integrated
  plot panel.

The header, theme, and styling come from the standard slowdash `Frame`
class, so switching themes (`style.theme: dark` in your project) just works.

Cmd/Ctrl-S in the prompt textarea saves; the slowtask hot-reloads the
config on its next loop tick.

## REST endpoints

| Route                                  | Method | Purpose                              |
|----------------------------------------|--------|--------------------------------------|
| `/api/agent/layouts`                   | GET    | list of `slowagent-*.json` layouts   |
| `/api/agent/prompt/{layout_file}`      | POST   | patch only the `llm.prompt` field    |
| `/api/config/file/{filename}`          | GET    | (existing) load any layout JSON      |
| `/api/data/{channels}?length=&to=`     | GET    | (existing) time-series data          |
| `/api/blob/{channel}?id=`              | GET    | (existing) the LatestImage blob      |

## Security model

- **API key**: looked up by `slowagent.secrets.get_secret('anthropic_api_key')`
  in this order — TOML at `~/.config/slowdash/secrets.toml` (the loader
  refuses to read it if it's group- or world-readable), then the
  `ANTHROPIC_API_KEY` environment variable. Keys never appear in the
  project repo, datastore, or layout files.
- **No code execution from the LLM**: by design the LLM is asked for JSON
  values, not Python. The slowtask never `eval`s the response. The
  `slowagent.sandbox` module is scaffolded for a future advanced mode (an
  AST-validated sandbox that allows only a curated subset of builtins,
  blocks `import`, `__import__`, and `__dunder__` access) but it is **not**
  enabled by the default slowtask.
- **Frame retention**: captured frames live only in process memory between
  capture and extraction. After extraction:
    - every frame in the in-memory buffer is dropped,
    - the most recent frame is written to disk as the `LatestImage` blob,
    - the *previous* `LatestImage` file is deleted before returning.
  At any moment exactly one frame exists on disk — the one currently
  visible in the dashboard.
- **Path safety**: blob deletion validates the id against the same
  filename rules as `BlobStorage_File.get_blob()` (alphanumerics plus
  `_-+.`, no leading dots, no path traversal).

## Tested

- **Python imports**: `slowagent` and all submodules import cleanly under
  the slowdash venv (verified via `lib/slowpy/slowagent` symlink + the
  slowtask's `sys.path` injection).
- **Slowdash startup**: with the WebcamOCR example project, slowdash boots,
  loads `sd_agent` as a user module, registers the `webcam_ocr` slowtask,
  serves `/slowagent.html`, and the slowtask runs its capture loop.
- **REST endpoints**: `/api/agent/layouts` lists the layout, `/api/agent/prompt/{file}`
  patches the prompt, `/api/config/contentlist` shows the slowagent entry
  via slowdash's built-in scanner.
- **Hot reload**: editing `slowagent-NAME.json` (manually or via the page's
  Save Prompt button) is detected within ~10 s and the slowtask picks up
  the new prompt without restart.
- **JSON parser tolerance**: handles fenced code blocks, leading prose,
  numeric strings, and explicit `null` values from the LLM.
- **Sandbox guardrails**: `import`, `__import__`, dunder attribute access
  all rejected before the script runs.
- **Frame retention**: confirmed via end-to-end test that exactly one
  blob exists on disk after each extraction (the most recent one), with
  the previous file removed.

The only step that depends on a working Anthropic API key is the actual
LLM call. With an invalid key, the slowtask logs the auth error and clears
the buffer; the rest of the pipeline keeps running.

## License

MIT
