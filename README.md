# Fuel Route Planner API

A Django REST API that takes a start and finish location in the USA and returns the
driving route, the most cost-effective places to buy diesel along it, and the total
fuel cost — for a vehicle with a 500-mile range that gets 10 miles per gallon.

- **One routing API call per new route** (OSRM, free, no key). Repeat routes: zero.
- **~20 ms** for a repeat request; **~2 s** for a new route, almost all of it the OSRM call.
- **Provably optimal** fuel plans, checked against brute-force search in the test suite.
- Interactive Leaflet map at `/api/map/`; every API response includes a `map_url` to it.

Built with Django 6.1 (latest stable) and Django REST Framework.

---

## Quick start

Requires Python 3.12+.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
python manage.py test router_app
python manage.py runserver
```

- API: http://127.0.0.1:8000/api/route/?start=New%20York,%20NY&finish=Los%20Angeles,%20CA
- Map: http://127.0.0.1:8000/api/map/
- Postman collection: [`docs/postman_collection.json`](docs/postman_collection.json)

The geocoded station data is committed, so there is **no data-preparation step**.
To rebuild it from the CSV: `python manage.py geocode_stations` (≈3.5 min, see below).

---

## API

### `GET /api/route/`

| Parameter | Required | Default | Description |
|---|---|---|---|
| `start` | yes | — | `"City, ST"`, `"City, State"`, `"lat,lon"`, or any US address/landmark |
| `finish` | yes | — | same as `start` |
| `strategy` | no | `full_tank` | `full_tank` or `exact_gallon` (see [Algorithms](#algorithms)) |
| `stop_minutes` | no | `20` | time spent per fuel stop, used for total trip time |

**Response (abridged)** — New York, NY → Los Angeles, CA, `strategy=full_tank`:

```json
{
  "strategy": "full_tank",
  "route": { "distance_miles": 2810.4, "driving_duration_minutes": 3019, "driving_duration_hours": 50.3 },
  "fuel_plan": {
    "total_fuel_cost_usd": 738.11,
    "total_gallons_purchased": 236.0,
    "fuel_stop_count": 6,
    "stops": [
      {
        "stop_number": 1, "station_name": "SHEETZ #639", "address": "I-80 Exit 223",
        "city": "Youngstown", "state": "OH", "lat": 41.099, "lon": -80.646,
        "mile_marker": 396.0, "price_per_gallon": 3.059,
        "gallons_purchased": 39.6, "cost_at_stop": 121.14
      }
    ],
    "fuel_consumed": { "gallons": 281.04, "cost_usd": 891.65 }
  },
  "strategy_comparison": {
    "full_tank":    { "total_fuel_cost_usd": 738.11, "fuel_stop_count": 6 },
    "exact_gallon": { "total_fuel_cost_usd": 709.21, "fuel_stop_count": 15 }
  },
  "timing": { "refueling_halting_minutes": 120, "total_trip_duration_hours": 52.3 },
  "performance": { "algorithm_computation_ms": 1.1, "total_api_response_ms": 25.1, "external_api_calls": 0 },
  "map_url": "http://127.0.0.1:8000/api/map/?start=New+York%2C+NY&finish=Los+Angeles%2C+CA&strategy=full_tank&stop_minutes=20",
  "map_data": {
    "route_geometry": { "type": "LineString", "coordinates": [[-74.006, 40.713], "..."] },
    "fuel_stop_markers": [{ "lat": 41.099, "lon": -80.646, "popup": "SHEETZ #639 ..." }]
  }
}
```

**Two cost figures are returned:**

- `total_fuel_cost_usd` — money spent at fuel stops. The vehicle starts with a full tank,
  so a trip under 470 miles costs $0 here.
- `fuel_consumed.cost_usd` — cost of all fuel burned on the trip, valuing the starting
  tank at the price of the station nearest the start. Use this to compare trips of any length.

Every stop reads like a fuel receipt: price and gallons to 3 decimals, cost to the cent,
and `gallons_purchased × price_per_gallon = cost_at_stop` exactly.

**Errors** return `{"error": "...", "detail": "..."}`:

| Status | When |
|---|---|
| 400 | Invalid parameters, unknown location, no drivable route, or a stretch longer than the vehicle's range with no station (e.g. Seattle → Anchorage through Canada) |
| 502 | The routing or geocoding API failed |
| 503 | Station data not generated |

Also: `GET /api/health/` and `GET /api/map/`.

---

## How a request works

```
start, finish
   │
   ▼
1. Geocode      offline US Census gazetteer for "City, ST" (0 network calls);
                Nominatim only for addresses/landmarks; results cached on disk
   ▼
2. Route        ONE OSRM call → road geometry, distance, duration; cached on disk for 7 days
   ▼
