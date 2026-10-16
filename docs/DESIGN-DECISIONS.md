# Design decisions

One entry per real decision, in the order they were made, taken from the change log and design specs of the
private system this was extracted from. Each entry: the problem, what was tried, what was measured, the
decision. Failures and reversals are kept. Company and person names from the records are replaced; numbers are
as recorded. Test counts are those of the private suites at the time, which covered more than this repo.

The first three entries concern the scheduled email scan that keeps my application tracker current. That routine
is not part of this repo, but its failures shaped the record-matching rules `/job-apply` uses.

---

## 2026-09-09 — Keyword queries silently missed two emails

**Problem.** A scheduled routine searched my inbox for application emails with two keyword queries. It missed an
interview invite (the subject had a typo, and the body said "are you free this week" instead of "schedule") and a
rejection that said "not to move forward" instead of "not moving forward". It reported "done".

**Tried.** Found both by searching on the company name, fixed the two tracker records by hand.

**Measured.** 2 actionable emails never fetched, so classification never saw them.

**Decision.** Add a fallback pass by company name. The session that found it could not edit the routine, so the
fix waited two days. Lesson recorded: a brittle keyword list fails silently, which is worse than failing loudly.

## 2026-09-11 — The fallback query, and a field that did not exist

**Problem.** The routine still had the narrow queries, wrote to a field name that did not exist, and never set the
next-interview date.

**Decision.** Rewrote the routine: a required third query that lists companies with open applications and searches
each company and recruiter name; wider phrases for the first two; the next-interview date set and the follow-up
date cleared once an interview is booked. The change was verified only by re-reading the saved routine, and the
entry says so: "not yet verified against a live run".

## 2026-09-28 — The scan wrote one role's interview onto another role's record

**Problem.** I had two open applications at the same company: one live in a first-round interview, one dormant.
The company-name query had no rule for a company with several open records. It picked the dormant record and wrote
the live role's status, stage, interview date and interviewer schedule onto it. The live record was never
updated. Then the weekly status report read the change and called it a successful sync, in four places, citing the
wrong record.

**Tried.** Reset the dormant record with a dated correction note, re-read it, regenerated the report.

**Measured.** A sweep of all 305 tracker records (27 open) found one company with more than one open application.
The first pass counted 26 open; it had dropped a record, so the count was re-derived from the full list rather than
patched. The first corrected routine also stated, as fact, that a second company had two open applications; that
came unverified from the regenerated report and was wrong, and a second edit removed it.

**Decision.** Match to the role, not the company: build the candidate set for the company, accept exactly one,
disambiguate by role title, then posting URL, then recruiter; if it still is not one record, write nothing and
report the email as ambiguous. The post-write check confirms the role title, not just that a value was saved.
`/job-apply`'s rule for finding an earlier record (exactly one match reuses it, more than one stops and names
them) follows the same idea. [JOSE TO CONFIRM: that the /job-apply rule was written with this incident in mind]

## 2026-09-30 — Triage v2: resolve links before anyone reads them

**Problem.** The first batch run took a spreadsheet of 82 links and produced 86 triaged leads with 0 failures, and
the output was still hard to act on:
- 74 of the 82 links were LinkedIn hiring posts, not postings. The real link sat behind LinkedIn's "you're leaving"
  page, which the first wave of agents did not follow, so 3 of the first 5 scores came from post text alone.
- Roundup posts had to be expanded into 27 leads by hand.
- Non-jobs and internships were screened by hand; two graduate programs were skipped without checking the
  graduation window.
- JS-only career pages came back empty, and each agent improvised a different fallback.
- The fit rubric produced 1 High / 28 Medium / 45 Low, while my real "apply first" list had 11 rows built on
  judgment the rubric did not encode. Not reproducible.
- About 86 agents at about 70K tokens each, with no estimate shown before the run.

**Decision.** Four stages with a script at each end:
1. Intake, no LLM: unwrap redirects, resolve short links, expand roundups (3+ distinct job links), canonicalize,
   one lead file per job, keyed by a hash of the canonical URL.
2. Screen with one Haiku call per 50 leads, failing open; dedupe against the tracker.
3. Triage in waves of 6, one checkpoint per lead through a manifest; stop if more than half fail; confirm before
   more than 20 agents; one prescribed fetch ladder.
4. Merge with a script: validate every checkpoint; malformed means failed, never half-merged.

Each field group of a lead file has exactly one writer, and the store raises on a write outside the group.

**Measured.** 79 tests at completion, 90 by the end of the review. A dry run of intake on the real spreadsheet:
134 created, 3 merged, 7 roundups, 1 login-walled, 0 crashes.

**Reversal.** A planned 10-lead re-triage sample to compare the new rules with my hand tiers was dropped: the
tests and a clean live intake were judged enough, and the sample would have cost about 0.7M tokens. The review
left one item open, per-URL error isolation in intake, flagged as required before any unattended run.

