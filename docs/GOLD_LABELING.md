# Gold labeling guide

The gold set is how we know our accuracy. Every published accuracy number comes from comparing pipeline output with these hand labels (design section 12). The gold set, the quote gate and the eval report are never cut from scope.

## Dataset plan (Texas)

- **60 FAP bundles** (FAP, application, plain-language summary, billing and collections policy), stratified by single vs multi-facility system, born-digital vs scanned, HTML vs PDF, Spanish availability, and size (critical access through academic). Split **40 dev / 20 frozen test**.
- **15 double-coded policies** labelled independently by a second coder (section "Double coding").
- **15 policies from the second state** to measure generalization.
- **40 facilities** with a manually verified governing policy, for the facility-mapping audit.
- Budget: about 25 minutes per policy (about 25 hours), plus about 6 hours for the second coder.

The demo corpus (`data/demo/gold/`) uses the same format with eight fictional hospitals.

## File format

One JSON file per hospital, `gold/<hospital_id>.json`:

```json
{
  "hospital_id": "example-methodist",
  "name": "Example Methodist Hospital",
  "facility_ids": ["tx-459901"],
  "policy": { "<field>": { "value": ..., "evidence": [{"doc_sha256": "...", "page": 3, "quote": "..."}],
                            "confidence": 1.0, "abstain_reason": null } },
  "schedule_h": { "object_id": "...", "tax_period_end": "2025-12-31",
                  "expected_crosschecks": { "fpg_tiers": "agree", "asset_test": "disagree_unexplained" } },
  "notes": ["why anything unusual is labelled the way it is"]
}
```

`policy` is a complete `CompiledPolicy` (all 23 fields) and must validate against `schemas/open-fap-schema.json`. Register the hospitals and splits in `evals/datasets/<name>.yaml`.

## Labeling rules

1. **Hash first.** Record the SHA-256 of the exact document you label. Quotes refer to that version only.
2. **Value, quote, page for every field.** Copy the quote verbatim (up to 400 characters) from the cited page. If a value needs two clauses (for example a threshold and its exclusions), give two evidence items.
3. **Label what the document says, not what you expect.** If a table and the prose disagree, label `conflicting_clauses` and note both.
4. **Abstain on purpose.** Use `not_addressed` when the bundle is silent, `ambiguous_may_clause` for discretionary language ("the Hospital may consider..."), `conflicting_clauses` for contradictions, and `illegible` when a page cannot be read even by a person. An abstention has no value.
5. **Thresholds.** `fpg_tiers` lists every band with its upper bound inclusive (`max_pct_inclusive`) and its lower bound exclusive. Use `applies_to` for tiers that only apply to insured or uninsured patients. A policy with no asset test is `asset_test.applies = false`, not an abstention.
6. **Lists.** Label each required document with a `doc_type` from the schema enum (use `other` plus a description when nothing fits). Counties are names without the word "County".
7. **FPG year.** `stated_year` only when the policy names a guideline year; `unspecified` when it is silent.
8. **Schedule H expectations.** For each compared field, record what the comparison *should* resolve to, using the filing period end and the policy's revision date (see METHODS.md section 5).
9. **Notes** explain any judgment call so the second coder and the adjudicator can follow it.

Gold labels for fields on image-only pages keep their true value (read by a person) even when the pipeline must abstain; the eval reports that gap separately.

## Double coding and agreement

A second coder labels 15 policies without seeing the first coder's labels. Then run:

```
python -m evals agreement --a gold/coder_a --b gold/coder_b --out agreement.json
```

This reports per-field percentage agreement and Cohen's kappa (labels are compared with the same normalization the scorers use, so whitespace or capitalization differences in quotes and free text do not count as disagreement), plus a pooled kappa and the list of disagreements. Publish the per-field agreement and kappa in the eval report. Adjudicate each disagreement with both coders; the adjudicated label becomes gold and the reason goes in `notes`. A field with kappa below 0.6 gets a clarified rule in this guide before more labeling.

## Freezing the test split

The 20 test policies are labelled, double-checked and frozen before any prompt tuning. Nobody looks at per-hospital test results while developing; the `test` suite runs only for releases (`release_gated: true`). If a test document changes upstream, the old hash stays in the test set; the new version can join the dev split.

## Citation support sample

Separately from gold, a person judges whether quotes support their values on a 100-field random sample:

```
python -m evals support-sample --suite full --n 100 --out support.csv   # fill the "supports" column: yes / no / unsure
python -m evals support-score support.csv                               # target >= 95%
```
