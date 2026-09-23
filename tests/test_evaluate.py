"""Metrics and the threshold sweep, on hand-worked numbers."""

from __future__ import annotations

from screener.decide import Label
from screener.evaluate import evaluate
from screener.tune import sweep

from .conftest import answers


def case(label: Label) -> Label:
    return label


class TestMetrics:
    def test_a_perfect_screener(self):
        metrics = evaluate(
            [("a", Label.INCLUDE), ("b", Label.EXCLUDE)],
            {"a": "include", "b": "exclude"},
        )
        assert metrics.recall == 1.0
        assert metrics.precision == 1.0
        assert metrics.missed == 0

    def test_maybe_counts_as_reaching_a_human(self):
        """The positive class is 'a reviewer still sees it', not 'the screener said yes'."""
        metrics = evaluate([("a", Label.MAYBE)], {"a": "include"})
        assert metrics.recall == 1.0
        assert metrics.true_positives == 1

    def test_not_retrievable_also_counts_as_reaching_a_human(self):
        metrics = evaluate([("a", Label.NOT_RETRIEVABLE)], {"a": "include"})
        assert metrics.false_negatives == 0

    def test_an_eligible_study_auto_excluded_is_the_costly_error(self):
        metrics = evaluate([("a", Label.EXCLUDE)], {"a": "include"})
        assert metrics.recall == 0.0
        assert metrics.missed == 1

    def test_hand_worked_confusion_matrix(self):
        #  8 eligible: 7 kept, 1 dropped   |   12 ineligible: 3 kept, 9 dropped
        predictions = (
            [(f"tp{i}", Label.INCLUDE) for i in range(7)]
            + [("fn0", Label.EXCLUDE)]
            + [(f"fp{i}", Label.MAYBE) for i in range(3)]
            + [(f"tn{i}", Label.EXCLUDE) for i in range(9)]
        )
        gold = {record_id: ("include" if record_id.startswith(("tp", "fn")) else "exclude")
                for record_id, _ in predictions}
        metrics = evaluate(predictions, gold)

        assert metrics.n == 20
        assert metrics.confusion() == {
            "eligible_retained": 7,
            "eligible_dropped": 1,
            "ineligible_retained": 3,
            "ineligible_dropped": 9,
        }
        assert round(metrics.recall, 3) == 0.875
        assert round(metrics.precision, 3) == 0.7
        assert round(metrics.specificity, 3) == 0.75
        # auto-excluded = (9 + 1) / 20 = 0.5 ; WSS = 0.5 - (1 - 0.875) = 0.375
        assert round(metrics.auto_excluded, 3) == 0.5
        assert round(metrics.workload_saved, 3) == 0.375

    def test_workload_saved_punishes_lost_studies(self):
        """A screener that excludes everything gets a big raw reduction and a bad WSS."""
        predictions = [("a", Label.EXCLUDE), ("b", Label.EXCLUDE)]
        metrics = evaluate(predictions, {"a": "include", "b": "exclude"})
        assert metrics.auto_excluded == 1.0
        assert metrics.workload_saved == 0.0

    def test_unlabelled_records_are_counted_but_not_scored(self):
        metrics = evaluate(
            [("a", Label.INCLUDE), ("b", Label.EXCLUDE)], {"a": "include"}
        )
        assert metrics.n == 1
        assert metrics.unlabelled == 1

    def test_no_labels_at_all_yields_zeroes_not_a_crash(self):
        metrics = evaluate([("a", Label.INCLUDE)], {})
        assert metrics.n == 0
        assert metrics.recall == 0.0

    def test_eligible_studies_are_broken_down_by_band(self):
        metrics = evaluate(
            [("a", Label.INCLUDE), ("b", Label.MAYBE), ("c", Label.EXCLUDE)],
            {"a": "include", "b": "include", "c": "include"},
        )
        assert metrics.eligible_by_band == {"include": 1, "maybe": 1, "exclude": 1}


class TestSweep:
    def test_a_lower_exclusion_bar_drops_more_and_risks_more(self, protocol):
        """An eligible paper that scores 0.7 on an exclusion criterion survives at 0.85 and
        is lost at 0.65 — which is exactly the trade the sweep is there to expose."""
        cases = [
            ("keep", answers(exc_digital=0.70)),
            ("drop", answers(exc_digital=0.99, inc_humans=0.1)),
        ]
        gold = {"keep": "include", "drop": "exclude"}

        strict = sweep(protocol, cases, gold, include_grid=[0.6], exclude_grid=[0.85])
        assert strict.points[0].recall == 1.0

        loose = sweep(protocol, cases, gold, include_grid=[0.6], exclude_grid=[0.65])
        assert loose.points[0].recall == 0.0
        assert loose.points[0].missed == 1

    def test_the_winner_holds_the_target_and_saves_the_most(self, protocol):
        cases = [
            ("e1", answers(exc_digital=0.70)),
            ("e2", answers(exc_digital=0.10)),
            ("i1", answers(exc_digital=0.99, inc_humans=0.05)),
            ("i2", answers(exc_gambling=0.99, inc_humans=0.05)),
        ]
        gold = {"e1": "include", "e2": "include", "i1": "exclude", "i2": "exclude"}
        result = sweep(protocol, cases, gold, target_recall=1.0)
        assert result.best is not None
        assert result.best.recall == 1.0
        assert all(p.recall >= 1.0 for p in result.meeting_target())

    def test_when_nothing_holds_the_target_the_best_recall_is_returned(self, protocol):
        cases = [("a", answers(exc_digital=0.99))]
        result = sweep(protocol, cases, {"a": "include"}, target_recall=0.95,
                       include_grid=[0.6], exclude_grid=[0.7])
        assert result.meeting_target() == []
        assert result.best is not None
        assert result.best.recall == 0.0

    def test_ties_are_broken_towards_the_stricter_exclusion_bar(self, protocol):
        """`include_min` only moves records between include and maybe, and both still reach
        a human — so it changes neither recall nor auto-exclusion, and large stretches of
        the grid tie. Recommending the loosest of them would invite dropping papers on a
        corpus bigger than the sample."""
        cases = [("a", answers()), ("b", answers(exc_digital=0.99, inc_humans=0.05))]
        gold = {"a": "include", "b": "exclude"}
        result = sweep(protocol, cases, gold, target_recall=1.0,
                       include_grid=[0.3, 0.6], exclude_grid=[0.6, 0.75, 0.9])

        assert result.best is not None
        assert result.best.exclude_min == 0.9, "should prefer the most conservative tie"
        assert len(result.tied_with_best()) > 1

    def test_needs_human_reports_the_adjudication_burden(self, protocol):
        """The thing `include_min` actually controls, which the recall metric cannot see."""
        cases = [("a", answers(inc_humans=0.5))]
        lenient = sweep(protocol, cases, {"a": "include"}, include_grid=[0.3], exclude_grid=[0.85])
        strict = sweep(protocol, cases, {"a": "include"}, include_grid=[0.9], exclude_grid=[0.85])
        assert lenient.points[0].needs_human == 0.0
        assert strict.points[0].needs_human == 1.0

    def test_no_labels_produces_no_points(self, protocol):
        assert sweep(protocol, [("a", answers())], {}).points == []
