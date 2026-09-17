"""Start the built add-on image and verify the actual shipped entrypoint and HTTP readiness."""
import json
import subprocess
import sys

CHECK = r'''
import json, time, urllib.request
deadline = time.monotonic() + 45
last = 'no HTTP response'
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen('http://127.0.0.1:8099/health', timeout=2) as response:
            status = json.load(response)
        last = str(status)
        if status.get('startup', {}).get('error'):
            raise SystemExit(last)
        if status.get('ready'):
            print(json.dumps(status))
            break
    except OSError as exc:
        last = str(exc)
    time.sleep(.2)
else:
    raise SystemExit('Container did not become ready: ' + last)
'''


def main():
    image = sys.argv[1] if len(sys.argv) > 1 else 'homemind:smoke'
    container = subprocess.check_output(
        ['docker', 'run', '--detach', '--network', 'none', image], text=True).strip()
    try:
        # run.sh uses exec, therefore PID 1 must be the final Python entrypoint rather
        # than an untested helper shell or an older queue_main layer.
        cmdline = subprocess.check_output(
            ['docker', 'exec', container, 'sh', '-c', "tr '\\000' ' ' </proc/1/cmdline"], text=True
        ).strip()
        if 'trial_queue_main.py' not in cmdline:
            raise RuntimeError(f'Unexpected image PID1: {cmdline}')
        subprocess.run(['docker', 'exec', container, 'python3', '-c', CHECK], check=True, timeout=60)
        print(json.dumps({'image': image, 'pid1': cmdline, 'entrypoint_verified': True}))
    except Exception:
        subprocess.run(['docker', 'logs', container], check=False)
        raise
    finally:
        subprocess.run(['docker', 'rm', '--force', container], check=True)


if __name__ == '__main__':
    main()
