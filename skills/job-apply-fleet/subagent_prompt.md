# /job-apply-fleet — triage subagent instructions (v3, ranking engine 2026-10-13)

You are a triage subagent. Research exactly ONE lead and write ONE checkpoint file. Read-only everywhere: do not submit applications, do not write to Airtable, do not send messages, do not fill in forms. Working directory: the job-search-copilot repo root.

Your dispatch message gives you: `lead_id`, `url`, `jd_links`, `hint`, `note` (the user's own words about this role from Slack — context only, never a scoring input by itself), `batch_id`, and the checkpoint path `data/.state/checkpoints/<batch_id>/<lead_id>.json`.

Before starting, read the user's triage preferences: `skills/job-apply-fleet/preferences.md` (if it does not exist, `skills/job-apply-fleet/preferences.example.md`, and say so in `jd_note`). They override anything you would assume.

## 1. Get the job description — fetch ladder (stop at the first rung that returns real JD text)

1. WebFetch each of `jd_links`.
2. If the page is empty or JS-only, use the ATS JSON API:
   - Ashby: `https://api.ashbyhq.com/posting-api/job-board/<org>` (find the posting id in the list)
   - Greenhouse: `https://boards-api.greenhouse.io/v1/boards/<org>/jobs/<id>`
   - Workday: first Read `data/.state/checkpoints/<batch_id>/jd/<lead_id>.txt` if it exists (the nightly Slack intake prefetched the JD there; for a Workday lead, check it before rung 1); else WebFetch the cxs API: replace `/<site>/job/...` with `/wday/cxs/<tenant>/<site>/job/...`; curl via Bash only in interactive runs. Prefetched text counts as `"fetch_method": "api"`, `"jd_read": true`.
   - Large-company careers sites: the JSON endpoint the page itself loads for the job id, when there is one
   - Workable: `https://apply.workable.com/api/v1/widget/accounts/<org>`
3. X/Twitter: `https://api.fxtwitter.com/<user>/status/<id>`.
4. LinkedIn post: WebFetch the `url`. A "You're leaving LinkedIn" page contains the real destination (`href` or `?url=`) — follow it. Login-walled posts: use a browser MCP if one is connected (the user's logged-in browser); close any tab you open. Without one, go to the next rung.
5. No link at all: search the company careers page for the role in the post/hint.
6. Everything failed but you have post text: continue on post text, set `"jd_read": false` and explain in `"jd_note"`.
7. Nothing readable at all: write `{"url", "lead_id", "batch_id", "status": "failed", "fetch_method", "error": "fetch_failed"}` and stop.

Record `"fetch_method"` as `"webfetch"`, `"api"`, `"playwright"` (browser), or `"post_text"`.

**Multi-role pages** (a careers list, a post naming several roles): score the single best role for the user and list the rest in `"alternates"` as `[{"role_title", "url"}]`.

## 2. Extract the facts (never estimate; null when the page doesn't say)

- `"company"`, `"role_title"`, `"location"` (free text as posted), `"pay"` (as posted, or null), `"pay_type"` — `"salary"`, `"equity_only"`, `"unpaid"`, `"unknown"` — and `"posting_status"` — `"open"`, `"closed"` (filled / no longer accepting / deadline passed / 404), or `"unknown"`.
- `"job_summary"`: what the job is, one plain line, ≤ 120 characters.
- `"level"`: `"entry"`, `"mid"`, `"senior"`, `"manager"`, `"director_plus"`, or `"unknown"`, as the posting states it.
- `"locations"`: every listed location as `{"label": "<as posted>", "metro": "<slug>"}`, metro one of `"new_york"`, `"north_jersey"`, `"remote_us"`, `"san_francisco"`, `"boston"`, `"austin"`, `"seattle"`, `"los_angeles"`, `"washington_dc"`, `"chicago"`, `"other_us"`, `"non_us"`. Pasadena, Long Beach, Santa Monica = `"los_angeles"`; Jersey City, Hoboken, Newark = `"north_jersey"`. Empty list if none is stated.
- `"modality"`: `"remote"`, `"hybrid"`, `"onsite"`, or `"unknown"`. `"relocation_assistance"`: true / false / null.
- `"pay_bands"`: only ranges printed on the posting or returned by the ATS API, one per location that has one: `{"location_metro": "<slug>", "min": 123000, "max": 165000, "period": "year", "basis": "base"}`. `period` is `"year"`, `"hour"` or `"month"`; `basis` is `"base"`, or `"ote"` when the figure includes commission. Never compute, convert or guess a band. `"pay_source"`: `"posting"`, `"ats_api"`, or `"none"`. When the posting text explicitly says the role is equity-only or unpaid, set `"pay_type"` to `"equity_only"` / `"unpaid"` **and** `"pay_source": "posting"` even though `"pay_bands"` is empty; if you only infer it (no explicit statement), leave `"pay_source": "none"`. `"bonus"`, `"equity"`: true when the posting mentions them, else null.
- `"posted_date"`: `YYYY-MM-DD` or null; `"posted_date_source"`: `"posting"`, `"ats_api"`, `"linkedin_relative"` (converted from "2 weeks ago"), or `"none"`. `"reposted"`: true when the page says reposted, else false/null. `"applicant_count"`: integer when shown (LinkedIn "200+" → 200), else null.
- `"inbound"`: `{"kind": "recruiter_outreach" | "hiring_manager_post" | "referral_offered", "note": "<one line>"}` when someone at the company asked the user to apply (a recruiter message, a hiring manager's own post, an offered referral), else null.

## 3. Airtable re-check (read-only)

Only when `modules.airtable.enabled` is true in `config.yaml` (otherwise set `"airtable_match": {"relation": "none", "record_id": null, "note": null, "stage": null}` and go on). Search the applications table (base `modules.airtable.base_id`, table `modules.airtable.applications_table`) for this company. Set `"airtable_match"` = `{"relation", "record_id", "note"}` where relation is:
- `"same_role"` — same company and same role/req already there. Write the checkpoint now and stop. Every key is required, so write exactly this shape (fill `<...>` from what you know; use null for unknown company/role_title/location):

```json
{"url": "<url>", "lead_id": "<lead_id>", "batch_id": "<batch_id>", "status": "complete", "checkpoint_version": 3, "fetch_method": "<method used, or \"none\">",
 "company": null, "role_title": null, "location": null,
 "posting_status": "unknown", "pay_type": "unknown", "jd_read": false, "jd_url": null,
 "fit_score": null, "fit_rationale": "",
 "network": {"contacts": [], "summary": ""}, "engage": null,
 "airtable_match": {"relation": "same_role", "record_id": "<record id>", "note": "<company / role>"}}
```

- `"reapply_candidate"` — a prior application to this role was rejected/closed and the role is re-posted or re-opened.
- `"adjacent_opening"` — same company and role family, different req/level/location than an existing record.
- `"none"` — otherwise.

When the matched record has a Status, copy it into `"airtable_match"."stage"` (e.g. `"Interviewing"`, `"Rejected"`), else null.

## 4. Network check

Follow `skills/job-apply/SKILL.md` Phase 1.5 exactly (Grep the contacts folder if `modules.job_apply.contacts_dir` is set, search Airtable Contacts if Airtable is on, classify warmth). With neither, write `"network": {"contacts": [], "summary": "No contact sources configured."}`. Store `"network"` = `{"contacts": [...], "summary": "1–2 lines"}`, where every contact has exactly these keys:

```json
{"name": "Pat Example", "title": "VP Strategy, Acme", "status": "current", "warmth": "warm", "recommendation": "Referral ask: reach out before applying"}
```

- `"status"` is `"current"` (works at the company now) or `"former"`. Nothing else.
- `"warmth"` is `"warm"` (a real relationship: an exchange on record, a classmate or colleague who would reply) or `"cold"` (met once, no exchange, or only a shared background). Nothing else; put the detail (how you know them, last contact) in `"recommendation"`.
- `"recommendation"` is the Phase 1.5 action plus one line of context.

A `"warm"` + `"current"` contact is the strongest odds signal on the Jobs tab and is shown on the board as "↑ warm contact", so get these two fields right. Set `"engage"` = `{"name", "url", "note"}` for the best person to message — a warm contact first, else the post author (name + their post/profile URL) — or null.

## 5. Score role fit and domain (the only two judgment scores)

Read the rubrics in the preferences file and apply them exactly. Write `"scores"` = `{"role_fit": {"score": <int 0-100>, "evidence": "<≤160 chars: why he fits>"}, "domain": {"score": <int 0-100>, "evidence": "<≤160 chars>"}}` and `"main_gap"` = the single biggest gap against the basic requirements (≤ 120 chars), or null. Location never lowers role fit. Grad programs outside the window: role fit ≤ 20, explain in `"eligibility_note"`. Do not judge pay, location, odds or freshness: the dashboard computes those from your facts.

## 6. Interview chances (information only)

Apply `skills/job-apply/SKILL.md` "Interview Chances Assessment Criteria" exactly. `"fit_score"` is `"High"`, `"Medium"`, or `"Low"`; `"fit_rationale"` is 2–3 candid sentences.

## 7. Draft answers (optional)

Only if the page exposed real application questions. Read a few files from the user's writing samples folder (`modules.job_apply.writing_samples_dir`) first, if it exists. `"draft_answers"` = `[{"question", "draft"}]`, else null. Never invent questions.

## 8. Write the checkpoint

Exactly these keys (plus `"alternates"` / `"eligibility_note"` when relevant):

```json
{"url": "https://boards.greenhouse.io/acme/jobs/1", "lead_id": "0123456789ab", "batch_id": "batch-x", "status": "complete", "checkpoint_version": 3, "fetch_method": "webfetch",
 "fetched_at": "2026-10-13T12:00:00Z", "company": "Acme AI", "role_title": "Strategy Lead", "location": "New York, NY (hybrid)", "pay": "$140,000 - $160,000",
 "pay_type": "salary", "jd_url": "https://boards.greenhouse.io/acme/jobs/1", "jd_read": true, "jd_note": null, "posting_status": "open",
 "job_summary": "Own AI product strategy and the partner roadmap", "main_gap": "No formal PM title",
 "scores": {"role_fit": {"score": 72, "evidence": "Five years of B2B product work, two on AI tooling"}, "domain": {"score": 90, "evidence": "AI product strategy at a platform company"}},
 "level": "senior", "locations": [{"label": "New York, NY", "metro": "new_york"}], "modality": "hybrid", "relocation_assistance": null,
 "pay_bands": [{"location_metro": "new_york", "min": 140000, "max": 160000, "period": "year", "basis": "base"}], "pay_source": "posting", "bonus": true, "equity": null,
 "posted_date": "2026-10-10", "posted_date_source": "posting", "reposted": false, "applicant_count": null, "inbound": null,
 "fit_score": "Medium", "fit_rationale": "Two or three candid sentences.",
 "network": {"contacts": [], "summary": "No contacts found."}, "engage": null,
 "airtable_match": {"relation": "none", "record_id": null, "note": null, "stage": null},
 "eligibility_note": null, "alternates": [], "draft_answers": null}
```

Then output one line: `Checkpoint written: <lead_id>.json — <Company> / <Role> — fit <role_fit>/domain <domain>` (append ` (JD not read)` if `jd_read` is false).
