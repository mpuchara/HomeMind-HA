"""Bounded host-only evidence timing, separate from acceptance runs."""
import argparse
import json
import os
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('trace', type=Path)
    args, harness_args = parser.parse_known_args()
    sys.path[:0] = [str(args.source/'tools'), str(args.source/'adaptive_ai/src')]
    for key in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
        os.environ[key] = '1'
    events = []
    overflow = 0
    local = threading.local()

    def record(kind, **fields):
        nonlocal overflow
        if len(events) < 16384:
            events.append(dict(kind=kind, ns=time.perf_counter_ns(), **fields))
        else:
            overflow += 1

    with tempfile.TemporaryDirectory(prefix='hm-evidence-trace-') as directory, ExitStack() as stack:
        os.environ['ADAPTIVE_AI_DATA'] = directory
        from benchmark_qualified_evidence import main as benchmark
        import universal_predecision as predecision
        from universal_history_collector import HistoricalTrainingCollector as Collector
        import storage_budget
        from storage_budget import BudgetCursor
        evidence_type = predecision.PredecisionEvidence
        original_request = evidence_type.request
        original_begin = evidence_type.begin
        original_matches = evidence_type._matches
        original_bind = evidence_type.bind
        original_prepare = evidence_type.prepare_one
        original_verify = predecision.verify_for_write
        original_submit = Collector.decision
        original_write = Collector._write
        original_execute = BudgetCursor.execute
        original_clean = storage_budget._clean_sql
        classification = {}

        def clean(sql):
            count = getattr(local, 'classification', None)
            if count is None:
                count = local.classification = [0, 0]
                classification[threading.get_ident()] = count
            cpu = time.thread_time_ns()
            result = original_clean(sql)
            count[1] += time.thread_time_ns()-cpu
            count[0] += 1
            return result

        def request(self, agent_id):
            result = original_request(self, agent_id)
            record('request', agent=agent_id)
            return result

        def matches(self, evidence, agent, state, values):
            result = original_matches(self, evidence, agent, state, values)
            local.checked = True
            if not result:
                record('match_rejected', agent=agent['id'], stage=local.stage,
                       observed_expired=time.time() >= evidence['valid_until_ts'],
                       observed_epoch_mismatch=evidence['epoch'] != self.collector.epoch,
                       observed_registry_mismatch=evidence['registry_revision'] != self.engine.context.registry_revision,
                       state_mismatch=evidence['state'] != state, agent_mismatch=evidence['agent'] != agent)
            return result

        def begin(self, agent, state, values):
            local.checked = False
            local.stage = 'begin'
            entered = time.perf_counter_ns()
            result = original_begin(self, agent, state, values)
            record('begin', agent=agent['id'], entered_ns=entered,
                   outcome='available' if result is not None else
                   'rejected_after_lookup' if local.checked else 'not_prepared_at_lookup',
                   inference_started_ts=result[1] if result else None)
            return result

        def bind(self, ticket, intent, agent, state, values):
            local.stage = 'bind'
            result = original_bind(self, ticket, intent, agent, state, values)
            record('bind', agent=agent['id'], decision=intent.intent_id, decision_ts=intent.created_at,
                   outcome='consumed' if result is not None else 'binding_failed' if ticket else 'no_ticket')
            return result

        def execute(self, *a, **kw):
            sql = getattr(local, 'prepare_sql', None)
            if sql is None:
                return original_execute(self, *a, **kw)
            started = time.perf_counter_ns()
            try:
                return original_execute(self, *a, **kw)
            finally:
                sql[0] += 1
                sql[1] += time.perf_counter_ns()-started

        def prepare(self):
            with self.lock:
                agent = next(iter(self.pending), None)
                pending = len(self.pending)
                previous = self.prepared.get(agent)
            sql = local.prepare_sql = [0, 0]
            started, cpu = time.perf_counter_ns(), time.thread_time_ns()
            error = None
            try:
                return original_prepare(self)
            except Exception as exc:
                error = dict(kind=type(exc).__name__, sqlite_code=getattr(exc, 'sqlite_errorcode', None))
                raise
            finally:
                elapsed_cpu = time.thread_time_ns()-cpu
                local.prepare_sql = None
                with self.lock:
                    value = self.prepared.get(agent)
                published = value is not None and value is not previous
                record('prepare', agent=agent, pending=pending, started_ns=started,
                       cpu_ns=elapsed_cpu, native_cursor_calls=sql[0], native_cursor_ns=sql[1],
                       published=published, available_ts=value['available_ts'] if published else None,
                       valid_until_ts=value['valid_until_ts'] if published else None, error=error)

        def verify(conn, evidence, intent, agent, state):
            started = time.perf_counter_ns()
            reason = None
            try:
                return original_verify(conn, evidence, intent, agent, state)
            except (ValueError, KeyError, TypeError) as exc:
                reason = str(exc)
                raise
            finally:
                record('verify', decision=intent['intent_id'], elapsed_ns=time.perf_counter_ns()-started,
                       rejection=reason)

        def submit(self, intent, *a, **kw):
            result = original_submit(self, intent, *a, **kw)
            record('submit', decision=intent.intent_id, accepted=bool(result))
            return result

        def write(self, batch, gap, epoch, reset_registries):
            started = time.perf_counter_ns()
            result = original_write(self, batch, gap, epoch, reset_registries)
            for row_epoch, kind, payload, _, queued in batch:
                if kind == 'decision' and row_epoch == epoch:
                    record('write_completed', decision=payload['intent']['intent_id'],
                           started_ns=started, queued_perf_s=queued,
                           missing_proof=payload.get('missing_proof'), receipt=bool(payload.get('receipt')))
            return result

        for owner, name, wrapper in (
                (evidence_type, 'request', request), (evidence_type, 'begin', begin),
                (evidence_type, '_matches', matches), (evidence_type, 'bind', bind),
                (evidence_type, 'prepare_one', prepare), (BudgetCursor, 'execute', execute),
                (storage_budget, '_clean_sql', clean),
                (predecision, 'verify_for_write', verify), (Collector, 'decision', submit),
                (Collector, '_write', write)):
            stack.enter_context(patch.object(owner, name, wrapper))
        sys.argv[1:] = harness_args
        try:
            return benchmark()
        finally:
            with args.trace.open('x', encoding='utf-8') as output:
                json.dump(dict(diagnostic_only=True, overflow=overflow, events=events,
                               clean_sql_calls=sum(row[0] for row in classification.values()),
                               clean_sql_cpu_ns=sum(row[1] for row in classification.values())), output)


if __name__ == '__main__':
    sys.exit(main())