3. Corridor     KD-tree (built at startup) finds stations within 10 road-side miles;
                each is projected onto the route to get its mile-marker
   ▼
4. Optimize     1D problem over ~200 stations sorted by mile-marker (≈1 ms)
   ▼
5. Respond      stops, costs, timings, simplified route geometry, map_url
```

| Step | New route | Repeat route |
|---|---|---|
| Geocoding ("City, ST") | <1 ms, 0 calls | <1 ms, 0 calls |
| Routing (OSRM) | ~2,000 ms, 1 call | ~5 ms (disk cache), 0 calls |
| Corridor search | ~10 ms | ~10 ms |
| Optimizer | ~1 ms | ~1 ms |
| **Total** | **~2 s** | **~20–30 ms** |

### Why OSRM

It is the only source of road knowledge: which stations the truck actually passes,
the road distance to each (what the 500-mile range is checked against), driving time,
and the line drawn on the map. The public demo server is free and needs no key.
If it fails and `OPENROUTESERVICE_API_KEY` is set, OpenRouteService is used as a fallback.

---

## Algorithms

Fuel is planned in *usable* miles: a 500-mile tank with a **30-mile safety reserve**
that is never planned to be burned, so each leg is at most **470 miles**.
The vehicle starts with a full tank.

**`full_tank` (default)** — every stop fills the tank, like a real truck stop.
Minimizes the number of stops first, then cost. Exact dynamic programming over stations:

```
key[i] = min over j with d[i] − d[j] ≤ 470 of  key[j] + BIG + (d[i] − d[j]) / mpg × price[i]
```

`BIG` is larger than any possible fuel bill, so one extra stop always outweighs any
price saving. Each window minimum is a vectorised NumPy operation.

**`exact_gallon`** — buy any amount at each stop; lowest possible cost. This is the
greedy algorithm from Khuller, Malekian & Mestre, *"To Fill or Not to Fill: The Gas
Station Problem"* (ACM TALG 2011), which is optimal on a fixed path:

- If a cheaper station is within range, buy just enough to reach the **first** one.
- Otherwise fill up here and go to the cheapest station within range.
- The destination counts as a free station, so no fuel is bought that would be left over.

On New York → Los Angeles, `exact_gallon` saves $29 but makes 15 stops instead of 6,
so `full_tank` is the practical default. The response includes both for comparison.

**Correctness:** `router_app/tests.py` compares both strategies against exhaustive search
on 400 random instances each.

---

## Station data

The CSV has no coordinates. `python manage.py geocode_stations` runs once, offline:

1. Downloads the US Census Bureau Gazetteer (places + county subdivisions, public domain).
2. Matches each (City, State) using normalized names ("Saint Johns" = "St. Johns",
   "Mc Graw" = "McGraw").
3. Sends the few unmatched names to Nominatim at 1 request/second; answers are saved
   to `data/geocode_fallback_cache.json` so re-runs make no network calls.
4. Keeps one row per OPIS station ID at its **lowest** listed price.

| | |
|---|---|
| CSV rows | 8,151 (620 Canadian rows dropped — USA only) |
| Unique (City, State) pairs | 3,813 — 3,686 gazetteer, 123 Nominatim, 4 not found |
| Rows geocoded | 99.9% |
| Unique stations | 6,621 |

Output: `data/stations_geocoded.json` (stations) and `data/us_places.json`
(the gazetteer, also used to geocode `start`/`finish` without a network call).

---

## Assumptions and limitations

- Stations are placed at their **city's coordinates**, not the exact highway exit
  (the CSV only has city and exit text). The 10-mile corridor absorbs most of this;
  each stop reports `off_route_miles`.
- Prices for stations listed more than once use the **minimum** price.
- Detour distance to reach a station is not added to fuel use.
- The public OSRM server is rate-limited and has no SLA; route caching keeps calls rare.
- Caches are per machine (`data/cache/`); use Redis in production.

---

## Project layout

```
router_app/
  services/
    geocoding.py     start/finish geocoding (gazetteer → Nominatim), name normalization
    routing.py       OSRM client (+ OpenRouteService fallback), disk cache
    spatial.py       station KD-tree, corridor search, mile-marker projection
    optimizer.py     full_tank DP, exact_gallon greedy, tank/cost accounting
    errors.py        error types → HTTP status codes
  management/commands/geocode_stations.py   offline geocoding
  views.py           /api/route/, /api/map/, /api/health/
  templates/, static/   Leaflet map UI
  tests.py           brute-force optimality tests + edge cases
spotter_project/settings.py   all constants (vehicle, corridor, caching, APIs)
data/                CSV + generated station and gazetteer JSON
docs/postman_collection.json
```
