"""HTTP layer: the fuel-route API, the map page and a health check."""
from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlencode

from django.conf import settings
from django.urls import reverse
from django.views.generic import TemplateView
from rest_framework import serializers, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from shapely.geometry import LineString

from .services.errors import RoutePlanningError
from .services.geocoding import get_gazetteer, get_geocoder
from .services.optimizer import FuelPlan, optimize
from .services.routing import Route, get_routing_client
from .services.spatial import Corridor, StationIndex, get_station_index

logger = logging.getLogger(__name__)


class RouteQuerySerializer(serializers.Serializer):
    """Validates the query string of GET /api/route/."""

    start = serializers.CharField(max_length=200, trim_whitespace=True)
    finish = serializers.CharField(max_length=200, trim_whitespace=True)
    strategy = serializers.ChoiceField(
        choices=settings.FUEL_STRATEGIES, default=settings.DEFAULT_FUEL_STRATEGY
    )
    stop_minutes = serializers.IntegerField(
        min_value=0, max_value=settings.MAX_STOP_MINUTES,
        default=settings.DEFAULT_STOP_MINUTES,
    )


def _r(value: float, ndigits: int = 2) -> float:
    """Round for JSON output."""
    return round(float(value), ndigits)


def _simplified_geometry(route: Route) -> dict:
    """GeoJSON LineString, Douglas-Peucker simplified for the browser."""
    coords = route.coordinates
    if len(coords) >= 2:
        line = LineString(coords).simplify(
            settings.ROUTE_GEOMETRY_SIMPLIFY_TOLERANCE_DEG, preserve_topology=False)
        coords = list(line.coords)
    return {
        'type': 'LineString',
        'coordinates': [[round(lon, 5), round(lat, 5)] for lon, lat in coords],
    }


def _stops_payload(plan: FuelPlan, corridor: Corridor,
                   index: StationIndex) -> list[dict[str, Any]]:
    """Serialise each purchase with its station details."""
    stops = []
    for n, (purchase, arrival) in enumerate(
            zip(plan.purchases, plan.arrival_gallons), start=1):
        station_idx = int(corridor.station_ids[purchase.node])
        station = index.stations[station_idx]
        stops.append({
            'stop_number': n,
            'opis_id': station['opis_id'],
            'station_name': station['name'],
            'address': station['address'],
            'city': station['city'],
            'state': station['state'],
            'lat': station['lat'],
            'lon': station['lon'],
            'mile_marker': _r(purchase.mile_marker, 1),
            'off_route_miles': _r(corridor.off_route_miles[purchase.node], 1),
            'price_per_gallon': _r(purchase.price, 3),
            'gallons_in_tank_on_arrival': _r(arrival, 1),
            'gallons_purchased': _r(purchase.gallons, 3),
            'cost_at_stop': purchase.billed_cost,
        })
    return stops


def _plan_summary(plan: FuelPlan) -> dict[str, Any]:
    return {
        'total_fuel_cost_usd': _r(plan.total_cost),
        'fuel_consumed_cost_usd': _r(plan.consumed_cost),
        'fuel_stop_count': plan.stop_count,
    }


