import json
import os
import re
import secrets
import threading
from pathlib import Path
from urllib.parse import urlsplit

from flask import jsonify, render_template, session
from framework import F, Job
from plugin import PluginModuleBase

from .common import DEFAULTS, WolfError, parse_id, read_json, title_ids, validate, write_json, write_log
from .history import History
from .manager import Manager


class ModuleBasic(PluginModuleBase):
    db_default = DEFAULTS

    def __init__(self, P):
        super().__init__(P, first_menu='setting', name='basic', scheduler_desc='늑대 자동 수집')
        self.manager = None
        self.settings_lock = threading.RLock()

    def data_dir(self):
        return Path(F.config['path_data']) / 'wolf_downloader'

    def _manager(self):
        if self.manager is None:
            self.manager = Manager(self.data_dir())
        return self.manager

    def config(self):
        raw = {k: self.P.ModelSetting.get(k) for k in DEFAULTS}
        raw = {k: (DEFAULTS[k] if v is None else v) for k, v in raw.items()}
        if not raw.get('download_path'):
            raw['download_path'] = str(Path(F.config['path_data']) / 'downloads' / 'wolf_downloader')
        return validate(raw)

    def domain_state(self, config=None):
        config = config or self.config()
        cached = read_json(self.data_dir() / 'domain.json', {})
        valid = (cached.get('configured_base') == config['base_url'] and
                 cached.get('family') == config['domain_family'] and bool(cached.get('base_url')))
        return {'url': cached['base_url'] if valid else config['base_url'],
                'verified': bool(valid)}

    def runtime_config(self):
        config = self.config()
        secret = read_json(self.data_dir() / 'secrets.json', {})
        config.update(cookie=secret.get('cookie', ''), cookie_origin=secret.get('cookie_origin', ''), secrets=secret)
        return config

    def sync_schedule(self, config, previous=None):
        name = self.get_scheduler_name()
        included = F.scheduler.is_include(name)
        if not config['basic_auto_start']:
            if included:
                F.scheduler.remove_job(name)
            return
        if included and (previous is None or previous == config['basic_interval']):
            return
        if included:
            F.scheduler.remove_job(name)
        job = Job(self.P.package_name, name, str(config['basic_interval']), self.scheduler_function, self.get_scheduler_desc())
        F.scheduler.add_job_instance(job, run=False)

    def plugin_load(self):
        self._manager()
        self.sync_schedule(self.config())
        write_log(self.data_dir(), 'INFO', '플러그인 로드 완료 — 작업 로그 기록 시작')

    def plugin_unload(self):
        self._manager().cancel()
        name = self.get_scheduler_name()
        if F.scheduler.is_include(name):
            F.scheduler.remove_job(name)

    def scheduler_function(self):
        try:
            self._manager().start('download', self.runtime_config())
        except WolfError:
            write_log(self.data_dir(), 'WARNING', '늑대 자동 수집 시작 불가: 진행 중 작업 또는 설정 확인 필요')
        except Exception as exc:
            write_log(self.data_dir(), 'ERROR', '늑대 자동 수집 시작 실패: ' + type(exc).__name__)

    def process_menu(self, page, req):
        if page not in ('setting', 'browse', 'manual', 'status', 'history', 'log'):
            page = 'setting'
        key = 'wolf_downloader_csrf'
        if key not in session:
            session[key] = secrets.token_urlsafe(32)
        secret = read_json(self.data_dir() / 'secrets.json', {})
        return render_template('wolf_downloader_basic.html', page=page, config=self.config(),
                               package_name=self.P.package_name, csrf_token=session[key], domain_state=self.domain_state(),
                               secrets_saved={k: bool(secret.get(k)) for k in ('cookie', 'notify_webhook_download', 'notify_webhook_alert')})

    def save_secrets(self, raw, conf):
        path = self.data_dir() / 'secrets.json'
        value = read_json(path, {})
        for key in ('cookie', 'notify_webhook_download', 'notify_webhook_alert'):
            if raw.get('clear_' + key) is True:
                value.pop(key, None)
                if key == 'cookie':
                    value.pop('cookie_origin', None)
            elif raw.get(key):
                text = str(raw[key]).strip()
                if len(text) > 32768 or '\n' in text or '\r' in text:
                    raise WolfError('쿠키/웹훅은 줄바꿈 없이 입력하세요 (최대 32KB).')
                if key == 'cookie':
                    if not conf['base_url']:
                        raise WolfError('쿠키를 저장할 도메인을 먼저 입력하세요.')
                    value['cookie_origin'] = conf['base_url']
                else:
                    p = urlsplit(text)
                    valid = (p.hostname in ('discord.com', 'discordapp.com') and p.path.startswith('/api/webhooks/')) or (p.hostname == 'hooks.slack.com' and p.path.startswith('/services/'))
                    if p.scheme != 'https' or p.username or p.password or p.port not in (None, 443) or not valid:
                        raise WolfError('Discord 또는 Slack의 HTTPS 웹훅 주소를 입력하세요.')
                value[key] = text
        write_json(path, value)

    def process_command(self, command, arg1, arg2, arg3, req):
        try:
            expected = session.get('wolf_downloader_csrf', '')
            if not expected or not secrets.compare_digest(expected, req.form.get('csrf_token', '')):
                return jsonify(ret='error', msg='페이지를 새로고침하고 다시 시도하세요.'), 403
            manager = self._manager()
            if command == 'status':
                state = manager.status()
                state['domain_state'] = self.domain_state()
                state['base_url'] = state['domain_state']['url']
                state['scheduler_enabled'] = F.scheduler.is_include(self.get_scheduler_name())
                return jsonify(ret='success', state=state)
            if command == 'result':
                return jsonify(ret='success', result=manager.result())
            if command == 'logs':
                path = self.data_dir().parent / 'log' / 'wolf_downloader.log'
                try:
                    with path.open('rb') as stream:
                        stream.seek(0, os.SEEK_END)
                        size = stream.tell()
                        start = max(0, size - 262144)
                        stream.seek(start)
                        data = stream.read(262144)
                    if start:
                        data = data.partition(b'\n')[2]
                except FileNotFoundError:
                    data = b''
                rows = []
                for line in data.decode('utf-8', errors='replace').splitlines()[-500:]:
                    match = re.match(r'^\[([^|]+)\|(DEBUG|INFO|WARNING|ERROR|CRITICAL)\|wolf_downloader\]\s*(.*)$', line)
                    rows.append({'time': match[1] if match else '', 'level': match[2] if match else 'INFO',
                                 'message': match[3] if match else line})
                return jsonify(ret='success', rows=list(reversed(rows)))
            if command == 'history':
                data = json.loads(arg1 or '{}')
                rows = History(self.data_dir() / 'history.db').rows(max(1, min(100000, int(data.get('page', 1)))), str(data.get('query', '')), str(data.get('status', '')))
                return jsonify(ret='success', **rows)
            if command == 'save':
                raw = json.loads(arg1 or '{}')
                conf = validate(raw)
                with self.settings_lock:
                    previous = self.config()['basic_interval']
                    self.save_secrets(raw, conf)
                    for key, value in conf.items():
                        self.P.ModelSetting.set(key, str(value))
                    self.sync_schedule(conf, previous)
                write_log(self.data_dir(), 'INFO', '설정 저장 완료 (다운로드 실행 없음)')
                return jsonify(ret='success', msg='저장했습니다. 다운로드는 시작하지 않습니다.')
            if command == 'recent_works':
                return jsonify(ret='success', rows=History(self.data_dir() / 'history.db').recent_works())
            if command == 'titles':
                key = 'separate_titles' if arg1 == 'separate' else 'titles'
                return jsonify(ret='success', rows=History(self.data_dir() / 'history.db').registered(title_ids(self.P.ModelSetting.get(key) or '')))
            if command == 'remove_auto':
                wid = parse_id(arg1)
                with self.settings_lock:
                    key = 'separate_titles' if arg2 == 'separate' else 'titles'
                    ids = title_ids(self.P.ModelSetting.get(key) or '')
                    self.P.ModelSetting.set(key, '\n'.join(x for x in ids if x != wid))
                return jsonify(ret='success', msg='자동 수집 목록에서 제외했습니다. 저장 파일과 이력은 유지합니다.')
            if command == 'run_title':
                wid = parse_id(arg1)
                conf = self.runtime_config()
                if wid not in title_ids(conf['titles']) + title_ids(conf['separate_titles']):
                    raise WolfError('자동 수집 목록에 등록된 작품만 실행할 수 있습니다.')
                job = manager.start('download', conf, {'work_id': wid})
                return jsonify(ret='success', msg='선택 작품의 미완료 회차 수집을 시작했습니다.', job_id=job)
            if command == 'add_auto':
                wid = parse_id(arg1)
                with self.settings_lock:
                    key, other = ('separate_titles', 'titles') if arg2 == 'separate' else ('titles', 'separate_titles')
                    if key == 'separate_titles' and not self.config()['separate_path']:
                        raise WolfError('설정에서 별도 저장 경로를 먼저 저장하세요.')
                    ids = title_ids(self.P.ModelSetting.get(key) or '')
                    if wid not in ids:
                        ids.append(wid)
                        self.P.ModelSetting.set(key, '\n'.join(ids))
                    self.P.ModelSetting.set(other, '\n'.join(x for x in title_ids(self.P.ModelSetting.get(other) or '') if x != wid))
                write_log(self.data_dir(), 'INFO', '자동 수집 작품 등록: ' + wid)
                return jsonify(ret='success', msg='자동 수집 목록에 등록했습니다. 즉시 다운로드하지 않습니다.')
            if command == 'browse_path':
                path = Path(str(arg1 or '/'))
                if not path.is_absolute():
                    raise WolfError('절대 경로를 입력하세요.')
                while not (path.is_dir() and os.access(str(path), os.R_OK | os.X_OK)):
                    if path == path.parent:
                        raise WolfError('열 수 있는 상위 경로가 없습니다.')
                    path = path.parent
                return jsonify(ret='success', path=str(path))
            if command == 'stop':
                manager.cancel()
                return jsonify(ret='success', msg='중지 요청을 보냈습니다. 진행 중 네트워크 응답은 최대 15초 정도 기다릴 수 있습니다.')
            args = {}
            if command in ('analyze', 'download'):
                args['work_id'] = parse_id(arg1)
                args['separate'] = req.form.get('separate', '') == 'true'
                if args['separate'] and not self.config()['separate_path']:
                    raise WolfError('설정에서 별도 저장 경로를 먼저 저장하세요.')
                if command == 'download':
                    selected = json.loads(arg2 or '[]')
                    if not isinstance(selected, list) or not selected or len(selected) > 10000 or any(not str(x).isdigit() for x in selected):
                        raise WolfError('다운로드할 회차를 선택하세요.')
                    args['selected'] = list(dict.fromkeys(str(x) for x in selected))
            elif command == 'browse':
                args = json.loads(arg1 or '{}')
                if not isinstance(args, dict):
                    raise WolfError('목록 요청 형식 오류')
            elif command in ('run', 'resolve', 'probe', 'metadata', 'reconcile', 'compress', 'notify_test'):
                if command == 'notify_test':
                    args['kind'] = 'download' if arg1 == 'download' else 'alert'
            else:
                raise WolfError('지원하지 않는 명령입니다.')
            job = manager.start('download' if command == 'run' else command, self.runtime_config(), args)
            return jsonify(ret='success', msg='백그라운드 작업을 시작했습니다.', job_id=job)
        except (WolfError, ValueError, TypeError, KeyError) as exc:
            write_log(self.data_dir(), 'WARNING', '요청 처리 실패: ' + type(exc).__name__)
            return jsonify(ret='error', msg=str(exc))
        except Exception as exc:
            write_log(self.data_dir(), 'ERROR', '요청 처리 오류: ' + type(exc).__name__)
            return jsonify(ret='error', msg='처리 실패: FF 로그를 확인하세요.')


