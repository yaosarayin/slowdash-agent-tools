// slowagent-app.mjs — SlowDash LLM Agent page entry point.
// Author: Yao Yin
//
// Layout:
//   ┌────────────── header (Frame, themed by project) ──────────────┐
//   │                                                                │
//   │  ┌──────────────────┐   ┌────────────────────────────────────┐│
//   │  │ recent frames    │   │  slowplot controls (selects+status)││
//   │  │ (cycler)         │   ├────────────────────────────────────┤│
//   │  ├──────────────────┤   │   live plots                       ││
//   │  │ capture cadence  │   │   (slowplot iframe)                ││
//   │  ├──────────────────┤   │                                    ││
//   │  │ connected        │   │                                    ││
//   │  │ channels         │   │                                    ││
//   │  ├──────────────────┤   │                                    ││
//   │  │ prompt textarea  │   │                                    ││
//   │  │ + save button    │   │                                    ││
//   │  ├──────────────────┤   │                                    ││
//   │  │ example prompt   │   │                                    ││
//   │  │ (read-only)      │   │                                    ││
//   │  └──────────────────┘   └────────────────────────────────────┘│
//   └────────────────────────────────────────────────────────────────┘

import { JG as $ } from '../slowjs/jagaimo/jagaimo.mjs';
import { Frame }  from '../slowjs/frame.mjs';
import { AgentAPI } from './slowagent-api.mjs';


// Cycler tuning.  The list-poll interval is the main robustness lever:
// the slowtask wipes batch_dir at the start of every cycle, so any cached
// list older than a couple of seconds risks pointing at files that no
// longer exist (→ 404 on <img>).  Polling at 2 s keeps the gap small.
const FRAME_LIST_REFRESH_MS = 2000;
const FRAME_CYCLE_MS        = 1500;


export class SlowAgentApp {

    constructor() {
        this.frame      = null;
        this.config     = null;        // parsed slowagent-NAME.json
        this.configFile = null;        // filename including the .json extension
        this.projectConfig = null;

        this._imgEl     = null;
        this._promptEl  = null;
        this._exampleEl = null;
        this._statusEl  = null;
        this._iframeEl  = null;

        // Cycler state
        this._cycleFrames    = [];   // [{name, mtime}, ...]
        this._cycleIdx       = 0;
        this._cycleTimer     = null;
        this._lastListMs     = 0;
        this._listInFlight   = null;
        this._captionEl      = null;

        // Settings controls
        this._cycleInput      = null;
        this._cycleApplyBtn   = null;
        this._framesInput     = null;
        this._framesApplyBtn  = null;
        this._intervalInput   = null;
        this._intervalApplyBtn = null;
        this._cycleStatus     = null;
        this._channelChecks   = {};   // {channelName: <input type=checkbox>}
        this._channelStatus   = null;
        this._channelTimer    = null;

        // Prompt save state
        this._initialPrompt   = '';
    }


    async run() {
        const params = new URLSearchParams(window.location.search);
        this.configFile = params.get('config') || '';

        try {
            this.projectConfig = await AgentAPI.getProjectConfig();
        } catch (e) {
            document.body.innerHTML =
                `<h3>Cannot connect to SlowDash backend: ${e.message}</h3>`;
            return;
        }

        const theme = this.projectConfig?.style?.theme || 'light';
        try { await this._loadTheme(theme); }
        catch (e) { console.warn('theme css failed to load', e); }

        const projTitle = this.projectConfig?.project?.title
                       || this.projectConfig?.project?.name
                       || 'SlowDash';
        this.frame = new Frame($('#sa-header'), {
            title:         projTitle + ' — Agent',
            style:         this.projectConfig?.style || {},
            initialStatus: 'LLM Agent',
        });
        this._buildHeaderControls();

        if (!this.configFile) {
            this._buildEmptyBody();
            return;
        }

        try {
            this.config = await AgentAPI.loadLayout(this.configFile);
        } catch (e) {
            this._buildEmptyBody(`Cannot load ${this.configFile}: ${e.message}`);
            return;
        }

        document.title = `SlowAgent — ${this.config?.meta?.title || this.configFile}`;
        this.frame.setStatus(`Loaded: ${this.configFile}`);

        this._buildBody();
        this._wireEvents();
        this._startFrameCycler();
    }


