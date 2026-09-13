"""Start the built add-on image with no HA network or data; verify HTTP readiness."""
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
        subprocess.run(['docker', 'exec', container, 'python3', '-c', CHECK], check=True, timeout=60)
    except Exception:
        subprocess.run(['docker', 'logs', container], check=False)
        raise
    finally:
        subprocess.run(['docker', 'rm', '--force', container], check=True)


if __name__ == '__main__':
    main()
