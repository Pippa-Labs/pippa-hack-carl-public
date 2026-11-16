"""London Recycling: the nearest reuse and recycling centre, and who collects
your recycling, for anywhere in Greater London.

Two outcomes:

* ``recycling.nearest_centre`` — the closest household reuse and recycling
  centres ("the tip") to a London postcode or place, with distances.
* ``recycling.collection_day`` — which borough council collects recycling at
  that address, and where to look up the exact collection day.

Locations are resolved with two free, keyless services:

* postcodes.io for UK postcodes (full ones like "E8 1EA" or districts like
  "E8"), and for turning coordinates back into a borough;
* Nominatim (OpenStreetMap) for place names like "Clapham" or "Brixton".

The list of centres (``centres.json``) and the council links
(``councils.json``) are data files shipped beside this module, so finding the
nearest centre is arithmetic, not another network call.

There is no public, London-wide API for collection days: every council runs
its own address lookup. So the collection outcome says which council it is and
links to that council's page and to GOV.UK's official bin-day finder, rather
than inventing a day.
"""

from __future__ import annotations

import json
import math
import re
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlencode

from pippa_agentapp_sdk import (
    AgentAppError,
    Call,
    Manifest,
    RateLimitedError,
    Result,
    ServiceUnavailableError,
    load_manifest_file,
    validate_manifest,
)

HERE = Path(__file__).resolve().parent
MANIFEST_PATH = HERE / "manifest.yaml"
CENTRES = json.loads((HERE / "centres.json").read_text("utf-8"))
COUNCILS: dict[str, str] = json.loads((HERE / "councils.json").read_text("utf-8"))

#: Nominatim answers 403 to a request without a descriptive User-Agent.
USER_AGENT = (
    "pippa-agent-app-london-recycling/0.1 "
    "(https://github.com/Pippa-Labs/pippa-hack-carl-public)"
)

#: GOV.UK's official "find your bin collection day" service. It takes a
#: postcode and sends the person to the right council's own lookup.
GOV_UK_BIN_DAY = "https://www.gov.uk/rubbish-collection-day"

#: A full UK postcode ("SW1A 1AA", "e81ea") or just its district ("E8", "SW11").
FULL_POSTCODE = re.compile(r"^[A-Z]{1,2}[0-9][A-Z0-9]?\s*[0-9][A-Z]{2}$")
OUTCODE = re.compile(r"^[A-Z]{1,2}[0-9][A-Z0-9]?$")

#: Rough box around Greater London, for steering Nominatim. The answer is
#: still checked afterwards, because a bounded search can land just outside.
LONDON_VIEWBOX = "-0.52,51.70,0.34,51.28"

HOW_MANY_NEARBY = 3


# ── Errors about this app's own subject ─────────────────────────────────────


class NoLocation(AgentAppError):
    user_message = (
        "Tell me a London postcode or area, for example "
        "\"E8 1EA\" or \"Clapham\", and I will find the nearest recycling centre."
    )
    retryable = False


class PlaceNotFound(AgentAppError):
    user_message = "I could not find that place in London."
    retryable = False


class NotInLondon(AgentAppError):
    user_message = "That is outside Greater London, and I only cover the London boroughs."
    retryable = False


# ── The app ─────────────────────────────────────────────────────────────────