    // ── Theme load (mirrors Platform._load_theme) ──────────────────────── //

    _loadTheme(theme) {
        return new Promise((resolve, reject) => {
            const link = document.getElementById('sd-theme-css');
            if (!link) return resolve();
            link.addEventListener('load',  () => resolve(), { once: true });
            link.addEventListener('error', (e) => reject(e), { once: true });
            link.setAttribute('href', 'slowjs/slowdash-' + theme + '.css');
        });
    }


    // ── Body layout ────────────────────────────────────────────────────── //

    _buildEmptyBody(msg) {
        const body = document.getElementById('sa-body');
        body.innerHTML = '';
        body.className = 'sa-body sa-body-empty';
        const card = document.createElement('div');
        card.className = 'sa-empty-card';
        card.innerHTML = `
            <h2>No layout loaded</h2>
            <p>${msg || 'Open a SlowAgent layout from the home page or pass <code>?config=slowagent-NAME.json</code> in the URL.'}</p>
        `;
        body.appendChild(card);
    }

    _buildBody() {
        const body = document.getElementById('sa-body');
        body.innerHTML = '';
        body.className = 'sa-body';

        // Left column
        const left = document.createElement('div');
        left.className = 'sa-col sa-col-left';

        this._buildImageSection(left);
        this._buildCaptureSection(left);
        this._buildChannelsSection(left);
        this._buildPromptSection(left);
        this._buildExampleSection(left);

        body.appendChild(left);

        // Splitter
        const splitter = document.createElement('div');
        splitter.className = 'sa-splitter';
        body.appendChild(splitter);
        this._wireSplitter(splitter, left);

        // Right column — slowplot iframe
        const right = document.createElement('div');
        right.className = 'sa-col sa-col-right';

        const plotHdr = document.createElement('div');
        plotHdr.className = 'sa-section-hdr';
        plotHdr.textContent = 'Live Plots';
        right.appendChild(plotHdr);

        const iframe = document.createElement('iframe');
        iframe.className = 'sa-plot-iframe';
        iframe.title = 'Live plots';
        iframe.src = this._plotURL();

        // Trim the embedded slowplot's header to JUST the row of plot-control
        // selects (Time Range, Auto-Reload, Grid) and the "Update: …" status.
        // We hide the redundant title, clock, logo, and the duplicate
        // Home/Help/Save/Snapshot buttons — those are already in the
        // slowagent header above.
        iframe.addEventListener('load', () => {
            try {
                const doc = iframe.contentDocument;
                if (!doc) return;
                if (!doc.getElementById('sa-trim-header-style')) {
                    const style = doc.createElement('style');
                    style.id = 'sa-trim-header-style';
                    style.textContent =
                        '.sd-header-title{display:none!important}' +
                        '.sd-header-logo{display:none!important}' +
                        '.sd-header-clock{display:none!important}' +
                        '.sd-header-buttons{display:none!important}' +
                        '.sd-header-progress{display:none!important}' +
                        '#sd-header{padding:2px 6px!important;min-height:0!important}' +
                        'body{margin:0!important}' +
                        '#sd-layout{margin:0!important;padding:0!important}';
                    doc.head.appendChild(style);
                }
            } catch (e) { /* same-origin so this should never fire */ }
        });
        right.appendChild(iframe);
        this._iframeEl = iframe;

        body.appendChild(right);
    }

