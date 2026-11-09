"""Cross-version migration rehearsal on generated disposable data, never a live home.

Uses each selected source tree in a fresh process. This verifies Store and selected
schema/model guards, not the complete production startup or interrupted-job recovery.
"""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory(path):
    result = {}
    with sqlite3.connect(path) as conn:
        assert conn.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            quoted = '"'+name.replace('"','""')+'"'
            columns = [r[1] for r in conn.execute(f'PRAGMA table_info({quoted})')]
            rows = list(conn.execute(f'SELECT * FROM {quoted}'))
            result[name] = dict(columns=columns, rows=rows)
    return result


def check(before, after):
    # These are operational startup metadata/logs. Protected learned/history data
    # retain exact values, row counts and identifiers in every pre-existing column.
    ignored = {'app_meta', 'events'}
    diffs = []
    hashes = {}
    for name, old in before.items():
        if name in ignored:
            continue
        new = after.get(name)
        if new is None or not set(old['columns']) <= set(new['columns']):
            diffs.append(name+': missing table/columns')
            continue
        indices = [new['columns'].index(c) for c in old['columns']]
        def packed(rows):
            return Counter(json.dumps(row, sort_keys=True, default=repr) for row in rows)
        previous = packed(old['rows'])
        current = packed([tuple(row[i] for i in indices) for row in new['rows']])
        if previous != current:
            diffs.append(name+': protected rows changed')
        hashes[name] = dict(rows=sum(previous.values()),
                           sha256=hashlib.sha256(json.dumps(sorted(previous.items())).encode()).hexdigest())
    if diffs:
        raise AssertionError(diffs)
    return hashes


