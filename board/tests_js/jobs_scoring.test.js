// Tests for the ranking engine scorer (web/jobs_scoring.js; spec
// docs/ARCHITECTURE.md section 4).
// Run:  node --test tests_js/*.test.js
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const S = require('../web/jobs_scoring.js');

const CFG = {   // the invented sample config (jobs_scoring_config.defaults())
  active_weights: { role_fit: 0.3, domain: 0.2, odds: 0.15, comp: 0.15, location: 0.1, freshness: 0.1 },
  compensation_settings: { annual_base_floor: 100000, hard_gate_clamp_ceiling: 40 },
  market_bands: { entry: [70000, 95000], mid: [90000, 125000], senior: [120000, 160000],
    manager: [130000, 175000], director_plus: [165000, 230000] },
  market_bands_note: 'sample band',
  home_metros: ['boston'],
};
const band = (min, max, extra = {}) => ({ location_metro: 'boston', min, max, period: 'year', basis: 'base', ...extra });
const near = (a, b, eps = 1e-6) => assert.ok(Math.abs(a - b) < eps, `${a} != ${b}`);

test('FACTORS are the six keys in spec order', () => {
  assert.deepEqual(S.FACTOR_KEYS, ['role_fit', 'domain', 'odds', 'comp', 'location', 'freshness']);
});

test('normalizeWeights: sums to 1, ignores junk, all zero falls back to equal', () => {
  const w = S.normalizeWeights({ role_fit: 30, domain: 20, odds: 15, comp: 15, location: 10, freshness: 10 });
  near(w.role_fit, 0.30); near(Object.values(w).reduce((a, b) => a + b, 0), 1);
  const z = S.normalizeWeights({ role_fit: 0, domain: 0, odds: 0, comp: 0, location: 0, freshness: 0 });
  S.FACTOR_KEYS.forEach((k) => near(z[k], 1 / 6));
  const j = S.normalizeWeights({ role_fit: 'x', domain: -5, odds: 1, comp: NaN });
  near(j.odds, 1); near(j.role_fit, 0);
});

test('locationPoints: home, hubs, other, remote, unknown', () => {
  assert.equal(S.locationPoints('boston', 'hybrid'), 100);
  assert.equal(S.locationPoints('north_jersey', 'onsite', ['north_jersey', 'new_york']), 85);   // any home metro
  assert.equal(S.locationPoints('boston', 'unknown'), 90);
  assert.equal(S.locationPoints('boston', 'onsite'), 85);
  assert.equal(S.locationPoints('new_york', 'onsite'), 55);                       // a hub when it is not home
  assert.equal(S.locationPoints('boston', 'onsite', ['new_york']), 55);           // and home is never also a hub
  assert.equal(S.locationPoints('remote_us', 'unknown'), 100);
  assert.equal(S.locationPoints('austin', 'remote'), 100);
  assert.equal(S.locationPoints('san_francisco', 'onsite'), 55);
  assert.equal(S.locationPoints('other_us', 'hybrid'), 45);
  assert.equal(S.locationPoints('non_us', 'remote'), 20);
  assert.equal(S.locationPoints(undefined, 'hybrid'), 60);
});

test('locationFactor: best location wins, relocation help +5, empty list 60', () => {
  const f = S.locationFactor({ modality: 'unknown', locations: [{ label: 'Tulsa, OK', metro: 'other_us' },
    { label: 'Pasadena, CA', metro: 'los_angeles' }] });
  assert.equal(f.score, 55);
  assert.match(f.evidence, /^Relocation: Tulsa, OK \/ Pasadena, CA \(best: Pasadena, CA, hub\)$/);
  assert.equal(S.locationFactor({ modality: 'onsite', relocation_assistance: true,
    locations: [{ label: 'Austin, TX', metro: 'austin' }] }).score, 60);
  assert.equal(S.locationFactor({ modality: 'hybrid', locations: [{ label: 'Boston, MA', metro: 'boston' }] }).evidence,
    'Boston, MA, hybrid');
  assert.deepEqual(S.locationFactor({ locations: [] }).score, 60);
});

