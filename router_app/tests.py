"""Tests for the optimizer (against brute force) and text normalisation."""
from __future__ import annotations

import itertools
import random

import numpy as np
from django.test import SimpleTestCase

from .management.commands.geocode_stations import clean_gazetteer_name
from .services.errors import NoFeasiblePlanError
from .services.geocoding import _parse_city_state, normalize_place
from .services.optimizer import (
    FuelPlan, Vehicle, optimize, plan_exact_gallon, plan_full_tank, simulate_tank,
)


def brute_exact(markers: list[int], prices: list[float], total: int, R: int) -> float:
    """Exact min cost by DP over integer fuel levels (mpg = 1)."""
    d = [0] + markers + [total]
    inf = float('inf')
    # best[f] = min cost to be at node i with f units of fuel before buying
    best = {R: 0.0}
    for i in range(len(d) - 1):
        gap = d[i + 1] - d[i]
        price = prices[i - 1] if i > 0 else None
        nxt: dict[int, float] = {}
        for f, cost in best.items():
            targets = range(f, R + 1) if price is not None else [f]
            for t in targets:
                if t < gap:
                    continue
                c = cost + (t - f) * (price or 0)
                if c < nxt.get(t - gap, inf):
                    nxt[t - gap] = c
        best = nxt
        if not best:
            return inf
    return min(best.values())


def brute_full_tank(markers: list[int], prices: list[float], total: int,
                    R: int) -> tuple[int, float]:
    """Exhaustive (stops, cost) minimum over all stop subsets (mpg = 1)."""
    best = (10 ** 9, float('inf'))
    n = len(markers)
    for k in range(n + 1):
        for subset in itertools.combinations(range(n), k):
            pos, cost, ok = 0, 0.0, True
            for i in subset:
                if markers[i] - pos > R:
                    ok = False
                    break
                cost += (markers[i] - pos) * prices[i]
                pos = markers[i]
            if ok and total - pos <= R:
                best = min(best, (k, round(cost, 9)))
    return best


class OptimizerBruteForceTests(SimpleTestCase):
    """Both strategies must match exhaustive search on random instances."""

    vehicle = Vehicle(tank_gallons=10, mpg=1, usable_range_miles=10)

    def _instances(self, count: int, max_stations: int):
        rng = random.Random(42)
        for _ in range(count):
            total = rng.randint(11, 45)
            k = rng.randint(1, max_stations)
            markers = sorted(rng.randint(0, total) for _ in range(k))
            prices = [round(rng.uniform(2.5, 4.5), 2) for _ in range(k)]
            yield markers, prices, total

    def test_exact_gallon_is_optimal(self) -> None:
        checked = 0
        for markers, prices, total in self._instances(400, 9):
            expected = brute_exact(markers, prices, total, 10)
            m, p = np.array(markers, float), np.array(prices, float)
            if expected == float('inf'):
                with self.assertRaises(NoFeasiblePlanError):
                    plan_exact_gallon(m, p, total, self.vehicle)
                continue
            got = sum(x.cost for x in plan_exact_gallon(m, p, total, self.vehicle))
            self.assertAlmostEqual(got, expected, places=6,
                                   msg=f'{markers} {prices} {total}')
            checked += 1
        self.assertGreater(checked, 100)

    def test_full_tank_is_lexicographically_optimal(self) -> None:
        checked = 0
        for markers, prices, total in self._instances(400, 8):
            stops, cost = brute_full_tank(markers, prices, total, 10)
            m, p = np.array(markers, float), np.array(prices, float)
            if stops == 10 ** 9:
                with self.assertRaises(NoFeasiblePlanError):
                    plan_full_tank(m, p, total, self.vehicle)
                continue
            plan = plan_full_tank(m, p, total, self.vehicle)
            self.assertEqual(len(plan), stops)
            self.assertAlmostEqual(sum(x.cost for x in plan), cost, places=6)
            checked += 1
        self.assertGreater(checked, 100)

    def test_exact_never_costs_more_than_full_tank(self) -> None:
        for markers, prices, total in self._instances(200, 8):
            m, p = np.array(markers, float), np.array(prices, float)
            try:
                full = sum(x.cost for x in plan_full_tank(m, p, total, self.vehicle))
            except NoFeasiblePlanError:
                continue
            exact = sum(x.cost for x in plan_exact_gallon(m, p, total, self.vehicle))
            self.assertLessEqual(exact, full + 1e-9)


class OptimizerEdgeCaseTests(SimpleTestCase):
    """Behaviour with real vehicle parameters."""

    vehicle = Vehicle(tank_gallons=50, mpg=10, usable_range_miles=470)

    def test_short_route_needs_no_stops(self) -> None:
        for solver in (plan_full_tank, plan_exact_gallon):
            self.assertEqual(solver(np.array([100.0]), np.array([3.0]), 400, self.vehicle), [])

    def test_gap_longer_than_range_is_reported(self) -> None:
        with self.assertRaises(NoFeasiblePlanError) as ctx:
            plan_full_tank(np.array([100.0, 700.0]), np.array([3.0, 3.0]), 900, self.vehicle)
        self.assertIn('mile 100', ctx.exception.detail)

    def test_exact_gallon_price_gradient(self) -> None:
        # Prices fall along the route: buy only the minimum at each stop.
        markers = np.array([400.0, 800.0, 1200.0])
        prices = np.array([4.0, 3.5, 3.0])
        plan = plan_exact_gallon(markers, prices, 1500, self.vehicle)
        self.assertEqual([round(x.gallons, 6) for x in plan], [33.0, 40.0, 30.0])
        # Reserve is kept: never arrive with less than 3 gallons.
        fp = FuelPlan('exact_gallon', plan, 1500, self.vehicle)
        simulate_tank(fp, 3.5)
        self.assertGreaterEqual(min(fp.arrival_gallons), 3.0 - 1e-9)
        self.assertAlmostEqual(fp.ending_tank_gallons, 3.0, places=6)

    def test_full_tank_prefers_fewer_stops(self) -> None:
        # One stop at a pricey station beats two stops at cheap ones.
        markers = np.array([300.0, 450.0, 600.0])
        prices = np.array([3.0, 5.0, 3.0])
        plan = plan_full_tank(markers, prices, 900, self.vehicle)
        self.assertEqual([x.mile_marker for x in plan], [450.0])


class BillingConsistencyTests(SimpleTestCase):
    """Displayed gallons × displayed price must equal displayed cost."""

    def test_receipts_multiply_out_and_sum(self) -> None:
        rng = random.Random(7)
        markers = np.sort(np.array([rng.uniform(0, 2800) for _ in range(120)]))
        prices = np.array([rng.uniform(2.7, 4.2) for _ in range(120)])  # unrounded
        for strategy in ('full_tank', 'exact_gallon'):
            plan = optimize(strategy, markers, prices, 2800.0, 3.5)
            self.assertGreater(plan.stop_count, 0)
            for p in plan.purchases:
                self.assertEqual(p.price, round(p.price, 3))
                self.assertEqual(p.gallons, round(p.gallons, 3))
                self.assertEqual(p.billed_cost, round(p.gallons * p.price + 1e-9, 2))
            self.assertAlmostEqual(plan.total_cost,
                                   sum(p.billed_cost for p in plan.purchases), places=9)


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
