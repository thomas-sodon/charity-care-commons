# Contributing

Thank you for helping. Charity Care Commons is a public good: code under Apache-2.0, data under CC BY 4.0. Contributions are accepted under the same licenses.

## Setup

```
python -m pip install -e ".[dev]"
ccc demo build            # regenerate the synthetic corpus (deterministic)
python -m pytest -q       # offline, no API key, zero cost
python -m evals run --suite smoke --mode replay
```

Everything runs offline on recorded fixtures. A live run needs `ANTHROPIC_API_KEY`; `CCC_LLM=record` records new fixtures.

## Ground rules

- **Shared models** live in `src/charitycare/schemas.py`. Do not redefine them; propose changes in an issue. Any change to the Open FAP Schema must pass `python -m evals schema-compat --base <previous schema>`: removing a field, making one required, changing a type or removing an enum value is breaking and needs a version bump.
- **Prompts are versioned.** Any change to prompt text or an output schema bumps its entry in `config.PROMPT_VERSIONS`, so stale fixtures miss loudly. Commit re-recorded fixtures with the change.
- **The model never decides eligibility.** Models extract; code computes; people verify.
- **No quote, no publish.** Do not weaken the quote gate or the citation-validity gate (100%).
- **Wording.** Generated text says "likely eligible" and "discrepancy for review", never that a household is "eligible", or that a hospital is "compliant" or committed a "violation" (`charitycare.wording` checks this).
- **Privacy.** Never persist household facts, bills or pay stubs, and never log request bodies. Tests and examples use synthetic people only.
- **Discovery manners.** Respect robots.txt and rate limits; never circumvent bot protection.

## Pull requests

CI runs on every pull request: tests, ruff (when configured), offline evals on the replay fixtures, the regression gates and the schema check. Pull requests that touch `prompts/`, `schemas/` or `src/charitycare/compile/` also run the live `smoke` suite when the repository has an API key. A pull request fails if any gated metric drops by more than 2 points, citation validity is below 100%, or cost per hospital rises by more than 25%.

If a change legitimately moves a metric (for example a better prompt that changes abstentions), update the baseline in the same pull request and explain why:

```
python -m evals run --suite smoke --mode replay
python -m evals gate --suite smoke --update-baseline
```

## Adding a new state

New states are accepted **only if their gold sample passes the evaluation thresholds in CI**.

1. Build the facility universe (CMS) and Schedule H seed for the state, and run discovery and compile.
2. Label a gold sample of at least **15 policies** following [GOLD_LABELING.md](GOLD_LABELING.md), with at least 5 double-coded; include the agreement report.
3. Add `evals/datasets/<state>.yaml` and a suite `evals/suites/<state>.yaml` (copy `test.yaml`, set `dataset` and `split`).
4. Run the suite and gate it against the section 12 targets:

   ```
   python -m evals run --suite <state> --registry <state registry>
   python -m evals gate --suite <state> --update-baseline
   python -m evals gate --suite <state> --enforce-targets
   ```

   `--enforce-targets` fails on any missed target: threshold exact match >= 97%, threshold abstention <= 10%, categorical macro-F1 >= 0.90, list item F1 >= 0.85, citation validity 100%, calibration ECE <= 0.05 and cost per hospital <= $0.50.
5. Open the pull request with the eval report attached. Maintainers review a sample of the gold labels before merging.

## Adding a form template

Form templates are approved once per form hash by a person. Submit the template JSON with a filled-and-read-back proof from the packet suite (`python -m evals run --suite packet`); a template is accepted only at 100% readback on its fields.

## Corrections from hospitals

Hospitals can correct a listing at any time, free of charge, through the correction form or by email. Corrections are reviewed, applied, and noted in the change feed.

## Conduct

Be accurate and kind. We describe policies; we do not accuse hospitals or rank them.
