"""Start the local demo detached from the invoking terminal; no login auto-start."""
from pathlib import Path
import os
import socket
import subprocess
import time

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / '.runtime/services'
RUNTIME.mkdir(parents=True, exist_ok=True)
RUNTIME.chmod(0o700)
PYTHON = str(ROOT / '.venv/bin/python')


def listening(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=.5):
            return True
    except OSError:
        return False


def main():
    services = [
        ('hermes', 8642, [PYTHON, str(ROOT/'scripts/start_hermes.py')]),
        ('web', 8765, [PYTHON, str(ROOT/'scripts/serve_web.py')]),
    ]
    for name,port,command in services:
        if listening(port):
            print(f'{name}: already listening; no duplicate started', flush=True)
            continue
        log_path = RUNTIME / f'{name}.log'
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.chmod(log_path, 0o600)
        with os.fdopen(fd, 'ab', buffering=0) as log:
            process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
        (RUNTIME/f'{name}.pid').write_text(str(process.pid)+'\n')
        for _ in range(40):
            if listening(port):
                print(f'{name}: listening',flush=True)
                break
            if process.poll() is not None:
                raise SystemExit(f'{name}: exited; inspect local service log')
            time.sleep(.5)
        else:
            raise SystemExit(f'{name}: startup not yet confirmed; process retained, inspect local service log')


if __name__ == '__main__':
    main()
