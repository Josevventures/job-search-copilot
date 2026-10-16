// Tests for the Jobs tab's pure page logic (web/jobs.js; spec
// docs/ARCHITECTURE.md §3, §9). DOM glue lives in
// jobs.html and is covered by manual acceptance, same precedent as the
// voice-selector tests.
// Run (Node 24 rejects a bare directory):  node --test tests_js/*.test.js
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const L = require('../web/jobs.js');

function lead(over = {}) {
  const base = {
    lead_id: 'aaaaaaaaaaaa', canonical_url: 'https://jobs.example.com/acme/1', effective_bucket: 'tier1',
    undo_remaining_s: null,
    triage: { company: 'Acme', role_title: 'Associate', location: 'NYC', pay: '$150K',
      jd_url: 'https://jobs.example.com/acme/1', jd_read: true, fit_score: 'High', bucket: 'tier1',
      bucket_reason: 'strong_fit_enjoyed: fund role', network: { contacts: [], summary: '' }, engage: null,
      airtable_match: { relation: 'none' }, eligibility_note: null },
    decision: { decision: null, pending: null, job_status: null, prep_file: null, airtable_record_id: null },
  };
  return { ...base, ...over, triage: { ...base.triage, ...(over.triage || {}) },
    decision: { ...base.decision, ...(over.decision || {}) } };
}
const pending = (action, at = '2026-10-01T12:00:00+00:00') =>
  ({ action, reason: null, note: null, requested_at: at, execute_after: '2026-10-01T12:01:30+00:00' });
const PAT = { name: 'Pat Example', title: 'VP', status: 'current', warmth: 'warm', recommendation: 'referral ask' };

test('actionsFor: full row, nothing while pending/running/applied, skip only for Airtable', () => {
  assert.deepEqual(L.actionsFor(lead()).map((a) => a.action), ['applied', 'help', 'skip']);
  assert.deepEqual(L.actionsFor(lead({ decision: { pending: pending('applied') } })), []);
  assert.deepEqual(L.actionsFor(lead({ decision: { decision: 'needs_help', job_status: { kind: 'prep', state: 'running' } } })), []);
  assert.deepEqual(L.actionsFor(lead({ decision: { decision: 'needs_help', job_status: { kind: 'prep', state: 'starting' } } })), []);
  assert.deepEqual(L.actionsFor(lead({ decision: { decision: 'applied' } })), []);
  assert.deepEqual(L.actionsFor(lead({ effective_bucket: null, triage: { airtable_match: { relation: 'same_role' } } }))
    .map((a) => a.action), ['skip']);
});

const S = require('../web/jobs_scoring.js');
const fs2 = require('node:fs');
const path2 = require('node:path');
const G = JSON.parse(fs2.readFileSync(path2.join(__dirname, 'fixtures', 'scoring_golden.json'), 'utf8'));
const GNOW = Date.parse(G.now);
const v2 = (over = {}) => {
  const src = JSON.parse(JSON.stringify(G.synthetic_board.leads[1]));
  return { ...src, decision: { decision: null, pending: null, job_status: null }, effective_bucket: null, ...over,
    triage: { ...src.triage, ...(over.triage || {}) } };
};
const placed = (band, overridden = false) => ({ band, computed_band: overridden ? 'low' : band, overridden });