test('comp: verified band scored on midpoint vs floor, bonus and equity add 5 each', () => {
  const c = S.compFactor({ pay_type: 'salary', pay_source: 'posting', pay_bands: [band(123000, 165000)] }, CFG);
  near(c.score, 50 + 100 * (144000 / 100000 - 1));
  assert.equal(c.gate, null); assert.deepEqual(c.flags, []);
  assert.deepEqual(c.pay, { text: '$123K\u2013$165K verified', kind: 'verified' });
  assert.match(c.evidence, /^\$123K\u2013\$165K base, verified \(posting\) \u00b7 1\.44\u00d7 floor$/);
  const b = S.compFactor({ pay_type: 'salary', pay_source: 'posting', bonus: true, equity: true,
    pay_bands: [band(100000, 100000)] }, CFG);
  near(b.score, 60);
});

test('comp: gate fires when verified band max is below the floor, never on unverified pay', () => {
  const under = { pay_type: 'salary', pay_source: 'posting', pay_bands: [band(81900, 99900)] };
  const g = S.compFactor(under, CFG);
  assert.equal(g.gate, 'Capped at 40: posted base $81.9K\u2013$99.9K is below your $100K floor');
  const low = { ...CFG, compensation_settings: { annual_base_floor: 95000, hard_gate_clamp_ceiling: 40 } };
  assert.equal(S.compFactor(under, low).gate, null);
  const unv = S.compFactor({ pay_type: 'salary', pay_source: 'none', level: 'senior', pay_bands: [] }, CFG);
  assert.equal(unv.gate, null);
  assert.deepEqual(unv.flags, ['comp_unspecified']);
  near(unv.score, 50 + 0.6 * (50 + 100 * (140000 / 100000 - 1) - 50));
  assert.deepEqual(unv.pay, { text: '~$120K\u2013$160K est.', kind: 'estimated' });
  assert.match(unv.evidence, /est\. \$120K\u2013\$160K \(senior band, sample band\)/);
});

test('comp: equity-only and unpaid gate with score 0 when the posting says so', () => {
  for (const pt of ['equity_only', 'unpaid']) {
    const c = S.compFactor({ pay_type: pt, pay_source: 'posting', pay_bands: [] }, CFG);
    assert.equal(c.score, 0); assert.equal(c.gate, 'Capped at 40: no base pay');
  }
});

test('comp: an inferred equity-only/unpaid pay_type without a posted source never gates', () => {
  const c = S.compFactor({ pay_type: 'equity_only', pay_source: 'none', level: 'senior', pay_bands: [] }, CFG);
  assert.equal(c.gate, null); assert.deepEqual(c.flags, ['comp_unspecified']);
});

test('money: truncates to 0.1K, never rounds up across the floor', () => {
  assert.equal(S.money(109990), '$109.9K');
  assert.equal(S.money(110000), '$110K');
  assert.equal(S.money(123000), '$123K');
});

test('comp: level unknown and no band is neutral 50 with comp_unspecified', () => {
  const c = S.compFactor({ pay_type: 'unknown', pay_source: 'none', level: 'unknown', pay_bands: [] }, CFG);
  assert.equal(c.score, 50); assert.deepEqual(c.flags, ['comp_unspecified']);
  assert.equal(c.pay.kind, 'unknown'); assert.equal(c.evidence, 'Pay not posted; level unknown');
});

test('comp: hourly band annualized', () => {
  const c = S.compFactor({ pay_type: 'salary', pay_source: 'ats_api',
    pay_bands: [band(60, 70, { period: 'hour' })] }, CFG);
  assert.equal(c.pay.text, '$124.8K\u2013$145.6K verified');
});

