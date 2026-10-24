# Worked examples (hard cases)

Each example shows a short excerpt and the correct field output. Quotes are copied verbatim.
The sha256 `"<SHA>"` stands for the document hash given in the user message.

## 1. Two-band rule with an AGB cap

Page 3: "Patients with household income at or below 200% of the Federal Poverty Guidelines
qualify for free care, a 100% discount. Patients with household income above 200% and at or
below 400% of the Federal Poverty Guidelines will not be charged more than amounts generally billed."

```json
{"fpg_tiers": {"value": [
   {"min_pct_exclusive": null, "max_pct_inclusive": 200, "benefit": "free", "discount_pct": null, "applies_to": "all"},
   {"min_pct_exclusive": 200, "max_pct_inclusive": 400, "benefit": "agb_cap", "discount_pct": null, "applies_to": "all"}],
  "evidence": [
   {"doc_sha256": "<SHA>", "page": 3, "section_label": "4.2", "quote": "Patients with household income at or below 200% of the Federal Poverty Guidelines qualify for free care, a 100% discount."},
   {"doc_sha256": "<SHA>", "page": 3, "section_label": "4.2", "quote": "Patients with household income above 200% and at or below 400% of the Federal Poverty Guidelines will not be charged more than amounts generally billed."}],
  "confidence": 0.96, "abstain_reason": null}}
```

## 2. Discretionary "may" clause: abstain, keep the quote

Page 6: "The Hospital may, at its sole discretion, consider presumptive eligibility for patients
experiencing homelessness."

```json
{"presumptive_eligibility": {"value": null, "abstain_reason": "ambiguous_may_clause", "confidence": 0.6,
  "evidence": [{"doc_sha256": "<SHA>", "page": 6, "section_label": "7.2", "quote": "The Hospital may, at its sole discretion, consider presumptive eligibility for patients experiencing homelessness."}]}}
```

## 3. Silence: abstain as not addressed

The policy has no refund clause anywhere.

```json
{"refund_policy": {"value": null, "evidence": [], "confidence": 0.7, "abstain_reason": "not_addressed"}}
```

## 4. A printed income table is not the rule, and its year matters

Page 2: "Eligibility is determined using the 2026 Federal Poverty Guidelines shown in the table in
Section 4." Page 3 prints a table of dollar limits.

```json
{"fpg_year_rule": {"value": "stated_year", "confidence": 0.9, "abstain_reason": null,
  "evidence": [{"doc_sha256": "<SHA>", "page": 2, "section_label": "3.3", "quote": "Eligibility is determined using the 2026 Federal Poverty Guidelines shown in the table in Section 4."}]}}
```

Do not derive tiers from the table's dollar amounts; code re-derives the table from your tiers and
flags any mismatch.

## 5. Catastrophic (medical indigency) clause for insured patients

Page 4: "Insured patients are eligible for assistance with deductibles, co-insurance and other
balances after insurance. Patients whose out-of-pocket medical bills for the prior 12 months exceed
20% of household income also qualify for a discount, regardless of insurance status."

```json
{"insured_patients": {"value": {"insured_eligible": true, "catastrophic_threshold_pct_income": 20, "notes": null},
  "confidence": 0.92, "abstain_reason": null,
  "evidence": [
   {"doc_sha256": "<SHA>", "page": 4, "section_label": "5.3", "quote": "Insured patients are eligible for assistance with deductibles, co-insurance and other balances after insurance."},
   {"doc_sha256": "<SHA>", "page": 4, "section_label": "5.3", "quote": "Patients whose out-of-pocket medical bills for the prior 12 months exceed 20% of household income also qualify for a discount, regardless of insurance status."}]}}
```

## 6. System policy with an addendum: list every named facility, cite the addendum page

Page 7: "The following hospital facilities of the System are covered by this Policy: Example
Medical Center North; Example Medical Center South."

```json
{"facilities_covered": {"value": ["Example Medical Center North", "Example Medical Center South"],
  "confidence": 0.95, "abstain_reason": null,
  "evidence": [{"doc_sha256": "<SHA>", "page": 7, "section_label": "Addendum A", "quote": "The following hospital facilities of the System are covered by this Policy: Example Medical Center North; Example Medical Center South."}]}}
```

A list item that wraps onto the next page is cited on the page where the quoted words are printed.
