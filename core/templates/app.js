// Chat surface: send prompt -> render answer -> click a dollar figure for the audit
// drawer (decision trace + citation with the docling bbox highlighted on the page).
//
// Sections (each delimited with a banner comment so wave 2 can extend one without
// touching the others):
//   1. case metadata + helpers
//   2. request lifecycle (I1): one ask() with pending bubble, disabled controls, errors
//   3. renderers (I2/I4/I6): costing, entitlement, clarify, policy, refusal
//   4. audit drawer (I4; wave 2 adds the search tree + replay here)
//   5. AI-involvement panel (I3)
//   6. drawer open/close, home-page examples and ?q= deep link (I16)
let CASE = {};
const log = document.getElementById('log');

// ============================================================================ //
// 1. case metadata + helpers
// ============================================================================ //
fetch('/api/case').then(r => r.json()).then(c => {
  CASE = c || {};
  document.getElementById('caseName').textContent = CASE.name || '';
  const b = document.getElementById('keyBadge');
  b.textContent = CASE.llm_mode === 'off' ? 'LLM: off (deterministic)'   // agentic (G9/A8)
    : CASE.llm_status === 'degraded' ? 'LLM: Claude (degraded, using fallbacks)'
    : CASE.has_api_key ? 'LLM: Claude' : 'LLM: deterministic fallback';
  showBanner(CASE.banner);
}).catch(() => {});

// role="note" rather than an alert: it is standing context, not an event, so it should
// be reachable in the reading order without interrupting whatever is being announced.
function showBanner(text) {
  if (!text) return;
  const el = document.getElementById('demoBanner');
  if (!el) return;
  el.textContent = text;
  el.hidden = false;
}

function el(html) { const d = document.createElement('div'); d.innerHTML = html.trim(); return d.firstChild; }
function esc(s) { return (s ?? '').toString().replace(/[&<>]/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[m])); }
function fmt(n) { return '$' + Number(n).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2}); }

// An MOU is a rulebook: a rule's value may be money, days, hours, a date or a yes/no.
function fmtVal(n, type) {
  const v = Number(n);
  switch (type) {
    case 'days':    return v + (v === 1 ? ' day' : ' days');
    case 'shifts':  return v + (v === 1 ? ' shift' : ' shifts');   // entitlement-retrieval (B3): 56-hour units count shifts
    case 'hours':   return v + (v === 1 ? ' hour' : ' hours');
    case 'boolean': return v ? 'Yes' : 'No';
    case 'date':    return String(n);
    case 'text':    return String(n);
    default:        return fmt(v);
  }
}

// A document is called by its declared title everywhere a person reads it (I4). The
// id stays available under "Technical details". Responses that carry titles win;
// otherwise the case manifest (served by /api/case) maps the id.
function docTitle(docId, res) {
  if (!docId) return '';
  const fromRes = ((res && res.chosen_docs) || []).find(d => d.doc_id === docId);
  if (fromRes && fromRes.title) return fromRes.title;
  const fromCase = (CASE.sources || []).find(d => d.doc_id === docId);
  if (fromCase && fromCase.title) return fromCase.title;
  return docId;
}

// Citation label: never a bare section sign. Most chunks carry no clause label, so
// the page is the address; a clause label that already names its page is used as is.
function cite(clause, page) {
  const c = (clause ?? '').toString().trim();
  const p = page ? `p.${page}` : '';
  if (!c) return p;
  if (/\bp\.\s*\d+/.test(c)) return c;
  return p ? `${c}, ${p}` : c;
}

// Extraction-tier chip (OCR-4): says where a cited passage's text CAME FROM. The tier
// itself is computed server-side (core/app.py::_extraction_tier — the one mapping);
// this only renders it, so client and server can never disagree. Unknown/absent tier
// renders nothing: no claim beats a wrong claim.
const TIER_TITLES = {
  'text layer': "Read directly from the PDF's digital text layer with exact clause positions",
  "OCR'd scan": "Scanned paper; the text was produced by OCR (Tesseract via OCRmyPDF) and can contain misread characters. Check the page image.",
  'recovered layout': 'The layout model misread this page; the text was recovered from raw span geometry',
  'page-level': 'Extracted as raw page text — citations open the page, not the exact clause',
  'sidecar extract': 'Loaded from a hash-bound sidecar extraction',
};
function tierChip(tier) {
  if (!tier || !TIER_TITLES[tier]) return '';
  return ` <span class="tier-chip" title="${esc(TIER_TITLES[tier])}">${esc(tier)}</span>`;
}

function scroll() { window.scrollTo(0, document.body.scrollHeight); }
function addUser(text) { log.appendChild(el(`<div class="msg user">${esc(text)}</div>`)); scroll(); }

