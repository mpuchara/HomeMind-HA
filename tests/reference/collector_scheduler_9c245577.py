"""Frozen collector loop from production 9c245577."""

def _run(self):
    evidence = getattr(self, 'predecision', None)
    if callable(getattr(evidence, 'close', None)):
        evidence.close()
    self.ready.set()
    while True:
        with self.cv:
            if not self.pending and self.pending_gap is None and not self.stopping:
                self.cv.wait(self._idle_wait())
            if self.stopping and not self.pending and self.pending_gap is None:
                break
        if self.retry.delay()>0:
            with self.cv:self.cv.wait(min(.5,self.retry.delay()))
        elif not self.stopping and not self.idle_ready():
            # Waiting for *all* futures starved this queue under sustained
            # inference. Offer a bounded, non-waiting writer turn instead.
            # Expensive mask preparation and housekeeping remain idle-only.
            if not self.drain(foreground=True):
                self.stats['deferred_for_foreground']+=1
            try:
                with self.store.background_sqlite(timeout_ms=0):
                    self.journal.flush_pending_prune(wait_for_store=False)
            except Exception as exc:
                if not is_sqlite_busy(exc):
                    self.stats['errors']+=1;self.stats['last_error']=type(exc).__name__
            self.halt.wait(.005)
        else:
            if self.drain():
                self._prepare_turn()
    with self.store.conn() as c:
        self._close(c,'clean_shutdown')
        c.execute("UPDATE universal_collection_sessions SET ended=?,reason='clean_shutdown' WHERE id=?",(self.clock(),self.session))
