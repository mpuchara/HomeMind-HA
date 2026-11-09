"""Install branch probes on disposable copies of the scheduler's methods."""
import ast
import inspect
import textwrap
import time


def install(stack, patch, collector, evidence, record):
    def snapshot(owner):
        ev = getattr(owner, 'predecision', None)
        return dict(evidence_pending=len(ev.pending) if ev is not None else 0,
                    writer_pending=len(owner.pending),
                    priority_s=collector.foreground_priority_remaining(ev.engine) if ev is not None else None,
                    budget_s=owner.preparation_budget.remaining(),
                    retry_s=owner.retry.delay())

    def mark(owner, kind, **fields):
        record(kind, **snapshot(owner), **fields)

    def idle(owner):
        result = owner.idle_ready()
        ev = getattr(owner, 'predecision', None)
        futures = list(ev.engine.in_flight.values()) if ev is not None else []
        mark(owner, 'idle_checked', idle=result,
             active_futures=sum(not f.done() for f in futures))
        return result

    def drain(owner, **kwargs):
        start, cpu = time.perf_counter_ns(), time.thread_time_ns()
        result = owner.drain(**kwargs)
        mark(owner, 'drain_checked', ok=result, foreground=kwargs.get('foreground', False),
             started_ns=start, cpu_ns=time.thread_time_ns()-cpu)
        return result

    def wait(owner, timeout):
        mark(owner, 'cv_wait', timeout_s=timeout)
        result = owner.cv.wait(timeout)
        mark(owner, 'cv_wake', notified=result)
        return result

    class Calls(ast.NodeTransformer):
        def visit_Call(self, node):
            self.generic_visit(node)
            replacement = {'self.idle_ready': '_probe_idle', 'self.drain': '_probe_drain',
                           'self.cv.wait': '_probe_wait'}.get(ast.unparse(node.func))
            if replacement:
                node.func = ast.Name(id=replacement, ctx=ast.Load())
                node.args.insert(0, ast.Name(id='self', ctx=ast.Load()))
            return node

    def instrument(owner, name, inserts, replace_calls=False, branches=()):
        original = getattr(owner, name)
        source = textwrap.dedent(inspect.getsource(original))
        lines = source.splitlines(keepends=True)
        locations = [(anchor, probe, False) for anchor, probe in inserts]
        locations.extend((anchor, probe, True) for anchor, probe in branches)
        for anchor, probe, inside in locations:
            matches = [i for i, line in enumerate(lines) if line.strip() == anchor]
            if len(matches) != 1:
                raise RuntimeError(f'{name}: expected one probe anchor for {anchor!r}')
            i = matches[0]
            indent = lines[i][:len(lines[i])-len(lines[i].lstrip())]
            lines.insert(i+int(inside), indent+('    ' if inside else '')+probe+'\n')
        tree = ast.parse(''.join(lines))
        if replace_calls:
            tree = Calls().visit(tree)
        namespace = dict(original.__globals__, _probe_mark=mark, _probe_record=record,
                         _probe_idle=idle, _probe_wait=wait, _probe_drain=drain)
        exec(compile(ast.fix_missing_locations(tree), inspect.getfile(original), 'exec'), namespace)
        stack.enter_context(patch.object(owner, name, namespace[name]))

    owner = collector.HistoricalTrainingCollector
    instrument(owner, '_run', [
        ('with self.cv:', '_probe_mark(self, "loop")'),
        ('with self.cv:self.cv.wait(min(.5,self.retry.delay()))',
         '_probe_mark(self, "retry_cooldown")'),
        ('if not self.drain(foreground=True):', '_probe_mark(self, "foreground_path")'),
    ], replace_calls=True)
    # The cooldown uses a one-line with statement, which the call transformer
    # also instruments without re-evaluating the timeout expression.
    instrument(owner, '_prepare_turn', [
        ('evidence = getattr(self, \'predecision\', None)', '_probe_mark(self, "admission")'),
        ("self.preparation_stats['budget_deferrals'] += 1", '_probe_mark(self, "budget_debt")'),
        ('with self.store.background_sqlite(timeout_ms=0):', '_probe_mark(self, "attempt")'),
    ], replace_calls=True)
    instrument(evidence, 'request', [
        ('self.pending[agent_id] = None', '_probe_record("enqueue", agent=agent_id, repeated=agent_id in self.pending, size=len(self.pending))'),
    ])
    instrument(evidence, 'begin', [
        ("evidence = self.prepared.get(agent['id'])", '_probe_record("lookup", agent=agent["id"], target=agent["target_entity"], prepared=agent["id"] in self.prepared, pending=agent["id"] in self.pending)'),
    ])
    instrument(evidence, 'prepare_one', [
        ('if not self.preparation_lock.acquire(blocking=False):', '_probe_record("prepare_enter")'),
        ('return self._prepare_one()', '_probe_record("preparation_lock_acquired")'),
    ], branches=[
        ('if not self.preparation_lock.acquire(blocking=False):', '_probe_record("preparation_lock_unavailable")'),
    ])
    instrument(evidence, '_prepare_one', [
        ('aid, _ = self.pending.popitem(last=False)', '_probe_record("selected", agent=next(iter(self.pending)), pending=len(self.pending))'),
        ('if not engine.lock.acquire(blocking=False):', '_probe_record("before_engine_lock", agent=aid)'),
        ("context_lock = getattr(engine.context, 'lock', None)", '_probe_record("engine_lock_acquired", agent=aid)'),
        ('old = self.prepared.pop(aid, None)', '_probe_record("publication_enter", agent=aid, available_ts=evidence["available_ts"], valid_until_ts=evidence["valid_until_ts"])'),
        ("self.stats['prepared'] += 1", '_probe_record("published", agent=aid)'),
    ], branches=[
        ('if not engine.lock.acquire(blocking=False):', '_probe_record("engine_lock_unavailable", agent=aid)'),
        ('if context_lock is not None and not context_lock.acquire(blocking=False):',
         '_probe_record("context_lock_unavailable", agent=aid)'),
    ])
