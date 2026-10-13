"""Inject a real stop at startup ownership boundaries; always reap our child."""
import json
import os
from pathlib import Path
import signal
import sys
import types

source, boundary, signal_name = sys.argv[1:]
service = types.ModuleType('startup_fixture')
exec(compile(Path(source).read_bytes(), source, 'exec'), service.__dict__)
signum = getattr(signal, signal_name)
old_handler = signal.signal(signum, service.stop)
old_mask = signal.pthread_sigmask(signal.SIG_UNBLOCK, {signum})
old_trace = sys.gettrace()
child = None
triggered = False
caught = False
alive = None


def trace(frame, event, _arg):
    global child, triggered
    if event == 'line' and frame.f_code is service.run_step.__code__ and not triggered:
        child = frame.f_locals.get('child')
        if child is not None:
            blocked = signum in signal.pthread_sigmask(signal.SIG_BLOCK, set())
            # While blocked, queue delivery in unmask itself. Otherwise inject at
            # the very first line after unmask, including an intervening try line.
            if blocked == (boundary == 'unmask'):
                triggered = True
                os.kill(os.getpid(), signum)
    return trace


try:
    sys.settrace(trace)
    try:
        service.run_step([sys.executable, '-c', 'import time; time.sleep(30)'], 10)
    except service.Stopped:
        caught = True
    finally:
        sys.settrace(old_trace)
    alive = child is not None and child.poll() is None
finally:
    sys.settrace(old_trace)
    signal.signal(signum, old_handler)
    signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
    if child is not None:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=5)

result = dict(boundary=boundary, signal=signal_name, signal_delivered=triggered,
              stopped_exception_propagated=caught, startup_child_survived_run_step=alive,
              owned_fixture_child_reaped=child is not None and child.returncode is not None)
print(json.dumps(result))
assert triggered and caught, result
assert alive is False, result
assert result['owned_fixture_child_reaped'], result
