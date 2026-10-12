"""Offline "is this point in the USA?" check.

``data/us_boundary.json`` is the US Census Bureau's 2024 cartographic boundary
of the nation (1:5,000,000, public domain), simplified to ~0.5 km and buffered
outward by the same amount so waterfront coordinates still count. It separates
border pairs such as Detroit / Windsor and San Ysidro / Tijuana. Built by
``manage.py geocode_stations``.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from django.conf import settings
from shapely.geometry import Point, shape
from shapely.prepared import prep

from .errors import DataNotReadyError


class UsBoundary:
    """Point-in-polygon test against the US national boundary."""

    def __init__(self, geojson: dict) -> None:
        self._geometry = prep(shape(geojson))

    @classmethod
    def from_file(cls, path: Path) -> 'UsBoundary':
        """Load the boundary GeoJSON geometry."""
        if not Path(path).exists():
            raise DataNotReadyError(
                'US boundary data not found.',
                f'{path} is missing. Run: python manage.py geocode_stations',
            )
        with open(path, encoding='utf-8') as fh:
            return cls(json.load(fh))

    def contains(self, lat: float, lon: float) -> bool:
        """True if (lat, lon) is on US soil."""
        return self._geometry.contains(Point(lon, lat))


_boundary: UsBoundary | None = None
_boundary_lock = threading.Lock()


def get_us_boundary() -> UsBoundary:
    """Return the process-wide boundary, loading it on first use."""
    global _boundary
    if _boundary is None:
        with _boundary_lock:
            if _boundary is None:
                _boundary = UsBoundary.from_file(settings.US_BOUNDARY_JSON)
    return _boundary
