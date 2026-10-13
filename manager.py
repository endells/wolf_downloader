"""FF-facing lightweight process controller. No HTTP/download/ZIP on FF threads."""
import fcntl
import os
import subprocess
import sys
import uuid
from pathlib import Path
from .common import WolfError, read_json, write_json, write_log


class Manager:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.process = None

    def busy(self):
        with (self.directory / 'worker.lock').open('a+b') as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(f, fcntl.LOCK_UN)
            return False

    def start(self, mode, config, args=None):
        lock = (self.directory / 'worker.lock').open('a+b')
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise WolfError('작업이 진행 중입니다. 완료되거나 중지된 뒤 다시 실행하세요.')
            if self.process is not None:
                self.process.poll()  # Reap the previous subprocess.
            job_id = uuid.uuid4().hex
            (self.directory / 'cancel').unlink(missing_ok=True)
            (self.directory / 'result.json').unlink(missing_ok=True)
            write_json(self.directory / 'job.json', {'id': job_id, 'mode': mode, 'config': config, 'args': args or {}})
            write_json(self.directory / 'status.json', {'id': job_id, 'mode': mode, 'status': 'running', 'message': '작업 시작 중', 'done': 0, 'total': 0})
            worker = str(Path(__file__).with_name('runner.py'))
            self.process = subprocess.Popen([sys.executable, worker, str(self.directory)],
                                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL, close_fds=True,
                                            pass_fds=(lock.fileno(),), start_new_session=True)
            write_log(self.directory, 'INFO', '[{}] 작업 접수: {}'.format(job_id[:8], mode))
            return job_id
        except Exception:
            # Credentials should never be left in a pending job if spawn failed.
            # A busy attempt must not remove another worker's job file.
            if 'job_id' in locals():
                (self.directory / 'job.json').unlink(missing_ok=True)
                write_json(self.directory / 'status.json', {'status': 'error', 'message': '작업 프로세스를 시작하지 못했습니다.'})
                write_log(self.directory, 'ERROR', '작업 프로세스 시작 실패')
            raise
        finally:
            # Do not LOCK_UN here: child owns the inherited open-file lock.
            lock.close()

    def status(self):
        active = self.busy()
        if self.process:
            self.process.poll()
        state = read_json(self.directory / 'status.json', {'status': 'idle', 'message': '대기'})
        state['active'] = active
        if not active and state.get('status') == 'running':
            state['status'] = 'interrupted'
            state['message'] = '작업 프로세스가 종료되었습니다. 기존 파일은 보존되며 다시 시도할 수 있습니다.'
            (self.directory / 'job.json').unlink(missing_ok=True)
            write_json(self.directory / 'status.json', state)
            write_log(self.directory, 'WARNING', state['message'])
        return state

    def result(self):
        return read_json(self.directory / 'result.json', {})

    def cancel(self):
        (self.directory / 'cancel').touch(mode=0o600)
        write_log(self.directory, 'INFO', '작업 중지 요청')
