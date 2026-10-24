# Sunset policy

Earlier public charity-care datasets went dark when their grants ended. This policy says in advance what happens if maintenance stops, so nobody relies on stale data without knowing it.

Policy version: `sunset@2026-10-24`.

## Maintenance commitments while active

- Weekly hash-diff refresh of every known policy URL; changed documents are recompiled and reviewed.
- Weekly `full` evaluation and a public eval report.
- Pre-publication emails and a 10-business-day right of reply for every discrepancy.
- The January poverty-guideline update is applied to the engine within 30 days of HHS publication.

## Staleness signals

| Condition | What users see |
|---|---|
| A policy's `as_of` is older than 18 months | The hospital page shows "policy last confirmed [date]; may be out of date" and the facility no longer counts toward coverage. |
| No successful weekly refresh for 30 days | A site-wide banner with the last-verified date; the API adds `stale: true` and `last_verified` to every response. |
| No refresh for 90 days | Screening estimates are labelled preliminary and say the data may be out of date. |
| Poverty guidelines for the current year are not loaded by 31 March | Estimates show the guideline year used and a warning. |

## If maintenance ends

1. Announce the end date at least 30 days ahead on the site, in the change feed and in the repository README.
2. Freeze a final data release with a DOI (Zenodo) and a final eval report; mark the Hugging Face dataset as archived.
3. Switch the site to a static archive with the last-verified date shown on every page; the API returns `stale: true` and stops accepting screening requests after 90 days (or serves them labelled preliminary).
4. Keep the pipeline reproducible from public sources with one command, and leave the repository public under Apache-2.0 (code) and CC BY 4.0 (data).
5. Offer the dataset, schema and pipeline to organizations that could adopt them (for example Lown Institute, Dollar For or a university group), with a handover call and documentation.

## Funding triggers (design section 15)

- No funder or payer interest within 3 months of launch: maintain Texas monthly (not weekly), with a stated end date.
- Kill signals from the validation plan: archive as a portfolio piece following the steps above.
