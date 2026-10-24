# Architecture and module contract

This document is the contract between the stages that build Charity Care Commons in parallel. The authoritative product design is the *winning-idea design* document (sections cited as §N). If this file and the code disagree, the code in `src/charitycare/schemas.py` wins, and this file should be fixed.

## 1. Data flow (§8)

```
            ┌──────────── seed/ ────────────┐
 IRS 990 XML│ Schedule H Part V parser      │──► Organization, Facility, ScheduleHFacility
 CMS CSV ──►│ facility universe + URL seeds │──► Source (16a-c URLs, Section C narrative URLs)
            └───────────────┬───────────────┘
                            ▼
            ┌────────── discover/ ──────────┐
            │ agent: fetch_page, sitemap,   │──► Source, DocumentVersion (sha256 = identity)
            │ wayback, schedule_h_context;  │──► PolicyVersion (+ doc_shas), PolicyFacilityMap
            │ Haiku doc classifier          │
            └───────────────┬───────────────┘
                            ▼
            ┌────────── compile/ ───────────┐
            │ text layer (pdftext) → Sonnet │──► FieldRecord (status=provisional|abstained)
            │ compile → quote gate → re-ask │──► CrossCheck (vs ScheduleHFacility)
            │ → round-trip → cross-check →  │──► ReviewTask
            │ Opus adjudication (advisory)  │
            └───────────────┬───────────────┘
                            ▼  human review flips fields to verified (api/ console)
        ┌──────────────┬────┴──────────┬──────────────────┐
        ▼              ▼               ▼                  ▼
     engine/        packet/          api/              site/          evals/ (scores every stage
  Determination  ReadbackResult  REST + /v1/screen   static pages,     against data/demo/gold and
  (code only)    PacketMetric    review console,     CSV/Parquet       records EvalRun)
                                 MCP server          exports
```

The LLM extracts; code computes; humans verify. No LLM output ever becomes a determination directly.

## 2. Module map and ownership

| Path | Owner stage | Responsibility | Main inputs → outputs |
|---|---|---|---|
| `schemas.py` | foundation | All shared Pydantic models (§5, §8, §10) and the Open FAP Schema export. **Do not redefine these models elsewhere.** Add new models here only through the integrator. | n/a |
| `config.py` | foundation | Paths (`CCC_*` env), model tiers, section 13 prices, budget caps, `PROMPT_VERSIONS`, disclaimer text/version | env → `Settings` |
| `llm.py` | foundation | `LLMRequest`, `get_backend()`, Anthropic structured-output backend, replay/record backends, cost ledger | `LLMRequest[M]` → `LLMResult[M]` |
| `store.py` | foundation | SQLite registry with one table per §10 entity, typed repository methods (Postgres is the production target) | models ↔ rows |
| `fpg.py` | foundation | HHS poverty guidelines 2023-2026, `fpg()`, `pct_fpl()`, exact `income_within_pct()` | year, size → dollars |
| `pdftext.py` | foundation | sha256, per-page text layer, optional Tesseract OCR, `normalize_text`, `find_quote` | PDF bytes → `PageText[]` |
| `wording.py` | foundation | Banned-wording guard ("eligible", "compliant", "violation"), outcome labels | text → violations |
| `demo/` | foundation | Deterministic synthetic corpus + gold + replay fixtures | specs → `data/demo/`, `fixtures/llm/` |
| `cli.py` | foundation, integrator extends | `ccc` entry point; register subcommands in `COMMANDS` | argv |
| `seed/` | seed builder | Schedule H Part V parser with pinned xpaths (§8 S1), CMS loader, facility universe, narrative URL recovery | `data/demo/irs/*`, `data/demo/cms/*` → registry rows |
| `discover/` | discover builder | Discovery agent and tools (read-only, domain-allowlisted), doc classifier, versioned doc store, facility→policy mapping (§5.4) | `Source` seeds, `web/url_map.json` offline → `DocumentVersion`, `PolicyVersion`, `PolicyFacilityMap` |
| `compile/` | compile builder | Policy compiler, quote-verification gate (§8 S4), targeted re-ask, round-trip tests (S5), Schedule H cross-check (S6), adjudicator (S7) | `DocumentVersion` + `ScheduleHFacility` → `FieldRecord`, `CrossCheck`, `ReviewTask` |
| `engine/` | engine builder | Deterministic eligibility engine, 240-day and policy deadlines, synthetic households, property tests | `CompiledPolicy` / `FieldRecord[]` + `Household` → `Determination` |
| `packet/` | packet builder | Bill and pay-stub extraction, form templates, fill + render + readback, cover letter, checklist, `.ics` | `Determination` + `FormTemplate` → packet PDF, `ReadbackResult`, `PacketMetric` |
| `api/` | api builder | FastAPI read API (§2b), `POST /v1/screen`, review console (HTMX), MCP server (`find_facility`, `get_policy`, `screen_household`, `list_changes`) | registry → HTTP/MCP |
| `site/` | site builder | Static public site (dashboard, hospital pages, methods, eval report), CC-BY CSV/Parquet export via DuckDB | registry → `site/dist/`, exports |
| `evals/` | evals builder | Suites, scorers (numeric, enum, list-F1, citation validity), naive-agent baseline, `python -m evals run --suite ...` (or `ccc eval`) | gold + pipeline outputs → `EvalRun` |