test('sectionOf: v2 board/closed, Still open returns to board, v1 and failed -> needs_retriage', () => {
  assert.equal(L.sectionOf(v2()), 'board');
  assert.equal(L.sectionOf(v2({ triage: { route: 'closed' } })), 'closed');
  assert.equal(L.sectionOf(v2({ triage: { route: 'closed' }, decision: { still_open: { at: 'x' } } })), 'board');
  assert.equal(L.sectionOf(v2({ triage: { route: 'closed', triaged_at: 'T2' },
    decision: { still_open: { at: 'x', triaged_at_seen: 'T1' } } })), 'closed');
  assert.equal(L.sectionOf(v2({ triage: { route: 'screened' } })), null);
  assert.equal(L.sectionOf(v2({ triage: { triage_status: 'failed' } })), 'needs_retriage');
  assert.equal(L.sectionOf(v2({ triage: { scores: null } })), 'needs_retriage');
  assert.equal(L.sectionOf(lead()), 'needs_retriage');
  assert.equal(L.sectionOf(lead({ effective_bucket: 'skip' })), null);
  assert.equal(L.sectionOf(v2({ decision: { decision: 'applied' } })), 'applied');
  assert.equal(L.sectionOf(v2({ decision: { pending: pending('prep') } })), 'in_progress');
  assert.equal(L.sectionOf(v2({ triage: { airtable_match: { relation: 'same_role' } } })), 'airtable');
});

test('sectionOf: v1 leads honour triage.route first (screened hidden, airtable in its section)', () => {
  assert.equal(L.sectionOf(lead({ triage: { route: 'screened' } })), null);
  assert.equal(L.sectionOf(lead({ triage: { route: 'airtable' } })), 'airtable');
  assert.equal(L.sectionOf(lead({ effective_bucket: null, triage: { route: 'airtable' } })), 'airtable');
  assert.equal(L.sectionOf(lead({ decision: { pending: pending('skip') } })), null);
  // v2 keeps the brief's order: same_role Airtable match wins over route 'screened'
  assert.equal(L.sectionOf(v2({ triage: { route: 'screened', airtable_match: { relation: 'same_role' } } })), 'airtable');
  assert.equal(L.sectionOf(lead({ schema_version: 1, triage: { route: 'screened', airtable_match: { relation: 'same_role' } } })), null);
});

test('sectionOf: other pending actions and decisions', () => {
  assert.equal(L.sectionOf(v2({ decision: { pending: pending('skip') } })), null);
  assert.equal(L.sectionOf(v2({ decision: { pending: pending('applied') } })), 'applied');
  assert.equal(L.sectionOf(v2({ decision: { pending: pending('terminal') } })), 'in_progress');
  assert.equal(L.sectionOf(v2({ decision: { decision: 'needs_help' } })), 'in_progress');
});

test('actionsFor: band moves on board cards only, edges, Reset when an override is stored', () => {
  const acts = (l, p) => L.actionsFor(l, p).map((a) => a.action);
  assert.deepEqual(acts(v2(), placed('worth_a_look')), ['applied', 'help', 'move_up', 'move_down', 'skip']);
  assert.deepEqual(acts(v2(), placed('apply_now')), ['applied', 'help', 'move_down', 'skip']);
  assert.deepEqual(acts(v2(), placed('low')), ['applied', 'help', 'move_up', 'skip']);
  assert.deepEqual(acts(v2(), placed('apply_now', true)), ['applied', 'help', 'move_down', 'reset_band', 'skip']);
  assert.deepEqual(acts(lead(), null), ['applied', 'help', 'skip']);
  assert.deepEqual(L.actionsFor(v2({ decision: { pending: pending('move_up') } }), placed('low')), []);
  assert.ok(!acts(v2(), placed('low')).some((a) => a.startsWith('move_tier')));
  const labels = L.actionsFor(v2(), placed('worth_a_look')).filter((a) => a.action.startsWith('move_')).map((a) => a.label);
  assert.deepEqual(labels, ['\u2191 Move up', '\u2193 Move down']);
});

test('actionsFor: unsaved ranking settings disable moves and Reset with a hint', () => {
  const a = L.actionsFor(v2(), placed('worth_a_look', true), { settingsDirty: true });
  const moves = a.filter((x) => ['move_up', 'move_down', 'reset_band'].includes(x.action));
  assert.equal(moves.length, 3);
  moves.forEach((m) => { assert.equal(m.disabled, true); assert.equal(m.hint, L.MOVE_BLOCKED_HINT); });
  assert.ok(!a.find((x) => x.action === 'applied').disabled);
});

