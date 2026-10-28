// Admin console (served at /static/admin.js, loaded by core/templates/admin.html).
// A classic script, loaded synchronously where the inline block used to be, so it
// runs at the same point in parsing and shares globals with tour.js exactly as before.
// Handlers stay delegated: dynamic values travel as data-* attributes (see "actions").
    const $ = id => document.getElementById(id);
    // Escapes for HTML text AND attribute values (either quote). Never feed its output
    // into an inline handler: dynamic values travel as data-* attributes and one
    // delegated listener below reads them (see "actions").
    const esc = s => (s ?? '').toString().replace(/[&<>"']/g, m =>
      ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
    const el = h => { const d = document.createElement('div'); d.innerHTML = h.trim(); return d.firstChild; };

    /* ---------------- accessible tabs (WCAG 2.1 AA keyboard pattern) ---------------- */
    const TABS = ['verify', 'docs', 'review', 'lib', 'audit'];
    const DEFAULT_TAB = 'verify';
    function showTab(name, focus = true, remember = true) {
      TABS.forEach(t => {
        const tab = $('tab-' + t), panel = $('panel-' + t);
        const on = t === name;
        tab.setAttribute('aria-selected', on);
        tab.tabIndex = on ? 0 : -1;
        panel.hidden = !on;
      });
      if (focus) $('tab-' + name).focus();
      if (remember) history.replaceState(null, '', location.pathname + location.search + '#' + name);
    }
    TABS.forEach((t, i) => {
      $('tab-' + t).addEventListener('click', () => showTab(t));
      $('tab-' + t).addEventListener('keydown', e => {
        const k = e.key;
        if (!['ArrowRight', 'ArrowLeft', 'Home', 'End'].includes(k)) return;
        e.preventDefault();
        const n = k === 'Home' ? 0 : k === 'End' ? TABS.length - 1
                : k === 'ArrowRight' ? (i + 1) % TABS.length : (i - 1 + TABS.length) % TABS.length;
        showTab(TABS[n]);
      });
    });

    /* ---------------- actions ----------------
       Every button rendered from data carries its arguments in data-* attributes and
       is wired here, once. No handler is ever assembled from a string, so a quote in a
       document id, clause label or scenario name cannot break out into script. */
    document.addEventListener('click', e => {
      const b = e.target.closest('[data-act]');
      if (!b || b.disabled) return;
      const d = b.dataset;
      switch (d.act) {
        case 'xray':    openXray(d.doc); break;
        case 'compare': openCompare(d.doc, d.page ? Number(d.page) : 1); break;
        case 'source':  viewSource(d.doc || '', d.clause || '', d.rule || ''); break;
        case 'break':   tryBreak(b); break;
        case 'draft':   draftScenario(b, d.name || ''); break;
        case 'reread':  ingest(); break;
        case 'reread-go':     ingestConfirmed(); break;
        case 'reread-cancel': $('rereadMsg').textContent = ''; $('rereadBtn').focus(); break;
      }
    });

    /* ---------------- documents + coverage ---------------- */
    let COVERAGE = [], SHOWCASE = null;
    async function loadDocs() {
      const res = await fetch('/admin/coverage').then(r => r.json());
      COVERAGE = res.documents;
      SHOWCASE = res.showcase || null;   // D4: the page Compare opens on from one click
      $('cnt-docs').textContent = COVERAGE.length;
      const byDept = {};
      COVERAGE.forEach(d => (byDept[d.department || 'unassigned'] ||= []).push(d));
      const order = Object.keys(byDept).sort((a, b) =>
        a === 'citywide' ? 1 : b === 'citywide' ? -1 : a.localeCompare(b));
      $('docs').innerHTML = order.map(dept => `
        <section aria-label="${esc(dept)} documents" class="dept">
          <h3 class="dept-head">${esc(dept)} <span class="muted">${byDept[dept].length} document(s)</span></h3>
          ${byDept[dept].map(docCard).join('')}
        </section>`).join('');
    }

    // Extraction scorecard (OCR_TICKETS.md OCR-3): how well the machine READ the
    // document — parse tier, what kinds of clauses came out, which pages yielded
    // nothing, which pages the layout-model rescue saved, and the sha binding.
    // Shared by the scorecard rows and the upload result card (OCR-6) so a freshly
    // uploaded document announces its tier with the same badge it will wear in the list.
    const PARSE_TIER = {docling: ['tier-good', 'docling'],
                        sidecar: ['tier-amber', 'sidecar'],
                        'raw-text-fallback': ['tier-orange', 'raw text fallback'],
                        empty: ['tier-bad', 'empty']};
    const parseTier = src => PARSE_TIER[src] || ['tier-amber', src || 'unknown'];

    // Text origin (D1): what the parse tier cannot say — whether the text docling
    // read was a digital layer or an invisible OCR layer stamped over a scan.
    const ORIGIN = {'ocr-layer': ['origin-ocr', "OCR'd scan",
                      'Scanned paper with an invisible OCR text layer: the text can contain misread characters; check the page image'],
                    digital: ['origin-digital', 'digital text', 'Born-digital text layer'],
                    'image-only': ['origin-ocr', 'image only, no text', 'A scan with no text layer at all'],
                    mixed: ['origin-ocr', 'mixed scan/digital', 'Some pages are scans with an OCR layer, some are digital']};
    function originBadge(d) {
      const o = ORIGIN[d.text_origin];
      if (!o) return '';
      const prod = d.producer ? ` — ${d.producer}` : '';
      return `<span class="score-origin ${o[0]}" title="${esc(o[2] + prod)}">${esc(o[1])}</span>`;
    }
    function verifiedBadge(d) {
      const pages = d.verified_pages || [];
      if (!pages.length) return '';
      const disputed = pages.reduce((n, p) => n + (p.disputed || 0), 0);
      const list = pages.map(p => `p.${p.page} (${p.disputed} disputed)`).join(', ');
      return `<span class="score-verified" title="Numeric table cells re-read by a second OCR engine and checked against their column arithmetic: ${esc(list)}">${disputed} cell${disputed === 1 ? '' : 's'} disputed on ${pages.length} verified page${pages.length === 1 ? '' : 's'}</span>`;
    }

    function scorecard(d) {
      const [cls, word] = parseTier(d.parse_source);
      const kinds = d.kinds || {};
      const rows = kinds['table-row'] || 0;
      const rec = (d.recovered_pages || []).length;
      const emp = (d.pages_empty || []).length;
      const low = d.low_confidence_pages || [];
      const s = n => n === 1 ? '' : 's';
      return `<p class="score">
        <span class="score-tier ${cls}" title="Parse tier: how the text was extracted from the PDF">${esc(word)}</span>
        ${originBadge(d)}${verifiedBadge(d)}
        <span class="score-stat">${d.clauses || 0} clause${s(d.clauses || 0)}</span>
        <span class="score-stat">${rows} table row${s(rows)}</span>
        ${rec ? `<span class="score-badge score-recovered" title="docling's layout model classified the page as a Picture; the spans were regrouped into rows from raw geometry">${rec} page${s(rec)} recovered from layout misclassification</span>` : ''}
        ${emp ? `<span class="score-badge score-empty" title="Nothing was extracted from page${s(emp)} ${esc((d.pages_empty || []).join(', '))}">${emp} empty page${s(emp)}</span>` : ''}
        ${low.length ? `<span class="score-badge score-lowconf" title="docling's OCR scored page${s(low.length)} ${esc(low.join(', '))} below its 'excellent' grade — the text read from there may contain misread characters; check it in the X-ray">low OCR confidence on ${low.length} page${s(low.length)}</span>` : ''}
        ${d.index_error ? `<span class="score-badge score-fail" title="${esc(d.index_error)} — catalogued but absent from search">indexing failed</span>` : ''}
        ${d.pdf_sha256_short ? `<span class="score-sha" title="SHA-256 binding of the ingested PDF — provenance anchor">${esc(d.pdf_sha256_short)}</span>` : ''}
      </p>`;
    }

    function docCard(d) {
      const eff = d.effective_start ? `${esc(d.effective_start)} to ${esc(d.effective_end || 'open')}` : null;
      const chips = [['department', d.department], ['unit', d.bargaining_unit],
                     ['type', d.doc_type], ['version', d.mou_version], ['effective', eff]]
        .filter(([, v]) => v)
        .map(([k, v], i) => `<span class="chip lvl${Math.min(i, 2)}"><b>${k}</b> ${esc(v)}</span>`)
        .join('');
      const tot = d.clauses || 0;
      const pct = n => tot ? (n ? Math.max(2, Math.round(n / tot * 100)) : 0) : 0;
      const bar = tot ? `
        <div class="cov" role="img" aria-label="Of ${tot} clauses: ${d.live} live, ${d.pending} awaiting review, ${d.blocked} blocked on missing data, ${d.narrative} narrative">
          <span class="cov-live" style="width:${pct(d.live)}%"></span>
          <span class="cov-pending" style="width:${pct(d.pending)}%"></span>
          <span class="cov-blocked" style="width:${pct(d.blocked)}%"></span>
          <span class="cov-narrative" style="width:${pct(d.narrative)}%"></span>
        </div>
        <ul class="cov-key">
          <li><i class="k-live"></i>${d.live} live</li>
          <li><i class="k-pending"></i>${d.pending} awaiting review</li>
          <li><i class="k-blocked"></i>${d.blocked} blocked on data</li>
          <li><i class="k-narrative"></i>${d.narrative} narrative</li>
        </ul>` : '';
      const warn = !tot
        ? `<p class="flagline">Nothing could be read from this document — it is not searchable.</p>`
        : d.parse_source === 'raw-text-fallback'
        ? `<p class="notice">Read as plain text: this document's layout could not be analysed, so
             citations open the right <em>page</em> rather than highlighting the exact clause.</p>` : '';
      const sup = d.supersedes ? `<p class="muted">Replaces <code>${esc(d.supersedes.doc_id)}</code>
        §${esc((d.supersedes.clauses || []).join(', '))}</p>` : '';
      // The title links to the contract itself. The rendered page proves one citation;
      // a reader checking a document as a whole wants the document, not a picture of it.
      return `<article class="card">
        <h4><a href="/doc/${encodeURIComponent(d.doc_id)}/file" target="_blank"
               rel="noopener">${esc(d.title || d.doc_id)}<span class="ext" aria-hidden="true"> &#8599;</span><span class="sr-only"> (opens the PDF in a new tab)</span></a></h4>
        <p class="mono id">${esc(d.doc_id)} · ${tot} clauses
          <button type="button" class="ghost small" data-act="xray" data-doc="${esc(d.doc_id)}"
                  title="See every clause the machine extracted, boxed on the page it came from">
            X-ray</button>
          <button type="button" class="ghost small" data-act="compare" data-doc="${esc(d.doc_id)}"
                  title="Page image beside its extracted text — hover either side to check the extraction by eye">
            Compare</button>${SHOWCASE && SHOWCASE.doc === d.doc_id ? `
          <button type="button" class="ghost small btn-showcase" data-act="compare" data-doc="${esc(d.doc_id)}" data-page="${Number(SHOWCASE.page)}"
                  title="${esc(SHOWCASE.label)}: open Compare on the page where a second OCR read disagrees with what was stored">
            Showcase: p.${Number(SHOWCASE.page)}</button>` : ''}</p>
        ${scorecard(d)}
        ${d.extracted_title && d.extracted_title !== d.title
          ? `<p class="muted">Cover page reads “${esc(d.extracted_title)}”.</p>` : ''}
        <p class="chips">${chips}</p>
        ${sup}${warn}${bar}
        ${d.summary ? `<p class="summary">${esc(d.summary)}</p>` : ''}
        ${(d.tags || []).length ? `<p class="chips">${d.tags.map(t => `<span class="tag">${esc(t)}</span>`).join('')}</p>` : ''}
      </article>`;
    }

    /* ---------------- rules ---------------- */
    let PROPOSED = [], RATIFIED = new Set(), RATIFIED_RULES = [], ERRORS = {};
    async function loadRules() {
      const res = await fetch('/admin/proposed').then(r => r.json());
      PROPOSED = res.rules || [];
      RATIFIED = new Set(res.ratified_ids || []);
      RATIFIED_RULES = res.ratified_rules || [];
      try { ERRORS = (await fetch('/admin/validate').then(r => r.json())).errors || {}; }
      catch (e) { ERRORS = {}; }
      renderReview();
      renderLibrary();
      renderGaps(res.needs_data);
      loadSkeptic();   // agentic (G1): the stored skeptic reviews, no model call
    }

    // The DECLARED title (from case.yaml) wins in the rule library and review queue: it is
    // the clean, human-filed name. The auto-extracted cover title is great on a clean PDF
    // but on an OCR'd scan it can grab a stray line (a date fragment) — which surfaced as
    // "December 31, 2028" standing in for a document name.
    const docTitle = id => { const d = COVERAGE.find(x => x.doc_id === id) || {};
      return d.declared_title || d.title || id; };

    const ruleBody = r => `<p class="muted">${esc(r.human_readable || '')}</p>
      <p class="expr"><code>when ${esc(r.when)}</code>
      ${r.compute ? `<code>= ${esc(r.compute)}</code>` : ''}
      ${r.set ? `<code>set ${esc(JSON.stringify(r.set))}</code>` : ''}</p>`;

    function renderReview() {
      const pending = PROPOSED.filter(r => !RATIFIED.has(r.id));
      $('cnt-review').textContent = pending.length;
      if (!pending.length) {
        $('review').innerHTML = PROPOSED.length
          ? `<p class="empty">Nothing awaiting review — every rule drawn from these contracts has
             been dealt with.</p>`
          : `<p class="empty">No rules drafted yet. Go to <strong>Verification</strong> and press
             <em>Draft the rules for this scenario</em> on a pending paystub.</p>`;
        $('reviewBtns').hidden = true;
        return;
      }
      const byDoc = {};
      pending.forEach(r => ((byDoc[(r.citation || {}).doc_id || r._doc_id || 'unknown'] ||= []).push(r)));
      $('review').innerHTML = Object.entries(byDoc).map(([doc, rules]) => `
        <section aria-label="Rules from ${esc(docTitle(doc))}" class="dept">
          <h3 class="dept-head">${esc(docTitle(doc))} <span class="muted">${rules.length} to review</span></h3>
          ${rules.map(reviewCard).join('')}
        </section>`).join('');
      $('reviewBtns').hidden = false;
    }

    function reviewCard(r) {
      const cit = r.citation || {};
      const errs = ERRORS[r.id], bad = !!errs;
      return `<article class="card rule ${bad ? 'invalid' : ''}">
        <div class="rule-head">
          <input type="checkbox" class="ruleChk" id="chk-${esc(r.id)}" data-id="${esc(r.id)}"
                 ${bad ? 'disabled' : 'checked'}>
          <label for="chk-${esc(r.id)}"><strong>${esc(r.topic || r.id)}</strong></label>
          <span class="badge-type">${esc(r.result_type || 'currency')}</span>
          <span class="muted">§${esc(cit.clause || '?')}</span>
          ${bad ? `<span class="bad">Cannot be approved</span>`
                : `<span class="ok" title="Parses and uses only real fields. The known-answer check runs when you approve.">Well-formed</span>`}
          <button type="button" class="ghost small"
            data-act="source" data-doc="${esc(cit.doc_id || r._doc_id || '')}" data-clause="${esc(cit.clause || '')}" data-rule="${esc(r.id)}">
            View source</button>
          ${bad ? '' : `<button type="button" class="ghost small" data-id="${esc(r.id)}"
            data-act="break">Try to break it</button>`}
        </div>
        ${ruleBody(r)}
        ${bad ? `<p class="flagline">${errs.map(esc).join('<br>')}</p>` : ''}
        <div class="break-result" hidden></div>
      </article>`;
    }

    /* ---------------- try to break it (gate chunk, DEMO_TICKETS.md E3) ----------------
       Mutate ONE rule in memory (multiplier off by a step, guard dropped or inverted,
       threshold nudged, clause re-homed to another contract) and run every known answer
       against each mutant. No model call; nothing is written; the attempt is ledgered.
       A survivor is a part of the rule no known answer tests — which is the honest
       answer to "how tightly does one known answer pin this rule down?". */
    const fmtAmount = (v, t) => v == null ? '—'
      : (t === 'currency' ? '$' + Number(v).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})
         : `${Number(v)} ${t}`);

    async function tryBreak(btn) {
      const id = btn.dataset.id;
      const panel = btn.closest('article').querySelector('.break-result');
      btn.disabled = true; btn.textContent = 'Trying…';
      let res;
      try {
        res = await fetch('/admin/try_break', {method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({rule_id: id})}).then(r => r.json());
      } catch (e) { res = {error: String(e)}; }
      btn.disabled = false; btn.textContent = 'Try to break it';
      panel.hidden = false;
      if (!res || res.error) { panel.innerHTML = `<p class="flagline">${esc(res && res.error || 'request failed')}</p>`; return; }
      const t = res.result_type || 'currency';
      const head = res.verdict === 'not actually tested'
        ? `<span class="warn-badge">Not actually tested</span> <span class="muted">No known answer catches any
             deliberate error in this rule${(res.fires_in || []).length ? '' : ' — it fires in no passing known answer'}.</span>`
        : `<strong>${res.caught} of ${res.total} deliberate errors caught</strong>
           <span class="muted">· ${esc(res.verdict)} · no model call, nothing written, attempt recorded in the ledger</span>`;
      panel.innerHTML = `<p>${head}</p><div class="xtable-wrap">
        <table class="lines"><thead><tr><th>Deliberate error</th><th>Result</th><th>Known answer</th><th>Expected</th><th>Computed</th></tr></thead>
        <tbody>${res.mutants.map(m => m.caught
          ? `<tr><td>${esc(m.label)}</td><td class="ok">Caught</td><td>${esc(m.caught_by.scenario)}</td>
               <td>${esc(fmtAmount(m.caught_by.expected, t))}</td>
               <td>${m.caught_by.actual == null ? `<span class="muted">refused (${esc(m.caught_by.status)})</span>` : esc(fmtAmount(m.caught_by.actual, t))}</td></tr>`
          : `<tr><td>${esc(m.label)}</td><td class="warn-text">Survived</td>
               <td colspan="3" class="warn-text">No known answer tests this part of the rule</td></tr>`).join('')}
        </tbody></table></div>`;
      loadLedger();
    }

    const setAll = v => document.querySelectorAll('.ruleChk').forEach(c => { if (!c.disabled) c.checked = v; });

    /* ---------------- tour-and-gate-demo (I15): load the seeded wrong draft ----------------
       POST /admin/demo_draft/load merges cases/<case>/rules/demo_wrong_draft.json into the
       review queue by id (idempotent). The card then renders like any other draft; approving
       it goes through the ordinary gate, which refuses it. window.loadDemoDraft is what the
       tour drives. */
    async function loadDemoDraft(btn) {
      const note = $('demoDraftMsg');
      if (btn) { btn.disabled = true; btn.textContent = 'Loading…'; }
      let res;
      try {
        res = await fetch('/admin/demo_draft/load', {method: 'POST'}).then(r => r.json());
      } catch (e) { res = {error: String(e)}; }
      if (btn) { btn.disabled = false; btn.textContent = 'Load the demo draft'; }
      if (!res || res.error) { if (note) note.textContent = res && res.error || 'request failed'; return res; }
      if (note) note.textContent = res.already_loaded
        ? 'The demo draft is already in the queue below.'
        : 'Loaded: a deliberately wrong draft (double time on the $640.80 rule). Try approving it.';
      await loadRules();
      loadLedger();
      return res;
    }
    window.loadDemoDraft = loadDemoDraft;
    /* ---------------- /tour-and-gate-demo ---------------- */

    async function ratify() {
      const ids = [...document.querySelectorAll('.ruleChk')].filter(c => c.checked).map(c => c.dataset.id);
      if (!ids.length) { msg('reviewMsg', 'bad', 'Select at least one rule to approve.'); return; }
      const res = await fetch('/admin/ratify', {method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({approver: approverName(), rule_ids: ids})}).then(r => r.json());

      if (res.rejected) {
        msg('reviewMsg', 'bad', `<strong>Nothing was approved.</strong> These rules cannot run
          against your employee data:<br>` + Object.entries(res.rejected)
            .map(([id, e]) => `<code>${esc(id)}</code>: ${e.map(esc).join('; ')}`).join('<br>'));
      } else if (res.golden_failed) {
        const g = res.golden_failed;
        msg('reviewMsg', 'bad', `<strong>Nothing was approved — the known-answer check failed.</strong><br>
          “${esc(g.scenario)}” must come to <strong>${esc(g.expected)}</strong>, but these rules
          produce <strong>${esc(g.actual)}</strong>.
          ${g.per_subject ? `<br><span class="muted">${Object.entries(g.per_subject)
            .map(([k, v]) => `${esc(k)}: ${esc(v)}`).join(' · ')}</span>` : ''}
          ${g.error ? '<br>' + esc(g.error) : ''}
          <br>The rules are well-formed but do not reproduce the verified answer. Compare the
          flagged rule against its clause.`);
      } else if (res.uncovered || res.orphaned) {
        const ids = [...(res.uncovered || []), ...(res.orphaned || [])];
        msg('reviewMsg', 'bad', `<strong>Nothing was approved — no known answer exercises:</strong>
          ${ids.map(i => `<code>${esc(i)}</code>`).join(', ')}.
          ${(res.orphaned || []).length ? `(${res.orphaned.length} of these are live rules this approval would leave proven by nothing.) ` : ''}
          Untick them or add a known answer that uses them. A rule nothing proves cannot go live.`);
      } else if (res.ratified && res.ratified.length) {
        const pb = res.proved_by || {};
        msg('reviewMsg', 'ok', `<strong>Approved ${res.ratified.length} rule(s).</strong>
          Each fired in a known answer that reproduces:<br>${res.ratified.map(i =>
            `<code>${esc(i)}</code> → ${esc((pb[i] || []).join('; ') || '?')}`).join('<br>')}
          <br>They are live — see the <strong>Rule library</strong>.`);
      } else {
        msg('reviewMsg', 'bad', esc(res.warning || res.error || 'Nothing was approved.'));
      }
      loadRules(); loadVerify(); loadDocs(); loadLedger();
    }

    const msg = (where, kind, html) =>
      $(where).innerHTML = `<p class="${kind === 'ok' ? 'notice ok-notice' : 'flagline'}">${html}</p>`;

    /* ---------------- rule library ---------------- */
    async function renderLibrary() {
      // Render from the ratified LIBRARY, never from the proposed queue: the queue is
      // ephemeral (cleared on reseed), and filtering it by ratified ids showed a count
      // of 4 above an empty page.
      const live = RATIFIED_RULES;
      $('cnt-lib').textContent = live.length;
      if (!live.length) {
        $('library').innerHTML = `<p class="empty">No rules are live yet. Approve rules on the
          <strong>Review queue</strong> and they will appear here.</p>`;
        return;
      }
      let unverified = new Set(), provedBy = {};
      try {
        const v = await fetch('/admin/verification').then(r => r.json());
        unverified = new Set((v.unverified || []).map(u => u.id));
        provedBy = v.proved_by || {};
      } catch (e) {}
      await loadApprovals();   // ledger chunk (F2): approval line on each card
      const byDoc = {};
      live.forEach(r => ((byDoc[(r.citation || {}).doc_id || '?'] ||= []).push(r)));
      $('library').innerHTML = Object.entries(byDoc).map(([doc, rules]) => `
        <section aria-label="Live rules from ${esc(docTitle(doc))}" class="dept">
          <h3 class="dept-head">${esc(docTitle(doc))} <span class="muted">${rules.length} live</span></h3>
          ${rules.map(r => {
            const cit = r.citation || {};
            const proved = provedBy[r.id] || [];
            return `<article class="card rule ratified">
              <div class="rule-head">
                <strong>${esc(r.topic || r.id)}</strong>
                <span class="badge-type">${esc(r.result_type || 'currency')}</span>
                <span class="muted">§${esc(cit.clause || '?')}</span>
                <span class="badge-ratified">Live</span>
                ${unverified.has(r.id) ? `<span class="warn-badge">No known answer exercises this rule</span>` : ''}
                <button type="button" class="ghost small"
                  data-act="source" data-doc="${esc(cit.doc_id || '')}" data-clause="${esc(cit.clause || '')}" data-rule="${esc(r.id)}">
                  View source</button>
                <button type="button" class="ghost small" data-id="${esc(r.id)}"
                  data-act="break">Try to break it</button>
              </div>
              ${ruleBody(r)}
              ${proved.length ? `<p class="muted">Proved by: ${proved.map(esc).join('; ')} — checked at that answer's inputs only.</p>` : ''}
              ${approvalLine(r)}
              <p class="muted mono id">${esc(r.id)}</p>
              <div class="break-result" hidden></div>
            </article>`;
          }).join('')}
        </section>`).join('');
    }

    /* ---------------- verification ---------------- */
    // A known answer reads the way chat prints it: money as $640.80, other units named.
    // Mirrors app.js fmtVal; anything non-numeric (a date, text) is shown as given.
    function fmtVal(v, type) {
      if (v === null || v === undefined || v === '') return '—';
      const n = Number(v);
      if (!Number.isFinite(n)) return String(v);
      const unit = (one, many) => `${n} ${n === 1 ? one : many}`;
      switch (type) {
        case 'currency': return '$' + n.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
        case 'days':   return unit('day', 'days');
        case 'shifts': return unit('shift', 'shifts');
        case 'hours':  return unit('hour', 'hours');
        default:       return String(v);
      }
    }
    async function loadVerify() {
      const v = await fetch('/admin/verification').then(r => r.json());
      $('cnt-verify').textContent = (v.goldens || []).length;
      // Three states, not two. "Pending" means no live rule covers the scenario yet, so
      // Kenny refuses to cost it — unproven, but not wrong, and not a blocker.
      const CARD = {pass: 'ratified', fail: 'invalid', pending: 'warn-card'};
      const WORD = {pass: 'Reproduced', fail: 'Did not reproduce', pending: 'Not covered yet'};
      const TONE = {pass: 'ok', fail: 'bad', pending: 'warn-text'};
      const rows = (v.goldens || []).map(g => `
        <article class="card ${CARD[g.status] || 'invalid'}">
          <div class="rule-head">
            <strong>${esc(g.name)}</strong>
            <span class="badge-type">${esc(g.result_type)}</span>
            <span class="${TONE[g.status] || 'bad'}">${WORD[g.status] || 'Did not reproduce'}</span>
          </div>
          <p class="muted">Known answer <strong>${esc(fmtVal(g.expected, g.result_type))}</strong> · Kenny computes
            <strong>${esc(fmtVal(g.actual, g.result_type))}</strong>${
              g.status !== 'pending' && g.error ? ' · ' + esc(g.error) : ''}</p>
          ${g.source ? `<p class="muted">Source: ${esc(g.source)}</p>` : ''}
          ${(g.fired || []).length ? `<p class="muted">Exercises: ${g.fired.map(esc).join('; ')}</p>` : ''}
          ${g.note ? `<p class="muted">${esc(g.note)}</p>` : ''}
          ${g.per_subject ? `<p class="muted">${Object.entries(g.per_subject)
            .map(([k, x]) => `${esc(k)}: ${esc(fmtVal(x, g.result_type))}`).join(' · ')}</p>` : ''}
          ${(g.needs_docs || []).length ? `<p class="muted">Needs: ${g.needs_docs.map(d =>
            `${esc(d.title)} <span class="${d.ingested ? 'ok' : 'bad'}">${d.ingested ? '✓ ingested' : '✗ not ingested'}</span>`).join(' · ')}</p>` : ''}
          ${g.status !== 'pass' ? (g.ready
            ? `<button type="button" class="ghost" data-act="draft" data-name="${esc(g.name)}">
                 Draft the rules for this scenario</button>`
            : `<p class="warn-text">Ingest the documents above first (Documents tab), then draft.</p>`) : ''}
        </article>`).join('');
      const un = v.unverified || [];
      // Three footers, each literally what was computed (E7): nothing live means nothing
      // proven; a live rule that fired in no passing known answer is named; otherwise every
      // live rule fired in a reproducing known answer — at that answer's inputs only.
      const footer = !v.rule_count
        ? `<p class="notice">No rules are live. Nothing is proven yet.</p>`
        : un.length ? `<div class="card warn-card">
            <h4>Live rules that fire in no passing known answer (${un.length} of ${v.rule_count})</h4>
            <p class="muted">Approved and running, but no known answer proves they are right.
              Add a known answer that uses them.</p>
            ${un.map(u => `<p class="muted">${esc(u.topic || u.id)} · ${esc(u.result_type)} · §${esc(u.clause)}</p>`).join('')}
          </div>`
        : `<p class="notice ok-notice">All ${v.rule_count} live rules fired in at least one known answer
            that reproduces. Each is checked at that answer's inputs only.</p>`;
      $('verify').innerHTML = (rows || `<p class="empty">No known answers defined. Add at least one
          per bargaining unit — a rule no known answer exercises is unproven.</p>`)
        + footer + `<p class="muted" id="mutationReport"></p>`;
      loadMutationReport();
    }

    // Foot of Verification: the mutation check over every live rule (gate chunk, E3).
    async function loadMutationReport() {
      const el = $('mutationReport');
      if (!el) return;
      try {
        const m = await fetch('/admin/mutation_report').then(r => r.json());
        if (!m.total) { el.textContent = ''; return; }
        const surv = (m.survivors || []).map(s => `${esc(s.rule_id.split(':').pop())}: ${esc(s.label)}`);
        el.innerHTML = `Try to break it: <strong>${m.caught} of ${m.total}</strong> deliberate errors across the
          live rules are caught by the known answers` + (surv.length ? `; survivors: ${surv.join(' · ')}.` : '.')
          + ` Press <em>Try to break it</em> on any rule in the Rule library to see its table.`;
      } catch (e) { el.textContent = ''; }
    }

    // Draft ONLY the rules this scenario needs — retrieve its pay-branch clauses, draft
    // the handful, verify against the paystub. Review collapses from a whole MOU to ~5.
    async function draftScenario(btn, name) {
      btn.disabled = true; btn.textContent = 'Drafting the rules this scenario needs…';
      const res = await fetch('/admin/draft_scenario', {method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({scenario: name})}).then(r => r.json());
      if (res.error) { btn.textContent = res.error; return; }
      const v = res.verify || {};
      const n = (res.drafted || []).length;
      const clauses = (res.considered_clauses || []).join(', ');
      btn.outerHTML = `<p class="muted">Drafted <strong>${n}</strong> rule(s) from ${esc(clauses)} —
        they now sit in the <strong>Review queue</strong>. Known-answer check:
        <strong class="${v.status === 'pass' ? 'ok' : 'bad'}">${esc(v.status)}</strong>
        (expected ${esc(v.expected)}, computed ${esc(v.actual ?? '—')}).
        ${v.status === 'pass' ? 'Review each against its clause, then approve.'
          : 'Close, but not exact — open the rules and compare against the clause.'}</p>`;
      loadRules();  // refresh the review queue
    }

    /* ---------------- data gaps ---------------- */
    // Part two of the Rule library: clauses Kenny DECLINED to model. Not a failure — the
    // contract pays on a dimension the classification table doesn't have (an `event` like
    // court time or a holdover; an `assignment` like Detective Bureau). Refusing beats
    // silently paying every classification; the card names the dimension that unlocks it.
    async function renderGaps(needsData) {
      const res = await fetch('/admin/gaps').then(r => r.json());
      const gaps = res.gaps || [];
      const narrative = (needsData || []).filter(n => n.category === 'narrative');
      if (!gaps.length && !narrative.length) { $('gaps').innerHTML = ''; return; }
      $('gaps').innerHTML = `<h3 class="dept-head" style="margin-top:18px">Declined to model
          <span class="muted">${gaps.reduce((n, g) => n + g.unlocks.length, 0)} clause(s)</span></h3>`
        + (gaps.length ? gaps.map(g => `
        <article class="card ${g.field === 'other' ? '' : 'gap-actionable'}">
          <h4>${g.field === 'other'
            ? `Needs a decision — ${g.unlocks.length} clause(s) with no single unlocking dimension`
            : `Needs a <code>${esc(g.field)}</code> dimension on the classification table`}</h4>
          <p class="muted">${g.field === 'other'
            ? 'A person has to say what fact would settle these.'
            : `Add it (new classification rows) and ${g.unlocks.length} clause(s) become costable:`}</p>
          <ul class="unlocks">
            ${g.unlocks.map(u => `<li><strong>§${esc(u.clause)}</strong>
              <span class="muted">${esc(u.title || u.doc_id)}</span><br>
              <span class="muted">${esc(u.reason || '')}</span></li>`).join('')}
          </ul>
        </article>`).join('') : '')
        + (narrative.length ? `<details class="card">
            <summary><strong>Answered by quoting, not computing (${narrative.length})</strong></summary>
            <p class="muted">Narrative or procedural text — recognition, management rights, conduct.
              Chat quotes these clauses directly. No rule will ever exist for them, correctly.</p>
            ${narrative.slice(0, 40).map(n =>
              `<p class="muted">§${esc(n.clause)} — ${esc(n.reason || '')}</p>`).join('')}
          </details>` : '');
    }

    /* ---------------- agentic: skeptic reviews (G1/G6) ---------------- */
    // Rendered from GET /admin/skeptic (an index) and GET /admin/skeptic/<rule_id> (the
    // stored artifact). No inline handlers: one delegated listener reads data-rule.
    let SKEPTIC = {};
    async function loadSkeptic() {
      try { SKEPTIC = (await fetch('/admin/skeptic').then(r => r.json())).reviews || {}; }
      catch (e) { SKEPTIC = {}; }
      const ids = Object.keys(SKEPTIC);
      $('skeptic-count').textContent = ids.length ? `${ids.length} rule(s) reviewed` : 'none yet';
      const list = $('skeptic-list');
      list.textContent = '';
      if (!ids.length) {
        list.appendChild(el(`<p class="empty">No skeptic review stored yet. Bake one offline:
          <code>env -u ANTHROPIC_API_KEY .venv/bin/python scripts/skeptic.py start --rule &lt;rule_id&gt;
          --producer claude-code-session</code></p>`));
        return;
      }
      ids.forEach(id => {
        const s = SKEPTIC[id];
        const rule = RATIFIED_RULES.find(r => r.id === id) || PROPOSED.find(r => r.id === id) || {};
        const card = el(`<article class="card rule ${s.fresh ? '' : 'warn-card'}">
          <div class="rule-head">
            <strong></strong>
            <span class="badge-type"></span>
            <span class="muted mono id"></span>
            <button type="button" class="ghost small skeptic-open"></button>
          </div>
          <p class="muted skeptic-prov"></p>
        </article>`);
        card.querySelector('strong').textContent = rule.topic || id;
        card.querySelector('.badge-type').textContent = s.verdict || '?';
        card.querySelector('.id').textContent = id;
        const btn = card.querySelector('button');
        btn.dataset.rule = id;
        btn.textContent = s.fresh === false
          ? `Skeptic: stale (${(s.stale_reasons || []).join('; ')})`
          : `Skeptic: ${s.warnings} warning${s.warnings === 1 ? '' : 's'} (${s.high} high)`;
        card.querySelector('.skeptic-prov').textContent =
          `Precomputed offline · ${s.producer || 'unknown producer'} · ${s.produced_at || ''}`;
        list.appendChild(card);
      });
    }

    async function openSkeptic(ruleId) {
      LAST_FOCUS = document.activeElement;
      let art;
      try { art = await fetch('/admin/skeptic/' + encodeURIComponent(ruleId)).then(r => r.json()); }
      catch (e) { art = {error: String(e)}; }
      const body = $('drawerBody');
      body.textContent = '';
      $('drawerTitle').textContent = 'Skeptic review — ' + ruleId;
      const d = $('drawer');
      d.hidden = false; d.classList.add('open');
      d.querySelector('button').focus();
      if (!art || art.error) {
        body.appendChild(el(`<p class="flagline">${esc((art && art.error) || 'no review')}</p>`));
        return;
      }
      const p = art.provenance || {};
      const strip = el(`<p class="muted"></p>`);
      strip.textContent = `Precomputed offline · ${p.producer || '?'} · ${p.model || 'model unreported'}`
        + ` · ${p.produced_at || ''} · ${p.tool_calls ?? (art.tool_calls || []).length} tool calls`
        + ` · ${art.fresh ? 'inputs match' : 'STALE: ' + (art.stale_reasons || []).join('; ')}`
        + ` · billing: ${p.billing || '?'} · API key in env: ${p.api_key_in_env ? 'yes' : 'no'}`
        + (p.reverified ? ` · re-verified at ${p.reverified.code_rev || '?'} (${p.reverified.at || ''}, no model call)` : '');   // integration-leftovers hook
      body.appendChild(strip);
      if (!art.fresh) body.appendChild(el(`<p class="flagline">This review was produced against
        inputs that have since changed; it is shown greyed, not hidden.</p>`));
      const sum = el(`<p class="lede"></p>`);
      sum.textContent = `${art.verdict}: ${art.summary || ''}`;
      body.appendChild(sum);
      (art.warnings || []).forEach(w => {
        const card = el(`<article class="card ${w.severity === 'high' ? 'invalid' : 'warn-card'}"
            style="${art.fresh ? '' : 'opacity:.6'}">
          <div class="rule-head"><span class="badge-type sev"></span><strong class="kind"></strong></div>
          <p class="claim"></p>
        </article>`);
        card.querySelector('.sev').textContent = w.severity;
        card.querySelector('.kind').textContent = (w.kind || '').replace(/_/g, ' ');
        card.querySelector('.claim').textContent = w.claim || '';
        (w.evidence || []).forEach(ev => {
          const bq = el(`<blockquote class="clause"></blockquote>`);
          bq.textContent = `“${ev.quote}” — ${docTitle(ev.doc_id)}, p.${ev.page}`;
          card.appendChild(bq);
          const img = new Image();
          img.src = `/doc/${encodeURIComponent(ev.doc_id)}/page/${encodeURIComponent(ev.page)}`
            + `?bbox=${(ev.bbox || []).join(',')}`;
          img.alt = `Page ${ev.page} of ${ev.doc_id} with the quoted clause outlined`;
          img.loading = 'lazy';
          img.onerror = () => img.remove();
          card.appendChild(img);
        });
        const ce = w.counter_example;
        if (ce) {
          const row = el(`<p class="expr"></p>`);
          const sc = ce.scenario || {};
          const base = ce.baseline_total != null ? `engine, rule as written: ${ce.baseline_total} · ` : '';
          row.textContent = `${(sc.subjects || []).join(', ')} · ${JSON.stringify(sc.params || {})} · `
            + base + `engine, ${ce.rule_set === 'variant' ? 'clause reading' : 'rule as written'}: `
            + `${ce.engine_total} · ${ce.why || ''}`;
          card.appendChild(row);
        }
        if ((w.needs_data || []).length) {
          const nd = el(`<p class="muted"></p>`);
          nd.textContent = 'Needs data: ' + w.needs_data.join(', ');
          card.appendChild(nd);
        }
        if (w.suggested_action) {
          const sa = el(`<p class="muted"></p>`);
          sa.textContent = 'Suggested: ' + w.suggested_action;
          card.appendChild(sa);
        }
        body.appendChild(card);
      });
      if ((art.rejected || []).length) {
        const rj = el(`<details class="card"><summary>Dropped by the validator (${art.rejected.length})</summary></details>`);
        art.rejected.forEach(r => { const q = el('<p class="muted"></p>'); q.textContent = r.reason || ''; rj.appendChild(q); });
        body.appendChild(rj);
      }
      const did = el(`<details class="card"><summary>What the agent did (${(art.tool_calls || []).length} tool calls)</summary></details>`);
      (art.tool_calls || []).forEach(c => {
        const q = el('<p class="muted mono"></p>');
        q.textContent = `#${c.n} ${c.tool} — ${c.summary || ''}`;
        did.appendChild(q);
      });
      body.appendChild(did);
      body.appendChild(el(`<p class="lede">The agent read and challenged; every number came from the
        engine; approval is yours.</p>`));
    }
    document.addEventListener('click', e => {
      const b = e.target.closest('button.skeptic-open');
      if (b && b.dataset.rule) openSkeptic(b.dataset.rule);
    });

    /* ---------------- ingest ---------------- */
    // ids-stale-warm (A2): a two-step inline confirm. A stray click on the first button
    // on this page used to start a multi-minute, multi-GB re-parse of every contract and
    // re-check every live rule with no way back. Nothing is POSTed until confirmed.
    function ingest() {
      const n = (typeof COVERAGE !== 'undefined' && COVERAGE.length) || 5;
      const r = (typeof RATIFIED_RULES !== 'undefined' && RATIFIED_RULES.length) || 0;
      $('rereadMsg').innerHTML =
        `<p class="warn-text">This parses all ${n} PDFs again (several minutes, multi-GB RAM) ` +
        `and re-checks ${r} live rule${r === 1 ? '' : 's'} against the new text. On the shipped ` +
        `case the rule library goes empty until <code>scripts/backfill_quote_sha.py</code> is run.</p>` +
        `<p><button type="button" id="ingestConfirm" data-act="reread-go">Re-read anyway</button> ` +
        `<button type="button" class="ghost" data-act="reread-cancel">Cancel</button></p>`;
      $('ingestConfirm').focus();
    }
    async function ingestConfirmed() {
      $('rereadMsg').textContent = 'Reading all documents…';
      const start = await fetch('/admin/ingest', {method: 'POST'}).then(r => r.json());
      if (start.error) { $('rereadMsg').innerHTML = `<span class="bad">${esc(start.error)}</span>`; return; }
      const res = await pollJob(start.job_id, 'rereadMsg');
      if (!res) return;
      // Say which declared documents have no file. Silently cataloguing them as empty
      // made the library list contracts Kenny could not answer from, with no reason given.
      const miss = res.missing || [];
      const skipped = res.skipped_unchanged || [];
      const stale = res.stale_rules || [];
      let msg = `Read ${res.ingested.length} document(s)` +
                (skipped.length ? ` (${skipped.length} unchanged, not re-parsed)` : '') +
                `. ${res.rules_rechecked || 0} rule${res.rules_rechecked === 1 ? '' : 's'} re-checked, ` +
                (stale.length ? `${stale.length} stale: ${stale.join(', ')}.` : '0 stale.') +
                ` Ask about the documents in chat right away; costing rules are drafted per ` +
                `scenario on the Verification tab.`;
      if (miss.length) {
        msg += ` ${miss.length} declared in this case have no PDF yet and were skipped — ` +
               `upload them to add them: ${miss.map(m => m.doc_id).join(', ')}.`;
      }
      $('rereadMsg').innerHTML = (miss.length || stale.length)
        ? `<span class="warn-text">${esc(msg)}</span>` : esc(msg);
      loadDocs(); loadRules();
    }

    /* ------------- live upload demo (OCR_TICKETS.md OCR-6) -------------
       The upload is async on the server: the request saves the file and returns a
       job id; the job's `stage` walks parsing → tagging → indexing → done. Polling
       renders the walk as a checklist, then the result card links straight into the
       new document's X-ray so the extraction is inspectable the moment it lands. */
    const UPLOAD_STAGES = [
      ['saving',   'saving'],
      ['parsing',  'parsing pages'],
      ['tagging',  'tagging'],
      ['indexing', 'indexing'],
    ];
    function stageLine(current) {
      const at = current === 'done' ? UPLOAD_STAGES.length
               : Math.max(0, UPLOAD_STAGES.findIndex(([k]) => k === current));
      return `<p class="stages">` + UPLOAD_STAGES.map(([k, label], i) => {
        const cls = i < at ? 'stage-done' : i === at ? 'stage-active' : 'stage-todo';
        const mark = i < at ? '<span class="stage-mark" aria-hidden="true">✓</span>'
                   : i === at ? '<span class="spin" aria-hidden="true"></span>' : '';
        return `<span class="stage ${cls}">${mark}${label}</span>`;
      }).join('<span class="stage-sep" aria-hidden="true">→</span>')
        + `<span class="stage ${current === 'done' ? 'stage-done' : 'stage-todo'}">`
        + `${current === 'done' ? '<span class="stage-mark" aria-hidden="true">✓</span>' : ''}done</span></p>`;
    }

    function uploadResult(r) {
      const [cls, word] = parseTier(r.parse_source);
      const n = r.clauses || 0;
      const low = r.low_confidence_pages || [];
      return stageLine('done') + `<div class="card upload-result">
        <p><strong>${esc(r.title || r.doc_id)}</strong> — read.
          <span class="score-tier ${cls}" title="Parse tier: how the text was extracted from the PDF">${esc(word)}</span>
          <span class="score-stat">${n} clause${n === 1 ? '' : 's'} extracted</span>
          ${low.length ? `<span class="score-badge score-lowconf" title="docling's OCR scored page${low.length === 1 ? '' : 's'} ${esc(low.join(', '))} below its 'excellent' grade — the text read from there may contain misread characters">low OCR confidence</span>` : ''}</p>
        ${r.warning ? `<p class="notice">${esc(r.warning)}</p>` : ''}
        <p class="muted">${esc(r.note || '')}</p>
        <p><button type="button" data-act="xray" data-doc="${esc(r.doc_id)}"
             title="See every clause just extracted, boxed on the page it came from">
           Open X-ray</button></p></div>`;
    }

    async function upload() {
      const f = $('pdfFile').files[0];
      if (!f) { $('uploadMsg').textContent = 'Choose a PDF first.'; return; }
      const head = `<p class="muted">Reading ${esc(f.name)}… a scanned contract can take
        a few minutes (every page is OCR'd and layout-analysed).</p>`;
      $('uploadMsg').innerHTML = head + stageLine('saving');
      const fd = new FormData(); fd.append('file', f);
      const res = await fetch('/admin/upload', {method: 'POST', body: fd}).then(r => r.json());
      if (res.error) { $('uploadMsg').innerHTML = `<span class="bad">${esc(res.error)}</span>`; return; }
      for (;;) {
        const j = await fetch(`/admin/ingest/status/${res.job_id}`).then(r => r.json());
        if (j.status === 'error') {
          $('uploadMsg').innerHTML = `<span class="bad">Could not read ${esc(f.name)}: ${esc(j.error)}</span>`;
          return;
        }
        if (j.status === 'done') {
          $('uploadMsg').innerHTML = uploadResult(j.result);
          break;
        }
        $('uploadMsg').innerHTML = head + stageLine(j.stage || 'parsing');
        await new Promise(r => setTimeout(r, 700));
      }
      await loadDocs(); loadRules();   // COVERAGE must know the new doc before its X-ray opens
    }

    async function pollJob(jobId, out = 'uploadMsg') {
      for (;;) {
        await new Promise(r => setTimeout(r, 900));
        const j = await fetch(`/admin/ingest/status/${jobId}`).then(r => r.json());
        if (j.status === 'running') {
          $(out).textContent = `Reading ${j.current || ''} — ${j.done} of ${j.total} documents done.`;
          continue;
        }
        if (j.status === 'error') {
          $(out).innerHTML = `<span class="bad">Could not read the documents: ${esc(j.error)}</span>`;
          return null;
        }
        return j.result;
      }
    }

    /* ---------------- source drawer ---------------- */
    let LAST_FOCUS = null;
    async function viewSource(docId, clause, ruleId) {
      LAST_FOCUS = document.activeElement;
      // A rule being inspected can live in the review queue OR the ratified library —
      // the queue is ephemeral, so a library rule must resolve from RATIFIED_RULES
      // (looking only in PROPOSED rendered an empty drawer and lost the citation page
      // the clause fallback needs → 404).
      const rule = PROPOSED.find(r => r.id === ruleId)
                || RATIFIED_RULES.find(r => r.id === ruleId) || {};
      const body = $('drawerBody');
      $('drawerTitle').textContent = `${rule.topic || ruleId} — ${docId} §${clause}`;
      body.innerHTML = `<div class="card">${ruleBody(rule)}</div>
        <p class="lede">Does the clause below actually say this?</p>`;
      const d = $('drawer');
      d.hidden = false; d.classList.add('open');
      d.querySelector('button').focus();

      // The rule's own citation carries page + bbox — authoritative even when the
      // document has no clause numbers to resolve (OCR'd contracts). Use it as the
      // fallback so View source always renders the page, never a 404.
      const cite = rule.citation || {};
      let c = null, err = null;
      try {
        const resp = await fetch(`/admin/clause?doc_id=${encodeURIComponent(docId)}`
          + `&clause=${encodeURIComponent(clause)}`
          + (cite.page != null ? `&page=${encodeURIComponent(cite.page)}` : ''));
        c = await resp.json().catch(() => null);
        if (!resp.ok && !(cite.page != null)) err = `HTTP ${resp.status}`;
      } catch (e) { err = String(e); }
      // Prefer the endpoint's resolved clause; else fall back to the rule's citation.
      const page = (c && c.page != null) ? c.page : cite.page;
      const bbox = (c && c.bbox) ? c.bbox : (cite.bbox || []);
      if (page == null) {
        body.appendChild(el(`<p class="flagline">Could not load the clause: ${esc(err || (c && c.error) || 'no page on this citation')}</p>`));
        return;
      }
      if (c && c.text) body.appendChild(el(`<blockquote class="clause">${esc(c.text)}</blockquote>`));
      const fig = el(`<figure class="cite"><figcaption class="muted">Page ${esc(page)} of the
        contract, with the cited passage highlighted:</figcaption></figure>`);
      const img = new Image();
      img.src = `/doc/${encodeURIComponent(docId)}/page/${page}?bbox=${bbox.join(',')}`;
      img.alt = `Page ${page} of ${docId} with the citation outlined in red`;
      img.onerror = () => { img.remove(); fig.appendChild(el('<p class="muted">The page image could not be produced.</p>')); };
      fig.appendChild(img);
      body.appendChild(fig);
    }
    function closeDrawer() {
      const d = $('drawer');
      d.classList.remove('open'); d.hidden = true;
      if (LAST_FOCUS) LAST_FOCUS.focus();
    }
    document.addEventListener('keydown', e => {
      if (e.key !== 'Escape') return;
      if (!$('xray').hidden) closeXray();
      else if (!$('drawer').hidden) closeDrawer();
    });

    /* ------- document X-ray + side-by-side Compare (OCR_TICKETS.md OCR-1/2) ------- */
    // Boxes are positioned in % of the page so they stay glued to their clauses at any
    // rendered width. Catalog bboxes are bottom-left origin ([l,t,r,b], t measured from
    // the page BOTTOM) — the same flip core/pdfview.py does, here in percentages.
    // One dialog serves both views; XRAY_MODE picks the layout. Compare adds a right
    // pane with the extracted text and a shared hover state keyed by merged-box index.
    let XRAY_DOC = null, XRAY_PAGE = 1, XRAY_PAGES = 1, XRAY_FOCUS = null, XRAY_MODE = 'xray';

    const xrayKind = k => k === 'table-row' ? 'xk-table'
      : (k === 'recovered-row' || k === 'recovered-text') ? 'xk-recovered'
      : k === 'page-text' ? 'xk-page' : 'xk-text';

    function xrayCounts(clauses) {
      const n = {text: 0, table: 0, recovered: 0, page: 0};
      clauses.forEach(c => n[{ 'xk-table': 'table', 'xk-recovered': 'recovered',
                               'xk-page': 'page' }[xrayKind(c.kind)] || 'text']++);
      const parts = [];
      if (n.text) parts.push(`${n.text} text`);
      if (n.table) parts.push(`${n.table} table row${n.table === 1 ? '' : 's'}`);
      if (n.recovered) parts.push(`${n.recovered} recovered`);
      if (n.page) parts.push(`${n.page} page-level`);
      return `${clauses.length} clause${clauses.length === 1 ? '' : 's'}`
        + (parts.length ? ` (${parts.join(', ')})` : '');
    }

    const openXray = (docId, page = 1) => openViewer('xray', docId, page);
    // D4: Compare takes a page so the showcase button, the tour and the deep link
    // #compare=<doc>:<page> (also #documents?compare=<doc>&p=<page>) land on it.
    const openCompare = (docId, page = 1) => openViewer('compare', docId, page);
    function openViewer(mode, docId, page = 1) {
      XRAY_FOCUS = document.activeElement;
      XRAY_DOC = docId; XRAY_PAGE = Math.max(1, Number(page) || 1); XRAY_MODE = mode;
      $('xrayTitle').textContent = `${mode === 'compare' ? 'Compare' : 'X-ray'} — ${docTitle(docId)}`;
      $('xray').classList.toggle('compare', mode === 'compare');
      $('xrayText').hidden = mode !== 'compare';
      $('xray').hidden = false;
      $('xrayClose').focus();
      loadXray();
    }
    function closeXray() {
      $('xray').hidden = true;
      $('xrayStage').innerHTML = '';
      $('xrayText').innerHTML = '';
      XPIN = null;
      if (XRAY_FOCUS) XRAY_FOCUS.focus();
    }
    $('xray').addEventListener('click', e => { if (e.target === $('xray')) closeXray(); });
    function xrayStep(d) {
      XRAY_PAGE = Math.min(Math.max(1, XRAY_PAGE + d), XRAY_PAGES);
      loadXray();
    }

    async function loadXray() {
      const stage = $('xrayStage'), pane = $('xrayText');
      stage.innerHTML = `<p class="muted">Reading page ${XRAY_PAGE}&hellip;</p>`;
      pane.innerHTML = '';
      XPIN = null;  // a pinned highlight is per-page; the groups are about to be rebuilt
      let res;
      try {
        const resp = await fetch(`/doc/${encodeURIComponent(XRAY_DOC)}/clauses?page=${XRAY_PAGE}`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        res = await resp.json();
      } catch (e) {
        stage.innerHTML = `<p class="flagline">Could not load the extraction for this page: ${esc(String(e))}</p>`;
        return;
      }
      XRAY_PAGE = res.page; XRAY_PAGES = res.page_count || 1;
      $('xrayPageNo').textContent = `page ${XRAY_PAGE} of ${XRAY_PAGES}`;
      $('xrayPrev').disabled = XRAY_PAGE <= 1;
      $('xrayNext').disabled = XRAY_PAGE >= XRAY_PAGES;
      const W = res.width, H = res.height;
      // Clauses sharing an identical bbox (docling gives every row of some tables the
      // whole-table box) are merged into ONE div: stacked translucent fills compose to
      // an opaque blob that hides the page, and only the topmost would be hoverable.
      // Each merged box gets a group index (data-g); in Compare the SAME index is
      // stamped on every right-side row drawn from that box, so hovering one merged
      // table box lights all of its rows, and any of those rows lights the box.
      const groups = new Map();
      res.clauses.forEach(c => {
        c._g = null;
        if (!c.bbox || c.bbox.length !== 4) return;
        const k = c.bbox.join(',');
        if (!groups.has(k)) groups.set(k, {gi: groups.size, clauses: []});
        const g = groups.get(k);
        g.clauses.push(c);
        c._g = g.gi;
      });
      const compare = XRAY_MODE === 'compare';
      // Table X-ray (OCR-5, widened by D4): in Compare every table on the page —
      // each group of >= 2 table rows sharing a box — renders as the grid it was,
      // and the count strip names the mode. The salary schedule (one box per
      // page) behaves exactly as before.
      const models = compare ? tableModels(res.clauses) : [];
      const model = models.length === 1 ? models[0] : null;
      const nrows = models.reduce((n, m) => n + m.rows.length, 0);
      const ncols = Math.max(0, ...models.map(m => m.cols));
      // Low OCR confidence (OCR-7): the server flags the PAGE (docling reports
      // confidence per page, not per word) and every clause on it — the count strip
      // names the condition once, each box/row wears the amber tint.
      // Disputed cells (D2): the second OCR read disagrees with the stored text.
      const disputed = Number(res.disputed_cells || 0);
      $('xrayCounts').textContent = xrayCounts(res.clauses)
        + (models.length ? ` · table page — ${nrows} row${nrows === 1 ? '' : 's'}`
                 + ` × ${ncols} column${ncols === 1 ? '' : 's'}` : '')
        + (res.low_confidence ? ` · low OCR confidence on this page (score ${Number(res.ocr_confidence).toFixed(2)})` : '')
        + (disputed ? ` · ${disputed} cell${disputed === 1 ? '' : 's'} disputed by a second OCR read` : '');
      const boxes = [...groups.values()].map(({gi, clauses: g}) => {
        const c = g[0], [l, t, r, b] = c.bbox;
        const dis = g.some(x => x.cell_status === 'disputed');
        // t is the TOP edge's distance from the page bottom, hence (H - t) from the top.
        // Compare needs no tooltip: the full text sits in the right pane, and the tip
        // would cover the very rows the hover is pointing at.
        const tip = compare ? '' : `<span class="xtip">${esc(
          (dis ? 'disputed cells: a second OCR read disagrees with the stored text · ' : '')
          + (c.low_confidence ? 'low OCR confidence on this page · ' : '')
          + (g.length > 1 ? `${g.length} clauses share this box · ` : '')
          + (c.clause ? `§${c.clause} — ` : '') + (c.text || '').slice(0, 200))}</span>`;
        return `<div class="xbox ${xrayKind(c.kind)}${c.low_confidence ? ' xlow' : ''}${dis ? ' xdisputed' : ''}" tabindex="0" data-g="${gi}"
          style="left:${(l / W * 100).toFixed(3)}%;top:${((H - t) / H * 100).toFixed(3)}%;width:${((r - l) / W * 100).toFixed(3)}%;height:${((t - b) / H * 100).toFixed(3)}%">${tip}</div>`;
      }).join('');
      stage.innerHTML = `<img src="/doc/${encodeURIComponent(XRAY_DOC)}/page/${XRAY_PAGE}"
        alt="Page ${XRAY_PAGE} of ${esc(XRAY_DOC)}, with every extracted clause boxed">` + boxes;
      if (compare) renderComparePane(res.clauses, models);
    }

    // Right pane (OCR-2): the page's clauses in reading order. bbox `t` is measured
    // from the page BOTTOM, so descending t = top of the page first; the sort is
    // stable, so merged table rows keep their catalog order within a shared box.
    // Clauses with no bbox cannot be placed on the page and go last, unmarked.
    function renderComparePane(clauses, models) {
      const placed = clauses.filter(c => c._g != null).sort((a, b) => b.bbox[1] - a.bbox[1]);
      const loose = clauses.filter(c => c._g == null);
      const cellTitle = c => c.cell_status === 'disputed'
        ? ' title="a second OCR read disagrees with the stored text of this row — see the amber cells"'
        : c.low_confidence ? ' title="low OCR confidence on this page — the text may contain misread characters"' : '';
      const row = c => `<div class="xrow ${xrayKind(c.kind)}${c.low_confidence ? ' xlow' : ''}${c.cell_status === 'disputed' ? ' xcell-disputed' : ''}"${c._g != null ? ` data-g="${c._g}"` : ''} tabindex="0"${cellTitle(c)}>
          <i class="xdot ${xrayKind(c.kind)}" aria-hidden="true"></i>
          <div class="xrow-body">${c.clause ? `<span class="xrow-no">§${esc(c.clause)}</span> ` : ''}<span class="xrow-text">${esc(c.text || '')}</span></div>
        </div>`;
      if (!models.length) {
        $('xrayText').innerHTML = placed.map(row).join('')
          + (loose.length ? `<p class="muted xrow-loose-head">No position recorded on the page for:</p>`
             + loose.map(row).join('') : '');
        return;
      }
      // Table page (OCR-5 / D4): each structured table replaces its table rows in
      // the pane, at the reading-order position of its box; the page's other
      // clauses keep their place above, between and below the tables. Grouped rows
      // that did not make a grid stay plain rows.
      const gridded = new Set(models.flatMap(m => m.rows.map(r => r.c)));
      const items = placed.filter(c => !gridded.has(c)).map(c => ({t: c.bbox[1], html: row(c)}));
      models.forEach(m => {
        const t = Math.max(-Infinity, ...m.rows.filter(r => r.c.bbox).map(r => r.c.bbox[1]));
        items.push({t, html: renderXTable(m)});
      });
      items.sort((a, b) => b.t - a.t);
      const looseLeft = loose.filter(c => !gridded.has(c));
      $('xrayText').innerHTML = items.map(i => i.html).join('')
        + (looseLeft.length ? `<p class="muted xrow-loose-head">No position recorded on the page for:</p>`
           + looseLeft.map(row).join('') : '');
    }

    // ---- table X-ray (OCR_TICKETS.md OCR-5) ----
    // A page whose extraction is MOSTLY table rows reads better as the grid it was on
    // paper. The catalog flattens each row to "<caption> — cell | cell | …" (cells are
    // often "Header: value") and may open with a synthetic whole-table summary line
    // "<caption> (table: H1, H2; N rows)". Rebuild the table from those strings.
    const isTableKind = k => k === 'table-row' || k === 'recovered-row';

    // D4: every table on the page gets a grid — each group of >= 2 table rows
    // sharing a bbox (docling gives every row of a table the whole-table box), in
    // page order. The old majority rule hid the p.22 vacation table (6 rows among
    // 14 clauses) as plain rows, where no cell could be coloured.
    function tableModels(clauses) {
      const groups = new Map();
      clauses.filter(c => isTableKind(c.kind)).forEach(c => {
        const k = c.bbox && c.bbox.length === 4 ? c.bbox.join(',') : `loose-${clauses.indexOf(c)}`;
        if (!groups.has(k)) groups.set(k, []);
        groups.get(k).push(c);
      });
      return [...groups.values()].filter(g => g.length >= 2).map(tableModel).filter(Boolean);
    }

    function tableModel(trows) {
      if (!trows || !trows.length) return null;
      let caption = null, headers = null, body = trows;
      const sum = /^(.*?)\s*\(table:\s*(.+);\s*\d+\s+rows?\)$/.exec(trows[0].text || '');
      if (sum) {
        caption = sum[1].trim() || null;
        headers = sum[2].split(/,\s*/).filter(Boolean);
        body = trows.slice(1);
      }
      if (!body.length) return null;
      // Strip the caption prefix the flattener stamped on every row. Without a summary
      // line, the caption is whatever prefix ALL rows share before their first " — ".
      if (caption == null && body.length > 1) {
        const cand = (body[0].text || '').split(' — ')[0];
        if (cand && body.every(c => (c.text || '').startsWith(cand + ' — '))) caption = cand;
      }
      const strip = t => caption != null && t.startsWith(caption + ' — ')
        ? t.slice(caption.length + 3) : t;
      const raw = body.map(c => ({c, cells: strip(c.text || '').split(' | ')}));
      if (!headers) {
        // No summary line: headers can still be inferred from "Header: value" cells
        // when each column's header is consistent across every row; when they are
        // inconsistent the cells stay plain text — never guess a wrong heading.
        const n = Math.max(...raw.map(r => r.cells.length));
        const inf = Array(n).fill(undefined);
        let ok = true;
        raw.forEach(({cells}) => cells.forEach((cell, i) => {
          const m = /^(.{1,60}?):\s(.*)$/.exec(cell);
          const h = m ? m[1] : null;
          if (inf[i] === undefined) inf[i] = h;
          else if (inf[i] !== h) ok = false;
        }));
        if (ok && inf.every(h => h)) headers = inf;
      }
      // With known headers, "Header: value" cells file under their column and show
      // just the value; a cell with no matching header keeps its raw text in the next
      // free slot, so nothing the machine extracted is ever dropped from view.
      const rows = headers ? raw.map(({c, cells}) => {
        const out = Array(headers.length).fill('');
        let pos = 0;
        cells.forEach(cell => {
          const j = headers.findIndex((h, k) => !out[k] && cell.startsWith(h + ': '));
          if (j >= 0) { out[j] = cell.slice(headers[j].length + 2); pos = j + 1; }
          else if (pos < out.length && !out[pos]) out[pos++] = cell;
          else out.push(cell);
        });
        return {c, cells: out};
      }) : raw;
      const cols = Math.max(headers ? headers.length : 1, ...rows.map(r => r.cells.length));
      return {caption, headers, rows, cols};
    }

    function renderXTable(model) {
      // Numeric-only headers are docling's positional placeholders (0, 1, 2) — noise
      // as column headings, so the heading row renders only when a header says something.
      const showHead = model.headers && model.headers.some(h => /[^\d\s]/.test(h));
      const line = (r, tag) => Array.from({length: model.cols},
        (_, i) => `<${tag}>${esc(r[i] ?? '')}</${tag}>`).join('');
      // D2: a verified row carries one record per cell (same column index as the
      // grid, the server rebuilds it with the same parser). Disputed: amber, with
      // the second read beside the stored text — never in place of it. Unverified:
      // hatched. The stored text stays exactly what the catalog holds.
      const cellTd = (c, cells) => Array.from({length: model.cols}, (_, i) => {
        const rec = (c.cells || [])[i];
        if (!rec) return `<td>${esc(cells[i] ?? '')}</td>`;
        const st = rec.status || 'unverified';
        const tip = st === 'disputed'
          ? `stored ${JSON.stringify(rec.stored)}; re-read ${JSON.stringify(rec.reread ?? '—')}${rec.score != null ? ` (score ${Number(rec.score).toFixed(2)})` : ''}${(rec.flags || []).length ? ` · ${rec.flags.join(', ')}` : ''}`
          : st === 'unverified' ? 'the second OCR read produced no token for this cell'
          : `stored ${JSON.stringify(rec.stored)}; re-read agrees${rec.score != null ? ` (score ${Number(rec.score).toFixed(2)})` : ''}`;
        const reread = st === 'disputed' && rec.reread != null ? `<span class="xreread">${esc(rec.reread)}</span>` : '';
        return `<td class="xcell-${st}" title="${esc(tip)}">${esc(cells[i] ?? '')}${reread}</td>`;
      }).join('');
      const anyDisputed = model.rows.some(({c}) => c.cell_status === 'disputed');
      return `<div class="xtable-wrap"><table class="xtable">
        ${model.caption ? `<caption>${esc(model.caption)}</caption>` : ''}
        ${showHead ? `<thead><tr>${line(model.headers, 'th')}</tr></thead>` : ''}
        <tbody>${model.rows.map(({c, cells}) =>
          `<tr class="${xrayKind(c.kind)}${c.low_confidence ? ' xlow' : ''}"${c._g != null ? ` data-g="${c._g}"` : ''} tabindex="0"${c.low_confidence ? ' title="low OCR confidence on this page — the text may contain misread characters"' : ''}>${cellTd(c, cells)}</tr>`).join('')}</tbody>
      </table>${anyDisputed ? `<p class="xcell-note">Amber cells: a second OCR engine (RapidOCR) read the page image and disagrees with the stored text; its reading is shown beside the stored value, never written over it. Hatched cells could not be re-read.</p>` : ''}</div>`;
    }

    // Shared hover: a right-side row (plain .xrow or an OCR-5 table <tr> — anything
    // carrying data-g) lights its box; a box lights all of its rows and brings the
    // first into view. Delegated on the two containers so it survives every innerHTML
    // rebuild on page steps. Clicking pins the highlight (XPIN): it survives the mouse
    // leaving, until the group is clicked again or the page changes.
    let XPIN = null;
    function xsync(g, on, fromStage) {
      $('xrayStage').querySelectorAll(`.xbox[data-g="${g}"]`)
        .forEach(b => b.classList.toggle('active', on));
      const rows = $('xrayText').querySelectorAll(`[data-g="${g}"]`);
      rows.forEach(r => r.classList.toggle('active', on));
      if (on && fromStage && rows.length) rows[0].scrollIntoView({block: 'nearest'});
    }
    [['xrayStage', true], ['xrayText', false]].forEach(([id, fromStage]) => {
      const sync = on => e => {
        if (XRAY_MODE !== 'compare') return;
        const t = e.target.closest('[data-g]');
        if (t && (on || t.dataset.g !== XPIN)) xsync(t.dataset.g, on, fromStage);
      };
      $(id).addEventListener('mouseover', sync(true));
      $(id).addEventListener('mouseout', sync(false));
      $(id).addEventListener('focusin', sync(true));
      $(id).addEventListener('focusout', sync(false));
      $(id).addEventListener('click', e => {
        if (XRAY_MODE !== 'compare') return;
        const t = e.target.closest('[data-g]');
        if (!t) return;
        const g = t.dataset.g;
        if (XPIN != null && XPIN !== g) xsync(XPIN, false, false);
        XPIN = XPIN === g ? null : g;
        xsync(g, true, fromStage);
        if (XPIN != null && !fromStage) {
          const b = $('xrayStage').querySelector(`.xbox[data-g="${g}"]`);
          if (b) b.scrollIntoView({block: 'nearest'});
        }
      });
    });

    /* ---------------- audit (ledger chunk: F2 read side, F3, F6) ---------------- */
    let LEDGER = {events: [], count: 0, head: null, keyed: false};
    let LEDGER_QID = null;          // when set, the log shows one question's events
    const OPEN_EVTS = new Set();    // seqs whose full payload is expanded
    const USD = new Intl.NumberFormat('en-US', {style: 'currency', currency: 'USD'});
    const money = v => USD.format(v);
    // The hashed `ts` field is the time of record; `iso` carries no zone and would be
    // read as local by new Date(). Local time on screen, UTC ISO on hover.
    const when = ts => {
      if (ts == null) return '';
      const d = new Date(ts * 1000);
      const utc = d.toISOString().replace(/\.\d{3}Z$/, 'Z');
      return `<time datetime="${utc}" title="${utc}">${esc(d.toLocaleString())}</time>`;
    };
    const fmtTotal = x => {
      if (x.total == null) return `<span class="muted">${esc(x.outcome || 'no answer')}</span>`;
      if (!x.result_type || x.result_type === 'currency') return money(x.total);
      return `${esc(x.total)} ${esc(x.result_type)}`;
    };
    const short = h => (h || '').slice(0, 12);

    async function loadLedger() {
      const res = await fetch('/admin/ledger').then(r => r.json());
      LEDGER = res;
      $('verifyChain').innerHTML = res.verified
        ? `<span class="badge-ratified">Record intact</span>`
        : `<span class="bad">Record altered: ${esc(res.verify_message)}</span>`;
      const head = res.head;
      $('ledgerStrip').innerHTML = `
        <span><strong>${res.count}</strong> events</span>
        ${head ? `<span>head <strong>#${head.seq}</strong> <code title="${esc(head.hash)}">${esc(short(head.hash))}…</code></span>` : ''}
        <span>${res.keyed ? 'Signed (HMAC-SHA256)' : 'Integrity-only (unsigned)'}</span>`;
      renderLedger();
      const h = await fetch('/admin/history').then(r => r.json());
      $('history').innerHTML = h.history.length ? h.history.slice(0, 12).map(x => `
        <p class="evt qrow" tabindex="0" role="button" data-qid="${esc(x.query_id)}"
           aria-label="Show the ${x.events} events behind this question">
          <strong>${fmtTotal(x)}</strong>
          <span class="muted">${when(x.ts)} · ${x.events} events · ${esc(x.query_id)}</span><br>${esc(x.prompt || '')}</p>`).join('')
        : `<p class="empty">No questions asked yet.</p>`;
    }

    function renderLedger() {
      const q = ($('ledgerSearch').value || '').trim().toLowerCase();
      let evs = LEDGER.events || [];
      if (LEDGER_QID) evs = evs.filter(e => e.query_id === LEDGER_QID);   // oldest first
      else evs = evs.slice(-60).reverse();
      if (q) evs = evs.filter(e => (e.type + ' ' + JSON.stringify(e.payload)).toLowerCase().includes(q));
      $('ledgerFilter').innerHTML = LEDGER_QID
        ? `<span class="fchip">Question ${esc(LEDGER_QID)} · ${evs.length} events
             <button type="button" onclick="filterQuestion(null)">clear</button></span>` : '';
      $('ledgerShown').textContent = LEDGER_QID ? '' : `last ${evs.length} of ${LEDGER.count || 0}`;
      $('ledger').innerHTML = evs.length ? evs.map(e => {
        const open = OPEN_EVTS.has(e.seq);
        const body = open ? JSON.stringify(e.payload, null, 2) : JSON.stringify(e.payload).slice(0, 180);
        return `<p class="evt${open ? ' open' : ''}" data-seq="${e.seq}" tabindex="0" role="button"
                   aria-expanded="${open}" title="Click to ${open ? 'collapse' : 'expand'} the payload">
          <span class="t">${esc(e.type)}</span>
          <span class="muted">#${e.seq} · ${esc(e.actor)}${e.query_id ? ' · ' + esc(e.query_id) : ''} · ${when(e.ts)}</span>
          <code>${esc(body)}</code></p>`;
      }).join('') : `<p class="empty">No events match.</p>`;
    }

    function filterQuestion(qid) {
      LEDGER_QID = qid;
      renderLedger();
      if (qid) $('ledger').scrollIntoView({block: 'nearest'});
    }
    // One delegated handler, no string-built onclicks on data rows.
    $('history').addEventListener('click', e => {
      const row = e.target.closest('.qrow'); if (row) filterQuestion(row.dataset.qid);
    });
    $('history').addEventListener('keydown', e => {
      const row = e.target.closest('.qrow');
      if (row && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); filterQuestion(row.dataset.qid); }
    });
    const toggleEvt = row => {
      const seq = Number(row.dataset.seq);
      OPEN_EVTS.has(seq) ? OPEN_EVTS.delete(seq) : OPEN_EVTS.add(seq);
      renderLedger();
    };
    $('ledger').addEventListener('click', e => {
      if (window.getSelection && String(window.getSelection())) return;   // let people copy
      const row = e.target.closest('.evt'); if (row) toggleEvt(row);
    });
    $('ledger').addEventListener('keydown', e => {
      const row = e.target.closest('.evt');
      if (row && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); toggleEvt(row); }
    });

    async function tamperDemo() {
      const mode = $('tamperMode').value;
      const btn = $('tamperBtn'); btn.disabled = true;
      $('tamperResult').innerHTML = `<p class="muted">Copying the record and running the attack…</p>`;
      try {
        const res = await fetch('/admin/ledger/tamper-demo?mode=' + encodeURIComponent(mode)).then(r => r.json());
        if (res.error) { $('tamperResult').innerHTML = `<p class="flagline">${esc(res.error)}</p>`; return; }
        const c = res.copy, t = res.target || {};
        const verifyLine = c.ok
          ? `<span class="hash-ok">passed</span> — ${esc(c.count)} events verify; an unsigned chain only proves internal consistency.`
          : `<span class="hash-bad">FAILED at #${esc(c.failed_seq)}</span> (${esc(c.reason)}${c.event_type ? ', ' + esc(c.event_type) : ''})`
            + (c.stored_hash ? `<br>stored <code>${esc(short(c.stored_hash))}…</code> recomputed <code>${esc(short(c.recomputed_hash || '—'))}…</code>` : '');
        const anchorLine = c.anchor
          ? `<p>Recorded head #${esc(c.anchor.recorded_head.seq)} <code>${esc(short(c.anchor.recorded_head.hash))}…</code>: `
            + (c.anchor.ok ? `<span class="hash-ok">intact</span>` : `<span class="hash-bad">${esc(c.anchor.message)}</span>`) + `</p>` : '';
        const change = t.field
          ? `Changed <strong>#${esc(t.seq)}</strong> ${esc(t.type)} <code>${esc(t.field)}</code> ${esc(JSON.stringify(t.before))} → ${esc(JSON.stringify(t.after))}`
          : mode === 'delete' ? `Deleted <strong>#${esc(t.seq)}</strong> ${esc(t.type)}`
          : `Dropped the last ${esc(t.dropped)} events (${esc(t.before)} → ${esc(t.after)})`;
        $('tamperResult').innerHTML = `
          <article class="card ${c.ok && !(c.anchor && !c.anchor.ok) ? 'warn-card' : 'invalid'}">
            <p>Copied <strong>${esc(res.real.count)}</strong> events to a scratch file. ${change}.</p>
            <p>Verify on the copy: ${verifyLine}</p>
            ${anchorLine}
            <p>Real ledger: <strong>${res.real.untouched ? 'untouched' : 'CHANGED'}</strong>,
               ${res.real.verified ? 'still intact' : 'NOT intact'} (${esc(res.real.count)} events). The copy was deleted.</p>
            <p class="muted">${esc(res.explanation)}</p>
          </article>`;
      } catch (e) {
        $('tamperResult').innerHTML = `<p class="flagline">The demonstration could not run: ${esc(e.message)}</p>`;
      } finally { btn.disabled = false; }
    }

    /* approvals (F2 read side): who approved which rule text, against which clause and known answer */
    let APPROVALS = {};
    async function loadApprovals() {
      try {
        const res = await fetch('/admin/approvals').then(r => r.json());
        APPROVALS = Object.fromEntries((res.approvals || []).map(a => [a.rule_id, a]));
      } catch (e) { APPROVALS = {}; }
    }
    function approvalLine(r) {
      const a = APPROVALS[r.id];
      if (!a || !a.approval) return `<p class="muted">No approval record in this ledger yet.</p>`;
      const ap = a.approval, cit = ap.citation || {};
      const d = ap.approved_at && !isNaN(Date.parse(ap.approved_at))
        ? new Date(ap.approved_at).toLocaleDateString('en-US', {day: 'numeric', month: 'short', year: 'numeric'})
        : esc(ap.approved_at || 'date unknown');
      const ka = (ap.known_answers || [])[0];
      const rt = r.result_type || 'currency';
      const kaText = ka
        ? `reproduced <strong>${rt === 'currency' ? money(ka.expected) : esc(ka.expected) + ' ' + esc(rt)}</strong> (${esc(ka.status)})`
        : `<span class="warn-text">no known answer exercises it</span>`;
      return `<p class="muted approval">Approved by <strong>${esc(ap.approver)}</strong> · ${d} ·
        against ${esc(docTitle(cit.doc_id || ''))}${cit.page ? ' p.' + esc(cit.page) : ''} “${esc(cit.clause || '?')}” ·
        ${kaText} · ledger #${esc(ap.seq)}
        ${ap.backfilled ? `<span class="chip" title="${esc(ap.basis || '')}">back-filled${ap.original_event ? ` (original #${esc(ap.original_event.seq)})` : ''}</span>` : ''}
        ${a.matches_live ? '' : `<span class="bad">rule text edited since this approval</span>`}</p>`;
    }
    function approverName() {
      const input = $('approverName');
      let name = input ? input.value.trim() : '';
      try {
        if (name) localStorage.setItem('kenny.approver', name);
        else if (input) { name = localStorage.getItem('kenny.approver') || ''; input.value = name; }
      } catch (e) {}
      return name;
    }
    try { const saved = localStorage.getItem('kenny.approver'); if (saved && $('approverName')) $('approverName').value = saved; } catch (e) {}

    /* ---------------- boot ---------------- */
    fetch('/api/case').then(r => r.json()).then(c => {
      $('caseName').textContent = c.name || 'Contract library, rule review and audit';
      if (c.banner) { const b = $('demoBanner'); b.textContent = c.banner; b.hidden = false; }
      if (c.version && $('buildTag')) $('buildTag').textContent = 'build ' + c.version.sha + (c.version.dirty ? ' +uncommitted' : '');   // handoff (K1)
    });
    // D4 deep link: #compare=<doc>:<page> or #documents?compare=<doc>&p=<page> opens
    // Compare on that page once the Documents tab has loaded (the viewer needs the
    // document titles). The tab part of the hash still selects the tab.
    function compareDeepLink(hash) {
      const h = (hash || '').replace(/^#/, '');
      let m = /^compare=([^:&]+)(?::(\d+))?$/.exec(h);
      if (m) return {doc: decodeURIComponent(m[1]), page: Number(m[2] || 1), tab: 'docs'};
      const [tab, query] = h.split('?');
      if (!query) return null;
      const p = new URLSearchParams(query);
      if (!p.get('compare')) return null;
      return {doc: p.get('compare'), page: Number(p.get('p') || 1), tab: tab || 'docs'};
    }
    const DEEP = compareDeepLink(location.hash);
    const docsLoaded = loadDocs(); loadRules(); loadVerify(); loadLedger();
    // No hash opens the default tab without writing one, so a plain /admin stays plain.
    if (location.hash && TABS.includes(location.hash.slice(1))) showTab(location.hash.slice(1), false);
    else if (!DEEP) showTab(DEFAULT_TAB, false, false);
    if (DEEP) {
      if (TABS.includes(DEEP.tab)) showTab(DEEP.tab, false);
      docsLoaded.then(() => openCompare(DEEP.doc, DEEP.page));
    }
