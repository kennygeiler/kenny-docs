# Roadmap

Written 2026-10-28, after the audit (PRs #19–#21). Status of what exists today:
[`STATUS.md`](STATUS.md). Linear project:
[Kenny core: full rulebook, real-pay check, accuracy score](https://linear.app/kennydocs/project/kenny-core-full-rulebook-real-pay-check-accuracy-score-659951f7c3d6)
(P-KEN-9). The Linear workspace is at the free plan's issue limit, so the work is tracked
as project milestones with task lists rather than issues.

## Where Kenny stands

The core design is right for money questions: the model reads, a deterministic engine
computes, every figure cites its clause, a named person approves every rule, and Kenny
refuses what it cannot ground.

What holds it back:

- **Coverage.** Five live rules. One contract holds dozens of pay and leave provisions,
  and each rule is drafted, tested and approved one scenario at a time, so most real
  questions end in a refusal.
- **The hard part is not the arithmetic.** It is which clause governs (side letters
  override the MOU), which date applies (rates change mid-contract), and how provisions
  combine (does longevity raise the overtime rate?). Each rule is proven at one set of
  inputs.
- **The check is against hand-worked answers.** Known answers are analyst-derived, and
  the hourly rates are transcribed without a citation. The proof an HR director or
  auditor accepts is real pay.
- **The trust machinery is ahead of the usefulness.** The ledger, mutation testing and
  skeptic reviews are enough. More of them will not impress anyone until coverage does.

## The target

Give Kenny a district's contracts. Within a day it builds the full dated pay rulebook, a
person approves it, and Kenny explains every dollar of each employee's published pay
with a citation, flagging what it cannot explain.

Real pay data for this district is public: Transparent California lists each Central
Fire Protection District (Santa Cruz) employee's yearly regular, overtime and other pay,
and the district publishes its Master Salary Schedule.

## Milestones

The core version is **M1 + M4 + M5, about three weeks**. M2 and M3 make it correct
across dates and combinations; M6 and M7 make it a product. Estimates are rough, for one
person working with coding agents.

### M1. Full rulebook for the Local 3535 MOU (core, about 1.5 weeks)

Every pay and leave provision is modelled, approved and cited, or listed as not
modelled with a reason. Approval is per contract section, beside the clauses.

1. Provision inventory: every pay and leave provision (page, section, type) in
   `cases/santacruz/provisions.yaml`.
2. Structured rulebook: rates, overtime triggers, premiums, longevity, holiday pay,
   leave accruals and stacking order in one table per contract, drafted by an agent in a
   Claude Code session (no API spend), each row citing its clause.
3. Section review: approve or reject rulebook rows per section beside the boxed clause;
   the existing gate still runs.
4. Every hourly rate cites its Master Salary Schedule row (old ticket I14).
5. Coverage meter on chat and admin: "N of M provisions modelled", with the not-modelled
   list and reasons.

Done when the coverage meter is live, every rate is cited, and all known answers and
`scripts/demo_smoke.py` stay green.

### M5. Accuracy score, clause answers first (core, about 1 week, parallel with M1)

1. 100 realistic questions (leave, deadlines, eligibility, pay) with graded expected
   answers and the governing clause, in `tests/eval/questions.yaml`; at least 20 should
   be refusals.
2. `scripts/eval.py` scores right / correctly refused / wrong, wired into
   `scripts/demo_smoke.py` so a drop fails the build.
3. An eval page on the site with the three numbers, the build they came from, and a link
   to the question set.
4. The home page and README lead with "find and quote the governing clause"; costing is
   the second example.

Done when the eval page is live and the score reproduces from the repo.

### M4. Check against real published pay (core, about 1 week, needs M1)

1. Check Transparent California's terms, then load the district's 2022–2024 records
   (title, regular, overtime and other pay) into `cases/santacruz/data/`, with source URLs
   and retrieval date in `SOURCES.md`.
2. Map published titles (for example "Fire Captain III") to roster classifications and
   bargaining units; list the unmapped ones.
3. For each record, find the schedule steps whose yearly base fits the regular pay (step
   is not published); record the match, the gap and the cited rates.
4. Overtime plausibility: implied hours from overtime pay and the cited overtime rate;
   flag outliers.
5. Results page: "Base pay reproduced for X of Y records within $Z", each figure cited,
   residuals listed with likely causes (mid-year raise, acting pay, step change). Show
   results by job title and year, not by employee name. State the limit on the page:
   yearly totals cannot verify individual shifts; a real customer would supply payroll
   records.

Done when the results page is live and reproduces from the repo.

### M2. Dates and precedence (about 1 week)

Start and end dates on every rule and rate (old ticket B7); side letters explicitly
override the MOU; the date in a question selects the version. Done when the same
question for 2024 and 2026 gives different, correctly cited answers.

### M3. Combinations (about 1 week)

Declared stacking order (for example longevity into the overtime base); federal overtime
law (FLSA 7(k)) checked as a separate floor; thousands of generated cases (every class ×
step × hours, with threshold edges) checked against an independent calculation. Done when
they agree or every disagreement is explained.

### M6. A second agency in a day (about 1 week)

Onboard another set of contracts (a neighbouring fire district or a city police MOU)
with the M1 pipeline. Publish the hours taken, the coverage and the share of pay
reproduced.

### M7. Front door (2–3 days)

Open the site with the real-pay result and a 60-second walkthrough; fold the caveats into
one Limits page.

## Not on the roadmap

More trust machinery (ledger, mutation testing and skeptic reviews are enough), user
accounts or multi-tenancy, and a live model on the public site.

## Risks

- Scanned contracts: the MOUs are OCR'd scans, which makes whole-contract extraction
  harder (Local 3535 p.22 already has six disputed cells).
- Public pay data omits step and hire date, so M4 reconciles to a range of plausible
  steps, not exact figures.
- Transparent California's terms may limit bulk use; check before loading.