Each stage owns `src/charitycare/<dir>/` and `tests/<dir>/`. Stages do not edit each other's directories or the foundation modules; request contract changes through the integrator. New third-party dependencies go in `pyproject.toml` (integrator review).

## 3. Shared contracts

### 3.1 The verification-carrying tuple (§5)

`FieldRecord` = `field: Field[T]` (value + `Evidence[]` + confidence + abstain_reason) + `status: FieldStatus` + `crosschecks: CrossCheck[]` + `as_of`. Rules:

- **No quote, no publish.** A field is published only when every evidence quote is found on its cited page of the hashed document, using `pdftext.find_quote` (normalized exact match) or the compile stage's ≥ 0.98 similarity matcher built on `pdftext.normalize_text`.
- `Field.abstain_reason` set ⇒ `value is None` (the model validator enforces this). Abstention is a valid, first-class answer.
- `FieldStatus.provisional` fields must not drive a determination. The engine returns `preliminary_provisional_fields` and lists them in `Determination.provisional_fields_used`. `schemas.DETERMINATION_FIELDS` names the fields a determination depends on.
- Only `CrossCheckResult.disagree_unexplained` can reach the public "discrepancies for review" list, and only after the hospital's right of reply (`HospitalResponse`).

### 3.2 LLM calls (§8, §13)

```python
from charitycare.llm import LLMRequest, DocumentPart, get_backend
from charitycare.config import PROMPT_VERSIONS

req = LLMRequest(
    prompt_version=PROMPT_VERSIONS["compile_policy"],   # bump on any prompt or schema change
    model_tier="sonnet",                                 # haiku | sonnet | opus
    doc_sha256=fap_sha,                                  # primary document: part of the fixture key
    system=STATIC_FIELD_GUIDE,                           # cached prefix (breakpoint 1)
    documents=(DocumentPart("text", page_text_xml),),    # cached documents (breakpoint 2)
    user="Compile the policy.",                          # volatile
    output_schema=CompiledPolicy,
    variant="",                                          # e.g. "fpg_tiers" for a re-ask
)
result = get_backend().complete(req)   # result.output is a validated CompiledPolicy
```

- Backend selection: replay unless `ANTHROPIC_API_KEY` is set or `CCC_LLM=live`; `CCC_LLM=record` calls live and writes fixtures.
- Fixture key = `(prompt_version, doc_sha256, model_tier, variant)`, stored at `fixtures/llm/<prompt_version>/<tier>/<sha>[.<variant>].json`. Stages that need fixtures for their own prompts write them with `llm.write_fixture(..., synthetic=True)` derived from gold, and commit them.
- Errors: `FixtureMissing`, `LLMRefusal`, `LLMTruncated` and `BudgetExceeded` all subclass `LLMError`. Callers catch `LLMError` and abstain or fall back; they never invent values.
- Costs: `LLMResult.cost_usd` is computed at section 13 prices. Replayed results are flagged `simulated=True`, so eval cost metrics work offline. The ledger enforces `CCC_RUN_TOKEN_CEILING` and the daily cap `CCC_DAILY_SPEND_CAP_USD` for live calls.
- Registered prompt versions: `compile_policy`, `reask_field` (output: one `Field` JSON object; variant = field key), `classify_doc`, `adjudicate`, `change_triage`, `extract_bill`, `extract_income`, `form_template`, `naive_baseline`.

### 3.3 Registry