class App:
    def __init__(self) -> None:
        self._manifest = validate_manifest(load_manifest_file(MANIFEST_PATH))

    @property
    def manifest(self) -> Manifest:
        return self._manifest

    def handle(self, call: Call) -> Result:
        where = (call.inputs.get("location") or "").strip()
        if not where:
            raise NoLocation("no location in the message")

        if call.outcome_id == "recycling.nearest_centre":
            return self._nearest_centre(where)
        if call.outcome_id == "recycling.collection_day":
            return self._collection_day(where)
        raise AgentAppError(f"this app cannot do {call.outcome_id!r}")

    # ── Outcomes ────────────────────────────────────────────────────────────

    def _nearest_centre(self, where: str) -> Result:
        spot = self._locate(where, need_borough=False)
        ranked = sorted(
            CENTRES,
            key=lambda c: _km(spot["lat"], spot["lon"], c["lat"], c["lon"]),
        )
        nearest = ranked[0]
        distance = round(_km(spot["lat"], spot["lon"], nearest["lat"], nearest["lon"]), 1)

        others = []
        for centre in ranked[1:HOW_MANY_NEARBY]:
            km = _km(spot["lat"], spot["lon"], centre["lat"], centre["lon"])
            others.append(f"{centre['name']} ({centre['borough']}), {km:.1f} km")

        map_link = (
            "https://www.openstreetmap.org/?"
            + urlencode({"mlat": nearest["lat"], "mlon": nearest["lon"]})
            + "#map=17/{:.5f}/{:.5f}".format(nearest["lat"], nearest["lon"])
        )
        data = {
            "location_used": spot["label"],
            "centre_name": nearest["name"],
            "centre_borough": nearest["borough"],
            "distance_km": distance,
            "map_link": map_link,
            "other_nearby": others,
        }
        if nearest.get("postcode"):
            data["centre_postcode"] = nearest["postcode"]

        where_text = f" ({nearest['postcode']})" if nearest.get("postcode") else ""
        narrative = (
            f"The nearest London recycling centre to {spot['label']} is "
            f"{nearest['name']}{where_text} in {nearest['borough']}, about "
            f"{distance} km away. Most London centres are for residents of the "
            f"boroughs that run them and some need booking, so check the "
            f"council's website before you go."
        )
        return Result(data=data, narrative=narrative)

    def _collection_day(self, where: str) -> Result:
        spot = self._locate(where, need_borough=True)
        borough = spot["borough"]
        council_site = COUNCILS.get(borough, GOV_UK_BIN_DAY)

        data = {
            "location_used": spot["label"],
            "borough": borough,
            "council_website": council_site,
            "bin_day_finder": GOV_UK_BIN_DAY,
            "other_possible_boroughs": spot.get("other_boroughs", []),
        }

        narrative = (
            f"Recycling at {spot['label']} is collected by {borough} council. "
            f"Collection days are set street by street, so enter your full "
            f"postcode at {GOV_UK_BIN_DAY} (or on {council_site}) to see yours."
        )
        if spot.get("other_boroughs"):
            narrative += (
                f" {spot['label']} also covers parts of "
                f"{', '.join(spot['other_boroughs'])}, so a full postcode "
                f"will say for certain."
            )
        return Result(data=data, narrative=narrative)

    # ── Finding where somebody means ────────────────────────────────────────

    def _locate(self, where: str, *, need_borough: bool) -> dict:
        """Turn a postcode or place into {lat, lon, label, borough, ...}."""
        compact = where.upper().replace(" ", "")
        if FULL_POSTCODE.match(compact):
            return self._from_postcode(where, compact)
        if OUTCODE.match(compact):
            return self._from_outcode(where, compact)
        return self._from_place(where, need_borough=need_borough)

    def _from_postcode(self, where: str, compact: str) -> dict:
        url = f"https://api.postcodes.io/postcodes/{quote(compact, safe='')}"
        body = self._get_json(url, where, not_found=f"I could not find the postcode {where.upper()}.")
        result = body.get("result") if isinstance(body, dict) else None
        if not result or result.get("latitude") is None:
            raise _not_found(where, f"I could not find the postcode {where.upper()}.")
        if result.get("region") != "London":
            raise _outside(where)
        return {
            "lat": result["latitude"],
            "lon": result["longitude"],
            "label": result.get("postcode") or where.upper(),
            "borough": _tidy_borough(result.get("admin_district") or ""),
        }

    def _from_outcode(self, where: str, compact: str) -> dict:
        url = f"https://api.postcodes.io/outcodes/{quote(compact, safe='')}"
        body = self._get_json(url, where, not_found=f"I could not find the postcode area {compact}.")
        result = body.get("result") if isinstance(body, dict) else None
        if not result or result.get("latitude") is None:
            raise _not_found(where, f"I could not find the postcode area {compact}.")
        boroughs = [_tidy_borough(b) for b in (result.get("admin_district") or [])]
        london = [b for b in boroughs if b in COUNCILS]
        if not london:
            raise _outside(where)
        return {
            "lat": result["latitude"],
            "lon": result["longitude"],
            "label": compact,
            "borough": london[0],
            "other_boroughs": london[1:],
        }

    def _from_place(self, where: str, *, need_borough: bool) -> dict:
        # Not bounded to London on purpose: bounded, "Brighton" finds Brighton
        # Road in Croydon. Unbounded (London only preferred), it finds
        # Brighton, and the answer is honestly "that is not in London".
        query = urlencode({
            "q": where,
            "format": "jsonv2",
            "countrycodes": "gb",
            "limit": 5,
            "addressdetails": 1,
            "viewbox": LONDON_VIEWBOX,
        })
        found = self._get_json(
            "https://nominatim.openstreetmap.org/search?" + query, where,
            not_found=f"I could not find {where} in London.",
        )
        # Nominatim answers 200 with [] for a place it does not know.
        if not isinstance(found, list) or not found:
            raise _not_found(where, f"I could not find {where} in London.")

        in_london = [hit for hit in found if _is_in_london(hit)]
        if not in_london:
            raise _outside(where)
        hit = in_london[0]
        # Take a London match only if it is the best match overall, or is an
        # area (a suburb, a borough) rather than a road that shares the name.
        if hit is not found[0] and hit.get("category") not in ("place", "boundary"):
            raise _outside(where)

        address = hit.get("address") or {}
        lat, lon = float(hit["lat"]), float(hit["lon"])
        label = _tidy_borough(hit.get("name") or where)
        borough = _tidy_borough(address.get("city_district") or "")
        if borough not in COUNCILS:
            borough = _tidy_borough(address.get("city") or "")
        if borough not in COUNCILS:
            borough = self._borough_at(lat, lon) if need_borough else borough
        return {"lat": lat, "lon": lon, "label": label, "borough": borough}

    def _borough_at(self, lat: float, lon: float) -> str:
        """The borough at a point, from the nearest postcode to it."""
        url = "https://api.postcodes.io/postcodes?" + urlencode(
            {"lat": lat, "lon": lon, "limit": 1, "radius": 1000}
        )
        body = self._get_json(url, f"{lat},{lon}", not_found="I could not work out which London borough that is.")
        results = body.get("result") if isinstance(body, dict) else None
        if not results:
            raise _not_found(f"{lat},{lon}", "I could not work out which London borough that is.")
        borough = _tidy_borough(results[0].get("admin_district") or "")
        if borough not in COUNCILS:
            raise _outside(f"{lat},{lon}")
        return borough

    # ── The network, in one place ───────────────────────────────────────────

    def _get_json(self, url: str, where: str, *, not_found: str):
        try:
            raw = self._fetch(url)
        except urllib.error.HTTPError as exc:      # MUST come first: it IS a URLError
            if exc.code in (400, 404, 422):
                raise _not_found(where, not_found)
            if exc.code == 429:
                raise RateLimitedError(f"rate limited fetching {url}: {exc.code}")
            raise ServiceUnavailableError(f"{url} returned {exc.code}")
        except TimeoutError as exc:                # NOT a URLError — its own clause
            raise ServiceUnavailableError(f"{url} did not answer in time: {exc}")
        except urllib.error.URLError as exc:       # unreachable, DNS, TLS
            raise ServiceUnavailableError(f"could not reach {url}: {exc.reason}")
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise ServiceUnavailableError(f"{url} sent something that was not JSON: {exc}")

    def _fetch(self, url: str, timeout: float = 6.0) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()


# ── Helpers ─────────────────────────────────────────────────────────────────


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line (great-circle) distance in kilometres."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _tidy_borough(name: str) -> str:
    """'London Borough of Lambeth' / 'Royal Borough of Greenwich' -> 'Lambeth'."""
    name = name.strip()
    for prefix in ("London Borough of ", "Royal Borough of ", "City of Westminster"):
        if name.startswith(prefix):
            return "Westminster" if prefix == "City of Westminster" else name[len(prefix):]
    return name


#: How Nominatim's `address.city` names places in Greater London. The two
#: cities inside it are named for themselves, not "Greater London".
LONDON_CITIES = {"Greater London", "City of Westminster", "City of London"}


def _is_in_london(hit: dict) -> bool:
    address = hit.get("address") or {}
    return (
        address.get("city") in LONDON_CITIES
        or address.get("state_district") == "Greater London"
    )


def _not_found(where: str, message: str) -> PlaceNotFound:
    error = PlaceNotFound(f"no match for {where!r}")
    error.user_message = message
    return error


def _outside(where: str) -> NotInLondon:
    error = NotInLondon(f"{where!r} is outside Greater London")
    error.user_message = (
        f"{where} is outside Greater London, and I only cover the London boroughs."
    )
    return error
