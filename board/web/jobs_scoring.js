/* Jobs ranking engine (design: docs/ARCHITECTURE.md, "Ranking engine"; section numbers cite the private spec).
 * The ONLY implementation of the factor formulas, the comp gate, the composite and the sort.
 * Pure: no DOM, no fetch. Browser: window.JobsScoring. Node: module.exports (+ a stdin CLI, see bottom).
 * Non-ASCII glyphs are written as \u escapes so the file is encoding-proof. */
(function (root) {
  'use strict';

  const MODEL_VERSION = '2026-10-13.1';
  const FACTORS = [
    { key: 'role_fit', label: 'Role & qualification fit', short: 'role fit' },
    { key: 'domain', label: 'Domain & mission', short: 'domain' },
    { key: 'odds', label: 'Conversion odds', short: 'odds' },
    { key: 'comp', label: 'Compensation', short: 'pay' },
    { key: 'location', label: 'Location & modality', short: 'location' },
    { key: 'freshness', label: 'Posting freshness', short: 'freshness' },
  ];
  const FACTOR_KEYS = FACTORS.map((f) => f.key);
  // Home metros come from config.home_metros (a sample default below). A home metro is never also scored as a hub.
  const HOME = ['boston'];
  const HUBS = ['new_york', 'san_francisco', 'boston', 'austin', 'seattle', 'los_angeles', 'washington_dc', 'chicago'];
  function homeOf(config) {
    const h = config && Array.isArray(config.home_metros) ? config.home_metros.filter((m) => typeof m === 'string') : [];
    return h.length ? h : HOME;
  }
  function isHub(metro, home) { return !home.includes(metro) && HUBS.includes(metro); }
  const PERIOD = { year: 1, hour: 2080, month: 12 };
  const DASH = '\u2013';
  const DOT = ' \u00b7 ';
  const TIMES = '\u00d7';

  function clamp(x, lo, hi) { return Math.min(hi, Math.max(lo, x)); }
  function r1(x) { return Math.round(x * 10) / 10; }
  // Truncates (never rounds up) so a band just under the floor never displays as the floor itself.
  function money(n) { return n >= 1000 ? '$' + (Math.floor(n / 100) / 10) + 'K' : '$' + n; }

  function normalizeWeights(raw) {
    const vals = FACTOR_KEYS.map((k) => {
      const v = Number(raw && raw[k]);
      return Number.isFinite(v) && v > 0 ? v : 0;
    });
    const sum = vals.reduce((a, b) => a + b, 0);
    const out = {};
    FACTOR_KEYS.forEach((k, i) => { out[k] = sum > 0 ? vals[i] / sum : 1 / FACTOR_KEYS.length; });
    return out;
  }

  // ---- Location (spec 4.5) -------------------------------------------------------
  function locationPoints(metro, modality, home = HOME) {
    if (home.includes(metro)) {
      if (modality === 'hybrid' || modality === 'remote') return 100;
      return modality === 'onsite' ? 85 : 90;
    }
    if (metro === 'remote_us') return 100;
    if (metro === 'non_us') return 20;
    if (modality === 'remote' && metro) return 100;
    if (isHub(metro, home)) return 55;
    if (metro === 'other_us') return 45;
    return 60;
  }

  function locationFactor(t, home = HOME) {
    const locs = Array.isArray(t.locations) ? t.locations.filter((l) => l && typeof l === 'object' && l.metro) : [];
    if (!locs.length) return { score: 60, evidence: 'Location not stated', best: null };
    let best = null;
    locs.forEach((l) => { const p = locationPoints(l.metro, t.modality, home); if (!best || p > best.p) best = { p, l }; });
    const atHome = best.p === 100 || home.includes(best.l.metro);
    let score = best.p;
    if (!atHome && t.relocation_assistance === true) score += 5;
    const mod = t.modality && t.modality !== 'unknown' ? ', ' + t.modality : '';
    const evidence = atHome ? String(best.l.label || best.l.metro) + mod
      : 'Relocation: ' + locs.map((l) => l.label || l.metro).join(' / ') + ' (best: ' + (best.l.label || best.l.metro)
        + (isHub(best.l.metro, home) ? ', hub' : '') + ')' + (t.relocation_assistance === true ? ' \u00b7 relocation help' : '');
    return { score: clamp(score, 0, 100), evidence, best: best.l.metro };
  }

  // ---- Compensation + the Stage 1 gate (spec 4.4) --------------------------------
  function pickBand(t, home = HOME) {
    const bands = (Array.isArray(t.pay_bands) ? t.pay_bands : []).filter((b) => b && typeof b === 'object'
      && Number.isFinite(b.min) && Number.isFinite(b.max) && PERIOD[b.period]);
    if (!bands.length) return null;
    let best = bands[0];
    let bp = -1;
    bands.forEach((b) => { const p = locationPoints(b.location_metro, t.modality, home); if (p > bp) { bp = p; best = b; } });
    const m = PERIOD[best.period];
    return { min: best.min * m, max: best.max * m, basis: best.basis, metro: best.location_metro };
  }

  function compFactor(t, config) {
    const cs = config.compensation_settings;
    const floor = cs.annual_base_floor;
    const ceil = cs.hard_gate_clamp_ceiling;
    // Gates only when stated on the posting / ATS; an LLM-inferred pay_type alone falls through to unverified.
    if ((t.pay_type === 'equity_only' || t.pay_type === 'unpaid') && (t.pay_source === 'posting' || t.pay_source === 'ats_api')) {
      const what = t.pay_type === 'unpaid' ? 'Unpaid' : 'Equity only';
      return { score: 0, evidence: what + ', no base pay', gate: `Capped at ${ceil}: no base pay`, flags: [],
        pay: { text: what, kind: 'verified' } };
    }
    const band = pickBand(t, homeOf(config));
    const verified = !!band && band.basis === 'base' && (t.pay_source === 'posting' || t.pay_source === 'ats_api');
    const flags = [];
    let lo;
    let hi;
    if (verified) { lo = band.min; hi = band.max; } else {
      flags.push('comp_unspecified');
      const mb = (config.market_bands || {})[t.level];
      if (!Array.isArray(mb) || mb.length !== 2) {
        return { score: 50, evidence: 'Pay not posted; level unknown', gate: null, flags,
          pay: { text: 'Pay not posted', kind: 'unknown' } };
      }
      lo = mb[0]; hi = mb[1];
    }
    const r = ((lo + hi) / 2) / floor;
    let raw = clamp(50 + 100 * (r - 1), 0, 100);
    if (t.bonus === true) raw += 5;
    if (t.equity === true) raw += 5;
    raw = Math.min(100, raw);
    const range = money(lo) + DASH + money(hi);
    const extras = [t.bonus === true ? 'bonus' : null, t.equity === true ? 'equity' : null].filter(Boolean);
    const tail = extras.length ? DOT + '+ ' + extras.join(', ') : '';
    if (verified) {
      return { score: raw, gate: hi < floor ? `Capped at ${ceil}: posted base ${range} is below your ${money(floor)} floor` : null,
        evidence: `${range} base, verified (${t.pay_source})${DOT}${r.toFixed(2)}${TIMES} floor${tail}`, flags,
        pay: { text: range + ' verified', kind: 'verified' } };
    }
    const note = config.market_bands_note ? ', ' + config.market_bands_note : '';
    return { score: 50 + 0.6 * (raw - 50), gate: null, flags,
      evidence: `est. ${range} (${t.level} band${note})${DOT}pay not verified${tail}`,
      pay: { text: '~' + range + ' est.', kind: 'estimated' } };
  }

  // ---- Conversion odds (spec 4.3) -----------------------------------------------
  // Prefix match on warmth: older triage versions stored free text like "warm (former colleague...)".
  function isWarm(c) { return String(c.warmth || '').trim().toLowerCase().startsWith('warm'); }
  function isCurrent(c) { return String(c.status || '').trim().toLowerCase() === 'current'; }
  const INBOUND = { recruiter_outreach: [30, 'Recruiter reached out'], referral_offered: [20, 'Referral offered'],
    hiring_manager_post: [10, 'Hiring manager posted it'] };
  const ACTIVE_STAGES = ['screen', 'interviewing', 'offer'];

  function oddsFactor(t) {
    let s = 30;
    const parts = [];
    const net = t.network;
    const cs = net && Array.isArray(net.contacts) ? net.contacts.filter((c) => c && typeof c === 'object') : [];
    const wc = cs.find((c) => isCurrent(c) && isWarm(c));
    const cc = cs.find((c) => isCurrent(c) && !isWarm(c));
    const wf = cs.find((c) => !isCurrent(c) && isWarm(c));
    if (wc) { s += 40; parts.push(`Referral path: ${wc.name || 'unnamed'} (warm, current)`); }
    else if (cc) { s += 15; parts.push(`Inside contact: ${cc.name || 'unnamed'} (cold, current)`); }
    else if (wf) { s += 10; parts.push(`Intel: ${wf.name || 'unnamed'} (warm, former)`); }
    const ib = t.inbound && typeof t.inbound === 'object' ? INBOUND[t.inbound.kind] : null;
    if (ib) { s += ib[0]; parts.push(ib[1]); }
    const am = t.airtable_match || {};
    if (am.relation === 'adjacent_opening') {
      const active = ACTIVE_STAGES.includes(String(am.stage || '').trim().toLowerCase());
      s += active ? 15 : 5;
      parts.push(active ? `Active process at the company (${am.stage})` : 'Adjacent opening at the company');
    } else if (am.relation === 'reapply_candidate') { s -= 10; parts.push('Re-apply after a prior rejection'); }
    const n = t.applicant_count;
    if (Number.isFinite(n)) {
      if (n < 50) s += 10; else if (n >= 200) s -= 10;
      parts.push(n >= 200 ? '200+ applicants' : `${n} applicants`);
    }
    return { score: clamp(s, 0, 100), evidence: parts.length ? parts.join(DOT) : 'Cold application, no signals' };
  }

  // ---- Freshness (spec 4.6) -----------------------------------------------------
  // "Still open" holds only for the triage the user answered: a newer triage (new triaged_at) voids it, so a posting
  // that re-closes goes back to Closed?. Same rule as jobs.py still_open_active().
  function stillOpenActive(lead) {
    const so = lead && lead.decision && lead.decision.still_open;
    if (!so || typeof so !== 'object') return false;
    const t = (lead && lead.triage) || {};
    return t.triaged_at == null || so.triaged_at_seen === t.triaged_at;
  }
  const DAY_MS = 86400000;
  function localMidnight(d) { return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime(); }
  function dayDiff(fromDate, nowMs) { return Math.round((localMidnight(new Date(nowMs)) - localMidnight(fromDate)) / DAY_MS); }
  function parseYmd(s) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(s || ''));
    if (!m) return null;
    const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
    return d.getMonth() === Number(m[2]) - 1 && d.getDate() === Number(m[3]) ? d : null; // rejects 2026-13-40
  }
  function curve(d) {
    if (d <= 7) return 100;
    if (d <= 21) return 100 - (d - 7) * 30 / 14;
    if (d <= 45) return 70 - (d - 21) * 30 / 24;
    if (d <= 90) return 40 - (d - 45) * 25 / 45;
    return 10;
  }

  function freshnessFactor(lead, nowMs) {
    const t = (lead && lead.triage) || {};
    const stillOpen = stillOpenActive(lead);   // the user tapped "Still open" and no newer triage exists
    if (t.posting_status === 'closed' && !stillOpen) return { score: 0, evidence: 'Posting closed', days: { days: null, basis: 'unknown' }, flags: [] };
    let basis = 'posted';
    let from = parseYmd(t.posted_date);
    if (!from) {
      const fs = lead && lead.first_seen ? new Date(lead.first_seen) : null;
      if (fs && !Number.isNaN(fs.getTime())) { from = fs; basis = 'first_seen'; }
    }
    if (!from) return { score: 50, evidence: 'Posting date unknown', days: { days: null, basis: 'unknown' }, flags: ['posted_date_unknown'] };
    const days = Math.max(0, dayDiff(from, nowMs));
    let s = curve(days);
    if (t.reposted === true) s -= 15;
    if (t.recruiter_signal === 'responded') s += 10; else if (t.recruiter_signal === 'ghosted') s -= 20;
    const flags = [];
    let evidence = `Posted ${days} d ago` + (t.reposted === true ? ' (reposted)' : '');
    if (basis === 'first_seen') {
      s = 50 + 0.5 * (s - 50);
      flags.push('posted_date_unknown');
      evidence = `First seen ${days} d ago (posting date unknown)`;
    }
    if (t.recruiter_signal === 'responded') evidence += DOT + 'recruiter responded';
    if (t.recruiter_signal === 'ghosted') evidence += DOT + 'no recruiter reply';
    return { score: clamp(s, 0, 100), evidence, days: { days, basis }, flags };
  }

  // ---- Judgment factors (spec 4.1, 4.2): the triage LLM's sub-scores ------------
  function judged(t, key) {
    const v = t && t.scores && t.scores[key];
    const n = v && typeof v === 'object' ? Number(v.score) : NaN;
    if (v == null || typeof v.score !== 'number' || !Number.isFinite(n)) return null;
    return { score: clamp(n, 0, 100), evidence: String(v.evidence || '') };
  }

  // ---- Composite (spec section 4, section 4.7, section 4.8, section 4.9) -------------------------------------
  function needsRetriage(lead) {
    const t = (lead && lead.triage) || {};
    return Number((lead && lead.schema_version) || 1) < 2 || !judged(t, 'role_fit') || !judged(t, 'domain');
  }

  // Config-independent parts, cached per lead object and local day. Poll responses are new objects, so a data
  // change is always a cache miss; a weight or floor change reuses this and redoes only comp + the weighted sum.
  const BASE = typeof WeakMap === 'function' ? new WeakMap() : null;
  function dayKey(nowMs) { const d = new Date(nowMs); return d.getFullYear() * 10000 + d.getMonth() * 100 + d.getDate(); }
  function baseParts(lead, nowMs, home = HOME) {
    const k = dayKey(nowMs) + '|' + home.join(',');
    const hit = BASE && BASE.get(lead);
    if (hit && hit.k === k) return hit.parts;
    const t = lead.triage || {};
    const parts = { role_fit: judged(t, 'role_fit'), domain: judged(t, 'domain'), odds: oddsFactor(t),
      location: locationFactor(t, home), freshness: freshnessFactor(lead, nowMs) };
    if (BASE) BASE.set(lead, { k, parts });
    return parts;
  }

  function whyRank(factors, gate) {
    const idx = (f) => FACTOR_KEYS.indexOf(f.key);
    const short = (f) => FACTORS[idx(f)].short;
    const top = [...factors].sort((a, b) => b.points - a.points || idx(a) - idx(b)).slice(0, 2);
    const rest = factors.filter((f) => !top.includes(f));
    // Biggest drag = the factor outside the top two with the largest weighted shortfall weight x (100 - score).
    const drag = rest.sort((a, b) => b.weight * (100 - b.score) - a.weight * (100 - a.score) || idx(a) - idx(b))[0];
    const head = `Strong on ${short(top[0])} (${Math.round(top[0].score)}) and ${short(top[1])} (${Math.round(top[1].score)})`;
    const shortfall = drag ? drag.weight * (100 - drag.score) : 0;
    const s = shortfall > 0 ? `${head}; held back by ${short(drag)}: ${drag.evidence}.` : `${head}.`;
    return gate ? `${gate}. ${s}` : s;
  }

  function score(lead, config, nowMs) {
    if (needsRetriage(lead)) return { state: 'needs_retriage' };
    const w = normalizeWeights(config.active_weights);
    const base = baseParts(lead, nowMs, homeOf(config));
    const comp = compFactor(lead.triage || {}, config);
    const parts = { ...base, comp };
    const factors = FACTORS.map((f) => {
      const sc = r1(clamp(parts[f.key].score, 0, 100));
      return { key: f.key, label: f.label, score: sc, weight: w[f.key], points: w[f.key] * sc, evidence: parts[f.key].evidence };
    });
    const raw = r1(factors.reduce((a, f) => a + f.points, 0));
    let composite = clamp(Math.round(raw), 1, 100);
    if (comp.gate) composite = Math.min(composite, config.compensation_settings.hard_gate_clamp_ceiling);
    return { state: 'scored', composite, band: bandFor(composite, config.bands), raw, gated: !!comp.gate, gate_reason: comp.gate,
      flags: [...comp.flags, ...(base.freshness.flags || [])], factors, why_rank: whyRank(factors, comp.gate),
      pay_display: comp.pay, days_open: base.freshness.days };
  }

  function compareRanked(a, b) {
    if (b.score.composite !== a.score.composite) return b.score.composite - a.score.composite;
    const rf = (x) => x.score.factors[0].score;
    if (rf(b) !== rf(a)) return rf(b) - rf(a);
    const da = a.score.days_open.days;
    const db = b.score.days_open.days;
    if (da !== db) { if (da == null) return 1; if (db == null) return -1; return da - db; }
    const c = String(a.lead.triage.company || '').localeCompare(String(b.lead.triage.company || ''));
    if (c) return c;
    return String(a.lead.lead_id) < String(b.lead.lead_id) ? -1 : String(a.lead.lead_id) > String(b.lead.lead_id) ? 1 : 0;
  }

  function rankLeads(leads, config, nowMs) {
    const out = [];
    (leads || []).forEach((lead) => { const s = score(lead, config, nowMs); if (s.state === 'scored') out.push({ lead, score: s, rank: 0 }); });
    out.sort(compareRanked);
    out.forEach((x, i) => { x.rank = i + 1; });
    return out;
  }

  // ---- Score bands and manual overrides (spec section 4.10, section 4.11) ----------------------
  const BANDS = [
    { key: 'apply_now', label: 'Apply now' },
    { key: 'worth_a_look', label: 'Worth a look' },
    { key: 'low', label: 'Low' },
  ];
  const BAND_KEYS = BANDS.map((b) => b.key);
  const DEFAULT_BANDS = { apply_now_min: 75, worth_a_look_min: 55 };
  // Embedded copy of jobs_scoring_config.defaults() (contract-tested): the board scores at once and keeps
  // scoring if GET /api/jobs/scoring-config fails. Every value is an invented sample; set your own.
  const BAL = { role_fit: 0.30, domain: 0.20, odds: 0.15, comp: 0.15, location: 0.10, freshness: 0.10 };
  const DEFAULT_CONFIG = {
    schema_version: 1, version: 0, updated_at: null, active_preset: 'Balanced', active_weights: { ...BAL },
    compensation_settings: { annual_base_floor: 100000, hard_gate_clamp_ceiling: 40 },
    bands: { ...DEFAULT_BANDS },
    presets: { Balanced: { ...BAL },
      'High Leverage': { role_fit: 0.20, domain: 0.10, odds: 0.40, comp: 0.10, location: 0.10, freshness: 0.10 },
      'Mission First': { role_fit: 0.20, domain: 0.40, odds: 0.10, comp: 0.10, location: 0.10, freshness: 0.10 } },
    market_bands: { entry: [70000, 95000], mid: [90000, 125000], senior: [120000, 160000],
      manager: [130000, 175000], director_plus: [165000, 230000] },
    market_bands_note: 'sample band',
    home_metros: [...HOME],
  };

  function bandFor(composite, bands) {
    const b = bands || DEFAULT_BANDS;
    if (composite >= b.apply_now_min) return 'apply_now';
    if (composite >= b.worth_a_look_min) return 'worth_a_look';
    return 'low';
  }
  function bandLabel(key) { const b = BANDS.find((x) => x.key === key); return b ? b.label : String(key); }
  function stepBand(band, dir) {   // dir +1 = up (toward Apply now), -1 = down
    const i = BAND_KEYS.indexOf(band) - dir;
    return BAND_KEYS.indexOf(band) >= 0 && i >= 0 && i < BAND_KEYS.length ? BAND_KEYS[i] : null;
  }
  function overrideNote(item) {
    return item && item.marked
      ? `Moved to ${bandLabel(item.band)} by you; score says ${bandLabel(item.computed_band)}.` : '';
  }
  function placeBands(ranked, bands, overrideOf) {
    const out = BANDS.map((b) => ({ key: b.key, label: b.label, items: [] }));
    (ranked || []).forEach((r) => {
      const ov = overrideOf ? overrideOf(r.lead) : null;
      const overridden = BAND_KEYS.includes(ov);          // an override is stored (Reset is offered)
      const band = overridden ? ov : r.score.band;
      const marked = overridden && band !== r.score.band;  // "moved by you" only when it changes the band
      out[BAND_KEYS.indexOf(band)].items.push({ ...r, band, computed_band: r.score.band, overridden, marked });
    });
    out.forEach((g) => g.items.sort(compareRanked));
    return out;
  }

  const api = { MODEL_VERSION, FACTORS, FACTOR_KEYS, HOME, HUBS, homeOf, isHub, clamp, r1, money, normalizeWeights,
    locationPoints, locationFactor, pickBand, compFactor,
    isWarm, isCurrent, oddsFactor, dayDiff, stillOpenActive, freshnessFactor, judged,
    needsRetriage, score, whyRank, compareRanked, rankLeads, BANDS, BAND_KEYS, DEFAULT_BANDS, DEFAULT_CONFIG,
    bandFor, bandLabel, stepBand, overrideNote, placeBands };
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
    // Node CLI for scoring_bridge.py (spec D3): stdin {leads, config, now_ms} -> stdout ranked JSON.
    if (typeof require !== 'undefined' && require.main === module) {
      let buf = '';
      process.stdin.setEncoding('utf8');
      process.stdin.on('data', (c) => { buf += c; });
      process.stdin.on('end', () => {
        const inp = JSON.parse(buf);
        const ranked = rankLeads(inp.leads, inp.config, inp.now_ms).map((x) => ({
          lead_id: x.lead.lead_id, rank: x.rank, composite: x.score.composite, raw: x.score.raw, gated: x.score.gated,
          flags: x.score.flags, factors: x.score.factors.map((f) => ({ key: f.key, score: f.score, weight: f.weight, points: f.points })),
        }));
        const needs = (inp.leads || []).filter(needsRetriage).map((l) => l.lead_id);
        process.stdout.write(JSON.stringify({ ranked, needs_retriage: needs, model_version: MODEL_VERSION }));
      });
    }
  } else root.JobsScoring = api;
})(typeof window !== 'undefined' ? window : globalThis);