test('comp: OTE band is unverified and never gates', () => {
  const c = S.compFactor({ pay_type: 'salary', pay_source: 'posting', level: 'mid',
    pay_bands: [band(60000, 90000, { basis: 'ote' })] }, CFG);
  assert.equal(c.gate, null); assert.deepEqual(c.flags, ['comp_unspecified']); assert.equal(c.pay.kind, 'estimated');
});

test('pickBand: prefers the band for the best-scoring location', () => {
  const b = S.pickBand({ modality: 'unknown', pay_bands: [band(108500, 145500, { location_metro: 'los_angeles' }),
    band(113700, 152400)] });
  assert.deepEqual([b.min, b.max, b.metro], [113700, 152400, 'boston']);
});

const NOW = new Date(2026, 9, 13, 12, 0, 0).getTime(); // 2026-10-13 noon, local time
const WARM_CUR = { name: 'Pat', status: 'current', warmth: 'warm', recommendation: 'referral ask' };

test('odds: baseline 30, best contact signal only (no stacking), clamp', () => {
  assert.deepEqual(S.oddsFactor({}), { score: 30, evidence: 'Cold application, no signals' });
  const t = { network: { contacts: [WARM_CUR, { name: 'Lee', status: 'current', warmth: 'cold' },
    { name: 'Ana', status: 'former', warmth: 'warm' }] } };
  assert.equal(S.oddsFactor(t).score, 70);
  assert.equal(S.oddsFactor(t).evidence, 'Referral path: Pat (warm, current)');
  assert.equal(S.oddsFactor({ network: { contacts: [{ name: 'Lee', status: 'current', warmth: 'cold' }] } }).score, 45);
  assert.equal(S.oddsFactor({ network: { contacts: [{ name: 'Ana', status: 'former', warmth: 'warm' }] } }).score, 40);
  const max = { network: { contacts: [WARM_CUR] }, inbound: { kind: 'recruiter_outreach', note: '' },
    airtable_match: { relation: 'adjacent_opening', stage: 'Interviewing' }, applicant_count: 12 };
  assert.equal(S.oddsFactor(max).score, 100);
});

test('odds: legacy free-text warmth and status case', () => {
  const t = { network: { contacts: [{ name: 'Alex', status: 'Current', warmth: 'warm (MBA classmate, close friend)' }] } };
  assert.equal(S.oddsFactor(t).score, 70);
  assert.equal(S.oddsFactor({ network: { contacts: [{ name: 'X', status: 'current', warmth: 'cold - no contact logged' }] } }).score, 45);
});

test('odds: inbound, pipeline and applicant volume', () => {
  assert.equal(S.oddsFactor({ inbound: { kind: 'referral_offered' } }).score, 50);
  assert.equal(S.oddsFactor({ inbound: { kind: 'hiring_manager_post' } }).score, 40);
  assert.equal(S.oddsFactor({ inbound: { kind: 'bogus' } }).score, 30);
  assert.equal(S.oddsFactor({ airtable_match: { relation: 'adjacent_opening' } }).score, 35);
  assert.equal(S.oddsFactor({ airtable_match: { relation: 'reapply_candidate' } }).score, 20);
  assert.equal(S.oddsFactor({ applicant_count: 49 }).score, 40);
  assert.equal(S.oddsFactor({ applicant_count: 150 }).score, 30);
  assert.equal(S.oddsFactor({ applicant_count: 200 }).score, 20);
  assert.equal(S.oddsFactor({ applicant_count: 200 }).evidence, '200+ applicants');
});

test('freshness: the curve', () => {
  const f = (d) => S.freshnessFactor({ triage: { posted_date: d } }, NOW).score;
  assert.equal(f('2026-10-06'), 100);              // 7 d
  assert.equal(f('2026-09-29'), 85);               // 14 d: 100 - 7*30/14
  assert.equal(f('2026-09-22'), 70);               // 21 d
  assert.equal(f('2026-08-29'), 40);               // 45 d
  assert.equal(f('2026-07-15'), 15);               // 90 d: 40 - 45*25/45
  assert.equal(f('2026-01-01'), 10);               // > 90 d
});

