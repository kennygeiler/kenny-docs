// Guided demo tour: one shared step engine, per-page step lists. Loaded by BOTH
// chat.html and admin.html; which list runs is keyed by <body data-page>. The visitor
// presses one button and the tour performs the clicks — asking the questions, opening
// the drawer, stepping the admin viewers, loading and trying to approve the demo draft —
// with a Next button to advance.
//
// Everything in this file is inert until startTour() runs: the only load-time work is
// wiring the start button and the ?tour=1 resume hook, so the tour has zero effect on
// normal use. Any selector that has moved skips its step with a console.warn — the tour
// must never crash or trap a visitor.
//
// Hardening (DEMO_TICKETS.md I5):
//   - every step action gets ctx.alive() and re-checks it after each await, so a Skip
//     (or Esc) mid-action never lets stale work click or send on the next step;
//   - waits are on real DOM signals (the pending bubble, the drawer, the table), never
//     fixed timeouts; a slow answer shows a visible "still working" note and the step
//     finishes by itself when the element arrives;
//   - a target that measures 0x0 or is detached counts as missing; anchors are re-found
//     after a re-render (the admin page rebuilds its lists with innerHTML);
//   - the card is placed by least overlap with the target and with `avoid` (the cited
//     page image, the X-ray stage), so it never covers the boxed clause;
//   - bodies may be functions, so the card narrates what is on screen right now.
(() => {
  'use strict';

  const PAGE = document.body.dataset.page
    || (location.pathname.startsWith('/admin') ? 'admin' : 'chat');

  /* ------------- tiny helpers (no dependence on either page's globals) ------------- */
  const q = (sel, root = document) => { try { return root.querySelector(sel); } catch (e) { return null; } };
  const qa = (sel, root = document) => { try { return [...root.querySelectorAll(sel)]; } catch (e) { return []; } };
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const SMOOTH = matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';
  const text = n => ((n && n.textContent) || '').replace(/\s+/g, ' ').trim();
  const money = v => {
    const n = Number(String(v).replace(/[^0-9.-]/g, ''));
    return Number.isFinite(n) ? '$' + n.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2}) : String(v);
  };

  // Poll a predicate on a real signal. `every` is short so a Skip is honoured quickly.
  async function poll(fn, timeout, every = 150) {
    const t0 = Date.now();
    for (;;) {
      let v = null;
      try { v = fn(); } catch (e) { v = null; }
      if (v) return v;
      if (Date.now() - t0 > timeout) return null;
      await sleep(every);
    }
  }

  // An element the visitor can actually see: attached, laid out, non-zero.
  function visible(el) {
    if (!el || !el.isConnected) return false;
    if (el.offsetParent === null && getComputedStyle(el).position !== 'fixed') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 || r.height > 0;
  }

  // Visible fast-type effect — the demo should look driven, not teleported.
  async function typeInto(input, value, ctx) {
    input.focus();
    input.value = '';
    for (let i = 0; i < value.length; i += 3) {
      if (ctx && !ctx.alive()) return false;
      input.value = value.slice(0, i + 3);
      await sleep(8);
    }
    input.value = value;
    return true;
  }

  // Chat: the pending bubble (app.js I1) is the one real "a request is in flight" signal.
  const chatIdle = () => !q('#log .msg.pending');

  // Ask a question the way a visitor would: the home-page example button when one carries
  // exactly this question (wave-1 UI), else typed into the box and sent.
  async function askQuestion(question, ctx) {
    await poll(chatIdle, 30000);
    if (!ctx.alive()) return;
    const ex = qa('#log button.example').find(b => (b.getAttribute('data-q') || '') === question);
    if (ex && !ex.disabled) {
      ex.classList.add('tour-glow');
      await sleep(250);
      ex.classList.remove('tour-glow');
      if (!ctx.alive()) return;
      ex.click();
      return;
    }
    const input = q('#prompt');
    if (!input) return;
    if (!(await typeInto(input, question, ctx))) return;
    await sleep(120);
    if (!ctx.alive()) return;
    if (typeof window.send === 'function') window.send();
    else { const f = q('#askForm'); if (f && f.requestSubmit) f.requestSubmit(); }
  }

  // The costing answer the drawer steps need. A Skip pressed while the tour was still
  // typing the question aborts the send (never send after a skip), so the next step
  // asks it itself — once, and only when nothing is in flight.
  async function ensureCostingAnswer(ctx) {
    await poll(chatIdle, 30000);
    if (!ctx.alive()) return;
    if (!qa('#log .msg.bot .total').length) await askQuestion(QUESTION_COSTING, ctx);
  }

  // Find a document-card button by its onclick prefix, preferring one whose doc id
  // (in the onclick attribute) matches `re`, then one whose card text does — the tour
  // survives documents being reordered or retitled. (Matching card TEXT first was a
  // bug: the salary schedule's summary mentions "firefighter" rows.)
  function docButton(prefix, re) {
    const btns = qa(`#docs button[onclick^="${prefix}"]`);
    return btns.find(b => re.test(b.getAttribute('onclick') || ''))
        || btns.find(b => re.test((b.closest('.card') || b).textContent || ''))
        || btns[0] || null;
  }

  const closeDrawerIfOpen = () => {
    if (q('#drawer.open') && typeof window.closeDrawer === 'function') window.closeDrawer();
  };
  const closeXrayIfOpen = () => {
    if (q('#xray:not([hidden])') && typeof window.closeXray === 'function') window.closeXray();
  };

  // Record which dialogs the tour itself opened, so ending mid-way closes them and
  // never leaves the visitor under a modal they did not ask for.
  const mark = tag => { if (state) state.opened.add(tag); };

  const QUESTION_COSTING =
    'Cost an 8-hour overtime shift for a Firefighter/Paramedic (56 hr, top step)';
  const QUESTION_POLICY =
    'How is premium overtime compensated under the Firefighters Local 3535 MOU?';
  // The $640.80 rule and the seeded wrong draft that the gate must refuse (I15).
  const LIVE_OT_ID = 'firefighters_local3535_mou:overtime_premium_rate';
  const DEMO_ID = 'firefighters_local3535_mou:overtime_double_time_draft';
  const SHOWCASE_DOC = /3535|firefighter/i;

  const lastBot = () => qa('#log .msg.bot').pop() || null;
  const demoChk = () => q(`.ruleChk[data-id="${DEMO_ID}"]`);
  const demoCard = () => { const c = demoChk(); return c ? c.closest('.card') : null; };
  const libraryCard = id => qa('#library .card.rule')
    .find(c => text(c.querySelector('.id')) === id || !!c.querySelector(`button[data-id="${id}"]`)) || null;

  /* ---------------- step lists, keyed by page ----------------
     A step: { title, body (string | fn -> string), target (selector | fn -> element |
               absent = centered card), avoid (selector kept clear of the card),
               pre (async action, receives ctx {alive}), waitFor (selector; waitForNew
               waits for a NEW match, not a leftover), waitTimeout, timeoutNote,
               nextLabel, advance (replaces plain Next) }. */
  const STEPS = {
    chat: [
      {
        title: 'Welcome to Kenny',
        body: () => {
          const badge = text(q('#keyBadge'));
          return 'Kenny answers HR costing and policy questions straight from contract '
            + 'PDFs. The badge in the header' + (badge ? ` reads “${badge}”: it` : '')
            + ' says who is answering — Claude, or deterministic code. The money math is '
            + 'deterministic either way. Press Next and the tour will drive; Skip moves '
            + 'past a slow step, Esc ends it any time.';
        },
        target: '#keyBadge',
      },
      {
        title: 'Three kinds of answer',
        body: 'The three example buttons are the three behaviours: a number, a quoted '
          + 'clause, and a refusal. Each is a question the product answers correctly '
          + 'today, with no key. The tour presses the first two.',
        target: '#log .examples',
      },
      {
        title: 'Watch a costing question',
        body: () => {
          const t = qa('#log .msg.bot .total').pop();
          const amt = t ? text(t.querySelector('.amount') || t).split(' per member')[0] : '';
          return (amt ? `${amt} ` : 'That figure ') + 'was computed by a deterministic '
            + 'engine from human-approved rules — the question was only read into '
            + 'structured fields. No model produced the number.';
        },
        pre: ctx => askQuestion(QUESTION_COSTING, ctx),
        waitFor: '#log .msg.bot .total',
        waitForNew: true,
        waitTimeout: 30000,
        timeoutNote: 'The answer is taking longer than usual. It will appear in the '
          + 'conversation, and this step will continue when it does.',
        target: () => qa('#log .msg.bot .total').pop(),
      },
      {
        title: 'How the question was read',
        body: () => {
          const line = qa('#log .msg.bot .read-as').pop();
          const read = text(line).replace(/^Read as:\s*/, '');
          return 'Every answer opens with what the engine was handed'
            + (read ? `: “${read}”.` : '.')
            + ' Hours, pay type, classification and date — if one of these is missing, '
            + 'Kenny asks instead of guessing.';
        },
        // A Skip pressed while the answer is still in flight lands here: wait for the
        // same real signal instead of skipping the step because the line is not there yet.
        pre: ensureCostingAnswer,
        waitFor: '#log .msg.bot .read-as',
        waitTimeout: 30000,
        timeoutNote: 'The answer is taking longer than usual. It will appear in the '
          + 'conversation, and this step will continue when it does.',
        target: () => qa('#log .msg.bot .read-as').pop(),
      },
      {
        title: 'Every number opens its audit trail',
        body: 'Clicking an amount opens the drawer: the decision trace (which rules were '
          + 'checked, which was applied, the arithmetic), the source clause boxed in red '
          + 'on the rendered page, and whether AI was involved at any step. The "number '
          + 'I can defend", made literal.',
        pre: async ctx => {
          // A request may still be in flight (Skip pressed early): wait for the real
          // signal, never double-send. If no answer exists and nothing is pending, ask.
          await ensureCostingAnswer(ctx);
          if (!ctx.alive()) return;
          const a = await poll(() => chatIdle() && qa('#log .msg.bot .amount').pop(), 30000);
          if (!ctx.alive()) return;
          if (a) { mark('drawer'); a.click(); }
        },
        waitFor: '#drawer.open',
        waitTimeout: 35000,
        timeoutNote: 'The answer is taking longer than usual. The drawer opens from the '
          + 'figure once it lands in the conversation.',
        target: () => q('#drawer .trace-block') || q('#drawer .db'),
        avoid: '#drawer .cite img',
      },
      {
        title: 'Where the cited text came from',
        body: () => {
          const chip = text(q('#drawer .tier-chip'));
          return 'Every citation says how its text was obtained'
            + (chip ? ` — this one reads “${chip}”.` : '.')
            + ' These contracts arrived as scanned paper (the chip says "OCR\'d scan") and '
            + 'were OCR\'d before ingest: the positions are exact, the characters are OCR '
            + 'output, so check them against the page image below.';
        },
        target: '#drawer .tier-chip',
        avoid: '#drawer .cite img',
      },
      {
        // Present only when the ledger chunk's replay control is in the drawer.
        title: 'Replay it from the record',
        body: 'Every answer is frozen with its inputs and rule text. Replay recomputes it '
          + 'from that snapshot and must land on the same figure.',
        target: () => q('#drawer .replay, #drawer [data-replay], #drawer button[data-action="replay"]'),
        avoid: '#drawer .cite img',
        optional: true,
      },
      {
        title: 'Policy answers quote, never compute',
        body: () => {
          const m = lastBot();
          const chip = m ? text(m.querySelector('.source-chip .tier-chip')) : '';
          const first = m ? m.querySelector('.source-chip') : null;
          const where = first ? qa('strong', first).map(text).join(', ') : '';
          const p8 = /\bp\.\s*8\b/.test(where);
          return 'A policy answer is composed only from the clauses shown beneath it. Each '
            + 'source chip names the contract and page' + (chip ? ` and carries the same “${chip}” chip` : '')
            + '; clicking one opens the clause highlighted on its page — read it yourself.'
            + (where ? ` The top source is ${where}` : '')
            + (p8 ? ' — the same clause the engine boxed a moment ago.' : (where ? '.' : ''));
        },
        pre: async ctx => {
          closeDrawerIfOpen();
          await askQuestion(QUESTION_POLICY, ctx);
        },
        waitFor: '#log .source-chip',
        waitForNew: true,
        waitTimeout: 30000,
        timeoutNote: 'The quoted answer is taking longer than usual (the first policy '
          + 'question after a restart loads the search model). It will appear in the '
          + 'conversation, and this step will continue when it does.',
        target: () => { const m = lastBot(); return m ? m.querySelector('.source-chip') : null; },
      },
      {
        title: 'Now the ops side',
        body: 'Everything you just saw rests on an admin loop: documents are read and '
          + 'checked by eye, rules are drafted against known answers and human-approved, '
          + 'and every event lands in a hash-chained ledger. Next takes you there.',
        target: 'header nav a[href="/admin"]',
        nextLabel: 'Go to Admin',
        advance: () => { location.href = '/admin?tour=1'; },
      },
    ],

    admin: [
      {
        title: 'The extraction scorecard',
        body: () => {
          const parts = ['Every document Kenny holds, with how well the machine read it: '
            + 'the green badge names the parser, the counts show clauses and table rows'];
          if (q('#docs .score-origin')) parts.push(`, “${text(q('#docs .score-origin'))}” says the text is an OCR layer over scanned paper`);
          if (q('#docs .score-sha')) parts.push(', and the grey code is the SHA-256 of the exact PDF ingested');
          parts.push('.');
          // The showcase document's own badge (the firefighters MOU), not the first card's.
          const sc = q('#docs .btn-showcase');
          const v = (sc && sc.closest('.card') || document).querySelector('#docs .score-verified, .score-verified');
          if (v) parts.push(` “${text(v)}” is where a second OCR read disagrees — next.`);
          return parts.join('');
        },
        waitFor: '#docs .score',
        waitTimeout: 20000,
        target: '#docs .score',
      },
      {
        title: 'X-ray: proof the machine read it',
        body: 'This is the Firefighters MOU with every extracted clause boxed on the '
          + 'page it came from — the machine\'s reading laid over the paper. The arrows '
          + 'above step through the pages; hover a box to read what was extracted from it.',
        pre: async ctx => {
          await poll(() => q('#docs button[onclick^="openXray"]'), 15000);
          if (!ctx.alive()) return;
          const b = docButton('openXray', SHOWCASE_DOC);
          if (b) { mark('xray'); b.click(); }
        },
        waitFor: '#xrayStage .xbox',
        waitTimeout: 15000,
        target: '#xrayStage',
        avoid: '#xrayStage',
      },
      {
        // D4 showpiece: the one click that lands on the vacation table on p.22, where the
        // OCR layer misread 10.15 as "0) £5" and 468 as "A468".
        title: () => q('#docs .btn-showcase') ? 'Compare: the vacation table on p.22' : 'Compare: check the extraction by eye',
        body: () => {
          const cells = qa('#xrayText td.xcell-disputed').map(td => {
            const re = td.querySelector('.xreread');
            const stored = text(td).replace(re ? text(re) : '', '').trim();
            return {stored, reread: re ? text(re) : ''};
          }).filter(c => c.stored);
          if (!cells.length) {
            return 'Same page, split view: the rendered PDF left, the extracted text in reading '
              + 'order right. Hovering either side lights its counterpart. No cell on this page '
              + 'is disputed.';
          }
          const named = cells.slice(0, 3)
            .map(c => `“${c.stored}”` + (c.reread ? ` where the page says ${c.reread}` : '')).join(', ');
          return `This is the scan's vacation accrual table, rebuilt as the grid it was on paper. `
            + `${cells.length} amber cell${cells.length === 1 ? '' : 's'}: the OCR layer stored ${named}. `
            + 'Amber is where a second OCR engine read the page image and disagrees with what was '
            + 'stored; its reading sits beside the stored value, never written over it.';
        },
        pre: async ctx => {
          closeXrayIfOpen();
          await poll(() => q('#docs button[onclick^="openCompare"]'), 15000);
          if (!ctx.alive()) return;
          const b = q('#docs .btn-showcase') || docButton('openCompare', SHOWCASE_DOC);
          if (b) { mark('xray'); b.click(); }
        },
        waitFor: '#xrayText .xtable, #xrayText .xrow',
        waitTimeout: 20000,
        timeoutNote: 'The page is taking longer than usual to render. It will appear in the '
          + 'split view, and this step will continue when it does.',
        target: () => q('#xrayText .xtable-wrap') || q('#xrayText'),
        avoid: '#xrayStage',
      },
      {
        title: 'Verification: known answers first',
        body: () => {
          const cards = qa('#verify .card.ratified, #verify .card.invalid, #verify .card.warn-card')
            .filter(c => c.querySelector('.rule-head'));
          const ok = cards.filter(c => c.classList.contains('ratified')).length;
          return `Each card is a known answer: ${cards.length || 'the ones'} here were worked out by `
            + 'hand from the contract, not taken from payroll (a real paystub replaces each at '
            + `onboarding)${cards.length ? `, and ${ok} read “Reproduced”` : ''}. A rule goes live only `
            + 'if it fires in a known answer and the library then reproduces that answer.';
        },
        pre: async () => {
          closeXrayIfOpen();
          const t = q('#tab-verify');
          if (t) t.click();
        },
        waitFor: '#verify .card',
        waitTimeout: 20000,
        target: '#verify .card',
      },
      {
        // E3: mutate the $640.80 rule in memory and show which errors the known answers catch.
        title: 'Try to break it',
        body: () => {
          const card = libraryCard(LIVE_OT_ID);
          const panel = card && card.querySelector('.break-result');
          if (!panel || panel.hidden) return 'Press “Try to break it” on a live rule and every known answer is run against deliberate errors in it. No model call, nothing written.';
          const head = text(panel.querySelector('p strong')) || text(panel.querySelector('p'));
          const rows = qa('tbody tr', panel);
          const caught = rows.filter(r => r.querySelector('td.ok'));
          const surv = rows.filter(r => !r.querySelector('td.ok')).map(r => text(r.querySelector('td')));
          const ex = caught[0] ? qa('td', caught[0]).map(text) : null;
          let s = `${head}. `;
          if (ex && ex.length >= 5) s += `For example “${ex[0]}”: the known answer is ${ex[3]}, the error computes ${ex[4]}, so it is caught. `;
          if (surv.length) s += `${surv.length} survived (${surv.join('; ')}): no known answer tests that part of the rule, and the table says so. `;
          return s + 'No model call; nothing was written; the attempt is on the ledger.';
        },
        pre: async ctx => {
          const t = q('#tab-lib');
          if (t) t.click();
          const card = await poll(() => libraryCard(LIVE_OT_ID), 20000);
          if (!ctx.alive() || !card) return;
          const panel = card.querySelector('.break-result');
          if (panel && !panel.hidden && panel.querySelector('table')) return;   // already shown
          const b = card.querySelector(`button[data-id="${LIVE_OT_ID}"]`);
          if (b) b.click();
        },
        waitFor: '#library .break-result:not([hidden]) table',
        waitTimeout: 20000,
        timeoutNote: 'The mutation check is taking longer than usual. Its table will appear '
          + 'under the rule, and this step will continue when it does.',
        target: () => { const c = libraryCard(LIVE_OT_ID); return c ? c.querySelector('.break-result') : null; },
      },
      {
        // I15: the human gate, live. Load the seeded wrong draft if it is not queued yet.
        title: 'Nothing computes until a person approves it',
        body: () => {
          const card = demoCard();
          const topic = card ? text(card.querySelector('.rule-head strong')) : '';
          const clause = card ? text(card.querySelector('.rule-head .muted')) : '';
          return 'A draft in this queue affects no answer until a person approves it. This one '
            + `was seeded for the demo${topic ? ` (“${topic}”, ${clause})` : ''}: well-formed, cites the real `
            + 'Overtime Rate clause on p.8 — and says double time where the clause says one and one half.';
        },
        pre: async ctx => {
          closeDrawerIfOpen();
          const t = q('#tab-review');
          if (t) t.click();
          await poll(() => q('#review .card, #review .empty'), 20000);
          if (!ctx.alive()) return;
          if (demoChk()) return;
          const btn = q('#loadDemoDraft');
          if (typeof window.loadDemoDraft === 'function') {
            if (btn) btn.classList.add('tour-glow');
            await window.loadDemoDraft(btn);
            if (btn) btn.classList.remove('tour-glow');
          } else if (btn) {
            btn.click();
          }
        },
        waitFor: `.ruleChk[data-id="${DEMO_ID}"]`,
        waitTimeout: 20000,
        timeoutNote: 'The demo draft did not load into the queue. The tour continues with '
          + 'what is here.',
        target: () => demoCard(),
      },
      {
        title: 'Check the draft against its clause',
        body: 'The draft\'s formula — effective_base × 2 × hours — sits above the page it '
          + 'cites, with the clause boxed in red. The clause says one and one half times the '
          + 'regular rate; the draft says two. A reviewer would stop here. The tour will '
          + 'approve it anyway, to show what happens.',
        pre: async ctx => {
          const card = demoCard();
          if (!card) return;
          const b = qa('button', card).find(x => /view source/i.test(x.textContent));
          if (b) { mark('drawer'); b.click(); }
          await poll(() => q('#drawer.open .cite'), 10000);
          if (!ctx.alive()) return;
        },
        waitFor: '#drawer.open',
        waitTimeout: 10000,
        target: () => q('#drawer .card') || q('#drawer .db'),
        avoid: '#drawer .cite img',
        optional: () => !!demoCard(),
      },
      {
        title: 'Approve it anyway — the gate says no',
        body: () => {
          const m = q('#reviewMsg');
          const strongs = m ? qa('strong', m).map(text) : [];
          const nums = strongs.filter(s => /^[0-9.]+$/.test(s));
          const exp = nums[0] ? money(nums[0]) : '$640.80';
          const act = nums[1] ? money(nums[1]) : '$854.40';
          const lib = text(q('#cnt-lib'));
          return `The known-answer check refused it: ${act} is not ${exp}. The draft is well-formed `
            + 'and cites a real clause, but the shipped known answer for this shift does not '
            + `reproduce with it, so it cannot go live${lib ? ` — the Rule library still holds ${lib}` : ''}. `
            + 'The attempt is on the ledger as authoring.blocked, under the approver\'s name.';
        },
        pre: async ctx => {
          closeDrawerIfOpen();
          const chk = demoChk();
          if (!chk) return;
          qa('.ruleChk').forEach(c => { c.checked = false; });
          chk.checked = true;
          const name = q('#approverName');
          if (name && !name.value.trim()) {
            if (!(await typeInto(name, 'Tour visitor', ctx))) return;
          }
          if (!ctx.alive()) return;
          // Only ever approve the seeded draft, and only when it is the sole selection.
          const checked = qa('.ruleChk').filter(c => c.checked);
          if (checked.length !== 1 || checked[0].dataset.id !== DEMO_ID) return;
          const b = qa('#reviewBtns button').find(x => /approve selected/i.test(x.textContent));
          if (b) b.click();
        },
        waitFor: '#reviewMsg .flagline',
        waitForNew: true,
        waitTimeout: 15000,
        timeoutNote: 'The gate is taking longer than usual to answer. Its verdict will appear '
          + 'above the queue, and this step will continue when it does.',
        target: '#reviewMsg',
        optional: () => !!demoCard(),
      },
      {
        title: 'Audit: the tamper-evident ledger',
        body: () => {
          const chain = text(q('#verifyChain'));
          const blocked = qa('#ledger .evt').find(e => /authoring\.blocked/.test(text(e.querySelector('.t'))));
          return 'Every question, draft, approval and refusal is a hash-chained event'
            + (chain ? ` — “${chain}” means the chain verified end to end` : '')
            + '; altering any entry breaks it and is detected.'
            + (blocked ? ' The refusal you just saw is here as authoring.blocked, with the approver and the reason.' : '')
            + ' The whole record exports for an auditor.';
        },
        pre: async ctx => {
          const t = q('#tab-audit'); if (t) t.click();
          await poll(() => qa('#ledger .evt').some(e => /authoring\.blocked/.test(text(e.querySelector('.t')))), 6000);
          if (!ctx.alive()) return;
        },
        waitFor: '#verifyChain .badge-ratified, #verifyChain .bad, #ledger .evt',
        waitTimeout: 15000,
        target: () => qa('#ledger .evt').find(e => /authoring\.blocked/.test(text(e.querySelector('.t')))) || q('#panel-audit h2'),
      },
      {
        title: 'That\'s the loop',
        body: 'Documents in, read and checked by eye; rules proven against known answers '
          + 'and approved by a person; defensible numbers out; every step on the ledger. '
          + 'Go back to chat and ask your own question — or hand the Audit tab to an auditor.',
        nextLabel: 'Back to chat',
        advance: () => { location.href = '/'; },
      },
    ],
  };

  /* ---------------- engine ---------------- */
  let state = null;   // null = not running; every entry point checks it

  function startTour() {
    if (state) return;
    const steps = STEPS[PAGE] || [];
    if (!steps.length) return;
    const card = document.createElement('div');
    card.className = 'tour-card';
    card.setAttribute('role', 'dialog');
    card.setAttribute('aria-live', 'polite');
    card.setAttribute('aria-label', 'Guided tour');
    document.body.appendChild(card);
    state = { steps, card, i: -1, seq: 0, target: null, step: null, opened: new Set(),
              skipped: 0, busy: false, note: '' };
    document.addEventListener('keydown', onKey);
    window.addEventListener('resize', reposition);
    window.addEventListener('scroll', reposition, true);
    // Anchors are re-found after re-renders: the admin page rebuilds its lists with
    // innerHTML, and chat answers arrive after the card is shown.
    state.watch = setInterval(refind, 500);
    next();
  }

  function endTour() {
    if (!state) return;
    const s = state;
    state = null;   // stops any in-flight step the moment its next await resolves
    clearInterval(s.watch);
    qa('.tour-glow').forEach(n => n.classList.remove('tour-glow'));
    document.removeEventListener('keydown', onKey);
    window.removeEventListener('resize', reposition);
    window.removeEventListener('scroll', reposition, true);
    s.card.remove();
    // Close anything the tour itself opened, so ending never traps the visitor.
    if (s.opened.has('drawer')) closeDrawerIfOpen();
    if (s.opened.has('xray')) closeXrayIfOpen();
    const start = document.getElementById('tourStart');
    if (start) start.focus({ preventScroll: true });
  }

  function onKey(e) { if (e.key === 'Escape') endTour(); }

  const resolveTarget = s => {
    const el = typeof s.target === 'function' ? s.target() : (s.target ? q(s.target) : null);
    return visible(el) ? el : null;
  };

  function setGlow(target) {
    if (!state) return;
    if (state.target && state.target !== target) state.target.classList.remove('tour-glow');
    state.target = target;
    if (target) target.classList.add('tour-glow');
  }

  async function next() {
    if (!state) return;
    const seq = ++state.seq;
    const i = ++state.i;
    if (i >= state.steps.length) { endTour(); return; }
    const s = state.steps[i];
    const ctx = { alive: () => !!state && state.seq === seq };
    // Optional steps run only when their feature is on screen (no gap in the numbering).
    if (!stepWanted(s)) { state.skipped++; next(); return; }
    qa('.tour-glow').forEach(n => n.classList.remove('tour-glow'));
    state.target = null; state.step = s; state.note = '';
    state.busy = !!(s.pre || s.waitFor);
    render(s, i);
    position(null, s);
    // Watchdog: whatever happens inside this step — an action that never settles, a
    // silent seq race — the card must not stay busy forever.
    if (state.busy) {
      const deadline = (s.waitTimeout || 15000) + 5000;
      setTimeout(() => {
        if (ctx.alive() && state.busy) {
          state.busy = false;
          state.note = s.timeoutNote || 'This part did not load in time — continuing anyway.';
          render(s, i);
          position(resolveTarget(s), s);
        }
      }, deadline);
    }
    // Baseline BEFORE the action, so "wait for the response" means a NEW element,
    // not one left over from an earlier answer in the same session.
    const base = (s.waitFor && s.waitForNew) ? qa(s.waitFor).length : 0;
    const arrived = () => {
      const n = qa(s.waitFor).length;
      return s.waitForNew ? n > base : n > 0;
    };
    if (s.pre) {
      try { await s.pre(ctx); }
      catch (e) { console.warn('[tour] step action failed:', s.title, e); }
    }
    if (!ctx.alive()) return;
    if (s.waitFor) {
      const ok = await poll(arrived, s.waitTimeout || 15000);
      if (!ctx.alive()) return;
      if (!ok) {
        // Say what is happening (never blame a code path), show the card where it can,
        // and keep waiting on the real signal: when it arrives, clear the note and
        // highlight the element.
        console.warn('[tour] waitFor still pending:', s.title, s.waitFor);
        state.busy = false;
        state.note = s.timeoutNote || 'This part did not load in time — continuing anyway.';
        render(s, i);
        position(resolveTarget(s), s);
        await poll(() => !ctx.alive() || arrived(), 10 * 60 * 1000, 300);
        if (!ctx.alive()) return;
        state.note = '';
      }
    }
    const target = resolveTarget(s);
    if (!target && s.target && !state.note) {
      // The feature moved or is absent: never crash, never trap — skip the step.
      console.warn('[tour] target missing, skipping step:', s.title, s.target);
      state.skipped++;
      next();
      return;
    }
    if (target) {
      setGlow(target);
      target.scrollIntoView({ block: 'center', behavior: SMOOTH });
      await sleep(SMOOTH === 'smooth' ? 350 : 0);
      if (!ctx.alive()) return;
    }
    state.busy = false;
    render(s, i);
    position(target, s);
  }

  // Re-find the current step's anchor after a re-render: a detached or now-hidden target
  // is swapped for the element that replaced it; a target that only appeared later
  // (an answer landing after a timeout note) is picked up here.
  function refind() {
    if (!state || state.busy || !state.step) return;
    const s = state.step;
    if (!s.target) return;
    const cur = state.target;
    if (cur && visible(cur)) return;
    const el = resolveTarget(s);
    if (!el) { if (cur) setGlow(null); return; }
    setGlow(el);
    position(el, s);
  }

  // Steps that are optional and not (yet) on screen are left out of "of N", so the count
  // does not shrink when one is skipped later.
  const stepCount = () => state.steps.length - state.skipped
    - state.steps.filter((s, k) => k > state.i && !stepWanted(s)).length;
  const stepWanted = s => typeof s.optional === 'function' ? !!s.optional()
    : (s.optional === true ? !!resolveTarget(s) : true);

  function render(s, i) {
    const n = stepCount();
    const card = state.card;
    card.innerHTML = '';
    const h = document.createElement('h3');
    h.textContent = typeof s.title === 'function' ? s.title() : s.title;
    const p = document.createElement('p');
    let body = '';
    if (!state.busy) {
      try { body = typeof s.body === 'function' ? s.body() : s.body; }
      catch (e) { body = typeof s.body === 'string' ? s.body : ''; }
    }
    p.textContent = state.busy ? 'One moment — performing this step for you…' : body;
    card.append(h, p);
    if (state.note) {
      const w = document.createElement('p');
      w.className = 'tour-note';
      w.textContent = state.note;
      card.appendChild(w);
    }
    const foot = document.createElement('div');
    foot.className = 'tour-foot';
    const prog = document.createElement('span');
    prog.className = 'tour-progress';
    prog.textContent = `Step ${i + 1 - state.skipped} of ${n}`;
    const nextBtn = document.createElement('button');
    nextBtn.type = 'button';
    nextBtn.className = 'tour-next';
    // Never disabled: while the step is still performing, the button reads "Skip" and
    // simply advances past the wait. A disabled Next plus any stalled action is a
    // trapped visitor; ctx.alive() guards make skipping mid-action safe (the stale
    // step's remaining work no-ops when it sees the sequence moved on).
    nextBtn.textContent = state.busy ? 'Skip'
      : (s.nextLabel || (i + 1 === state.steps.length ? 'Finish' : 'Next'));
    nextBtn.onclick = () => { if (s.advance) { endTour(); s.advance(); } else next(); };
    const endBtn = document.createElement('button');
    endBtn.type = 'button';
    endBtn.className = 'tour-end';
    endBtn.textContent = 'End tour';
    endBtn.onclick = endTour;
    foot.append(prog, nextBtn, endBtn);
    card.appendChild(foot);
    if (!state.busy) nextBtn.focus({ preventScroll: true });
  }

  /* ---------------- placement ----------------
     Candidates: below, above, left and right of the target, then the four viewport
     corners and the top/bottom centre. Each is scored by how much of it would cover the
     target, cover the `avoid` element (the boxed clause image, the X-ray stage) or fall
     outside the viewport; the least-covering spot wins, then it is clamped on screen. */
  const overlap = (a, b) => {
    if (!a || !b) return 0;
    const w = Math.min(a.right, b.right) - Math.max(a.left, b.left);
    const h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
    return w > 0 && h > 0 ? w * h : 0;
  };

  function position(target, s) {
    if (!state) return;
    const c = state.card, pad = 14;
    const cw = c.offsetWidth, ch = c.offsetHeight;
    const vw = innerWidth, vh = innerHeight;
    const r = target && visible(target) ? target.getBoundingClientRect() : null;
    const avoidEl = s && s.avoid ? q(s.avoid) : null;
    const av = avoidEl && visible(avoidEl) ? avoidEl.getBoundingClientRect() : null;
    const cands = [];
    if (r) {
      cands.push([r.left, r.bottom + pad], [r.left, r.top - pad - ch],
                 [r.left - pad - cw, Math.max(8, r.top)], [r.right + pad, Math.max(8, r.top)]);
    }
    cands.push([8, 8], [vw - cw - 8, 8], [8, vh - ch - 8], [vw - cw - 8, vh - ch - 8],
               [(vw - cw) / 2, 8], [(vw - cw) / 2, vh - ch - 8]);
    if (!r) cands.unshift([(vw - cw) / 2, (vh - ch) / 2]);
    let best = null, bestScore = Infinity;
    cands.forEach(([left, top], idx) => {
      const rect = { left, top, right: left + cw, bottom: top + ch };
      const outside = cw * ch - overlap(rect, { left: 0, top: 0, right: vw, bottom: vh });
      const score = overlap(rect, r) * 1 + overlap(rect, av) * 1 + outside * 3 + idx * 0.001;
      if (score < bestScore) { bestScore = score; best = [left, top]; }
    });
    const [left, top] = best || [(vw - cw) / 2, (vh - ch) / 2];
    c.style.left = Math.min(Math.max(8, left), Math.max(8, vw - cw - 8)) + 'px';
    c.style.top = Math.min(Math.max(8, top), Math.max(8, vh - ch - 8)) + 'px';
  }

  function reposition() { if (state) position(state.target, state.step); }

  /* ------------- entry points (the only work done at load time) ------------- */
  const startBtn = document.getElementById('tourStart');
  if (startBtn) startBtn.addEventListener('click', startTour);
  // ?tour=1 starts the tour on either page (chat for a shared link, admin for the
  // hand-off from the chat leg). The flag is stripped so a reload does not restart it.
  if (new URLSearchParams(location.search).get('tour') === '1') {
    history.replaceState(null, '', location.pathname + location.hash);
    startTour();
  }
  window.kennyTour = { start: startTour, end: endTour, running: () => !!state };
})();