// ============================================================================ //
// 2. request lifecycle (I1)
//    One ask() behind send/answerDepartment/confirmDoc: the question is echoed, a
//    pending bubble holds its place, the controls are disabled, and whatever comes
//    back (answer, HTTP error, bad JSON, network failure, timeout) lands IN THAT
//    BUBBLE. The question is never lost: Retry re-sends the same body, Edit puts the
//    text back in the box.
// ============================================================================ //
let IN_FLIGHT = null;   // the AbortController of the one request allowed at a time
let SLOT = null;        // the pending bubble the next addBot() fills
const ASK_TIMEOUT_MS = 90000;   // longer than the measured 62 s cold start

function addBot(node) {
  if (SLOT) {
    const s = SLOT; SLOT = null;
    s.classList.remove('pending', 'error');
    s.removeAttribute('aria-busy');
    s.innerHTML = '';
    s.appendChild(node);
    scroll();
    return;
  }
  const m = el('<div class="msg bot"></div>'); m.appendChild(node); log.appendChild(m); scroll();
}

function setBusy(on) {
  const form = document.getElementById('askForm');
  const input = document.getElementById('prompt');
  if (form) form.querySelectorAll('button[type="submit"]').forEach(b => b.disabled = on);
  if (input) input.disabled = on;
  // Option rows: all disabled while a request runs; a row whose option was used stays
  // disabled afterwards (data-used), the others come back.
  log.querySelectorAll('.confirm').forEach(row => {
    row.querySelectorAll('button').forEach(b => { b.disabled = on || row.hasAttribute('data-used'); });
  });
  log.querySelectorAll('.example').forEach(b => b.disabled = on);
  if (on) log.setAttribute('aria-busy', 'true'); else log.removeAttribute('aria-busy');
}

function pendingBubble() {
  const p = el('<div class="msg bot pending" aria-busy="true"><span class="spin" aria-hidden="true"></span><span class="pending-text">Working on it…</span></div>');
  log.appendChild(p); scroll();
  return p;
}

function errorBubble(slot, text, body, opts) {
  slot.classList.remove('pending');
  slot.classList.add('error');
  slot.removeAttribute('aria-busy');
  slot.innerHTML = '';
  const box = el('<div class="flagline" role="alert"></div>');
  box.textContent = text;
  slot.appendChild(box);
  const row = el('<div class="confirm"></div>');
  const retry = el('<button type="button" class="ghost">Retry</button>');
  retry.onclick = () => ask(body, {restore: opts.restore, slot});
  row.appendChild(retry);
  if (opts.restore) {
    const edit = el('<button type="button" class="ghost">Edit question</button>');
    edit.onclick = () => {
      const input = document.getElementById('prompt');
      input.value = opts.restore; input.focus();
    };
    row.appendChild(edit);
  }
  slot.appendChild(row);
  scroll();
}

async function ask(body, opts) {
  opts = opts || {};
  if (IN_FLIGHT) return;                       // one request at a time: no double submits
  if (opts.echo) addUser(opts.echo);
  const slot = opts.slot || pendingBubble();
  if (opts.slot) {                             // Retry: the error bubble becomes pending again
    slot.classList.remove('error'); slot.classList.add('pending');
    slot.setAttribute('aria-busy', 'true');
    slot.innerHTML = '<span class="spin" aria-hidden="true"></span><span class="pending-text">Working on it…</span>';
  }
  SLOT = slot;
  const ctl = new AbortController();
  IN_FLIGHT = ctl;
  setBusy(true);
  const t0 = Date.now();
  const tick = setInterval(() => {
    const s = Math.round((Date.now() - t0) / 1000);
    const t = slot.querySelector('.pending-text');
    if (!t) return;
    if (s >= 15) t.textContent = `Still working (${s}s). The first policy question after a restart loads the search model and can take up to a minute.`;
    else if (s >= 3) t.textContent = `Still working — searching the contracts (${s}s)`;
  }, 1000);
  const timer = setTimeout(() => ctl.abort(), ASK_TIMEOUT_MS);
  let data = null, failure = null;
  try {
    let r;
    try {
      r = await fetch('/chat', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                body: JSON.stringify(body), signal: ctl.signal});
    } catch (e) {
      failure = e && e.name === 'AbortError'
        ? `No answer after ${ASK_TIMEOUT_MS / 1000} seconds. The question is kept — try again.`
        : 'Could not reach the server. The question is kept — check the connection and try again.';
    }
    if (!failure) {
      try { data = await r.json(); } catch (e) { data = null; }
      if (!r.ok || !data || data.error || data.detail) {
        const msg = (data && (data.error || data.message
          || (typeof data.detail === 'string' ? data.detail : null)));
        failure = msg || `The server answered with an error (HTTP ${r.status}). The question is kept — try again.`;
      }
    }
  } finally {
    clearInterval(tick); clearTimeout(timer);
  }
  try {
    if (failure) {
      errorBubble(slot, failure, body, opts);
    } else {
      try {
        render(data, opts.restore || body.prompt);
      } catch (e) {
        errorBubble(slot, `The answer arrived but the page could not display it. It is in the audit record for query ${data.query_id || '?'}.`, body, opts);
      }
      // A renderer that forgot to call addBot leaves the pending bubble: say so.
      if (SLOT === slot) {
        errorBubble(slot, `This answer came back in a form the page does not know how to display (mode: ${data.mode || 'unset'}). It is in the audit record for query ${data.query_id || '?'}.`, body, opts);
      }
    }
  } finally {
    SLOT = null; IN_FLIGHT = null;
    setBusy(false);
    const input = document.getElementById('prompt');
    if (input) input.focus();
  }
}

