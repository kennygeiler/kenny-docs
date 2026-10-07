// node --test tests/tree_render.test.mjs
// Renders a tree JSON (TREE_JSON=path, as tests/test_decision_tree.py passes from a
// live TestClient run; else the small inline fixture) through core/templates/tree.js
// and checks the DOM contract the drawer relies on.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const { treeHtml } = require(path.join(here, '..', 'core', 'templates', 'tree.js'));

const INLINE = {
  legacy: false,
  counts: { documents: { corpus: 5, candidates: 5, chosen: 1 }, clauses: { searched: 0, hits: 0, cited: 1 } },
  nodes: [
    { id: 'root', fork: 'prompt', label: 'Cost an 8-hour shift', status: 'info', children: [] },
    { id: 'd1', fork: 'governance', label: 'Which contract governs', status: 'fork', decided_by: 'fixed-logic',
      counts: { considered: 2, chosen: 1 },
      children: [
        { id: 'd1c0', fork: 'governance', label: 'Local 3535 MOU', status: 'chosen', reason: '', children: [],
          ref: { doc_id: 'ff', page: 8, bbox: [1, 2, 3, 4], text: 'An employee <who> works overtime' } },
        { id: 'd1r0', fork: 'governance', label: 'Admin MOU', status: 'rejected', reason: 'unit admin ≠ fire', children: [] },
      ] },
  ],
};

function countNodes(nodes) {
  return nodes.reduce((n, x) => n + 1 + countNodes(x.children || []), 0);
}

const tree = process.env.TREE_JSON ? JSON.parse(readFileSync(process.env.TREE_JSON, 'utf8')) : INLINE;

test('one li.node per node, rejected inside details, a badge per fork, refs carry data-page', () => {
  const html = treeHtml(tree);
  const nodes = countNodes(tree.nodes);
  assert.equal((html.match(/<li class="node /g) || []).length, nodes, 'li.node count');
  const rejectedLis = (html.match(/<li class="node rejected"/g) || []).length;
  const rejectedInTree = JSON.stringify(tree).split('"status":"rejected"').length - 1;
  assert.equal(rejectedLis, rejectedInTree, 'rejected li count');
  if (rejectedLis) {
    // every rejected li is inside a <details> block
    const parts = html.split('<li class="node rejected"');
    for (let i = 1; i < parts.length; i++) {
      const before = parts.slice(0, i).join('');
      const opens = (before.match(/<details>/g) || []).length;
      const closes = (before.match(/<\/details>/g) || []).length;
      assert.ok(opens > closes, 'rejected node outside <details>');
    }
  }
  const forks = (JSON.stringify(tree).match(/"status":"fork"/g) || []).length;
  assert.equal((html.match(/<span class="badge /g) || []).length, forks, 'one badge per fork node');
  const refs = html.match(/<button type="button" class="ref"[^>]*>/g) || [];
  for (const r of refs) assert.match(r, /data-page="\d+"/);
  assert.ok(html.startsWith('<div class="counts">'), 'counts strip first');
  assert.ok(!html.includes('<who>'), 'labels and texts are escaped');
});

test('a legacy tree renders nothing so the caller shows its text fallback', () => {
  assert.equal(treeHtml({ legacy: true, nodes: [], counts: {} }), '');
  assert.equal(treeHtml(null), '');
});
