# Methods

This page explains how Charity Care Commons turns a hospital's published financial assistance policy (FAP) into cited, checkable data, how each field earns its verification status, how we compare policies with the hospital's IRS filing, and how we measure our own accuracy. It is the public methods note linked from every hospital page.

Methods version: `methods@2026-10-24`. Changes to this page are listed in the changelog at the end.

## 1. What we publish

For every in-scope Texas hospital facility we publish the rules of the financial assistance policy that governs it: income tiers as a percentage of the federal poverty guidelines (FPG), the asset test, residency, rules for insured patients, required documents, the application window, submission channels, presumptive screening, refunds, notices and more (23 fields, defined in the [Open FAP Schema](../schemas/open-fap-schema.json)).

Each field is published as a *verification-carrying tuple*:

```
(value, evidence{doc_sha256, page, verbatim quote, character span}, status, cross-checks[], as_of)
```

- **value**: what the policy says, in a typed form (for example `free care up to 200% FPG`).
- **evidence**: the verbatim quote that supports the value, the page it is on, and the SHA-256 hash of the exact document version we read. The hash is the document's identity: if the hospital changes the PDF, the hash changes and we re-read it.
- **status**: see section 3.
- **cross-checks**: comparisons with the hospital's Schedule H filing (section 5).
- **as_of**: the date we last confirmed the document.

The language model *extracts*. Code computes percentages of the poverty guidelines and tiers. People verify fields. No model output ever becomes a screening result directly.

## 2. Pipeline

1. **Seed.** The facility universe comes from the CMS Hospital General Information file. Each nonprofit hospital's IRS Form 990 Schedule H Part V is parsed with pinned element paths, including the policy URLs on lines 16a-c and any URLs written into the Part V Section C narrative.
2. **Discover.** A read-only, domain-allowlisted agent finds the FAP, application, plain-language summary and billing policy on the hospital's site. It respects robots.txt, waits 5 seconds between requests to a host, identifies itself, and never circumvents bot protection. Dead links fall back to an Internet Archive copy (published as provisional and dated), then to an email to the hospital, then to a Texas Public Information Act request.
3. **Compile.** Born-digital pages go to the model as text; scanned or table-heavy pages go as images. The model returns every field with a quote and page, or an explicit abstention.
4. **Quote gate ("no quote, no publish").** An independent text layer is re-read from the hashed PDF. Each quote must appear on its cited page after normalization (Unicode, quotes and dashes, line-end hyphenation, whitespace, case). A guarded fuzzy match (similarity of at least 0.98) is allowed for OCR noise, but never when a differing word is a number or a negation such as "not" or "only". Every number in a value must also be printed in one of its quotes. A field that fails gets one targeted re-ask; if it still fails, it is stored as abstained and sent to a reviewer.
5. **Round trip.** When a policy prints its own income table (for example "household of 3: $54,640"), the compiled tiers plus the HHS guidelines for the policy's stated year must reproduce the table. A mismatch flags a misread tier or the wrong guideline year.
6. **Cross-check** with Schedule H (section 5).
7. **Facility mapping.** Each facility is mapped to the policy version that governs it (system policy plus facility addendum, Schedule H group reporting, renamed campuses). The method and confidence are stored and shown.
8. **Review.** A maintainer reviews fields, prioritized by impact. An Opus-class model may draft an adjudication note for disagreements, but its draft is advisory and never changes a value.

## 3. Verification statuses

| Status | Meaning | Can it drive a screening estimate? |
|---|---|---|
| `provisional` | Extracted automatically and passed the quote gate; not yet reviewed by a person. | Only as a *preliminary* result that names the provisional fields used. |
| `verified` | A reviewer read the quote on the cited page and approved the value. | Yes. |
| `disputed` | The hospital or a reviewer contests the value. We show the value, the quote and the statement. | Only as preliminary, with the dispute shown. |
| `abstained` | We publish no value. Reasons: `not_addressed` (the policy is silent), `ambiguous_may_clause` (discretionary "may" language, shown as "hospital discretion, see quote"), `conflicting_clauses`, `illegible` (no readable text layer and no OCR). | No; the estimate lists it as an unevaluated condition. |

Abstention is a correct, first-class answer. A reviewer's verdict on an unchanged value carries over when a document is re-fetched; any change to the value resets it to provisional.

## 4. Screening estimates

`POST /v1/screen` and the navigator console compute a household's percentage of the poverty guidelines with exact arithmetic and match it to the policy's tiers. The FPG year follows the policy's rule; when the policy does not say, we use the guidelines current at the application date, state that assumption, and also show the result under the prior year if the tier changes. The output is labelled "Likely eligible for free care", "Likely eligible for discounted care", "Likely not eligible on income alone", "Preliminary: based on fields not yet verified by a reviewer", or "Not enough information to estimate", and it always carries the versioned [disclaimer](DISCLAIMER.md). The hospital makes the final decision.

## 5. Schedule H cross-checks and filing lag

Hospitals answer Schedule H Part V under penalty of perjury. We compare compiled values with lines 13a (free and discount %FPG), 13c (asset level), 13e (insurance status), 13g (residency), 15a-b (application information and documents), 16a-c (URLs), 16i (translations), 18 (extraordinary collection actions) and 22 (method for amounts generally billed).