function send() {
  const input = document.getElementById('prompt');
  const prompt = input.value.trim();
  if (!prompt || IN_FLIGHT) return;
  input.value = '';
  return ask({prompt}, {echo: prompt, restore: prompt});
}
window.send = send;   // tour.js drives the page through this

// Answering "which department?" — re-ask the same question, now scoped. The sibling
// options stay disabled once one is used.
function useOption(btn) {
  const row = btn && btn.closest('.confirm');
  if (!row) return;
  row.setAttribute('data-used', '');
  row.querySelectorAll('button').forEach(b => b.disabled = true);
}

function answerDepartment(prompt, queryId, dept, btn) {
  if (IN_FLIGHT) return;
  useOption(btn);
  return ask({prompt, query_id: queryId, department: dept}, {echo: dept, restore: prompt});
}

function confirmDoc(prompt, queryId, docId, btn) {
  if (IN_FLIGHT) return;
  useOption(btn);
  return ask({prompt, query_id: queryId, doc_id: docId},
             {echo: `Use ${docTitle(docId)}`, restore: prompt});
}

// ============================================================================ //
// 3. renderers
// ============================================================================ //
function render(res, prompt) {
  if (res.mode === 'clarify') { renderClarify(res); return; }
  if (res.mode === 'refused') { renderRefused(res); return; }  // costing-correctness (B1)
  if (res.mode === 'entitlement') { renderEntitlement(res); return; }
  // 'lookup' renders exactly like 'policy' — same retrieve-and-quote shape, with the
  // answer read from a rate table instead of prose. Listed explicitly rather than
  // defaulted: a mode this renderer does not know silently renders NOTHING, which is how
  // adding `lookup` server-side left the chat blank while the API returned 200.
  if (res.mode === 'policy' || res.mode === 'lookup') { renderPolicy(res); return; }
  // --- entitlement-retrieval (B6): an off-corpus question is refused by name, not
  // asked which department it is about. Same shape as a policy answer with no sources.
  if (res.mode === 'out_of_scope') { renderPolicy(res); return; }
  if (res.mode === 'blocked') { renderRefusal(res); return; }
  if (res.needs_confirmation) {
    const wrap = el('<div></div>');
    wrap.appendChild(el(`<div>${esc(res.message)}</div>`));
    const c = el('<div class="confirm"></div>');
    (res.options || []).forEach(o => {
      const btn = el('<button type="button" class="ghost"></button>');
      btn.textContent = docTitle(o.doc_id, res) !== o.doc_id ? docTitle(o.doc_id, res) : (o.title || o.doc_id);
      btn.onclick = () => confirmDoc(prompt, res.query_id, o.doc_id, btn);
      c.appendChild(btn);
    });
    wrap.appendChild(c);
    addBot(wrap);
    return;
  }
  // Costing is the last branch, so anything the server sends that this renderer does not
  // know lands here with no `result` and renders a blank bubble — an answer that arrived
  // and was thrown away. Say so instead.
  if (!res.result) {
    addBot(el(`<div class="flagline">This answer came back in a form the page does not
      know how to display (mode: ${esc(res.mode || 'unset')}). The answer is not lost —
      it is in the audit record for query ${esc(res.query_id || '?')}.</div>`));
    return;
  }
  renderCosting(res);
}

// The refusal reads as one plain sentence naming the contract, with one link to an
// admin tab that exists (I6). Built with DOM calls: the message is text, never HTML.
function renderRefusal(res) {
  const wrap = el('<div></div>');
  const box = el('<div class="notice refusal"><div class="refusal-head">Refused, with the reason</div></div>');
  const p = document.createElement('p');
  p.className = 'refusal-text';
  // A server that predates core/refusal.py sends markdown asterisks around an id.
  p.textContent = String(res.message || '').replace(/\*\*/g, '');
  box.appendChild(p);
  if (res.next && res.next.href) {
    const a = document.createElement('a');
    a.href = res.next.href;
    a.className = 'refusal-link';
    a.textContent = res.next.label || 'Open the admin page';
    box.appendChild(a);
  }
  wrap.appendChild(box);
  addBot(wrap);
}