    _buildImageSection(left) {
        const wrap = document.createElement('div');
        wrap.className = 'sa-section sa-image-wrap';
        const hdr = document.createElement('div');
        hdr.className = 'sa-section-hdr';
        hdr.textContent = 'Recent Frames';
        wrap.appendChild(hdr);

        const inner = document.createElement('div');
        inner.className = 'sa-image-inner';
        const img = document.createElement('img');
        img.className = 'sa-image sa-hidden';
        img.alt = 'Recent webcam frames';
        inner.appendChild(img);
        const placeholder = document.createElement('div');
        placeholder.className = 'sa-image-placeholder';
        placeholder.textContent = 'Waiting for the slowtask to capture frames…';
        inner.appendChild(placeholder);
        const caption = document.createElement('div');
        caption.className = 'sa-image-caption';
        inner.appendChild(caption);
        wrap.appendChild(inner);
        left.appendChild(wrap);

        this._imgEl          = img;
        this._imgPlaceholder = placeholder;
        this._captionEl      = caption;
    }

    _buildCaptureSection(left) {
        const wrap = document.createElement('div');
        wrap.className = 'sa-section';
        const hdr = document.createElement('div');
        hdr.className = 'sa-section-hdr';
        hdr.textContent = 'Capture Cadence';
        wrap.appendChild(hdr);

        const _makeRow = (labelText, value, min, max, step, applyTitle) => {
            const row   = document.createElement('div');
            row.className = 'sa-capture-row';
            const lbl   = document.createElement('span');
            lbl.className = 'sa-capture-lbl';
            lbl.textContent = labelText;
            const inp   = document.createElement('input');
            inp.type = 'number';
            inp.min  = String(min);
            inp.max  = String(max);
            inp.step = String(step);
            inp.className = 'sa-capture-input';
            inp.value = String(value);
            const btn   = document.createElement('button');
            btn.className = 'sa-btn';
            btn.textContent = 'Apply';
            btn.title = applyTitle;
            row.appendChild(lbl);
            row.appendChild(inp);
            row.appendChild(btn);
            return { row, inp, btn };
        };

        const cycleRow = _makeRow(
            'Batch every',
            this.config?.capture?.cycle_seconds ?? 30,
            5, 86400, 1,
            'Save the new refresh rate to ' + this.configFile + '. The slowtask picks it up within a few seconds.'
        );
        const cycleUnit = document.createElement('span');
        cycleUnit.className = 'sa-capture-unit';
        cycleUnit.textContent = 'seconds';
        cycleRow.row.insertBefore(cycleUnit, cycleRow.btn);
        wrap.appendChild(cycleRow.row);

        const framesRow = _makeRow(
            'Frames per batch',
            this.config?.capture?.frames_per_cycle ?? 5,
            1, 60, 1,
            'How many photos to take per cycle. Saved to ' + this.configFile + '.'
        );
        const framesUnit = document.createElement('span');
        framesUnit.className = 'sa-capture-unit';
        framesUnit.textContent = 'frames';
        framesRow.row.insertBefore(framesUnit, framesRow.btn);
        wrap.appendChild(framesRow.row);

        const intervalRow = _makeRow(
            'Interval between frames',
            this.config?.capture?.frame_interval ?? 1.0,
            0.1, 60, 0.1,
            'Seconds between each frame within a batch. Saved to ' + this.configFile + '.'
        );
        const intervalUnit = document.createElement('span');
        intervalUnit.className = 'sa-capture-unit';
        intervalUnit.textContent = 'seconds';
        intervalRow.row.insertBefore(intervalUnit, intervalRow.btn);
        wrap.appendChild(intervalRow.row);

        const status = document.createElement('span');
        status.className = 'sa-prompt-status sa-capture-status';
        wrap.appendChild(status);

        const hint = document.createElement('div');
        hint.className = 'sa-hint';
        hint.textContent =
            'Batch every: how often a new batch starts (min 5 s). '
          + 'Frames per batch: photos taken each cycle. '
          + 'Interval: seconds between individual frames. '
          + 'All values are saved to the layout file and picked up by the slowtask within a few seconds.';
        wrap.appendChild(hint);

        this._cycleInput      = cycleRow.inp;
        this._cycleApplyBtn   = cycleRow.btn;
        this._framesInput     = framesRow.inp;
        this._framesApplyBtn  = framesRow.btn;
        this._intervalInput   = intervalRow.inp;
        this._intervalApplyBtn = intervalRow.btn;
        this._cycleStatus     = status;

        left.appendChild(wrap);
    }

