"""Differential check of the diagnostic admission branch probes."""
import unittest
from contextlib import ExitStack, nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import support
import universal_history_collector as collector
from tools.trace_preparation_scheduler import install
from universal_predecision import PredecisionEvidence


class SchedulerProbeTests(unittest.TestCase):
    def test_admission_and_budget_match_uninstrumented_turn(self):
        original = collector.HistoricalTrainingCollector._prepare_turn

        def replay(turn, *, idle, pending, ready_at, outcome):
            now = [10.]
            calls = []
            budget = collector.PreparationBudget(lambda: now[0])
            budget.ready_at = ready_at

            def prepare():
                calls.append('prepare')
                if outcome == 'exception':
                    now[0] += .02
                    raise RuntimeError('synthetic preparation exception')
                if outcome == 'completed':
                    now[0] += .02

            def idle_ready():
                calls.append('idle')
                return idle

            owner = SimpleNamespace(
                predecision=SimpleNamespace(pending={'root': None} if pending else {},
                                           engine=SimpleNamespace(in_flight={}),
                                           prepare_one=prepare, stats={'errors': 0}),
                pending=[], stopping=False, idle_ready=idle_ready, preparation_budget=budget,
                retry=SimpleNamespace(delay=lambda: 0.),
                store=SimpleNamespace(background_sqlite=lambda **_: nullcontext()),
                preparation_stats=dict(budget_deferrals=0, wall_seconds=0., thread_cpu_seconds=0., turns=0))
            turn(owner)
            stats = dict(owner.preparation_stats)
            stats.pop('thread_cpu_seconds')
            return calls, stats, budget.ready_at, owner.predecision.stats

        cases = (
            dict(idle=False, pending=True, ready_at=0., outcome='completed'),
            dict(idle=True, pending=False, ready_at=0., outcome='completed'),
            dict(idle=True, pending=True, ready_at=10.1, outcome='completed'),
            dict(idle=True, pending=True, ready_at=0., outcome='completed'),
            dict(idle=True, pending=True, ready_at=0., outcome='noop'),
            dict(idle=True, pending=True, ready_at=0., outcome='exception'),
        )
        with ExitStack() as stack:
            records = []
            install(stack, patch, collector, PredecisionEvidence,
                    lambda kind, **fields: records.append((kind, fields)))
            probed = collector.HistoricalTrainingCollector._prepare_turn
            for case in cases:
                with self.subTest(**case):
                    self.assertEqual(replay(original, **case), replay(probed, **case))
            kinds = [kind for kind, _ in records]
            self.assertEqual(kinds.count('budget_debt'), 1)
            self.assertEqual(kinds.count('attempt'), 3)


if __name__ == '__main__':
    unittest.main()
