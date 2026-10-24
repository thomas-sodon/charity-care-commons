# Privacy

Charity Care Commons publishes facts about hospital policies. It does not need, and does not keep, facts about patients.

Privacy notice version: `privacy@2026-10-24` (draft; counsel review is required before any real-case use).

## What we store

| Data | Stored? | Where |
|---|---|---|
| Hospital policy documents, their hashes and extracted fields | Yes | Registry and document store; published under CC BY 4.0 as extracted facts plus short quotes. We link to and hash documents rather than re-hosting them in bulk. |
| Schedule H and CMS facility data | Yes | Registry (public government data). |
| Household facts entered for a screening estimate (size, income, assets, county, insurance, dates) | **No** | Request memory only. There is no database table for them. |
| Bill and pay-stub images and their extracted values | **No** | Processed in memory and in a private temporary directory that is deleted before the response returns (and swept after 30 minutes at most if a process dies). |
| Packet telemetry | Yes, without household data | `PacketMetric`: template id, field count, whether readback matched, retries, latency, cost. |
| Request logs | Allowlisted fields only | Template id, timings and costs. Request bodies are never logged. |
| Reviewer and hospital correspondence | Yes | Reviewer names on field verdicts; hospital statements submitted for publication. |

## Data minimization

- The packet compiler never asks for Social Security numbers, account numbers or full dates of birth. Those form fields are left blank and marked "write by hand".
- An input filter masks SSN-like and account-number patterns in uploaded or typed text.
- EXIF metadata is stripped from images before processing.
- The `.ics` deadline file is generated for download; we do not keep a calendar of patients.
- There are no user accounts in the MVP.

## Model provider

Extraction calls go to the Anthropic Claude API. Before any real patient data is processed we will confirm the API's retention terms and request zero data retention; US-only inference is available if a partner requires it. The public demo and all tests use synthetic data and recorded fixtures only.

## Law (to confirm with counsel)

- HIPAA likely does not apply: we are not a covered entity, and not a business associate unless a covered entity contracts with us.
- Consumer health-data laws can apply. Because of Washington's My Health My Data Act, the pilot is not offered to Washington users.
- Texas Data Privacy and Security Act: confirm small-business status and the consent rule for sensitive data with counsel.

## Real cases

Real cases run only through design partners, with the patient's consent, on an invite-gated instance, after the attorney review. Any privacy incident halts real-case use until it is resolved and disclosed.

## Contact

Privacy questions and deletion requests: privacy@charitycarecommons.org (placeholder until the project domain is live).