    _buildChannelsSection(left) {
        const channels = this.config?.channels || [];
        if (!channels.length) return;

        const wrap = document.createElement('div');
        wrap.className = 'sa-section';
        const hdr = document.createElement('div');
        hdr.className = 'sa-section-hdr';
        hdr.textContent = 'Connected Channels';
        wrap.appendChild(hdr);

        const list = document.createElement('div');
        list.className = 'sa-channels';

        this._channelChecks = {};

        for (const c of channels) {
            const row = document.createElement('label');
            row.className = 'sa-channel-row';
            const cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.className = 'sa-channel-cb';
            cb.checked = (c.connected !== false);  // default true if absent
            const dot = document.createElement('span');
            dot.className = 'sa-channel-dot';
            dot.style.background = c.color || '#888';
            const lbl = document.createElement('span');
            lbl.className = 'sa-channel-label';
            lbl.textContent = c.label || c.name;
            row.appendChild(cb);
            row.appendChild(dot);
            row.appendChild(lbl);
            list.appendChild(row);

            this._channelChecks[c.name] = cb;
        }

        wrap.appendChild(list);

        const statusRow = document.createElement('div');
        statusRow.className = 'sa-channels-status-row';
        const status = document.createElement('span');
        status.className = 'sa-prompt-status';
        statusRow.appendChild(status);
        wrap.appendChild(statusRow);
        this._channelStatus = status;

        const hint = document.createElement('div');
        hint.className = 'sa-hint';
        hint.textContent =
            'Uncheck channels that are not physically connected. '
          + 'The LLM is told explicitly to ignore unchecked channels, '
          + 'and any value the LLM still returns for an unchecked channel '
          + 'is dropped before being plotted.';
        wrap.appendChild(hint);

        left.appendChild(wrap);
    }

    _buildPromptSection(left) {
        const wrap = document.createElement('div');
        wrap.className = 'sa-section';
        const hdr = document.createElement('div');
        hdr.className = 'sa-section-hdr';
        hdr.textContent = 'Extraction Prompt';
        wrap.appendChild(hdr);

        const area = document.createElement('textarea');
        area.className = 'sa-textarea sa-prompt';
        area.spellcheck = false;
        area.value = this.config?.llm?.prompt || '';
        area.placeholder = 'Describe what the LLM should extract from each frame…';
        wrap.appendChild(area);
        this._promptEl = area;
        this._initialPrompt = area.value;

        const row = document.createElement('div');
        row.className = 'sa-prompt-row';
        const saveBtn = document.createElement('button');
        saveBtn.className = 'sa-btn sa-btn-primary';
        saveBtn.textContent = 'Save Prompt';
        saveBtn.title = 'Save this prompt to ' + this.configFile + '.  '
                      + 'It becomes the default — loaded automatically on '
                      + 'next page visit and picked up by the slowtask within '
                      + 'a couple of seconds.';
        const refreshBtn = document.createElement('button');
        refreshBtn.className = 'sa-btn';
        refreshBtn.textContent = 'Force Refresh';
        refreshBtn.title = 'Run the LLM right now on the frames currently in last_images';
        const status = document.createElement('span');
        status.className = 'sa-prompt-status';
        row.appendChild(saveBtn);
        row.appendChild(refreshBtn);
        row.appendChild(status);
        wrap.appendChild(row);

        const hint = document.createElement('div');
        hint.className = 'sa-hint';
        hint.textContent =
            'Save Prompt: persists the prompt to the layout file (this becomes '
          + 'the default for next visit). Force Refresh: re-runs the LLM '
          + 'immediately on the current images, bypassing the dedup cache — '
          + 'useful for testing a prompt edit without waiting for the next cycle.';
        wrap.appendChild(hint);

        this._statusEl   = status;
        this._saveBtn    = saveBtn;
        this._refreshBtn = refreshBtn;

        left.appendChild(wrap);
    }