class FuelRouteView(APIView):
    """GET /api/route/?start=...&finish=...&strategy=full_tank&stop_minutes=20

    Flow: geocode (offline gazetteer, Nominatim fallback) → one OSRM call
    (cached) → KD-tree corridor search → 1D optimizer → JSON with map data.
    """

    def get(self, request: Request) -> Response:
        started = time.perf_counter()
        query = RouteQuerySerializer(data=request.query_params)
        if not query.is_valid():
            return Response({'error': 'Invalid query parameters.',
                             'detail': query.errors},
                            status=status.HTTP_400_BAD_REQUEST)
        params = query.validated_data
        try:
            payload = self._plan(params)
        except RoutePlanningError as exc:
            return Response({'error': exc.message, 'detail': exc.detail},
                            status=exc.status_code)
        except Exception:
            logger.exception('Unexpected error planning route %s', dict(params))
            return Response({'error': 'Internal server error.',
                             'detail': 'Unexpected failure; see server logs.'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        # Link that opens the interactive map with this exact route drawn.
        map_query = urlencode({key: params[key] for key in
                               ('start', 'finish', 'strategy', 'stop_minutes')})
        payload['map_url'] = request.build_absolute_uri(
            f"{reverse('router_app:map')}?{map_query}")
        payload['performance']['total_api_response_ms'] = _r(
            (time.perf_counter() - started) * 1000, 1)
        return Response(payload)

    def _plan(self, params: dict[str, Any]) -> dict[str, Any]:
        t0 = time.perf_counter()
        index = get_station_index()
        geocoder = get_geocoder()
        start = geocoder.geocode(params['start'])
        finish = geocoder.geocode(params['finish'])
        t_geo = time.perf_counter()

        route = get_routing_client().get_route((start.lat, start.lon),
                                               (finish.lat, finish.lon))
        t_route = time.perf_counter()

        corridor = index.corridor(route)
        t_corridor = time.perf_counter()

        start_station_idx, start_station_miles = index.nearest(start.lat, start.lon)
        start_price = float(index.price[start_station_idx])

        plans: dict[str, FuelPlan] = {}
        comparison: dict[str, Any] = {}
        for strategy in settings.FUEL_STRATEGIES:
            try:
                plans[strategy] = optimize(strategy, corridor.mile_markers,
                                           corridor.prices, route.distance_miles,
                                           start_price)
                comparison[strategy] = _plan_summary(plans[strategy])
            except RoutePlanningError as exc:
                if strategy == params['strategy']:
                    raise
                comparison[strategy] = {'error': exc.message}
        plan = plans[params['strategy']]

        stops = _stops_payload(plan, corridor, index)
        halting = plan.stop_count * params['stop_minutes']
        driving = route.duration_minutes
        start_station = index.stations[start_station_idx]
        external_calls = (int(start.made_network_call) + int(finish.made_network_call)
                          + int(route.made_network_call))

        return {
            'start': params['start'],
            'finish': params['finish'],
            'strategy': params['strategy'],
            'locations': {'start': start.as_dict(), 'finish': finish.as_dict()},
            'route': {
                'distance_miles': _r(route.distance_miles, 1),
                'driving_duration_minutes': round(driving),
                'driving_duration_hours': _r(driving / 60, 1),
                'provider': route.provider,
                'served_from_cache': route.from_cache,
            },
            'fuel_plan': {
                'total_fuel_cost_usd': _r(plan.total_cost),
                'total_gallons_purchased': _r(plan.total_gallons, 3),
                'average_price_paid': (_r(plan.total_cost / plan.total_gallons, 3)
                                       if plan.total_gallons else None),
                'fuel_stop_count': plan.stop_count,
                'stops': stops,
                'fuel_consumed': {
                    'note': ('Cost of all fuel burned on the trip. The starting '
                             'full tank is valued at the nearest station to the '
                             'start; purchases are blended at average cost.'),
                    'gallons': _r(plan.consumed_gallons, 2),
                    'cost_usd': _r(plan.consumed_cost),
                    'starting_tank_gallons': plan.vehicle.tank_gallons,
                    'starting_tank_price_per_gallon': _r(start_price, 3),
                    'starting_tank_price_source': (
                        f"{start_station['name']}, {start_station['city']}, "
                        f"{start_station['state']} ({start_station_miles:.0f} mi from start)"
                    ),
                    'ending_tank_gallons': _r(plan.ending_tank_gallons, 2),
                },
                'vehicle': {
                    'tank_gallons': plan.vehicle.tank_gallons,
                    'mpg': plan.vehicle.mpg,
                    'max_range_miles': settings.VEHICLE_TANK_MILES,
                    'safety_reserve_miles': settings.SAFETY_RESERVE_MILES,
                    'planning_range_miles': plan.vehicle.usable_range_miles,
                },
            },
            'strategy_comparison': comparison,
            'timing': {
                'refueling_halting_minutes': halting,
                'total_trip_duration_minutes': round(driving + halting),
                'total_trip_duration_hours': _r((driving + halting) / 60, 1),
                'stop_dwell_minutes': params['stop_minutes'],
            },
            'performance': {
                'algorithm_computation_ms': _r(plan.computation_ms, 3),
                'geocoding_ms': _r((t_geo - t0) * 1000, 1),
                'routing_ms': _r((t_route - t_geo) * 1000, 1),
                'corridor_search_ms': _r((t_corridor - t_route) * 1000, 1),
                'corridor_stations_considered': len(corridor),
                'external_api_calls': external_calls,
            },
            'map_data': {
                'route_geometry': _simplified_geometry(route),
                'start_marker': {'lat': start.lat, 'lon': start.lon,
                                 'label': start.label},
                'finish_marker': {'lat': finish.lat, 'lon': finish.lon,
                                  'label': finish.label},
                'fuel_stop_markers': [
                    {'lat': s['lat'], 'lon': s['lon'],
                     'popup': (f"{s['station_name']}\n${s['price_per_gallon']:.3f}/gal\n"
                               f"{s['gallons_purchased']:.3f} gal = ${s['cost_at_stop']:.2f}\n"
                               f"Stop {s['stop_number']} of {len(stops)}")}
                    for s in stops
                ],
            },
        }


class MapView(TemplateView):
    """GET /api/map/ — Leaflet UI that calls /api/route/."""

    template_name = 'router_app/map.html'

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context['default_stop_minutes'] = settings.DEFAULT_STOP_MINUTES
        context['max_stop_minutes'] = settings.MAX_STOP_MINUTES
        return context


class HealthCheckView(APIView):
    """GET /api/health/ — reports whether the in-memory data is loaded."""

    def get(self, request: Request) -> Response:
        try:
            stations = len(get_station_index())
        except RoutePlanningError as exc:
            return Response({'status': 'degraded', 'detail': exc.detail},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({'status': 'ok', 'service': 'spotter-fuel-route-planner',
                         'stations_loaded': stations,
                         'gazetteer_places': len(get_gazetteer())})