function renderCosting(res) {
  const r = res.result;
  const wrap = el('<div></div>');
  const docs = (res.chosen_docs && res.chosen_docs.length)
    ? res.chosen_docs.map(d => d.title || d.doc_id)
    : String(res.chosen_doc || '').split(',').map(s => docTitle(s.trim(), res)).filter(Boolean);
  const units = (res.bargaining_units || []).join(', ');
  const route = el('<div class="route"></div>');
  route.appendChild(document.createTextNode('Answered under '));
  const strong = document.createElement('strong'); strong.textContent = docs.join(' and ');
  route.appendChild(strong);
  route.appendChild(document.createTextNode(
    (units ? ` · covers ${units}` : '') + ` · ${res.shift_date ? res.shift_date : 'no date given'}`));
  wrap.appendChild(route);
  if (res.interpretation) wrap.appendChild(readAsLine(res.interpretation));  // costing-correctness (B4)

  // The headline is honest about what it is. For ONE classification, the total IS the
  // per-member answer, and the figure itself is the button that opens the audit trail
  // (I2: thumb-sized on a phone). For several, a grand total would be "one member of
  // each class" — a number that corresponds to no real staffing — so the per-member
  // rows are the answer and the sum is labelled as exactly what it is.
  const items = r.line_items || [];
  if (items.length > 1) {
    wrap.appendChild(el(`<div class="total">${fmt(r.total)}
      <span class="muted" style="font-size:.45em; font-weight:400; display:block">
        sum of ONE member of each of the ${items.length} classifications below —
        multiply each row by your staffing to cost a real shift</span></div>`));
  } else if (items.length === 1) {
    const li = items[0];
    const total = el('<div class="total"></div>');
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'amount total-btn';
    btn.textContent = fmtVal(li.total, li.result_type);
    btn.setAttribute('aria-label',
      `${fmtVal(li.total, li.result_type)} per member for ${li.subject} — open the audit trail`);
    btn.onclick = () => openAudit(res.query_id, li, res);
    total.appendChild(btn);
    total.appendChild(el('<span class="muted" style="font-size:.45em; font-weight:400; display:block">per member — click the figure to see how it was calculated</span>'));
    wrap.appendChild(total);
  }

  // Amounts are PER MEMBER of each classification — multiplying by a headcount is the
  // reader's arithmetic, deliberately not ours (PRD §6a).
  const linesWrap = el('<div class="lines-wrap"></div>');
  const table = el(`<table class="lines"><caption>Cost per member, by classification — select an amount to see how it was calculated</caption>
    <thead><tr><th scope="col">Classification</th><th scope="col">Basis</th><th scope="col">Amount (per member)</th></tr></thead><tbody></tbody></table>`);
  const tb = table.querySelector('tbody');
  items.forEach(li => {
    const row = document.createElement('tr');
    const amount = document.createElement('button');
    amount.type = 'button';
    amount.className = 'amount';
    amount.setAttribute('aria-label',
      `${fmtVal(li.total, li.result_type)} per member for ${li.subject} — open the audit trail`);
    amount.textContent = fmtVal(li.total, li.result_type);
    amount.onclick = () => openAudit(res.query_id, li, res);
    const c1 = document.createElement('td'); c1.textContent = li.subject;
    const c2 = document.createElement('td'); c2.textContent = basisLabel(li);
    const c3 = document.createElement('td'); c3.appendChild(amount);
    row.append(c1, c2, c3);
    tb.appendChild(row);
  });
  appendUncoveredRows(tb, r.uncovered);  // costing-correctness (B2)
  linesWrap.appendChild(table);
  wrap.appendChild(linesWrap);

  items.filter(li => li.needs_human_confirmation).forEach(li => {
    (li.flags || []).forEach(f => {
      wrap.appendChild(el(`<div class="flagline">⚑ <strong>${esc(li.subject)}:</strong>
        ${esc(f.message)} (primary ${fmt(f.primary)}, alternate ${fmt(f.alternate)})</div>`));
    });
  });
  wrap.appendChild(el('<div class="muted" style="margin-top:8px">Click any amount to open its audit trail.</div>'));
  addBot(wrap);
}

// "Basis" cell: the rule's topic in plain words plus where it comes from — never the
// rule id (that is one click away under Technical details in the drawer).
function basisLabel(li) {
  const c = (li.citations || [])[0];
  const topic = li.topic ? li.topic.replace(/[_-]+/g, ' ') : 'approved rule';
  const t = topic.charAt(0).toUpperCase() + topic.slice(1);
  return c ? `${t} — ${cite(c.clause, c.page)}` : t;
}

// "Which department?" — the answer differs per contract, so ask rather than pick.
function renderClarify(res) {
  const wrap = el('<div></div>');
  wrap.appendChild(el(`<div class="policy-answer">${esc(res.question)}</div>`));
  const row = el('<div class="confirm"></div>');
  (res.options || []).forEach(d => {
    const b = el(`<button type="button" class="ghost">${esc(d)}</button>`);
    // costing-correctness (B1/B4): a field-specific clarify posts back `clarified`
    b.onclick = () => res.field ? answerClarified(res, d, b) : answerDepartment(res.prompt_echo, res.query_id, d, b);
    row.appendChild(b);
  });
  wrap.appendChild(row);
  if (res.considered && res.considered.length) {
    wrap.appendChild(el(`<div class="muted" style="margin-top:8px">Matching documents span:
      ${esc([...new Set(res.considered.map(c => c.department).filter(Boolean))].join(', '))}</div>`));
  }
  addBot(wrap);
}