test('actionsFor: Closed? cards offer Confirm closed and Still open', () => {
  assert.deepEqual(L.actionsFor(v2({ triage: { route: 'closed' } }), null), [
    { action: 'skip', label: 'Confirm closed', reason: 'closed_stale' },
    { action: 'still_open', label: 'Still open' }]);
});

test('overrideOf: a pending move shows its target; a pending reset shows none; else the stored override', () => {
  const pm = { ...pending('move_up'), target_band: 'apply_now' };
  assert.equal(L.overrideOf(v2({ decision: { pending: pm, band_override: { band: 'low' } } })), 'apply_now');
  assert.equal(L.overrideOf(v2({ decision: { pending: pending('reset_band'), band_override: { band: 'low' } } })), null);
  assert.equal(L.overrideOf(v2({ decision: { band_override: { band: 'low' } } })), 'low');
  assert.equal(L.overrideOf(v2()), null);
  assert.equal(L.sectionOf(v2({ decision: { pending: pm } })), 'board');
});

test('countSections counts board/closed/needs_retriage/applied and ignores skipped', () => {
  const c = L.countSections([v2(), v2({ triage: { route: 'closed' } }), lead(), v2({ decision: { pending: pending('skip') } }),
    v2({ decision: { decision: 'applied' } })]);
  assert.deepEqual(c, { board: 1, closed: 1, needs_retriage: 1, in_progress: 0, applied: 1, airtable: 0 });
});

test('groupLeads + bandHeaders: board in band order with positions; Low collapsed; needs_retriage old order', () => {
  const C = G.synthetic_board;
  const leads = C.leads.map((l) => v2({ lead_id: l.lead_id, triage: l.triage }))
    .concat([lead({ lead_id: 'bbbbbbbbbbbb', triage: { fit_score: 'Low' } }), lead({ lead_id: 'cccccccccccc' })]);
  const ranked = S.rankLeads(leads, G.configs.balanced, GNOW);
  const groups = L.groupLeads(leads, null, '', ranked, G.configs.balanced.bands);
  assert.deepEqual(groups.map((g) => g.key), ['board', 'closed', 'needs_retriage']);
  assert.deepEqual(groups[0].leads.map((l) => l.lead_id), ranked.map((r) => r.lead.lead_id));
  assert.deepEqual(groups[0].bands.flatMap((b) => b.items.map((i) => i.position)), [1, 2, 3, 4, 5, 6, 7]);
  assert.deepEqual(L.bandHeaders(groups[0], new Set(['low'])), [
    { key: 'apply_now', label: 'Apply now', count: 2, expanded: true },
    { key: 'worth_a_look', label: 'Worth a look', count: 4, expanded: true },
    { key: 'low', label: 'Low', count: 1, expanded: false }]);
  const moved = leads.map((l) => (l.lead_id === '5e0000000007'
    ? { ...l, decision: { ...l.decision, band_override: { band: 'apply_now' } } } : l));
  const mg = L.groupLeads(moved, null, '', S.rankLeads(moved, G.configs.balanced, GNOW), G.configs.balanced.bands)[0];
  assert.equal(mg.bands[0].items.at(-1).lead.lead_id, '5e0000000007');
  assert.equal(mg.leads.at(2).lead_id, '5e0000000007');
  assert.deepEqual(groups[2].leads.map((l) => l.lead_id), ['cccccccccccc', 'bbbbbbbbbbbb']);
  assert.equal(groups[2].label, 'Needs re-triage (old triage order)');
  assert.equal(L.groupLeads(leads, null, 'role c', ranked, G.configs.balanced.bands)[0].leads.length, 1);
});

