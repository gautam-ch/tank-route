"""Fuel-stop optimisation on a 1D route.

Input: stations sorted by road mile-marker, their prices, the route length and
the vehicle. Fuel is measured in *usable* miles: the tank holds
``VEHICLE_TANK_GALLONS`` but ``SAFETY_RESERVE_MILES`` worth is never planned
to be burned, so the usable range per tank is ``EFFECTIVE_RANGE_MILES`` (470).
The vehicle starts with a full tank.

Strategies
----------
full_tank
    Every stop fills the tank completely (realistic truck-stop behaviour).
    The plan uses the **fewest stops** possible and, among all plans with that
    many stops, the **lowest cost**. Exact DP over stations (vectorised window
    minimum, O(K·W)). ``dp[i]`` is the best (stops, cost) pair for a plan
    whose latest stop is station ``i``, compared stops first, then cost.
    Filling up at ``i`` after stopping at ``j`` buys exactly the fuel burned
    since ``j``:

    dp[0] = (0, $0)                             (start, tank full)
    dp[i] = min_{j : d_i - d_j <= R} (stops_j + 1, cost_j + (d_i - d_j) / mpg * p_i)
    answer = min_{i : total - d_i <= R} dp[i]

exact_gallon
    Buy any amount at each stop. Minimum total cost, solved exactly by the
    classic greedy from Khuller, Malekian & Mestre ("To Fill or Not to Fill",
    ACM TALG 2011) for a fixed path:

    * If a strictly cheaper station is within one tank's range, buy just enough
      to reach the *first* such station.
    * Otherwise fill up here and drive to the cheapest station within range.

    The destination is a node with price 0 so no fuel is bought that would
    still be in the tank on arrival.

Trip cost
---------
The vehicle starts with a full tank bought at the origin. Fuel in the tank is
valued at its weighted-average cost, so for every plan::

    trip fuel cost = starting tank + purchases at stops - fuel left on arrival

which is exactly the cost of the ``total_miles / mpg`` gallons burned.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np
from django.conf import settings

from .errors import NoFeasiblePlanError

EPS = 1e-9
BILLING_DECIMALS = 3  # pumps price and meter fuel to 1/1000


@dataclass(frozen=True)
class Vehicle:
    """Vehicle fuel parameters."""

    tank_gallons: float
    mpg: float
    usable_range_miles: float

    @classmethod
    def from_settings(cls) -> 'Vehicle':
        """Build from Django settings constants."""
        return cls(
            tank_gallons=float(settings.VEHICLE_TANK_GALLONS),
            mpg=float(settings.VEHICLE_MPG),
            usable_range_miles=float(settings.EFFECTIVE_RANGE_MILES),
        )


@dataclass(frozen=True)
class Purchase:
    """Fuel bought at one corridor station."""

    node: int            # index into the corridor arrays
    mile_marker: float
    price: float
    gallons: float

    @property
    def cost(self) -> float:
        """Money spent at this stop."""
        return self.gallons * self.price

    def billed(self) -> 'Purchase':
        """Round to pump precision: $/gal to 3 decimals, gallons to 3 decimals.

        Applied after optimisation so displayed gallons × displayed price
        reproduces the stop cost exactly (see ``FuelPlan.cost_at_stops``).
        """
        return Purchase(self.node, self.mile_marker,
                        round(self.price, BILLING_DECIMALS),
                        round(self.gallons, BILLING_DECIMALS))

    @property
    def billed_cost(self) -> float:
        """Stop cost rounded to the cent, as on a fuel receipt."""
        return round(self.gallons * self.price + EPS, 2)


@dataclass
class FuelPlan:
    """Result of one strategy."""

    strategy: str
    purchases: list[Purchase]
    total_miles: float
    vehicle: Vehicle
    computation_ms: float = 0.0
    arrival_gallons: list[float] = field(default_factory=list)
    starting_tank_price: float = 0.0
    ending_tank_gallons: float = 0.0
    ending_tank_price: float = 0.0  # weighted-average cost of the fuel left

    @property
    def stop_count(self) -> int:
        """Number of fuel stops."""
        return len(self.purchases)

    @property
    def gallons_burned(self) -> float:
        """Fuel used to drive the whole route."""
        return self.total_miles / self.vehicle.mpg

    @property
    def gallons_at_stops(self) -> float:
        """Gallons bought on the trip (excludes the initial full tank)."""
        return sum(p.gallons for p in self.purchases)

    @property
    def cost_at_stops(self) -> float:
        """Money spent at fuel stops: the sum of the per-stop receipts."""
        return round(sum(p.billed_cost for p in self.purchases), 2)

    @property
    def starting_tank_cost(self) -> float:
        """Cost of the full tank the vehicle leaves with."""
        return round(self.vehicle.tank_gallons * self.starting_tank_price + EPS, 2)

    @property
    def ending_tank_value(self) -> float:
        """Value of the fuel still in the tank at the destination."""
        return round(self.ending_tank_gallons * self.ending_tank_price + EPS, 2)

    @property
    def trip_fuel_cost(self) -> float:
        """Cost of all fuel burned on the trip (see module docstring)."""
        return round(self.starting_tank_cost + self.cost_at_stops
                     - self.ending_tank_value, 2)


def assert_feasible(markers: np.ndarray, total_miles: float, range_miles: float) -> None:
    """Raise NoFeasiblePlanError if any gap between fuel points exceeds range."""
    points = np.concatenate(([0.0], markers, [total_miles]))
    gaps = np.diff(points)
    if gaps.size and gaps.max() > range_miles + EPS:
        k = int(np.argmax(gaps))
        raise NoFeasiblePlanError(
            'Cannot find a route: no fuel stations within range of a segment.',
            f'No station between mile {points[k]:.0f} and mile {points[k + 1]:.0f} '
            f'({gaps[k]:.0f} miles, usable range is {range_miles:.0f} miles).',
        )


def _fewest_then_cheapest(stops: np.ndarray, cost: np.ndarray) -> int:
    """Index of the (stops, cost) pair that is smallest: fewest stops, then cheapest."""
    fewest = stops.min()
    return int(np.argmin(np.where(stops == fewest, cost, np.inf)))


def plan_full_tank(markers: np.ndarray, prices: np.ndarray, total_miles: float,
                   vehicle: Vehicle) -> list[Purchase]:
    """Fewest fill-up stops; among plans with that many stops, the cheapest."""
    R, mpg = vehicle.usable_range_miles, vehicle.mpg
    if total_miles <= R + EPS:
        return []
    assert_feasible(markers, total_miles, R)

    d = np.concatenate(([0.0], markers))          # node 0 = start (full tank)
    p = np.concatenate(([0.0], prices))
    n = len(d)
    # dp[i] = (stops[i], cost[i]): the best plan whose latest stop is station i
    # (tank full there) — fewest stops first, then lowest cost.
    # parent[i] = the stop before i in that plan (0 = the start).
    stops = np.full(n, np.inf)
    cost = np.full(n, np.inf)
    stops[0], cost[0] = 0, 0.0
    parent = np.full(n, -1, dtype=int)
    # First node within one tank's range behind each node (d is sorted).
    window_lo = np.searchsorted(d, d - R - EPS, side='left')

    for i in range(1, n):
        lo = int(window_lo[i])
        if lo >= i or not np.isfinite(stops[lo:i].min()):
            continue
        # From j, stopping at i costs cost[j] + (d[i] - d[j]) / mpg * p[i].
        # Written as (cost[j] - d[j] * rate) + d[i] * rate so every j in the
        # window is compared in one vectorised NumPy call.
        rate = p[i] / mpg
        m = lo + _fewest_then_cheapest(stops[lo:i], cost[lo:i] - d[lo:i] * rate)
        stops[i] = stops[m] + 1
        cost[i] = cost[m] + (d[i] - d[m]) * rate
        parent[i] = m

    # Answer: the best last stop that can still reach the destination.
    lo = int(np.searchsorted(d, total_miles - R - EPS, side='left'))
    if not np.isfinite(stops[lo:].min()):
        raise NoFeasiblePlanError('Cannot find a route: no feasible fuel plan.')
    last = lo + _fewest_then_cheapest(stops[lo:], cost[lo:])

    # Walk parent[] back from the last stop to recover the stops in order.
    path: list[int] = []
    node = last
    while node > 0:
        path.append(node)
        node = int(parent[node])
    path.reverse()

    purchases, prev = [], 0
    for node in path:
        purchases.append(Purchase(node - 1, float(d[node]), float(p[node]),
                                  float((d[node] - d[prev]) / mpg)))
        prev = node
    return purchases


def _next_cheaper(p: np.ndarray) -> np.ndarray:
    """For each node, index of the first later node with a strictly lower price."""
    nxt = np.full(len(p), -1, dtype=int)
    stack: list[int] = []
    for i in range(len(p) - 1, -1, -1):
        while stack and p[stack[-1]] >= p[i]:
            stack.pop()
        nxt[i] = stack[-1] if stack else -1
        stack.append(i)
    return nxt


def plan_exact_gallon(markers: np.ndarray, prices: np.ndarray, total_miles: float,
                      vehicle: Vehicle) -> list[Purchase]:
    """Minimum-cost plan with variable fill amounts (Khuller et al. greedy)."""
    R, mpg = vehicle.usable_range_miles, vehicle.mpg
    if total_miles <= R + EPS:
        return []
    assert_feasible(markers, total_miles, R)

    # Node 0 = start (free full tank, cannot buy), last node = destination.
    d = np.concatenate(([0.0], markers, [total_miles]))
    p = np.concatenate(([-np.inf], prices, [0.0]))
    dest = len(d) - 1
    nxt = _next_cheaper(p)

    purchases: list[Purchase] = []
    cur, fuel = 0, R  # fuel in usable miles

    def buy(node: int, miles: float) -> None:
        if miles > EPS:
            purchases.append(Purchase(node - 1, float(d[node]), float(p[node]),
                                      miles / mpg))

    while cur != dest:
        reach = d[cur] + R
        j = int(nxt[cur])
        if j != -1 and d[j] <= reach + EPS:
            # Case 1: cheaper station in range — buy only enough to get there.
            need = (d[j] - d[cur]) - fuel
            if need > EPS:
                buy(cur, need)
                fuel += need
        else:
            # Case 2: this is the cheapest in range — fill up, go to the
            # cheapest reachable station (farthest one on ties).
            hi = int(np.searchsorted(d, reach + EPS, side='right'))
            if hi <= cur + 1:
                raise NoFeasiblePlanError(
                    'Cannot find a route: no fuel stations within range of a segment.',
                    f'Stranded after mile {d[cur]:.0f}.')
            window = p[cur + 1:hi]
            j = cur + 1 + int(np.flatnonzero(window == window.min())[-1])
            if cur != 0:
                buy(cur, R - fuel)
            fuel = R
        fuel -= d[j] - d[cur]
        cur = j
    return purchases


def simulate_tank(plan: FuelPlan, starting_tank_price: float) -> None:
    """Fill in tank levels and the average cost of the fuel left at the end.

    Weighted-average inventory costing: the initial full tank is valued at
    ``starting_tank_price`` and each purchase blends into the tank's average.
    """
    v = plan.vehicle
    starting_tank_price = round(starting_tank_price, BILLING_DECIMALS)
    tank, avg, pos = v.tank_gallons, starting_tank_price, 0.0
    arrivals: list[float] = []
    for purchase in plan.purchases:
        tank -= (purchase.mile_marker - pos) / v.mpg
        arrivals.append(tank)
        if purchase.gallons > 0:
            avg = (avg * tank + purchase.price * purchase.gallons) / (tank + purchase.gallons)
            tank += purchase.gallons
        pos = purchase.mile_marker
    tank -= (plan.total_miles - pos) / v.mpg

    plan.arrival_gallons = arrivals
    plan.starting_tank_price = starting_tank_price
    plan.ending_tank_gallons = tank
    plan.ending_tank_price = avg


STRATEGIES = {
    'full_tank': plan_full_tank,
    'exact_gallon': plan_exact_gallon,
}


def optimize(strategy: str, markers: np.ndarray, prices: np.ndarray,
             total_miles: float, starting_tank_price: float,
             vehicle: Vehicle | None = None) -> FuelPlan:
    """Run one strategy, timing only the algorithm, then simulate the tank."""
    vehicle = vehicle or Vehicle.from_settings()
    solver = STRATEGIES[strategy]
    started = time.perf_counter()
    purchases = solver(np.asarray(markers, float), np.asarray(prices, float),
                       float(total_miles), vehicle)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    purchases = [p.billed() for p in purchases]
    plan = FuelPlan(strategy, purchases, float(total_miles), vehicle, elapsed_ms)
    simulate_tank(plan, starting_tank_price)
    return plan
