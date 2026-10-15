# Job triage preferences (example)

Read by every `/job-apply-fleet` triage subagent. Copy this file to `preferences.md` (gitignored), point
`modules.ai_triage.preferences_file` at it, and rewrite every line for yourself. Change preferences here, never
in the subagent prompt.

Everything below describes an invented candidate so the rubrics have something concrete to show. It is a
template, not a recommendation.

Rank is not decided here. The Jobs board scores every lead on six weighted factors; the weights and the pay
floor live in the board's Ranking settings drawer. Triage supplies the facts and two judgment sub-scores.

- **Direction (what the Domain score measures):** climate and energy software, developer tools, B2B data
  products; product management and product operations. Generic project management is a fallback, not the
  direction.
- **Role fit rubric (`scores.role_fit`, 0-100):** 85-100 meets every basic requirement with on-record evidence;
  65-84 meets all but one gap that can be prepared for; 40-64 one real gap (e.g. "5+ years as a people
  manager" with no manager title); 20-39 a literal screen likely fails (degree field, licence, years far off);
  0-19 outside the candidate's background or level. Location never lowers role fit.
- **Domain rubric (`scores.domain`, 0-100):** 85-100 product roles at climate or energy software companies;
  65-84 product or platform roles at developer-tool or data companies; 40-64 product operations or program
  roles outside tech; 20-39 adjacent safety-net work (business analysis, PMO); 0-19 unrelated functions
  (quota sales, engineering IC, creative production).
- **Missing records are not missing experience.** Score from what is on record and name the unknown in
  `main_gap`; never score a skill as absent only because no writing sample or note mentions it.
- **Grad programs:** the candidate finished a master's degree in 2024. Programs for recent master's graduates
  are in scope; check the stated graduation window. Outside it: `scores.role_fit` <= 20 and explain in
  `eligibility_note`. Undergraduate programs and internships are screened out.
- **Location:** report locations and modality as facts; the scorer applies the location weight and the home
  metros listed in the scoring config (`home_metros`).
- **Pay:** report only figures printed on the posting or returned by the ATS API. Never estimate pay; the
  scorer estimates from level when pay is missing.

```json
{
  "screen_labels": ["non_job", "event", "internship_or_undergrad"],
  "grad_eligibility": {"degree": "master's", "graduated_year": 2024}
}
```