test('badges', () => {
  assert.deepEqual(L.badgesFor(lead()), []);
  assert.deepEqual(L.badgesFor(lead({ triage: { jd_read: false } })), ['JD not read']);
  assert.deepEqual(L.badgesFor(lead({ triage: { airtable_match: { relation: 'reapply_candidate' } } })), ['re-apply candidate']);
  assert.deepEqual(L.badgesFor(lead({ triage: { airtable_match: { relation: 'adjacent_opening' } } })), ['adjacent opening']);
  assert.deepEqual(L.badgesFor(lead({ triage: { eligibility_note: 'class of 2025 ok' } })), ['grad program']);
  assert.deepEqual(L.badgesFor(lead({ triage: { role_title: 'MBA Leadership Development Program' } })), ['grad program']);
  assert.deepEqual(L.badgesFor(lead({ triage: { network: { contacts: [PAT] } } })), ['↑ warm contact']);
  const legacy = { ...PAT, status: 'Current', warmth: 'warm (former colleague, close friend)' };
  assert.deepEqual(L.badgesFor(lead({ triage: { network: { contacts: [legacy] } } })), ['↑ warm contact']);
  assert.deepEqual(L.badgesFor(lead({ triage: { network: { contacts: [{ ...PAT, status: 'former' }] } } })), []);
  assert.deepEqual(L.badgesFor(lead({ triage: { network: { contacts: [{ ...PAT, warmth: 'cold - no contact logged' }] } } })), []);
  assert.deepEqual(L.badgesFor(lead({ triage: { network: 'none' } })), []);
});

test('networkLine prefers the warm current contact', () => {
  assert.equal(L.networkLine(lead({ triage: { network: { contacts: [{ ...PAT, status: 'former' }, PAT] } } })),
    '↑ warm contact: Pat Example — referral ask');
  const jorge = { name: 'Lee Example', status: 'former', warmth: 'warm (former colleague)', recommendation: 'Intel ask' };
  assert.equal(L.networkLine(lead({ triage: { network: { contacts: [jorge] } } })), 'Lee Example (former, warm) — Intel ask');
  assert.equal(L.networkLine(lead()), '');
});

test('links: only http(s) hrefs ever render', () => {
  assert.equal(L.safeHref(' https://a.com/x '), 'https://a.com/x');
  assert.equal(L.safeHref('http://a.com'), 'http://a.com');
  for (const bad of ['javascript:alert(1)', 'JAVASCRIPT:alert(1)', 'data:text/html,x', '', null, undefined, '//a.com'])
    assert.equal(L.safeHref(bad), null, String(bad));
  assert.deepEqual(L.postingLink(lead()), { href: 'https://jobs.example.com/acme/1', label: 'Open posting' });
  assert.deepEqual(L.postingLink(lead({ triage: { jd_url: null } })), { href: null, label: 'no posting — DM is the application' });
  assert.equal(L.postingLink(lead({ triage: { jd_url: 'javascript:alert(1)' } })).href, null);
  assert.deepEqual(L.engageLink(lead({ triage: { engage: { name: 'Morgan Example', url: 'https://www.linkedin.com/posts/g' } } })),
    { href: 'https://www.linkedin.com/posts/g', label: 'Message Morgan Example' });
  assert.equal(L.engageLink(lead({ triage: { engage: { name: null, url: 'https://x.com/p' } } })).label, 'Message poster');
  assert.equal(L.engageLink(lead({ triage: { engage: { name: 'X', url: null } } })), null);
  assert.equal(L.engageLink(lead()), null);
});

test('terminalCommand prefers jd_url, falls back to canonical_url, and carries the lead file', () => {
  const LEAD = ' --lead data/leads/aaaaaaaaaaaa.json';
  assert.equal(L.terminalCommand(lead()), '/job-apply https://jobs.example.com/acme/1' + LEAD);
  assert.equal(L.terminalCommand(lead({ triage: { jd_url: null } })), '/job-apply https://jobs.example.com/acme/1' + LEAD);
  assert.equal(L.terminalCommand(lead({ canonical_url: 'https://lnkd.in/p', triage: { jd_url: null } })),
    '/job-apply https://lnkd.in/p' + LEAD);
  // a malformed lead id never reaches the command: the skill would refuse it anyway
  assert.equal(L.terminalCommand(lead({ lead_id: '../x' })), '/job-apply https://jobs.example.com/acme/1');
});

