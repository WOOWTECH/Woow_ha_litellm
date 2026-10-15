"""Interruptible long startup work outside the s6-rc state transition lock.

Each step has its own process group: stop/timeout reaches pg_dump, Prisma and
any grandchildren, before s6 stops PostgreSQL. Never include commands/env in logs.
"""
import os
import signal
import subprocess
import sys
import time

LIB = '/usr/local/lib/woow-litellm'
STARTUP = (('litellm-predump', 600), ('litellm-migrate', 1500), ('woow-revoke-ui-sessions', 60))
STOP_GRACE = 5
# Written once LiteLLM answers /health/liveliness, removed when it stops (litellm finish script):
# the Ingress nginx shows its "starting" page for its own 502 only while this file is absent.
READY_FLAG = '/run/woow/litellm.ready'
READY_POLL = 5
LIVELINESS = ['/usr/bin/curl', '-fs', '-o', '/dev/null', '--max-time', '3', 'http://127.0.0.1:4000/health/liveliness']


class Stopped(Exception):
    pass


def stop(_signum, _frame):
    raise Stopped()


def terminate_group(child):
    # Stop handlers must not interrupt cleanup when a second signal arrives.
    old = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + STOP_GRACE
        while time.monotonic() < deadline:
            child.poll()  # reap the leader, but also wait for any surviving grandchildren
            try:
                os.killpg(child.pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        child.wait()
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)


def run_step(command, timeout):
    # Block stop during spawn so a signal cannot orphan a newly created process.
    signals = {signal.SIGTERM, signal.SIGINT}
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, signals)
    child = None
    # Keep ownership continuous across spawn, pending-signal delivery on unmask,
    # and the handoff to wait. No exception after acquisition may orphan a child.
    try:
        try:
            child = subprocess.Popen(command, start_new_session=True,
                preexec_fn=lambda: signal.pthread_sigmask(signal.SIG_SETMASK, previous))
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)
        rc = child.wait(timeout=timeout)
        if rc:
            terminate_group(child)
        return rc
    except BaseException:
        if child is not None:
            terminate_group(child)
        raise


def clear_ready():
    try:
        os.unlink(READY_FLAG)
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f'[woow-litellm] WARNING could not clear the readiness flag ({type(exc).__name__})', file=sys.stderr)


def mark_ready():
    try:
        os.close(os.open(READY_FLAG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644))
    except OSError as exc:  # the sidebar keeps its "starting" page for nginx 502s; nothing else depends on it
        print(f'[woow-litellm] WARNING could not write the readiness flag ({type(exc).__name__})', file=sys.stderr)


def start_litellm():
    clear_ready()
    for name, seconds in STARTUP:
        if run_step([f'{LIB}/{name}'], seconds):
            return 1
    # Restore credentials under the same cancellation boundary before starting the API.
    if run_step([f'{LIB}/litellm-restore'], 60):
        return 1
    os.execv(f'{LIB}/litellm-start', [f'{LIB}/litellm-start'])


def indexes():
    while run_step(LIVELINESS, 5):
        time.sleep(2)
    mark_ready()
    # Warn about names the HA Ingress cannot address and arm its write guard (never fatal).
    run_step([f'{LIB}/woow-name-scan'], 60)
    run_step([f'{LIB}/woow-spendlogs-indexes'], None)
    # Do not rebuild in a restart loop after success (or the documented warning). Only keep the
    # readiness flag in step with LiteLLM restarts: probe while the flag is absent.
    while True:
        if not os.path.exists(READY_FLAG) and run_step(LIVELINESS, 5) == 0:
            mark_ready()
        time.sleep(READY_POLL)


def main():
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    try:
        return start_litellm() if sys.argv[1] == 'litellm' else indexes()
    except Stopped:
        return 143
    except subprocess.TimeoutExpired:
        print('[woow-litellm] startup step timed out; refusing to continue', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
