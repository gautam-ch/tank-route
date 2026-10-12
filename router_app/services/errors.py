"""Exception hierarchy shared by the route-planning services.

Every error carries an HTTP status code so the view layer can translate it
into a JSON response without knowing which service raised it.
"""
from __future__ import annotations


class RoutePlanningError(Exception):
    """Base class for all expected (non-bug) failures while planning a route."""

    status_code: int = 400

    def __init__(self, message: str, detail: str = '') -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class LocationNotFoundError(RoutePlanningError):
    """A start/finish string could not be resolved to a US coordinate."""

    status_code = 400


class NoRouteError(RoutePlanningError):
    """The routing engine found no drivable route between the two points."""

    status_code = 400


class NoFeasiblePlanError(RoutePlanningError):
    """The route has a gap between fuel stations longer than the vehicle range."""

    status_code = 400


class UpstreamServiceError(RoutePlanningError):
    """An external API (routing / geocoding) failed or timed out."""

    status_code = 502


class DataNotReadyError(RoutePlanningError):
    """The geocoded station file has not been generated yet."""

    status_code = 503
