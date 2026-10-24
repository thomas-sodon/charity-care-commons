# Disclaimer

Every screening estimate, packet and public page carries the disclaimer below. The text is versioned; each `Determination` records the `disclaimer_version` it was shown with, so we can tell which wording a navigator saw.

The canonical copy lives in `charitycare.config.DISCLAIMER_TEXT` and `DISCLAIMER_VERSION`. This file must match it (a test checks this). Change both together, bump the version, and add a changelog entry.

## Current version: `disclaimer@2026-10-24`

> Charity Care Commons provides legal information, not legal advice. 'Likely eligible' is an estimate based on the hospital's published policy as of {as_of}, quoted above, and the facts you entered; the hospital makes the final decision. We are not affiliated with any hospital or the IRS. Confirm details with the hospital. For free full-service help, contact Dollar For.

`{as_of}` is replaced with the date we last confirmed the hospital's policy document.

## Where it appears

- Every response of `POST /v1/screen` and every MCP `screen_household` result.
- The navigator console, next to each estimate.
- The packet cover letter and checklist.
- The footer of every public hospital page and the dashboard.

## Related boundaries

- The cover letter *requests* suspension of extraordinary collection actions and written notice of any missing information, citing 26 CFR 1.501(r)-6(c)(5)-(6). It never asserts an outcome.
- Nothing is submitted for a patient; the patient reviews and signs the packet.
- Outcome labels are fixed (`charitycare.wording.OUTCOME_LABELS`): "Likely eligible for free care", "Likely eligible for discounted care", "Likely not eligible on income alone", "Preliminary: based on fields not yet verified by a reviewer", "Not enough information to estimate".

## Review status

Draft pending attorney review (planned for build week 4). Until that review is complete, real patient cases run only with design partners, with patient consent, on an invite-gated instance.

## Changelog

- `disclaimer@2026-10-24`: first version, text from design section 11.
