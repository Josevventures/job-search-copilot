# Setup

Every module is off until you switch it on in `config.yaml`. Copy the example first:

```bash
cp config.example.yaml config.yaml
python -m copilot modules        # what is on, where your data dir is
```

Requirements for everything: Python 3.11+ and Node 18+ (the scorer is JavaScript; the browser runs it on the
board, and Python reaches it through Node for the Slack replies and tests).

## Core: the board, the ranking engine, link/file intake

```bash
pip install -r requirements.txt
python -m copilot demo           # invented sample leads, opens http://127.0.0.1:8420/jobs
python -m copilot serve          # your own data dir (data/ by default)
python -m copilot add https://job-boards.greenhouse.io/acme/jobs/123 https://lnkd.in/xyz
python -m copilot add --file links.txt
python -m copilot add --xlsx links.xlsx
```

`add` creates lead files; it does not score them. Without the AI triage module they wait in **Needs re-triage**.

The settings that matter on day one are behind the board's **Ranking** button: your pay floor (the one hard gate),
the weights and the two band thresholds. Your home metros are set in the scoring config (`home_metros`, any of `new_york`, `north_jersey`,
`san_francisco`, `boston`, `austin`, `seattle`, `los_angeles`, `washington_dc`, `chicago`, `other_us`); POST it once:

```bash
curl -s localhost:8420/api/jobs/scoring-config | python -c "import json,sys; c=json.load(sys.stdin)['config']; \
print(json.dumps({'expected_version': c['version'], 'active_preset': c['active_preset'], \
'active_weights': c['active_weights'], 'compensation_settings': c['compensation_settings'], 'home_metros': ['chicago']}))" \
| curl -s -X POST -H 'Content-Type: application/json' -d @- localhost:8420/api/jobs/scoring-config
```

The board has no authentication. It binds to 127.0.0.1; to use it from a phone, put both on a private network you
trust (a VPN such as Tailscale or WireGuard) and set `board.host` to that interface. Do not expose it to the
internet.

## Module: AI triage

What it needs: [Claude Code](https://docs.anthropic.com/en/docs/claude-code), signed in (a claude.ai subscription or
an Anthropic API key). Triage runs as Claude Code subagents: there is no direct API client in this repo.

1. `modules.ai_triage.enabled: true`.
2. Install the skills where Claude Code finds them, from the repo root:
   ```bash
   mkdir -p .claude/skills
   ln -s ../../skills/job-apply-fleet .claude/skills/job-apply-fleet     # or copy the folder
   ln -s ../../skills/job-apply .claude/skills/job-apply
   ```
   On Windows without symlink rights, copy the folders. The scripts find the repo root by walking up, so a copy works.
3. Copy `skills/job-apply-fleet/preferences.example.md` to `preferences.md` (gitignored) and rewrite it: your
   direction, your two rubrics, your graduation window. This is the only place triage learns who you are.
4. From the repo root: `claude`, then `/job-apply-fleet <links or a file>`.

Cost: about 70K tokens per lead (Sonnet). The skill stops and asks above 20 leads.

If you sign in with claude.ai and also have `ANTHROPIC_API_KEY` set, headless runs strip the key so the claude.ai
connectors (Airtable) keep working. Set `JSC_KEEP_API_KEY=1` if you mean to run the CLI on the key.

## Module: Slack intake

Needs AI triage too.

1. Create a Slack app (api.slack.com/apps) with a bot token and these scopes:
   - `channels:history` (public channel) or `groups:history` (private channel) — read the drops and the threads
   - `chat:write` — post the thread replies
   Install it to your workspace and invite the bot to the channel.
2. Put the token in the environment or in `.env` at the repo root (gitignored): `SLACK_BOT_TOKEN=xoxb-...`
3. In `config.yaml`: `modules.slack_intake.enabled: true`, `channel_id` (C...), `user_id` (your U..., so only your
   top-level messages count), `timezone`, `quiet_hours`.
4. Dry run, posts nothing: `python skills/job-apply-fleet/jobs_slack_intake.py --no-post`
5. Schedule it once a day, early morning (replies wait for the end of quiet hours inside the script):
   - Windows: `powershell -NoProfile -ExecutionPolicy Bypass -File skills/job-apply-fleet/install_jobs_intake_task.ps1 -Python <venv>\Scripts\pythonw.exe`
   - macOS/Linux, cron: `30 5 * * * cd /path/to/job-search-copilot && /path/to/venv/bin/python skills/job-apply-fleet/jobs_slack_intake.py`

It runs on your machine on purpose: LinkedIn walls cloud IP ranges, and the lead files stay local.
Logs and run state: `data/.state/slack-intake/` (`runs.log`, `run-status.json`).

## Module: form extractor

```bash
pip install -r requirements-extractor.txt
python -m playwright install chromium
```

`modules.form_extractor.enabled: true`. If Playwright lives in a separate environment, set
`modules.form_extractor.python` to that interpreter. Try it on any public posting:

```bash
python -m prep_extract --url https://job-boards.greenhouse.io/<board>/jobs/<id> --out /tmp/form.json
```

It reads Greenhouse, Lever and Ashby forms well and anything else on a best-effort basis. It never types,
uploads, submits or signs in.

## Module: Claude Code skills (job_apply)

Needs Claude Code and the skills installed (see AI triage, step 2).

1. `modules.job_apply.enabled: true`. This turns on the board's **Applied** and **Need help** buttons.
2. Put your own material in the data dir:
   - `data/resumes/` — your resume as a PDF;
   - `data/writing-samples/` — a folder of your own writing (cover letters, answers you were happy with, a few
     emails); the drafts are calibrated on it;
   - `data/profile.md` — facts the drafter may use: name, home city, work authorization, notice period;
   - optional: `modules.job_apply.contacts_dir`, one markdown file per person, for the network check.
3. `/job-apply <url>` in Claude Code for an interactive application, or use the board's buttons.

The prep drafter is locked down per job (see ARCHITECTURE.md, step 7): it can read only the data dir, the skills
and the profile file, and write only `.md` files in `data/prep/`.

## Module: Airtable

Local files stay the default store. Turn this on to log applications to Airtable and dedupe new leads against
it. Claude Code reads and writes Airtable through the claude.ai Airtable connector (or any Airtable MCP server
exposing `list_records_for_table`, `search_records`, `get_table_schema`, `create_records_for_table`,
`update_records_for_table`).

Base schema (field names are what the skill looks for; it reads the field IDs with `get_table_schema`):

**Applications** table

| Field | Type |
|---|---|
| Company | single line text |
| Role Title | single line text |
| Role URL | URL |
| Location | single line text |
| Application Date | date |
| Status | single select: Researching, Applied, Phone Screen, Interview, Offer, Rejected, Closed |
| Interview Chances | single select: High, Medium, Low |
| Interview Chances Rationale | long text |

**Contacts** table (optional, for the network check): Name, Company, Title, Status (current / former), Notes.

Then in `config.yaml`: `modules.airtable.enabled: true`, `base_id`, `applications_table`, optional
`contacts_table`, and under `fields` the three field names (or IDs) the dedupe step reads.

## Running the tests

```bash
pip install -r requirements-dev.txt
python scripts/run_tests.py
```

Four pytest suites (one process each) and the Node tests. The extractor's browser tests are skipped with a reason
unless Playwright's Chromium is installed.