    _buildExampleSection(left) {
        const example = (this.config?.llm?.example_prompt || '').trim();
        if (!example) return;

        const wrap = document.createElement('div');
        wrap.className = 'sa-section';
        const hdr = document.createElement('div');
        hdr.className = 'sa-section-hdr';
        hdr.textContent = 'Example Prompt (read-only)';
        wrap.appendChild(hdr);
        const body = document.createElement('textarea');
        body.className = 'sa-textarea sa-example';
        body.readOnly = true;
        body.value = example;
        wrap.appendChild(body);
        this._exampleEl = body;
        left.appendChild(wrap);
    }

    _plotURL() {
        const baseName = this.configFile.replace(/^slowagent-/, '').replace(/\.json$/, '');
        const slowplotFile = `slowplot-${baseName}.json`;
        return `slowplot.html?config=${encodeURIComponent(slowplotFile)}`;
    }

    _wireEvents() {
        // ── Prompt save ───────────────────────────────────────────────── //
        this._saveBtn.addEventListener('click', () => this._savePrompt());

        const refreshSaveBtnState = () => {
            if (this._promptEl.value !== this._initialPrompt) {
                this._saveBtn.classList.add('sa-btn-modified');
            } else {
                this._saveBtn.classList.remove('sa-btn-modified');
            }
        };
        this._promptEl.addEventListener('input', refreshSaveBtnState);

        this._refreshBtn.addEventListener('click', async () => {
            this._setStatus('Forcing LLM refresh…');
            this._refreshBtn.disabled = true;
            try {
                await AgentAPI.sendCommand('force_refresh');
                this._setStatus('Refresh queued — values will appear in the plot shortly.');
            } catch (e) {
                this._setStatus(`Error: ${e.message}`, true);
            } finally {
                this._refreshBtn.disabled = false;
            }
        });

        this._promptEl.addEventListener('keydown', (e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === 's') {
                e.preventDefault();
                this._saveBtn.click();
            }
        });

