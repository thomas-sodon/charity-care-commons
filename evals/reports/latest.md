# Charity Care Commons evaluation report

Accuracy of the compiled policy data, measured against hand-labelled gold policies (design section 12). 95% confidence intervals come from a bootstrap that resamples whole hospitals, households or forms.

## Suite `engine` (engine)

Run `engine-20261024T183854Z-618d9fac` · git `uncommitted` · dataset `demo-2026.10+70a269d61e21` · mode `replay` · 300 units · 2026-10-24T18:38:54+00:00

> Dataset is the synthetic demo corpus (fictional hospitals, synthetic gold).

| Metric | Value | 95% CI | n | Target | Status |
|---|---|---|---|---|---|
| engine_agreement | 100.0% | 100.0% to 100.0% | 300 | >= 100.0% (hard gate) | meets target |

## Suite `full` (compile)

Run `full-20261024T183848Z-77ea8e93` · git `uncommitted` · dataset `demo-2026.10+70a269d61e21` · mode `replay` · 5 units · 2026-10-24T18:38:48+00:00

> Dataset is the synthetic demo corpus (fictional hospitals, synthetic gold).
> Naive-agent baseline answers are SIMULATED synthetic fixtures (documented failure modes), not measured model outputs; re-record with CCC_LLM=record.
> Costs are simulated: recorded token usage priced at section 13 list prices.

| Metric | Value | 95% CI | n | Target | Status |
|---|---|---|---|---|---|
| numeric_threshold_exact | 100.0% | 100.0% to 100.0% | 13 | >= 97.0% | meets target |
| threshold_abstention_rate | 13.3% | 0.0% to 40.0% | 15 | <= 10.0% | misses target |
| correct_abstention_rate | 100.0% | 100.0% to 100.0% | 5 |  | report |
| categorical_macro_f1 | 100.0% | 100.0% to 100.0% | 75 | >= 90.0% | meets target |
| list_item_f1 | 97.4% | 92.8% to 100.0% | 60 | >= 85.0% | meets target |
| citation_validity | 100.0% | 100.0% to 100.0% | 107 | >= 100.0% (hard gate) | meets target |
| field_precision | 100.0% | 100.0% to 100.0% | 107 |  | report |
| field_recall | 97.3% | 95.6% to 99.1% | 110 |  | report |
| crosscheck_expected_match | 95.0% | 85.0% to 100.0% | 20 |  | report |
| schedule_h_free_pct_agreement | 100.0% | 100.0% to 100.0% | 2 | >= 85.0% | meets target |
| schedule_h_agree_count | 41 |  | 41 |  | report |
| schedule_h_disagree_policy_newer_than_filing_count | 0 |  | 0 |  | report |
| schedule_h_disagree_unexplained_count | 1 |  | 1 |  | report |
| schedule_h_not_reported_count | 0 |  | 0 |  | report |
| schedule_h_missing_count | 1 |  | 1 |  | report |
| disagreement_triage_rate | 100.0% | 100.0% to 100.0% | 1 | >= 100.0% | meets target |
| calibration_ece | 0.065 | 0.064 to 0.066 | 107 | <= 0.050 | misses target |
| cost_per_hospital_usd | $0.1283 | $0.0982 to $0.1837 | 5 | <= $0.5000 | meets target |
| compile_error_rate | 0.0% | 0.0% to 0.0% | 5 |  | report |
| commons_free_cutoff_accuracy | 80.0% | 40.0% to 100.0% | 5 |  | report |
| naive_free_cutoff_accuracy | 40.0% | 0.0% to 80.0% | 5 |  | report |
| naive_baseline_gap_points | +40.0 pts | +0.0 pts to +80.0 pts | 5 |  | report |