test('a terminal-in-progress card labels Applied as done in terminal; other cards keep "Applied"', () => {
  const term = lead({ effective_bucket: 'tier1', decision: { decision: 'needs_help',
    job_status: { kind: 'terminal', state: 'in_progress' } } });
  const acts = L.actionsFor(term);
  assert.deepEqual(acts.find((a) => a.action === 'applied'), { action: 'applied', label: 'Applied (done in terminal)' });
  assert.equal(L.actionsFor(lead()).find((a) => a.action === 'applied').label, 'Applied');
  const prepDone = lead({ decision: { decision: 'needs_help', job_status: { kind: 'prep', state: 'done' } } });
  assert.equal(L.actionsFor(prepDone).find((a) => a.action === 'applied').label, 'Applied');
});

test('countdown formatting and time since the response, never the phone clock', () => {
  assert.equal(L.formatCountdown(90), '1:30');
  assert.equal(L.formatCountdown(73), '1:13');
  assert.equal(L.formatCountdown(5), '0:05');
  assert.equal(L.formatCountdown(0), '0:00');
  assert.equal(L.formatCountdown(-3), '0:00');
  assert.equal(L.formatCountdown(null), '0:00');
  const l = { ...lead({ decision: { pending: pending('skip') } }), undo_remaining_s: 90, _receivedAt: 1000 };
  assert.equal(L.remainingSeconds(l, 11500), 80);
  assert.equal(L.remainingSeconds(l, 1000 + 200000), 0);
  assert.equal(L.remainingSeconds(lead(), 5000), null);
});

test('statusLine covers every job state', () => {
  assert.deepEqual(L.statusLine(lead({ undo_remaining_s: 90, decision: { pending: pending('applied') } }), 73),
    { kind: 'pending', text: 'Pending (Undo 1:13)' });
  assert.deepEqual(L.statusLine(lead({ decision: { job_status: { kind: 'prep', state: 'starting' } } })),
    { kind: 'running', text: 'Starting…' });
  assert.equal(L.statusLine(lead({ decision: { job_status: { kind: 'applied', state: 'running' } } })).text, 'Logging to Airtable…');
  assert.equal(L.statusLine(lead({ decision: { job_status: { kind: 'prep', state: 'running' } } })).text, 'Drafting prep pack…');
  assert.equal(L.statusLine(lead({ decision: { airtable_record_id: 'recAbC123dEf456Gh',
    job_status: { kind: 'applied', state: 'done' } } })).text, 'Logged ✓ recAbC123dEf456Gh');
  assert.deepEqual(L.statusLine(lead({ decision: { prep_file: 'data/prep/acme co-2026-10-01.md',
    job_status: { kind: 'prep', state: 'done' } } })),
    { kind: 'done', text: 'Prep ready → open', href: '/api/files/data/prep/acme%20co-2026-10-01.md' });
  assert.deepEqual(L.statusLine(lead({ decision: { job_status: { kind: 'prep', state: 'failed', error: 'boom' } } })),
    { kind: 'failed', text: 'Failed: boom', button: 'retry' });
  assert.deepEqual(L.statusLine(lead({ decision: { job_status: { kind: 'applied', state: 'needs_permission',
    denied_tools: ['mcp__claude_ai_Gmail__search_threads'] } } })),
    { kind: 'permission', text: 'Needs permission: mcp__claude_ai_Gmail__search_threads', button: 'grant' });
  assert.equal(L.statusLine(lead({ decision: { job_status: { kind: 'terminal', state: 'in_progress' } } })).text, 'Continue in terminal');
  assert.equal(L.statusLine(lead()), null);
});

test('poll fast only while something is pending, starting or running', () => {
  assert.equal(L.pollIntervalMs([lead()]), 60000);
  assert.equal(L.pollIntervalMs([lead(), lead({ decision: { pending: pending('skip') } })]), 5000);
  assert.equal(L.pollIntervalMs([lead({ decision: { job_status: { kind: 'applied', state: 'running' } } })]), 5000);
  assert.equal(L.pollIntervalMs([lead({ decision: { job_status: { kind: 'prep', state: 'starting' } } })]), 5000);
  assert.equal(L.pollIntervalMs([]), 60000);
});

