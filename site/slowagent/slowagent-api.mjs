// slowagent-api.mjs — REST client for the slowdash backend.
// Author: Yao Yin
//
// All endpoints below already exist in stock slowdash, except `/api/agent/*`
// which lives in `server/sd_agent.py` of this submodule.


export class AgentAPI {

    // ── Project ──────────────────────────────────────────────────────────── //

    static async getProjectConfig() {
        const resp = await fetch('./api/config');
        if (!resp.ok) throw new Error(`Config fetch failed: ${resp.status}`);
        return resp.json();
    }


    // ── Layout JSON ──────────────────────────────────────────────────────── //

    static async loadLayout(filename) {
        const resp = await fetch(`./api/config/file/${filename}`);
        if (!resp.ok) throw new Error(`Cannot load ${filename}: HTTP ${resp.status}`);
        return resp.json();
    }

    /** Patch the `llm.prompt` field of a layout file. */
    static async updatePrompt(filename, prompt) {
        const resp = await fetch(`./api/agent/prompt/${filename}`, {
            method:  'POST',
            headers: { 'Content-Type': 'application/json; charset=utf-8' },
            body:    JSON.stringify({ prompt }),
        });
        if (!resp.ok) {
            const text = await resp.text().catch(() => '');
            throw new Error(`Save prompt failed: HTTP ${resp.status} — ${text}`);
        }
        return resp.json();
    }


    // ── Data / Blob ──────────────────────────────────────────────────────── //

    // ── Cycling-frames source ─────────────────────────────────────────── //

    /** List image files currently available in the layout's source/display
     *  directory.  Returns [{name, mtime, size}, ...] newest-first. */
    static async listSourceFrames(layoutFile) {
        const r = await fetch(`./api/agent/frames/${encodeURIComponent(layoutFile)}`);
        if (!r.ok) return [];
        return r.json();
    }

    /** URL for a single frame from the source directory. */
    static sourceFrameURL(layoutFile, name) {
        return `./api/agent/frame/${encodeURIComponent(layoutFile)}`
             + `/${encodeURIComponent(name)}`;
    }


    /** URL of the latest blob on `channel`, with cache-buster.  Returns ''
     *  if the channel has no data yet.
     *
     *  Slowdash's data API returns the blob record as either:
     *    - a JSON-encoded string `{"mime": "...", "id": "..."}` (older path), or
     *    - an already-parsed object (current SQLite path returns it pre-decoded).
     *  panel-table.mjs handles both.  We do too. */
    static async getLatestBlobURL(channel) {
        const resp = await fetch(
            `./api/data/${encodeURIComponent(channel)}?length=3600&to=0`
        );
        if (!resp.ok) return '';
        const data = await resp.json();
        const block = data?.[channel];
        if (!block) return '';

        // Pick the most recent record.
        let last = null;
        if (Array.isArray(block.x)) {
            if (block.x.length === 0) return '';
            last = block.x[block.x.length - 1];
        } else if (block.x !== undefined && block.x !== null) {
            last = block.x;
        }
        if (last === null) return '';

        // Accept either a string (parse it) or an already-decoded object.
        let parsed = last;
        if (typeof last === 'string') {
            try { parsed = JSON.parse(last); }
            catch { return ''; }
        }
        if (!parsed || typeof parsed !== 'object' || !parsed.id) return '';

        const cacheBust = Date.now();
        return `./api/blob/${encodeURIComponent(channel)}`
             + `?id=${encodeURIComponent(parsed.id)}&_t=${cacheBust}`;
    }
}