Schedule H cross-checks by result (only `disagree_unexplained` can become a public discrepancy for review, after the hospital's right of reply):

- `agree`: 41
- `disagree_policy_newer_than_filing`: 0
- `disagree_unexplained`: 1
- `missing`: 1
- `not_reported`: 0

| Field | Gold answered | Published | Correct | Precision | Recall | Gold abstained | Abstentions matched |
|---|---|---|---|---|---|---|---|
| agb_method | 5 | 5 | 5 | 100% | 100% | 0 | 0 |
| agb_pct | 5 | 5 | 5 | 100% | 100% | 0 | 0 |
| application_window | 5 | 5 | 5 | 100% | 100% | 0 | 0 |
| asset_test | 5 | 5 | 5 | 100% | 100% | 0 | 0 |
| award_period_months | 5 | 5 | 5 | 100% | 100% | 0 | 0 |
| determination_notice | 4 | 4 | 4 | 100% | 100% | 1 | 1 |
| effective_or_revision_date | 5 | 5 | 5 | 100% | 100% | 0 | 0 |
| excluded_services | 5 | 5 | 5 | 100% | 100% | 0 | 0 |
| facilities_covered | 5 | 5 | 5 | 100% | 100% | 0 | 0 |
| fpg_tiers | 5 | 4 | 4 | 100% | 80% | 0 | 0 |
| fpg_year_rule | 5 | 5 | 5 | 100% | 100% | 0 | 0 |
| household_definition | 5 | 5 | 5 | 100% | 100% | 0 | 0 |
| income_definition | 5 | 5 | 5 | 100% | 100% | 0 | 0 |
| insured_patients | 5 | 5 | 5 | 100% | 100% | 0 | 0 |
| medicaid_application_required_first | 5 | 5 | 5 | 100% | 100% | 0 | 0 |
| policy_title | 5 | 5 | 5 | 100% | 100% | 0 | 0 |
| presumptive_eligibility | 4 | 4 | 4 | 100% | 100% | 1 | 1 |
| provider_list_location | 5 | 5 | 5 | 100% | 100% | 0 | 0 |
| refund_policy | 4 | 3 | 3 | 100% | 75% | 1 | 1 |
| required_documents | 5 | 4 | 4 | 100% | 80% | 0 | 0 |
| residency | 5 | 5 | 5 | 100% | 100% | 0 | 0 |
| screening_timing | 3 | 3 | 3 | 100% | 100% | 2 | 2 |
| submission_channels | 5 | 5 | 5 | 100% | 100% | 0 | 0 |

Naive-agent baseline vs the Commons on the free-care cutoff (%FPG):

| Hospital | Gold | Commons | Naive agent |
|---|---|---|---|
| example-methodist | 200% | 200% | 200% |
| lone-star-health | 250% | 250% | 400% |
| gulf-coast-baptist | 200% | 200% | 200% |
| pineywoods-regional | 150% | abstained | 125% |
| hill-country-community | 200% | 200% | no answer |

## Suite `packet` (packet)

Run `packet-20261024T183857Z-95d8ae1f` · git `uncommitted` · dataset `demo-2026.10+70a269d61e21` · mode `replay` · 12 units · 2026-10-24T18:38:57+00:00

> Dataset is the synthetic demo corpus (fictional hospitals, synthetic gold).
> Costs are simulated: recorded token usage priced at section 13 list prices.

| Metric | Value | 95% CI | n | Target | Status |
|---|---|---|---|---|---|
| readback_fillable_exact | 100.0% | 100.0% to 100.0% | 65 | >= 100.0% | meets target |
| readback_flat_exact | 100.0% | 100.0% to 100.0% | 52 | >= 98.0% | meets target |
| verified_packets_with_mismatch | 0 |  | 9 | <= 0 (hard gate) | meets target |
| packet_verified_rate | 100.0% | 100.0% to 100.0% | 9 |  | report |
| template_f1 | 100.0% | 100.0% to 100.0% | 9 |  | report |
| extraction_field_exact | 100.0% | 100.0% to 100.0% | 21 | >= 95.0% | meets target |
| cost_per_packet_usd | $0.0324 | $0.0108 to $0.0648 | 9 | <= $0.1000 | meets target |

## Suite `smoke` (compile)

Run `smoke-20261024T183841Z-19929b16` · git `uncommitted` · dataset `demo-2026.10+70a269d61e21` · mode `replay` · 4 units · 2026-10-24T18:38:41+00:00

> Dataset is the synthetic demo corpus (fictional hospitals, synthetic gold).
> Costs are simulated: recorded token usage priced at section 13 list prices.

| Metric | Value | 95% CI | n | Target | Status |
|---|---|---|---|---|---|
| numeric_threshold_exact | 100.0% | 100.0% to 100.0% | 10 | >= 97.0% | meets target |
| threshold_abstention_rate | 16.7% | 0.0% to 50.0% | 12 | <= 10.0% | misses target |
| correct_abstention_rate | 100.0% | 100.0% to 100.0% | 2 |  | report |
| categorical_macro_f1 | 100.0% | 100.0% to 100.0% | 60 | >= 90.0% | meets target |
| list_item_f1 | 96.9% | 91.4% to 100.0% | 50 | >= 85.0% | meets target |
| citation_validity | 100.0% | 100.0% to 100.0% | 87 | >= 100.0% (hard gate) | meets target |
| field_precision | 100.0% | 100.0% to 100.0% | 87 |  | report |
| field_recall | 96.7% | 95.5% to 98.9% | 90 |  | report |
| crosscheck_expected_match | 93.8% | 81.2% to 100.0% | 16 |  | report |
| schedule_h_free_pct_agreement | 100.0% | 100.0% to 100.0% | 1 | >= 85.0% | meets target |
| schedule_h_agree_count | 32 |  | 32 |  | report |
| schedule_h_disagree_policy_newer_than_filing_count | 0 |  | 0 |  | report |
| schedule_h_disagree_unexplained_count | 1 |  | 1 |  | report |
| schedule_h_not_reported_count | 0 |  | 0 |  | report |
| schedule_h_missing_count | 1 |  | 1 |  | report |
| disagreement_triage_rate | 100.0% | 100.0% to 100.0% | 1 | >= 100.0% | meets target |
| calibration_ece | 0.065 | 0.065 to 0.066 | 87 | <= 0.050 | misses target |
| cost_per_hospital_usd | $0.1360 | $0.0993 to $0.2037 | 4 | <= $0.5000 | meets target |
| compile_error_rate | 0.0% | 0.0% to 0.0% | 4 |  | report |
| commons_free_cutoff_accuracy | 75.0% | 25.0% to 100.0% | 4 |  | report |

Schedule H cross-checks by result (only `disagree_unexplained` can become a public discrepancy for review, after the hospital's right of reply):

- `agree`: 32
- `disagree_policy_newer_than_filing`: 0
- `disagree_unexplained`: 1
- `missing`: 1
- `not_reported`: 0

| Field | Gold answered | Published | Correct | Precision | Recall | Gold abstained | Abstentions matched |
|---|---|---|---|---|---|---|---|
| agb_method | 4 | 4 | 4 | 100% | 100% | 0 | 0 |
| agb_pct | 4 | 4 | 4 | 100% | 100% | 0 | 0 |
| application_window | 4 | 4 | 4 | 100% | 100% | 0 | 0 |
| asset_test | 4 | 4 | 4 | 100% | 100% | 0 | 0 |
| award_period_months | 4 | 4 | 4 | 100% | 100% | 0 | 0 |
| determination_notice | 4 | 4 | 4 | 100% | 100% | 0 | 0 |
| effective_or_revision_date | 4 | 4 | 4 | 100% | 100% | 0 | 0 |
| excluded_services | 4 | 4 | 4 | 100% | 100% | 0 | 0 |
| facilities_covered | 4 | 4 | 4 | 100% | 100% | 0 | 0 |
| fpg_tiers | 4 | 3 | 3 | 100% | 75% | 0 | 0 |
| fpg_year_rule | 4 | 4 | 4 | 100% | 100% | 0 | 0 |
| household_definition | 4 | 4 | 4 | 100% | 100% | 0 | 0 |
| income_definition | 4 | 4 | 4 | 100% | 100% | 0 | 0 |
| insured_patients | 4 | 4 | 4 | 100% | 100% | 0 | 0 |
| medicaid_application_required_first | 4 | 4 | 4 | 100% | 100% | 0 | 0 |
| policy_title | 4 | 4 | 4 | 100% | 100% | 0 | 0 |
| presumptive_eligibility | 3 | 3 | 3 | 100% | 100% | 1 | 1 |
| provider_list_location | 4 | 4 | 4 | 100% | 100% | 0 | 0 |
| refund_policy | 4 | 3 | 3 | 100% | 75% | 0 | 0 |
| required_documents | 4 | 3 | 3 | 100% | 75% | 0 | 0 |
| residency | 4 | 4 | 4 | 100% | 100% | 0 | 0 |
| screening_timing | 3 | 3 | 3 | 100% | 100% | 1 | 1 |
| submission_channels | 4 | 4 | 4 | 100% | 100% | 0 | 0 |

## Suite `test` (compile)

Run `test-20261024T183853Z-d993a038` · git `uncommitted` · dataset `demo-2026.10+70a269d61e21` · mode `replay` · 3 units · 2026-10-24T18:38:53+00:00

> Dataset is the synthetic demo corpus (fictional hospitals, synthetic gold).
> Naive-agent baseline answers are SIMULATED synthetic fixtures (documented failure modes), not measured model outputs; re-record with CCC_LLM=record.
> Costs are simulated: recorded token usage priced at section 13 list prices.

| Metric | Value | 95% CI | n | Target | Status |
|---|---|---|---|---|---|
| numeric_threshold_exact | 100.0% | 100.0% to 100.0% | 9 | >= 97.0% | meets target |
| threshold_abstention_rate | 0.0% | 0.0% to 0.0% | 9 | <= 10.0% | meets target |
| correct_abstention_rate | n/a |  | 0 |  | report |
| categorical_macro_f1 | 100.0% | 100.0% to 100.0% | 45 | >= 90.0% | meets target |
| list_item_f1 | 100.0% | 100.0% to 100.0% | 32 | >= 85.0% | meets target |
| citation_validity | 100.0% | 100.0% to 100.0% | 69 | >= 100.0% (hard gate) | meets target |
| field_precision | 100.0% | 100.0% to 100.0% | 69 |  | report |
| field_recall | 100.0% | 100.0% to 100.0% | 69 |  | report |
| crosscheck_expected_match | 100.0% | 100.0% to 100.0% | 12 |  | report |
| schedule_h_free_pct_agreement | n/a |  | 0 | >= 85.0% | n/a |
| schedule_h_agree_count | 27 |  | 27 |  | report |
| schedule_h_disagree_policy_newer_than_filing_count | 0 |  | 0 |  | report |
| schedule_h_disagree_unexplained_count | 0 |  | 0 |  | report |
| schedule_h_not_reported_count | 0 |  | 0 |  | report |
| schedule_h_missing_count | 0 |  | 0 |  | report |
| disagreement_triage_rate | n/a |  | 0 | >= 100.0% | n/a |
| calibration_ece | 0.065 | 0.065 to 0.065 | 69 | <= 0.050 | misses target |
| cost_per_hospital_usd | $0.0977 | $0.0977 to $0.0977 | 3 | <= $0.5000 | meets target |
| compile_error_rate | 0.0% | 0.0% to 0.0% | 3 |  | report |
| commons_free_cutoff_accuracy | 100.0% | 100.0% to 100.0% | 3 |  | report |
| naive_free_cutoff_accuracy | 66.7% | 0.0% to 100.0% | 3 |  | report |
| naive_baseline_gap_points | +33.3 pts | +0.0 pts to +100.0 pts | 3 | >= +20.0 pts | meets target |

Schedule H cross-checks by result (only `disagree_unexplained` can become a public discrepancy for review, after the hospital's right of reply):

- `agree`: 27
- `disagree_policy_newer_than_filing`: 0
- `disagree_unexplained`: 0
- `missing`: 0
- `not_reported`: 0

| Field | Gold answered | Published | Correct | Precision | Recall | Gold abstained | Abstentions matched |
|---|---|---|---|---|---|---|---|
| agb_method | 3 | 3 | 3 | 100% | 100% | 0 | 0 |
| agb_pct | 3 | 3 | 3 | 100% | 100% | 0 | 0 |
| application_window | 3 | 3 | 3 | 100% | 100% | 0 | 0 |
| asset_test | 3 | 3 | 3 | 100% | 100% | 0 | 0 |
| award_period_months | 3 | 3 | 3 | 100% | 100% | 0 | 0 |
| determination_notice | 3 | 3 | 3 | 100% | 100% | 0 | 0 |
| effective_or_revision_date | 3 | 3 | 3 | 100% | 100% | 0 | 0 |
| excluded_services | 3 | 3 | 3 | 100% | 100% | 0 | 0 |
| facilities_covered | 3 | 3 | 3 | 100% | 100% | 0 | 0 |
| fpg_tiers | 3 | 3 | 3 | 100% | 100% | 0 | 0 |
| fpg_year_rule | 3 | 3 | 3 | 100% | 100% | 0 | 0 |
| household_definition | 3 | 3 | 3 | 100% | 100% | 0 | 0 |
| income_definition | 3 | 3 | 3 | 100% | 100% | 0 | 0 |
| insured_patients | 3 | 3 | 3 | 100% | 100% | 0 | 0 |
| medicaid_application_required_first | 3 | 3 | 3 | 100% | 100% | 0 | 0 |
| policy_title | 3 | 3 | 3 | 100% | 100% | 0 | 0 |
| presumptive_eligibility | 3 | 3 | 3 | 100% | 100% | 0 | 0 |
| provider_list_location | 3 | 3 | 3 | 100% | 100% | 0 | 0 |
| refund_policy | 3 | 3 | 3 | 100% | 100% | 0 | 0 |
| required_documents | 3 | 3 | 3 | 100% | 100% | 0 | 0 |
| residency | 3 | 3 | 3 | 100% | 100% | 0 | 0 |
| screening_timing | 3 | 3 | 3 | 100% | 100% | 0 | 0 |
| submission_channels | 3 | 3 | 3 | 100% | 100% | 0 | 0 |

Naive-agent baseline vs the Commons on the free-care cutoff (%FPG):

| Hospital | Gold | Commons | Naive agent |
|---|---|---|---|
| valley-childrens | 300% | 300% | 300% |
| panhandle-university | 200% | 200% | 500% |
| brazos-valley | 200% | 200% | 200% |
