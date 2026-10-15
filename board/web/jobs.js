/* Jobs board page logic (design: docs/ARCHITECTURE.md, "The board").
 * Pure functions only: no DOM, no fetch. jobs.html renders with them.
 * Browser: window.JobsLogic. Node (tests_js/jobs.test.js): module.exports.
 * Non-ASCII glyphs are written as \u escapes so the file is encoding-proof. */
(function (root) {
  'use strict';

  const S = (typeof module !== 'undefined' && module.exports) ? require('./jobs_scoring.js') : root.JobsScoring;

  const SECTIONS = [
    { key: 'board', label: 'Ranked' },
    { key: 'closed', label: 'Closed?' },
    { key: 'needs_retriage', label: 'Needs re-triage' },
    { key: 'in_progress', label: 'In progress' },
    { key: 'applied', label: 'Applied' },
    { key: 'airtable', label: 'Already in Airtable' },
  ];
  const DEFAULT_SECTIONS = ['board', 'closed', 'needs_retriage'];
  const GROUP_LABELS = { needs_retriage: 'Needs re-triage (old triage order)' };
  const SKIP_REASONS = [
    { key: 'wrong_level', label: 'Wrong level' },
    { key: 'wrong_function', label: 'Wrong function' },
    { key: 'location', label: 'Location' },
    { key: 'pay', label: 'Pay' },
    { key: 'company_industry', label: 'Company / industry' },
    { key: 'closed_stale', label: 'Closed / stale' },
    { key: 'not_interested', label: 'Not interested' },
  ];
  const ACTION_LABELS = { skip: 'Skipped', applied: 'Applied', prep: 'Prep pack', terminal: 'Terminal',
    move_up: 'Moved up', move_down: 'Moved down', reset_band: 'Band reset', still_open: 'Marked still open' };
  const MOVE_BLOCKED_HINT = 'Save or revert your ranking changes to move cards';
  const BAND_ACTIONS = ['move_up', 'move_down', 'reset_band'];
  const FIT_ORDER = { High: 0, Medium: 1, Low: 2 };
  const FAST_POLL_MS = 5000;
  const SLOW_POLL_MS = 60000;
  const MAX_UNDO_BARS = 3;
  const GRAD_RE = /\b(mba|graduate|grad)\b.*\bprogram\b/i;
  const ARROW = '\u2192';
  const UP = '\u2191';
  const DOWN = '\u2193';
  const CHECK = '\u2713';
  const ELLIPSIS = '\u2026';
  const DASH = '\u2014';
  const LEADS_DIR = 'data/leads/';   // default; the server sends the real one with every lead list
  const LEAD_ID_RE = /^[0-9a-f]{12}$/;

  function triageOf(lead) { return (lead && lead.triage) || {}; }
  function decisionOf(lead) { return (lead && lead.decision) || {}; }
  function jobOf(lead) { return decisionOf(lead).job_status || {}; }
  // "starting" = job saved, launch not yet confirmed; "extracting" = the prep form reader runs before the
  // drafter (prep-extractor). All three are treated like running everywhere.
  const ACTIVE_STATES = ['running', 'starting', 'extracting'];
  function isActive(job) { return ACTIVE_STATES.includes(job.state); }

  function sectionOf(lead) {
    const d = decisionOf(lead);
    const t = triageOf(lead);
    const act = d.pending ? d.pending.action : null;
    if (act === 'skip') return null;
    if (act === 'applied') return 'applied';
    if (act === 'prep' || act === 'terminal') return 'in_progress';
    if (d.decision === 'applied') return 'applied';
    if (d.decision === 'needs_help') return 'in_progress';
    if (Number((lead && lead.schema_version) || 1) < 2) {
      // The skill writes route on v1 screened / Airtable-deduped leads without bumping schema_version.
      if (t.route === 'screened') return null;
      if (t.route === 'airtable') return 'airtable';
      if ((t.airtable_match || {}).relation === 'same_role') return 'airtable';
      return lead && lead.effective_bucket === 'skip' ? null : 'needs_retriage';
    }
    if ((t.airtable_match || {}).relation === 'same_role') return 'airtable';
    if (t.route === 'screened') return null;
    if (t.triage_status === 'failed' || S.needsRetriage(lead)) return 'needs_retriage';
    if (t.route === 'closed' && !S.stillOpenActive(lead) && act !== 'still_open') return 'closed';
    return 'board';
  }

  function overrideOf(lead) {
    const d = decisionOf(lead);
    const p = d.pending;
    if (p && (p.action === 'move_up' || p.action === 'move_down')) return p.target_band || null;
    if (p && p.action === 'reset_band') return null;
    return d.band_override && d.band_override.band ? d.band_override.band : null;
  }

  function sectionLabel(key) {
    const s = SECTIONS.find((x) => x.key === key);
    return s ? s.label : String(key);
  }

  function contactsOf(lead) {
    const net = triageOf(lead).network;
    const list = net && Array.isArray(net.contacts) ? net.contacts : [];
    return list.filter((c) => c && typeof c === 'object');
  }
  function warmCurrentContact(lead) { return contactsOf(lead).find((c) => S.isCurrent(c) && S.isWarm(c)) || null; }

  function badgesFor(lead) {
    const t = triageOf(lead);
    const rel = (t.airtable_match || {}).relation;
    const out = [];
    if (t.jd_read === false) out.push('JD not read');
    if (rel === 'reapply_candidate') out.push('re-apply candidate');
    if (rel === 'adjacent_opening') out.push('adjacent opening');
    if (t.eligibility_note || GRAD_RE.test(String(t.role_title || ''))) out.push('grad program');
    if (warmCurrentContact(lead)) out.push(UP + ' warm contact');
    return out;
  }

  function networkLine(lead) {
    const warm = warmCurrentContact(lead);
    if (warm) return `${UP} warm contact: ${warm.name || 'unnamed'} ${DASH} ${warm.recommendation || 'reach out'}`;
    const c = contactsOf(lead)[0];
    if (!c) return '';
    return `${c.name || 'unnamed'} (${S.isCurrent(c) ? 'current' : 'former'}, ${S.isWarm(c) ? 'warm' : 'cold'}) ${DASH} ${c.recommendation || 'FYI'}`;
  }

  function safeHref(url) {
    const s = String(url == null ? '' : url).trim();
    return /^https?:\/\/\S+$/i.test(s) ? s : null;
  }

  function postingLink(lead) {
    const href = safeHref(triageOf(lead).jd_url);
    return href ? { href, label: 'Open posting' } : { href: null, label: `no posting ${DASH} DM is the application` };
  }

  function engageLink(lead) {
    const e = triageOf(lead).engage;
    const href = e ? safeHref(e.url) : null;
    return href ? { href, label: 'Message ' + (e.name || 'poster') } : null;
  }

  // --lead lets the terminal run record a completion line the dashboard applies (SKILL.md "--lead <lead file>").
  function terminalCommand(lead, leadsDir) {
    const cmd = ('/job-apply ' + String(triageOf(lead).jd_url || (lead && lead.canonical_url) || '')).trim();
    const lid = String((lead && lead.lead_id) || '');
    const dir = String(leadsDir || LEADS_DIR).replace(/\/?$/, '/');
    return LEAD_ID_RE.test(lid) ? `${cmd} --lead ${dir}${lid}.json` : cmd;
  }

  function isTerminalInProgress(lead) {
    const js = jobOf(lead);
    return js.kind === 'terminal' && js.state === 'in_progress';
  }

  // opts.dispatchEnabled === false (job_apply module off): no Applied / Need help, which need the claude CLI.
  function actionsFor(lead, placed, opts) {
    const dirty = !!(opts && opts.settingsDirty);
    const dispatch = !(opts && opts.dispatchEnabled === false);
    const s = sectionOf(lead);
    if (!s || s === 'applied' || decisionOf(lead).pending || isActive(jobOf(lead))) return [];
    if (s === 'airtable') return [{ action: 'skip', label: 'Skip' }];
    if (s === 'closed') return [{ action: 'skip', label: 'Confirm closed', reason: 'closed_stale' },
      { action: 'still_open', label: 'Still open' }];
    // Normally the terminal run's completion line moves the card on its own; this is the fallback
    // (the server dispatches it with --from-terminal so the existing Airtable record is reused).
    const appliedLabel = isTerminalInProgress(lead) ? 'Applied (done in terminal)' : 'Applied';
    const out = dispatch ? [{ action: 'applied', label: appliedLabel }, { action: 'help', label: 'Need help' }] : [];
    if (s === 'board' && placed) {
      if (S.stepBand(placed.band, +1)) out.push({ action: 'move_up', label: UP + ' Move up' });
      if (S.stepBand(placed.band, -1)) out.push({ action: 'move_down', label: DOWN + ' Move down' });
      if (placed.overridden) out.push({ action: 'reset_band', label: 'Reset' });
    }
    out.push({ action: 'skip', label: 'Skip' });
    return dirty ? out.map((a) => (BAND_ACTIONS.includes(a.action) ? { ...a, disabled: true, hint: MOVE_BLOCKED_HINT } : a)) : out;
  }

  function matchesQuery(lead, query) {
    const q = String(query || '').trim().toLowerCase();
    if (!q) return true;
    const t = triageOf(lead);
    return `${t.company || ''} ${t.role_title || ''}`.toLowerCase().includes(q);
  }

  function rank(lead) {
    const r = FIT_ORDER[triageOf(lead).fit_score];
    return r === undefined ? 3 : r;
  }

  function compareLeads(a, b) {
    if (rank(a) !== rank(b)) return rank(a) - rank(b);
    return String(triageOf(a).company || '').localeCompare(String(triageOf(b).company || ''));
  }

  function countSections(leads) {
    const counts = {};
    SECTIONS.forEach((s) => { counts[s.key] = 0; });
    (leads || []).forEach((l) => { const s = sectionOf(l); if (s) counts[s] += 1; });
    return counts;
  }

  function groupLeads(leads, filter, query, ranked, bands) {
    const keys = filter ? [filter] : DEFAULT_SECTIONS;
    return keys.map((key) => {
      const members = (leads || []).filter((l) => sectionOf(l) === key && matchesQuery(l, query));
      if (key !== 'board') {
        members.sort(compareLeads);
        return { key, label: GROUP_LABELS[key] || sectionLabel(key), leads: members };
      }
      const ids = new Set(members.map((l) => l.lead_id));
      const placedBands = S.placeBands((ranked || []).filter((r) => ids.has(r.lead.lead_id)), bands || S.DEFAULT_BANDS, overrideOf);
      let pos = 0;
      placedBands.forEach((b) => b.items.forEach((it) => { pos += 1; it.position = pos; }));
      return { key, label: sectionLabel(key), leads: placedBands.flatMap((b) => b.items.map((it) => it.lead)), bands: placedBands };
    });
  }

  function bandHeaders(group, collapsed) {
    return (group.bands || []).map((b) => ({ key: b.key, label: b.label, count: b.items.length,
      expanded: !(collapsed && collapsed.has(b.key)) }));
  }

  function daysText(d) {
    if (!d || d.days == null) return null;
    return (d.basis === 'first_seen' ? 'first seen ' : 'posted ') + d.days + ' d';
  }

  const SEP = ' \u00b7 ';
  function cardModel(lead, ranked) {   // ranked = a placeBands item (+ position), or null
    const t = triageOf(lead);
    if (!ranked) {
      const failed = t.triage_status === 'failed';
      return { scoreText: null, rank: null, gated: false, band: null, colorBand: null, movedByYou: false,
        note: failed ? 'Triage failed: ' + (t.triage_error || 'unknown') : 'Needs re-triage',
        summary: '', yourNote: lead.source_note || '', meta: [t.location].filter(Boolean).join(SEP), pay: null, fit: t.fit_rationale || '',
        gap: '', whyRank: '', breakdown: [] };
    }
    const s = ranked.score;
    const locs = Array.isArray(t.locations) && t.locations[0] ? t.locations.map((l) => l.label).join(' / ') : (t.location || '');
    const mod = t.modality && t.modality !== 'unknown' ? t.modality : null;
    const note = S.overrideNote(ranked);
    return {
      scoreText: String(s.composite), rank: ranked.position || ranked.rank, gated: s.gated, note: null,
      band: ranked.band || s.band, colorBand: s.band, movedByYou: !!ranked.marked,
      summary: t.job_summary || '', yourNote: lead.source_note || '',
      meta: [locs, mod, daysText(s.days_open)].filter(Boolean).join(SEP),
      pay: { text: s.pay_display.text, kind: s.pay_display.kind, unspecified: s.flags.includes('comp_unspecified') },
      fit: s.factors[0].evidence, gap: t.main_gap || '',
      whyRank: (note ? note + ' ' : '') + s.why_rank,
      breakdown: s.factors.map((f) => ({ label: f.label, score: Math.round(f.score), pct: Math.round(f.weight * 100),
        points: Math.round(f.points * 10) / 10, evidence: f.evidence })),
    };
  }

  function scoreSig(r) {
    return r ? [r.score.composite, r.position || r.rank, r.band, r.overridden, r.marked].join('|') + '|'
      + r.score.factors.map((f) => f.score + ':' + f.weight.toFixed(4)).join(',') : 'none';
  }
  // No shown score while the drawer holds unsaved changes: the preview is not what the saved config ranks (4.11).
  function shownFor(placed, settingsDirty) {
    if (!placed || settingsDirty) return null;
    return { score: placed.score.composite, rank: placed.position || placed.rank, band: placed.computed_band };
  }

  // Ranking Settings drawer logic (pure): drafts, presets, thresholds, steppers, save outcomes.
  const THRESHOLD_STEP = 5;
  const FLOOR_STEP = 5000;
  const FLOOR_MIN = 10000;
  const FLOOR_MAX = 1000000;

  function draftFromConfig(cfg) {
    const w = {};
    S.FACTOR_KEYS.forEach((k) => { w[k] = Math.round(Number(cfg.active_weights[k]) * 100); });
    const b = cfg.bands || S.DEFAULT_BANDS;
    return { weights: w, preset: cfg.active_preset || null, floor: cfg.compensation_settings.annual_base_floor,
      bands: { apply_now_min: b.apply_now_min, worth_a_look_min: b.worth_a_look_min } };
  }
  function isDirty(draft, cfg) { return !!(draft && cfg) && JSON.stringify(draft) !== JSON.stringify(draftFromConfig(cfg)); }
  function percentages(weights) {
    const n = S.normalizeWeights(weights);
    const out = {};
    S.FACTOR_KEYS.forEach((k) => { out[k] = Math.round(n[k] * 100); });
    return out;
  }
  function presetFor(draft, presets) {
    const pct = percentages(draft.weights);
    return Object.keys(presets || {}).find((name) => S.FACTOR_KEYS.every((k) => Math.round(presets[name][k] * 100) === pct[k])) || null;
  }
  function bandsProblem(draft, ceiling) {
    const b = draft.bands;
    if (!(b.apply_now_min > b.worth_a_look_min)) return 'Apply now must start above Worth a look';
    if (!(b.worth_a_look_min > ceiling)) return `Worth a look must start above ${ceiling} (the pay-floor cap)`;
    if (b.apply_now_min > 100 || b.worth_a_look_min < 1) return 'Thresholds run from 1 to 100';
    return null;
  }
  function previewConfig(saved, draft) {
    const ceiling = saved.compensation_settings.hard_gate_clamp_ceiling;
    return { ...saved, active_weights: S.normalizeWeights(draft.weights),
      compensation_settings: { ...saved.compensation_settings, annual_base_floor: draft.floor },
      bands: draft.bands && !bandsProblem(draft, ceiling) ? { ...draft.bands } : saved.bands };
  }
  function stepTo(v, step, dir, lo, hi) {
    const n = Number(v) || 0;
    const next = dir > 0 ? (Math.floor(n / step) + 1) * step : (Math.ceil(n / step) - 1) * step;
    return Math.min(hi, Math.max(lo, next));
  }
  function stepThreshold(v, dir) { return stepTo(v, THRESHOLD_STEP, dir, 1, 100); }
  function stepFloor(v, dir) { return stepTo(v, FLOOR_STEP, dir, FLOOR_MIN, FLOOR_MAX); }
  function formatMoney(n) { return '$' + Math.round(Number(n) || 0).toLocaleString('en-US'); }
  function settingsSaveOutcome(status) {
    if (status >= 200 && status < 300) return 'saved';
    if (status === 409) return 'conflict';
    if (status === 422) return 'invalid';
    return 'error';
  }
  function rankButtonText({ draft, config, usingDefaults, dirty }) {
    const name = draft && config ? (presetFor(draft, config.presets) || 'Custom') : '\u2026';
    return 'Ranking: ' + name + (usingDefaults ? ' (default settings)' : '') + (dirty ? ' \u2022 not saved' : '');
  }

  function formatCountdown(seconds) {
    const n = Math.max(0, Math.floor(Number(seconds) || 0));
    return `${Math.floor(n / 60)}:${String(n % 60).padStart(2, '0')}`;
  }

  // Server-computed remaining seconds minus time since that response arrived: immune to phone clock skew.
  function remainingSeconds(lead, nowMs) {
    if (!lead || lead.undo_remaining_s == null) return null;
    const received = typeof lead._receivedAt === 'number' ? lead._receivedAt : nowMs;
    return Math.max(0, lead.undo_remaining_s - Math.floor((nowMs - received) / 1000));
  }

  function prepHref(path) {
    return path ? '/api/files/' + String(path).split('/').map(encodeURIComponent).join('/') : null;
  }

  function statusLine(lead, remaining) {
    const d = decisionOf(lead);
    const js = jobOf(lead);
    if (d.pending) {
      const left = remaining == null ? lead.undo_remaining_s : remaining;
      return { kind: 'pending', text: `Pending (Undo ${formatCountdown(left)})` };
    }
    if (js.state === 'extracting') return { kind: 'running', text: 'Reading the form' + ELLIPSIS };
    if (js.state === 'starting') return { kind: 'running', text: 'Starting' + ELLIPSIS };
    if (js.state === 'running') {
      return { kind: 'running', text: js.kind === 'prep' ? 'Drafting prep pack' + ELLIPSIS : 'Logging to Airtable' + ELLIPSIS };
    }
    if (js.state === 'done' && js.kind === 'applied') {
      return { kind: 'done', text: `Logged ${CHECK} ${d.airtable_record_id || js.record_id || ''}`.trim() };
    }
    if (js.state === 'done' && js.kind === 'prep') {
      return { kind: 'done', text: `Prep ready ${ARROW} open`, href: prepHref(d.prep_file || js.prep_file) };
    }
    if (js.state === 'failed') return { kind: 'failed', text: `Failed: ${js.error || 'unknown error'}`, button: 'retry' };
    if (js.state === 'needs_permission') {
      // The prep drafter is locked down (nothing can be granted to it), so prep offers Retry, never Grant.
      return { kind: 'permission', text: `Needs permission: ${(js.denied_tools || []).join(', ')}`,
        button: js.kind === 'prep' ? 'retry' : 'grant' };
    }
    if (js.kind === 'terminal') return { kind: 'info', text: 'Continue in terminal' };
    if (d.prep_file) return { kind: 'done', text: `Prep ready ${ARROW} open`, href: prepHref(d.prep_file) };
    return null;
  }

  function pollIntervalMs(leads) {
    return (leads || []).some((l) => decisionOf(l).pending || isActive(jobOf(l))) ? FAST_POLL_MS : SLOW_POLL_MS;
  }

  function undoBars(leads, nowMs) {
    return (leads || [])
      .filter((l) => decisionOf(l).pending)
      .map((l) => {
        const p = decisionOf(l).pending;
        return { lead_id: l.lead_id, requested_at: String(p.requested_at || ''),
          label: `${ACTION_LABELS[p.action] || 'Action'}: ${triageOf(l).company || 'lead'}`,
          remaining: remainingSeconds(l, nowMs) };
      })
      .filter((u) => u.remaining > 0)
      .sort((a, b) => (a.requested_at < b.requested_at ? 1 : a.requested_at > b.requested_at ? -1 : 0))
      .slice(0, MAX_UNDO_BARS)
      .map(({ lead_id, label, remaining }) => ({ lead_id, label, remaining }));
  }

  function stampLeads(leads, nowMs) { return (leads || []).map((l) => ({ ...l, _receivedAt: nowMs })); }

  function replaceLead(leads, updated, nowMs) {
    const stamped = { ...updated, _receivedAt: nowMs };
    let found = false;
    const out = (leads || []).map((l) => {
      if (l.lead_id === updated.lead_id) { found = true; return stamped; }
      return l;
    });
    if (!found) out.push(stamped);
    return out;
  }

  const api = {
    SECTIONS, DEFAULT_SECTIONS, SKIP_REASONS, FAST_POLL_MS, SLOW_POLL_MS, MAX_UNDO_BARS,
    sectionOf, overrideOf, sectionLabel, warmCurrentContact, badgesFor, networkLine, safeHref, postingLink,
    engageLink, terminalCommand, actionsFor, MOVE_BLOCKED_HINT, bandHeaders, matchesQuery, countSections, groupLeads,
    cardModel, scoreSig, shownFor,
    draftFromConfig, isDirty, presetFor, percentages, previewConfig, bandsProblem, stepThreshold, stepFloor, formatMoney,
    settingsSaveOutcome, rankButtonText, FLOOR_STEP, THRESHOLD_STEP, formatCountdown, remainingSeconds, prepHref, statusLine, pollIntervalMs, undoBars, stampLeads, replaceLead,
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.JobsLogic = api;
})(typeof window !== 'undefined' ? window : globalThis);