// Non-money rule answers: "5 days per §11.3" — same engine, same proof, different unit.
function renderEntitlement(res) {
  const r = res.result || {};
  const wrap = el('<div></div>');
  wrap.appendChild(el(`<div class="route">Rule answer · ${esc(res.department || 'corpus')} ·
    computed from approved rules</div>`));
  const linesWrap = el('<div class="lines-wrap"></div>');
  const t = el(`<table class="lines"><caption>Answer by employee — select an answer to see the rule and clause behind it</caption>
    <thead><tr><th scope="col">Employee</th><th scope="col">Basis</th><th scope="col">Answer</th></tr></thead><tbody></tbody></table>`);
  const tb = t.querySelector('tbody');
  (r.line_items || []).forEach(li => {
    const row = document.createElement('tr');
    const val = document.createElement('button');
    val.type = 'button';
    val.className = 'amount';
    val.setAttribute('aria-label',
      `${fmtVal(li.total, li.result_type)} for ${li.subject} — open the audit trail`);
    val.textContent = fmtVal(li.total, li.result_type);
    val.onclick = () => openAudit(res.query_id, li, res);
    const c1 = document.createElement('td'); c1.textContent = li.subject;
    const c2 = document.createElement('td'); c2.textContent = basisLabel(li);
    const c3 = document.createElement('td'); c3.appendChild(val);
    row.append(c1, c2, c3);
    tb.appendChild(row);
  });
  linesWrap.appendChild(t);
  wrap.appendChild(linesWrap);
  wrap.appendChild(el('<div class="muted" style="margin-top:8px">Click the answer to see the rule and the clause it came from.</div>'));
  addBot(wrap);
}

// How the policy text was produced, in the reader's words (I4). Keyed by the server's
// answer_source so the header and the AI panel below it can never disagree.
const POLICY_HOW = {
  claude: 'written by Claude from the clauses below',
  guarded: 'model draft discarded — quoted word for word',
  stub: 'quoted word for word (no model)',
  none: 'nothing found to quote',
};

function renderPolicy(res) {
  const wrap = el('<div></div>');
  const scope = res.department ? `${res.department} documents` : 'the corpus';
  const n = (res.considered || []).length;
  const kind = res.mode === 'lookup' ? 'Published figure' : 'Policy answer';
  const how = POLICY_HOW[res.answer_source] || '';
  wrap.appendChild(el(`<div class="route">${kind} · searched <strong>${n}</strong>
    of ${esc(res.corpus_size || n)} documents (${esc(scope)})${how ? ` · ${esc(how)}` : ''}</div>`));
  wrap.appendChild(el(`<div class="policy-answer">${esc(res.answer)}</div>`));
  // These are the answers a model may actually have WRITTEN, so this is where "was that
  // AI?" matters most — the costing drawer is not reachable from here (no line items).
  const ai = el('<div style="margin-top:8px"></div>');
  wrap.appendChild(ai);
  renderAiTrail(res.query_id, ai, res.mode === 'lookup' ? 'lookup' : 'policy', res.answer_source);
  if (res.sources && res.sources.length) {
    wrap.appendChild(el('<div class="muted" style="margin:10px 0 4px">Read it yourself — click a section to see it on the page:</div>'));
    res.sources.forEach(s => {
      const btn = el(`<button type="button" class="source-chip"><strong>${esc(s.title || docTitle(s.doc_id, res))}</strong>
        <span class="tag">${esc(s.department || '')}</span> <strong>${esc(cite(s.clause, s.page))}</strong>${tierChip(s.tier)}<br>
        <span class="src-text">${esc(s.text || '')}</span></button>`);
      btn.onclick = () => openSource(s, res);
      wrap.appendChild(btn);
    });
  }
  if (res.considered && res.considered.length > 1) {
    const used = new Set((res.sources || []).map(s => s.doc_id));
    const others = res.considered.filter(c => !used.has(c.doc_id));
    if (others.length) {
      wrap.appendChild(el(`<details style="margin-top:8px"><summary class="muted">
        Also considered (${others.length}) — not used</summary>
        <div class="muted" style="margin-top:4px">${others.map(o =>
          esc(o.title || o.doc_id)).join(' · ')}</div></details>`));
    }
  }
  addBot(wrap);
}

