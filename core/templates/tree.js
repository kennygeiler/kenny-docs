// Search tree renderer for the audit drawer (DEMO_TICKETS.md L3).
//
// Pure: treeHtml(tree) -> HTML string, from the `tree` object GET /chat/audit/{id}
// returns (core/audit.py::build_tree, built from the hash-chained ledger events — not
// from the HTTP answer). renderTree(tree, el, onRef) puts it in the drawer and wires
// every clause reference to the page render. No fetch here, no DOM in treeHtml, so
// tests/tree_render.test.mjs can run it under node.
//
// Shape of a node: {id, fork, label, status: fork|chosen|rejected|info, decided_by,
// reason, ref: {doc_id, page, bbox, clause, text}, value, detail, children}.
// Rejected children sit inside <details> so the winner reads first; their reason is
// shown in the open, never hidden.
(function (global) {
  'use strict';

  const BADGES = {
    'fixed-logic': ['fixed', 'fixed logic', 'Deterministic code decided this step'],
    'ai': ['ai', 'AI', 'The model\'s structured output decided this step'],
    'human-rule': ['human', 'human rule', 'A human-ratified rule decided this step'],
    'user': ['user', 'you', 'You answered a clarifying question'],
  };

  function esc(s) {
    return (s === null || s === undefined ? '' : String(s))
      .replace(/[&<>"']/g, m => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[m]));
  }

  function badge(by) {
    const b = BADGES[by];
    if (!b) return '';
    return `<span class="badge ${b[0]}" title="${esc(b[2])}">${esc(b[1])}</span>`;
  }

  function refButton(ref) {
    if (!ref || !ref.doc_id || !ref.page) return '';
    const text = (ref.text || '').trim();
    const quote = text ? ` · ‘${esc(text.length > 60 ? text.slice(0, 60) + '…' : text)}’` : '';
    return ` <button type="button" class="ref" data-doc="${esc(ref.doc_id)}" data-page="${esc(ref.page)}"`
      + ` data-bbox="${esc((ref.bbox || []).join(','))}" data-clause="${esc(ref.clause || '')}"`
      + ` data-text="${esc(text)}" title="Open page ${esc(ref.page)} with this passage outlined">`
      + `p.${esc(ref.page)}${quote}</button>`;
  }

  function nodeHtml(n) {
    const kids = n.children || [];
    const chosen = kids.filter(c => c.status !== 'rejected');
    const rejected = kids.filter(c => c.status === 'rejected');
    const isFork = n.status === 'fork';
    const counts = n.counts || {};
    const tally = isFork && counts.considered
      ? ` <span class="tally">${esc(counts.chosen)} of ${esc(counts.considered)}</span>` : '';
    let html = `<li class="node ${esc(n.status)}" data-fork="${esc(n.fork)}" data-id="${esc(n.id)}">`;
    if (isFork) html += badge(n.decided_by);
    html += `<span class="nl">${esc(n.label)}</span>${tally}`;
    if (n.reason) html += ` <em class="why">${esc(n.reason)}</em>`;
    html += refButton(n.ref);
    if (chosen.length) html += `<ol>${chosen.map(nodeHtml).join('')}</ol>`;
    if (rejected.length) {
      html += `<details><summary>${rejected.length} rejected</summary><ol>${rejected.map(nodeHtml).join('')}</ol></details>`;
    }
    return html + '</li>';
  }

  function countsHtml(c) {
    if (!c) return '';
    const d = c.documents || {}, k = c.clauses || {};
    const parts = [];
    if (d.corpus || d.candidates) {
      parts.push(`<span>documents <b>${esc(d.corpus || d.candidates)}</b> → <b>${esc(d.chosen || 0)}</b></span>`);
    }
    parts.push(`<span>clauses searched <b>${esc(k.searched || 0)}</b> → hits <b>${esc(k.hits || 0)}</b> → cited <b>${esc(k.cited || 0)}</b></span>`);
    if (c.decisions) parts.push(`<span>decisions recorded <b>${esc(c.decisions)}</b></span>`);
    return `<div class="counts">${parts.join('')}</div>`;
  }

  // Empty string when the query has no decision events: the caller shows its text
  // fallback (the old trace list) and says the answer predates decision events.
  function treeHtml(tree) {
    if (!tree || tree.legacy || !(tree.nodes || []).length) return '';
    return countsHtml(tree.counts) + `<ol class="tree">${tree.nodes.map(nodeHtml).join('')}</ol>`;
  }

  function renderTree(tree, host, onRef) {
    const html = treeHtml(tree);
    host.innerHTML = html;
    if (!html) return false;
    host.querySelectorAll('button.ref').forEach(b => {
      b.addEventListener('click', () => {
        if (typeof onRef !== 'function') return;
        const bbox = b.dataset.bbox ? b.dataset.bbox.split(',').map(Number) : [];
        onRef({doc_id: b.dataset.doc, page: Number(b.dataset.page), bbox,
               clause: b.dataset.clause || '', text: b.dataset.text || ''});
      });
    });
    return true;
  }

  const api = {treeHtml, renderTree, nodeHtml, countsHtml};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  global.KennyTree = api;
})(typeof window !== 'undefined' ? window : globalThis);
