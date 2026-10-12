"""Start/finish resolution: name matching, US boundary, geocoder fallbacks."""
from __future__ import annotations

from unittest.mock import patch

from django.core.cache import caches
from django.test import SimpleTestCase, override_settings
from geopy.geocoders import Nominatim
from geopy.location import Location

from ..management.commands.geocode_stations import clean_gazetteer_name
from ..services.boundary import get_us_boundary
from ..services.errors import LocationNotFoundError
from ..services.geocoding import LocationGeocoder, _parse_city_state, normalize_place

TEST_CACHES = {
    'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
                'LOCATION': 'test-default'},
    'persistent': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
                   'LOCATION': 'test-persistent'},
}


class TextNormalisationTests(SimpleTestCase):
    """Place-name matching rules shared by stations and user input."""

    def test_normalize_place_aliases(self) -> None:
        self.assertEqual(normalize_place('Saint Johns'), normalize_place('St. Johns'))
        self.assertEqual(normalize_place('Mc Graw'), normalize_place('McGraw'))
        self.assertEqual(normalize_place('S Coffeyville'),
                         normalize_place('South Coffeyville'))

    def test_clean_gazetteer_name(self) -> None:
        self.assertEqual(clean_gazetteer_name('Indianapolis city (balance)'), 'Indianapolis')
        self.assertEqual(clean_gazetteer_name('St. Johns CDP'), 'St. Johns')
        self.assertEqual(clean_gazetteer_name('Lake Station city'), 'Lake Station')

    def test_parse_city_state(self) -> None:
        self.assertEqual(_parse_city_state('Austin, TX'), ('Austin', 'TX'))
        self.assertEqual(_parse_city_state('Austin, Texas, USA'), ('Austin', 'TX'))
        self.assertEqual(_parse_city_state('Austin TX 78701'), ('Austin', 'TX'))
        self.assertIsNone(_parse_city_state('Empire State Building'))
        self.assertIsNone(_parse_city_state('Toronto, ON'))

    def test_street_address_is_not_treated_as_city(self) -> None:
        self.assertIsNone(_parse_city_state('1600 Pennsylvania Ave NW, Washington, DC'))


class UsBoundaryTests(SimpleTestCase):
    """Border pairs a few miles apart must land on the right side."""

    inside = {
        'Detroit': (42.33, -83.05), 'San Ysidro': (32.556, -117.05),
        'Sault Ste. Marie, MI': (46.4977, -84.3476), 'Calais, ME': (45.1887, -67.2786),
        'Point Roberts, WA': (48.985, -123.07), 'Key West': (24.555, -81.78),
        'Honolulu': (21.307, -157.858), 'Anchorage': (61.22, -149.9),
        'Seattle waterfront': (47.6062, -122.3421),
    }
    outside = {
        'Windsor, ON': (42.317, -83.036), 'Tijuana': (32.514, -117.038),
        'Sault Ste. Marie, ON': (46.5219, -84.3461), 'St. Stephen, NB': (45.1946, -67.2753),
        'Toronto': (43.653, -79.383), 'Vancouver': (49.283, -123.121),
        'Mexico City': (19.43, -99.13), 'London': (51.5074, -0.1278),
    }

    def test_points(self) -> None:
        boundary = get_us_boundary()
        for name, (lat, lon) in self.inside.items():
            self.assertTrue(boundary.contains(lat, lon), name)
        for name, (lat, lon) in self.outside.items():
            self.assertFalse(boundary.contains(lat, lon), name)


@override_settings(CACHES=TEST_CACHES)
class GeocoderTests(SimpleTestCase):
    """Resolution order and the outside-the-USA guard."""

    def setUp(self) -> None:
        caches['persistent'].clear()
        self.geocoder = LocationGeocoder()

    def test_city_state_uses_offline_gazetteer(self) -> None:
        with patch.object(Nominatim, 'geocode') as nominatim:
            location = self.geocoder.geocode('chicago il')
        nominatim.assert_not_called()
        self.assertEqual((location.source, location.label), ('gazetteer', 'Chicago, IL'))
        self.assertFalse(location.made_network_call)

    def test_gazetteer_cities_with_offshore_centroids_are_accepted(self) -> None:
        # Census internal points for these lie in the water (islands, bays).
        with patch.object(Nominatim, 'geocode') as nominatim:
            for query in ('San Francisco, CA', 'Portland, ME', 'Corpus Christi, TX'):
                self.assertEqual(self.geocoder.geocode(query).source, 'gazetteer', query)
        nominatim.assert_not_called()

    def test_honolulu_is_the_city_not_the_county_subdivision(self) -> None:
        location = self.geocoder.geocode('Honolulu, HI')
        self.assertAlmostEqual(location.lat, 21.32, places=1)
        self.assertAlmostEqual(location.lon, -157.85, places=1)

    def test_coordinates_outside_usa_are_rejected(self) -> None:
        with self.assertRaisesRegex(LocationNotFoundError, 'outside the USA'):
            self.geocoder.geocode('51.5074,-0.1278')

    def test_foreign_place_is_rejected_not_matched_to_a_us_street(self) -> None:
        toronto = Location('Toronto, Ontario, Canada', (43.653, -79.383, 0), {})
        with patch.object(Nominatim, 'geocode', return_value=toronto) as nominatim:
            with self.assertRaisesRegex(LocationNotFoundError, 'outside the USA'):
                self.geocoder.geocode('Toronto, ON')
        self.assertNotIn('country_codes', nominatim.call_args.kwargs)

    def test_unknown_place(self) -> None:
        with patch.object(Nominatim, 'geocode', return_value=None):
            with self.assertRaises(LocationNotFoundError):
                self.geocoder.geocode('asdkjhqwe zzxq')

    def test_nominatim_results_are_cached(self) -> None:
        esb = Location('Empire State Building, New York', (40.7484, -73.9857, 0), {})
        with patch.object(Nominatim, 'geocode', return_value=esb) as nominatim:
            first = self.geocoder.geocode('Empire State Building')
            second = self.geocoder.geocode('Empire State Building')
        self.assertEqual(nominatim.call_count, 1)
        self.assertTrue(first.made_network_call)
        self.assertFalse(second.made_network_call)
