"""Executed by a fresh Python interpreter, outside FF/gevent's process."""
import os
import sys
from pathlib import Path


def main():
    directory = Path(sys.argv[1])
    # Lower CPU priority; use one CPU and bounded address space when available.
    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass
    try:
        allowed = os.sched_getaffinity(0)
        os.sched_setaffinity(0, {max(allowed)})
    except (AttributeError, OSError):
        pass
    try:
        import resource
        cap = 512 * 1024 * 1024
        old_soft, old_hard = resource.getrlimit(resource.RLIMIT_AS)
        cap = min(cap, old_hard) if old_hard != resource.RLIM_INFINITY else cap
        resource.setrlimit(resource.RLIMIT_AS, (cap, old_hard))
    except (ImportError, OSError, ValueError):
        pass
    from common import read_json, write_json, Cancelled
    from engine import Engine
    job = read_json(directory / 'job.json')
    (directory / 'job.json').unlink(missing_ok=True)
    if not job:
        write_json(directory / 'status.json', {'status': 'error', 'message': '작업 설정을 읽지 못했습니다.'})
        return
    engine = None
    try:
        engine = Engine(directory, job)
        engine.run()
    except Cancelled as exc:
        if engine:
            engine.status(status='canceled', message=str(exc))
    except Exception as exc:
        # Do not dump request objects, cookies or webhook URLs into logs/status.
        from common import WolfError
        message = str(exc) if isinstance(exc, WolfError) else '작업 오류: ' + type(exc).__name__
        if engine:
            engine.status(status='error', message=message)
            engine.notify('alert', message)
        else:
            write_json(directory / 'status.json', {'id': job['id'], 'status': 'error', 'message': message})


if __name__ == '__main__':
    main()