## 2026-10-01 — The Jobs board: act, then let the system know

**Problem.** I was reviewing 86 leads as a terminal table. I wanted to act on each one from my phone and have the
system know what I did.

**Decision.**
- A phone-first page on a local server, reachable only on my private network.
- Every action is first written as pending with an execute-after time 90 seconds out; a sweeper runs every 5
  seconds. Undo works inside the window, nothing irreversible happens before it closes, and the pending action
  lives in the lead file, so a restart cannot lose it.
- Skip needs a reason (7 chips) and writes a skip log line before deleting the lead; intake refuses to re-create a
  skipped lead, so I never pay to re-triage a job I rejected.
- Only the board writes the decision group. Dispatched jobs report back with one marker line; no marker means the
  job failed.

**Measured and fixed in review.** Dispatch happened before the state write, which could have logged an application
twice: fixed with a `starting` state written first, and a `starting` job without an id is failed, never
re-dispatched. The prep-file path check now resolves the path, closing `..` and symlink escapes. The Undo button
was rebuilt every second and lost about 10% of taps; the 5-second refresh lost about 2% of card taps; fixed with
in-place patching and signature-gated rebuilds. Tests: 122 skill, 281 backend, 40 JS.

**Failure.** The plan was for prep to read the application form through my logged-in browser. A feasibility spike
said stop: a headless run loaded no browser tools, and with the browser connected every page read was refused.
Prep shipped without a form map.

## 2026-10-01 — The form extractor: take the model out of the browser