test('undoBars: newest first, at most three, expired ones dropped', () => {
  const mk = (id, at, company, remaining) => ({ ...lead({ lead_id: id, triage: { company }, decision: { pending: pending('skip', at) } }),
    undo_remaining_s: remaining, _receivedAt: 0 });
  const leads = [mk('a', '2026-10-01T12:00:01+00:00', 'A', 90), mk('b', '2026-10-01T12:00:04+00:00', 'B', 90),
    mk('c', '2026-10-01T12:00:02+00:00', 'C', 90), mk('d', '2026-10-01T12:00:03+00:00', 'D', 90),
    mk('e', '2026-10-01T12:00:05+00:00', 'E', 0), lead()];
  const bars = L.undoBars(leads, 0);
  assert.deepEqual(bars.map((b) => b.lead_id), ['b', 'd', 'c']);
  assert.equal(bars[0].label, 'Skipped: B');
  assert.equal(bars[0].remaining, 90);
});

test('stampLeads and replaceLead record when each lead arrived', () => {
  const s = L.stampLeads([lead()], 42);
  assert.equal(s[0]._receivedAt, 42);
  const updated = lead({ decision: { pending: pending('skip') } });
  const r = L.replaceLead(s, updated, 99);
  assert.equal(r.length, 1);
  assert.equal(r[0]._receivedAt, 99);
  assert.equal(r[0].decision.pending.action, 'skip');
  assert.equal(L.replaceLead(s, { ...updated, lead_id: 'bbbbbbbbbbbb' }, 1).length, 2);
});

test('skip reason keys match the backend', () => {
  assert.deepEqual(L.SKIP_REASONS.map((r) => r.key),
    ['wrong_level', 'wrong_function', 'location', 'pay', 'company_industry', 'closed_stale', 'not_interested']);
  assert.deepEqual(L.SKIP_REASONS.map((r) => r.label), ['Wrong level', 'Wrong function', 'Location', 'Pay',
    'Company / industry', 'Closed / stale', 'Not interested']);
});

test('extracting reads the form, counts as active, and prep never offers Grant', () => {
  const ex = lead({ decision: { decision: 'needs_help', job_status: { kind: 'prep', state: 'extracting' } } });
  assert.deepEqual(L.statusLine(ex), { kind: 'running', text: 'Reading the form…' });
  assert.deepEqual(L.actionsFor(ex), []);
  assert.equal(L.pollIntervalMs([ex]), 5000);
  assert.equal(L.sectionOf(ex), 'in_progress');
  assert.deepEqual(L.statusLine(lead({ decision: { job_status: { kind: 'prep', state: 'needs_permission',
    denied_tools: ['WebFetch'] } } })),
    { kind: 'permission', text: 'Needs permission: WebFetch', button: 'retry' });
});

const place1 = (l, ov) => {
  const r = S.rankLeads([l], G.configs.balanced, GNOW);
  const it = S.placeBands(r, G.configs.balanced.bands, () => ov || null).flatMap((b) => b.items)[0];
  return { ...it, position: 1 };
};

test('cardModel: board card carries every R8 field; v1 card shows no score', () => {
  const l = v2();
  const r = place1(l);
  const m = L.cardModel(l, r);
  assert.deepEqual([m.band, m.colorBand, m.movedByYou], ['apply_now', 'apply_now', false]);
  assert.equal(m.scoreText, String(r.score.composite));
  assert.match(m.meta, /Boston, MA/); assert.match(m.meta, /posted 5 d/);
  assert.deepEqual(m.pay, { text: '$123K\u2013$165K verified', kind: 'verified', unspecified: false });
  assert.equal(m.fit, 'Synthetic B: meets most requirements');
  assert.equal(m.whyRank, r.score.why_rank);
  assert.equal(m.breakdown.length, 6);
  assert.deepEqual(Object.keys(m.breakdown[0]).sort(), ['evidence', 'label', 'pct', 'points', 'score']);
  const old = L.cardModel(lead(), null);
  assert.equal(old.scoreText, null); assert.equal(old.note, 'Needs re-triage');
});

