# Outreach drafts (week 0)

**Drafts only.** Nothing here has been sent. Bracketed fields are placeholders to fill in by hand; do not send any of these under another organization's name, and do not imply an endorsement or partnership that does not exist. Real contact names and addresses are looked up and confirmed before sending.

Plan (design sections 2b and 14): before writing code, contact 8-10 Texas navigator organizations to secure 2-3 design partners; in parallel, ask Dollar For and Tradeoffs for permission to use their material; book a 2-hour attorney review for week 4; and file one batched Texas records request.

## 1. Design-partner emails

### 1a. Oncology financial navigation team

> **Subject:** 30 minutes on Texas hospital charity-care rules? (free tool, looking for 2-3 pilot teams)
>
> Hi [Name],
>
> I'm [Your name], an independent developer building Charity Care Commons, a free, open registry of Texas nonprofit hospitals' financial assistance rules. Every rule is quoted from the hospital's own policy with the page number, and checked against the hospital's IRS Schedule H filing. On top of it is a packet tool that fills the hospital's own application form and lists the documents that policy asks for.
>
> Your team helps patients with large hospital bills every week, so I'd value your view on what would actually save time. Could we have a 30-minute call in the next two weeks? I'd also like to test the packet tool on your common case types, using made-up or fully de-identified examples only. No patient data is needed and nothing is stored.
>
> It's free and will stay free for patients and navigators. If it's not useful, that's a helpful answer too.
>
> Thanks,
> [Your name] · [email] · [phone]

### 1b. Legal-aid medical-debt or consumer practice

> **Subject:** Cited hospital FAP rules and the 240-day window, for medical-debt clients
>
> Hi [Name],
>
> I'm building Charity Care Commons: a free, open registry of Texas nonprofit hospitals' financial assistance policies, with each rule quoted from the policy (page cited) and compared with the hospital's Schedule H. It also computes the federal 240-day application window and drafts a cover letter that *requests* suspension of collection actions under 26 CFR 1.501(r)-6. It provides legal information, not advice, and the patient signs everything.
>
> Would someone on your consumer or medical-debt team give me 30 minutes of feedback, and say whether a cited lookup like this would help with intake? We're looking for 2-3 pilot organizations. I'm also arranging an attorney review of the letter template and would welcome your comments on it.
>
> Best,
> [Your name] · [email]

### 1c. FQHC eligibility team

> **Subject:** Helping patients apply for hospital charity care (pilot, free)
>
> Hi [Name],
>
> Your eligibility staff already screen patients for Medicaid and sliding-fee programs. Charity Care Commons adds the local hospitals' own financial assistance rules: income cutoffs, asset tests, residency, required documents, deadlines, each quoted from the hospital's policy. A short form gives a "likely eligible" estimate with citations, and can produce a filled hospital application for the patient to review and sign.
>
> Could we talk for 30 minutes about whether this fits your workflow? We'd test only with synthetic examples.
>
> Thank you,
> [Your name]

### 1d. Texas 2-1-1 / United Way

> **Subject:** Data for "help with a hospital bill" referrals (free and open)
>
> Hi [Name],
>
> When callers ask for help with a hospital bill, the answer depends on that hospital's financial assistance policy. Charity Care Commons publishes those policies for Texas as open, cited data (CC BY 4.0) with a free API, updated when a policy changes. I'd like to learn how your specialists handle these calls today and whether a lookup would help. Could we schedule 30 minutes?
>
> Best regards,
> [Your name]

**Follow-up (one week later, once):** "Just bumping this in case it got buried. Happy to work around your schedule, or to send a two-minute video instead of a call."

## 2. Permission requests

### 2a. Dollar For: URL list and correction exchange