test('freshness: reposted, recruiter signal, first-seen fallback, closed', () => {
  const r = S.freshnessFactor({ triage: { posted_date: '2026-10-05', reposted: true } }, NOW);
  assert.ok(Math.abs(r.score - (100 - 1 * 30 / 14 - 15)) < 1e-9);
  assert.equal(r.evidence, 'Posted 8 d ago (reposted)');
  assert.deepEqual(r.days, { days: 8, basis: 'posted' });
  assert.equal(S.freshnessFactor({ triage: { posted_date: '2026-10-13', recruiter_signal: 'ghosted' } }, NOW).score, 80);
  const fs = S.freshnessFactor({ first_seen: '2026-10-01T14:00:00+00:00', triage: { posted_date: null } }, NOW);
  assert.ok(Math.abs(fs.score - (50 + 0.5 * (100 - 5 * 30 / 14 - 50))) < 1e-9);
  assert.deepEqual(fs.flags, ['posted_date_unknown']);
  assert.equal(fs.evidence, 'First seen 12 d ago (posting date unknown)');
  const none = S.freshnessFactor({ triage: {} }, NOW);
  assert.deepEqual([none.score, none.days.basis, none.flags], [50, 'unknown', ['posted_date_unknown']]);
  assert.equal(S.freshnessFactor({ triage: { posting_status: 'closed', posted_date: '2026-10-12' } }, NOW).score, 0);
  assert.equal(S.freshnessFactor({ decision: { still_open: { at: 'x' } },
    triage: { posting_status: 'closed', posted_date: '2026-10-12' } }, NOW).score, 100);   // the user said still open
  const so = (seen, triaged) => ({ decision: { still_open: { at: 'x', triaged_at_seen: seen } },
    triage: { posting_status: 'closed', posted_date: '2026-10-12', triaged_at: triaged } });
  assert.equal(S.freshnessFactor(so('T1', 'T1'), NOW).score, 100);
  assert.equal(S.freshnessFactor(so('T1', 'T2'), NOW).score, 0);                          // newer triage voids it
  assert.equal(S.stillOpenActive(so('T1', 'T2')), false);
});

test('freshness: future and malformed dates', () => {
  assert.deepEqual(S.freshnessFactor({ triage: { posted_date: '2026-11-30' } }, NOW).days, { days: 0, basis: 'posted' });
  const bad = S.freshnessFactor({ first_seen: '2026-10-11T12:00:00+00:00', triage: { posted_date: '2026-13-40' } }, NOW);
  assert.equal(bad.days.basis, 'first_seen');
  assert.ok(bad.score <= 100 && bad.score >= 0);
});

test('judged: reads triage.scores, clamps, null when missing or non-numeric', () => {
  assert.deepEqual(S.judged({ scores: { role_fit: { score: 140, evidence: 'e' } } }, 'role_fit'), { score: 100, evidence: 'e' });
  assert.equal(S.judged({ scores: { role_fit: { score: 'high' } } }, 'role_fit'), null);
  assert.equal(S.judged({}, 'domain'), null);
});

const fs = require('node:fs');
const path = require('node:path');
const G = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'scoring_golden.json'), 'utf8'));
const GNOW = Date.parse(G.now);

test('golden: normalization cases (6-decimal agreement with the Python normalizer)', () => {
  G.normalize_cases.forEach((c) => {
    const w = S.normalizeWeights(c.raw);
    S.FACTOR_KEYS.forEach((k) => assert.ok(Math.abs(w[k] - c.expected[k]) < 1e-6, `${k}: ${w[k]}`));
  });
});

