# Fuel Route Planner API

A Django REST API that takes a start and finish location in the USA and returns the
driving route, the most cost-effective places to buy diesel along it, and the total
fuel cost — for a vehicle with a 500-mile range that gets 10 miles per gallon.

- **One routing API call per new route** (OSRM, free, no key). Repeat routes: zero.
- **~20–60 ms** for a repeat request; **0.5–2 s** for a new route, almost all of it the OSRM call.
- **Provably optimal** fuel plans, checked against brute-force search in the test suite.
- **US-only**: places and coordinates outside the USA are rejected with a clear 400.
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

The geocoded station data is committed, so there is **no data-preparation step**, and
the API is stateless, so there is **no database and no `migrate`**.
To rebuild the data from the CSV: `python manage.py geocode_stations` (≈3.5 min, see below).

### Configuration

Nothing is required for local use. For a real deployment set:

| Variable | Default | Purpose |
|---|---|---|
| `DJANGO_DEBUG` | `True` | Set `False` in production |
| `DJANGO_SECRET_KEY` | dev-only key | **Required** when `DJANGO_DEBUG=False` (startup fails without it) |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1]` | Comma-separated host names |
| `CORS_ALLOWED_ORIGINS` | — | Comma-separated origins (all origins are allowed while `DEBUG` is on) |
| `API_THROTTLE_RATE` | `1000/minute` | Per-IP limit, protecting the free OSRM/Nominatim servers |
| `OPENROUTESERVICE_API_KEY` | — | Optional routing fallback if OSRM is down |
| `HTTP_USER_AGENT` | `spotter-fuel-route-planner/1.0` | Sent to OSRM and Nominatim, as their usage policies require |

No API keys or secrets are committed.

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
    "total_fuel_cost_usd": 891.64,
    "total_gallons": 281.04,
    "fuel_stop_count": 6,
    "cost_at_stops_usd": 738.11,
    "stops": [
      {
        "stop_number": 1, "station_name": "SHEETZ #639", "address": "I-80 Exit 223",
        "city": "Youngstown", "state": "OH", "lat": 41.099, "lon": -80.646,
        "mile_marker": 396.0, "off_route_miles": 3.8, "price_per_gallon": 3.059,
        "gallons_in_tank_on_arrival": 10.4, "gallons_purchased": 39.6, "cost_at_stop": 121.14
      }
    ],
    "cost_breakdown": {
      "starting_tank_price_per_gallon": 3.409,
      "starting_tank_price_source": "DUNCAN TRUCK STOP, Jersey City, NJ (7 mi from start)",
      "starting_tank_cost_usd": 170.45,
      "cost_at_stops_usd": 738.11,
      "ending_tank_gallons": 4.96,
      "ending_tank_value_usd": 16.92
    }
  },
  "strategy_comparison": {
    "full_tank":    { "total_fuel_cost_usd": 891.64, "cost_at_stops_usd": 738.11, "fuel_stop_count": 6 },
    "exact_gallon": { "total_fuel_cost_usd": 869.91, "cost_at_stops_usd": 709.21, "fuel_stop_count": 15 }
  },
  "timing": { "refueling_halting_minutes": 120, "total_trip_duration_hours": 52.3 },
  "performance": { "algorithm_computation_ms": 1.1, "corridor_stations_considered": 232, "external_api_calls": 0 },
  "map_url": "http://127.0.0.1:8000/api/map/?start=New+York%2C+NY&finish=Los+Angeles%2C+CA&strategy=full_tank&stop_minutes=20",
  "map_data": {
    "route_geometry": { "type": "LineString", "coordinates": [[-74.006, 40.713], "..."] },
    "fuel_stop_markers": [{ "lat": 41.099, "lon": -80.646, "popup": "SHEETZ #639 ..." }]
  }
}
```

**`total_fuel_cost_usd` is the cost of all the fuel the trip burns**:
`total_gallons` (= miles ÷ 10) at what that fuel cost. A 196-mile Austin → Dallas trip
costs $57.46 (19.6 gal at $2.932) even though no stop is needed. It adds up exactly:

```
total_fuel_cost_usd = starting_tank_cost_usd + cost_at_stops_usd − ending_tank_value_usd
         891.64     =        170.45          +      738.11       −        16.92
```

- The vehicle leaves with a full tank priced at the station nearest the start
  (`starting_tank_price_source`).
- `cost_at_stops_usd` is the money actually handed over at the planned stops.
- Fuel left in the tank on arrival is not part of this trip's cost; it is valued at the
  tank's weighted-average price.

Every stop reads like a fuel receipt: price and gallons to 3 decimals, cost to the cent,
and `gallons_purchased × price_per_gallon = cost_at_stop` exactly.

**Errors** return `{"error": "...", "detail": "..."}`:

