"""Real WAL control, durable deferral and composed ingress evidence (host only)."""
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

ROOT=Path(__file__).resolve().parents[1]
REFERENCE='01c3bd76c3d4e14f0c39aefe533bbf57844a9a6a'


def ingress(source):
    process=subprocess.run([sys.executable,str(ROOT/'tools/benchmark_runtime_scaling.py'),
        '--source-root',str(source),'--agents','3','--events','1000','--warmup','10',
        '--training','--history-preparation'],cwd=ROOT,capture_output=True,text=True,timeout=900)
    if process.returncode:
        raise RuntimeError(process.stdout+process.stderr)
    lines=[line for line in process.stdout.splitlines() if line.startswith('SCALING_JSON=')]
    result=json.loads(lines[-1].split('=',1)[1])
    assert result['history_preparation']['runs']>0,result['background_work']
    assert not result['background_work']['errors'],result['background_work']
    assert result['events']*result['agents']==len(result['samples']), 'lost ingress observation'
    assert result['ha_calls']==0
    return result


def main():
    sys.path.insert(0,str(ROOT/'tests'))
    import support
    import test_training_wal_0158 as tests
    suite=tests.TrainingWALTests()
    suite.setUp()
    try:
        suite.test_01_real_wal_reader_starvation_control_and_released_batches()
        control=suite.wal_report
    finally:
        suite.doCleanups()
    outcomes={}
    for name in ('test_02_actual_soft_wal_pressure_releases_and_resumes_same_copy',
                 'test_06_supervisor_retries_same_descriptor_after_real_wal_maintenance',
                 'test_07_durable_wait_descriptor_is_recovered_without_rebuild',
                 'test_08_killed_waiting_supervisor_recovers_durable_job'):
        case=tests.TrainingWALTests()
        case.setUp()
        try:
            getattr(case,name)()
            outcomes[name]='PASS'
        finally:
            case.doCleanups()
    try:
        raw=subprocess.check_output(['git','archive','--format=tar',REFERENCE,'adaptive_ai/src','tests','tools'],cwd=ROOT,stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        subprocess.run(['git','fetch','--depth=1','origin',REFERENCE],cwd=ROOT,check=True)
        raw=subprocess.check_output(['git','archive','--format=tar',REFERENCE,'adaptive_ai/src','tests','tools'],cwd=ROOT)
    with tempfile.TemporaryDirectory(prefix='hm-wal-reference-') as directory:
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            for member in archive.getmembers():
                target=(Path(directory)/member.name).resolve()
                if not target.is_relative_to(Path(directory).resolve()) or member.issym() or member.islnk():
                    raise RuntimeError('Invalid reference archive path')
            archive.extractall(directory)
        reference=ingress(Path(directory))
    candidate=ingress(ROOT)
    result=dict(environment='Host synthetic Engine ingress; no HA websocket/ACK or physical Pi acceptance',
        reference_commit=REFERENCE,wal_control=control,pressure_resume=outcomes,
        realtime_reference=reference,realtime_candidate=candidate)
    target=ROOT/'docs/benchmarks/training-wal-0158.json'
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(dict(control_peak=control['long_reader']['wal_peak_bytes'],
        bounded_peak=control['bounded_copy']['wal_peak_bytes'],
        reference_latency=reference['event_to_pipeline_return'],candidate_latency=candidate['event_to_pipeline_return']),indent=2))
    print(target)


if __name__=='__main__':
    main()