`store.Registry(path | ":memory:")` (default `data/registry.sqlite`). Generic `put/get/select/delete/count`, plus typed helpers: `upsert_facility`, `list_facilities`, `add_schedule_h`, `schedule_h_for_ein`, `add_document_version`, `add_policy_version` (supersedes handling), `map_policy_facility`, `policy_for_facility(facility_id, as_of)`, `put_field` (with crosschecks), `fields_for_policy`, `set_field_status`, `discrepancies`, `open_review_tasks`, `update_review_task`, `add_hospital_response`, `changes_since`, `upsert_form_template`, `add_eval_run`, `add_packet_metric`. IDs: facility `tx-{ccn}`; field `{policy_version_id}:{key}`; Schedule H section `{object_id}:{group_letter or facility_num}`.

### 3.4 Privacy and wording (§11)

- Household facts, bills and pay stubs live in request memory only. There is no table for them. `PacketMetric` is the only packet telemetry, and logs use an allowlist (template id, timings, costs).
- All generated user-facing text passes `wording.assert_safe_wording` (quotes from hospital documents are exempt). Use `wording.OUTCOME_LABELS` and `config.DISCLAIMER_TEXT` / `DISCLAIMER_VERSION`.

## 4. The synthetic demo corpus

Build with `ccc demo build` (`ccc demo` without a subcommand runs the whole pipeline; see section 7) (deterministic: byte-identical PDFs, stable sha256s, and the corpus checksum in `MANIFEST.json`). Tests can use the session fixture `demo_corpus` from `tests/conftest.py`, which builds into a temp dir.

```
data/demo/
  MANIFEST.json                    hospitals → facilities (facility_id, ccn), documents (path, sha256, url), gold path
  docs/<hospital>/fap.pdf, application_<en|es>.pdf, pls_<en|es>.pdf
  gold/<hospital>.json             policy (CompiledPolicy JSON), documents (incl. form_template), income_table,
                                   schedule_h.expected_crosschecks, notes
  irs/xml/<OBJECT_ID>_public.xml   IRS e-file layout (namespace http://www.irs.gov/efile)
  irs/index_2026_demo.csv, irs/2026_TEOS_XML_DEMO.zip
  cms/Hospital_General_Information.csv   real CMS column layout (+4 decoys to exclude)
  web/<domain>/... + web/url_map.json    offline website: absolute URL → corpus-relative file
fixtures/llm/compile_policy@v1/sonnet/<fap_sha>.json   compile replay fixtures (one per hospital)
fixtures/llm/reask_field@v1/sonnet/<fap_sha>.fpg_tiers.json
fixtures/llm/DELIBERATE_ERRORS.json
```

| Hospital | Exercises |
|---|---|
| example-methodist | prints a 2025 income table (round trip must **match**); $10k asset test missing from Schedule H → `disagree_unexplained`; 6-county residency; fillable form; fixture error: paraphrased refund quote |
| lone-star-health | 3-facility system policy + Addendum A (page 7); Schedule H group A; flat form; fixture error: one required-docs quote cites the wrong page |
| valley-childrens | children's hospital; English fillable + **Spanish** flat application + Spanish PLS; statewide residency |
| pineywoods-regional | **image-only** FAP page 3 and a scanned application; policy revised after the filing period → `disagree_policy_newer_than_filing` |
| gulf-coast-baptist | insured/catastrophic (20% of income) clause; Medicaid first; discretionary "may" presumptive clause → abstain `ambiguous_may_clause`; fixture error: free tier misread as 250% (re-ask fixture returns 200%) |
| hill-country-community | critical access; $5k asset test; net income; no refund or screening clause → abstain `not_addressed` |
| panhandle-university | sliding scale to 500%; table labelled 2026 but computed from 2025 guidelines → round trip must **mismatch** |
| brazos-valley | 365-day window; presumptive eligibility via credit data; Schedule H 16a says "SEE PART V, SECTION C" (narrative URL recovery) |

Schedule H xpaths follow the IRSx variable catalogue, for example `/IRS990ScheduleH/HospitalFcltyPoliciesPrctcGrp/FPGFamilyIncmLmtFreeCarePct` (13a), `AssetLevelCriteriaInd` (13c), `InsuranceStatusCriteriaInd` (13e), `ResidencyCriteriaInd` (13g), `DescribedInfoInd`/`DescribedSuprtDocInd` (15a-b), `FAPAvailableOnWebsiteURLTxt`/`FAPAppAvailableOnWebsiteURLTxt`/`FAPSummaryOnWebsiteURLTxt` (16a-c), `FAPTranslatedInd` (16i), `Permit*Ind` (18), `LookBack*Ind`/`ProspectiveMedicareMedicaidInd` (22), `SupplementalInformationGrp/ExplanationTxt` (Section C). Unchecked checkboxes are omitted; booleans appear as `true`/`1`/`false`/`0`; percentages as `200` or `200.00`.

