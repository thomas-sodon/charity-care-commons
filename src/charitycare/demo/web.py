"""A tiny offline "web" of fictional hospital sites for the discovery agent.

``web/url_map.json`` maps every absolute URL to a corpus-relative file (HTML
pages under ``web/`` and the PDFs under ``docs/``), so ``discover/`` can run its
fetch tools against the corpus without network access. Link depth and anchor
text vary on purpose (the Pineywoods policy sits two clicks deep under
"Billing", Lone Star links facilities to one system policy).
"""

from __future__ import annotations

import html

from charitycare.demo.spec import HospitalSpec


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(title)}</title></head>\n<body>\n<p><em>Fictional hospital - synthetic demo site.</em></p>\n"
        f"{body}\n</body></html>\n"
    )


def site_pages(h: HospitalSpec, doc_urls: dict[str, str]) -> dict[str, str]:
    """Return {absolute URL: HTML} for one hospital site."""
    base = h.base_url
    pages: dict[str, str] = {}
    fa_links = []
    labels = {
        "fap": "Financial Assistance Policy (PDF)" if h.id != "brazos-valley" else "Charity care (Financial Assistance Policy)",
        "application_en": "Financial Assistance Application (PDF)",
        "application_es": "Solicitud de Asistencia Financiera (PDF, Español)",
        "pls_en": "Plain Language Summary (PDF)",
        "pls_es": "Resumen en lenguaje sencillo (PDF, Español)",
    }
    for kind, url in doc_urls.items():
        fa_links.append(f'<li><a href="{html.escape(url)}">{html.escape(labels.get(kind, kind))}</a></li>')
    fa_body = f"<h1>Financial Assistance</h1>\n<p>Call {html.escape(h.phone)} with questions.</p>\n<ul>\n" + "\n".join(fa_links) + "\n</ul>"
    if h.id == "pineywoods-regional":
        pages[f"{base}/"] = _page(h.name, f'<h1>{html.escape(h.name)}</h1>\n<a href="{base}/patients/">Patients &amp; Visitors</a>')
        pages[f"{base}/patients/"] = _page("Patients", f'<a href="{base}/billing/">Billing &amp; Insurance</a>')
        pages[f"{base}/billing/"] = _page("Billing", f'<a href="{base}/financial-assistance/">Help paying your bill</a>')
    elif h.addendum:
        fac_links = "\n".join(
            f'<li><a href="{base}/locations/{f.ccn}/">{html.escape(f.name)}</a></li>' for f in h.facilities
        )
        pages[f"{base}/"] = _page(h.name, f"<h1>{html.escape(h.name)}</h1>\n<ul>\n{fac_links}\n</ul>\n<a href=\"{base}/financial-assistance/\">Financial Assistance</a>")
        for f in h.facilities:
            pages[f"{base}/locations/{f.ccn}/"] = _page(
                f.name, f'<h1>{html.escape(f.name)}</h1>\n<p>{html.escape(f.address)}, {html.escape(f.city)}</p>\n<a href="{base}/financial-assistance/">Paying for care</a>'
            )
    else:
        pages[f"{base}/"] = _page(h.name, f'<h1>{html.escape(h.name)}</h1>\n<a href="{base}/financial-assistance/">Financial Assistance</a>')
    pages[f"{base}/financial-assistance/"] = _page("Financial Assistance", fa_body)
    pages[f"{base}/robots.txt"] = "User-agent: *\nAllow: /\n"
    return pages