test('golden: edge cases', () => {
  G.edge_cases.forEach((c) => {
    const s = S.score(c.lead, G.configs[c.config], GNOW);
    if (c.expect.state) { assert.equal(s.state, c.expect.state, c.name); return; }
    assert.equal(s.state, 'scored', c.name);
    assert.equal(s.gated, c.expect.gated, c.name);
    if (c.expect.composite_max != null) assert.ok(s.composite <= c.expect.composite_max, c.name);
    if (c.expect.flags) assert.deepEqual(s.flags.filter((f) => f === 'comp_unspecified'), c.expect.flags, c.name);
    if (c.expect.pay_kind) assert.equal(s.pay_display.kind, c.expect.pay_kind, c.name);
    if (c.expect.freshness != null) assert.equal(s.factors.find((f) => f.key === 'freshness').score, c.expect.freshness, c.name);
  });
});

test('score: shape, rounding, clamp to 1..100, gate caps composite', () => {
  const lead = G.synthetic_board.leads[1];
  const s = S.score(lead, G.configs.balanced, GNOW);
  assert.deepEqual(s.factors.map((f) => f.key), S.FACTOR_KEYS);
  assert.equal(s.composite, Math.round(s.raw));
  s.factors.forEach((f) => assert.equal(f.score, Math.round(f.score * 10) / 10));
  const zero = JSON.parse(JSON.stringify(lead));
  zero.triage.scores = { role_fit: { score: 0, evidence: '' }, domain: { score: 0, evidence: '' } };
  zero.triage.network = { contacts: [] }; zero.triage.applicant_count = 500; zero.triage.posted_date = '2025-01-01';
  zero.triage.pay_type = 'unpaid'; zero.triage.locations = [{ label: 'Paris', metro: 'non_us' }];
  const z = S.score(zero, G.configs.balanced, GNOW);
  assert.ok(z.composite >= 1);
});

test('whyRank: names the top two contributors and the biggest drag outside them; gate first', () => {
  const s = S.score(G.synthetic_board.leads[0], G.configs.balanced, GNOW);   // Role A
  assert.match(s.why_rank, /^Strong on domain \(92\) and role fit \(55\); held back by location: Relocation: /);
  const capped = S.score(G.edge_cases[0].lead, G.configs.balanced, GNOW);
  assert.match(capped.why_rank, /^Capped at 40: posted base \$81\.9K\u2013\$99\.9K is below your \$100K floor\. Strong on /);
  assert.doesNotMatch(s.why_rank, /strong_fit|v1_import|tier/);
  const perfect = S.FACTORS.map((f) => ({ key: f.key, label: f.label, score: 100, weight: 1 / 6, points: 100 / 6, evidence: 'e' }));
  assert.equal(S.whyRank(perfect, null), 'Strong on role fit (100) and domain (100).');   // no zero-shortfall clause
});

test('rankLeads: sorted, total and stable; needs_retriage leads excluded', () => {
  const leads = [...G.synthetic_board.leads, G.edge_cases[4].lead];
  const r = S.rankLeads(leads, G.configs.balanced, GNOW);
  assert.equal(r.length, 7);
  assert.deepEqual(r.map((x) => x.rank), [1, 2, 3, 4, 5, 6, 7]);
  const again = S.rankLeads([...leads].reverse(), G.configs.balanced, GNOW);
  assert.deepEqual(again.map((x) => x.lead.lead_id), r.map((x) => x.lead.lead_id));
  const twin = JSON.parse(JSON.stringify(G.synthetic_board.leads[2]));
  twin.lead_id = '5e00000000ff';
  const t = S.rankLeads([twin, G.synthetic_board.leads[2]], G.configs.balanced, GNOW);
  assert.deepEqual(t.map((x) => x.lead.lead_id), ['5e0000000003', '5e00000000ff']); // tie broken by lead_id
});

test('golden: synthetic board reproduces the formulas\u2019 expected raw scores and order', () => {
  const C = G.synthetic_board;
  const r = S.rankLeads(C.leads, G.configs[C.config], GNOW);
  assert.deepEqual(r.map((x) => x.lead.lead_id), C.expected_order);
  r.forEach((x) => assert.ok(Math.abs(x.score.raw - C.expected_raw[x.lead.lead_id]) <= 0.1,
    `${x.lead.lead_id} raw ${x.score.raw} (expected ${C.expected_raw[x.lead.lead_id]})`));
});

