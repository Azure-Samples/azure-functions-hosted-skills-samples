"""Deterministic comparison of extracted contract terms with Contoso's playbook.

The model reads the contract text and extracts the terms; this tool compares them with
the playbook thresholds and computes the risk score in code, so the score is the same
for the same terms on every run.

Do not add ``from __future__ import annotations`` here: the runtime loads tool
files dynamically and pydantic must resolve the model's types at import time.
"""

from typing import Any, Literal

from _sample_data import load
from pydantic import BaseModel, Field


class ContractTerms(BaseModel):
    contract_type: str = Field(description="contract_type from the contract, for example saas_subscription.")
    liability_cap_months: float | None = Field(
        default=None,
        description="Vendor liability cap expressed in months of fees (12 for 'fees paid in the prior 12 months'). Null if no cap clause.",
    )
    liability_cap_covers_data_breach: bool | None = Field(
        default=None,
        description="True if the liability cap also limits data protection breaches or indemnified claims (no carve-out).",
    )
    customer_liability_uncapped: bool | None = Field(
        default=None, description="True if Contoso's liability is uncapped while the vendor's is capped."
    )
    payment_days: int | None = Field(default=None, description="Days Contoso has to pay an invoice (net N). Null if not stated.")
    termination_for_convenience: bool | None = Field(
        default=None, description="True if Contoso may terminate for convenience. Null if there is no termination clause."
    )
    termination_notice_days: int | None = Field(default=None, description="Notice days required to terminate for convenience.")
    auto_renews: bool | None = Field(default=None, description="True if the contract renews automatically. Null if not stated.")
    renewal_notice_days: int | None = Field(default=None, description="Days of notice Contoso must give to stop a renewal.")
    renewal_price_increase_pct: float | None = Field(
        default=None, description="Maximum price increase per renewal in percent. Null if uncapped or not stated."
    )
    indemnity: Literal["mutual", "vendor_only", "customer_only", "none"] | None = Field(
        default=None, description="Who indemnifies whom. Null if there is no indemnification clause."
    )
    governing_law: str | None = Field(default=None, description="Governing law jurisdiction, for example Washington.")
    processes_personal_data: bool | None = Field(default=None, description="True if the vendor processes Contoso personal data.")
    dpa_included: bool | None = Field(default=None, description="True if a data processing addendum (DPA) is incorporated.")
    breach_notification_hours: int | None = Field(
        default=None, description="Hours the vendor has to notify Contoso of a data breach. Null if not stated."
    )


Status = Literal["standard", "acceptable", "non_standard", "missing"]


def _finding(clause: str, status: Status, detail: str) -> dict[str, Any]:
    return {"clause": clause, "status": status, "detail": detail}


def _band_min(value: float, limits: dict[str, float]) -> Status:
    if value >= limits["standard_min"]:
        return "standard"
    return "acceptable" if value >= limits["acceptable_min"] else "non_standard"


def _band_max(value: float, limits: dict[str, float]) -> Status:
    if value <= limits["standard_max"]:
        return "standard"
    return "acceptable" if value <= limits["acceptable_max"] else "non_standard"


def _worst(*statuses: Status) -> Status:
    order = ["standard", "acceptable", "non_standard", "missing"]
    return max(statuses, key=order.index)


