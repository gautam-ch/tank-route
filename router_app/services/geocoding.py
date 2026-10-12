"""Geocoding for user-supplied start / finish locations.

Resolution order (cheapest first):

1. ``"lat, lon"`` literal            → no lookup at all
2. Persistent disk cache             → previously resolved query
3. Offline US Census gazetteer       → "City, ST" / "City, State" inputs, ~0.01 ms
4. Nominatim (OpenStreetMap)         → anything else (addresses, landmarks, ZIPs)

Because most inputs are "City, ST", step 3 means a typical request makes
**zero** geocoding network calls, leaving OSRM as the only external API call.

Coordinates and Nominatim results must lie inside the US boundary (see
``boundary.py``). Nominatim is deliberately *not* restricted to the US:
"Toronto, ON" should resolve to Canada and be rejected, not silently match a
"Toronto Court" in Indiana.

This module also owns the place-name normalisation used by the
``geocode_stations`` management command, so station cities and user input are
matched with exactly the same rules.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.core.cache import caches
from geopy.exc import GeopyError
from geopy.geocoders import Nominatim

from .boundary import get_us_boundary
from .errors import LocationNotFoundError, UpstreamServiceError

logger = logging.getLogger(__name__)

US_STATES: dict[str, str] = {
    'AL': 'Alabama', 'AK': 'Alaska', 'AZ': 'Arizona', 'AR': 'Arkansas',
    'CA': 'California', 'CO': 'Colorado', 'CT': 'Connecticut',
    'DE': 'Delaware', 'DC': 'District of Columbia', 'FL': 'Florida',
    'GA': 'Georgia', 'HI': 'Hawaii', 'ID': 'Idaho', 'IL': 'Illinois',
    'IN': 'Indiana', 'IA': 'Iowa', 'KS': 'Kansas', 'KY': 'Kentucky',
    'LA': 'Louisiana', 'ME': 'Maine', 'MD': 'Maryland',
    'MA': 'Massachusetts', 'MI': 'Michigan', 'MN': 'Minnesota',
    'MS': 'Mississippi', 'MO': 'Missouri', 'MT': 'Montana',
    'NE': 'Nebraska', 'NV': 'Nevada', 'NH': 'New Hampshire',
    'NJ': 'New Jersey', 'NM': 'New Mexico', 'NY': 'New York',
    'NC': 'North Carolina', 'ND': 'North Dakota', 'OH': 'Ohio',
    'OK': 'Oklahoma', 'OR': 'Oregon', 'PA': 'Pennsylvania',
    'RI': 'Rhode Island', 'SC': 'South Carolina', 'SD': 'South Dakota',
    'TN': 'Tennessee', 'TX': 'Texas', 'UT': 'Utah', 'VT': 'Vermont',
    'VA': 'Virginia', 'WA': 'Washington', 'WV': 'West Virginia',
    'WI': 'Wisconsin', 'WY': 'Wyoming',
}
_STATE_NAME_TO_CODE = {name.lower(): code for code, name in US_STATES.items()}

# Word-level aliases applied to BOTH sides of every comparison, so
# "Saint Johns" == "St. Johns", "Mc Graw" == "McGraw", "S Coffeyville" ==
# "South Coffeyville", "La Place" == "Laplace".
_WORD_ALIASES = {
    'saint': 'st', 'sainte': 'ste', 'mount': 'mt', 'fort': 'ft',
    'north': 'n', 'south': 's', 'east': 'e', 'west': 'w',
}
_COUNTRY_SUFFIXES = {'usa', 'us', 'united states', 'united states of america'}
_LATLON_RE = re.compile(
    r'^\s*(-?\d{1,2}(?:\.\d+)?)\s*[,\s]\s*(-?\d{1,3}(?:\.\d+)?)\s*$'
)
_ZIP_RE = re.compile(r'\s*\d{5}(?:-\d{4})?$')


def normalize_place(name: str) -> str:
    """Return a canonical, punctuation/space-free key for a place name.

    >>> normalize_place('Saint Johns') == normalize_place('St. Johns')
    True
    """
    ascii_name = (
        unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode()
    )
    cleaned = re.sub(r'[^a-z0-9 ]', ' ', ascii_name.lower().replace('&', ' and '))
    return ''.join(_WORD_ALIASES.get(word, word) for word in cleaned.split())


def resolve_state_code(token: str) -> str | None:
    """Map 'TX', 'tx' or 'Texas' to 'TX'; return None if not a US state."""
    token = _ZIP_RE.sub('', token).strip().rstrip('.')
    if token.upper() in US_STATES:
        return token.upper()
    return _STATE_NAME_TO_CODE.get(token.lower())


@dataclass(frozen=True)
class GeocodedLocation:
    """A resolved user location."""

    query: str
    lat: float
    lon: float
    label: str
    source: str  # 'coordinates' | 'gazetteer' | 'nominatim'
    from_cache: bool = False

    @property
    def made_network_call(self) -> bool:
        """True if resolving this location hit an external geocoding API."""
        return self.source == 'nominatim' and not self.from_cache

    def as_dict(self) -> dict:
        """Serialise for the API response."""
        return {
            'query': self.query,
            'resolved_label': self.label,
            'lat': round(self.lat, 6),
            'lon': round(self.lon, 6),
            'geocoder': self.source,
        }


class Gazetteer:
    """In-memory lookup of US places: (state, normalized name) → (lat, lon).

    Built from ``data/us_places.json`` (written by ``geocode_stations``).
    """

    def __init__(self, places: dict[str, dict[str, list[float]]]) -> None:
        self._places = places

    @classmethod
    def from_file(cls, path: Path) -> 'Gazetteer':
        """Load the gazetteer JSON; an absent file yields an empty gazetteer."""
        if not Path(path).exists():
            logger.warning('Gazetteer file %s missing; run geocode_stations.', path)
            return cls({})
        with open(path, encoding='utf-8') as fh:
            return cls(json.load(fh))

    def __len__(self) -> int:
        return sum(len(v) for v in self._places.values())

    def lookup(self, city: str, state_code: str) -> tuple[float, float] | None:
        """Return (lat, lon) for a city in a state, or None."""
        hit = self._places.get(state_code, {}).get(normalize_place(city))
        return (hit[0], hit[1]) if hit else None


_gazetteer: Gazetteer | None = None
_gazetteer_lock = threading.Lock()


def get_gazetteer() -> Gazetteer:
    """Return the process-wide gazetteer singleton (lazy, thread-safe)."""
    global _gazetteer
    if _gazetteer is None:
        with _gazetteer_lock:
            if _gazetteer is None:
                _gazetteer = Gazetteer.from_file(settings.US_PLACES_JSON)
    return _gazetteer


def _parse_city_state(query: str) -> tuple[str, str] | None:
    """Split 'City, ST', 'City, State', 'City ST', 'City, ST 12345, USA'.

    Anything with more parts ('1600 Pennsylvania Ave NW, Washington, DC') is
    a street address and returns None, so it is geocoded precisely.
    """
    parts = [p.strip() for p in query.split(',') if p.strip()]
    while parts and parts[-1].lower() in _COUNTRY_SUFFIXES:
        parts.pop()
    if len(parts) == 2:
        state = resolve_state_code(parts[-1])
        return (parts[-2], state) if state else None
    if len(parts) == 1:
        tokens = _ZIP_RE.sub('', parts[0]).split()
        if len(tokens) >= 2:
            state = resolve_state_code(tokens[-1])
            if state:
                return ' '.join(tokens[:-1]), state
    return None


class LocationGeocoder:
    """Resolves free-text US locations to coordinates (see module docstring)."""

    def __init__(self) -> None:
        self._nominatim = Nominatim(
            user_agent=settings.HTTP_USER_AGENT,
            timeout=settings.GEOCODING_TIMEOUT_SECONDS,
        )

    def geocode(self, query: str) -> GeocodedLocation:
        """Resolve ``query`` or raise LocationNotFoundError / UpstreamServiceError."""
        query = ' '.join(query.split())
        if not query:
            raise LocationNotFoundError('Location must not be empty.')

        coords = _LATLON_RE.match(query)
        if coords:
            lat, lon = float(coords.group(1)), float(coords.group(2))
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise LocationNotFoundError(f'Invalid coordinates: "{query}".')
            if not get_us_boundary().contains(lat, lon):
                raise LocationNotFoundError(f'"{query}" is outside the USA.',
                                            'Only US locations are supported.')
            return GeocodedLocation(query, lat, lon, query, 'coordinates')

        cache_key = f'geocode:v2:{normalize_place(query)}'
        cached = caches['persistent'].get(cache_key)
        if cached:
            return GeocodedLocation(**{**cached, 'query': query, 'from_cache': True})

        location = self._from_gazetteer(query) or self._from_nominatim(query)
        # Gazetteer entries are US places by construction (and Census internal
        # points can sit offshore, e.g. San Francisco's includes the Farallons),
        # so only free-text Nominatim results need the boundary check.
        if (location.source == 'nominatim'
                and not get_us_boundary().contains(location.lat, location.lon)):
            raise LocationNotFoundError(
                f'"{query}" resolved to {location.label}, which is outside the USA.',
                'Only US locations are supported. For a US place, add the state, '
                'e.g. "Paris, TX".',
            )
        caches['persistent'].set(
            cache_key,
            {'query': query, 'lat': location.lat, 'lon': location.lon,
             'label': location.label, 'source': location.source},
            settings.GEOCODE_CACHE_SECONDS,
        )
        return location

    @staticmethod
    def _from_gazetteer(query: str) -> GeocodedLocation | None:
        parsed = _parse_city_state(query)
        if not parsed:
            return None
        city, state = parsed
        hit = get_gazetteer().lookup(city, state)
        if not hit:
            return None
        return GeocodedLocation(query, hit[0], hit[1], f'{city.title()}, {state}',
                                'gazetteer')

    def _from_nominatim(self, query: str) -> GeocodedLocation:
        try:
            # No country filter: a foreign place must resolve abroad and be
            # rejected by the boundary check, not match a US street name.
            result = self._nominatim.geocode(query, exactly_one=True)
        except GeopyError as exc:
            logger.warning('Nominatim failed for %r: %s', query, exc)
            raise UpstreamServiceError(
                'Geocoding service unavailable.',
                f'Nominatim error while resolving "{query}": {exc}',
            ) from exc
        if result is None:
            raise LocationNotFoundError(
                f'Could not find location "{query}".',
                'Try the form "City, ST" (e.g. "Austin, TX") or "lat,lon".',
            )
        return GeocodedLocation(query, result.latitude, result.longitude,
                                result.address, 'nominatim')


_geocoder: LocationGeocoder | None = None


def get_geocoder() -> LocationGeocoder:
    """Return the process-wide LocationGeocoder."""
    global _geocoder
    if _geocoder is None:
        _geocoder = LocationGeocoder()
    return _geocoder