const B = { apply_now_min: 75, worth_a_look_min: 55 };

test('bands: thresholds are inclusive lower bounds; missing config uses the default pair', () => {
  assert.deepEqual([75, 74, 55, 54, 1, 100].map((c) => S.bandFor(c, B)),
    ['apply_now', 'worth_a_look', 'worth_a_look', 'low', 'low', 'apply_now']);
  assert.equal(S.bandFor(75, undefined), 'apply_now');
  assert.equal(S.bandFor(74, undefined), 'worth_a_look');
  assert.deepEqual(S.BAND_KEYS, ['apply_now', 'worth_a_look', 'low']);
  assert.equal(S.bandLabel('worth_a_look'), 'Worth a look');
});

test('bands: score() carries the band; gated leads are always Low; synthetic board bands', () => {
  assert.equal(S.score(G.edge_cases[0].lead, G.configs.balanced, GNOW).band, 'low');     // capped at 40
  assert.equal(S.score(G.edge_cases[2].lead, G.configs.balanced, GNOW).band, 'low');     // equity only
  const C = G.synthetic_board;
  S.rankLeads(C.leads, G.configs.balanced, GNOW).forEach((x) =>
    assert.equal(x.score.band, C.expected_band[x.lead.lead_id], x.lead.lead_id));
});

test('bands: changing thresholds moves leads between bands with no other change', () => {
  const strict = { ...G.configs.balanced, bands: { apply_now_min: 80, worth_a_look_min: 60 } };
  const r = S.rankLeads(G.synthetic_board.leads, strict, GNOW);
  assert.equal(r.filter((x) => x.score.band === 'apply_now').length, 0);
  assert.equal(r.filter((x) => x.score.band === 'low').length, 2);                      // Roles F (57) and G (55)
});

test('stepBand: one band per tap; null past the top and the bottom', () => {
  assert.equal(S.stepBand('worth_a_look', +1), 'apply_now');
  assert.equal(S.stepBand('worth_a_look', -1), 'low');
  assert.equal(S.stepBand('apply_now', +1), null);
  assert.equal(S.stepBand('low', -1), null);
});

test('placeBands: override wins, keeps the real score, sorts by score inside the band, notes the move', () => {
  const r = S.rankLeads(G.synthetic_board.leads, G.configs.balanced, GNOW);
  const gScore = r.find((x) => x.lead.lead_id === '5e0000000007').score.composite;
  const ov = (l) => (l.lead_id === '5e0000000007' ? 'apply_now' : l.lead_id === '5e0000000002' ? 'tier1' : null);
  const g = S.placeBands(r, B, ov);
  assert.deepEqual(g.map((x) => [x.key, x.label]), [['apply_now', 'Apply now'], ['worth_a_look', 'Worth a look'], ['low', 'Low']]);
  const top = g[0].items;
  assert.equal(top[top.length - 1].lead.lead_id, '5e0000000007');                       // lowest score sorts last
  const g7 = top[top.length - 1];
  assert.deepEqual([g7.band, g7.computed_band, g7.overridden, g7.score.composite],
    ['apply_now', 'low', true, gScore]);
  assert.equal(S.overrideNote(g7), 'Moved to Apply now by you; score says Low.');
  const b2 = top.find((i) => i.lead.lead_id === '5e0000000002');
  assert.equal(b2.overridden, false);                                                     // junk override ignored
  assert.equal(S.overrideNote(b2), '');
  const same = S.placeBands(r, B, (l) => (l.lead_id === '5e0000000007' ? 'low' : null))[2].items
    .find((i) => i.lead.lead_id === '5e0000000007');
  assert.deepEqual([same.overridden, same.marked, S.overrideNote(same)], [true, false, '']);  // stored, not marked
});

