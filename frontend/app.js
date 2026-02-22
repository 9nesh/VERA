/* VERA — Alpine.js application logic
 *
 * All API data is rendered via Alpine x-text bindings — never innerHTML.
 * async/await throughout; errors surface as toast notifications.
 */

function vera() {
  return {
    /* ── Navigation ─────────────────────────────────────────────────── */
    view: 'search',          // 'search' | 'project'

    /* ── Search ─────────────────────────────────────────────────────── */
    searchQuery: '',
    searchProcessType: '',
    searchResults: [],
    searchLoading: false,
    searched: false,

    /* ── Project detail ─────────────────────────────────────────────── */
    project: null,
    flags: null,             // null = no scan yet; {} = scan ran (even if empty)
    scanLoading: false,
    flagsExplainLoading: false,

    /* ── Solana attestation ──────────────────────────────────────────── */
    attestLoading: false,
    attestResult: null,

    /* ── Chat ───────────────────────────────────────────────────────── */
    chatOpen: false,
    chatTab: 'project',      // 'project' | 'all'
    chatMessages: [],
    chatInput: '',
    chatLoading: false,
    _chatMsgId: 0,

    /* ── Toast ──────────────────────────────────────────────────────── */
    toasts: [],
    _toastId: 0,

    /* ═══════════════════════════════════════════════════════════════════
       Init
    ═══════════════════════════════════════════════════════════════════ */
    init() {
      // Nothing to preload; state starts at search view.
    },

    /* ═══════════════════════════════════════════════════════════════════
       Toast helpers
    ═══════════════════════════════════════════════════════════════════ */
    toast(msg, type = 'error') {
      const id = ++this._toastId;
      this.toasts.push({ id, msg, type });
      setTimeout(() => this.removeToast(id), 5000);
    },

    removeToast(id) {
      this.toasts = this.toasts.filter(t => t.id !== id);
    },

    /* ═══════════════════════════════════════════════════════════════════
       API helpers
    ═══════════════════════════════════════════════════════════════════ */
    async _get(url) {
      const res = await fetch(url, { headers: { Accept: 'application/json' } });
      if (!res.ok) throw Object.assign(new Error(`HTTP ${res.status}`), { status: res.status });
      return res.json();
    },

    async _post(url, body) {
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw Object.assign(new Error(`HTTP ${res.status}`), { status: res.status });
      return res.json();
    },

    /* ═══════════════════════════════════════════════════════════════════
       Navigation
    ═══════════════════════════════════════════════════════════════════ */
    goHome() {
      this.view = 'search';
      this.project = null;
      this.flags = null;
      this.attestResult = null;
      this.chatOpen = false;
      this.chatMessages = [];
    },

    /* ═══════════════════════════════════════════════════════════════════
       Search
    ═══════════════════════════════════════════════════════════════════ */
    async search() {
      const q = this.searchQuery.trim();
      if (!q) return;
      this.searchLoading = true;
      this.searched = true;
      try {
        const url = `/api/projects/search?q=${encodeURIComponent(q)}`
          + (this.searchProcessType ? `&process_type=${this.searchProcessType}` : '');
        this.searchResults = await this._get(url);
      } catch (e) {
        this.toast(`Search failed: ${e.message}`);
        this.searchResults = [];
      } finally {
        this.searchLoading = false;
      }
    },

    /* ═══════════════════════════════════════════════════════════════════
       Project detail
    ═══════════════════════════════════════════════════════════════════ */
    async openProject(p) {
      this.flags = null;
      this.attestResult = null;
      this.chatMessages = [];
      this.chatOpen = false;
      this.view = 'project';
      try {
        this.project = await this._get(`/api/projects/${encodeURIComponent(p.id)}`);
        // Load any existing stored flags
        await this._loadStoredFlags();
      } catch (e) {
        this.toast(`Could not load project: ${e.message}`);
      }
    },

    async _loadStoredFlags(includeExplanation = false) {
      if (!this.project) return;
      try {
        const url = `/api/projects/${encodeURIComponent(this.project.id)}/flags${includeExplanation ? '?include_explanation=true' : ''}`;
        const rows = await this._get(url);
        if (rows && rows.length > 0) {
          this.flags = this._groupBySeverity(rows);
        }
      } catch (_) {
        // Silently ignore — project may simply have no stored flags yet
      }
    },

    /* ═══════════════════════════════════════════════════════════════════
       Scan
    ═══════════════════════════════════════════════════════════════════ */
    async runScan() {
      if (!this.project) return;
      this.scanLoading = true;
      this.flags = null;  // clear previous results first
      try {
        const result = await this._post(`/api/projects/${encodeURIComponent(this.project.id)}/scan`, {});
        // result is already grouped by severity from the backend
        this.flags = result;
        // If the project has already been attested, re-fetch to pick up the solana columns
        this.project = await this._get(`/api/projects/${encodeURIComponent(this.project.id)}`);
        const count = Object.values(result).reduce((n, arr) => n + (arr ? arr.length : 0), 0);
        this.toast(`Scan complete — ${count} flag(s) found.`, 'success');
        this.$nextTick(() => {
          document.getElementById('flags-panel')?.scrollIntoView({ behavior: 'smooth' });
        });
      } catch (e) {
        this.toast(`Scan failed: ${e.message}`);
      } finally {
        this.scanLoading = false;
      }
    },

    /* ═══════════════════════════════════════════════════════════════════
       Flags helper
    ═══════════════════════════════════════════════════════════════════ */
    _groupBySeverity(rows) {
      const out = {};
      for (const f of rows) {
        const sev = f.severity || 'low';
        if (!out[sev]) out[sev] = [];
        out[sev].push(f);
      }
      return out;
    },

    async loadFlagsWithExplanations() {
      if (!this.project || !this.flags) return;
      this.flagsExplainLoading = true;
      try {
        await this._loadStoredFlags(true);
        this.toast('AI explanations added.', 'success');
      } catch (e) {
        this.toast(`Could not load explanations: ${e.message}`);
      } finally {
        this.flagsExplainLoading = false;
      }
    },

    /* ═══════════════════════════════════════════════════════════════════
       Attestation
    ═══════════════════════════════════════════════════════════════════ */
    async attest() {
      if (!this.project) return;
      this.attestLoading = true;
      try {
        const result = await this._post(`/api/projects/${encodeURIComponent(this.project.id)}/attest`, {});
        if (result.status === 'not_implemented') {
          this.toast('Solana attestation is coming soon.', 'success');
        } else {
          this.attestResult = result;
          this.toast('Attested on Solana!', 'success');
          // Refresh project to update solana_tx_signature header badge
          this.project = await this._get(`/api/projects/${encodeURIComponent(this.project.id)}`);
        }
      } catch (e) {
        this.toast(`Attestation failed: ${e.message}`);
      } finally {
        this.attestLoading = false;
      }
    },

    truncateSig(sig) {
      if (!sig) return '';
      return sig.length > 24 ? `${sig.slice(0, 12)}…${sig.slice(-8)}` : sig;
    },

    /* ═══════════════════════════════════════════════════════════════════
       Chat
    ═══════════════════════════════════════════════════════════════════ */
    async sendChat() {
      const q = this.chatInput.trim();
      if (!q || this.chatLoading) return;
      this.chatInput = '';

      const userMsg = { id: ++this._chatMsgId, role: 'user', content: q };
      this.chatMessages.push(userMsg);
      this._scrollChat();

      this.chatLoading = true;
      try {
        let data;
        if (this.chatTab === 'project' && this.project) {
          data = await this._post(`/api/projects/${encodeURIComponent(this.project.id)}/chat`, { question: q });
        } else {
          data = await this._post('/api/chat', { question: q });
        }
        const answer = data.answer ?? 'No answer returned.';
        this.chatMessages.push({ id: ++this._chatMsgId, role: 'vera', content: answer });
      } catch (e) {
        let content;
        if (e.status === 404) {
          content = 'This feature is coming soon.';
        } else {
          content = `Sorry, something went wrong (${e.message}).`;
        }
        this.chatMessages.push({ id: ++this._chatMsgId, role: 'vera', content });
      } finally {
        this.chatLoading = false;
        this._scrollChat();
      }
    },

    _scrollChat() {
      this.$nextTick(() => {
        const el = this.$refs.chatThread;
        if (el) el.scrollTop = el.scrollHeight;
      });
    },

    /* ═══════════════════════════════════════════════════════════════════
       UI utilities
    ═══════════════════════════════════════════════════════════════════ */
    ptBadge(pt) {
      return {
        CE: 'bg-blue-100 text-blue-700',
        EA: 'bg-amber-100 text-amber-700',
        EIS: 'bg-red-100 text-red-700',
      }[pt] ?? 'bg-slate-100 text-slate-600';
    },
  };
}
