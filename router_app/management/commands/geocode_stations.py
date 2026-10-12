"""Offline geocoding of the OPIS fuel-price CSV.

Usage::

    python manage.py geocode_stations
    python manage.py geocode_stations --no-nominatim   # gazetteer only, no network

Pipeline:

1. Download the US Census Gazetteer "places" and "county subdivisions" files
   once (≈2.7 MB, public domain) into ``data/gazetteer/``.
2. Build ``data/us_places.json`` — a compact (state → normalized name → lat/lon)
   lookup. It is reused at request time to geocode "City, ST" user input.
3. Load the CSV, drop non-US (Canadian) rows, strip padded city names.
4. Geocode each unique (City, State) pair against the gazetteer; pairs it
   misses fall back to Nominatim at ≤1 request/second. Nominatim answers are
   persisted so re-runs never repeat a network lookup.
5. Collapse duplicate rows per OPIS station ID, keeping the MINIMUM price.
6. Write ``data/stations_geocoded.json`` and print a summary.
"""
from __future__ import annotations

import csv
import io
import json
import time
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from geopy.exc import GeopyError
from geopy.geocoders import Nominatim

from router_app.services.geocoding import US_STATES, normalize_place

GAZETTEER_KINDS = ('place', 'cousubs')


def clean_gazetteer_name(raw: str) -> str:
    """Strip the Census legal/statistical suffix from a gazetteer NAME.

    'Lake Station city' → 'Lake Station'
    'Indianapolis city (balance)' → 'Indianapolis'
    'Nashville-Davidson metropolitan government (balance)' → 'Nashville-Davidson'
    'St. Johns CDP' → 'St. Johns'
    """
    name = raw.replace('(balance)', '').strip()
    for suffix in (' CDP', ' CCD', ' UT'):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    tokens = name.split()
    # Suffixes (city, town, village, township, borough, ...) are lower-case;
    # real place names are capitalised.
    while len(tokens) > 1 and tokens[-1].islower():
        tokens.pop()
    return ' '.join(tokens)


def name_variants(clean_name: str) -> set[str]:
    """Return normalized lookup keys for a place, incl. hyphen/slash parts."""
    keys = {normalize_place(clean_name)}
    for sep in ('-', '/'):
        if sep in clean_name:
            keys.update(normalize_place(part) for part in clean_name.split(sep))
    keys.discard('')
    return keys


