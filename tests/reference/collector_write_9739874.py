def _write(self, batch, gap, epoch, reset_registries):
        from settings import parse_ts
        require_storage_available(self.store)
        observations=[]; metadata=[]; advances={}; affected_topologies={}
        for row_epoch,kind,payload,size,queued in batch:
            if row_epoch!=epoch:
                continue
            if kind=='event':
                eid,st,event,received=payload
                observations.append(dict(entity_id=eid,state=st,event_time=event,received_time=received,
                                         source='ha_state_changed',quality=0. if st.get('state') in (None,'unknown','unavailable') else 1.))
            else:
                metadata.append((kind,payload))
        keys=self._journal_rows(observations)
        self.stats['native_event_upserts']+=len(keys)
        with self.store.conn() as c, c.storage_batch():
            c.execute('BEGIN IMMEDIATE')
            decision_rows=[];mask_rows=[]
            def flush_receipts():
                from sqlite_bulk_rows import insert_bounded_rows
                insert_bounded_rows(c,'INSERT OR IGNORE INTO universal_collection_decisions',decision_rows,6)
                insert_bounded_rows(c,'INSERT OR IGNORE INTO universal_collection_masks',mask_rows,5)
                decision_rows.clear();mask_rows.clear()
            if gap:
                self._close(c,gap)
                if reset_registries:
                    self.registries={};self.connected=self.subscription_known;self.topology=None
                    self.topology_at=None
                self.connected=self.subscription_known
                c.execute('INSERT INTO universal_collection_gaps(session,at,reason) VALUES(?,?,?)',(self.session,self.clock(),gap))
                self.stats['gaps']+=1
            retention=c.execute('SELECT retention_epoch FROM universal_collection_meta WHERE id=1').fetchone()[0]
            for kind,payload in metadata:
                if kind=='connected':
                    self.connected=True
                elif kind=='notice':
                    c.execute('INSERT INTO universal_collection_gaps(session,at,reason) VALUES(?,?,?)',
                              (self.session,self.clock(),payload))
                elif kind=='registry':
                    name,rows,at=payload; self._close(c,'registry_revision')
                    self.registries[name]=rows
                    if set(self.registries)=={'entity','device','area'}:
                        body=encoded(dict(version=1,registries=self.registries,at=at))
                        self.topology=digest(body)
                        self.topology_at=at
                        c.execute('INSERT OR IGNORE INTO universal_collection_topology VALUES(?,?,?,?)',
                                  (self.topology,at,body,digest(body)))
                elif kind=='snapshot':
                    states,at,requested=payload;self._close(c,'reconciliation')
                    expected={r['entity_id'] for r in self.registries.get('entity',())
                              if r.get('entity_id') and sensor(r['entity_id']) and not r.get('disabled_by')}
                    if (not self.connected or not self.topology or self.subscription_at is None
                            or requested<self.subscription_at or requested<self.topology_at
                            or not expected or not expected<=states.keys()):
                        c.execute('INSERT INTO universal_collection_gaps(session,at,reason) VALUES(?,?,?)',
                                  (self.session,at,'rejected_incomplete_snapshot_boundary'))
                        continue
                    seedrows=[];seeds=[]
                    for eid,st in states.items():
                        event=parse_ts(st.get('last_updated') or st.get('last_changed'))
                        if event is None or event>at:
                            raise ValueError('snapshot lacks original event clock')
                        seedrows.append(dict(entity_id=eid,state=st,event_time=event,received_time=at,
                                             source='universal_collection_seed',quality=0. if st.get('state') in (None,'unknown','unavailable') else 1.))
                        seeds.append((eid,event,at))
                    # Seed values stay in native FeatureJournal, committed BEFORE proof.
                    # Use a separate connection; metadata transaction has not written
                    # for this snapshot path after _close only if no previous segment.
                    flush_receipts()
                    c.commit()
                    self.partial_metadata_commit=True
                    from universal_retention import origin
                    with self.store.conn() as boundary:
                        tracker,origin_rev=origin(boundary)
                    seed_keys=self._journal_rows(seedrows)
                    self.stats['native_seed_upserts']+=len(seed_keys)
                    c.execute('BEGIN IMMEDIATE')
                    current=c.execute('SELECT retention_epoch FROM universal_collection_meta WHERE id=1').fetchone()[0]
                    if current!=retention or origin(c)!=(tracker,origin_rev) or len(seed_keys)!=len(seeds):
                        continue
                    self.segment=uuid.uuid4().hex
                    from universal_affected_sensors import topology as affected_topology
                    _,universe,_=affected_topology(c,self.topology)
                    proof=encoded(dict(version=3,source_revision=self.segment,start_ts=at,
                                       complete_through_ts=at,entity_ids=sorted(states),complete=True,
                                       seed_receipts=seeds,collector_retention_epoch=retention,
                                       topology_revision=self.topology,historical_sensor_universe_revision=digest(encoded(universe)),
                                       historical_sensor_universe_count=len(universe),retention_tracker=tracker,
                                       retention_origin_revision=origin_rev,
                                       seed_event_keys=[(seed[0],key,seed[1],seed[2]) for seed,key in zip(seeds,seed_keys)]))
                    proof_hash=digest(proof)
                    seal=digest(encoded([self.segment,at,at,self.topology,retention,proof_hash]))
                    c.execute('INSERT INTO universal_collection_segments VALUES(?,?,?,?,?,?,?,0,NULL,?,?)',
                              (self.segment,self.session,at,at,self.topology,proof,retention,proof_hash,seal))
                    from universal_retention import seal as seal_retention
                    seal_retention(c,at)
                elif kind=='barrier' and self.segment:
                    # A gap arriving during writes defeats advancement of this proof.
                    advances[self.segment]=max(payload,advances.get(self.segment,payload))
                elif kind=='decision':
                    evidence=payload.get('predecision')
                    if evidence is not None:
                        from universal_predecision import verify_for_write
                        try:
                            if evidence['epoch']!=self.epoch or evidence['topology']!=self.topology:
                                raise ValueError('predecision topology changed')
                            payload['predecision']=verify_for_write(c,evidence,payload['intent'],payload['agent'],payload['state'])
                            payload['missing_proof']='affected_sensor_evidence_unavailable' if payload['affected_sensor_ids'] is None else None
                            new_affected='affected_contract_version' in evidence
                            if new_affected:
                                from universal_affected_sensors import VERSION as AFFECTED_VERSION, validate, latest, topology as affected_topology
                                try:
                                    if evidence['affected_contract_version']!=AFFECTED_VERSION:
                                        raise ValueError('affected_unsupported_contract')
                                    proof=payload['affected_sensor_evidence']
                                    if proof is not None and latest(c,payload['agent']['id'])!=(proof['declaration_revision'],proof['declaration_json'],proof['declaration_checksum']):
                                        raise ValueError('affected_native_declaration_mismatch')
                                    if self.topology not in affected_topologies:
                                        affected_topologies[self.topology]=affected_topology(c,self.topology)
                                    ids=validate(proof,payload['predecision'],sensor_universe=affected_topologies[self.topology][1],
                                                 topology_information_ts=affected_topologies[self.topology][0])
                                    if list(ids)!=list(payload['affected_sensor_ids'] or ()):
                                        raise ValueError('affected_sensor_set_mismatch')
                                    payload['affected_sensor_status']='complete'
                                    payload['missing_proof']=None
                                except (ValueError,KeyError,TypeError) as exc:
                                    payload['affected_sensor_ids']=None
                                    payload['affected_sensor_status']=str(exc)
                                    payload['missing_proof']=str(exc)
                            if (new_affected or payload['affected_sensor_ids'] is not None) and self.topology:
                                from universal_training_data import HistoricalDecision
                                topology_at=c.execute('SELECT at FROM universal_collection_topology WHERE id=?',(self.topology,)).fetchone()[0]
                                payload['receipt']=asdict(HistoricalDecision.capture(
                                    decision_ts=payload['intent']['created_at'],event_ts=evidence['created_ts'],
                                    received_ts=evidence['available_ts'],registry_information_ts=topology_at,
                                    registry_revision=self.topology,agent=payload['agent'],descriptor=evidence['descriptor'],
                                    legal_mask=evidence['legal_mask'],state=payload['state'],
                                    required_sensor_ids=payload['required_sensor_ids'],affected_sensor_ids=payload['affected_sensor_ids'],
                                    predecision_evidence=payload['predecision'],
                                    affected_contract_version=evidence.get('affected_contract_version'),
                                    affected_sensor_evidence=payload['affected_sensor_evidence'],
                                    affected_sensor_status=payload['affected_sensor_status']))
                        except (ValueError,KeyError,TypeError):
                            payload['predecision']=None
                            payload['receipt']=None
                            payload['missing_proof']='stale_or_invalid_predecision_evidence'
                    body=encoded(payload);intent=payload['intent']
                    decision_rows.append((intent['intent_id'],intent['agent_id'],intent['created_at'],self.topology,body,digest(body)))
                elif kind=='mask':
                    body=encoded(payload)
                    mask_rows.append((digest(body),payload['decision_id'],payload['at'],body,digest(body)))
            flush_receipts()
            if self.epoch==epoch and self.pending_gap is None:
                for sid,at in advances.items():
                    row=c.execute('SELECT start,through,topology,retention_epoch,proof_hash FROM universal_collection_segments WHERE id=?',(sid,)).fetchone()
                    if row and row[3]==retention:
                        through=max(row[1],at)
                        seal=digest(encoded([sid,row[0],through,row[2],row[3],row[4]]))
                        c.execute('UPDATE universal_collection_segments SET through=?,watermark_hash=? WHERE id=?',(through,seal,sid))
                        from universal_retention import seal as seal_retention
                        seal_retention(c,through)
            if self.epoch!=epoch:
                self._close(c,'concurrent_gap')
        self.stats['written']+=len(batch);self.stats['batches']+=1
        with self.cv:
            for row in batch:self._count_kind(row[1],'written')
        if batch:
            self.stats['max_write_latency_ms']=max(self.stats['max_write_latency_ms'],(time.perf_counter()-batch[0][-1])*1000)
            with self.cv:
                self.latencies.extend((time.perf_counter()-r[-1])*1000 for r in batch)