test('cardModel: a moved card keeps its real score and colour, adds the marker and the note', () => {
  const l = G.synthetic_board.leads[5];                                               // Role F, Worth a look
  const it = place1(l, 'apply_now');
  const m = L.cardModel(l, it);
  assert.equal(m.scoreText, String(S.score(l, G.configs.balanced, GNOW).composite));
  assert.deepEqual([m.band, m.colorBand, m.movedByYou], ['apply_now', 'worth_a_look', true]);
  assert.match(m.whyRank, /^Moved to Apply now by you; score says Worth a look\. Strong on /);
});

test('cardModel: an override equal to the computed band is stored but shows no marker', () => {
  const l = G.synthetic_board.leads[5];
  const m = L.cardModel(l, place1(l, 'worth_a_look'));
  assert.equal(m.movedByYou, false);
  assert.doesNotMatch(m.whyRank, /by you/);
});

test('cardModel: a gated card moved up keeps the lock; comp_unspecified chip', () => {
  const m = L.cardModel(G.edge_cases[0].lead, place1(G.edge_cases[0].lead, 'worth_a_look'));
  assert.deepEqual([m.gated, m.band, m.movedByYou], [true, 'worth_a_look', true]);
  assert.equal(L.cardModel(G.edge_cases[3].lead, place1(G.edge_cases[3].lead)).pay.unspecified, true);
});

test('shownFor: computed band and display position; null while settings are unsaved', () => {
  const it = place1(G.synthetic_board.leads[5], 'apply_now');
  assert.deepEqual(L.shownFor(it), { score: it.score.composite, rank: 1, band: 'worth_a_look' });
  assert.equal(L.shownFor(it, true), null);
  assert.equal(L.shownFor(null), null);
});

const SAVED = { version: 3, active_preset: 'Balanced', presets: { Balanced: G.configs.balanced.active_weights },
  active_weights: G.configs.balanced.active_weights, compensation_settings: { annual_base_floor: 100000, hard_gate_clamp_ceiling: 40 },
  bands: { apply_now_min: 75, worth_a_look_min: 55 },
  market_bands: G.configs.balanced.market_bands, market_bands_note: 'seed band' };

test('settings: draft, dirty, presets, percentages (all-zero never NaN)', () => {
  const d = L.draftFromConfig(SAVED);
  assert.deepEqual(d.weights, { role_fit: 30, domain: 20, odds: 15, comp: 15, location: 10, freshness: 10 });
  assert.deepEqual(d.bands, { apply_now_min: 75, worth_a_look_min: 55 });
  assert.equal(L.isDirty(d, SAVED), false);
  assert.equal(L.isDirty({ ...d, floor: 115000 }, SAVED), true);
  assert.equal(L.presetFor(d, SAVED.presets), 'Balanced');
  assert.equal(L.presetFor({ ...d, weights: { ...d.weights, odds: 21 } }, SAVED.presets), null);
  assert.equal(L.percentages({ role_fit: 0, domain: 0, odds: 0, comp: 0, location: 0, freshness: 0 }).role_fit, 17);
});

