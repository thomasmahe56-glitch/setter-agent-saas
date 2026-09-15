"""Public, aggregate-only representation of Angellos commercial state.

This module intentionally knows nothing about customers, reservations, trials or
payments. Those records must never be exposed by the marketing catalog route.
"""

from __future__ import annotations

from typing import Any


CATALOG_VERSION_FALLBACK = "commercial-unconfigured"
PHASES = {"private_beta", "founding_open", "founding_closed", "public"}
AVAILABILITY = {"open", "reserved", "full", "closed", "unavailable"}
OFFERS = {
    "founding": {
        "priceUsd": 99,
        "monthlyCredits": 5000,
        "modules": ["prospecting", "setter"],
    },
    "setter": {
        "priceUsd": 149,
        "monthlyCredits": 5000,
        "modules": ["setter"],
    },
    "prospecting": {
        "priceUsd": 199,
        "monthlyCredits": 10000,
        "modules": ["prospecting"],
    },
    "complete": {
        "priceUsd": 299,
        "monthlyCredits": 15000,
        "modules": ["prospecting", "setter"],
    },
}
PHASE_OFFERS = {
    "private_beta": [],
    "founding_open": ["founding"],
    "founding_closed": [],
    "public": ["setter", "prospecting", "complete"],
}


def safe_catalog() -> dict[str, Any]:
    """Return the non-convertible state used for missing or invalid data."""
    return {
        "version": CATALOG_VERSION_FALLBACK,
        "phase": "private_beta",
        "availability": "unavailable",
        "offers": [],
        "conversionReady": False,
    }


def _matching_active_offer(row: dict[str, Any], offer_id: str) -> bool:
    expected = OFFERS[offer_id]
    try:
        price = float(row.get("price_usd"))
    except (TypeError, ValueError):
        return False
    return (
        row.get("offer_id") == offer_id
        and row.get("active") is True
        and price == expected["priceUsd"]
        and row.get("monthly_credits") == expected["monthlyCredits"]
        and row.get("modules") == expected["modules"]
    )


def build_public_catalog(
    state_rows: list[dict[str, Any]],
    plan_rows: list[dict[str, Any]],
    *,
    conversion_enabled: bool,
) -> dict[str, Any]:
    """Validate database rows before returning a catalog to anonymous visitors."""
    if len(state_rows) != 1:
        return safe_catalog()
    state = state_rows[0]
    phase = state.get("phase")
    availability = state.get("founding_availability")
    version = state.get("version")
    if (
        phase not in PHASES
        or availability not in AVAILABILITY
        or not isinstance(version, str)
        or not version.strip()
    ):
        return safe_catalog()

    required_offer_ids = PHASE_OFFERS[phase]
    active_by_id: dict[str, list[dict[str, Any]]] = {}
    for row in plan_rows:
        if isinstance(row, dict) and isinstance(row.get("offer_id"), str):
            active_by_id.setdefault(row["offer_id"], []).append(row)
    if any(
        len(active_by_id.get(offer_id, [])) != 1
        or not _matching_active_offer(active_by_id[offer_id][0], offer_id)
        for offer_id in required_offer_ids
    ):
        return safe_catalog()

    # A full Founding cohort must not be rendered as a purchasable offer.
    rendered_offer_ids = [] if phase == "founding_open" and availability == "full" else required_offer_ids
    return {
        "version": version,
        "phase": phase,
        "availability": availability,
        "offers": [{"id": offer_id, **OFFERS[offer_id]} for offer_id in rendered_offer_ids],
        "conversionReady": bool(state.get("conversion_ready")) and conversion_enabled,
    }