test('DEFAULT_CONFIG: embedded defaults score a board with no saved config', () => {
  assert.deepEqual(S.DEFAULT_CONFIG.bands, { apply_now_min: 75, worth_a_look_min: 55 });
  assert.equal(S.DEFAULT_CONFIG.compensation_settings.annual_base_floor, 100000);
  assert.deepEqual(S.DEFAULT_CONFIG.home_metros, ['boston']);
  assert.equal(S.rankLeads(G.synthetic_board.leads, S.DEFAULT_CONFIG, GNOW).length, 7);
});

test('placeBands: a gated lead moved up leaves Low but keeps its cap', () => {
  const r = S.rankLeads([G.edge_cases[0].lead], G.configs.balanced, GNOW);
  const g = S.placeBands(r, B, () => 'worth_a_look');
  const item = g[1].items[0];
  assert.ok(item.score.gated && item.score.composite <= 40 && item.overridden);
  assert.equal(g[2].items.length, 0);
});

const { performance } = require('node:perf_hooks');
const { execFileSync } = require('node:child_process');

function synthLeads(n) {
  const base = G.synthetic_board.leads;
  return Array.from({ length: n }, (_, i) => {
    const src = base[i % base.length];
    return { ...src, lead_id: i.toString(16).padStart(12, '0'), triage: { ...src.triage,
      company: 'Co' + (i % 17), applicant_count: (i * 13) % 260,
      scores: { role_fit: { score: (i * 37) % 101, evidence: 'r' }, domain: { score: (i * 53) % 101, evidence: 'd' } } } };
  });
}

test('benchmark: score + sort of 80 leads after weight, floor and data changes: median and p95 < 5 ms', () => {
  let leads = synthLeads(80);
  const cfg = JSON.parse(JSON.stringify(G.configs.balanced));
  const times = [];
  for (let i = 0; i < 220; i++) {
    cfg.active_weights = { ...cfg.active_weights, odds: 10 + (i % 30) };
    if (i % 2) cfg.compensation_settings = { ...cfg.compensation_settings, annual_base_floor: 100000 + (i % 10) * 5000 };
    if (i % 10 === 0) leads = leads.map((l) => ({ ...l }));          // lead-data change: every cache entry misses
    const t0 = performance.now();
    S.rankLeads(leads, cfg, GNOW);
    const dt = performance.now() - t0;
    if (i >= 20) times.push(dt);
  }
  times.sort((a, b) => a - b);
  const med = times[Math.floor(times.length / 2)];
  const p95 = times[Math.floor(times.length * 0.95)];
  console.log(`rankLeads(80): median ${med.toFixed(3)} ms, p95 ${p95.toFixed(3)} ms`);
  assert.ok(med < 5 && p95 < 5, `median ${med.toFixed(3)} ms, p95 ${p95.toFixed(3)} ms`);
  const big = synthLeads(1000);
  const t0 = performance.now(); S.rankLeads(big, cfg, GNOW);
  console.log(`rankLeads(1000): ${(performance.now() - t0).toFixed(2)} ms (reported only)`);
});

test('CLI: stdin leads + config -> ranked JSON', () => {
  const C = G.synthetic_board;
  const input = JSON.stringify({ leads: [...C.leads, G.edge_cases[4].lead], config: G.configs.balanced, now_ms: GNOW });
  const out = JSON.parse(execFileSync(process.execPath, [path.join(__dirname, '..', 'web', 'jobs_scoring.js')],
    { input, encoding: 'utf8' }));
  assert.equal(out.ranked.length, 7);
  assert.deepEqual(out.needs_retriage, ['e5e000000004']);
  assert.deepEqual(Object.keys(out.ranked[0]).sort(), ['composite', 'factors', 'flags', 'gated', 'lead_id', 'rank', 'raw']);
  assert.equal(out.model_version, S.MODEL_VERSION);
});
