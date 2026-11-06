"""Diagnostic-only bounded writer CPU/SQL instrumentation; never acceptance.

Compile temporary AST copies of selected functions. Native SQLite connections,
transactions, queries, payloads and benchmark fixtures remain unchanged.
Only aggregate function/call-site counters are retained (no event payloads).
"""
import argparse
import ast
from collections import Counter
from contextlib import contextmanager
import functools
import hashlib
import inspect
import json
import os
from pathlib import Path
import runpy
import sys
import textwrap
import threading
import time
import tempfile


class Probe:
    def __init__(self):
        self.local = threading.local()
        self.rows = {}
        self.batches = Counter()
        self.protection_states = Counter()
        self.sql_events = Counter()
        self.lock = threading.Lock()
        self.phase = 'setup'
        self.revisions = Counter()
        self.declarations = Counter()
        self.topologies = Counter()
        self.batch_kinds = Counter()

    @contextmanager
    def span(self, name):
        name = getattr(self.local, 'phase', self.phase) + '/' + name
        stack = getattr(self.local, 'stack', None)
        if stack is None:
            stack = self.local.stack = []
        frame = [time.thread_time_ns(), 0]
        stack.append(frame)
        try:
            yield
        finally:
            elapsed = time.thread_time_ns() - frame[0]
            stack.pop()
            if stack:
                stack[-1][1] += elapsed
            with self.lock:
                row = self.rows.setdefault(name, [0, 0, 0])
                row[0] += 1
                row[1] += elapsed
                row[2] += elapsed - frame[1]

    def call(self, name, fn, *args, **kwargs):
        with self.span(name):
            result = fn(*args, **kwargs)
            if name.endswith(':revision'):
                with self.lock:
                    self.revisions[str(result)] += 1
            elif name.endswith(':latest'):
                with self.lock:
                    self.declarations['missing' if result is None else str(result[0])] += 1
            elif name.endswith(':affected_topology'):
                with self.lock:
                    self.topologies[str(args[1])] += 1
            return result

    @contextmanager
    def context(self, name, manager):
        with self.span(name+':enter'):
            value = manager.__enter__()
        try:
            yield value
        except BaseException:
            with self.span(name+':exception_exit'):
                suppress = manager.__exit__(*sys.exc_info())
            if not suppress:
                raise
        else:
            with self.span(name+':exit'):
                manager.__exit__(None, None, None)

    def install(self, owner, name):
        original = inspect.getattr_static(owner, name)
        fn = original.__func__ if isinstance(original, classmethod) else original
        source = textwrap.dedent(inspect.getsource(fn))
        tree = ast.parse(source)
        tree.body[0].decorator_list = []
        prefix = fn.__module__ + '.' + fn.__qualname__
        probe = self

        class Calls(ast.NodeTransformer):
            # Inner per-entity/per-sample calls would distort the workload.
            # Their CPU belongs to the enclosing bounded operation instead.
            def visit_GeneratorExp(self, node): return node
            def visit_ListComp(self, node): return node
            def visit_DictComp(self, node): return node
            def visit_SetComp(self, node): return node
            def visit_Lambda(self, node): return node

            def visit_With(self, node):
                self.generic_visit(node)
                for item in node.items:
                    label = prefix+':'+str(node.lineno)+':'+ast.unparse(item.context_expr)
                    item.context_expr = ast.Call(
                        func=ast.Name(id='_writer_probe_context', ctx=ast.Load()),
                        args=[ast.Constant(label), item.context_expr], keywords=[])
                return node

            def visit_Call(self, node):
                self.generic_visit(node)
                label = ast.unparse(node.func)
                # All call sites have fixed labels: memory is bounded by source.
                return ast.copy_location(ast.Call(
                    func=ast.Name(id='_writer_probe_call', ctx=ast.Load()),
                    args=[ast.Constant(prefix+':'+str(node.lineno)+':'+label), node.func, *node.args],
                    keywords=node.keywords), node)

        tree = Calls().visit(tree)
        ast.fix_missing_locations(tree)
        namespace = dict(fn.__globals__, _writer_probe_call=probe.call,
                         _writer_probe_context=probe.context)
        exec(compile(tree, inspect.getsourcefile(fn), 'exec'), namespace)
        compiled = namespace[name]

        @functools.wraps(fn)
        def measured(*args, **kwargs):
            root = name == '_write'
            if not root and not getattr(probe.local, 'active', False):
                return fn(*args, **kwargs)
            if root:
                probe.local.active = True
                probe.local.phase = probe.phase
                with probe.lock:
                    probe.batches[probe.local.phase+'/'+str(len(args[1]))] += 1
                    kinds = '+'.join(sorted({row[1] for row in args[1]})) or 'empty'
                    probe.batch_kinds[probe.local.phase+'/'+kinds] += 1
            try:
                with probe.span(prefix):
                    return compiled(*args, **kwargs)
            finally:
                if root:
                    probe.local.active = False

        setattr(owner, name, classmethod(measured) if isinstance(original, classmethod) else measured)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--harness', type=Path, required=True)
    parser.add_argument('--profile-output', type=Path, required=True)
    args, benchmark_args = parser.parse_known_args()
    root = args.source_root.resolve()
    harness = args.harness.resolve()
    if harness.parent.parent != root:
        raise ValueError('Place the identical harness in each disposable source tree; it resolves runtime relative to __file__')
    sys.path[:0] = [str(root/'adaptive_ai/src'), str(harness.parent)]
    bootstrap = tempfile.TemporaryDirectory(prefix='hm-native-writer-profile-')
    os.environ['ADAPTIVE_AI_DATA'] = bootstrap.name
    for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
        os.environ[name] = '1'
    import universal_history_collector as collector
    import observation_contract as journal
    import universal_predecision as predecision
    import universal_training_data as data
    import universal_affected_sensors as affected
    probe = Probe()
    from storage import Store
    native_conn = Store.conn
    def trace_sql(sql):
        if getattr(probe.local, 'active', False):
            # Retain only the SQL verb, never interpolated values or payloads.
            verb = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else 'EMPTY'
            if verb not in ('SELECT', 'INSERT', 'UPDATE', 'DELETE', 'PRAGMA', 'BEGIN',
                            'COMMIT', 'ROLLBACK', 'SAVEPOINT', 'RELEASE', 'WITH'):
                verb = 'OTHER'
            with probe.lock:
                probe.sql_events[getattr(probe.local, 'phase', probe.phase)+'/'+verb] += 1
    @contextmanager
    def traced_conn(store, *a, **kw):
        with native_conn(store, *a, **kw) as connection:
            if getattr(probe.local, 'active', False):
                # Disposable harness only: it installs no user trace callback.
                # The native connection lifecycle closes/resets the callback.
                connection.set_trace_callback(trace_sql)
            yield connection
    Store.conn = traced_conn
    from storage_budget import BudgetConnection
    native_read = BudgetConnection.storage_readonly_one
    def observed_read(connection, sql, parameters=()):
        if getattr(probe.local, 'active', False) and 'samples(seq,entity,stamp)' in sql:
            if not connection.in_transaction:
                raise AssertionError('protection query outside writer transaction')
            with probe.span('diagnostic_only_protection_empty_check'):
                empty = connection.execute('SELECT 1 FROM feature_window_entities LIMIT 1').fetchone() is None
                with probe.lock:
                    probe.protection_states['empty' if empty else 'nonempty'] += 1
        return native_read(connection, sql, parameters)
    BudgetConnection.storage_readonly_one = observed_read
    for owner, names in (
        (collector.HistoricalTrainingCollector, ('_write', '_journal_rows')),
        (journal.FeatureJournal, ('record_batch', '_prepare_record')),
        (predecision, ('revision', 'validate_binding', 'verify_for_write')),
        (data.HistoricalDecision, ('capture',)),
        (affected, ('latest', 'topology', 'validate')),
    ):
        for name in names:
            probe.install(owner, name)
    sys.argv = [str(harness), *benchmark_args]
    try:
        namespace = runpy.run_path(sys.argv[0], run_name='writer_diagnostic')
        scope = namespace['main'].__globals__
        native_stream = scope['stream']
        def stream(*a, **kw):
            probe.phase = 'load'
            try:
                return native_stream(*a, **kw)
            finally:
                probe.phase = 'finalization'
        scope['stream'] = stream
        namespace['main']()
    finally:
        args.profile_output.write_text(json.dumps(dict(
            diagnostic_only=True, clock='thread_time_ns',
            source_root=str(root), harness_sha256=hashlib.sha256(harness.read_bytes()).hexdigest(),
            columns=['calls', 'inclusive_cpu_ns', 'exclusive_cpu_ns'],
            note='Diagnostic setup/load/finalization rows are separate; load is the unchanged harness stream() interval. Batches crossing a boundary retain their entry phase. Instrumentation overhead included. SQL trace counts include trigger expansion. Context entry/exit costs include native connection configuration and commit/cleanup.',
            batch_sizes=probe.batches, protection_states=probe.protection_states,
            source_revision_counts=probe.revisions,
            declaration_revision_counts=probe.declarations,
            topology_revision_counts=probe.topologies,
            batch_kinds=probe.batch_kinds,
            sql_trace_events=probe.sql_events,
            sql_trace_note='Includes trigger-expanded/repeated trace events; connection setup before callback installation is excluded. Diagnostic empty checks are included and separately timed.',
            rows=probe.rows), indent=2))


if __name__ == '__main__':
    main()
