"""Serial, cancellable download engine running only in the child interpreter."""
import hashlib
import json
import os
import re
import shutil
import time
import zipfile
from collections import Counter
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from xml.etree import ElementTree as ET

from client import Client, parse_images, image_extension, clean_work_title
from common import WolfError, Blocked, Cancelled, atomic_bytes, inside, read_json, safe_name, title_ids, write_json, write_log
from domains import resolve, verify
from history import History


class Engine:
    def __init__(self, directory, job, client=None):
        self.directory, self.job = Path(directory), job
        self.config, self.args = job['config'], job.get('args', {})
        self.client = client or Client(self.config, lambda: (self.directory / 'cancel').exists())
        if job['mode'] in ('reconcile', 'metadata', 'compress'):
            # Worker-local override; saved/download settings stay unchanged.
            self.client.config = dict(self.client.config, request_delay=min(float(self.config.get('request_delay', 1.5)), .5))
        self.history = History(self.directory / 'history.db')
        self.history.interrupt_stale()
        self.state = {'id': job['id'], 'mode': job['mode'], 'status': 'running',
                      'done': 0, 'total': 0, 'completed': 0, 'skipped': 0, 'failed': 0,
                      'pages_done': 0, 'pages_total': 0, 'warnings': []}
        self.last_status = 0
        self.last_log_event = None
        self.log('INFO', '작업 실행: ' + job['mode'])

    def log(self, level, message):
        secrets = list(self.config.get('secrets', {}).values()) + [self.config.get('cookie', '')]
        if not write_log(self.directory, level, '[{}] {}'.format(self.job['id'][:8], message), secrets):
            self.state['log_error'] = '로그 파일 쓰기 실패: 데이터 폴더 권한 또는 디스크 공간을 확인하세요.'

    def status(self, **values):
        self.state.update(values)
        event = tuple(self.state.get(k, '') for k in ('status', 'work_title', 'episode', 'message'))
        if event != self.last_log_event:
            level = 'ERROR' if self.state['status'] == 'error' else ('WARNING' if self.state['status'] in ('canceled', 'interrupted') else 'INFO')
            self.log(level, ' / '.join(str(x) for x in event if x))
            self.last_log_event = event
        write_json(self.directory / 'status.json', self.state)

    def warn(self, message):
        self.log('WARNING', message)
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

    def notify_downloaded(self, work, episodes):
        """Report only successful episodes, splitting without losing numbers."""
        if not episodes:
            return
        title = ' '.join(str(work['title']).split())[:300]
        header = '늑대 다운로더 다운로드 완료\n웹툰: {}\n회차: '.format(title)
        message = header
        for ep in episodes:
            number = float(ep['no'])
            label = '{}화'.format(int(number) if number.is_integer() else number)
            addition = (', ' if message != header else '') + label
            if len(message) + len(addition) > 1700:
                self.notify('download', message)
                message = header + label
            else:
                message += addition
        self.notify('download', message)

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

    def work_dir(self, work, migrate=False):
        title = clean_work_title(work['title'])
        if not title:
            raise WolfError('작품명을 확인하지 못해 저장을 중지했습니다.')
        root = Path(self.config['download_path']).resolve()
        name = safe_name(title)
        tagged_name = '{} [{}]'.format(name, work['id'])
        tagged, plain = [], []
        for parent in (root, inside(root, 'manhwa')):
            for candidate, matches in ((tagged_name, tagged), (name, plain)):
                if matches is plain and not migrate:
                    continue
                path = inside(root, str(parent.relative_to(root)), candidate)
                if not path.exists():
                    continue
                if not path.is_dir():
                    raise WolfError('작품 폴더와 같은 이름의 파일이 있습니다: ' + str(path))
                marker = read_json(path / '.wolf-work.json', {})
                if marker and str(marker.get('id')) != str(work['id']):
                    raise WolfError('같은 작품명 폴더에 다른 작품 ID가 있습니다: ' + str(path))
                matches.append(path)
        if len(tagged) > 1 or (migrate and (len(plain) > 1 or (plain and tagged))):
            raise WolfError('기존 작품 폴더가 중복되어 이름 변경·병합하지 않았습니다: ' + ', '.join(str(p) for p in tagged + plain))
        if migrate and plain:
            if str(work['id']) not in title_ids(self.config['titles']):
                raise WolfError('자동 수집 목록 밖의 작품은 이름 변경하지 않습니다.')
            source = plain[0]
            target = source.with_name(tagged_name)
            self.client.check()
            if target.exists():
                raise WolfError('대상 폴더가 이미 있어 이름 변경하지 않았습니다: ' + str(target))
            source.rename(target)
            self.history.relocate_work(work['id'], source, target)
            self.status(message='작품 폴더 이름 변경: ' + target.name)
            return target
        if tagged:
            if migrate:
                # Recover a process interruption between rename and DB update.
                self.history.relocate_work(work['id'], tagged[0].with_name(name), tagged[0])
            return tagged[0]
        return inside(root, tagged_name)

    def episode_name(self, ep):
        no = ep['no']
        if float(no).is_integer():
            number = str(int(no)).zfill(self.config['episode_digits'])
        else:
            number = str(no).zfill(self.config['episode_digits'])
        return '{}_{}'.format(number, self.clean_episode_title(ep['title'], no))

    def clean_episode_title(self, title, number):
        title = str(title).strip()
        for _ in range(3):
            title = re.sub(r'\s*(?:\[\d+\]|#\s*\d+|\d{4}[-./]\d{2}[-./]\d{2})\s*$', '', title).strip()
        leading = re.match(r'^(\d+(?:\.\d+)?)[\s_]+(.+)$', title)
        if leading and float(leading.group(1)) == float(number):
            title = leading.group(2).strip()
        return safe_name(title or '회차')

    def archive_name(self, ep, pages):
        return '{} #{}.zip'.format(self.episode_name(ep), pages)

    def archive_signature(self, path):
        with zipfile.ZipFile(path) as archive:
            images = [i for i in archive.infolist() if Path(i.filename).suffix.lower() in ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.avif')]
            if not images or any(i.file_size <= 0 for i in images):
                raise WolfError('이미지가 없는 ZIP입니다.')
            # Hash decompressed image bytes to confirm duplicates, not just counts.
            hashes = []
            image_names = {i.filename for i in images}
            processed = 0
            for item in archive.infolist():
                digest = hashlib.sha256()
                with archive.open(item) as stream:
                    while True:
                        self.client.check()
                        chunk = stream.read(256 * 1024)
                        if not chunk:
                            break
                        if item.filename in image_names:
                            digest.update(chunk)
                        processed += len(chunk)
                        if processed >= 8 * 1024 * 1024:
                            self.client.wait(.001)
                            processed = 0
                if item.filename in image_names:
                    hashes.append(digest.hexdigest())
            return tuple(sorted(hashes))

    def archive_page_count(self, path):
        # Central directory only: no decompression, hashing or viewer request.
        with zipfile.ZipFile(path) as archive:
            images = [i for i in archive.infolist() if not i.is_dir()
                      and Path(i.filename).suffix.lower() in ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.avif')]
            if not images or any(i.file_size <= 0 or i.flag_bits & 1 for i in images):
                raise WolfError('이미지가 없거나 비어 있거나 암호화된 ZIP입니다.')
            return len(images)

    def file_stamp(self, path):
        stat = path.stat()
        return [str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino, stat.st_dev]

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
        if root not in path.resolve().parents or not path.parent.name.endswith(' [{}]'.format(record['work_id'])):
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
        stage = inside(folder, '.download-' + eid)
        target = inside(folder, self.episode_name(ep))
        if self.config['use_compress']:
            target = target.with_name(target.name + '.zip')
        self.status(work_title=work['title'], episode=ep['title'], pages_done=0, pages_total=0, message='회차 분석 중', staging_path=str(stage), target_path=str(target))
        self.history.save(wid, eid, work_title=work['title'], title=ep['title'], status='downloading', path=str(stage), pages=0, bytes=0, error='')
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
        target = inside(folder, self.archive_name(ep, len(images)) if self.config['use_compress'] else self.episode_name(ep))
        self.status(pages_total=len(images), target_path=str(target), message='이미지 다운로드 중')
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
            target = inside(folder, self.archive_name(ep, len(images)))
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
        self.log('INFO', '다운로드 완료: {} / {} / {}장 / {}'.format(work['title'], ep['title'], len(images), target))

    def reconcile(self, work, folder):
        if str(work['id']) not in title_ids(self.config['titles']) or not folder.name.endswith(' [{}]'.format(work['id'])):
            return
        if not folder.is_dir():
            return
        cache_path = self.directory / ('reconcile-cache-' + str(work['id']) + '.json')
        cache = read_json(cache_path, {})
        context = ['fast-v2', self.client.base, self.config.get('domain_family'), self.config.get('viewer_selector'), self.config['episode_digits']]
        if cache.get('context') != context:
            cache = {'context': context, 'episodes': {}}
        entries = cache.setdefault('episodes', {})
        groups = {}
        for path in folder.iterdir():
            if path.is_symlink() or (not path.is_dir() and path.suffix.lower() not in ('.zip', '.cbz')):
                continue
            match = re.match(r'^(\d+(?:\.\d+)?)_', path.name)
            if match:
                groups.setdefault(float(match.group(1)), []).append(path)
        numbers = Counter(float(ep['no']) for ep in work['episodes'])
        pending = []
        progress_time = 0
        try:
            for ep in work['episodes']:
                self.client.check()
                number = float(ep['no'])
                candidates = groups.get(number, [])
                if not candidates:
                    continue
                if numbers[number] != 1:
                    self.warn('사이트 목록 번호가 중복되어 건너뜀: ' + str(ep['no']))
                    continue
                cached = entries.get(ep['id'], {})
                if len(candidates) == 1 and candidates[0].is_file():
                    found = candidates[0]
                    try:
                        stamp = self.file_stamp(found)
                        pages = cached['pages'] if cached.get('stamp') == stamp else self.archive_page_count(found)
                        label = re.sub(r'^\d+(?:\.\d+)?_', '', found.stem)
                        target = inside(folder, self.archive_name(dict(ep, title=label), pages))
                        if target != found:
                            if target.exists():
                                self.warn(target.name + ': 대상 파일 충돌로 이름 변경 안 함')
                                continue
                            self.client.check()
                            found.rename(target)
                            found = target
                        pending.append((work['id'], ep['id'], work['title'], ep['title'], str(found), pages))
                        entries[ep['id']] = {'stamp': self.file_stamp(found), 'pages': pages}
                        self.state['completed'] += 1
                        if len(pending) >= 100:
                            self.history.save_many(pending)
                            pending.clear()
                        if time.monotonic() - progress_time >= .5:
                            self.status(message='ZIP 파일 목록으로 이력 반영 중', work_title=work['title'])
                            progress_time = time.monotonic()
                    except (zipfile.BadZipFile, WolfError, OSError) as exc:
                        if isinstance(exc, (Cancelled, Blocked)):
                            raise
                        self.warn(found.name + ': 파일 목록 확인 실패로 보존')
                    continue
                entries.pop(ep['id'], None)
                html, url = self.client.html(self.client.base + '/view?' + urlencode({'toon': work['id'], 'num': ep['id']}))
                expected = len(parse_images(html, url, self.config['viewer_selector']))
                valid = []
                for path in candidates:
                    if not self.check_artifact(path, expected):
                        self.warn(path.name + ': 이미지 수가 맞지 않아 보존')
                        continue
                    try:
                        signature = self.archive_signature(path) if path.is_file() else None
                        valid.append((path, signature))
                    except (zipfile.BadZipFile, WolfError, OSError) as exc:
                        if isinstance(exc, (Cancelled, Blocked)):
                            raise
                        self.warn(path.name + ': 검증 실패로 보존')
                if not valid:
                    continue
                # Prefer an already page-tagged, validated archive.
                valid.sort(key=lambda pair: (not pair[0].is_file(), not bool(re.search(r'#\s*\d+$', pair[0].stem)), pair[0].name))
                found, signature = valid[0]
                if found.is_file():
                    label = re.sub(r'^\d+(?:\.\d+)?_', '', found.stem)
                    normalized_ep = dict(ep, title=label)
                    target = inside(folder, self.archive_name(normalized_ep, len(signature)))
                    if target != found and target.exists():
                        # Existing canonical archive wins only if its content is identical.
                        existing = next((sig for path, sig in valid if path == target), None)
                        if existing != signature:
                            self.warn(target.name + ': 대상 파일 충돌로 이름 변경 안 함')
                            continue
                        found = target
                    elif target != found:
                        old = found
                        found.rename(target)
                        found = target
                        valid = [(target if path == old else path, sig) for path, sig in valid]
                    for duplicate, sig in valid:
                        if duplicate == found or not duplicate.is_file():
                            continue
                        if sig != signature:
                            self.warn(duplicate.name + ': 같은 번호지만 내용이 달라 보존')
                            continue
                        # Reversible deduplication: retain originals outside normal episode scan.
                        backup = inside(folder, '.duplicates')
                        backup.mkdir(exist_ok=True)
                        destination = inside(backup, duplicate.name)
                        index = 1
                        while destination.exists():
                            destination = inside(backup, duplicate.stem + '.backup-' + str(index) + duplicate.suffix)
                            index += 1
                        self.client.check()
                        duplicate.rename(destination)
                self.history.save(work['id'], ep['id'], work_title=work['title'], title=ep['title'], status='completed', path=str(found), pages=expected, error='')
                self.state['completed'] += 1
                self.status(message='기존 파일 이름 정리 및 이력 반영 중')
                remaining = {p for p in candidates + [found] if p.exists()}
                if remaining == {found} and found.is_file():
                    entries[ep['id']] = {'time': time.time(), 'stamp': self.file_stamp(found), 'pages': expected}
        finally:
            if pending:
                self.history.save_many(pending)
        cache['episodes'] = {ep['id']: entries[ep['id']] for ep in work['episodes'] if ep['id'] in entries}
        write_json(cache_path, cache)

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
            target = inside(folder, self.archive_name(ep, int(record['pages'])))
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
        maintenance = mode in ('reconcile', 'metadata', 'compress')
        ids = title_ids(self.config['titles']) if maintenance else ([self.args['work_id']] if self.args.get('work_id') else title_ids(self.config['titles']))
        if not ids:
            raise WolfError('자동 수집 작품 목록이 비어 있습니다.')
        for wid in ids:
            self.client.check()
            self.status(message='작품 분석 중: ' + wid)
            downloaded = []
            work = None
            try:
                work = self.client.work(wid)
                folder = self.work_dir(work, migrate=(mode == 'reconcile'))
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
                        downloaded.append(ep)
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
            finally:
                if mode == 'download' and downloaded:
                    self.notify_downloaded(work, downloaded)
        self.status(status='done', staging_path='', target_path='', message='작업 완료: 성공 {}, 실패 {}'.format(self.state['completed'], self.state['failed']))
        if mode == 'download' and self.state['completed']:
            self.notify('download', '늑대 다운로더: {}회차 완료 / {}건 실패'.format(self.state['completed'], self.state['failed']))
