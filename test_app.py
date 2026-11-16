"""Tests for London Recycling. The build runs these on every push.

The network is blocked during tests (conftest.py), so every test replaces the
one method that does HTTP, `App._fetch`, with a stub that returns payloads
shaped like the real ones from postcodes.io and Nominatim (captured November
2026).
"""

from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timezone

import pytest
from pippa_agentapp_sdk import LocalRunner, OutputShapeViolation

import app as app_module
from app import App, NoLocation, NotInLondon, PlaceNotFound

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)

# ── Payloads shaped like the real APIs ──────────────────────────────────────

POSTCODE_SE15 = {
    "status": 200,
    "result": {
        "postcode": "SE15 1AL", "latitude": 51.4795, "longitude": -0.0640,
        "admin_district": "Southwark", "region": "London", "country": "England",
    },
}
POSTCODE_MANCHESTER = {
    "status": 200,
    "result": {
        "postcode": "M1 1AE", "latitude": 53.4794, "longitude": -2.2453,
        "admin_district": "Manchester", "region": "North West", "country": "England",
    },
}
OUTCODE_E8 = {
    "status": 200,
    "result": {
        "outcode": "E8", "latitude": 51.54379, "longitude": -0.06611,
        "admin_district": ["Hackney", "Islington", "Tower Hamlets"],
    },
}
NOMINATIM_CLAPHAM = [{
    "lat": "51.4614469", "lon": "-0.1386270", "name": "Clapham",
    "address": {
        "suburb": "Clapham", "city_district": "London Borough of Lambeth",
        "city": "Greater London", "postcode": "SW4 7AJ", "country_code": "gb",
    },
}]
NOMINATIM_KENT = [{
    "lat": "51.2844947", "lon": "0.3333032", "name": "Narnia",
    "address": {"county": "Kent", "state": "England", "country_code": "gb"},
}]
REVERSE_LAMBETH = {"status": 200, "result": [{"postcode": "SW4 7AJ", "admin_district": "Lambeth"}]}


def stub(routes: dict):
    """A fake `_fetch` answering by the first route whose key is in the URL.

    A value that is an exception is raised; anything else is returned as JSON.
    """
    def fetch(url, timeout=6.0):
        for fragment, answer in routes.items():
            if fragment in url:
                if isinstance(answer, BaseException):
                    raise answer
                return json.dumps(answer).encode()
        raise AssertionError(f"unexpected request: {url}")
    return fetch


def run(outcome: str, inputs: dict, routes: dict, *, explicit: bool = False):
    instance = App()
    instance._fetch = stub(routes)
    return LocalRunner(instance).call(
        outcome, inputs, granted_secrets={}, now=NOW, request_id="t1",
        explicit=explicit,
    ).result


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://example.com", code, "error", {}, None)


# ── The manifest and the data files ─────────────────────────────────────────


def test_the_manifest_is_valid():
    instance = App()
    assert instance.manifest.app_id == "hack.carl_public"
    assert [o.id for o in instance.manifest.outcomes] == [
        "recycling.nearest_centre", "recycling.collection_day",
    ]


def test_every_centre_is_in_a_known_borough():
    assert len(app_module.CENTRES) >= 30
    for centre in app_module.CENTRES:
        assert centre["borough"] in app_module.COUNCILS, centre["name"]
        assert 51.2 < centre["lat"] < 51.8 and -0.6 < centre["lon"] < 0.4, centre["name"]


def test_there_is_a_council_link_for_all_33_authorities():
    assert len(app_module.COUNCILS) == 33
    assert all(url.startswith("https://") for url in app_module.COUNCILS.values())


# ── Nearest centre ──────────────────────────────────────────────────────────


def test_a_full_postcode_finds_the_centre_on_its_doorstep():
    result = run("recycling.nearest_centre", {"location": "se15 1al"},
                 {"postcodes.io/postcodes/SE151AL": POSTCODE_SE15})
    assert result.data["centre_name"] == "Southwark Reuse and Recycling Centre"
    assert result.data["centre_borough"] == "Southwark"
    assert result.data["distance_km"] < 1
    assert result.data["location_used"] == "SE15 1AL"
    assert len(result.data["other_nearby"]) == 2
    assert "openstreetmap.org" in result.data["map_link"]


def test_a_place_name_is_geocoded():
    result = run("recycling.nearest_centre", {"location": "Clapham"},
                 {"nominatim.openstreetmap.org/search": NOMINATIM_CLAPHAM})
    assert result.data["location_used"] == "Clapham"
    assert result.data["distance_km"] < 6
    assert "Clapham" in result.narrative


def test_a_postcode_district_works():
    result = run("recycling.nearest_centre", {"location": "E8"},
                 {"postcodes.io/outcodes/E8": OUTCODE_E8})
    assert result.data["location_used"] == "E8"
    assert result.data["distance_km"] < 8


def test_no_location_says_what_to_type():
    with pytest.raises(NoLocation) as failure:
        run("recycling.nearest_centre", {"location": ""}, {})
    assert "postcode" in failure.value.user_message


def test_a_missing_location_input_is_the_same_as_blank():
    with pytest.raises(NoLocation):
        run("recycling.nearest_centre", {}, {})


def test_a_postcode_outside_london_is_refused_clearly():
    with pytest.raises(NotInLondon) as failure:
        run("recycling.nearest_centre", {"location": "M1 1AE"},
            {"postcodes.io/postcodes/M11AE": POSTCODE_MANCHESTER})
    assert "outside Greater London" in failure.value.user_message


def test_a_place_found_outside_london_is_refused():
    with pytest.raises(NotInLondon):
        run("recycling.nearest_centre", {"location": "Narnia"},
            {"nominatim.openstreetmap.org/search": NOMINATIM_KENT})