// ============================================================================ //
// 4. audit drawer
//    openSource(): one quoted passage on its page.
//    openAudit(): header -> decision trace -> citations -> technical details -> AI
//    panel. Wave 2 (search-tree, replay) extends the decision-trace block; keep the
//    order and the block comments.
// ============================================================================ //
function openSource(s, res) {
  const body = document.getElementById('drawerBody');
  body.innerHTML = '';
  const h = document.createElement('h3');
  h.textContent = `${s.title || docTitle(s.doc_id, res)} — ${cite(s.clause, s.page)}`;
  body.appendChild(h);
  body.appendChild(el(`<div class="muted">${esc(cite('', s.page))}${tierChip(s.tier)}</div>`));
  const quote = el('<div class="trace-step"></div>'); quote.textContent = s.text || '';
  body.appendChild(quote);
  const box = (s.bbox || []).join(',');
  const c = el(`<div class="cite"><div class="muted">Source section highlighted on the PDF:</div></div>`);
  const img = new Image(); img.src = `/doc/${s.doc_id}/page/${s.page}?bbox=${box}`;
  img.alt = `Page ${s.page} of ${s.title || s.doc_id} with the quoted passage outlined in red`;
  img.onerror = () => { img.remove(); c.appendChild(el('<div class="muted">(page render unavailable)</div>')); };
  c.appendChild(img);
  body.appendChild(c);
  body.appendChild(el(`<details class="tech"><summary>Technical details</summary>
    <div class="muted mono">document id ${esc(s.doc_id)} · page ${esc(s.page)} · search score ${esc(s.score ?? '')}</div></details>`));
  openDrawer();
}

// Plain labels for the engine's trace kinds (I4). Unknown kinds show their raw name.
const TRACE_LABELS = {
  'selector-considered': 'Rule checked',
  'selector-chosen': 'Rule applied',
  'math': 'Arithmetic',
  'modifier': 'Rate adjustment',
  'premium': 'Added premium',
  'flag': 'Needs a human decision',
};
const SCOPE_RE = /\s*\(scope \d+, priority \d+\)/g;

// The visible trace line: no ids, no scope/priority, money formatted. The raw detail
// is kept verbatim under Technical details.
function traceText(t, li) {
  let d = String(t.detail || '').replace(SCOPE_RE, '');
  if (li && li.rule_id) d = d.split(li.rule_id).join('this rule');
  if (t.kind === 'math') {
    d = d.replace(/=\s*(-?\d+(?:\.\d+)?)\s*$/, (m, n) => `= ${fmtVal(n, li ? li.result_type : 'currency')}`);
  }
  return d;
}

async function openAudit(queryId, li, res) {
  const body = document.getElementById('drawerBody');
  body.innerHTML = '';
  const cites = li.citations || [];
  const trace = li.trace || [];

  // --- header: who, how much, under which contract ---------------------------
  const h = document.createElement('h3');
  h.textContent = `${li.subject} — ${fmtVal(li.total, li.result_type)}`;
  body.appendChild(h);
  const under = cites.length
    ? `${cites[0].title || docTitle(cites[0].doc_id, res)}, ${cite(cites[0].clause, cites[0].page)}`
    : (li.topic || 'approved rule');
  const sub = el('<div class="muted"></div>');
  sub.textContent = `Rule applied: ${under}`;
  body.appendChild(sub);

  // --- decision trace (wave 2: search tree + replay button extend THIS block) ---
  const traceBlock = el('<div class="trace-block"></div>');
  trace.forEach(t => {
    const cls = t.kind === 'selector-chosen' ? 'chosen' : (t.kind === 'flag' ? 'flag' : '');
    const step = el(`<div class="trace-step ${cls}"><span class="k">${esc(TRACE_LABELS[t.kind] || t.kind)}</span><br></div>`);
    step.appendChild(document.createTextNode(traceText(t, li)));
    traceBlock.appendChild(step);
  });
  body.appendChild(traceBlock);

  // --- citations with the bbox highlighted on the page ----------------------
  const seen = new Set();
  cites.forEach(c => {
    const key = c.doc_id + c.clause;
    if (seen.has(key)) return; seen.add(key);
    const box = (c.bbox || []).join(',');
    const url = `/doc/${c.doc_id}/page/${c.page}?bbox=${box}`;
    const title = c.title || docTitle(c.doc_id, res);
    const citeBox = el('<div class="cite"></div>');
    const label = el('<div class="muted"></div>');
    label.textContent = `Source: ${title}, ${cite(c.clause, c.page)}`;
    label.insertAdjacentHTML('beforeend', tierChip(c.tier));
    citeBox.appendChild(label);
    const img = new Image(); img.src = url;
    img.alt = `Page ${c.page} of ${title} with the cited clause outlined in red`;
    img.onerror = () => { img.remove(); citeBox.appendChild(el('<div class="muted">(page render unavailable — bbox: ' + esc(box) + ')</div>')); };
    citeBox.appendChild(img);
    body.appendChild(citeBox);
  });

  // --- technical details: ids, scope/priority, raw trace lines ----------------
  const tech = el('<details class="tech"><summary>Technical details</summary></details>');
  const ids = el('<div class="muted mono"></div>');
  ids.textContent = `rule ${li.rule_id || '?'} · query ${queryId || '?'}`
    + (cites.length ? ` · document ${cites.map(c => c.doc_id).join(', ')}` : '');
  tech.appendChild(ids);
  trace.forEach(t => {
    const raw = el('<div class="muted mono"></div>');
    raw.textContent = `${t.kind}: ${t.detail || ''}`;
    tech.appendChild(raw);
  });
  body.appendChild(tech);

  openDrawer();
  // --- AI involvement (section 5) --------------------------------------------
  renderAiTrail(queryId, body, li.result_type === 'currency' ? 'costing' : 'entitlement');
}

