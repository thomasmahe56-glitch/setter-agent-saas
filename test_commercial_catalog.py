from commercial_catalog import build_public_catalog, safe_catalog
import asyncio

import main


def _plans(*ids: str) -> list[dict]:
    terms = {
        "founding": (99, 5000, ["prospecting", "setter"]),
        "setter": (149, 5000, ["setter"]),
        "prospecting": (199, 10000, ["prospecting"]),
        "complete": (299, 15000, ["prospecting", "setter"]),
    }
    return [
        {
            "offer_id": offer_id,
            "price_usd": terms[offer_id][0],
            "monthly_credits": terms[offer_id][1],
            "modules": terms[offer_id][2],
            "active": True,
        }
        for offer_id in ids
    ]


def test_missing_catalog_state_fails_closed() -> None:
    assert build_public_catalog([], _plans("founding"), conversion_enabled=True) == safe_catalog()


def test_public_catalog_uses_exact_active_terms_and_explicit_release_flag() -> None:
    catalog = build_public_catalog(
        [{"version": "brief-2026-09-15", "phase": "public", "founding_availability": "closed", "conversion_ready": True}],
        _plans("setter", "prospecting", "complete"),
        conversion_enabled=False,
    )
    assert [offer["id"] for offer in catalog["offers"]] == ["setter", "prospecting", "complete"]
    assert catalog["conversionReady"] is False


def test_full_founding_never_returns_a_purchasable_offer() -> None:
    catalog = build_public_catalog(
        [{"version": "brief-2026-09-15", "phase": "founding_open", "founding_availability": "full", "conversion_ready": True}],
        _plans("founding"),
        conversion_enabled=True,
    )
    assert catalog["offers"] == []
    assert catalog["conversionReady"] is True


def test_bad_terms_fail_closed() -> None:
    plans = _plans("founding")
    plans[0]["monthly_credits"] = 15000
    assert build_public_catalog(
        [{"version": "brief-2026-09-15", "phase": "founding_open", "founding_availability": "open", "conversion_ready": False}],
        plans,
        conversion_enabled=False,
    ) == safe_catalog()


def test_duplicate_active_plan_version_fails_closed() -> None:
    plans = _plans("founding", "founding")
    assert build_public_catalog(
        [{"version": "brief-2026-09-15", "phase": "founding_open", "founding_availability": "open", "conversion_ready": False}],
        plans,
        conversion_enabled=False,
    ) == safe_catalog()


def test_catalog_route_selects_only_aggregate_catalog_data(monkeypatch) -> None:
    class FakeResponse:
        status_code = 200

        def __init__(self, payload: list[dict]) -> None:
            self.payload = payload

        def json(self) -> list[dict]:
            return self.payload

    calls: list[tuple[str, dict]] = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url: str, **kwargs):
            calls.append((url, kwargs["params"]))
            if url == main.SUPABASE_COMMERCIAL_CATALOG_STATE_URL:
                return FakeResponse([
                    {"version": "brief-2026-09-15", "phase": "public", "founding_availability": "closed", "conversion_ready": True}
                ])
            return FakeResponse(_plans("setter", "prospecting", "complete"))

    monkeypatch.setattr(main.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(main, "SUPABASE_SERVICE_KEY", "test-service-key")
    monkeypatch.setattr(main.config, "supabase_url", "https://supabase.test/rest/v1")
    monkeypatch.setattr(main.config, "commercial_conversion_enabled", False)
    response = asyncio.run(main.commercial_catalog())

    assert [offer["id"] for offer in response["offers"]] == ["setter", "prospecting", "complete"]
    assert response["conversionReady"] is False
    assert calls == [
        (main.SUPABASE_COMMERCIAL_CATALOG_STATE_URL, {"select": "version,phase,founding_availability,conversion_ready", "limit": "2"}),
        (main.SUPABASE_COMMERCIAL_PLAN_VERSIONS_URL, {"select": "offer_id,price_usd,monthly_credits,modules,active", "active": "eq.true"}),
    ]