def _evaluate(clause: str, terms: ContractTerms, limits: dict[str, Any]) -> dict[str, Any]:
    if clause == "limitation_of_liability":
        if terms.liability_cap_months is None and not terms.customer_liability_uncapped:
            return _finding(clause, "missing", "No limitation of liability terms were extracted.")
        status = _band_min(terms.liability_cap_months or 0, limits["liability_cap_months"])
        if terms.liability_cap_covers_data_breach or terms.customer_liability_uncapped:
            status = "non_standard"
        return _finding(
            clause, status,
            f"Vendor cap {terms.liability_cap_months} months of fees; covers data breach: {terms.liability_cap_covers_data_breach}; "
            f"Contoso uncapped: {terms.customer_liability_uncapped}.",
        )
    if clause == "payment_terms":
        if terms.payment_days is None:
            return _finding(clause, "missing", "No payment terms were extracted.")
        return _finding(clause, _band_min(terms.payment_days, limits["payment_days"]), f"Net {terms.payment_days} days.")
    if clause == "termination":
        if terms.termination_for_convenience is None:
            return _finding(clause, "missing", "No termination terms were extracted.")
        if not terms.termination_for_convenience:
            return _finding(clause, "non_standard", "Contoso cannot terminate for convenience.")
        notice = terms.termination_notice_days if terms.termination_notice_days is not None else 0
        return _finding(clause, _band_max(notice, limits["termination_notice_days"]), f"Termination for convenience on {notice} days' notice.")
    if clause == "auto_renewal":
        if terms.auto_renews is None:
            return _finding(clause, "missing", "No renewal terms were extracted.")
        if not terms.auto_renews:
            return _finding(clause, "standard", "No automatic renewal.")
        notice = _band_max(terms.renewal_notice_days or 0, limits["renewal_notice_days"])
        price: Status = (
            "non_standard" if terms.renewal_price_increase_pct is None
            else _band_max(terms.renewal_price_increase_pct, limits["renewal_price_increase_pct"])
        )
        increase = "uncapped" if terms.renewal_price_increase_pct is None else f"{terms.renewal_price_increase_pct}%"
        return _finding(
            clause, _worst(notice, price),
            f"Auto-renews; cancel with {terms.renewal_notice_days} days' notice; renewal price increase {increase}.",
        )
    if clause == "indemnification":
        if terms.indemnity is None:
            return _finding(clause, "missing", "No indemnification terms were extracted.")
        status = {"mutual": "standard", "vendor_only": "acceptable"}.get(terms.indemnity, "non_standard")
        return _finding(clause, status, f"Indemnity: {terms.indemnity}.")
    if clause == "governing_law":
        if not terms.governing_law:
            return _finding(clause, "missing", "No governing law was extracted.")
        law = terms.governing_law.strip().lower().removeprefix("state of ").strip()
        allowed = limits["governing_law"]
        status = "standard" if law in allowed["standard"] else "acceptable" if law in allowed["acceptable"] else "non_standard"
        return _finding(clause, status, f"Governed by {terms.governing_law} law.")
    if clause == "data_protection":
        if terms.processes_personal_data is False:
            return _finding(clause, "standard", "The vendor does not process Contoso personal data.")
        if terms.processes_personal_data is None and terms.dpa_included is None:
            return _finding(clause, "missing", "No data protection terms were extracted.")
        if not terms.dpa_included:
            return _finding(clause, "non_standard", "Personal data is processed without a data processing addendum.")
        if terms.breach_notification_hours is None:
            return _finding(clause, "non_standard", "DPA included but no breach notification commitment.")
        return _finding(
            clause, _band_max(terms.breach_notification_hours, limits["breach_notification_hours"]),
            f"DPA included; breach notification within {terms.breach_notification_hours} hours.",
        )
    return _finding(clause, "missing", f"No rule for clause {clause}.")


def check_contract_terms(terms: ContractTerms) -> dict[str, Any]:
    """Compare the terms extracted from a contract with Contoso's playbook thresholds. Returns each clause's status (standard, acceptable, non_standard, missing), the deterministic risk score (0-100), and the risk level."""
    playbook = load("clause_playbook")
    contract = playbook["contract_types"].get(terms.contract_type.strip().lower())
    if contract is None:
        return {"error": f"No playbook for contract type {terms.contract_type!r}.", "known_contract_types": sorted(playbook["contract_types"])}

    scoring = playbook["scoring"]
    findings = []
    score = 0
    for clause in contract["required_clauses"]:
        finding = _evaluate(clause, terms, playbook["thresholds"])
        severity = playbook["clauses"][clause]["severity"]
        if finding["status"] == "acceptable":
            points = scoring["acceptable_points"]
        elif finding["status"] == "non_standard":
            points = scoring["non_standard_points"][severity]
        elif finding["status"] == "missing":
            points = scoring["missing_clause_points"]
        else:
            points = 0
        findings.append({**finding, "severity": severity, "risk_points": points})
        score += points

    score = min(score, 100)
    level = "low"
    for name, floor in sorted(scoring["risk_levels"].items(), key=lambda item: item[1]):
        if score >= floor:
            level = name
    return {
        "contract_type": terms.contract_type,
        "findings": findings,
        "non_standard_clauses": [f["clause"] for f in findings if f["status"] in ("non_standard", "missing")],
        "risk_score": score,
        "risk_level": level,
    }