        // ── Capture cadence apply ─────────────────────────────────────── //
        this._cycleApplyBtn.addEventListener('click', () => this._saveCycleSeconds());
        this._cycleInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); this._saveCycleSeconds(); }
        });
        this._framesApplyBtn.addEventListener('click', () => this._saveFramesPerCycle());
        this._framesInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); this._saveFramesPerCycle(); }
        });
        this._intervalApplyBtn.addEventListener('click', () => this._saveFrameInterval());
        this._intervalInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); this._saveFrameInterval(); }
        });

        // ── Connected-channel checkboxes (debounced auto-save) ────────── //
        for (const cb of Object.values(this._channelChecks)) {
            cb.addEventListener('change', () => this._saveConnectedDebounced());
        }
    }

    async _savePrompt() {
        const newPrompt = this._promptEl.value;
        this._setStatus('Saving…');
        try {
            await AgentAPI.updatePrompt(this.configFile, newPrompt);
            if (this.config?.llm) this.config.llm.prompt = newPrompt;
            this._initialPrompt = newPrompt;
            this._saveBtn.classList.remove('sa-btn-modified');
            this._setStatus('Saved. This is now the default prompt — it will load on next visit.');
        } catch (e) {
            this._setStatus(`Error: ${e.message}`, true);
        }
    }

    async _saveCycleSeconds() {
        const cs = parseFloat(this._cycleInput.value);
        if (!isFinite(cs) || cs < 5 || cs > 86400) {
            this._setCycleStatus('Enter a number between 5 and 86400 seconds.', true);
            return;
        }
        this._setCycleStatus('Saving…');
        try {
            await AgentAPI.updateSettings(this.configFile, { cycle_seconds: cs });
            if (this.config?.capture) this.config.capture.cycle_seconds = cs;
            this._setCycleStatus(`Saved — slowtask will pick up ${cs}s shortly.`);
        } catch (e) {
            this._setCycleStatus(`Error: ${e.message}`, true);
        }
    }

    async _saveFramesPerCycle() {
        const n = parseInt(this._framesInput.value, 10);
        if (!isFinite(n) || n < 1 || n > 60) {
            this._setCycleStatus('Frames per batch must be between 1 and 60.', true);
            return;
        }
        this._setCycleStatus('Saving…');
        try {
            await AgentAPI.updateSettings(this.configFile, { frames_per_cycle: n });
            if (this.config?.capture) this.config.capture.frames_per_cycle = n;
            this._setCycleStatus(`Saved — ${n} frames per batch.`);
        } catch (e) {
            this._setCycleStatus(`Error: ${e.message}`, true);
        }
    }

    async _saveFrameInterval() {
        const s = parseFloat(this._intervalInput.value);
        if (!isFinite(s) || s < 0.1 || s > 60) {
            this._setCycleStatus('Frame interval must be between 0.1 and 60 seconds.', true);
            return;
        }
        this._setCycleStatus('Saving…');
        try {
            await AgentAPI.updateSettings(this.configFile, { frame_interval: s });
            if (this.config?.capture) this.config.capture.frame_interval = s;
            this._setCycleStatus(`Saved — ${s}s between frames.`);
        } catch (e) {
            this._setCycleStatus(`Error: ${e.message}`, true);
        }
    }

    _saveConnectedDebounced() {
        clearTimeout(this._channelTimer);
        this._setChannelStatus('Saving…');
        this._channelTimer = setTimeout(async () => {
            const flags = {};
            for (const [name, cb] of Object.entries(this._channelChecks)) {
                flags[name] = cb.checked;
            }
            try {
                const res = await AgentAPI.updateSettings(
                    this.configFile, { connected: flags }
                );
                for (const c of (this.config?.channels || [])) {
                    if (c.name in flags) c.connected = flags[c.name];
                }
                const n = Object.values(flags).filter(Boolean).length;
                this._setChannelStatus(
                    `Saved — ${n} of ${Object.keys(flags).length} channel(s) connected.`
                );
                // Server-side slowplot regen ran inside the settings POST
                // (atomic with the slowagent file write).  If the channel
                // set actually changed, force the iframe to reload so the
                // legend reflects the new selection immediately.
                if (res?.slowplot_changed && this._iframeEl) {
                    this._iframeEl.src = this._plotURL() + '&_t=' + Date.now();
                }
            } catch (e) {
                this._setChannelStatus(`Error: ${e.message}`, true);
            }
        }, 350);
    }

    _setStatus(text, isError = false) {
        if (!this._statusEl) return;
        this._statusEl.textContent = text;
        this._statusEl.style.color = isError ? '#c0392b' : '';
    }
    _setCycleStatus(text, isError = false) {
        if (!this._cycleStatus) return;
        this._cycleStatus.textContent = text;
        this._cycleStatus.style.color = isError ? '#c0392b' : '';
    }
    _setChannelStatus(text, isError = false) {
        if (!this._channelStatus) return;
        this._channelStatus.textContent = text;
        this._channelStatus.style.color = isError ? '#c0392b' : '';
    }


    // ── Cycling-frames display ─────────────────────────────────────────── //

    _refreshFrameList() {
        // Coalesce concurrent in-flight requests.
        if (this._listInFlight) return this._listInFlight;
        this._listInFlight = (async () => {
            try {
                const list = await AgentAPI.listSourceFrames(this.configFile);
                if (Array.isArray(list)) {
                    // Show in chronological order.  API returns newest-first.
                    this._cycleFrames = [...list].sort((a, b) => a.mtime - b.mtime);
                    if (this._cycleIdx >= this._cycleFrames.length) {
                        this._cycleIdx = 0;
                    }
                }
            } catch (e) { /* keep previous list on error */ }
            finally {
                this._listInFlight = null;
                this._lastListMs = Date.now();
            }
        })();
        return this._listInFlight;
    }

    _startFrameCycler() {
        // If an image fails to load (most often: stale entry pointing at a
        // file the slowtask just wiped) re-list immediately so we drop it
        // before the next tick paints another broken image.
        this._imgEl.addEventListener('error', () => {
            // Hide the broken-image icon while we recover.
            this._imgEl.classList.add('sa-hidden');
            this._refreshFrameList();
        });

        const tick = () => {
            // Re-poll the directory at FRAME_LIST_REFRESH_MS or sooner.
            // This keeps the list fresh enough that the slowtask's wipe-
            // and-fill doesn't leave us with stale 404-prone entries.
            if (Date.now() - this._lastListMs > FRAME_LIST_REFRESH_MS) {
                this._refreshFrameList();
            }

            const frames = this._cycleFrames;
            if (!frames || frames.length === 0) {
                this._imgEl.classList.add('sa-hidden');
                this._imgPlaceholder.classList.remove('sa-hidden');
                this._imgPlaceholder.textContent =
                    'Waiting for the slowtask to capture frames…';
                if (this._captionEl) this._captionEl.textContent = '';
                return;
            }
            const i = this._cycleIdx % frames.length;
            const frame = frames[i];
            this._imgEl.src = AgentAPI.sourceFrameURL(this.configFile, frame.name);
            this._imgEl.classList.remove('sa-hidden');
            this._imgPlaceholder.classList.add('sa-hidden');
            if (this._captionEl) {
                this._captionEl.textContent =
                    `${frame.name}  ·  frame ${i + 1}/${frames.length}`;
            }
            this._cycleIdx = (this._cycleIdx + 1) % frames.length;
        };

        // Initial fetch + immediate tick so the first frame appears fast.
        this._refreshFrameList().then(() => tick());
        this._cycleTimer = setInterval(tick, FRAME_CYCLE_MS);
    }


    // ── Splitter (drag-to-resize the left column) ──────────────────────── //

    _wireSplitter(splitter, leftCol) {
        splitter.addEventListener('mousedown', (e) => {
            e.preventDefault();
            const startX = e.clientX;
            const startW = leftCol.getBoundingClientRect().width;
            const total  = leftCol.parentElement.getBoundingClientRect().width;

            const veil = document.createElement('div');
            veil.style.cssText = 'position:fixed;inset:0;cursor:col-resize;z-index:99999';
            document.body.appendChild(veil);

            const onMove = (ev) => {
                const w = Math.max(240, Math.min(total - 240, startW + (ev.clientX - startX)));
                leftCol.style.flex = `0 0 ${w}px`;
            };
            const onUp = () => {
                window.removeEventListener('mousemove', onMove);
                window.removeEventListener('mouseup',   onUp);
                veil.remove();
            };
            window.addEventListener('mousemove', onMove);
            window.addEventListener('mouseup',   onUp);
        });
    }


    // ── Header buttons ─────────────────────────────────────────────────── //

    _buildHeaderControls() {
        const reloadBtn = document.createElement('button');
        reloadBtn.innerHTML = '&#x21bb;';
        reloadBtn.title     = 'Reload image and plots';
        reloadBtn.addEventListener('click', () => {
            if (this._iframeEl) this._iframeEl.src = this._plotURL();
            if (this._imgEl?.src) {
                this._imgEl.src = this._imgEl.src.split('&_t=')[0]
                                + '&_t=' + Date.now();
            }
            this.frame.setStatus('Reloaded');
        });
        this.frame.appendButton($(reloadBtn));

        const homeBtn = document.createElement('button');
        homeBtn.innerHTML = '&#x1f3e0;';
        homeBtn.title     = 'Home';
        homeBtn.addEventListener('click', () => window.open('./'));
        this.frame.appendButton($(homeBtn));
        homeBtn.style.marginLeft = '1em';

        const docBtn = document.createElement('button');
        docBtn.innerHTML = '&#x2753;';
        docBtn.title     = 'Documents';
        docBtn.addEventListener('click', () => window.open('./slowdocs/index.html'));
        this.frame.appendButton($(docBtn));
    }
}
