"""Routing client: one OSRM call per uncached route.

OSRM's role: it is the only source of *road* knowledge in the system. One call
returns the road geometry (which stations are "along the route"), the road
distance (mile-markers and range checks) and the driving duration. Results are
cached on disk by rounded coordinates (survives restarts), so repeated routes make zero external calls.

If OSRM fails and ``OPENROUTESERVICE_API_KEY`` is set, OpenRouteService is
used as a fallback (one extra call, only on failure).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import requests
from django.conf import settings
from django.core.cache import caches

from .errors import NoRouteError, UpstreamServiceError

logger = logging.getLogger(__name__)

METERS_PER_MILE = 1609.344
EARTH_RADIUS_MILES = 3958.7613


@dataclass
class Route:
    """A driving route.

    ``coordinates`` is an (N, 2) array of [lon, lat] (GeoJSON order).
    ``cumulative_miles`` is the road distance from the start to each vertex,
    scaled so its last value equals the provider's reported distance.
    """

    coordinates: np.ndarray
    distance_miles: float
    duration_minutes: float
    provider: str
    cumulative_miles: np.ndarray = field(init=False)
    from_cache: bool = False

    def __post_init__(self) -> None:
        self.cumulative_miles = cumulative_distance_miles(
            self.coordinates, self.distance_miles
        )

    @property
    def made_network_call(self) -> bool:
        """True if this route was fetched from an external API."""
        return not self.from_cache


def haversine_miles(lat1: np.ndarray, lon1: np.ndarray,
                    lat2: np.ndarray, lon2: np.ndarray) -> np.ndarray:
    """Vectorised great-circle distance in miles."""
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = (np.sin((lat2 - lat1) / 2) ** 2
         + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def cumulative_distance_miles(coords: np.ndarray, total_miles: float) -> np.ndarray:
    """Cumulative along-line distance per vertex, scaled to ``total_miles``."""
    if len(coords) < 2:
        return np.zeros(len(coords))
    seg = haversine_miles(coords[:-1, 1], coords[:-1, 0], coords[1:, 1], coords[1:, 0])
    cum = np.concatenate(([0.0], np.cumsum(seg)))
    if cum[-1] > 0 and total_miles > 0:
        cum *= total_miles / cum[-1]
    return cum


class RoutingClient:
    """Fetches driving routes from OSRM (with optional ORS fallback)."""

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers['User-Agent'] = settings.NOMINATIM_USER_AGENT
        self._cache = caches['persistent']

    def get_route(self, start: tuple[float, float],
                  finish: tuple[float, float]) -> Route:
        """Return the route between (lat, lon) ``start`` and ``finish``."""
        key = 'route:v1:{:.4f},{:.4f};{:.4f},{:.4f}'.format(*start, *finish)
        cached = self._cache.get(key)
        if cached is not None:
            return Route(cached['coordinates'], cached['distance_miles'],
                         cached['duration_minutes'], cached['provider'],
                         from_cache=True)
        try:
            route = self._osrm(start, finish)
        except UpstreamServiceError:
            if not settings.OPENROUTESERVICE_API_KEY:
                raise
            logger.warning('OSRM failed; falling back to OpenRouteService.')
            route = self._openrouteservice(start, finish)
        self._cache.set(key, {
            'coordinates': route.coordinates,
            'distance_miles': route.distance_miles,
            'duration_minutes': route.duration_minutes,
            'provider': route.provider,
        }, settings.ROUTE_CACHE_SECONDS)
        return route

    def _get_json(self, url: str, params: dict, service: str) -> dict:
        try:
            response = self._session.get(
                url, params=params, timeout=settings.OSRM_TIMEOUT_SECONDS
            )
        except requests.RequestException as exc:
            raise UpstreamServiceError(
                f'{service} routing service unreachable.', str(exc)
            ) from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise UpstreamServiceError(
                f'{service} routing service unavailable '
                f'(HTTP {response.status_code}).', response.text[:300],
            )
        try:
            return response.json()
        except ValueError as exc:
            raise UpstreamServiceError(
                f'{service} returned an invalid response.', response.text[:300]
            ) from exc

    def _osrm(self, start: tuple[float, float],
              finish: tuple[float, float]) -> Route:
        # OSRM wants lon,lat order.
        url = (f'{settings.OSRM_BASE_URL}/route/v1/driving/'
               f'{start[1]},{start[0]};{finish[1]},{finish[0]}')
        data = self._get_json(url, {
            'overview': 'full', 'geometries': 'geojson',
            'alternatives': 'false', 'steps': 'false',
        }, 'OSRM')
        code = data.get('code')
        if code in ('NoRoute', 'NoSegment'):
            raise NoRouteError('No drivable route found between these locations.',
                               data.get('message', ''))
        if code != 'Ok' or not data.get('routes'):
            raise UpstreamServiceError('OSRM could not compute the route.',
                                       f'{code}: {data.get("message", "")}')
        best = data['routes'][0]
        return Route(
            coordinates=np.asarray(best['geometry']['coordinates'], dtype=float),
            distance_miles=best['distance'] / METERS_PER_MILE,
            duration_minutes=best['duration'] / 60.0,
            provider='osrm',
        )

    def _openrouteservice(self, start: tuple[float, float],
                          finish: tuple[float, float]) -> Route:
        url = f'{settings.OPENROUTESERVICE_BASE_URL}/v2/directions/driving-hgv'
        data = self._get_json(url, {
            'api_key': settings.OPENROUTESERVICE_API_KEY,
            'start': f'{start[1]},{start[0]}',
            'end': f'{finish[1]},{finish[0]}',
        }, 'OpenRouteService')
        features = data.get('features') or []
        if not features:
            raise NoRouteError('No drivable route found between these locations.',
                               str(data.get('error', ''))[:300])
        summary = features[0]['properties'].get('summary', {})
        return Route(
            coordinates=np.asarray(features[0]['geometry']['coordinates'], dtype=float),
            distance_miles=summary.get('distance', 0.0) / METERS_PER_MILE,
            duration_minutes=summary.get('duration', 0.0) / 60.0,
            provider='openrouteservice',
        )


_client: RoutingClient | None = None


def get_routing_client() -> RoutingClient:
    """Return the process-wide routing client (reuses one HTTP session)."""
    global _client
    if _client is None:
        _client = RoutingClient()
    return _client
