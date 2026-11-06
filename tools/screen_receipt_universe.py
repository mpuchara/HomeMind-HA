"""Pure validation CPU screen; not Gate D or native qualification acceptance."""
import json
from pathlib import Path
import sys
import time

root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root/'tests'), str(root/'adaptive_ai/src')]
from test_receipt_writer_differential import ReceiptWriterDifferentialTests, old_validate
import universal_affected_sensors as affected
from universal_history_collector import encoded, digest


def main():
    fixture = ReceiptWriterDifferentialTests()
    fixture.setUp()
    try:
        f = fixture.fixture.f
        fixture.fixture.declare([], empty_reason='independent fixture sensor')
        f.prepare(); _, parent = f.bind()
        evidence = parent['affected_sensor_evidence']
        results = []
        for count in (50, 1000):
            universe = ['sensor.s'+str(i) for i in range(count)]
            doc = json.loads(evidence['declaration_json'])
            doc.update(sensor_universe_count=count, sensor_universe_revision=digest(encoded(sorted(universe))))
            raw = encoded(doc)
            proof = dict(evidence, declaration_json=raw, declaration_checksum=digest(raw), declaration_revision=digest(raw))
            prepared = affected._PreparedSensorUniverse(universe)
            assert old_validate(proof,parent,sensor_universe=universe) == affected.validate(proof,parent,sensor_universe=prepared)
            for pair in range(6):
                row = dict(sensors=count, pair=pair+1, iterations=2000)
                for arm in (('old','new') if pair%2==0 else ('new','old')):
                    function, source = (old_validate, universe) if arm=='old' else (affected.validate, prepared)
                    start = time.thread_time_ns()
                    for _ in range(2000): function(proof,parent,sensor_universe=source)
                    row[arm+'_cpu_ns'] = time.thread_time_ns()-start
                results.append(row)
        Path(sys.argv[1]).write_text(json.dumps(dict(diagnostic_only=True, note='Pure derived-input validation; setup excluded; native work and preparation cost excluded. Not acceptance.', results=results), indent=2))
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    main()
