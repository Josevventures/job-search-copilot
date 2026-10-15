"""Checkpoint v3 schema (ranking-engine spec §3.1): what a triage subagent must return."""
from datetime import date

CHECKPOINT_VERSION = 3
REQUIRED_ALWAYS = {"url", "lead_id", "batch_id", "status", "fetch_method"}
REQUIRED_COMPLETE = {"checkpoint_version", "company", "role_title", "location", "posting_status", "pay_type",
                     "jd_read", "airtable_match", "fit_score", "fit_rationale", "network", "engage"}
REQUIRED_V3 = {"job_summary", "scores", "main_gap", "level", "locations", "modality", "relocation_assistance",
               "pay_bands", "pay_source", "bonus", "equity", "posted_date", "posted_date_source", "reposted",
               "applicant_count", "inbound"}
METROS = {"new_york", "north_jersey", "remote_us", "san_francisco", "boston", "austin", "seattle", "los_angeles",
          "washington_dc", "chicago", "other_us", "non_us"}
INBOUND_KINDS = {"recruiter_outreach", "hiring_manager_post", "referral_offered"}
ENUMS = {
    "status": {"complete", "failed"},
    "posting_status": {"open", "closed", "unknown"},
    "pay_type": {"salary", "equity_only", "unpaid", "unknown"},
    "fit_score": {"High", "Medium", "Low", None},
    "level": {"entry", "mid", "senior", "manager", "director_plus", "unknown"},
    "modality": {"remote", "hybrid", "onsite", "unknown"},
    "pay_source": {"posting", "ats_api", "none"},
    "posted_date_source": {"posting", "ats_api", "linkedin_relative", "none"},
}
BAND_PERIODS = {"year", "hour", "month"}
# Sanity bounds per period (review minor 3): a figure outside them is a parsing error, not a salary.
BAND_BOUNDS = {"year": (20_000, 2_000_000), "hour": (10, 500), "month": (1_500, 170_000)}
BAND_BASES = {"base", "ote"}
RELATIONS = {"none", "same_role", "reapply_candidate", "adjacent_opening"}
CONTACT_ENUMS = {"status": {"current", "former"}, "warmth": {"warm", "cold"}}
TRIAGE_KEYS_FROM_CHECKPOINT = ("checkpoint_version", "company", "role_title", "location", "pay", "pay_type", "jd_url",
                               "jd_read", "jd_note", "posting_status", "fit_score", "fit_rationale", "network",
                               "engage", "airtable_match", "eligibility_note", "alternates", "draft_answers",
                               "fetch_method", *sorted(REQUIRED_V3))


def _not_in(v, allowed) -> bool:
    try:
        return v not in allowed
    except TypeError:  # unhashable value (list, dict)
        return True


def _is_int(v, lo, hi) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def _is_text(v, n, nullable=False) -> bool:
    if v is None:
        return nullable
    return isinstance(v, str) and 0 < len(v.strip()) and len(v) <= n