// ============================================================================ //
// 5. AI-involvement panel (I3)
//    "How did the AI reach this answer?" begins with whether AI was involved at all.
//    Every touchpoint has a deterministic fallback, so with an expired key the product
//    answers exactly as before and says nothing — this section is what makes that
//    visible. The headline AND the caveat are both chosen from what actually ran, so
//    the panel can never say "No AI was used" and "written by the AI" in one breath.
// ============================================================================ //
const FN_LABELS = {
  classify_intent: 'Decide what kind of question this is',
  parse_intent: 'Read who, hours and date from the question',
  extract_department: 'Work out the department',
  answer_policy: 'Write the answer',
  'answer_policy/lookup': 'Read the figure out of the document',
  rank_documents: 'Pick the governing document',
};

function caveat(mode, ai, answerSource) {
  if (mode === 'policy' || mode === 'lookup') {
    switch (answerSource) {
      case 'claude':
        return mode === 'lookup'
          ? `The figure above was <strong>read out of the document by the AI</strong>, not
             computed — and not covered by an approved rule. It is quoted from the row shown
             below: check it against the source. Nothing here was calculated.`
          : `The answer above was <strong>written by the AI</strong>, composed only from the
             clauses shown below. It quotes the contract; it does not compute anything. Read the
             cited sections yourself.`;
      case 'guarded':
        return `The model's draft contained a figure that is not in the retrieved clauses, so it
          was discarded. What you see is the top-ranked passage, quoted word for word.`;
      case 'stub':
        return `<strong>No model wrote this.</strong> It is the top-ranked passage quoted word
          for word, and it may not answer the question. Read the sections below.`;
      case 'none':
        return '';
      default:
        return ai && ai.used_model
          ? `The answer above was <strong>written by the AI</strong>, composed only from the
             clauses shown below.`
          : `<strong>No model wrote this.</strong> It is the top-ranked passage quoted word for word.`;
    }
  }
  // costing / entitlement: the engine computed the figure either way
  const failed = ai && ai.errors && ai.errors.length
    ? 'The model was called and failed; deterministic code took over. ' : '';
  if (ai && ai.used_model) {
    return failed + `<strong>No figure above was produced by a model.</strong> The AI only routed
      the question and read it into structured fields. Every amount was computed by the
      deterministic engine from human-approved rules.`;
  }
  return failed + `<strong>No model was involved.</strong> The question was read by deterministic
    code and the ${mode === 'entitlement' ? 'answer' : 'amount'} was computed by the engine from human-approved rules.`;
}

// Identical consecutive rows (same fn, source, rule) collapse into one with "×N": a
// department follow-up reuses the query id and would list classify_intent twice.
function collapseCalls(calls) {
  const out = [];
  for (const c of calls) {
    const last = out[out.length - 1];
    if (last && last.c.fn === c.fn && last.c.source === c.source
        && (last.c.rule || '') === (c.rule || '') && (last.c.model || '') === (c.model || '')) {
      last.n += 1;
    } else {
      out.push({c, n: 1});
    }
  }
  return out;
}

async function renderAiTrail(queryId, body, mode, answerSource) {
  let ai;
  try {
    const r = await fetch(`/chat/audit/${queryId}`);
    if (!r.ok) return;
    ai = (await r.json()).ai;
  } catch (e) { return; }
  if (!ai || !(ai.calls || []).length) return;
  ai.errors = ai.errors || []; ai.fell_back = ai.fell_back || [];

  const rows = collapseCalls(ai.calls).map(({c, n}) => {
    const what = c.source === 'claude'
      ? `<span class="ok">AI</span> · ${esc(c.model)} · ${esc(c.ms)}ms`
      : c.source === 'error'
      ? `<span class="bad">AI failed</span> · ${esc(c.error || '')}`
      : `<span class="warn-text">no AI</span> · ${esc(c.rule || 'deterministic fallback')}`;
    const label = FN_LABELS[c.fn] || c.fn;
    return `<div class="trace-step"><span class="k">${esc(label)}${n > 1 ? ` ×${n}` : ''}</span><br>${what}</div>`;
  }).join('');

  const headline = ai.errors.length
    ? `<span class="bad">The model failed on this answer</span> — deterministic code answered instead.`
    : ai.fell_back.length && !ai.used_model
    ? `<span class="warn-text">No AI was used.</span> Deterministic fallbacks produced this answer.`
    : ai.fell_back.length
    ? `<span class="warn-text">Partly AI.</span> ${ai.fell_back.length} step(s) fell back to deterministic code.`
    // Reports involvement only. What the AI was ALLOWED to do differs by mode and is
    // stated in the caveat below — saying "translation only" here would be false on a
    // lookup, where the model reads the figure itself.
    : `AI ran ${ai.calls.length} step(s), ${esc(ai.total_ms)}ms total.`;

  const cav = caveat(mode, ai, answerSource);
  body.appendChild(el(`<details class="ai-panel" style="margin-top:10px" open>
    <summary><strong>AI involvement</strong></summary>
    <p class="muted" style="margin:6px 0">${headline}</p>
    ${rows}
    ${cav ? `<p class="muted">${cav}</p>` : ''}
  </details>`));
}

