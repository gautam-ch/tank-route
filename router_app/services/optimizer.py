"""Fuel-stop optimisation on a 1D route.

Input: stations sorted by road mile-marker, their prices, the route length and
the vehicle. Fuel is measured in *usable* miles: the tank holds
``VEHICLE_TANK_GALLONS`` but ``SAFETY_RESERVE_MILES`` worth is never planned
to be burned, so the usable range per tank is ``EFFECTIVE_RANGE_MILES`` (470).
The vehicle starts with a full tank.

Strategies
----------
full_tank
    Every stop fills the tank completely. Objective is lexicographic:
    **fewest stops first, then lowest cost**. Solved exactly with a DP over
    stations (vectorised window minimum, O(K·W)).

    key[i] = min_{j : d_i - d_j <= R} key[j] + BIG + (d_i - d_j) / mpg * p_i

    where BIG exceeds any possible fuel bill, so one extra stop always costs
    more than any price saving.

exact_gallon
    Buy any amount at each stop. Minimum total cost, solved exactly by the
    classic greedy from Khuller, Malekian & Mestre ("To Fill or Not to Fill",
    ACM TALG 2011) for a fixed path:

    * If a strictly cheaper station is within one tank's range, buy just enough
      to reach the *first* such station.
    * Otherwise fill up here and drive to the cheapest station within range.

    The destination is a node with price 0 so no fuel is bought that would
    still be in the tank on arrival.
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
        reproduces the stop cost exactly (see ``FuelPlan.total_cost``).
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
    consumed_gallons: float = 0.0
    consumed_cost: float = 0.0
    ending_tank_gallons: float = 0.0
    starting_tank_price: float = 0.0

    @property
    def stop_count(self) -> int:
        """Number of fuel stops."""
        return len(self.purchases)

    @property
    def total_gallons(self) -> float:
        """Gallons bought on the trip (excludes the initial full tank)."""
        return sum(p.gallons for p in self.purchases)

    @property
    def total_cost(self) -> float:
        """Money spent at fuel stops: the sum of the per-stop receipts."""
        return round(sum(p.billed_cost for p in self.purchases), 2)


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


def plan_full_tank(markers: np.ndarray, prices: np.ndarray, total_miles: float,
                   vehicle: Vehicle) -> list[Purchase]:
    """Fewest stops, then cheapest, when every stop fills the tank."""
    R, mpg = vehicle.usable_range_miles, vehicle.mpg
    if total_miles <= R + EPS:
        return []
    assert_feasible(markers, total_miles, R)

    d = np.concatenate(([0.0], markers))          # node 0 = start (full tank)
    p = np.concatenate(([0.0], prices))
    n = len(d)
    big = total_miles / mpg * float(prices.max()) * 10.0 + 1.0
    key = np.full(n, np.inf)
    key[0] = 0.0
    back = np.full(n, -1, dtype=int)
    window_lo = np.searchsorted(d, d - R - EPS, side='left')

    for i in range(1, n):
        lo = int(window_lo[i])
        if lo >= i:
            continue
        rate = p[i] / mpg
        cand = key[lo:i] - d[lo:i] * rate
        m = int(np.argmin(cand))
        if np.isfinite(cand[m]):
            key[i] = cand[m] + d[i] * rate + big
            back[i] = lo + m

    lo = int(np.searchsorted(d, total_miles - R - EPS, side='left'))
    last = lo + int(np.argmin(key[lo:]))
    if not np.isfinite(key[last]):
        raise NoFeasiblePlanError('Cannot find a route: no feasible fuel plan.')

    path: list[int] = []
    node = last
    while node > 0:
        path.append(node)
        node = int(back[node])
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
    """Fill in tank levels and the cost of fuel actually *consumed*.

    Uses weighted-average inventory costing: the initial full tank is valued
    at ``starting_tank_price``; each purchase blends into the tank's average
    cost; burned fuel is charged at the running average.
    """
    v = plan.vehicle
    tank, avg, pos, consumed_cost = v.tank_gallons, starting_tank_price, 0.0, 0.0
    arrivals: list[float] = []
    for purchase in plan.purchases:
        used = (purchase.mile_marker - pos) / v.mpg
        consumed_cost += used * avg
        tank -= used
        arrivals.append(tank)
        if purchase.gallons > 0:
            avg = (avg * tank + purchase.price * purchase.gallons) / (tank + purchase.gallons)
            tank += purchase.gallons
        pos = purchase.mile_marker
    used = (plan.total_miles - pos) / v.mpg
    consumed_cost += used * avg
    tank -= used

    plan.arrival_gallons = arrivals
    plan.consumed_gallons = plan.total_miles / v.mpg
    plan.consumed_cost = consumed_cost
    plan.ending_tank_gallons = tank
    plan.starting_tank_price = starting_tank_price


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
