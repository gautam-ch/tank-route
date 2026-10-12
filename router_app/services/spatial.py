"""In-memory station index and route-corridor search.

Stations are stored as 3D unit vectors on the sphere and indexed with a
``scipy.spatial.cKDTree``. Straight-line (chord) distance between unit vectors
is a monotonic function of great-circle distance, so a radius in miles means the
same thing at every latitude — unlike a raw degree buffer, where 0.145° of
longitude is ~10 miles in Texas but only ~7 miles in Montana.

Corridor search per request:
1. Resample the route every ``CORRIDOR_SAMPLE_INTERVAL_MILES``.
2. Ball-query the station tree around every sample → candidate stations.
3. Project candidates onto a finely resampled route to get a mile-marker and
   the perpendicular (off-route) distance; drop those beyond the radius.
4. Collapse stations sharing a coordinate (city-level geocoding) to the cheapest.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from django.conf import settings
from scipy.spatial import cKDTree

from .errors import DataNotReadyError
from .routing import EARTH_RADIUS_MILES, Route

logger = logging.getLogger(__name__)


def to_unit_xyz(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    """Convert degrees to (N, 3) unit vectors on the sphere."""
    lat_r, lon_r = np.radians(lat), np.radians(lon)
    cos_lat = np.cos(lat_r)
    return np.column_stack((cos_lat * np.cos(lon_r), cos_lat * np.sin(lon_r),
                            np.sin(lat_r)))


def miles_to_chord(miles: float) -> float:
    """Great-circle miles → chord length on the unit sphere."""
    return 2.0 * np.sin(miles / (2.0 * EARTH_RADIUS_MILES))


def chord_to_miles(chord: np.ndarray) -> np.ndarray:
    """Chord length on the unit sphere → great-circle miles."""
    return 2.0 * EARTH_RADIUS_MILES * np.arcsin(np.clip(chord / 2.0, 0.0, 1.0))


def resample_route(route: Route, interval_miles: float) -> tuple[np.ndarray, np.ndarray]:
    """Points every ``interval_miles`` along the route.

    Returns (miles, lonlat) where lonlat is (M, 2). Linear interpolation in
    degrees is accurate here because OSRM vertices are closely spaced.
    """
    cum = route.cumulative_miles
    total = float(cum[-1]) if len(cum) else 0.0
    miles = np.append(np.arange(0.0, total, interval_miles), total)
    lon = np.interp(miles, cum, route.coordinates[:, 0])
    lat = np.interp(miles, cum, route.coordinates[:, 1])
    return miles, np.column_stack((lon, lat))


@dataclass
class Corridor:
    """Stations along a route, sorted by mile-marker (1D problem input)."""

    station_ids: np.ndarray       # indices into StationIndex arrays
    mile_markers: np.ndarray
    off_route_miles: np.ndarray
    prices: np.ndarray
    raw_candidate_count: int      # before collapsing same-location duplicates

    def __len__(self) -> int:
        return len(self.station_ids)


class StationIndex:
    """All geocoded stations in memory, with a KD-tree over unit vectors."""

    def __init__(self, stations: list[dict]) -> None:
        if not stations:
            raise DataNotReadyError('Station dataset is empty.',
                                    'Run: python manage.py geocode_stations')
        self.stations = stations
        self.lat = np.array([s['lat'] for s in stations], dtype=float)
        self.lon = np.array([s['lon'] for s in stations], dtype=float)
        self.price = np.array([s['retail_price'] for s in stations], dtype=float)
        self.tree = cKDTree(to_unit_xyz(self.lat, self.lon))

    @classmethod
    def from_file(cls, path: Path) -> 'StationIndex':
        """Load ``stations_geocoded.json`` and build the index."""
        if not Path(path).exists():
            raise DataNotReadyError(
                'Geocoded station data not found.',
                f'{path} is missing. Run: python manage.py geocode_stations',
            )
        with open(path, encoding='utf-8') as fh:
            return cls(json.load(fh))

    def __len__(self) -> int:
        return len(self.stations)

    def nearest(self, lat: float, lon: float) -> tuple[int, float]:
        """Return (station index, distance in miles) of the closest station."""
        chord, idx = self.tree.query(to_unit_xyz(np.array([lat]), np.array([lon])))
        return int(idx[0]), float(chord_to_miles(chord)[0])

    def corridor(self, route: Route) -> Corridor:
        """Find stations within ``CORRIDOR_RADIUS_MILES`` of the route."""
        radius = float(settings.CORRIDOR_RADIUS_MILES)
        interval = float(settings.CORRIDOR_SAMPLE_INTERVAL_MILES)
        empty = Corridor(np.array([], dtype=int), np.array([]), np.array([]),
                         np.array([]), 0)
        if len(route.coordinates) < 2 or route.distance_miles <= 0:
            return empty

        # 1–2. Coarse samples + ball query. A point within `radius` of the
        # route is within radius + interval/2 of some sample.
        _, samples = resample_route(route, interval)
        hits = self.tree.query_ball_point(
            to_unit_xyz(samples[:, 1], samples[:, 0]),
            r=miles_to_chord(radius + interval / 2.0),
        )
        candidates = np.unique(np.fromiter(
            (i for group in hits for i in group), dtype=int))
        if candidates.size == 0:
            return empty

        # 3. Project onto a fine resampling of the route.
        fine_miles, fine = resample_route(
            route, float(settings.ROUTE_PROJECTION_RESOLUTION_MILES))
        fine_tree = cKDTree(to_unit_xyz(fine[:, 1], fine[:, 0]))
        chord, nearest = fine_tree.query(
            to_unit_xyz(self.lat[candidates], self.lon[candidates]))
        offset = chord_to_miles(chord)
        keep = offset <= radius
        candidates, markers, offset = candidates[keep], fine_miles[nearest[keep]], offset[keep]
        raw_count = int(candidates.size)

        # 4. Collapse same-coordinate stations (same geocoded city) to the
        # cheapest one — identical for the optimizer, fewer nodes.
        prices = self.price[candidates]
        order = np.lexsort((prices, self.lon[candidates], self.lat[candidates]))
        candidates, markers, offset, prices = (
            a[order] for a in (candidates, markers, offset, prices))
        loc = np.column_stack((self.lat[candidates], self.lon[candidates]))
        first = np.ones(len(candidates), dtype=bool)
        first[1:] = np.any(loc[1:] != loc[:-1], axis=1)
        candidates, markers, offset, prices = (
            a[first] for a in (candidates, markers, offset, prices))

        by_mile = np.argsort(markers, kind='stable')
        return Corridor(candidates[by_mile], markers[by_mile], offset[by_mile],
                        prices[by_mile], raw_count)


_index: StationIndex | None = None
_index_lock = threading.Lock()


def get_station_index() -> StationIndex:
    """Return the process-wide StationIndex, loading it on first use."""
    global _index
    if _index is None:
        with _index_lock:
            if _index is None:
                started = time.perf_counter()
                _index = StationIndex.from_file(settings.STATIONS_GEOCODED_JSON)
                logger.info('Loaded %d stations into KD-tree in %.0f ms.',
                            len(_index), (time.perf_counter() - started) * 1000)
    return _index
