"""Deterministic synthetic data generator for the Helix Therapeutics demo.

Everything here is fabricated. There are no human names, no real trial
identifiers and no real supplier names anywhere in the output. The seed is
fixed so that every attendee and every rehearsal gets byte-identical data.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

SEED = 20260918
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

PROTOCOLS = [
    ("Inclusion criteria", "Participants must be aged 18-75 at the time of screening."),
    ("Inclusion criteria", "Documented diagnosis confirmed by central laboratory review."),
    ("Exclusion criteria", "Prior exposure to an investigational agent within 90 days."),
    ("Exclusion criteria", "Clinically significant renal impairment at screening."),
    ("Dosing", "Study drug is administered once daily in the morning with food."),
    ("Dosing", "Dose reduction to the next lower level is permitted once per participant."),
    ("Dosing", "Missed doses are not made up; the next scheduled dose is taken as planned."),
    ("Visit schedule", "Screening occurs within 28 days prior to first administration."),
    ("Visit schedule", "Safety follow-up occurs 30 days after the final administration."),
    ("Visit schedule", "Unscheduled visits are permitted and recorded as such."),
    ("Safety reporting", "Serious adverse events are reported within 24 hours of awareness."),
    ("Safety reporting", "Severity is graded using the protocol-specified grading scale."),
    ("Safety reporting", "Causality is assessed independently by the site investigator."),
    ("Laboratory", "Central laboratory processes all haematology and chemistry samples."),
    ("Laboratory", "Repeat testing is permitted for values flagged as implausible."),
    ("Randomisation", "Participants are randomised 2:1 to active or comparator."),
    ("Randomisation", "Stratification is by region and by baseline severity band."),
    ("Blinding", "The study is double-blind; unblinding requires medical monitor approval."),
    ("Blinding", "Emergency unblinding is available to sites at any hour."),
    ("Endpoints", "The primary endpoint is assessed at week 24."),
    ("Endpoints", "Key secondary endpoints are tested in a fixed hierarchical order."),
    ("Statistics", "The primary analysis uses the intent-to-treat population."),
    ("Statistics", "Missing data are handled by multiple imputation."),
    ("Supply", "Investigational product is shipped to sites in temperature-controlled units."),
    ("Supply", "Sites confirm receipt and temperature excursion status within 48 hours."),
    ("Supply", "Resupply is triggered automatically below a two-participant threshold."),
    ("Monitoring", "Risk-based monitoring visits occur at least every 12 weeks."),
    ("Monitoring", "Source data verification targets protocol-critical variables."),
    ("Consent", "Re-consent is required after any substantial protocol amendment."),
    ("Consent", "Consent documents are retained in the site investigator file."),
    ("Data management", "Queries are issued within five working days of data entry."),
    ("Data management", "Database lock follows resolution of all protocol-critical queries."),
    ("Withdrawal", "Participants may withdraw at any time without giving a reason."),
    ("Withdrawal", "Withdrawal from treatment does not require withdrawal from follow-up."),
    ("Pharmacovigilance", "The safety database is reconciled with the clinical database monthly."),
    ("Pharmacovigilance", "Expedited reports follow the applicable regional timelines."),
    ("Protocol deviations", "Deviations are categorised as major or minor at the time of entry."),
    ("Protocol deviations", "Major deviations are reviewed by the medical monitor."),
    ("Training", "Site staff complete protocol training before enrolling any participant."),
    ("Training", "Training records are retained for the duration of the study."),
]

EVENT_TERMS = [
    "headache", "nausea", "fatigue", "dizziness", "rash", "pyrexia",
    "arthralgia", "insomnia", "cough", "diarrhoea", "hypertension", "anaemia",
]
SEVERITIES = ["mild", "moderate", "severe"]
OUTCOMES = ["recovered", "recovering", "not recovered", "recovered with sequelae"]
CASE_STATUS = ["open", "under review", "awaiting narrative", "closed"]
SITES = ["SITE-101", "SITE-102", "SITE-205", "SITE-206", "SITE-310", "SITE-402"]

SUPPLIER_CATEGORIES = [
    "cold chain logistics", "comparator sourcing", "central laboratory",
    "packaging and labelling", "ancillary supplies", "courier services",
]


def build() -> dict[str, list[dict]]:
    rng = random.Random(SEED)

    protocols = [
        {
            "doc_id": f"HTX-{101 + i}",
            "study": "HTX-204",
            "section": section,
            "title": f"{section} - clause {i + 1}",
            "text": text,
            "classification": "internal",
        }
        for i, (section, text) in enumerate(PROTOCOLS)
    ]

    cases = []
    for i in range(1, 31):
        term = rng.choice(EVENT_TERMS)
        severity = rng.choice(SEVERITIES)
        cases.append(
            {
                "case_id": f"AE-{i:04d}",
                "study": "HTX-204",
                # Pseudonymous subject codes only. No names, no dates of birth.
                "subject_id": f"SUBJ-{rng.randint(1000, 9999)}",
                "site": rng.choice(SITES),
                "event_term": term,
                "severity": severity,
                "serious": severity == "severe" and rng.random() < 0.6,
                "outcome": rng.choice(OUTCOMES),
                "status": rng.choice(CASE_STATUS),
                "narrative": (
                    f"Participant reported {term} of {severity} severity on study day "
                    f"{rng.randint(3, 180)}. Study drug was "
                    f"{rng.choice(['continued', 'interrupted', 'reduced'])}."
                ),
                "classification": "phi",
            }
        )

    suppliers = [
        {
            "supplier_id": f"SUP-{200 + i}",
            "name": f"Vendor {chr(ord('A') + i)} Clinical Services",
            "category": SUPPLIER_CATEGORIES[i % len(SUPPLIER_CATEGORIES)],
            "region": rng.choice(["EMEA", "NA", "APAC"]),
            "approved": i % 5 != 4,
            "classification": "financial",
        }
        for i in range(15)
    ]

    return {"protocols": protocols, "cases": cases, "suppliers": suppliers}


def write(target: Path = DATA_DIR) -> dict[str, Path]:
    target.mkdir(parents=True, exist_ok=True)
    written = {}
    for name, rows in build().items():
        path = target / f"{name}.json"
        path.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        written[name] = path
    return written


if __name__ == "__main__":
    for name, path in write().items():
        rows = json.loads(path.read_text(encoding="utf-8"))
        print(f"  {name:<10} {len(rows):>3} records -> {path}")
