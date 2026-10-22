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

from client import Client, parse_images, image_extension, clean_work_title, strip_episode_badges
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
        self.state['warning_count'] = self.state.get('warning_count', 0) + 1
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

    def connect(self, force=False, probe=False):
        cached = read_json(self.directory / 'domain.json', {})
        if cached.get('configured_base') == self.config['base_url'] and cached.get('family') == self.config['domain_family']:
            self.client.base = cached.get('base_url') or self.client.base
        if probe and not self.client.base:
            raise WolfError('먼저 도메인을 입력하거나 자동 갱신하세요.')
        previous = self.client.base
        new = verify(self.client, self.client.base) if probe else resolve(self.client, force=force, progress=lambda message: self.status(message=message))
        self.client.base = new
        # Cookies are only valid for the exact domain where the user saved them.
        if self.config.get('cookie_origin') != new:
            self.config['cookie'] = ''
        write_json(self.directory / 'domain.json', {'base_url': new, 'configured_base': self.config['base_url'], 'family': self.config['domain_family']})
        self.status(base_url=new)
        if urlsplit(previous or '').hostname != urlsplit(new).hostname:
            self.notify('alert', '늑대 다운로더 도메인 갱신 완료\n이전 주소: {}\n새 주소: {}'.format(previous or '(없음)', new))
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
        title = strip_episode_badges(title)
        for _ in range(3):
            title = re.sub(r'\s*(?:\[\d+\]|#\s*\d+|\d{4}[-./]\d{2}[-./]\d{2})\s*$', '', title).strip()
        leading = re.match(r'^(\d+(?:\.\d+)?)[\s_]+(.+)$', title)
        if leading and float(leading.group(1)) == float(number):
            title = strip_episode_badges(leading.group(2))
        return safe_name(title or '회차')

    def local_episode_title(self, ep, folder):
        """Keep local edits; infer only from consecutive, compatible neighbors."""
        source = self.clean_episode_title(ep['title'], ep['no'])
        labels = {}
        if not folder.is_dir():
            return source
        for path in folder.iterdir():
            if path.is_symlink() or (not path.is_dir() and path.suffix.lower() not in ('.zip', '.cbz')):
                continue
            match = re.match(r'^(\d+(?:\.\d+)?)_(.+)$', path.stem if path.is_file() else path.name)
            if not match or re.search(r'\[보존-\d+\]', match[2]):
                continue
            number = float(match[1])
            labels.setdefault(number, set()).add(self.clean_episode_title(match[2], number))
        number = float(ep['no'])
        if len(labels.get(number, set())) == 1:
            return next(iter(labels[number]))
        def parts(label):
            return re.fullmatch(r'(.*?)(\d+(?:\.\d+)?)\s*([화회])(.*)', label)
        target = parts(source)
        suggestions = []
        for delta in (-1, 1):
            neighbors = labels.get(number + delta, set())
            if len(neighbors) != 1:
                continue
            item = parts(next(iter(neighbors)))
            if not item:
                continue
            chapter = float(item[2]) - delta
            if chapter < 0:
                continue
            # Do not carry an old season's naming across a numbering reset.
            if target and float(target[2]) != chapter:
                continue
            if target:
                season = lambda text: re.search(r'(?:시즌\s*(\d+)|(\d+)\s*부)', text)
                old_season, new_season = season(item[1]), season(target[1])
                if old_season and new_season and (old_season[1] or old_season[2]) != (new_season[1] or new_season[2]):
                    continue
            value = str(int(chapter)) if chapter.is_integer() else str(chapter)
            suggestions.append(item[1] + value + item[3] + item[4])
        if suggestions and len(set(suggestions)) == 1 and (target or len(suggestions) == 2):
            return suggestions[0]
        return source

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

    def archive_inventory(self, path):
        # Central directory only: no decompression, hashing or viewer request.
        with zipfile.ZipFile(path) as archive:
            images = [i for i in archive.infolist() if not i.is_dir()
                      and Path(i.filename).suffix.lower() in ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.avif')]
            if not images or any(i.file_size <= 0 or i.flag_bits & 1 for i in images):
                raise WolfError('이미지가 없거나 비어 있거나 암호화된 ZIP입니다.')
            return tuple(sorted((i.file_size, i.CRC) for i in images))

    def archive_page_count(self, path):
        return len(self.archive_inventory(path))

    def reconcile_archives(self, ep, folder, paths):
        """Normalize every ZIP from its own page count; hash only likely duplicates."""
        records = []
        for path in paths:
            self.client.check()
            try:
                inventory = self.archive_inventory(path)
                records.append((path, inventory))
            except (zipfile.BadZipFile, WolfError, OSError):
                self.warn(path.name + ': ZIP 파일 목록 확인 실패로 보존')
        records.sort(key=lambda item: (not bool(re.search(r'#\s*\d+$', item[0].stem)), item[0].name))
        counts = Counter(inventory for _, inventory in records)
        seen = {}
        survivors = []
        for path, inventory in records:
            self.client.check()
            if counts[inventory] > 1:
                try:
                    signature = self.archive_signature(path)
                except (zipfile.BadZipFile, WolfError, OSError, RuntimeError, NotImplementedError) as exc:
                    if isinstance(exc, (Cancelled, Blocked)):
                        raise
                    self.warn(path.name + ': 내용 비교 실패, 중복 이동 없이 이름만 정리')
                else:
                    key = (inventory, signature)
                    if key in seen:
                        backup = inside(folder, '.duplicates')
                        backup.mkdir(exist_ok=True)
                        destination = inside(backup, path.name)
                        index = 1
                        while destination.exists():
                            destination = inside(backup, path.stem + '.backup-' + str(index) + path.suffix)
                            index += 1
                        self.client.check()
                        path.rename(destination)
                        ledger_path = self.directory / 'duplicate-retention.json'
                        ledger = read_json(ledger_path, {})
                        ledger[str(destination)] = {'stamp': self.file_stamp(destination), 'since': time.time()}
                        write_json(ledger_path, ledger)
                        continue
                    seen[key] = path
            survivors.append((path, len(inventory)))
        normalized = []
        for path, pages in survivors:
            self.client.check()
            label = re.sub(r'^\d+(?:\.\d+)?_', '', path.stem)
            episode = dict(ep, title=label)
            target = inside(folder, self.archive_name(episode, pages))
            if target != path and target.exists():
                # Different contents can normalize to the same name. Keep both,
                # retaining the leading episode number and trailing page count.
                base = self.episode_name(episode)
                index = 2
                while target.exists() and target != path:
                    target = inside(folder, '{} [보존-{}] #{}.zip'.format(base, index, pages))
                    index += 1
                self.warn(path.name + ': 이름 충돌로 별도 보존: ' + target.name)
            if target != path:
                path.rename(target)
            normalized.append((target, pages))
        # Stable primary: preserve the current history selection when possible.
        protected = self.history.completed_paths(re.search(r' \[(\d+)\]$', folder.name).group(1))
        normalized.sort(key=lambda pair: (str(pair[0]) not in protected, bool(re.search(r'\[보존-\d+\]', pair[0].name))))
        ledger_path = self.directory / 'duplicate-retention.json'
        ledger = read_json(ledger_path, {})
        for path, _ in normalized[1:]:
            stamp = self.file_stamp(path)
            if ledger.get(str(path), {}).get('stamp') != stamp:
                ledger[str(path)] = {'stamp': stamp, 'since': time.time()}
        if len(normalized) > 1:
            write_json(ledger_path, ledger)
        return normalized

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
        ep = dict(ep, title=self.local_episode_title(ep, folder))
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

    def repair_existing(self, work, ep, folder, candidates):
        broken = []
        for path in candidates:
            if not path.is_file():
                continue
            try:
                self.archive_page_count(path)
            except (Cancelled, Blocked):
                raise
            except (zipfile.BadZipFile, WolfError):
                broken.append((path, self.file_stamp(path)))
            except OSError:
                # Permission/device failures do not prove corrupt contents.
                self.warn(path.name + ': 접근 실패로 자동 복구 제외')
                return []
        if not broken:
            return candidates
        def chapter(text):
            match = re.search(r'(\d+(?:\.\d+)?)\s*[화회]', text)
            return float(match.group(1)) if match else None
        expected = chapter(ep['title'])
        for path, _ in broken:
            actual = chapter(re.sub(r'^\d+(?:\.\d+)?_', '', path.stem))
            if actual is not None and expected is not None and actual != expected:
                self.warn(path.name + ': 앞번호와 제목 화수가 사이트 회차와 일치하지 않아 자동 복구 제외')
                return []
            if actual is not None and expected is None:
                # Require both adjacent site entries and local files to agree.
                evidence = []
                for delta in (-1, 1):
                    adjacent = [e for e in work['episodes'] if float(e['no']) == float(ep['no']) + delta]
                    if len(adjacent) != 1 or chapter(adjacent[0]['title']) != actual + delta:
                        break
                    matching = []
                    for neighbor in folder.iterdir():
                        prefix = re.match(r'^(\d+(?:\.\d+)?)_', neighbor.name)
                        if prefix and float(prefix.group(1)) == float(ep['no']) + delta and neighbor.is_file() and not neighbor.is_symlink():
                            matching.append(chapter(re.sub(r'^\d+(?:\.\d+)?_', '', neighbor.stem)))
                    if not matching or any(value != actual + delta for value in matching):
                        break
                    evidence.append(delta)
                if len(evidence) != 2:
                    self.warn(path.name + ': 사이트 제목 정보 부족, 앞뒤 회차 교차 확인도 불충분하여 복구 보류')
                    return []
                ep = dict(ep, title=re.sub(r'^\d+(?:\.\d+)?_', '', path.stem))
        # Cross-check adjacent filenames against their corresponding site entries.
        neighbors = {float(e['no']): e for e in work['episodes'] if abs(float(e['no']) - float(ep['no'])) == 1}
        for neighbor in folder.iterdir():
            prefix = re.match(r'^(\d+(?:\.\d+)?)_', neighbor.name)
            if neighbor.is_symlink() or not neighbor.is_file() or not prefix:
                continue
            nearby = neighbors.get(float(prefix.group(1)))
            if nearby:
                local_chapter = chapter(re.sub(r'^\d+(?:\.\d+)?_', '', neighbor.stem))
                site_chapter = chapter(nearby['title'])
                if local_chapter is not None and site_chapter is not None and local_chapter != site_chapter:
                    self.warn(ep['title'] + ': 앞뒤 파일과 사이트 회차 배열이 달라 자동 복구 제외')
                    return []
        repair_folder = inside(folder, '.repair-' + str(ep['id']))
        repair_folder.mkdir(exist_ok=True)
        compress = self.config['use_compress']
        try:
            self.config['use_compress'] = True
            self.status(message='손상된 기존 ZIP 재다운로드 중', work_title=work['title'], episode=ep['title'])
            named_ep = dict(ep, title=self.local_episode_title(ep, folder))
            self.download_episode(work, named_ep, repair_folder)
            record = self.history.get(work['id'], ep['id'])
            downloaded = Path(record['path'])
            target = inside(folder, downloaded.name)
            broken_paths = {path for path, _ in broken}
            if any(not path.exists() or self.file_stamp(path) != stamp for path, stamp in broken):
                raise WolfError('복구 도중 기존 파일이 변경되어 자동 교체하지 않았습니다.')
            self.client.check()
            preserved = None
            try:
                if target.exists() and target not in broken_paths:
                    if target.is_symlink() or not target.is_file():
                        raise WolfError('대상 경로가 일반 ZIP 파일이 아니어서 교체하지 않았습니다.')
                    backup = inside(folder, '.duplicates')
                    backup.mkdir(exist_ok=True)
                    preserved = inside(backup, target.name)
                    index = 1
                    while preserved.exists():
                        preserved = inside(backup, target.stem + '.backup-' + str(index) + target.suffix)
                        index += 1
                    target.rename(preserved)
                    ledger_path = self.directory / 'duplicate-retention.json'
                    ledger = read_json(ledger_path, {})
                    ledger[str(preserved)] = {'stamp': self.file_stamp(preserved), 'since': time.time()}
                    write_json(ledger_path, ledger)
                self.client.check()
                os.replace(downloaded, target)
            except BaseException:
                if preserved is not None and preserved.exists() and not target.exists():
                    preserved.rename(target)
                raise
            for path, _ in broken:
                if path != target:
                    path.unlink()
            self.history.save(work['id'], ep['id'], status='completed', path=str(target), pages=record['pages'], error='')
            self.state['repaired'] = self.state.get('repaired', 0) + 1
            self.log('INFO', '기존 ZIP 복구 완료: {} / {}'.format(work['title'], ep['title']))
            self.notify_downloaded(work, [ep])
            if not any(repair_folder.iterdir()):
                repair_folder.rmdir()
            return [p for p in candidates if p not in broken_paths and p != target] + [target]
        except (Cancelled, Blocked):
            self.history.save(work['id'], ep['id'], status='interrupted', path=str(broken[0][0]), error='기존 ZIP 복구 중단')
            raise
        except (WolfError, OSError) as exc:
            self.state['failed'] += 1
            detail = str(exc) if isinstance(exc, WolfError) else '{} (errno={})'.format(type(exc).__name__, getattr(exc, 'errno', '?'))
            detail = re.sub(r'https?://\S+', '[URL]', detail)[:500]
            for secret in list(self.config.get('secrets', {}).values()) + [self.config.get('cookie', '')]:
                if secret:
                    detail = detail.replace(str(secret), '[REDACTED]')
            self.history.save(work['id'], ep['id'], status='failed', path=str(broken[0][0]), error='기존 ZIP 복구 실패: ' + detail)
            self.warn(ep['title'] + ': 재다운로드 실패, 기존 파일 유지 — ' + detail)
            self.notify('alert', '늑대 다운로더 기존 파일 복구 실패\n작품: {}\n회차: {} (앞번호 {})\n상세 오류: {}\n기존 파일을 유지했습니다.'.format(work['title'], ep['title'], ep['no'], detail))
            return []
        finally:
            self.config['use_compress'] = compress

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
                if not (len(candidates) == 1 and candidates[0].is_file() and cached.get('stamp') == self.file_stamp(candidates[0])):
                    candidates = self.repair_existing(work, ep, folder, candidates)
                    if not candidates:
                        continue
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
                archives = [path for path in candidates if path.is_file()]
                normalized = self.reconcile_archives(ep, folder, archives)
                if normalized:
                    found, pages = normalized[0]
                    pending.append((work['id'], ep['id'], work['title'], ep['title'], str(found), pages))
                    self.state['completed'] += 1
                    if len(pending) >= 100:
                        self.history.save_many(pending)
                        pending.clear()
                    self.status(message='기존 ZIP 페이지 수 및 이름 정리 중', work_title=work['title'])
                    if len(normalized) == 1 and all(path.is_file() for path in candidates if path.exists()):
                        entries[ep['id']] = {'stamp': self.file_stamp(found), 'pages': pages}
                    continue
                # Legacy image directories still need a viewer count to avoid
                # marking an interrupted, partially downloaded folder complete.
                directories = [path for path in candidates if path.is_dir()]
                if not directories:
                    continue
                html, url = self.client.html(self.client.base + '/view?' + urlencode({'toon': work['id'], 'num': ep['id']}))
                expected = len(parse_images(html, url, self.config['viewer_selector']))
                for path in directories:
                    if not self.check_artifact(path, expected):
                        self.warn(path.name + ': 회차 폴더 이미지 수가 사이트와 달라 이력 반영 제외')
                        continue
                    pending.append((work['id'], ep['id'], work['title'], ep['title'], str(path), expected))
                    self.state['completed'] += 1
                    if len(pending) >= 100:
                        self.history.save_many(pending)
                        pending.clear()
                    self.status(message='기존 회차 폴더 이력 반영 중')
                    break
        finally:
            if pending:
                self.history.save_many(pending)
        cache['episodes'] = {ep['id']: entries[ep['id']] for ep in work['episodes'] if ep['id'] in entries}
        write_json(cache_path, cache)

    def cleanup_duplicates(self):
        """Expire retained alternatives, regardless of content; keep primary files."""
        days = int(self.config.get('backup_retention_days', 7))
        if days == 0:
            return
        root = Path(self.config['download_path']).resolve()
        registered = set(title_ids(self.config['titles']))
        ledger_path = self.directory / 'duplicate-retention.json'
        ledger = read_json(ledger_path, {})
        now = time.time()
        retention = days * 86400
        try:
            for parent in (root, root / 'manhwa'):
                if parent.is_symlink() or not parent.is_dir():
                    continue
                for folder in parent.iterdir():
                    self.client.check()
                    match = re.search(r' \[(\d+)\]$', folder.name)
                    if folder.is_symlink() or not folder.is_dir() or not match or match.group(1) not in registered:
                        continue
                    marker = read_json(folder / '.wolf-work.json', {})
                    if marker and str(marker.get('id')) != match.group(1):
                        continue
                    protected = self.history.completed_paths(match.group(1))
                    backup = folder / '.duplicates'
                    files = list(folder.iterdir())
                    backups = list(backup.iterdir()) if backup.is_dir() and not backup.is_symlink() else []
                    candidates = backups + [p for p in files if str(p) in ledger or re.search(r'\[보존-\d+\]', p.name)]
                    candidate_set = set(candidates)
                    for path in candidates:
                        self.client.check()
                        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in ('.zip', '.cbz'):
                            continue
                        number = re.match(r'^(\d+(?:\.\d+)?)_', path.name)
                        if not number or str(path) in protected:
                            continue
                        key = str(path)
                        stamp = self.file_stamp(path)
                        entry = ledger.get(key, {})
                        if entry.get('stamp') != stamp:
                            ledger[key] = {'stamp': stamp, 'since': now}
                            continue
                        if now - entry['since'] < retention:
                            continue
                        # Primary must still exist. Never delete the final copy.
                        originals = []
                        for kept in files:
                            prefix = re.match(r'^(\d+(?:\.\d+)?)_', kept.name)
                            if (kept != path and not kept.is_symlink() and kept.is_file()
                                    and kept.suffix.lower() in ('.zip', '.cbz') and prefix
                                    and float(prefix.group(1)) == float(number.group(1))
                                    and (str(kept) in protected or kept not in candidate_set)):
                                originals.append(kept)
                        try:
                            usable = any(self.archive_page_count(kept) > 0 for kept in originals)
                            if usable and self.file_stamp(path) == stamp:
                                self.client.check()
                                path.unlink()
                                ledger.pop(key, None)
                                self.log('INFO', '보관 기간 만료 파일 삭제: ' + str(path))
                        except (zipfile.BadZipFile, WolfError, OSError) as exc:
                            if isinstance(exc, (Cancelled, Blocked)):
                                raise
                            self.warn(path.name + ': 원본 확인 또는 보관 파일 삭제 실패, 파일 유지')
                    if backup.is_dir() and not backup.is_symlink() and not any(backup.iterdir()):
                        backup.rmdir()
                        self.log('INFO', '빈 보관 폴더 삭제: ' + str(backup))
        finally:
            write_json(ledger_path, ledger)

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
        original = self.config
        try:
            return self._run()
        finally:
            self.config = original

    def _run(self):
        started = time.monotonic()
        mode = self.job['mode']
        if mode == 'notify_test':
            self.notify(self.args.get('kind', 'alert'), '늑대 다운로더 테스트 알림')
            self.status(status='done', message='알림 요청 처리 완료 (경고 확인)')
            return
        if mode == 'probe':
            domain = self.connect(probe=True)
            self.result(base_url=domain)
            self.status(status='done', message='도메인 정상', base_url=domain)
            return
        if mode in ('download', 'reconcile'):
            self.cleanup_duplicates()
            original = self.config
            if original.get('separate_path'):
                try:
                    self.config = dict(original, download_path=original['separate_path'], titles=original.get('separate_titles', ''))
                    self.cleanup_duplicates()
                finally:
                    self.config = original
        domain = self.connect(force=(mode == 'resolve'))
        if mode == 'resolve':
            self.result(base_url=domain)
            self.status(status='done', message='검증된 도메인을 다음 작업에 자동 적용합니다.')
            return
        if mode == 'browse':
            catalog = self.client.browse(self.args)
            for row in catalog['rows']:
                self.history.remember_work(row['id'], row['title'])
            self.result(catalog=catalog)
            self.status(status='done', message='목록 조회 완료')
            return
        maintenance = mode in ('reconcile', 'metadata', 'compress')
        original = self.config
        separate_ids = title_ids(original.get('separate_titles', ''))
        all_ids = list(dict.fromkeys(title_ids(original['titles']) + separate_ids))
        ids = all_ids if maintenance else ([self.args['work_id']] if self.args.get('work_id') else all_ids)
        if not ids:
            raise WolfError('자동 수집 작품 목록이 비어 있습니다.')
        for wid in ids:
            separate = (self.args.get('separate', wid in separate_ids) if self.args.get('work_id') and not maintenance else wid in separate_ids)
            self.config = dict(original, download_path=original.get('separate_path', '') if separate else original['download_path'],
                               titles=original.get('separate_titles', '') if separate else original['titles'])
            if not self.config['download_path']:
                raise WolfError('설정에서 별도 저장 경로를 먼저 저장하세요.')
            self.client.check()
            self.status(message='작품 분석 중: ' + wid)
            downloaded = []
            work = None
            try:
                work = self.client.work(wid)
                self.history.remember_work(wid, work['title'], recent=(mode in ('analyze', 'download')))
                folder = self.work_dir(work, migrate=(mode == 'reconcile'))
                if len(work['episodes']) > 10000:
                    raise WolfError('회차 수 한도(10000)를 초과했습니다.')
                if mode == 'analyze':
                    work['separate'] = bool(separate)
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
        if mode == 'reconcile':
            self.notify('download', '늑대 다운로더 기존 파일 이력 반영 완료\n대상 작품: {}개\n이력 반영: {}회차\n실패: {}건 / 경고: {}건\n손상 파일 복구: {}회차\n소요 시간: {:.1f}초'.format(len(ids), self.state['completed'], self.state['failed'], self.state.get('warning_count', 0), self.state.get('repaired', 0), time.monotonic() - started))


