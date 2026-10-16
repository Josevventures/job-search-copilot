"""Demo mode (`python -m copilot demo`): the Applied and Prep buttons run without Claude Code.

- Applied writes a real local application record, the same line `/job-apply` writes when the Airtable module is
  off (`data/applications.jsonl`, id from scripts/record_id.py), into the demo's scratch data dir.
- Prep writes a small sample prep pack built from the lead's own (invented) fields, labeled as a demo.

Nothing here is used outside demo mode; the real dispatch path in jobs.py is unchanged.
"""
import importlib.util
import json
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO_LABEL = ("Demo: sample prep pack. With the job_apply module on, this button runs /job-apply through Claude Code "
              "and fills this from the real application form (see docs/SETUP.md).")


def _record_id_mod():
    spec = importlib.util.spec_from_file_location("record_id", ROOT / "scripts" / "record_id.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def slug(text) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text or "lead").lower()).strip("-") or "lead"


def write_local_record(data_dir: Path, lead: dict, now: datetime) -> str:
    """Append the application line once (same marker rule as scripts/append_once.py). Returns the record id."""
    t = lead.get("triage") or {}
    rid = _record_id_mod().local_record_id(lead_id=lead["lead_id"])
    path = Path(data_dir) / "applications.jsonl"
    marker = f'"record_id": "{rid}"'
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if marker not in existing:
        line = {"record_id": rid, "company": t.get("company"), "role_title": t.get("role_title"),
                "role_url": t.get("jd_url") or lead.get("canonical_url"), "location": t.get("location"),
                "application_date": now.date().isoformat(), "status": "Applied",
                "interview_chances": t.get("fit_score") or "Medium",
                "rationale": t.get("fit_rationale") or "Demo record."}
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    return rid


def prep_pack_text(lead: dict, now: datetime) -> str:
    t = lead.get("triage") or {}
    company, role = t.get("company") or "the company", t.get("role_title") or "this role"
    summary = (t.get("job_summary") or "the work described in the posting").rstrip(".")
    fit = ((t.get("scores") or {}).get("role_fit") or {}).get("evidence") or "my recent product work"
    domain = ((t.get("scores") or {}).get("domain") or {}).get("evidence") or "the problem space"
    gap = t.get("main_gap")
    url = t.get("jd_url") or lead.get("canonical_url") or ""
    work = summary[0].lower() + summary[1:]
    # Triage evidence is third-person and often a fragment, so it never goes into an answer; it is listed
    # under "Drafting notes" for the reader instead.
    answers = [
        (f"Why are you interested in {company}?", 1000,
         f"{company} is working on the problem this role exists for: {work}. That is the kind of work I want to "
         f"do next, and I would rather do it where it is the core of the business."),
        ("Tell us about a project that prepared you for this role.", 1500,
         f"I would use one launch I owned end to end: the problem, what we measured, what we cut, and what changed "
         f"after. In this role, the job is to {work}."),
        ("What are your base salary expectations?", 200, "Flexible and open to discuss."),
        ("Are you legally authorized to work in the country of this role?", None,
         "From the profile file (data/profile.md). This sample answers: Yes."),
    ]
    out = [f"# Prep pack — {company} — {role}", "", f"> **{DEMO_LABEL}**", "",
           f"- **Apply URL:** {url}", f"- **Lead:** {lead['lead_id']}",
           f"- **Prepared:** {now.date().isoformat()} (demo; no form was read)",
           "- **questions_visible:** false — demo mode, the standard question set below", "",
           "## Form map", "| # | Label | Type | Required | Limit |", "|---|---|---|---|---|"]
    for i, (q, lim, _) in enumerate(answers, 1):
        out.append(f"| {i} | {q} | textarea | yes | {lim or '—'} |")
    out += ["", "## Answers"]
    for i, (q, lim, a) in enumerate(answers, 1):
        out += [f"### {i}. {q}" + (f" ({len(a)}/{lim} chars)" if lim else ""), a, ""]
    out += ["## Drafting notes (from triage)", f"- Role fit: {fit}", f"- Domain: {domain}", ""]
    out += ["## Open questions", f"- {gap}" if gap else "- None from triage.", ""]
    return "\n".join(out)


def write_prep_pack(prep_dir: Path, lead: dict, now: datetime) -> Path:
    t = lead.get("triage") or {}
    path = Path(prep_dir) / f"{slug(t.get('company'))}-{now.date().isoformat()}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(prep_pack_text(lead, now), encoding="utf-8", newline="\n")
    return path