// ============================================================================ //
// 6. drawer open/close, home-page examples, ?q= deep link
// ============================================================================ //
let LAST_FOCUS = null;
function openDrawer() {
  LAST_FOCUS = document.activeElement;
  const d = document.getElementById('drawer');
  d.hidden = false; d.classList.add('open');
  d.querySelector('button').focus();
}
function closeDrawer() {
  const d = document.getElementById('drawer');
  d.classList.remove('open'); d.hidden = true;
  if (LAST_FOCUS) LAST_FOCUS.focus();
}
window.closeDrawer = closeDrawer;
document.addEventListener('keydown', e => {
  const d = document.getElementById('drawer');
  if (e.key === 'Escape' && !d.hidden) closeDrawer();
});

// Home-page examples are buttons (I16): one tap fills the box and asks. The three
// questions are the three behaviours — a number, a quoted clause, a refusal — and
// tests/test_examples.py checks each still answers that way keyless.
log.addEventListener('click', e => {
  const b = e.target.closest('button.example');
  if (!b || b.disabled) return;
  const q = b.getAttribute('data-q');
  if (!q) return;
  const input = document.getElementById('prompt');
  input.value = q;
  send();
});



// --- costing-correctness (B1, B2, B4) ---------------------------------------------- //
// Every answer opens with what the engine was handed (B4). Plain text, no markdown.
function readAsLine(interp) {
  return el(`<div class="route read-as">Read as: <strong>${esc(interp.read_as || '')}</strong></div>`);
}

// Rows for the classifications that were NOT priced (B2): the reason, no amount button.
function appendUncoveredRows(tb, uncovered) {
  (uncovered || []).forEach(u => {
    const row = document.createElement('tr');
    const c1 = document.createElement('td'); c1.textContent = u.subject;
    const c2 = document.createElement('td'); c2.textContent = u.bargaining_unit || '';
    const c3 = document.createElement('td');
    const span = document.createElement('span');
    span.className = 'muted';
    span.textContent = u.reason || 'Not covered';
    c3.appendChild(span);
    row.append(c1, c2, c3);
    tb.appendChild(row);
  });
}

// A clarifying answer goes back as {prompt, query_id, clarified: {<field>: value}} — the
// server reads only the field it asked for and only a value it can verify.
// Goes through ask() (chat-ui I1) so the pending bubble, disabled controls and error
// handling are the same as for a typed question.
function answerClarified(res, value, btn) {
  if (IN_FLIGHT) return;
  if (btn) useOption(btn);
  const clarified = {};
  clarified[res.field] = value;
  return ask({prompt: res.prompt_echo, query_id: res.query_id, clarified},
             {echo: value, restore: res.prompt_echo});
}

// A refusal (B1): the reason, what IS approved for the unit (as a chip that opens the
// clause), and the closest passages labelled as text — never a dollar figure.
function renderRefused(res) {
  const wrap = el('<div></div>');
  if (res.interpretation) wrap.appendChild(readAsLine(res.interpretation));
  wrap.appendChild(el(`<div class="flagline">${esc(res.message || res.reason || 'Not computed.')}</div>`));
  const approved = (res.approved_topics || []).join(', ') || 'none';
  wrap.appendChild(el(`<div style="margin-top:8px">Approved for this unit: <strong>${esc(approved)}</strong></div>`));
  (res.approved_clauses || []).forEach(s => {
    const btn = el(`<button type="button" class="source-chip"><strong>${esc(s.title || s.doc_id)}</strong>
      <span class="tag">${esc(s.topic || '')}</span> <strong>${esc(s.clause || '')}</strong>
      <span class="muted"> p.${esc(s.page)}</span><br>
      <span class="src-text">${esc(s.text || '')}</span></button>`);
    btn.onclick = () => openSource({...s, score: 'approved rule'});
    wrap.appendChild(btn);
  });
  if (res.nearest && res.nearest.length) {
    wrap.appendChild(el(`<div class="muted" style="margin:10px 0 4px">${esc(res.nearest_label || 'closest text — not a computed answer')}:</div>`));
    res.nearest.forEach(s => {
      const btn = el(`<button type="button" class="source-chip"><strong>${esc(s.title || s.doc_id)}</strong>
        <span class="muted"> p.${esc(s.page)}</span>${tierChip(s.tier)}<br>
        <span class="src-text">${esc(s.text || '')}</span></button>`);
      btn.onclick = () => openSource(s);
      wrap.appendChild(btn);
    });
  }
  addBot(wrap);
}

// `/?q=…` asks once on load.
(function deepLink() {
  let q = '';
  try { q = new URLSearchParams(location.search).get('q') || ''; } catch (e) { return; }
  if (!q.trim()) return;
  const input = document.getElementById('prompt');
  input.value = q.trim();
  send();
})();
