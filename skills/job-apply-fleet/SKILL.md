---
name: job-apply-fleet
description: Batch triage for job leads — LinkedIn hiring posts, roundup posts, direct postings, spreadsheets of links. Resolves and expands links into one lead file each, screens out non-jobs and internships, optionally dedupes against Airtable, fans out Sonnet triage subagents in waves of 6, and routes each lead (board / closed / airtable / screened); rank is computed on the Jobs board. Never submits, never writes the Airtable applications table. Use when the user has more than a couple of job links to triage. Invoke with /job-apply-fleet + links, a file, or an .xlsx path.
---

# /job-apply-fleet (v3)

Upstream of `/job-apply`. Output: lead files in `data/leads/` + a summary `data/reports/fleet-review-<date>.md`. This skill **never writes** the Airtable applications table; when the Airtable module is on, Airtable is read once for dedupe. Design: `docs/ARCHITECTURE.md`.

**Announce:** "Using /job-apply-fleet to triage N links."

`PY=python` (the interpreter that has this repo's requirements) · `SK=skills/job-apply-fleet` · run from the repo root. Needs `modules.ai_triage.enabled: true` in `config.yaml`.

## Stage 1 — Intake (script)

```bash
$PY $SK/leads_intake.py --xlsx "<path>" --via sheet      # or --file <txt>, or URLs as args; --via slack|manual
```
Report the JSON line (created / merged / roundups / login_walled / ignored / failed) to the user in one sentence.

## Stage 2 — Screen (Haiku) + Airtable dedupe

1. `$PY $SK/leads_intake.py --export-screen <scratch dir>/screen_in.json`.
2. One Agent call, `model: "haiku"`, per ≤50 items: "Label each item in <file> as `job`, `event`, `non_job`, or `internship_or_undergrad` with a ≤12-word reason. MBA or master's graduate programs are `job`. Write `[{lead_id, label, reason}]` to <out file>." If the call fails, skip this step — the screen fails open.
3. `$PY $SK/leads_intake.py --apply-screen <out file>` then `$PY $SK/fleet_merge.py --screen-only`.
4. Only when `modules.airtable.enabled`: `list_records_for_table` on base `modules.airtable.base_id`, table `modules.airtable.applications_table`, the three fields named in `modules.airtable.fields` (role URL, company, role title), `pageSize` 1000. Save the JSON to the scratch dir, then `$PY $SK/fleet_merge.py --airtable-dedupe <file>`. Airtable off: skip, and say "Airtable dedupe skipped".

## Stage 3 — Triage waves (Sonnet)

1. `BATCH=batch-$(date -u +%Y%m%d-%H%M%S)`; `$PY $SK/fleet_wave.py init --batch $BATCH --max-agents 20`
   - Exit code 3 = over the cap. Tell the user the count and token estimate; re-run with `--yes` only after they confirm. Unattended runs (Slack) pass `--ids-file` + `--limit` instead: the first N in file order are triaged, the rest carried; they never pass `--yes`.
   - Exit code 2 = that batch id already has a manifest (init never overwrites). **Interrupted batch:** run `$PY $SK/fleet_merge.py --batch <old batch>` first (merges whatever checkpoints exist), then `init` a NEW batch id: it selects only pending/failed leads.
   - Re-triage a chosen set: `init --retriage --ids-file <file>` (one lead id per line; `#` comments allowed).
   - Options: `--retriage` (re-score already-triaged leads), `--only-failed` (retry failures).
2. For each wave N: `$PY $SK/fleet_wave.py mark --batch $BATCH N`, then `$PY $SK/fleet_wave.py show --batch $BATCH N` and dispatch one Agent per line, all in one message, `subagent_type: "general-purpose"`, `model: "sonnet"`, prompt:
   ```
   Read your full instructions first: skills/job-apply-fleet/subagent_prompt.md — follow them exactly.
   lead_id: <id>
   url: <canonical_url>
   jd_links: <list or none>
   hint: <hint or none>
   note: <note or none>
   batch_id: <BATCH>
   checkpoint: data/.state/checkpoints/<BATCH>/<id>.json
   ```
   Wait for the whole wave, then `$PY $SK/fleet_wave.py collect --batch $BATCH N`. If it prints `STOP`, stop and tell the user before merging.
3. Relay one-line progress per wave (counts of leads triaged / failed), not per agent.

## Stage 4 — Merge + summary (script)

```bash
$PY $SK/fleet_merge.py --batch $BATCH
$PY $SK/fleet_merge.py --summary
```
Present the run report: counts per route (board / closed / needs re-triage / already in Airtable / needs attention), then the Board table (alphabetical by company: role · location/pay · what the job is · role fit / domain · apply link · engage link · flags). The report is alphabetical on purpose; the ranked order lives on the Jobs board (`python -m copilot serve`), so point the user there and to the summary file.

## Unattended mode (Slack intake scheduled run)

Invoked headless by `jobs_slack_intake.py`. The prompt gives a batch id, an ids file, a limit, a result path, and the literal Python interpreter to call.

- Headless permission rules match the literal command. Use the literal interpreter path and the literal batch id the prompt supplies in every command. No `$PY`, `$SK`, `$(date ...)` or any other shell variable or substitution.
- Every Stage 2/3 command used "as written" below is rewritten literally before it runs: `$PY` → the interpreter path from the prompt, `$SK` → `skills/job-apply-fleet`, `$BATCH` → the batch id the prompt gives.
- If the prompt names previous batches to merge first, run `<interpreter> skills/job-apply-fleet/fleet_merge.py --batch <that id>` for each previous batch, in the order listed, before starting the new batch.
- Skip Stage 1 - the wrapper already ingested. Never ask the user anything; never pass `--yes`.
- Stage 2 as written. Haiku screen failure: skip it (fails open). Airtable call failure, or Airtable off: skip dedupe and remember `skipped`.
- Stage 3: `<interpreter> skills/job-apply-fleet/fleet_wave.py init --batch <batch id> --ids-file <ids file> --limit <limit>`. Waves as written. A `collect` that prints `STOP`: go straight to the Stage 4 merge and pass `--stopped` below.
- Stage 4, in this order (no report to the user - the wrapper replies in Slack):
  - `<interpreter> skills/job-apply-fleet/fleet_merge.py --batch <batch id>`
  - `<interpreter> skills/job-apply-fleet/fleet_merge.py --summary`
- Always finish with `<interpreter> skills/job-apply-fleet/fleet_wave.py result --batch <batch id> --out <result path> --airtable-dedupe ok|skipped [--stopped]`, even after a failure in Stages 2-3 (init first if no batch exists yet, so the result reflects the manifest).

## Rules
- Preferences live in the preferences file (`skills/job-apply-fleet/preferences.md`, copied from `preferences.example.md`). Change them there, never in the prompt.
- Routes come from `lib/routing.py`; never hand-edit a route in a lead file — a user decision goes in the `decision` group (Jobs board).
- Checkpoints and lead files live under `data/`, which is gitignored.
- Run from the repo root; paths are repo-relative.
