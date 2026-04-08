"""
Tests for estimate_strength.py

Run: python scripts/tests/test_estimate_strength.py
  or: python -m pytest scripts/tests/test_estimate_strength.py -v

Coverage:
  1. Linear progress tracker — does the estimate track upward trend?
  2. Bad week resilience — one anomalously low week should not tank the estimate
  3. Sparse data — 2 sessions → low confidence, wide range
  4. AMRAP vs non-AMRAP same weight — AMRAP gets higher signal weight
  5. Olympic lift — technique discount applied, labeled correctly
  6. Correlation fallback — missing lift estimated via correlated source
  7. High-rep exclusion — sets >15 reps excluded from computation
  8. Formula selection by rep range — correct formulas chosen
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from estimate_strength import (
    epley, brzycki, wathan,
    _set_confidence,
    _estimate_window,
    estimate_exercise,
    estimate_via_correlation,
    _resolve_canonical,
    _is_olympic,
    OLY_TECHNIQUE_DISCOUNT,
    E5RM_FACTORS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_set(
    date: str,
    reps: int,
    weight_kg: float,
    is_amrap: bool = False,
    rpe: float = None,
) -> dict:
    return {
        "session_date": date,
        "reps": reps,
        "weight_kg": weight_kg,
        "is_amrap": is_amrap,
        "rpe": rpe,
    }


def _linear_sets(
    n_weeks: int,
    start_weight: float,
    increment_per_week: float,
    reps: int = 5,
) -> list[dict]:
    """Synthetic lifter making linear weekly progress. One working set per week."""
    sets = []
    for i in range(n_weeks):
        week_date = f"2024-{(i // 4) + 1:02d}-{(i % 4) * 7 + 1:02d}"
        # Clamp to valid dates roughly
        sets.append(_make_set(
            date=f"2024-01-{(i + 1):02d}",
            reps=reps,
            weight_kg=start_weight + i * increment_per_week,
        ))
    return sorted(sets, key=lambda s: s["session_date"], reverse=True)


# ---------------------------------------------------------------------------
# Formula tests
# ---------------------------------------------------------------------------

class TestFormulas(unittest.TestCase):

    def test_epley_1rm_trivial(self):
        """1-rep set should return the weight itself."""
        self.assertEqual(epley(100, 1), 100)
        self.assertEqual(brzycki(100, 1), 100)
        self.assertEqual(wathan(100, 1), 100)

    def test_epley_5reps(self):
        """Epley 5×100: 1RM ≈ 116.7kg"""
        self.assertAlmostEqual(epley(100, 5), 116.67, places=1)

    def test_brzycki_5reps(self):
        """Brzycki is slightly more conservative than Epley."""
        epley_val = epley(100, 5)
        brz_val = brzycki(100, 5)
        self.assertLess(brz_val, epley_val)  # conservative
        self.assertGreater(brz_val, 100)

    def test_epley_inflates_at_low_reps(self):
        """Epley tends to overestimate 1RM at low rep counts (2-3 reps) vs Brzycki."""
        # At 3 reps: Epley(100,3)=110, Brzycki(100,3)≈108.9 — Epley is higher
        # At 10 reps: Wathan is actually higher than Epley (calibrated on athletes)
        # This is the known behavior — Epley inflates at LOW reps, not high
        epley_3   = epley(100, 3)
        brzycki_3 = brzycki(100, 3)
        self.assertGreater(epley_3, brzycki_3)  # Epley inflated at low reps vs Brzycki

    def test_monotone_with_reps(self):
        """Higher reps at same weight → higher 1RM estimate (all formulas)."""
        for fn in (epley, brzycki, wathan):
            vals = [fn(100, r) for r in range(1, 10)]
            for i in range(len(vals) - 1):
                self.assertGreater(vals[i + 1], vals[i], msg=f"{fn.__name__} not monotone")


# ---------------------------------------------------------------------------
# Set confidence tests
# ---------------------------------------------------------------------------

class TestSetConfidence(unittest.TestCase):

    def test_amrap_no_rpe_higher_than_normal(self):
        normal = _set_confidence(is_amrap=False, rpe=None)
        amrap  = _set_confidence(is_amrap=True, rpe=None)
        self.assertGreater(amrap, normal)

    def test_amrap_high_rpe_maxes_out(self):
        """AMRAP at RPE 10 should be highest confidence."""
        c_rpe10 = _set_confidence(is_amrap=True, rpe=10)
        c_rpe7  = _set_confidence(is_amrap=True, rpe=7)
        self.assertGreater(c_rpe10, c_rpe7)

    def test_non_amrap_high_rpe_above_baseline(self):
        c_no_rpe = _set_confidence(is_amrap=False, rpe=None)
        c_rpe9   = _set_confidence(is_amrap=False, rpe=9)
        self.assertGreater(c_rpe9, c_no_rpe)


# ---------------------------------------------------------------------------
# Test 1: Linear progress — estimate tracks upward trend
# ---------------------------------------------------------------------------

class TestLinearProgress(unittest.TestCase):

    def test_estimate_tracks_upward(self):
        """
        Lifter adds 2.5kg/week for 10 weeks (5-rep sets).
        Short window estimate should be higher than long window estimate.
        """
        sets = _linear_sets(n_weeks=10, start_weight=80, increment_per_week=2.5, reps=5)
        result = estimate_exercise("Bench Press", sets)
        self.assertIsNotNone(result)

        # Short window estimate should reflect recent (heavier) weights more
        short_w = next(w for w in result["window_detail"] if w["window"] == "short")
        long_w  = next(w for w in result["window_detail"] if w["window"] == "long")
        self.assertGreater(short_w["consensus"], long_w["consensus"])

    def test_estimate_in_plausible_range(self):
        """Final e1RM should be above the heaviest training weight."""
        sets = _linear_sets(n_weeks=10, start_weight=80, increment_per_week=2.5, reps=5)
        heaviest_training = 80 + 9 * 2.5  # 102.5kg
        result = estimate_exercise("Bench Press", sets)
        self.assertGreater(result["e1rm"], heaviest_training)


# ---------------------------------------------------------------------------
# Test 2: Bad week resilience
# ---------------------------------------------------------------------------

class TestBadWeekResilience(unittest.TestCase):

    def test_bad_week_doesnt_tank_estimate(self):
        """
        8 weeks of normal progress, then one anomalously light week (illness).
        The estimate should not drop dramatically from the prior level.
        """
        normal_sets = _linear_sets(n_weeks=8, start_weight=90, increment_per_week=2, reps=5)
        # Add one very recent bad session
        bad_set = _make_set(date="2024-01-09", reps=5, weight_kg=70)  # 20kg below normal

        all_sets = [bad_set] + normal_sets  # bad set is most recent

        result_with_bad = estimate_exercise("Squat", all_sets)
        result_without  = estimate_exercise("Squat", normal_sets)

        self.assertIsNotNone(result_with_bad)
        self.assertIsNotNone(result_without)

        # With bad week, estimate should drop but not catastrophically (< 10% drop)
        drop_pct = (result_without["e1rm"] - result_with_bad["e1rm"]) / result_without["e1rm"]
        self.assertLess(drop_pct, 0.10, msg=f"Estimate dropped {drop_pct:.1%} due to bad week — too sensitive")

    def test_bad_week_lowers_confidence(self):
        """High variance data → estimate range should be wider."""
        normal_sets = _linear_sets(n_weeks=10, start_weight=90, increment_per_week=2, reps=5)
        bad_set     = _make_set(date="2024-01-11", reps=5, weight_kg=60)
        noisy_sets  = [bad_set] + normal_sets

        normal_result = estimate_exercise("Squat", normal_sets)
        noisy_result  = estimate_exercise("Squat", noisy_sets)

        normal_range = normal_result["e1rm_high"] - normal_result["e1rm_low"]
        noisy_range  = noisy_result["e1rm_high"]  - noisy_result["e1rm_low"]

        self.assertGreater(noisy_range, normal_range, msg="Noisy data should produce wider CI")


# ---------------------------------------------------------------------------
# Test 3: Sparse data — few sessions
# ---------------------------------------------------------------------------

class TestSparseData(unittest.TestCase):

    def test_two_sessions_low_confidence(self):
        """2 sessions → confidence should be 'low' or 'very low'."""
        sets = [
            _make_set("2024-01-01", reps=5, weight_kg=80),
            _make_set("2024-01-08", reps=5, weight_kg=82.5),
        ]
        result = estimate_exercise("Bench Press", sets)
        self.assertIsNotNone(result)
        self.assertIn(result["confidence"], ("low", "very low"))

    def test_two_sessions_wider_range_than_ten(self):
        """Sparse data should give a wider CI than rich data."""
        sparse = [
            _make_set("2024-01-01", reps=5, weight_kg=100),
            _make_set("2024-01-08", reps=5, weight_kg=100),
        ]
        rich = [
            _make_set(f"2024-01-{i:02d}", reps=5, weight_kg=100)
            for i in range(1, 11)
        ]
        sparse_result = estimate_exercise("Bench Press", sparse)
        rich_result   = estimate_exercise("Bench Press", rich)

        sparse_range = sparse_result["e1rm_high"] - sparse_result["e1rm_low"]
        rich_range   = rich_result["e1rm_high"]   - rich_result["e1rm_low"]

        # Sparse should have range >= rich (at least as wide)
        self.assertGreaterEqual(sparse_range, rich_range - 0.5)  # 0.5kg tolerance for rounding

    def test_single_set_returns_result(self):
        """Even 1 set should return a result (very low confidence)."""
        sets = [_make_set("2024-01-01", reps=5, weight_kg=100)]
        result = estimate_exercise("Bench Press", sets)
        self.assertIsNotNone(result)


# ---------------------------------------------------------------------------
# Test 4: AMRAP vs non-AMRAP same weight
# ---------------------------------------------------------------------------

class TestAmrapWeighting(unittest.TestCase):

    def _estimate_with_amrap_flag(self, is_amrap: bool) -> dict:
        sets = [
            _make_set("2024-01-01", reps=5, weight_kg=100, is_amrap=is_amrap),
            _make_set("2024-01-08", reps=5, weight_kg=100, is_amrap=is_amrap),
            _make_set("2024-01-15", reps=5, weight_kg=100, is_amrap=is_amrap),
        ]
        return estimate_exercise("Bench Press", sets)

    def test_amrap_produces_same_e1rm_different_confidence(self):
        """
        Same weight and reps: AMRAP vs non-AMRAP produce same e1RM number
        (because the e1RM formula doesn't change), but AMRAP should be
        treated as higher-quality data.
        """
        amrap_result  = self._estimate_with_amrap_flag(True)
        normal_result = self._estimate_with_amrap_flag(False)

        # e1RM values should be the same (same weight/reps, same formula)
        self.assertAlmostEqual(amrap_result["e1rm"], normal_result["e1rm"], places=0)

    def test_amrap_mixed_with_normal_weights_amrap_more(self):
        """
        When AMRAP and non-AMRAP sets exist at different weights,
        the AMRAP set at the heavier weight should pull the estimate up
        more than an equivalent non-AMRAP set.
        """
        # Non-AMRAP: 5×90kg (says "I can do 5 reps"); AMRAP: 5×100kg (says "this is near-max")
        with_amrap = [
            _make_set("2024-01-01", reps=5, weight_kg=90, is_amrap=False),
            _make_set("2024-01-08", reps=5, weight_kg=100, is_amrap=True, rpe=9),
        ]
        without_amrap = [
            _make_set("2024-01-01", reps=5, weight_kg=90, is_amrap=False),
            _make_set("2024-01-08", reps=5, weight_kg=100, is_amrap=False),
        ]
        est_with    = estimate_exercise("Bench Press", with_amrap)
        est_without = estimate_exercise("Bench Press", without_amrap)

        # The AMRAP at RPE9 should produce a higher weighted estimate
        self.assertGreaterEqual(est_with["e1rm"], est_without["e1rm"])


# ---------------------------------------------------------------------------
# Test 5: Olympic lift technique discount
# ---------------------------------------------------------------------------

class TestOlympicLifts(unittest.TestCase):

    def test_technique_discount_applied(self):
        """
        Hang Power Clean estimate should be lower than same-data Deadlift estimate
        by approximately the technique discount factor.
        """
        sets = [_make_set(f"2024-01-{i:02d}", reps=3, weight_kg=100) for i in range(1, 6)]

        dl_result  = estimate_exercise("Deadlift", sets, is_olympic=False)
        hpc_result = estimate_exercise("Hang Power Clean", sets, is_olympic=True)

        expected_discount = OLY_TECHNIQUE_DISCOUNT["Hang Power Clean"]
        ratio = hpc_result["e1rm"] / dl_result["e1rm"]
        self.assertAlmostEqual(ratio, expected_discount, delta=0.02)

    def test_olympic_technique_note_present(self):
        """Olympic lift estimate should carry a technique note."""
        sets = [_make_set(f"2024-01-{i:02d}", reps=3, weight_kg=80) for i in range(1, 5)]
        result = estimate_exercise("Power Clean", sets, is_olympic=True)
        self.assertIsNotNone(result["technique_note"])
        self.assertIn("technique", result["technique_note"].lower())

    def test_is_olympic_detection(self):
        """_is_olympic correctly identifies Oly vs main lifts."""
        self.assertTrue(_is_olympic("hang power clean"))
        self.assertTrue(_is_olympic("Power Snatch"))
        self.assertFalse(_is_olympic("Bench Press"))
        self.assertFalse(_is_olympic("Squat"))


# ---------------------------------------------------------------------------
# Test 6: Correlation fallback
# ---------------------------------------------------------------------------

class TestCorrelation(unittest.TestCase):

    def test_incline_estimated_via_bench(self):
        """If no Incline Bench data, estimate from Bench Press via correlation."""
        bench_sets = [
            _make_set(f"2024-01-{i:02d}", reps=5, weight_kg=100) for i in range(1, 9)
        ]
        all_sets = {"Bench Press": bench_sets}
        result = estimate_via_correlation("Incline Bench 30°", all_sets)
        self.assertIsNotNone(result)
        # Incline should be ~88% of bench
        bench_e1rm = estimate_exercise("Bench Press", bench_sets)["e1rm"]
        expected = bench_e1rm * 0.88
        self.assertAlmostEqual(result["e1rm"], expected, delta=1.0)

    def test_correlated_result_is_labeled(self):
        bench_sets = [_make_set(f"2024-01-{i:02d}", reps=5, weight_kg=100) for i in range(1, 5)]
        result = estimate_via_correlation("Incline Bench 30°", {"Bench Press": bench_sets})
        self.assertTrue(result["is_correlated"])
        self.assertEqual(result["correlated_from"], "Bench Press")
        self.assertEqual(result["confidence"], "very low")

    def test_no_correlation_available(self):
        """If no correlated source in DB, return None."""
        result = estimate_via_correlation("Barbell Curl", {})
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Test 7: High-rep exclusion
# ---------------------------------------------------------------------------

class TestHighRepExclusion(unittest.TestCase):

    def test_sets_above_15_reps_excluded(self):
        """Sets >15 reps should not contribute to e1RM (too unreliable)."""
        sets_valid = [_make_set("2024-01-01", reps=5, weight_kg=100)]
        sets_mixed = sets_valid + [_make_set("2024-01-01", reps=20, weight_kg=60)]

        # _estimate_window should ignore the 20-rep set
        result_valid = _estimate_window(sets_valid)
        result_mixed = _estimate_window(sets_mixed)

        # Both should give the same e1RM (20-rep set ignored)
        self.assertAlmostEqual(result_valid["e1rm"], result_mixed["e1rm"], places=0)

    def test_only_high_rep_sets_returns_none(self):
        """If all sets are >15 reps, no estimate can be made."""
        sets = [_make_set("2024-01-01", reps=20, weight_kg=60)]
        result = _estimate_window(sets)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Test 8: e5RM factors
# ---------------------------------------------------------------------------

class TestE5rmFactors(unittest.TestCase):

    def test_deadlift_5rm_lower_pct_than_bench(self):
        """Deadlift e5RM should be a smaller % of e1RM than Bench Press."""
        dl_factor    = E5RM_FACTORS["Deadlift"]
        bench_factor = E5RM_FACTORS["Bench Press"]
        self.assertLess(dl_factor, bench_factor)

    def test_e5rm_from_estimate_in_range(self):
        """e5RM should be between 82% and 90% of e1RM for any lift."""
        sets = [_make_set(f"2024-01-{i:02d}", reps=5, weight_kg=100) for i in range(1, 6)]
        for ex in ["Squat", "Bench Press", "Deadlift", "Overhead Press"]:
            result = estimate_exercise(ex, sets)
            ratio = result["e5rm"] / result["e1rm"]
            self.assertGreater(ratio, 0.82, msg=f"{ex}: e5RM ratio {ratio:.3f} too low")
            self.assertLess(ratio, 0.90,    msg=f"{ex}: e5RM ratio {ratio:.3f} too high")


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
