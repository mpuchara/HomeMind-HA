"""Alternating unprofiled screen against the previous production writer."""
import argparse
from pathlib import Path
from run_gate_d_snapshot_study import compare, manifest, run, save


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    args = parser.parse_args()
    out = args.root/'screen'
    out.mkdir()
    plan = [(name+'-'+str(index+1), scale, bool(index%2))
            for name,scale in [('small50','50:10'),('normal','1000:200')] for index in range(2)]
    save(out/'protocol.json',dict(diagnostic_only=True, ticks=24,period=.4,fanout=16,plan=plan,
        manifests={arm:manifest(args.root/arm) for arm in ('before','source')}))
    results=[]
    for name,scale,reverse in plan:
        arms=('source','before') if reverse else ('before','source')
        rows={arm:run(args.root/arm,out,name+'-'+arm,scale) for arm in arms}
        result=dict(name=name,**compare(rows['before'],rows['source']))
        results.append(result)
        save(out/(name+'-comparison.json'),result)
        print(result,flush=True)
    save(out/'summary.json',results)


if __name__=='__main__':
    main()