Without Tesseract, image-only pages have no text layer. `pdftext.page_texts` marks them `ocr_unavailable`, and their fields must abstain as `illegible` (the gold labels still carry the true value, so evals can report the OCR gap).

## 5. Conventions

- Python ≥ 3.12, typed, Pydantic v2, concise docstrings. Tests use pytest (+ hypothesis) and must run offline at zero cost.
- Everything is deterministic under replay; no test may require network access or an API key.
- Money is `Decimal`; percentages of FPG are floats in the schema and are compared with `fpg.income_within_pct` (exact).
- Dates are `datetime.date`; timestamps are timezone-aware UTC (`schemas.utcnow`).

## 6. Evaluation harness (`evals/`, appended by the evals stage)

The harness is a top-level package `evals/` (run as `python -m evals` or `ccc eval`; it ships in the wheel and pytest puts the repo root on `pythonpath`), not `charitycare.evals`; it imports `charitycare.compile`, `engine` and `packet` lazily.

- `evals/suites/*.yaml`: `smoke`, `full`, `test` (compile, scored against gold), `engine` (300 households vs the oracle), `packet` (readback, templates, extraction). Targets are the section 12 numbers.
- `evals/datasets/demo.yaml`: corpus root and splits (smoke / dev / frozen test).
- `python -m evals run --suite <s> [--mode replay|live|record]` compiles in a fresh in-memory registry (or scores an existing one with `--registry`), re-verifies every published quote against the hashed PDFs, scores each metric with a bootstrap 95% CI over hospitals, writes `evals/runs/<run_id>.json` + `latest-<suite>.json`, an `EvalRun` row, and `evals/reports/latest.{json,md}` (consumed by `site/`).
- `python -m evals gate --suite <s>` compares with `evals/baselines/<s>.json` (`<s>.live.json` for live runs): fails on a > 2-point drop, citation validity < 100%, cost per hospital > +25%, or a missed hard target.
- Naive-agent baseline (`evals/baseline.py`): Sonnet-class model + `web_search` server tool, no pipeline; replay fixtures under `fixtures/llm/naive_baseline@v1/` keyed by `sha256("naive:<hospital_id>")`. The committed demo fixtures are synthetic and labelled SIMULATED.
- Also: `agreement` (inter-rater agreement + Cohen's kappa), `schema-compat` (Open FAP Schema freshness and backward compatibility), `support-sample` / `support-score` (human-judged citation support).
- CI: `.github/workflows/ci.yml` (always-on tests + offline evals + gates + schema check; live smoke on PRs touching `prompts/`, `schemas/`, `compile/` when the `ANTHROPIC_API_KEY` secret exists) and `.github/workflows/refresh.yml` (weekly refresh skeleton).

## 7. Integration (`ccc demo`) and shared contracts

- `ccc demo` (`src/charitycare/demo/pipeline.py`) chains the real stages on one SQLite registry: corpus build (checked against the committed `MANIFEST.json` checksum) → `seed.pipeline.seed_demo` → `discover.pipeline.discover_demo` → `compile.pipeline.compile_facility` (replay backend) → `demo.reviewer.auto_review` + `verify_templates` → `engine.screen` (persona Maria) → `api.navigator.build_packet` → evals (`full` scores this registry) → `export.snapshot.export_release` → `site.build.build_site`. Outputs go to `data/demo-run/` and `site/dist/` (both gitignored).
- The demo reviewer (`demo/reviewer.py`) refuses to run without an explicit `demo=True` flag and signs every decision `demo:gold-reviewer`. It approves only gold-matching values and never touches `quote_unverified` or `round_trip_mismatch` tasks.
- Shared contract additions made at integration:
  - `AbstainReason` gains `quote_unverified`, set when the quote gate still fails after the re-ask.
  - `ReviewTask` gains `impact` and `batch`; the `[impact N]` notes prefix is still read for older rows.
  - `ReviewDecision` lives in `schemas.py` and has its own `review_decision` table, in addition to the changelog entry.
  - `PROMPT_VERSIONS` adds `discover_agent`, `readback_crop` and `hardship_letter`.
  - `pdftext.uri_to_path` resolves `file://` document URIs for every reader.
  - `Registry.init_schema` adds columns for newly added model fields, so older registry files keep working.
- Every dependency is declared in `pyproject.toml`. pytest puts `src` and the repo root on `pythonpath`. Ruff is configured there as well.
