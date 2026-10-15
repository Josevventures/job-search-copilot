# job-search-copilot

![tests](https://github.com/Josevventures/job-search-copilot/actions/workflows/tests.yml/badge.svg)

A personal job-search pipeline: drop job links (a URL, a spreadsheet, a LinkedIn hiring post, a Slack message),
and it resolves them into one lead per job, has AI subagents read each posting, ranks everything on a 1-100 score
you can re-weight from your phone, and helps you prepare each application by reading the form and drafting
answers. It never submits anything. You decide on every card; the system records what you decided.

**Status:** finished work sample, maintained on my own cadence. Extracted from a private multi-agent system; the
git history starts at extraction.

## Why this exists

I built this to run my own job search, and I still run it that way.

It started as a spreadsheet of 82 links and a batch of AI agents to triage them. The first run produced 86
triaged leads with no errors, and I still could not act on the result. 74 of the 82 links were LinkedIn hiring
posts, not job postings; the real link sat behind LinkedIn's "you're leaving" page, which the agents did not
follow, so 3 of the first 5 scores were based on the post text alone. Roundup posts listing several roles had to be
split by hand. And the fit rubric said 1 High, 28 Medium, 45 Low, while the list I actually wanted to apply to first
had 11 rows, chosen on judgment the rubric did not encode. The output was a table in a terminal with nowhere to
record a decision.

So I changed the shape rather than the prompt. Links are resolved and expanded by a script before any model sees
them. The model is asked for facts and exactly two judgments (how well I fit, how much I want the domain); the
other four ranking factors are arithmetic on those facts. And the ranked list lives on a phone-friendly board
where every tap is undoable for 90 seconds and is written down with what the card showed at the time.

The part I am most careful about is the application form. An earlier design let a model drive my logged-in
browser, and a security review showed a hostile job page could talk it into clicking or typing. The form is now read
by a deterministic extractor with no model in it, behind a network fence, with click rules where a deny word always
wins. It has read live Greenhouse, Lever and Ashby forms with zero submissions.

## What the system does end to end

```mermaid
flowchart LR
    A[Links<br/>CLI, .xlsx, Slack] --> B[Intake<br/>resolve, expand roundups,<br/>one lead per job]
    B --> C[AI triage<br/>subagents read each posting]
    C --> D[Rank<br/>1-100, six weighted factors]
    D --> E{You decide<br/>on the board}
    E -->|skip, move, undo| D
    E -->|need help| F[Form extractor<br/>reads the form, never submits]
    F --> G[Prep pack<br/>answers drafted for you]
    E -->|applied| H[Log it<br/>local file or Airtable]
```

Every stage is optional except the core (intake, ranking, the board). Each module is one switch in `config.yaml`:

| Module | What it adds | Needs |
|---|---|---|
| **core** | lead files, the ranking engine, the Jobs board, link and file intake | Python 3.11+, Node 18+ |
| `ai_triage` | subagents read each posting and fill in facts and two scores | Claude Code |
| `slack_intake` | post links in a Slack channel; a daily run triages them and replies in the thread | Slack bot token, `ai_triage` |
| `form_extractor` | reads a Greenhouse, Lever or Ashby form into a prep file | Playwright + Chromium |
| `job_apply` | the board's Applied and Need help buttons; the `/job-apply` and `/job-apply-fleet` skills | Claude Code |
| `airtable` | log applications to Airtable and dedupe new leads against it (local files stay the default) | an Airtable base |

The full flow, the data contract between each pair of stages, and what you lose by skipping each one are in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## How it works

**One lead per job, one writer per field group.** A lead is a JSON file keyed by a hash of its canonical URL, so the
same job reached through a post, a roundup and a direct link is one lead. Intake writes the `intake` group, the
merge script writes `triage`, the board writes `decision`; the store raises on any write outside the caller's
group. AI runs never edit a lead: they report back with one marker line.

**Triage in waves, merged by a script.** Subagents work in waves of 6, each writing one checkpoint file through a
manifest; the run asks before spending on more than 20 leads and stops if more than half fail. A script validates
each checkpoint against a schema and either merges it whole or marks the lead failed. The batch pattern is
published separately as [resumable-fanout](https://github.com/Josevventures/resumable-fanout).

**One scorer.** The score is computed in one JavaScript file, in the browser, so the board re-ranks as you move a
weight. Python reaches the same file through Node and never re-implements a formula; one golden fixture is checked
by both test suites. The pay floor is the only hard gate, and only on pay the posting itself states.

**Undo before anything is final.** Every action is pending for 90 seconds; a sweeper executes it after the window.
A skip writes a log line with a snapshot of what the card showed, then deletes the lead, and a skipped job is never
re-created.

**The form is read, never filled.** The extractor clicks only navigation controls on the posting's own host and
blocks every write request after the first page renders. The answers are drafted by a model that has no browser,
no web and no shell, locked down per job by a settings file and a hook that is self-tested before every dispatch.

## Limits and non-goals

- **It never applies for you.** No submit, no sign-in, no auto-fill. That is the design, not a missing feature.
- **It does not find jobs.** You bring the links. LinkedIn posts are read from the public guest page only; a
  login-walled post is triaged from its text unless you connect a browser.
- **The score is not validated against outcomes.** It was checked by eye on 40 of my own leads. There is no
  agreement metric against a hand ranking, on purpose: I tried, and a rushed ranking of a small pool is not ground
  truth.
- **Triage is not free:** about 70K tokens per lead.
- **Single user, no authentication.** The board binds to localhost; reach it from a phone only over a private
  network.
- **The two skills are prompts**, run by Claude Code. Their contracts with the code (flags, marker lines, the inbox
  line) are tested; what the model writes inside them is not.
- **Built and run on Windows.** CI runs on Ubuntu. The extractor's browser tests run where Chromium is installed and
  are skipped in CI.

## Install and run

```bash
git clone https://github.com/Josevventures/job-search-copilot
cd job-search-copilot
pip install -r requirements.txt
python -m copilot demo
```

`demo` copies 19 invented leads into a scratch folder and opens the board at `http://127.0.0.1:8420/jobs`. Your own
data goes in `data/` (gitignored):

```bash
cp config.example.yaml config.yaml
python -m copilot add https://job-boards.greenhouse.io/acme/jobs/123
python -m copilot serve
```

Per-module setup (Slack app scopes, Claude Code skills, Playwright, the Airtable base schema) is in
[docs/SETUP.md](docs/SETUP.md).

## Sample output

The demo board (invented leads), desktop:

![The Jobs board with ranked cards](docs/img/board.png)

One card with **Why this rank** open: each factor's score, weight, points and evidence.

![A card with its factor breakdown](docs/img/why-this-rank.png)

Ranking settings on a phone: presets, six weights, the pay floor and the band thresholds. The board re-ranks as a
slider moves.

![Ranking settings on a phone](docs/img/ranking-settings-phone.png)

A Slack thread reply after the nightly run (format from `lib/slack_replies.py`; the content is invented):

```
Intake done [intake:final]
📋 roundup (3 roles): 2 on board — top: Saltmarsh Energy · Senior Product Manager (score 91); 1 screened
✅ Brambleworks · Product Manager, Developer Platform · Remote (US) · score 77
⚠️ Ironbark Data · Product Manager — couldn't read the posting (login wall / fetch error) — see the board card
```

## Build timeline

Dates are from the private repo's change log and commits.

| Date | Milestone |
|---|---|
| 2026-08-19 | First batch-triage skill: parallel subagents over a list of links |
| 2026-09-09 to 09-28 | The scheduled email scan that keeps my tracker current misses two emails, then writes one role's interview onto another role's record; matching moves from company to role |
| 2026-09-30 | First real batch: 82 links, 86 leads. Triage v2: intake script, roundups, screen, waves, checkpoints, one writer per field group |
| 2026-10-01 | The Jobs board with the 90-second undo. Browser-driven prep stopped by a feasibility spike and a security review; the extractor designed |
| 2026-10-02 | Form extractor merged after a read-only check on three live forms |
| 2026-10-03 | Terminal completion inbox: an interactive application reports back to its card |
| 2026-10-13 | Ranking engine: tiers replaced by a 1-100 score of six factors, one scorer, the settings drawer |
| 2026-10-14 | Calibration on 40 real leads; Slack intake live as a daily local task |
| 2026-10-15 | Acceptance on Greenhouse, Lever and Ashby with zero submissions; extracted to this repo |

## What I learned

- **Resolve before you reason.** The first batch scored LinkedIn posts instead of jobs. Following redirects and
  splitting roundups in plain code fixed more than any prompt change would have.
- **Judgment needs the model; arithmetic doesn't.** Two of six factors use the model. Pay, location, freshness and
  odds are formulas on extracted facts, so they are explainable and they change when I move a slider, not when a
  prompt drifts.
- **A hand ranking is not ground truth.** I ranked a set of roles by hand to calibrate the score, then decided that
  a quick ranking of a small pool should not be the test. The golden fixtures check the formulas; whether the order
  is right is my call on the board.
- **Unattended means the safety lives in code.** Nobody approves each click at 05:30. The extractor's fence, the
  drafter's lockdown and the wrapper owning Slack exist because instructions are not enough. A hook that cannot
  run fails open, so it is tested before every run.
- **Keep the state out of the model's hands.** Lead files have one writer per group; AI runs answer with a marker
  line; the Slack wrapper trusts a result file, never the model's summary.
- **A silent failure costs more than a loud one.** A keyword query that missed two emails, and a sync that wrote to
  the right company but the wrong role, both reported success. Every unattended path here records what it did and
  alerts once when it did not.

## Origin

Extracted on 2026-10-15 from the private multi-agent system I use day to day, where the board is one tab of a
personal dashboard and the skills run alongside others for research, writing and scheduling. The history starts
at extraction. What changed in extraction is listed at the end of
[docs/DESIGN-DECISIONS.md](docs/DESIGN-DECISIONS.md): sample values in place of my own, home metros made
configurable, an optional local store instead of Airtable-only logging, and the private integrations removed.

## Tests

```bash
pip install -r requirements-dev.txt
python scripts/run_tests.py
```

Four pytest suites and the Node tests for the scorer and page logic: 1,030 tests (56 of them need a browser). The fixtures are invented
or scrubbed (see `prep_extract/tests/fixtures/README.md`). CI installs only the core requirements, so the
extractor's browser tests are skipped there with a reason; they run locally with Playwright's Chromium installed.

## License

MIT, see [`LICENSE`](LICENSE).