test('settings: band thresholds validate and an invalid pair never previews', () => {
  const d = L.draftFromConfig(SAVED);
  assert.equal(L.bandsProblem(d, 40), null);
  assert.match(L.bandsProblem({ ...d, bands: { apply_now_min: 50, worth_a_look_min: 50 } }, 40), /Apply now/);
  assert.match(L.bandsProblem({ ...d, bands: { apply_now_min: 70, worth_a_look_min: 40 } }, 40), /40/);
  assert.deepEqual(L.previewConfig(SAVED, { ...d, bands: { apply_now_min: 80, worth_a_look_min: 60 } }).bands,
    { apply_now_min: 80, worth_a_look_min: 60 });
  assert.deepEqual(L.previewConfig(SAVED, { ...d, bands: { apply_now_min: 50, worth_a_look_min: 60 } }).bands, SAVED.bands);
  const p = L.previewConfig(SAVED, { ...d, floor: 125000 });
  assert.equal(p.compensation_settings.annual_base_floor, 125000);
  assert.equal(p.market_bands, SAVED.market_bands);
});

test('settings: steppers, money, save outcomes', () => {
  assert.equal(L.stepThreshold(70, 1), 75); assert.equal(L.stepThreshold(72, -1), 70);
  assert.equal(L.stepThreshold(100, 1), 100); assert.equal(L.stepThreshold(1, -1), 1);
  assert.equal(L.stepFloor(100000, 1), 105000); assert.equal(L.stepFloor(102345, -1), 100000);
  assert.equal(L.stepFloor(10000, -1), 10000); assert.equal(L.stepFloor(1000000, 1), 1000000);
  assert.equal(L.formatMoney(100000), '$100,000');
  assert.deepEqual([200, 409, 422, 500].map(L.settingsSaveOutcome), ['saved', 'conflict', 'invalid', 'error']);
});

test('settings: the Ranking button names the preset, unsaved state and default settings', () => {
  const d = L.draftFromConfig(SAVED);
  assert.equal(L.rankButtonText({ draft: d, config: SAVED, usingDefaults: false, dirty: false }), 'Ranking: Balanced');
  assert.equal(L.rankButtonText({ draft: { ...d, weights: { ...d.weights, odds: 30 } }, config: SAVED, usingDefaults: false, dirty: true }),
    'Ranking: Custom \u2022 not saved');
  assert.equal(L.rankButtonText({ draft: L.draftFromConfig(S.DEFAULT_CONFIG), config: S.DEFAULT_CONFIG, usingDefaults: true, dirty: false }),
    'Ranking: Balanced (default settings)');
});

test('cardModel: carries the user\'s Slack note on ranked and unranked cards', () => {
  const l = { ...v2(), source_note: 'Dana\'s team' };
  assert.equal(L.cardModel(l, place1(l)).yourNote, 'Dana\'s team');
  assert.equal(L.cardModel({ ...lead(), source_note: 'x' }, null).yourNote, 'x');
  assert.equal(L.cardModel(lead(), null).yourNote, '');
});

test('actionsFor: demo mode shows Applied and Need help even when dispatch is off', () => {
  const off = L.actionsFor(lead(), null, { dispatchEnabled: false }).map((a) => a.action);
  assert.deepEqual(off, ['skip']);
  const demo = L.actionsFor(lead(), null, { dispatchEnabled: false, demo: true }).map((a) => a.action);
  assert.deepEqual(demo, ['applied', 'help', 'skip']);
});

test('modulesPanel: all five modules with their state, setup links, and the demo note only in demo mode', () => {
  const p = L.modulesPanel({ demo: true, modules: { slack_intake: false, ai_triage: true, form_extractor: false,
    job_apply: false, airtable: false } });
  assert.deepEqual(p.items.map((m) => m.key), ['slack_intake', 'ai_triage', 'form_extractor', 'job_apply', 'airtable']);
  assert.deepEqual(p.items.map((m) => m.enabled), [false, true, false, false, false]);
  p.items.forEach((m) => { assert.match(m.href, /docs\/SETUP\.md#module-/); assert.ok(m.adds.length > 20); });
  assert.equal(p.demo, true); assert.equal(p.note, L.DEMO_NOTE);
  assert.match(p.note, /^Demo mode: buttons are simulated; turn modules on in config\.yaml/);
  const serve = L.modulesPanel({ demo: false, modules: {} });
  assert.equal(serve.note, null);
  assert.ok(serve.items.every((m) => m.enabled === false));
});