def phase(args):
    os.environ['ADAPTIVE_AI_DATA'] = str(args.data_dir)
    os.environ.pop('SUPERVISOR_TOKEN', None)
    os.environ.pop('ADAPTIVE_AI_TRAINING_WORKER', None)
    sys.path.insert(0, str(args.source_root/'adaptive_ai/src'))
    from storage import STORE as store
    from observation_contract import install_training_contract, _migrate_models
    from agent_candidates import ensure_tables
    from agent_candidate_lineage import ensure_lineage_tables, _ensure_root, _register_generation
    from device_agents import ensure_tables as device_tables
    from manual_feedback_contract import ManualFeedbackJournal
    from policy import MultiHorizonPolicy
    from policy_backend import verify_model_checksum

    install_training_contract()
    ensure_tables(store)
    ensure_lineage_tables(store)
    device_tables(store)
    feedback = ManualFeedbackJournal(store)
    if args.phase == 'seed':
        stamp = time.time()-1000
        for index, mode in enumerate(('control', 'shadow', 'paused')):
            target = f'light.upgrade_{index}'
            agent = store.create_agent(dict(name=f'Upgrade fixture {index}', target_entity=target,
                target_property='power', min_value=0, max_value=1, deadband=.5,
                input_entities=['binary_sensor.upgrade'], enabled=True,
                confidence_threshold=.78, action_interval=1, micro_exploration=False,
                exploration_step=1))
            states = {target:dict(entity_id=target,state='off',attributes={})}
            policy = MultiHorizonPolicy(agent, states, {}, set())
            for horizon in policy.horizons:
                policy.update(horizon, 0, {0:1.}, .8)
                policy.update(horizon, 1, {0:1.}, .2)
            store.save_model(agent['id'],policy.serialize())
            store.set_training_state(agent['id'],'qualified',score=.9,samples=48)
            store.update_agent(agent['id'], {'mode':mode})
            store.archive_batch([(target,stamp,'off',{},None,'release-upgrade-fixture')])
            store.add_feedback(agent['id'],stamp,1,1,'manual',{0:1.},'fixture operator')
            feedback.record(agent_id=agent['id'],selected_ts=stamp,source='correct',
                            correct_action=1.,feedback_id=f'correct-{index}')
            feedback.record(agent_id=agent['id'],selected_ts=stamp+1,source='correct',
                            correct_action=0.,feedback_id=f'undo-{index}')
            feedback.undo(f'undo-{index}')
            parent = _ensure_root(store,agent['id'])
            child = store.create_agent({**agent,'id':None,'mode':'paused','name':f'Candidate {index}'})
            store.save_model(child['id'],store.get_model(agent['id']))
            store.set_training_state(child['id'],'qualified',score=.9,samples=48)
            _register_generation(store,agent['id'],parent,child['id'],1,'release_upgrade_fixture',state='comparing')
            with store.conn() as conn:
                conn.execute("""INSERT INTO agent_candidates
                    (parent_agent_id,candidate_id,generation,state,reason,feedback_revision,build_revision,
                     dirty,queued_ts,comparison_json,updated_ts)
                    VALUES(?,?,1,'comparing','release_upgrade_fixture',0,0,0,?,'{}',?)""",
                    (agent['id'],child['id'],stamp,stamp))
    changed = _migrate_models(SimpleNamespace(STORE=store,ENGINE=None))
    if changed:
        raise AssertionError(f'Ordinary startup requires retraining: {changed}')
    models = {}
    with store.conn() as conn:
        rows = conn.execute('SELECT agent_id,model_json FROM rl_models').fetchall()
    for aid, packed in rows:
        raw = json.loads(packed)
        assert verify_model_checksum(raw), aid
        agent = store.get_agent_config(aid)
        # Construction/read-only prediction must neither reinterpret nor rewrite persisted weights.
        model = MultiHorizonPolicy(agent,{}, {},set(),model=raw)
        model.predict({0:1.})
        assert verify_model_checksum(raw), aid
        models[aid] = dict(policy=raw['version'],schema=raw['schema']['version'],
                          sha256=hashlib.sha256(packed.encode()).hexdigest())
    print(json.dumps(dict(phase=args.phase,models=models,retrain=changed)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--previous-root', type=Path)
    parser.add_argument('--current-root', type=Path)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--phase',choices=('seed','validate'))
    parser.add_argument('--source-root',type=Path)
    parser.add_argument('--data-dir',type=Path)
    args = parser.parse_args()
    if args.phase:
        phase(args)
        return
    if not all((args.previous_root,args.current_root,args.output)):
        parser.error('--previous-root, --current-root and --output required')
    args.output.mkdir(parents=True,exist_ok=False)
    seed, upgraded, rollback = (args.output/n for n in ('previous-data','upgrade-copy','restore-copy'))
    seed.mkdir()
    # Exercise an existing user option without rewriting it during the upgrade.
    (seed/'options.json').write_text(json.dumps(dict(policy_half_life_days=30,
        universal_training_collector_enabled=False, agent_training_history_days=14)))
    def run(label,source,data,kind):
        cmd = [sys.executable,str(Path(__file__).resolve()),'--phase',kind,
               '--source-root',str(source.resolve()),'--data-dir',str(data.resolve())]
        with (args.output/f'{label}.log').open('x') as log:
            result = subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT)
        if result.returncode:
            raise RuntimeError(f'{label} failed: {args.output/label}.log')
    run('seed',args.previous_root,seed,'seed')
    initial = inventory(seed/'adaptive_ai.db')
    # Seeding via public APIs creates qualified models before old startup has
    # filled legacy missing cursors from the archive. Reopen with the OLD code
    # first, retaining this difference explicitly. The comparison baseline is
    # therefore a previous-version installation after its own normal restart.
    # No protected column is excluded or relaxed in upgrade comparisons.
    run('previous-restart',args.previous_root,seed,'validate')
    baseline = inventory(seed/'adaptive_ai.db')
    # The child process is stopped and all SQLite connections are closed here.
    shutil.copytree(seed,upgraded)
    option_hash = sha(seed/'options.json')
    results = []
    for index in range(2):
        run(f'upgrade-{index+1}',args.current_root,upgraded,'validate')
        result = check(baseline,inventory(upgraded/'adaptive_ai.db'))
        assert sha(upgraded/'options.json') == option_hash
        results.append(dict(restart=index+1,protected_tables=result))
    # Reopen the upgraded DB with old code as a diagnostic, never a rollback promise.
    down = args.output/'diagnostic-downgrade-copy'
    shutil.copytree(upgraded,down)
    try:
        run('diagnostic-downgrade',args.previous_root,down,'validate')
        check(baseline,inventory(down/'adaptive_ai.db'))
        downgrade='PASS (generated fixture only)'
    except Exception as exc:
        downgrade='FAIL: '+repr(exc)
    shutil.copytree(seed,rollback)
    run('full-restore',args.previous_root,rollback,'validate')
    check(baseline,inventory(rollback/'adaptive_ai.db'))
    assert sha(seed/'options.json') == option_hash
    assert sha(rollback/'options.json') == option_hash
    report = dict(scope='Generated cross-version fixture; selected migration guards, not full production startup',
                  status='PASS',upgrades=results,diagnostic_downgrade=downgrade,full_data_restore='PASS',
                  previous_startup_tables_changed=[name for name in initial if initial[name]!=baseline.get(name)],
                  existing_installation_copy='NOT RUN',production_composition='NOT RUN',
                  pending_job_recovery='NOT RUN',storage_faults='NOT RUN', options_sha256=option_hash)
    (args.output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(dict(status=report['status'],scope=report['scope'],tables=len(results[-1]['protected_tables']))))


if __name__=='__main__':
    main()
