"""Synthetic CMS Hospital General Information CSV (Provider Data Catalog ``xubh-q36u`` layout).

The column header is copied from the real dataset (modified 2026-07-22). Rows
cover every demo facility plus decoys the facility-universe builder must
exclude: a proprietary hospital, a hospital-district hospital, a non-profit
psychiatric hospital and an out-of-state hospital.
"""

from __future__ import annotations

import csv
import io

from charitycare.demo.spec import HOSPITALS

COLUMNS = [
    "Facility ID", "Facility Name", "Address", "City/Town", "State", "ZIP Code", "County/Parish", "Telephone Number",
    "Hospital Type", "Hospital Ownership", "Emergency Services", "Meets criteria for birthing friendly designation",
    "Hospital overall rating", "Hospital overall rating footnote", "MORT Group Measure Count", "Count of Facility MORT Measures",
    "Count of MORT Measures Better", "Count of MORT Measures No Different", "Count of MORT Measures Worse", "MORT Group Footnote",
    "Safety Group Measure Count", "Count of Facility Safety Measures", "Count of Safety Measures Better",
    "Count of Safety Measures No Different", "Count of Safety Measures Worse", "Safety Group Footnote",
    "READM Group Measure Count", "Count of Facility READM Measures", "Count of READM Measures Better",
    "Count of READM Measures No Different", "Count of READM Measures Worse", "READM Group Footnote",
    "Pt Exp Group Measure Count", "Count of Facility Pt Exp Measures", "Pt Exp Group Footnote", "TE Group Measure Count",
    "Count of Facility TE Measures", "TE Group Footnote",
]

# (ccn, name, address, city, state, zip, county, phone, type, ownership)
DECOYS = [
    ("459951", "EXAMPLE RIVERWALK SURGICAL HOSPITAL", "400 EXAMPLE RIVER ROAD", "SAN ANTONIO", "TX", "78205", "BEXAR", "(210) 555-0951", "Acute Care Hospitals", "Proprietary"),
    ("459952", "EXAMPLE COUNTY HOSPITAL DISTRICT MEDICAL CENTER", "9100 EXAMPLE DISTRICT WAY", "EL PASO", "TX", "79905", "EL PASO", "(915) 555-0952", "Acute Care Hospitals", "Government - Hospital District or Authority"),
    ("459953", "EXAMPLE BEHAVIORAL HEALTH CENTER", "77 EXAMPLE SERENITY LANE", "AUSTIN", "TX", "78701", "TRAVIS", "(512) 555-0953", "Psychiatric", "Voluntary non-profit - Private"),
    ("379901", "EXAMPLE RED RIVER HOSPITAL", "10 EXAMPLE BORDER AVENUE", "DURANT", "OK", "74701", "BRYAN", "(580) 555-0901", "Acute Care Hospitals", "Voluntary non-profit - Private"),
]


def _measures(seed: int) -> list[str]:
    r = seed % 5
    rating = str(1 + r) if r < 4 else "Not Available"
    footnote = "" if rating != "Not Available" else "16"
    return [rating, footnote, "8", "8", "0", "8", "0", "", "8", str(5 + r % 3), "1", "4", "0", "", "11", "11", "1", "9", "1", "", "15", "15", "", "10", "10", ""]


def build_csv() -> str:
    buf = io.StringIO()
    buf.write(",".join(f'"{c}"' for c in COLUMNS) + "\n")
    w = csv.writer(buf, lineterminator="\n")
    seed = 0
    for h in HOSPITALS:
        for f in h.facilities:
            seed += 1
            w.writerow(
                [
                    f.ccn, f.name.upper(), f.address.upper(), f.city.upper(), "TX", f.zip, f.county.upper(), h.phone,
                    f.cms_type, h.ownership, "Yes", "Y" if seed % 2 else "",
                    *_measures(seed),
                ]
            )
    for row in DECOYS:
        seed += 1
        w.writerow([*row, "Yes", "", *_measures(seed)])
    return buf.getvalue()