Filings lag policies. About 90% of returns filed in 2026 cover tax periods that ended in 2025, so a filing can be 10 to 20 months older than the policy we read. Every comparison therefore records the filing period end next to the policy's effective or revision date, and resolves to one of:

| Result | Meaning | Public treatment |
|---|---|---|
| `agree` | The filing and the policy match. | Shown as context. |
| `disagree_policy_newer_than_filing` | They differ, and the policy was revised after the filing period ended. | Shown as context ("policy revised after the filing period"), never as a discrepancy. |
| `disagree_unexplained` | They differ and the dates do not explain it. | Becomes a **discrepancy for review**, only after the hospital's right of reply (section 6). |
| `not_reported` | The filing does not answer the line. | Shown as context. |

Group reporting (one Section B block for several facilities) is resolved through the facility reporting group letters before comparison.

## 6. Wording and the right of reply

We describe differences, not conduct.

- We say "discrepancy for review". We do not use the words "violation" or "non-compliant", or say that a hospital is "compliant", in anything we generate. We never rank hospitals.
- We say "likely eligible", never that a household is "eligible": only the hospital decides.
- Our generated text is checked automatically for this wording (`charitycare.wording`); verbatim quotes from hospital documents are exempt.

Before any `disagree_unexplained` item is published, we email the hospital's financial assistance or patient financial services contact with the field, our quote and page, the Schedule H line and filing period, and a link to correct us. The hospital has **10 business days** to respond. If it responds, its statement is published next to the item verbatim (`HospitalResponse`), and accepted corrections are applied and noted in the change feed. If a hospital sends a credible legal concern, we take the item down pending review. Hospitals are never charged to be listed or to correct a listing.

## 7. Evaluation

We publish our own accuracy, measured against hand-labelled gold policies (see [GOLD_LABELING.md](GOLD_LABELING.md)). The harness lives in `evals/` (`python -m evals run --suite <name>`); results are recorded as `eval_run` rows and in the public eval report (`evals/reports/latest.json`), and every number links to its run id, git SHA and dataset version.

| Metric | Definition | Target |
|---|---|---|
| Numeric thresholds | Free cutoff %FPG, top discount cutoff %FPG and asset threshold $, exact match where we published a value | >= 97% |
| Threshold abstention rate | Share of gold-answered thresholds on which we abstained | <= 10% |
| Categorical macro-F1 | Over (field, class) labels such as income definition, FPG year rule, residency area type; abstention is its own class | >= 0.90 |
| List item F1 | Item-level F1 of required documents, submission channels, presumptive criteria, counties, excluded services, facilities | >= 0.85 |
| **Citation validity** | Share of published fields whose every quote is found on its cited page of the hashed document, re-verified independently by the harness | **100% (hard gate)** |
| Citation support | Share of a 100-field random sample whose quote actually supports the value, judged by a person | >= 95% |
| Schedule H agreement | Agreement on free %FPG where the policy date is on or before the filing period end; all cross-checks are also counted by result | Report; expect >= 85%; 100% of unexplained disagreements triaged |
| Calibration (ECE) | Expected calibration error of the model's self-reported confidence, 10 equal-width bins; used to set abstain thresholds | <= 0.05 |
| Engine agreement | Screening engine vs an independent oracle on 300 synthetic households at rule edges | 100% |
| Readback | Filled form fields that read back exactly (fillable; flat after one retry); no packet is labelled verified with any mismatch | 100% / >= 98% / 0 |
| Naive-agent baseline | The same model tier with web search and no pipeline, scored on the free-care cutoff | Report the gap; expect >= 20 points |
| Cost | Inference per hospital compile and per packet, at list prices | <= $0.50 / <= $0.10 |

**Confidence intervals.** Every rate is reported with a 95% percentile bootstrap interval (1,000 resamples, fixed seed). We resample whole hospitals (or households, or forms), not individual fields, because fields from one policy are correlated.

**Splits.** Dev policies are used for development; a frozen test split is scored only for releases and its results are attached to the dataset release notes. The `smoke` suite (a small dev subset) runs on every pull request that touches prompts, schemas or the compiler.

**Regression gates.** CI fails when any gated metric worsens by more than 2 percentage points against the committed baseline, when citation validity is below 100%, or when cost per hospital rises by more than 25%.

**Offline replay.** Every model response is recorded as a fixture keyed by (prompt version, document hash, model tier), so the full evaluation can be replayed deterministically at zero cost. Replayed costs are what the recorded token usage would cost at list prices and are labelled "simulated".

**The demo corpus is synthetic.** The public demo uses eight fictional hospitals with synthetic gold labels. Its naive-agent baseline answers are simulated from documented failure modes (stale Schedule H values, a discount tier read as the free tier, no answer); they illustrate the method and are labelled as simulated in every report. Real baseline numbers come only from live runs on real policies.

## 8. Known limitations

- Fields on image-only pages abstain as `illegible` unless OCR is available; evaluations report this gap separately.
- Schedule H is a lagging, self-reported source. A difference is a question for the hospital, not a finding.
- A policy can be followed differently in practice than it is written. We describe the written policy.
- Accuracy numbers on the synthetic demo corpus do not predict accuracy on real Texas policies; the real gold set is reported separately.

## Changelog

- `methods@2026-10-24`: first version.