> **Subject:** Permission request: Texas policy URLs as discovery hints, plus a free feed back to you
>
> Hi [Name],
>
> I'm building Charity Care Commons, an open, cited registry of Texas nonprofit hospital financial assistance policies that is cross-checked against Schedule H. Our tools send patients who want full-service help to Dollar For.
>
> Two asks and one offer:
>
> 1. **Permission** to use your list of Texas hospital policy URLs as *discovery hints only*. We would fetch and hash the documents ourselves, never redistribute your list, and credit you wherever you prefer.
> 2. If you're open to it, a channel to send you corrections we find.
> 3. **Offer:** free link-health alerts (policy URLs that went dead or changed), our change feed, and our Schedule H discrepancy flags for Texas, in whatever format suits you.
>
> No is a fine answer; we'll discover URLs from IRS filings and hospital sites either way. Would a short call help?
>
> Thanks for the work you do,
> [Your name]

### 2b. Tradeoffs: benchmark permission

> **Subject:** Permission to benchmark against your Texas FAP codings (evaluation only)
>
> Hi [Name],
>
> Your Hidden Help reporting and methodology are the reason this project exists. I'm building Charity Care Commons, an open registry of Texas hospital financial assistance rules where each value is quoted from the policy and checked against Schedule H, with published accuracy.
>
> Your Texas codings table has no stated license, so I'm asking before using it. May I use it **only as an external benchmark** in our evaluation (comparing our extracted cutoffs with yours and reporting agreement), without redistributing it? We would cite Tradeoffs in the eval report and share the comparison with you before publishing, including cases where we think either of us is wrong.
>
> Thank you,
> [Your name]

## 3. Texas Public Information Act request (batched, one letter for all missing policies)

Send to the custodian of records at the agency that receives nonprofit hospitals' annual charity-care submissions (confirm the correct agency and officer for public information before sending). The discover stage generates the facility list for the attachment (`python -m charitycare.discover records --letter`).

> [Date]
>
> Officer for Public Information
> [Agency name]
> [Address]
> [Email for public information requests]
>
> **Re: Request for public information under the Texas Public Information Act (Gov't Code ch. 552)**
>
> Dear Officer for Public Information:
>
> Under the Texas Public Information Act, I request copies of the following records for the nonprofit hospitals listed in the attachment:
>
> 1. The most recent financial assistance (charity care) policy each hospital submitted to your agency, together with any application form, plain-language summary and billing and collections policy submitted with it.
> 2. Any record showing the date each document was submitted or the period it covers.
>
> I prefer electronic copies (PDF or the native format) sent to [email]. If any portion is withheld, please release the remainder and cite the exception relied on for each redaction. If the cost will exceed $40, please send an itemized estimate before proceeding. If these records are held by another governmental body, please tell me which one.
>
> The records will be used for a free, public, non-commercial registry of hospital financial assistance policies.
>
> Sincerely,
> [Name] · [Mailing address] · [Email] · [Phone]
>
> Attachment: list of hospitals (name, city, CMS Certification Number, EIN).

Track the request date; the Act's general deadline is "promptly", with a 10-business-day point by which the agency must produce the records, state when they will be available, or request an Attorney General ruling (confirm current rules before relying on this).

## 4. Hospital pre-publication email (right of reply)

Sent for every `disagree_unexplained` item before it appears on the public discrepancy list.

> **Subject:** Please review: financial assistance policy listing for [Hospital], response by [date, 10 business days]
>
> Dear [Financial assistance / patient financial services contact],
>
> Charity Care Commons publishes a free, open listing of Texas hospitals' financial assistance policies. Before publishing, we ask hospitals to review any item where the posted policy and the hospital's Schedule H filing appear to differ.
>
> - **Field:** [e.g. asset test]
> - **Your policy** ([document title], revised [date], page [n]): "[verbatim quote]"
> - **Schedule H** ([tax period], Part V line [13c]): [reported value]
>
> This may have a simple explanation (for example a policy revised after the filing period, or group reporting), and we will show your explanation next to the item, in your words. We describe these as "discrepancies for review", not findings. If our reading is wrong, tell us and we will correct it before publishing: [correction link] or reply to this email by [date].
>
> Hospitals are never charged to be listed or to correct a listing.
>
> Sincerely,
> [Your name], Charity Care Commons · [email]