class Command(BaseCommand):
    """Geocode fuel stations offline and write data/stations_geocoded.json."""

    help = 'Geocode the fuel price CSV (offline gazetteer + Nominatim fallback).'

    def add_arguments(self, parser) -> None:
        parser.add_argument('--csv', default=str(settings.FUEL_PRICES_CSV))
        parser.add_argument('--output', default=str(settings.STATIONS_GEOCODED_JSON))
        parser.add_argument(
            '--no-nominatim', action='store_true',
            help='Skip the Nominatim fallback (no network after gazetteer download).',
        )

    # ------------------------------------------------------------------ main
    def handle(self, *args: Any, **options: Any) -> None:
        started = time.perf_counter()
        csv_path = Path(options['csv'])
        if not csv_path.exists():
            raise CommandError(f'CSV not found: {csv_path}')

        places = self._build_gazetteer()
        rows, skipped_non_us = self._load_rows(csv_path)
        pairs = sorted({(r['city'], r['state']) for r in rows})
        self.stdout.write(
            f'Loaded {len(rows)} US rows ({skipped_non_us} non-US rows dropped), '
            f'{len(pairs)} unique (City, State) pairs.'
        )

        coords, sources = self._geocode_pairs(pairs, places, options['no_nominatim'])
        stations = self._build_stations(rows, coords, sources)

        output = Path(options['output'])
        output.parent.mkdir(parents=True, exist_ok=True)
        with open(output, 'w', encoding='utf-8') as fh:
            json.dump(stations, fh, separators=(',', ':'))

        failed = [p for p in pairs if p not in coords]
        geocoded_rows = sum(1 for r in rows if (r['city'], r['state']) in coords)
        by_source = defaultdict(int)
        for pair in coords:
            by_source[sources[pair]] += 1

        self.stdout.write(self.style.SUCCESS('\n=== Geocoding summary ==='))
        self.stdout.write(f'Total US rows:            {len(rows)}')
        self.stdout.write(
            f'Rows geocoded:            {geocoded_rows} '
            f'({100 * geocoded_rows / max(len(rows), 1):.1f}%)'
        )
        self.stdout.write(
            f'City/State pairs:         {len(pairs)} '
            f'(gazetteer {by_source["gazetteer"]}, nominatim {by_source["nominatim"]}, '
            f'failed {len(failed)})'
        )
        self.stdout.write(f'Unique stations written:  {len(stations)} -> {output}')
        if failed:
            preview = ', '.join(f'{c}, {s}' for c, s in failed[:25])
            more = f' ... (+{len(failed) - 25} more)' if len(failed) > 25 else ''
            self.stdout.write(self.style.WARNING(f'Failed pairs: {preview}{more}'))
        self.stdout.write(f'Done in {time.perf_counter() - started:.1f}s')

    # ------------------------------------------------------------ gazetteer
    def _download_gazetteer(self, kind: str) -> Path:
        """Ensure the Census gazetteer text file for ``kind`` exists locally."""
        year = settings.GAZETTEER_YEAR
        directory = Path(settings.GAZETTEER_DIR)
        directory.mkdir(parents=True, exist_ok=True)
        txt_path = directory / f'{year}_Gaz_{kind}_national.txt'
        if txt_path.exists():
            return txt_path
        url = (f'{settings.GAZETTEER_BASE_URL}/{year}_Gazetteer/'
               f'{year}_Gaz_{kind}_national.zip')
        self.stdout.write(f'Downloading {url} ...')
        try:
            response = requests.get(url, timeout=60)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise CommandError(f'Gazetteer download failed: {exc}') from exc
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            member = next(n for n in archive.namelist() if n.endswith('.txt'))
            txt_path.write_bytes(archive.read(member))
        return txt_path

    def _build_gazetteer(self) -> dict[str, dict[str, list[float]]]:
        """Build (and persist) state → normalized-name → [lat, lon]."""
        # (state, key) → (priority tuple, lat, lon). Places beat county
        # subdivisions; active governments beat CDPs; larger land area wins ties.
        best: dict[tuple[str, str], tuple[tuple, float, float]] = {}
        for kind_rank, kind in enumerate(GAZETTEER_KINDS):
            path = self._download_gazetteer(kind)
            with open(path, encoding='utf-8', errors='replace') as fh:
                reader = csv.DictReader(fh, delimiter='\t')
                reader.fieldnames = [f.strip() for f in reader.fieldnames or []]
                for row in reader:
                    state = row['USPS'].strip()
                    if state not in US_STATES:
                        continue
                    try:
                        lat = float(row['INTPTLAT'])
                        lon = float(row['INTPTLONG'].strip())
                        land = float(row['ALAND'])
                    except (KeyError, ValueError):
                        continue
                    priority = (-kind_rank, row.get('FUNCSTAT') == 'A', land)
                    for key in name_variants(clean_gazetteer_name(row['NAME'])):
                        current = best.get((state, key))
                        if current is None or priority > current[0]:
                            best[(state, key)] = (priority, lat, lon)

        places: dict[str, dict[str, list[float]]] = defaultdict(dict)
        for (state, key), (_, lat, lon) in best.items():
            places[state][key] = [round(lat, 6), round(lon, 6)]
        with open(settings.US_PLACES_JSON, 'w', encoding='utf-8') as fh:
            json.dump(places, fh, separators=(',', ':'))
        self.stdout.write(
            f'Gazetteer: {len(best)} place keys -> {settings.US_PLACES_JSON}'
        )
        return places

    # ------------------------------------------------------------------ CSV
    @staticmethod
    def _load_rows(csv_path: Path) -> tuple[list[dict], int]:
        """Read the CSV, normalise whitespace and drop non-US rows."""
        rows: list[dict] = []
        skipped = 0
        with open(csv_path, encoding='utf-8-sig', errors='replace', newline='') as fh:
            for raw in csv.DictReader(fh):
                state = (raw.get('State') or '').strip().upper()
                if state not in US_STATES:
                    skipped += 1
                    continue
                try:
                    price = float(raw['Retail Price'])
                except (TypeError, ValueError):
                    skipped += 1
                    continue
                rows.append({
                    'opis_id': int(raw['OPIS Truckstop ID']),
                    'name': ' '.join(raw['Truckstop Name'].split()),
                    'address': ' '.join(raw['Address'].split()),
                    'city': ' '.join(raw['City'].split()),
                    'state': state,
                    'rack_id': int(raw['Rack ID']) if raw['Rack ID'].strip() else None,
                    'retail_price': price,
                })
        return rows, skipped

    # ------------------------------------------------------------ geocoding
    def _geocode_pairs(
        self,
        pairs: list[tuple[str, str]],
        places: dict[str, dict[str, list[float]]],
        skip_nominatim: bool,
    ) -> tuple[dict[tuple[str, str], tuple[float, float]], dict[tuple[str, str], str]]:
        """Resolve every (city, state) pair; return coords and their source."""
        coords: dict[tuple[str, str], tuple[float, float]] = {}
        sources: dict[tuple[str, str], str] = {}
        missing: list[tuple[str, str]] = []
        for city, state in pairs:
            hit = places.get(state, {}).get(normalize_place(city))
            if hit:
                coords[(city, state)] = (hit[0], hit[1])
                sources[(city, state)] = 'gazetteer'
            else:
                missing.append((city, state))
        self.stdout.write(
            f'Gazetteer matched {len(coords)}/{len(pairs)} pairs; '
            f'{len(missing)} need fallback.'
        )
        if not missing:
            return coords, sources

        cache_path = Path(settings.GEOCODE_FALLBACK_CACHE_JSON)
        fallback: dict[str, list[float] | None] = {}
        if cache_path.exists():
            fallback = json.loads(cache_path.read_text(encoding='utf-8'))

        to_query = [p for p in missing if f'{p[0]}|{p[1]}' not in fallback]
        if to_query and not skip_nominatim:
            self.stdout.write(
                f'Querying Nominatim for {len(to_query)} pairs '
                f'(~{len(to_query) * settings.NOMINATIM_MIN_DELAY_SECONDS:.0f}s) ...'
            )
            geolocator = Nominatim(user_agent=settings.NOMINATIM_USER_AGENT,
                                   timeout=settings.GEOCODING_TIMEOUT_SECONDS)
            for n, (city, state) in enumerate(to_query, 1):
                fallback[f'{city}|{state}'] = self._nominatim_lookup(
                    geolocator, city, state
                )
                if n % 25 == 0 or n == len(to_query):
                    self.stdout.write(f'  {n}/{len(to_query)}')
                    cache_path.write_text(json.dumps(fallback), encoding='utf-8')
                time.sleep(settings.NOMINATIM_MIN_DELAY_SECONDS)

        for city, state in missing:
            hit = fallback.get(f'{city}|{state}')
            if hit:
                coords[(city, state)] = (hit[0], hit[1])
                sources[(city, state)] = 'nominatim'
        return coords, sources

    def _nominatim_lookup(
        self, geolocator: Nominatim, city: str, state: str
    ) -> list[float] | None:
        """One structured Nominatim query; None when not found or on error."""
        query = {'city': city, 'state': US_STATES[state], 'country': 'USA'}
        try:
            result = geolocator.geocode(query, exactly_one=True, country_codes='us')
        except GeopyError as exc:
            self.stderr.write(f'  Nominatim error for {city}, {state}: {exc}')
            return None
        if result is None:
            return None
        return [round(result.latitude, 6), round(result.longitude, 6)]

    # ------------------------------------------------------------- stations
    @staticmethod
    def _build_stations(
        rows: list[dict],
        coords: dict[tuple[str, str], tuple[float, float]],
        sources: dict[tuple[str, str], str],
    ) -> list[dict]:
        """Attach coordinates and collapse duplicate OPIS IDs (min price)."""
        by_id: dict[int, dict] = {}
        observations: dict[int, int] = defaultdict(int)
        for row in rows:
            pair = (row['city'], row['state'])
            if pair not in coords:
                continue
            observations[row['opis_id']] += 1
            current = by_id.get(row['opis_id'])
            if current is None or row['retail_price'] < current['retail_price']:
                lat, lon = coords[pair]
                by_id[row['opis_id']] = {
                    **row,
                    'retail_price': round(row['retail_price'], 4),
                    'lat': lat,
                    'lon': lon,
                    'geocode_source': sources[pair],
                }
        stations = sorted(by_id.values(), key=lambda s: s['opis_id'])
        for station in stations:
            station['price_observations'] = observations[station['opis_id']]
        return stations
