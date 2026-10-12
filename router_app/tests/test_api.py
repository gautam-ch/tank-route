"""End-to-end tests of GET /api/route/ with the routing API mocked.

OSRM is replaced by a straight line between the two points, so these run
offline but still exercise the real station index, corridor search,
optimizer and response format — and count every routing call.
"""
from __future__ import annotations

from unittest.mock import patch

import numpy as np
from django.core.cache import caches
from django.test import SimpleTestCase, override_settings
from geopy.geocoders import Nominatim

from ..services.errors import NoRouteError, UpstreamServiceError
from ..services.routing import Route, RoutingClient, haversine_miles
from .test_locations import TEST_CACHES

URL = '/api/route/'


def straight_line_route(_client: RoutingClient, start: tuple[float, float],
                        finish: tuple[float, float]) -> Route:
    """Stand-in for one OSRM call: 300 points on a straight line, 60 mph."""
    lat = np.linspace(start[0], finish[0], 300)
    lon = np.linspace(start[1], finish[1], 300)
    miles = float(haversine_miles(lat[:-1], lon[:-1], lat[1:], lon[1:]).sum())
    return Route(np.column_stack((lon, lat)), miles, miles, 'osrm')


@override_settings(CACHES=TEST_CACHES)
class RouteApiTests(SimpleTestCase):

    def setUp(self) -> None:
        for name in ('default', 'persistent'):
            caches[name].clear()
        osrm = patch.object(RoutingClient, '_osrm', autospec=True,
                            side_effect=straight_line_route)
        self.osrm = osrm.start()
        self.addCleanup(osrm.stop)
        # "City, ST" input must never need the network geocoder.
        nominatim = patch.object(Nominatim, 'geocode',
                                 side_effect=AssertionError('unexpected Nominatim call'))
        nominatim.start()
        self.addCleanup(nominatim.stop)

    def get(self, **params):
        return self.client.get(URL, params)

    # ------------------------------------------------------- external calls
    def test_new_route_calls_routing_api_once_and_repeat_never(self) -> None:
        first = self.get(start='Chicago, IL', finish='Dallas, TX')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(self.osrm.call_count, 1)
        self.assertEqual(first.json()['performance']['external_api_calls'], 1)

        repeat = self.get(start='chicago il', finish='Dallas, Texas')
        self.assertEqual(repeat.status_code, 200)
        self.assertEqual(self.osrm.call_count, 1)
        self.assertEqual(repeat.json()['performance']['external_api_calls'], 0)
        self.assertTrue(repeat.json()['route']['served_from_cache'])

    def test_same_start_and_finish_skips_routing(self) -> None:
        body = self.get(start='Denver, CO', finish='Denver, CO').json()
        self.assertEqual(self.osrm.call_count, 0)
        self.assertEqual(body['route']['distance_miles'], 0)
        self.assertEqual(body['fuel_plan']['total_fuel_cost_usd'], 0)

    # ------------------------------------------------------- response contract
    def test_plan_is_feasible_ordered_and_adds_up(self) -> None:
        response = self.get(start='Chicago, IL', finish='Dallas, TX')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        plan, distance = body['fuel_plan'], body['route']['distance_miles']
        stops = plan['stops']
        self.assertGreaterEqual(len(stops), 1)

        markers = [s['mile_marker'] for s in stops]
        self.assertEqual(markers, sorted(markers))
        legs = np.diff([0.0, *markers, distance])
        self.assertLessEqual(legs.max(), plan['vehicle']['planning_range_miles'] + 0.1)
        for s in stops:
            self.assertLessEqual(s['off_route_miles'], 10)
            self.assertAlmostEqual(s['gallons_purchased'] * s['price_per_gallon'],
                                   s['cost_at_stop'], delta=0.006)
            self.assertLessEqual(s['gallons_in_tank_on_arrival'] + s['gallons_purchased'],
                                 50 + 0.002)  # both shown to 0.001 gal

        breakdown = plan['cost_breakdown']
        self.assertAlmostEqual(plan['cost_at_stops_usd'],
                               sum(s['cost_at_stop'] for s in stops), places=2)
        self.assertAlmostEqual(
            plan['total_fuel_cost_usd'],
            breakdown['starting_tank_cost_usd'] + plan['cost_at_stops_usd']
            - breakdown['ending_tank_value_usd'], places=2)
        self.assertAlmostEqual(plan['total_gallons'], distance / 10, places=1)

        geometry = body['map_data']['route_geometry']
        self.assertEqual(geometry['type'], 'LineString')
        self.assertGreaterEqual(len(geometry['coordinates']), 2)
        self.assertEqual(len(body['map_data']['fuel_stop_markers']), len(stops))
        self.assertIn('/api/map/?', body['map_url'])

    def test_short_trip_reports_cost_of_fuel_burned(self) -> None:
        plan = self.get(start='Austin, TX', finish='Houston, TX').json()['fuel_plan']
        self.assertEqual(plan['fuel_stop_count'], 0)
        self.assertEqual(plan['cost_at_stops_usd'], 0)
        price = plan['cost_breakdown']['starting_tank_price_per_gallon']
        self.assertAlmostEqual(plan['total_fuel_cost_usd'],
                               plan['total_gallons'] * price, delta=0.02)
        self.assertGreater(plan['total_fuel_cost_usd'], 0)

    # ---------------------------------------------------------------- errors
    def test_validation_errors(self) -> None:
        cases = [
            ({'start': 'Chicago, IL'}, 'finish'),
            ({}, 'start'),
            ({'start': '', 'finish': 'Dallas, TX'}, 'start'),
            ({'start': 'Denver, CO', 'finish': 'Dallas, TX', 'strategy': 'cheapest'}, 'strategy'),
            ({'start': 'Denver, CO', 'finish': 'Dallas, TX', 'stop_minutes': -5}, 'stop_minutes'),
        ]
        for params, field in cases:
            response = self.client.get(URL, params)
            self.assertEqual(response.status_code, 400, params)
            self.assertIn(field, response.json()['detail'])
        self.assertEqual(self.osrm.call_count, 0)

    def test_location_outside_usa(self) -> None:
        response = self.get(start='51.5074,-0.1278', finish='48.8566,2.3522')
        self.assertEqual(response.status_code, 400)
        self.assertIn('outside the USA', response.json()['error'])
        self.assertEqual(self.osrm.call_count, 0)

    def test_gap_longer_than_range(self) -> None:
        response = self.get(start='Seattle, WA', finish='Anchorage, AK')
        self.assertEqual(response.status_code, 400)
        self.assertIn('no fuel stations within range', response.json()['error'])

    def test_no_drivable_route(self) -> None:
        self.osrm.side_effect = NoRouteError('No drivable route found.')
        response = self.get(start='Honolulu, HI', finish='Los Angeles, CA')
        self.assertEqual(response.status_code, 400)

    def test_routing_outage_is_502(self) -> None:
        self.osrm.side_effect = UpstreamServiceError('OSRM routing service unreachable.')
        response = self.get(start='Chicago, IL', finish='Dallas, TX')
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()['error'], 'OSRM routing service unreachable.')