| Status | When |
|---|---|
| 400 | Invalid parameters, unknown location, location outside the USA (`"Toronto, ON"`, `"51.5,-0.12"`), no drivable route, or a stretch longer than the vehicle's range with no station (e.g. Seattle → Anchorage through Canada) |
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
                Nominatim only for addresses/landmarks; results cached on disk.
                Coordinates and Nominatim results must fall inside the US border
                (offline point-in-polygon), else 400 before any routing call
   ▼
2. Route        ONE OSRM call → road geometry, distance, duration; cached on disk for 7 days
                (identical start and finish: no call at all)
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
| Routing (OSRM) | 400–1,800 ms, 1 call | ~5 ms (disk cache), 0 calls |
| Corridor search | ~10 ms | ~10 ms |
| Optimizer | ~1 ms | ~1 ms |
| **Total** | **0.5–2 s** | **~15 ms in-process, ~20–60 ms over HTTP** |

An address or landmark input adds one Nominatim call per location (also cached), so the
worst case is 3 external calls; "City, ST" and `lat,lon` inputs need none.

### Why OSRM

It is the only source of road knowledge: which stations the truck actually passes,
the road distance to each (what the 500-mile range is checked against), driving time,
and the line drawn on the map. The public demo server is free and needs no key.
If it fails and `OPENROUTESERVICE_API_KEY` is set, OpenRouteService is used as a fallback.

---

## Algorithms

After the corridor search the route is just a line. The optimizer gets:

| Symbol | Meaning |
|---|---|
| `d[i]` | mile marker of station `i` along the route, sorted (`d[0] = 0` is the start) |
| `p[i]` | diesel price at station `i` ($/gal) |
| `T` | total route length (miles) |
| `R` | range per tank: **470 miles** (500-mile tank minus a 30-mile safety reserve that is never planned to be burned) |
| `mpg` | 10 |

The truck leaves the start with a full tank. A trip with `T ≤ 470` needs no stops.

### `full_tank` (default) — dynamic programming

Every stop fills the tank completely, like a real truck stop. The goal, in priority order:

1. **As few stops as possible.** Every stop costs the driver time.
2. **Among plans with that many stops, the lowest fuel cost.**

**Key observation:** if you always fill up, then at stop `i` you buy exactly the fuel you
burned since your previous stop `j`, which is `(d[i] − d[j]) / mpg` gallons at `p[i]`. So
the cost of a stop depends only on the previous stop, which makes this a clean DP.

**State:** each `dp[i]` is a pair, compared by stops first and then by cost:

```
dp[i] = (stops, cost) of the best plan whose latest stop is station i (tank full there)
```

**Base case:** `dp[0] = (0 stops, $0)`, at the start with a full tank and nothing bought yet.

**Transition:** to stop at `i`, the previous stop `j` must be within one tank (`d[i] − d[j] ≤ 470`):

```
dp[i] = min over those j of  ( stops[j] + 1 ,  cost[j] + (d[i] − d[j]) / mpg × p[i] )
                                ────────────    ───────    ──────────────────────────
                                one more stop   cost to    fuel bought at i to refill
                                                reach j
```

"min" picks the fewest stops, and breaks ties by the lower cost. `parent[i]` remembers
which `j` won.

**Answer:** the last stop must be within one tank of the destination (`T − d[i] ≤ 470`):

```
best = min over those i of dp[i]
```

Following `parent[]` back from that `i` to the start gives the stops in order.

**Worked example.** `T = 900` miles, four stations:

| Station | `d` | `p` |
|---|---|---|
| A | 300 | $2.00 |
| B | 440 | $3.50 |
| C | 460 | $3.00 |
| D | 600 | $2.50 |

```
dp[start] = (0, $0)
dp[A] = start → A: (1, 300/10 × $2.00 = $60)                      = (1, $60)
dp[B] = min( start → B: (1, 440/10 × $3.50 = $154),
             A → B:     (2, 60 + 140/10 × $3.50 = $109) )         = (1, $154)  fewer stops wins
dp[C] = min( start → C: (1, 460/10 × $3.00 = $138),
             A → C:     (2, 60 + 160/10 × $3.00 = $108) )         = (1, $138)
dp[D] = start → D is 600 mi > 470, not allowed
        min( A → D: (2, 60  + 300/10 × $2.50 = $135),
             B → D: (2, 154 + 160/10 × $2.50 = $194),
             C → D: (2, 138 + 140/10 × $2.50 = $173) )            = (2, $135)  (parent A)

The last stop must be at mile ≥ 900 − 470 = 430, so B, C or D:
best = min( B (1, $154),  C (1, $138),  D (2, $135) ) = C  →  one stop at C, $138
```

B and C both need just one stop, so the cheaper one, C, wins. Plan D is $3 cheaper, but it
needs a second stop. A cheap stop that isn't needed is never added.

**Details:**
- **Speed:** a real corridor has ~200 stations, and each `dp[i]` only looks back at the
  stations within 470 miles. That window minimum is one vectorised NumPy call, so the
  whole DP takes about 1 ms.

