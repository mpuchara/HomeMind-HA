# Frozen oracle: 79bf067432c2cf6b7e0c4122352a6c609bb36067
# Execute with observation_contract globals; do not update expected behavior.
def record_batch(self, records, *, wait_for_store=True):
    """Persist multiple high-resolution observations in one writer transaction."""
    prepared = []
    for raw in records or ():
        if raw is None:
            continue
        if "attributes_json" in raw and "event_key" in raw:
            prepared.append(dict(raw))
            continue
        item = self._prepare_record(
            raw.get("entity_id"), raw.get("state"),
            event_time=raw.get("event_time"),
            received_time=raw.get("received_time"),
            source=raw.get("source") or "ha_state_changed",
            event_key=raw.get("event_key"),
            quality=raw.get("quality", 1.0),
        )
        if item is not None:
            prepared.append(item)
    if not prepared:
        return []

    # The optional collector uses SQLite's native writer snapshot directly
    # with busy_timeout=0. Protection lookup + insert remain in the SAME
    # BEGIN IMMEDIATE transaction: another window writer cannot interleave.
    # The default path retains its existing Store serialization.
    admission=self.store.lock if wait_for_store else nullcontext()
    with admission, self.store.conn() as c, c.storage_batch():
        # Read protection and insert observations in one writer snapshot.
        # Preserve the connection's normal WAL/checkpoint admission before
        # explicitly reserving the writer, including direct record() calls.
        if not c.in_transaction:
            admission = getattr(c, 'storage_write_admission', None)
            if admission is not None:
                admission()
            c.execute('BEGIN IMMEDIATE')
        protection = {}
        for start in range(0, len(prepared), 256):
            chunk = prepared[start:start+256]
            values = ','.join('(?,?,?)' for _ in chunk)
            params = [value for offset,row in enumerate(chunk) for value in
                      (start+offset, str(row['entity_id']), float(row['event_time']))]
            # Most fresh samples share the newest protecting window. When
            # the maximum expiry also covers an entity's complete sample
            # range, it is the exact answer for every sample in that range.
            # Partial/disjoint coverage retains the exact per-sample query.
            # Return one SQLite row for the whole bounded group. Iterating
            # one native result row per sample repeatedly yields the GIL
            # while Store is held; busy inference can turn a short query
            # into seconds of writer lock residency. CASE/MAX preserves
            # the native REAL values without a text/JSON round trip.
            columns = ','.join(f'MAX(CASE WHEN seq={start+offset} THEN expiry END)'
                               for offset in range(len(chunk)))
            query = f'''SELECT * FROM (
                WITH samples(seq,entity,stamp) AS (VALUES {values}),
                ranges AS (SELECT entity,MIN(stamp) lo,MAX(stamp) hi FROM samples GROUP BY entity),
                coverage AS (
                    SELECT r.entity,COALESCE(MAX(w.protected_until),0) maximum,
                        COALESCE(MAX(CASE WHEN w.start_time<=r.lo AND w.end_time>=r.hi
                            THEN w.protected_until END),0) common
                    FROM ranges r LEFT JOIN feature_window_entities e ON e.entity_id=r.entity
                    LEFT JOIN feature_windows w ON w.window_id=e.window_id
                        AND w.start_time<=r.hi AND w.end_time>=r.lo
                    GROUP BY r.entity
                )
                , protection AS (SELECT s.seq,CASE WHEN c.maximum=c.common THEN c.common ELSE (
                    SELECT MAX(w.protected_until) FROM feature_window_entities e
                    JOIN feature_windows w ON w.window_id=e.window_id
                    WHERE e.entity_id=s.entity AND w.start_time<=s.stamp AND w.end_time>=s.stamp
                ) END AS expiry FROM samples s JOIN coverage c ON c.entity=s.entity)
                SELECT {columns} FROM protection)'''
            read = getattr(c, 'storage_readonly_one', None)
            row = read(query, params) if callable(read) else c.execute(query, params).fetchone()
            protection.update((start+offset, float(value or 0.)) for offset,value in enumerate(row))
        _insert_journal_rows(c,
                """INSERT INTO feature_observation_events
                   (event_key,contract_version,entity_id,event_time,received_time,state,
                    attributes_json,last_changed,last_updated,source,quality,protected_until)""",
                ((
                    row["event_key"], CONTRACT_VERSION, row["entity_id"],
                    row["event_time"], row["received_time"], row["state"],
                    row["attributes_json"], row.get("last_changed"), row.get("last_updated"),
                    row["source"], row["quality"], protection[index],
                ) for index,row in enumerate(prepared)), 12,
                ''' ON CONFLICT(event_key) DO UPDATE SET
                     received_time=MIN(feature_observation_events.received_time,excluded.received_time),
                     protected_until=MAX(feature_observation_events.protected_until,excluded.protected_until),
                     quality=MAX(feature_observation_events.quality,excluded.quality)''')
        with self._write_count_lock:
            previous_writes = self._writes
            self._writes += len(prepared)
            if self._writes // 128 > previous_writes // 128:
                self._prune_pending = True
                self._prune_entity = prepared[-1]['entity_id']

    self.flush_pending_prune(wait_for_store=wait_for_store)
    return [row["event_key"] for row in prepared]