**Problem.** The next design let a model drive my logged-in browser behind a pre-tool guard. A security review
showed a hostile job page could get around it: the agent still had tools that could disable the guard, a `\` in a
URL beat the host check, "Continue" links escaped the allowlist, and element references were not tied to a tab.
That branch was never merged.

**Decision.** Two steps. A deterministic extractor with no model in it reads the form (Greenhouse API first, then
Lever/Ashby DOM adapters, then a generic one) in a fresh headless browser behind a network fence, with
navigation-only click rules where a deny word always wins, and it never types, uploads, submits or signs in. Then
a model drafts the answers with no browser, no web and no shell, under a per-job settings file and a hook that
fails closed and is self-tested before every dispatch, because a hook that cannot run fails open.

**Measured.** Fixed during the build, each reproduced first: redirect and userinfo `Location` headers bypassed the
fence; WebSockets were unfenced; five probes beat a single-label deny rule and clicked and posted; a busy-loop page
hung the CLI (now a watchdog writes a stub and kills the process tree); Grep and Glob reached an `.env` past the
secret guard; a mixed-slash network path opened an SMB connection before the deny (a 21-second stall). Tests at
merge: 234 extractor, 501 backend, 41 JS, 122 skill. A read-only check on three live forms: 12 fields in 0 s, 25 in
7 s, 15 in 4 s, no clicks, no errors.

## 2026-10-03 — The terminal completion inbox

**Problem.** "Continue in terminal" copied a bare `/job-apply <url>`. The terminal run logged the application, but
the board never learned which lead it was; the card stayed "in progress", and tapping Applied would have run the
whole flow again.

**Rejected.** Letting the skill write the lead file (breaks the single-writer rule). A byte-offset reader (cannot
re-check a deferred line, breaks if the file is edited by hand).

**Decision.** The copied command carries `--lead <lead file>`. After logging, the skill appends one line to an
append-only inbox through an append-once helper. The board applies a line only when the lead is in terminal
progress with nothing pending and no job running; otherwise it waits and re-checks. A hash-keyed sidecar records
each line's outcome, but idempotent application, not the sidecar, is the safety mechanism. Tests: 532 backend
(31 new), 42 JS.

## 2026-10-13 — A 1-100 score replaces tiers

**Problem.** Ranking one company's open roles by hand showed what the tier rules got wrong: they favored roles on
odds alone, capped product roles at the second tier, ignored pay, location and posting age, and showed a rule name
as the "why".

**Decision.** One board sorted by a weighted 1-100 composite of six factors. Only role fit and domain use model
judgment; the other four are arithmetic on extracted facts, and missing data is "no signal", never a penalty. The
pay floor is the only hard gate, and only on verified pay. Location never disqualifies. The weights are mine to
change from a drawer on the phone, and the formulas live in one JavaScript file: the browser has to re-rank on
every slider move, any Python copy would drift, and Python reads scores through a Node bridge, checked by one
golden fixture in both test suites. "Judgment needs the LLM; arithmetic doesn't." Every move between bands is
logged with the score, rank, bands and config version that were on screen.

**Rejected as ground truth: my own hand ranking.** It was a learning exercise on a small pool, done quickly. The
rule written down: never use it as ground truth. The golden fixtures are synthetic leads that check the formulas,
not taste. Whether the order is right is my call, not a test.

**Measured.** 80 leads scored and sorted in under 5 ms (median and p95, 200 timed runs after 20 warm-ups). Tests
before merge: 1,496 backend, 142 JS, 125 skill, with 2 failures that were already failing before this work.

## 2026-10-14 — Calibration on 40 real leads

**Problem.** The new score had to be checked against real leads before I relied on it.

**Tried.** A closed-posting check first; I confirmed the list and 27 closed or dead leads left through the board's
own Skip, so each kept its skip record. Then a 3-lead pilot, then 40 leads re-triaged into the new shape. Market
pay bands per level were derived from 27 verified postings (median of the posted ranges, only levels with at least
3).

**Measured.** The board came out 2 Apply now, 22 Worth a look, 13 Low. My verdict on the order: "looks right".
There is no agreement metric, on purpose (see the entry above). The record does not say where the other 3 of the
40 landed. [JOSE TO CONFIRM: the remaining 3 of the 40 re-triaged leads]

## 2026-10-14 — Slack intake: the model never touches Slack

**Problem.** Links only reached the pipeline through a terminal session. I wanted to drop a link in Slack during
the day and see it scored on the board the next morning.

**Decision.** A deterministic Python wrapper owns Slack, the ledger, the cap and the replies; one headless run does
only screen, triage and merge, and the wrapper trusts its result file, never its prose. Rejected: a single headless
session that reads Slack and replies (the cursor and the replies in the model's hands, so a run that dies midway
means duplicate or missing replies), and triage written in plain Python against the API. It runs as a local
scheduled task, not in the cloud, because LinkedIn walls cloud IP ranges and the lead files stay local.

- **Per-URL error isolation shipped first, on its own** — the open item from the 09-30 review.
- Cap of 20 leads a night; the rest go first the next night. Replies wait for quiet hours to end.
- A duplicate-reply guard: the wrapper looks for its own final marker in the thread before posting.
- A failed triage gets one automatic retry and one per re-drop, counted when queued and refunded if the whole run
  failed.
- Workday postings are prefetched by Python (JS-only pages) under a narrow scope: https, Workday hosts only, GET
  only, 15 s, 2 MB.
- Health: a heartbeat before anything else, one alert per night.

**Measured.** Skill tests went from 140 to 222. First live run: 12 messages with 13 links, all 13 triaged and on
the board, 12 final replies, 11 descriptions read in full, 2 login-walled posts triaged from the post text, 1
Workday description prefetched, about 4 minutes.

## 2026-10-15 — Acceptance: three ATSs, zero submissions

**Test.** From my phone, a prep pack on three live forms, one per ATS: Greenhouse (API, 12 fields), Lever (DOM, 15
fields), Ashby (DOM, 5 fields). No clicks on any of them. Each produced a pack and its frozen twin, and each form
map matched a read-only dry run made just before.

**Measured.** Zero submissions: no confirmation email from any of the three. Each drafter tried at least one tool
call the lockdown refused (a Glob, a Grep or a Read outside the allowlist) and still saved its pack, and the card
showed done, as designed. That doubled as the lockdown's smoke test.

**Fixed after.** The Ashby adapter emitted an unlabeled radio from a fieldset with no legend (now the question
title is taken from the sibling label, and a control with no readable question is flagged `label_missing`); the
Lever `✱` required marker was not read; the drafter grepped the profile file, which the hook allows only for Read,
so the skill now says Read, never Grep; and the job list said `permission_denied` while the card said done.

**Accepted gaps.** Ashby's required flag sits in a hashed CSS class; matching it would break on Ashby's next
release. Some label evasions (closed shadow DOM, `aria-describedby`, text drawn over an image) can still earn a
click; the write lock limits the worst case to a wrong click or a GET the page defines.

---

## Judgment calls made during extraction (2026-10-15)

Not decisions of the private system; choices made to publish it.

- **Sample values everywhere.** The default weights, pay floor, band thresholds, market bands and home metro are
  invented. The golden fixture's expected scores were recomputed for the sample config; the formulas are
  unchanged.
- **Home metros became config.** The private scorer had two home metros as a constant. Here they are
  `home_metros` in the scoring config, and a home metro is never also scored as a hub.
- **A local record for applications.** The private `/job-apply` logs to Airtable only. Here Airtable is optional,
  so the skill can log a line to `data/applications.jsonl` and the board accepts a standard UUID as a record id.
  That marker form is new.
- **Removed:** the heads-ups the Slack intake opened in my personal assistant, the voice-learning and contact
  skills `/job-apply` calls, the one-off import of the first batch (it held real names), and the parts of the
  dashboard unrelated to jobs.
- **Fixtures.** The LinkedIn post fixtures are invented with the structure of the real guest page. The three ATS
  page fixtures are real captures with every identifying string replaced; see `prep_extract/tests/fixtures/`.