### `exact_gallon` — greedy

Here you may buy **any amount** at a stop, not just a full tank. This is the classic
greedy from Khuller, Malekian & Mestre, *"To Fill or Not to Fill: The Gas Station
Problem"* (ACM TALG 2011), which is proven optimal on a fixed route. At each stop:

1. **A cheaper station is within range:** buy just enough fuel to reach the **first**
   cheaper one, and fill up properly there.
2. **No cheaper station is within range:** this is the cheapest fuel you will see for a
   while, so fill the tank and drive to the cheapest station within range.

The destination counts as a station with price $0. It is always "cheaper", so the truck
never buys fuel it will still be carrying on arrival.

**Worked example** (prices falling along the route; `T = 1,500`):

| Station | `d` | `p` | Why | Buys |
|---|---|---|---|---|
| A | 400 | $4.00 | B is cheaper and 400 mi ahead: buy just enough to reach B. Arrived with 70 mi of usable fuel, so needs 330 mi more. | 33 gal |
| B | 800 | $3.50 | C is cheaper and in range: buy just enough to reach C. | 40 gal |
| C | 1,200 | $3.00 | The destination (300 mi) counts as cheaper: buy just enough to finish. | 30 gal |

Total: 33 × $4.00 + 40 × $3.50 + 30 × $3.00 = **$362**, and the truck arrives with only
the 3-gallon safety reserve left.

### Comparing the two

The two strategies are the two ends of a trade-off, and `strategy_comparison` shows both in
every response:

| Route | `full_tank`: fewest stops | `exact_gallon`: cheapest fuel |
|---|---|---|
| New York → Los Angeles | **6 stops**, $738.11 at stops | 15 stops, $709.21 |
| Chicago → Dallas | **2 stops**, $153.02 | 5 stops, $142.04 |
| Seattle → Miami | **7 stops**, $883.24 | 18 stops, $859.79 |

`exact_gallon` is the proven minimum fuel cost, but it gets there with many small top-ups.
`full_tank` gives a practical plan with 3–4% higher fuel cost.

**Correctness** (`python manage.py test router_app`, 31 tests, ~1 s, no network):

- `tests/test_optimizer.py` — both strategies against exhaustive search on 400 random
  instances each (`full_tank`: fewest stops, then cheapest), plus the trip-cost identity checked against an independent
  mile-by-mile costing.
- `tests/test_api.py` — the endpoint end to end with OSRM mocked. It checks exactly one
  routing call for a new route and none for a repeat, plan feasibility and ordering,
  receipts and the cost identity, and every error status.
- `tests/test_locations.py` — name parsing, the US border (Detroit/Windsor,
  San Ysidro/Tijuana, ...), and the geocoder's fallbacks and caching.

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

Output: `data/stations_geocoded.json` (stations), `data/us_places.json`
(the gazetteer, also used to geocode `start`/`finish` without a network call) and
`data/us_boundary.json` (the Census 1:5M national boundary, simplified to ~0.5 km,
used to reject locations outside the USA).

---

## Assumptions and limitations

- Stations are placed at their **city's coordinates**, not the exact highway exit
  (the CSV only has city and exit text). The 10-mile corridor absorbs most of this;
  each stop reports `off_route_miles`.
- Prices for stations listed more than once use the **minimum** price (597 station IDs
  appear with different prices, typically under different names or rack IDs).
- Detour distance to reach a station is not added to fuel use.
- Free-text locations are resolved by Nominatim *without* a US filter, so a foreign place
  is rejected rather than matched to a similarly named US street. An ambiguous bare name
  ("Paris") may therefore resolve abroad; the error asks for a state ("Paris, TX").
- Census "internal points" for a few cities whose limits include water or islands sit
  offshore (San Francisco, Portland ME). They are accepted, and OSRM snaps them to the
  nearest road.
- The public OSRM server is rate-limited and has no SLA; route caching keeps calls rare.
- Caches are per machine (`data/cache/`); use Redis in production.

---

## Project layout

```
router_app/
  services/
    geocoding.py     start/finish geocoding (gazetteer → Nominatim), name normalization
    boundary.py      offline "is this point in the USA?" check
    routing.py       OSRM client (+ OpenRouteService fallback), disk cache
    spatial.py       station KD-tree, corridor search, mile-marker projection
    optimizer.py     full_tank DP, exact_gallon greedy, tank/cost accounting
    errors.py        error types → HTTP status codes
  management/commands/geocode_stations.py   offline geocoding
  views.py           /api/route/, /api/map/, /api/health/
  templates/, static/   Leaflet map UI
  tests/             optimizer (brute force), API (OSRM mocked), locations
spotter_project/settings.py   all constants (vehicle, corridor, caching, APIs) + env config
data/                CSV + generated station, gazetteer and US-boundary JSON
docs/postman_collection.json
```
