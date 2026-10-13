"""Serial, cancellable download engine running only in the child interpreter."""
import hashlib
import json
import os
import shutil
import time
import zipfile
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from xml.etree import ElementTree as ET

from client import Client, parse_images, image_extension
from common import WolfError, Blocked, Cancelled, atomic_bytes, inside, read_json, safe_name, title_ids, write_json
from domains import resolve, verify
from history import History


class Engine:
    def __init__(self, directory, job, client=None):
        self.directory, self.job = Path(directory), job
        self.config, self.args = job['config'], job.get('args', {})
        self.client = client or Client(self.config, lambda: (self.directory / 'cancel').exists())
        self.history = History(self.directory / 'history.db')
        self.history.interrupt_stale()
        self.state = {'id': job['id'], 'mode': job['mode'], 'status': 'running',
                      'done': 0, 'total': 0, 'completed': 0, 'skipped': 0, 'failed': 0,
                      'pages_done': 0, 'pages_total': 0, 'warnings': []}
        self.last_status = 0

    def status(self, **values):
        self.state.update(values)
        write_json(self.directory / 'status.json', self.state)

    def warn(self, message):
        self.state['warnings'] = (self.state['warnings'] + [message])[-10:]
        self.status()

    def notify(self, kind, message):
        secret = self.config.get('secrets', {})
        url = secret.get('notify_webhook_' + kind, '')
        if not url:
            return
        try:
            p = urlsplit(url)
            if p.hostname in ('discord.com', 'discordapp.com') and p.path.startswith('/api/webhooks/'):
                body = {'content': message[:1800], 'allowed_mentions': {'parse': []}}
            elif p.hostname == 'hooks.slack.com' and p.path.startswith('/services/'):
                body = {'text': message[:1800], 'mrkdwn': False}
            else:
                raise WolfError('지원하지 않는 웹훅 주소')
            with self.client.request(url, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'}) as response:
                response.read(4096)
        except Exception:
            # Notification failure must never invalidate a completed download.
            self.warn('알림 전송 실패: 웹훅 주소와 접속 상태를 확인하세요.')

    def result(self, **values):
        write_json(self.directory / 'result.json', dict(values, job_id=self.job['id']))

    def connect(self, force=False):
        cached = read_json(self.directory / 'domain.json', {})
        if cached.get('configured_base') == self.config['base_url'] and cached.get('family') == self.config['domain_family']:
            self.client.base = cached.get('base_url') or self.client.base
        previous = self.client.base
        new = resolve(self.client, force=force, progress=lambda message: self.status(message=message))
        self.client.base = new
        # Cookies are only valid for the exact domain where the user saved them.
        if self.config.get('cookie_origin') != new:
            self.config['cookie'] = ''
        write_json(self.directory / 'domain.json', {'base_url': new, 'configured_base': self.config['base_url'], 'family': self.config['domain_family']})
        self.status(base_url=new)
        if previous != new:
            self.notify('alert', '늑대 다운로더 도메인 확인: ' + new)
        return new

    def work_dir(self, work):
        return inside(self.config['download_path'], '{} [{}]'.format(safe_name(work['title']), work['id']))

    def episode_name(self, ep):
        no = ep['no']
        if float(no).is_integer():
            number = str(int(no)).zfill(self.config['episode_digits'])
        else:
            number = str(no).zfill(self.config['episode_digits'])
        return '{}_{} [{}]'.format(number, safe_name(ep['title']), ep['id'])

    def metadata(self, work, folder):
        folder.mkdir(parents=True, exist_ok=True)
        root = ET.Element('ComicInfo')
        for key, value in [('Series', work['title']), ('Summary', work.get('description', '')), ('Web', self.client.base + '/list?toon=' + work['id'])]:
            ET.SubElement(root, key).text = value
        atomic_bytes(folder / 'info.xml', ET.tostring(root, encoding='utf-8', xml_declaration=True))
        write_json(folder / '.wolf-work.json', {'id': work['id'], 'title': work['title']})
        if work.get('cover') and not any((folder / ('cover' + ext)).exists() for ext in ('.jpg', '.png', '.gif', '.webp', '.avif')):
            try:
                self.client.image(work['cover'], folder / 'cover.img', self.client.base + '/list?toon=' + work['id'])
            except (Cancelled, Blocked):
                raise
            except (WolfError, OSError):
                self.warn(work['title'] + ': 표지 저장 실패 (본문 다운로드는 계속)')

    def complete(self, record):
        if not record or record['status'] != 'completed' or not record.get('path'):
            return False
        path = Path(record['path'])
        # History is reusable only within the current root; a new destination
        # must actually receive downloads, even if old history still exists.
        root = Path(self.config['download_path']).resolve()
        if root not in path.resolve().parents:
            return False
        return self.check_artifact(path, int(record.get('pages') or 0))

    def check_artifact(self, path, expected):
        if expected <= 0 or not path.exists():
            return False
        try:
            if path.is_file() and path.suffix in ('.zip', '.cbz'):
                with zipfile.ZipFile(path) as z:
                    images = [i for i in z.infolist() if Path(i.filename).suffix.lower() in ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.avif')]
                    return len(images) == expected and all(i.file_size > 0 for i in images)
            files = [p for p in path.iterdir() if p.suffix.lower() in ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.avif') and p.is_file()]
            return len(files) == expected and all(p.stat().st_size > 0 for p in files)
        except (OSError, zipfile.BadZipFile):
            return False

    def zip_folder(self, folder, target):
        temp = target.with_name(target.name + '.part')
        files = sorted(p for p in folder.iterdir() if p.is_file() and not p.name.endswith('.part'))
        try:
            # JPEG/WebP/PNG are already compressed. ZIP_STORED avoids CPU-heavy
            # recompression; copy in chunks to permit cancellation and I/O pacing.
            with zipfile.ZipFile(temp, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                for p in files:
                    self.client.check()
                    with p.open('rb') as src, archive.open(p.name, 'w', force_zip64=True) as dst:
                        while True:
                            self.client.check()
                            b = src.read(256 * 1024)
                            if not b:
                                break
                            dst.write(b)
                            self.client.wait(.005)
            self.verify_zip(temp)
            self.client.check()
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)

    def verify_zip(self, path):
        with zipfile.ZipFile(path) as archive:
            for item in archive.infolist():
                self.client.check()
                with archive.open(item) as stream:
                    while stream.read(256 * 1024):
                        self.client.wait(.005)

    def download_episode(self, work, ep, folder):
        wid, eid = work['id'], ep['id']
        self.status(work_title=work['title'], episode=ep['title'], pages_done=0, pages_total=0, message='회차 분석 중')
        self.history.save(wid, eid, work_title=work['title'], title=ep['title'], status='downloading', error='')
        url = self.client.base + '/view?' + urlencode({'toon': wid, 'num': eid})
        html, actual = self.client.html(url)
        images = parse_images(html, actual, self.config['viewer_selector'])
        stage = inside(folder, '.download-' + eid)
        stage.mkdir(exist_ok=True)
        manifest = read_json(stage / '.wolf-episode.json', {})
        fingerprint = hashlib.sha256('\n'.join(images).encode()).hexdigest()
        if manifest.get('fingerprint') != fingerprint:
            # Stage belongs exclusively to this work/episode, never final data.
            for p in stage.iterdir():
                if p.is_file() and (p.name[:3].isdigit() or p.name == '.wolf-episode.json'):
                    p.unlink()
            manifest = {'work_id': wid, 'ep_id': eid, 'fingerprint': fingerprint, 'files': {}, 'pages': len(images)}
        self.status(pages_total=len(images), message='이미지 다운로드 중')
        total = 0
        for index, image in enumerate(images, 1):
            self.client.check()
            saved = manifest['files'].get(str(index), {})
            p = inside(stage, saved.get('name', 'missing'))
            if not (p.is_file() and p.stat().st_size == saved.get('size') and saved.get('size', 0) > 0):
                p, size = self.client.image(image, stage / ('%03d.img' % index), actual)
                manifest['files'][str(index)] = {'name': p.name, 'size': size}
                write_json(stage / '.wolf-episode.json', manifest)
            total += p.stat().st_size
            self.status(pages_done=index)
        if not self.check_artifact(stage, len(images)):
            raise WolfError('회차 파일 검증에 실패했습니다.')
        target = inside(folder, self.episode_name(ep))
        if self.config['use_compress']:
            target = target.with_name(target.name + '.zip')
            if target.exists():
                try:
                    with zipfile.ZipFile(target) as old_zip:
                        entry = old_zip.getinfo('.wolf-episode.json')
                        if entry.file_size > 1024 * 1024:
                            raise ValueError('manifest too large')
                        old = json.loads(old_zip.read(entry))
                    if old.get('work_id') != wid or old.get('ep_id') != eid:
                        raise ValueError('different owner')
                except (OSError, ValueError, KeyError, zipfile.BadZipFile):
                    raise WolfError('같은 이름의 기존 ZIP이 있어 보존했습니다.')
            self.status(message='ZIP 저장 중 (무압축)')
            self.zip_folder(stage, target)
            shutil.rmtree(stage)
        else:
            backup = inside(folder, '.backup-' + eid)
            if backup.exists():
                # Recover the crash window between rename and promotion.
                if not target.exists():
                    backup.rename(target)
                else:
                    raise WolfError('이전 백업 폴더가 있습니다: .backup-' + eid + ' 확인이 필요합니다.')
            if target.exists():
                old = read_json(target / '.wolf-episode.json', {})
                if old.get('work_id') != wid or old.get('ep_id') != eid:
                    raise WolfError('같은 이름의 기존 폴더가 있어 보존했습니다.')
                target.rename(backup)
            try:
                stage.rename(target)
            except Exception:
                if backup.exists() and not target.exists():
                    backup.rename(target)
                raise
            if backup.exists():
                shutil.rmtree(backup)
        self.history.save(wid, eid, status='completed', path=str(target), pages=len(images), bytes=total, error='')

    def reconcile(self, work, folder):
        roots = [folder, inside(self.config['download_path'], safe_name(work['title'])),
                 inside(self.config['download_path'], 'manhwa', safe_name(work['title']))]
        for ep in work['episodes']:
            self.client.check()
            names = [self.episode_name(ep), '{}_{}'.format(str(int(ep['no'])).zfill(4), safe_name(ep['title']))]
            candidates = [inside(root, name + suffix) for root in roots for name in names for suffix in ('', '.zip', '.cbz')]
            found = next((p for p in candidates if p.exists()), None)
            if not found:
                continue
            # Count against current viewer, not merely "folder exists".
            html, url = self.client.html(self.client.base + '/view?' + urlencode({'toon': work['id'], 'num': ep['id']}))
            expected = len(parse_images(html, url, self.config['viewer_selector']))
            if not self.check_artifact(found, expected):
                self.warn(ep['title'] + ': 이미지 수가 맞지 않아 이력 반영 안 함')
                continue
            if found.is_file():
                try:
                    self.verify_zip(found)
                except zipfile.BadZipFile:
                    self.warn(ep['title'] + ': ZIP 손상으로 반영 안 함')
                    continue
            self.history.save(work['id'], ep['id'], work_title=work['title'], title=ep['title'], status='completed', path=str(found), pages=expected, error='')
            self.state['completed'] += 1
            self.status(message='기존 파일 이력 반영 중')

    def compress_existing(self, work, folder):
        if not folder.exists():
            return
        for ep in work['episodes']:
            self.client.check()
            record = self.history.get(work['id'], ep['id'])
            if not self.complete(record):
                continue
            path = Path(record['path'])
            if not path.is_dir():
                continue
            # Only compress folders managed by this plugin, never arbitrary directories.
            meta = read_json(path / '.wolf-episode.json', {})
            if meta.get('work_id') != work['id'] or meta.get('ep_id') != ep['id']:
                continue
            target = path.with_name(path.name + '.zip')
            if target.exists():
                self.warn(ep['title'] + ': 기존 ZIP 보존, 압축 건너뜀')
                continue
            self.zip_folder(path, target)
            self.history.save(work['id'], ep['id'], path=str(target))
            shutil.rmtree(path)
            self.state['completed'] += 1
            self.status(message='기존 완료 회차 ZIP 변환 중')

    def run(self):
        mode = self.job['mode']
        if mode == 'notify_test':
            self.notify(self.args.get('kind', 'alert'), '늑대 다운로더 테스트 알림')
            self.status(status='done', message='알림 요청 처리 완료 (경고 확인)')
            return
        if mode == 'probe':
            if not self.client.base:
                raise WolfError('먼저 도메인을 입력하세요.')
            domain = verify(self.client, self.client.base)
            self.result(base_url=domain)
            self.status(status='done', message='도메인 정상', base_url=domain)
            return
        domain = self.connect(force=(mode == 'resolve'))
        if mode == 'resolve':
            self.result(base_url=domain)
            self.status(status='done', message='검증된 도메인을 다음 작업에 자동 적용합니다.')
            return
        if mode == 'browse':
            self.result(catalog=self.client.browse(self.args))
            self.status(status='done', message='목록 조회 완료')
            return
        ids = [self.args['work_id']] if self.args.get('work_id') else title_ids(self.config['titles'])
        if not ids:
            raise WolfError('자동 수집 작품 목록이 비어 있습니다.')
        for wid in ids:
            self.client.check()
            self.status(message='작품 분석 중: ' + wid)
            try:
                work = self.client.work(wid)
                folder = self.work_dir(work)
                if len(work['episodes']) > 10000:
                    raise WolfError('회차 수 한도(10000)를 초과했습니다.')
                if mode == 'analyze':
                    for ep in work['episodes']:
                        ep['completed'] = self.complete(self.history.get(wid, ep['id']))
                    self.result(work=work)
                    self.status(status='done', message='분석 완료 — 회차를 선택하세요.', work_title=work['title'])
                    return
                if mode == 'reconcile':
                    self.reconcile(work, folder)
                    continue
                if mode == 'compress':
                    self.compress_existing(work, folder)
                    continue
                self.metadata(work, folder)
                if mode == 'metadata':
                    self.state['completed'] += 1
                    continue
                selected = self.args.get('selected')
                if selected is not None:
                    available = {e['id'] for e in work['episodes']}
                    if set(selected) - available:
                        raise WolfError('선택 회차 중 현재 목록에서 사라진 회차가 있습니다. 다시 분석하세요.')
                    episodes = [e for e in work['episodes'] if e['id'] in set(selected)]
                else:
                    episodes = [e for e in work['episodes'] if not self.complete(self.history.get(wid, e['id']))]
                    maximum = self.config['max_per_run']
                    if maximum:
                        episodes = episodes[-maximum:]
                self.state['total'] += len(episodes)
                for ep in episodes:
                    self.client.check()
                    try:
                        # Manual selection intentionally re-downloads completed episodes.
                        self.download_episode(work, ep, folder)
                        self.state['completed'] += 1
                    except Cancelled:
                        self.history.save(wid, ep['id'], status='interrupted', error='사용자 중지')
                        raise
                    except Blocked as exc:
                        self.history.save(wid, ep['id'], status='failed', error=str(exc))
                        raise
                    except (WolfError, OSError) as exc:
                        message = str(exc) if isinstance(exc, WolfError) else '파일 저장 오류: ' + type(exc).__name__
                        self.history.save(wid, ep['id'], status='failed', error=message)
                        self.state['failed'] += 1
                        self.warn(ep['title'] + ': ' + message)
                    finally:
                        self.state['done'] += 1
                        self.status()
            except (Cancelled, Blocked):
                raise
            except WolfError as exc:
                self.state['failed'] += 1
                self.warn(wid + ': ' + str(exc))
        self.status(status='done', message='작업 완료: 성공 {}, 실패 {}'.format(self.state['completed'], self.state['failed']))
        if mode == 'download' and self.state['completed']:
            self.notify('download', '늑대 다운로더: {}회차 완료 / {}건 실패'.format(self.state['completed'], self.state['failed']))