def _v3_problems(d: dict) -> list[str]:
    probs = [f"missing {k}" for k in sorted(REQUIRED_V3 - d.keys())]
    scores = d.get("scores") if isinstance(d.get("scores"), dict) else {}
    for k in ("role_fit", "domain"):
        s = scores.get(k)
        if not (isinstance(s, dict) and _is_int(s.get("score"), 0, 100) and _is_text(s.get("evidence"), 160)):
            probs.append(f"bad scores.{k}: need {{score: int 0-100, evidence: 1-160 chars}}")
    if "job_summary" in d and not _is_text(d["job_summary"], 120):
        probs.append("bad job_summary: 1-120 chars")
    if "main_gap" in d and not _is_text(d["main_gap"], 120, nullable=True):
        probs.append("bad main_gap: null or 1-120 chars")
    locs = d.get("locations")
    if "locations" in d and not isinstance(locs, list):
        probs.append("bad locations: must be a list")
    for i, l in enumerate(locs if isinstance(locs, list) else []):
        if not (isinstance(l, dict) and _is_text(l.get("label"), 120) and not _not_in(l.get("metro"), METROS)):
            probs.append(f"bad locations[{i}]: need {{label, metro in METROS}}")
    bands = d.get("pay_bands")
    if "pay_bands" in d and not isinstance(bands, list):
        probs.append("bad pay_bands: must be a list")
    for i, b in enumerate(bands if isinstance(bands, list) else []):
        per = b.get("period") if isinstance(b, dict) else None
        lo, hi = BAND_BOUNDS.get(per, (1, 0)) if isinstance(per, str) else (1, 0)
        ok = (isinstance(b, dict) and not _not_in(b.get("location_metro"), METROS)
              and _is_int(b.get("min"), lo, hi) and _is_int(b.get("max"), lo, hi)
              and b["min"] <= b["max"] and not _not_in(b.get("basis"), BAND_BASES))
        if not ok:
            probs.append(f"bad pay_bands[{i}]: need location_metro, int min<=max within the period's bounds "
                         f"(year 20000-2000000, hour 10-500, month 1500-170000), basis base|ote")
    for k in ("relocation_assistance", "bonus", "equity", "reposted"):
        if k in d and d[k] not in (True, False, None):
            probs.append(f"bad {k}: true, false or null")
    pd = d.get("posted_date")
    if pd is not None:
        try:
            ok = isinstance(pd, str) and len(pd) == 10 and date.fromisoformat(pd) is not None
        except ValueError:
            ok = False
        if not ok:
            probs.append(f"bad posted_date {pd!r}: YYYY-MM-DD or null")
    ac = d.get("applicant_count")
    if ac is not None and not _is_int(ac, 0, 100_000):
        probs.append(f"bad applicant_count {ac!r}")
    ib = d.get("inbound")
    if ib is not None and not (isinstance(ib, dict) and not _not_in(ib.get("kind"), INBOUND_KINDS)
                               and isinstance(ib.get("note", ""), str)):
        probs.append(f"bad inbound {ib!r}")
    return probs


def validate(d: dict) -> list[str]:
    if not isinstance(d, dict):
        return ["not an object"]
    probs = [f"missing {k}" for k in sorted(REQUIRED_ALWAYS - d.keys())]
    if _not_in(d.get("status"), ENUMS["status"]):
        probs.append(f"bad status {d.get('status')!r}")
    if d.get("status") != "complete":
        return probs
    if d.get("checkpoint_version") != CHECKPOINT_VERSION:
        probs.append(f"stale_prompt: checkpoint_version must be {CHECKPOINT_VERSION}")
    probs += [f"missing {k}" for k in sorted(REQUIRED_COMPLETE - d.keys())]
    for k, allowed in ENUMS.items():
        if k in d and k != "status" and _not_in(d[k], allowed):
            probs.append(f"bad {k} {d[k]!r}")
    am = d.get("airtable_match")
    rel = am.get("relation") if isinstance(am, dict) else None
    if _not_in(rel, RELATIONS):
        probs.append(f"bad airtable_match.relation {rel!r}")
    if isinstance(am, dict) and am.get("stage") is not None and not isinstance(am.get("stage"), str):
        probs.append("bad airtable_match.stage: string or null")
    if not isinstance(d.get("jd_read"), bool):
        probs.append("jd_read must be bool")
    net = d.get("network")
    contacts = net.get("contacts") if isinstance(net, dict) else None
    if not isinstance(contacts, list):
        probs.append("network.contacts must be a list")
    else:
        for i, c in enumerate(contacts):
            if not isinstance(c, dict):
                probs.append(f"network.contacts[{i}] is not an object")
                continue
            if not c.get("name"):
                probs.append(f"network.contacts[{i}] missing name")
            for k, allowed in CONTACT_ENUMS.items():
                if _not_in(c.get(k), allowed):
                    probs.append(f"bad network.contacts[{i}].{k} {c.get(k)!r}")
    if rel != "same_role":
        probs += _v3_problems(d)
    return probs
