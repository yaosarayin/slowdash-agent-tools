// slowagent-app.mjs — SlowDash LLM Agent page entry point.
// Author: Yao Yin
//
// Layout:
//   ┌────────────── header (Frame, themed by project) ──────────────┐
//   │                                                                │
//   │  ┌──────────────────┐   ┌────────────────────────────────────┐│
//   │  │ latest webcam    │   │                                    ││
//   │  │ image            │   │   live plots                       ││
//   │  ├──────────────────┤   │   (slowplot iframe)                ││
//   │  │ prompt textarea  │   │                                    ││
//   │  │ + save button    │   │                                    ││
//   │  ├──────────────────┤   │                                    ││
//   │  │ example prompt   │   │                                    ││
//   │  │ (read-only)      │   │                                    ││
//   │  └──────────────────┘   └────────────────────────────────────┘│
//   └────────────────────────────────────────────────────────────────┘
//
// The slowtask is what actually drives the loop: this page just visualizes
// what the slowtask produces and lets the user edit the LLM prompt.

import { JG as $ } from '../slowjs/jagaimo/jagaimo.mjs';
import { Frame }  from '../slowjs/frame.mjs';
import { AgentAPI } from './slowagent-api.mjs';


const FRAME_LIST_REFRESH_MS = 5000;   // how often to re-list available frames
const FRAME_CYCLE_MS        = 1500;   // how long each frame is shown in the cycler


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
        this._frameListTimer = null;
        this._captionEl      = null;
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

        // Theme — same dance as slowcanvas: set the href, await `load`.
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

        // Left column ─────────────────────────────────────────────────────
        const left = document.createElement('div');
        left.className = 'sa-col sa-col-left';

        // Recent-frames cycler — runs entirely in JS.  Polls the source
        // dir every few seconds and rotates through whatever's there.
        const imgWrap = document.createElement('div');
        imgWrap.className = 'sa-section sa-image-wrap';
        const imgHdr = document.createElement('div');
        imgHdr.className = 'sa-section-hdr';
        imgHdr.textContent = 'Recent Frames';
        imgWrap.appendChild(imgHdr);

        const imgInner = document.createElement('div');
        imgInner.className = 'sa-image-inner';
        const img = document.createElement('img');
        img.className = 'sa-image';
        img.alt = 'Recent webcam frames';
        imgInner.appendChild(img);
        const placeholder = document.createElement('div');
        placeholder.className = 'sa-image-placeholder';
        placeholder.textContent = 'Waiting for the slowtask to capture frames…';
        imgInner.appendChild(placeholder);
        const caption = document.createElement('div');
        caption.className = 'sa-image-caption';
        imgInner.appendChild(caption);
        imgWrap.appendChild(imgInner);
        left.appendChild(imgWrap);
        this._imgEl         = img;
        this._imgPlaceholder = placeholder;
        this._captionEl     = caption;

        // Prompt textbox
        const promptWrap = document.createElement('div');
        promptWrap.className = 'sa-section';
        const promptHdr = document.createElement('div');
        promptHdr.className = 'sa-section-hdr';
        promptHdr.textContent = 'Extraction Prompt';
        promptWrap.appendChild(promptHdr);

        const promptArea = document.createElement('textarea');
        promptArea.className = 'sa-textarea sa-prompt';
        promptArea.spellcheck = false;
        promptArea.value = this.config?.llm?.prompt || '';
        promptArea.placeholder = 'Describe what the LLM should extract from each frame…';
        promptWrap.appendChild(promptArea);
        this._promptEl = promptArea;

        const promptRow = document.createElement('div');
        promptRow.className = 'sa-prompt-row';
        const saveBtn = document.createElement('button');
        saveBtn.className = 'sa-btn sa-btn-primary';
        saveBtn.textContent = 'Save Prompt';
        saveBtn.title = 'Write this prompt back to ' + this.configFile + '.  '
                      + 'The slowtask will reload within a couple of seconds.';
        const status = document.createElement('span');
        status.className = 'sa-prompt-status';
        promptRow.appendChild(saveBtn);
        promptRow.appendChild(status);
        promptWrap.appendChild(promptRow);
        this._statusEl = status;
        this._saveBtn  = saveBtn;
        left.appendChild(promptWrap);

        // Example prompt (read-only)
        const example = (this.config?.llm?.example_prompt || '').trim();
        if (example) {
            const exWrap = document.createElement('div');
            exWrap.className = 'sa-section';
            const exHdr = document.createElement('div');
            exHdr.className = 'sa-section-hdr';
            exHdr.textContent = 'Example Prompt (read-only)';
            exWrap.appendChild(exHdr);
            const exBody = document.createElement('textarea');
            exBody.className = 'sa-textarea sa-example';
            exBody.readOnly = true;
            exBody.value = example;
            exWrap.appendChild(exBody);
            this._exampleEl = exBody;
            left.appendChild(exWrap);
        }

        body.appendChild(left);

        // Splitter
        const splitter = document.createElement('div');
        splitter.className = 'sa-splitter';
        body.appendChild(splitter);
        this._wireSplitter(splitter, left);

        // Right column — slowplot iframe ─────────────────────────────────
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
        // Hide the embedded slowplot header so we don't get a doubled-up
        // SlowDash header inside the right pane.  Same trick as
        // slowdash-canvas's live preview iframe.
        iframe.addEventListener('load', () => {
            try {
                const doc = iframe.contentDocument;
                if (!doc) return;
                if (!doc.getElementById('sa-hide-header-style')) {
                    const style = doc.createElement('style');
                    style.id = 'sa-hide-header-style';
                    style.textContent =
                        '#sd-header{display:none!important}' +
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

    _plotURL() {
        // Look for a hand-crafted slowplot file with the same name root
        // (slowagent-Foo.json → slowplot-Foo.json).  This is the only
        // discovery rule; if a user wants different plotting, they edit
        // the slowplot file directly.
        const baseName = this.configFile.replace(/^slowagent-/, '').replace(/\.json$/, '');
        const slowplotFile = `slowplot-${baseName}.json`;
        return `slowplot.html?config=${encodeURIComponent(slowplotFile)}`;
    }

    _wireEvents() {
        this._saveBtn.addEventListener('click', async () => {
            const newPrompt = this._promptEl.value;
            this._setStatus('Saving…');
            try {
                await AgentAPI.updatePrompt(this.configFile, newPrompt);
                if (this.config?.llm) this.config.llm.prompt = newPrompt;
                this._setStatus('Saved — slowtask will pick it up shortly.');
            } catch (e) {
                this._setStatus(`Error: ${e.message}`, true);
            }
        });

        // Cmd/Ctrl-S in the prompt area saves too.
        this._promptEl.addEventListener('keydown', (e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === 's') {
                e.preventDefault();
                this._saveBtn.click();
            }
        });
    }

    _setStatus(text, isError = false) {
        if (!this._statusEl) return;
        this._statusEl.textContent  = text;
        this._statusEl.style.color  = isError ? '#c0392b' : '';
    }


    // ── Cycling-frames display ─────────────────────────────────────────── //

    _startFrameCycler() {
        // 1) periodically fetch the list of available frames
        const refreshList = async () => {
            try {
                const list = await AgentAPI.listSourceFrames(this.configFile);
                if (Array.isArray(list)) {
                    // Show in chronological order so the cycle reads as a
                    // forward-in-time animation; the API returns newest-first.
                    this._cycleFrames = [...list].sort((a, b) => a.mtime - b.mtime);
                    if (this._cycleIdx >= this._cycleFrames.length) {
                        this._cycleIdx = 0;
                    }
                }
            } catch (e) { /* keep previous list on error */ }
        };
        refreshList();
        this._frameListTimer = setInterval(refreshList, FRAME_LIST_REFRESH_MS);

        // 2) advance the displayed frame on its own faster cadence
        const tick = () => {
            const frames = this._cycleFrames;
            if (!frames || frames.length === 0) {
                this._imgEl.style.display = 'none';
                this._imgPlaceholder.style.display = '';
                if (this._captionEl) this._captionEl.textContent = '';
                return;
            }
            const i = this._cycleIdx % frames.length;
            const frame = frames[i];
            this._imgEl.src = AgentAPI.sourceFrameURL(this.configFile, frame.name);
            this._imgEl.style.display = '';
            this._imgPlaceholder.style.display = 'none';
            if (this._captionEl) {
                this._captionEl.textContent =
                    `${frame.name}  ·  frame ${i + 1}/${frames.length}`;
            }
            this._cycleIdx = (this._cycleIdx + 1) % frames.length;
        };
        tick();
        this._cycleTimer = setInterval(tick, FRAME_CYCLE_MS);
    }


    // ── Splitter (drag-to-resize the left column) ──────────────────────── //

    _wireSplitter(splitter, leftCol) {
        splitter.addEventListener('mousedown', (e) => {
            e.preventDefault();
            const startX = e.clientX;
            const startW = leftCol.getBoundingClientRect().width;
            const total  = leftCol.parentElement.getBoundingClientRect().width;

            // Veil so the iframe on the right can't swallow mousemove.
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
        // Reload — just refresh the iframe and image.
        const reloadBtn = document.createElement('button');
        reloadBtn.innerHTML = '&#x21bb;';
        reloadBtn.title     = 'Reload image and plots';
        reloadBtn.addEventListener('click', () => {
            if (this._iframeEl) this._iframeEl.src = this._plotURL();
            // Force the image to re-fetch.
            if (this._imgEl?.src) {
                this._imgEl.src = this._imgEl.src.split('&_t=')[0]
                                + '&_t=' + Date.now();
            }
            this.frame.setStatus('Reloaded');
        });
        this.frame.appendButton($(reloadBtn));

        // Home
        const homeBtn = document.createElement('button');
        homeBtn.innerHTML = '&#x1f3e0;';
        homeBtn.title     = 'Home';
        homeBtn.addEventListener('click', () => window.open('./'));
        this.frame.appendButton($(homeBtn));
        homeBtn.style.marginLeft = '1em';

        // Help
        const docBtn = document.createElement('button');
        docBtn.innerHTML = '&#x2753;';
        docBtn.title     = 'Documents';
        docBtn.addEventListener('click', () => window.open('./slowdocs/index.html'));
        this.frame.appendButton($(docBtn));
    }
}