def test_a_town_elsewhere_is_not_mistaken_for_a_london_road():
    """'Brighton' must not become Brighton Road in Croydon."""
    brighton = [
        {"lat": "50.82", "lon": "-0.14", "name": "Brighton", "category": "place",
         "address": {"city": "Brighton", "county": "Brighton and Hove"}},
        {"lat": "51.34", "lon": "-0.11", "name": "Brighton Road", "category": "highway",
         "address": {"city": "Greater London", "city_district": "London Borough of Croydon"}},
    ]
    with pytest.raises(NotInLondon):
        run("recycling.nearest_centre", {"location": "Brighton"},
            {"nominatim.openstreetmap.org/search": brighton})


def test_westminster_counts_as_london():
    """Nominatim names the City of Westminster for itself, not Greater London."""
    westminster = [{"lat": "51.4973", "lon": "-0.1372", "name": "Westminster",
                    "category": "place", "address": {"city": "City of Westminster"}}]
    result = run("recycling.collection_day", {"location": "Westminster"},
                 {"nominatim.openstreetmap.org/search": westminster})
    assert result.data["borough"] == "Westminster"


def test_an_unknown_place_is_named_back_not_crashed_on():
    """Nominatim answers 200 with [] for a place it does not know."""
    with pytest.raises(PlaceNotFound) as failure:
        run("recycling.nearest_centre", {"location": "Atlantis"},
            {"nominatim.openstreetmap.org/search": []})
    assert "Atlantis" in failure.value.user_message


def test_an_unknown_postcode_is_named_back():
    """postcodes.io answers 404 for a postcode that does not exist."""
    with pytest.raises(PlaceNotFound) as failure:
        run("recycling.nearest_centre", {"location": "ZZ99 9ZZ"},
            {"postcodes.io/postcodes/ZZ999ZZ": http_error(404)})
    assert "ZZ99 9ZZ" in failure.value.user_message


def test_a_rate_limit_is_retryable():
    from pippa_agentapp_sdk import RateLimitedError

    with pytest.raises(RateLimitedError) as failure:
        run("recycling.nearest_centre", {"location": "Clapham"},
            {"nominatim.openstreetmap.org/search": http_error(429)})
    assert failure.value.retryable is True


def test_a_timeout_is_caught():
    from pippa_agentapp_sdk import ServiceUnavailableError

    with pytest.raises(ServiceUnavailableError):
        run("recycling.nearest_centre", {"location": "SE15 1AL"},
            {"postcodes.io": TimeoutError("timed out")})


def test_a_server_error_is_service_unavailable():
    from pippa_agentapp_sdk import ServiceUnavailableError

    with pytest.raises(ServiceUnavailableError):
        run("recycling.nearest_centre", {"location": "SE15 1AL"},
            {"postcodes.io": http_error(503)})


# ── Collection day ──────────────────────────────────────────────────────────


def test_a_postcode_gives_its_council_and_the_bin_day_finder():
    result = run("recycling.collection_day", {"location": "SE15 1AL"},
                 {"postcodes.io/postcodes/SE151AL": POSTCODE_SE15})
    assert result.data["borough"] == "Southwark"
    assert result.data["council_website"].startswith("https://www.southwark.gov.uk")
    assert result.data["bin_day_finder"] == "https://www.gov.uk/rubbish-collection-day"
    assert result.data["other_possible_boroughs"] == []


def test_a_district_spanning_boroughs_says_so():
    result = run("recycling.collection_day", {"location": "e8"},
                 {"postcodes.io/outcodes/E8": OUTCODE_E8})
    assert result.data["borough"] == "Hackney"
    assert result.data["other_possible_boroughs"] == ["Islington", "Tower Hamlets"]
    assert "full postcode" in result.narrative


def test_a_place_name_gives_the_borough_from_nominatim():
    result = run("recycling.collection_day", {"location": "Clapham"},
                 {"nominatim.openstreetmap.org/search": NOMINATIM_CLAPHAM})
    assert result.data["borough"] == "Lambeth"


def test_a_place_without_a_borough_falls_back_to_reverse_lookup():
    no_district = [dict(NOMINATIM_CLAPHAM[0], address={"city": "Greater London"})]
    result = run("recycling.collection_day", {"location": "Clapham"}, {
        "nominatim.openstreetmap.org/search": no_district,
        "postcodes.io/postcodes?": REVERSE_LAMBETH,
    })
    assert result.data["borough"] == "Lambeth"


def test_borough_names_are_tidied():
    assert app_module._tidy_borough("Royal Borough of Kensington and Chelsea") == "Kensington and Chelsea"
    assert app_module._tidy_borough("London Borough of Richmond upon Thames") == "Richmond upon Thames"
    assert app_module._tidy_borough("City of Westminster") == "Westminster"
    assert app_module._tidy_borough("City of London") == "City of London"


# ── Picked with @ ───────────────────────────────────────────────────────────


def test_picked_by_name_it_answers_and_refuses_as_usual():
    routes = {"postcodes.io/postcodes/SE151AL": POSTCODE_SE15}
    picked = run("recycling.nearest_centre", {"location": "SE15 1AL"}, routes, explicit=True)
    assert picked.data == run("recycling.nearest_centre", {"location": "SE15 1AL"}, routes).data

    with pytest.raises(NoLocation):
        run("recycling.collection_day", {"location": ""}, {}, explicit=True)


def test_the_runner_enforces_the_manifest(monkeypatch):
    from pippa_agentapp_sdk import Result

    monkeypatch.setattr(App, "handle", lambda self, c: Result(data={"surprise": "hello"}))
    with pytest.raises(OutputShapeViolation):
        run("recycling.nearest_centre", {"location": "SE15 1AL"}, {})
