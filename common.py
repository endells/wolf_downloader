"""Shared, dependency-free configuration and file helpers."""
import json
import math
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

KST = timezone(timedelta(hours=9), 'KST')


def korean_time(value=None):
    """Format display times; UTC history storage remains sortable and unchanged."""
    if value is None:
        moment = datetime.now(KST)
    else:
        try:
            moment = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except (ValueError, TypeError):
            return str(value or '')
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(KST).strftime('%Y-%m-%d %H:%M:%S')


def write_log(directory, level, message, secret_values=()):
    """Append to FF's plugin log from either process without importing FF."""
    import fcntl
    import logging
    from logging.handlers import RotatingFileHandler
    message = str(message)
    for value in sorted((str(v) for v in secret_values if v), key=len, reverse=True):
        message = message.replace(value, '[REDACTED]')
    message = re.sub(r'https?://\S+', '[URL]', message)
    message = message.replace('\r', ' ').replace('\n', ' ')[:4000]
    handler = None
    try:
        path = Path(directory).parent / 'log' / 'wolf_downloader.log'
        path.parent.mkdir(parents=True, exist_ok=True)
        # Fresh handler per event avoids stale descriptors after another process rotates.
        with (path.parent / '.wolf_downloader.log.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            handler = RotatingFileHandler(str(path), maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8')
            formatter = logging.Formatter('[%(asctime)s|%(levelname)s|wolf_downloader] %(message)s', datefmt='%Y-%m-%d %H:%M:%S KST')
            formatter.converter = lambda stamp: datetime.fromtimestamp(stamp, KST).timetuple()
            handler.setFormatter(formatter)
            record = logging.LogRecord('wolf_downloader', getattr(logging, level, logging.INFO), '', 0, message, (), None)
            # Use explicit rollover/write so failures return False instead of stderr tracebacks.
            if handler.shouldRollover(record):
                handler.doRollover()
            handler.stream.write(handler.format(record) + '\n')
            handler.flush()
            handler.close()
        return True
    except OSError:
        return False
    finally:
        if handler is not None:
            try:
                handler.close()
            except OSError:
                pass


class WolfError(Exception):
    pass


class Cancelled(WolfError):
    pass


class Blocked(WolfError):
    pass


DEFAULTS = {
    'separate_path': '', 'separate_titles': '',
    'download_path': '', 'titles': '', 'base_url': '',
    'announcer_url': 'https://t.me/s/wftoon', 'auto_resolve_base_url': 'True',
    'domain_family': 'wfwf', 'max_per_run': '5', 'episode_digits': '4',
    'request_delay': '1.5', 'bandwidth_mib': '2', 'use_compress': 'False',
    'basic_auto_start': 'False', 'basic_interval': '360', 'backup_retention_days': '7',
    'viewer_selector': '#toon_img, #toonimg, #viewer, .viewer, .toon_img, .wt_viewer',
    'search_path': '',
}


def parse_id(value):
    value = str(value).strip()
    if value.isdigit() and len(value) <= 18:
        return str(int(value))
    parts = urlsplit(value)
    ids = parse_qs(parts.query).get('toon', [])
    if parts.scheme in ('http', 'https') and ids and ids[0].isdigit() and len(ids[0]) <= 18:
        return str(int(ids[0]))
    raise WolfError('작품 번호 또는 toon=번호가 포함된 작품 URL을 입력하세요.')


def title_ids(value):
    return list(dict.fromkeys(parse_id(x) for x in re.split(r'[\n|]+', str(value)) if x.strip()))


def origin(value):
    p = urlsplit(str(value).strip())
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise WolfError('http(s)://로 시작하는 도메인을 입력하세요.')
    if p.port not in (None, 80, 443):
        raise WolfError('사이트는 기본 HTTP/HTTPS 포트만 지원합니다.')
    return '{}://{}'.format(p.scheme, p.netloc).rstrip('/')


def validate(raw):
    c = dict(DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS})
    c['download_path'] = str(c['download_path']).strip()
    if not Path(c['download_path']).is_absolute():
        raise WolfError('다운로드 경로는 FF 컨테이너 내부의 절대 경로입니다.')
    c['titles'] = '\n'.join(title_ids(c['titles']))
    c['separate_titles'] = '\n'.join(title_ids(c['separate_titles']))
    c['separate_path'] = str(c['separate_path']).strip()
    if c['separate_titles'] and not c['separate_path']:
        raise WolfError('별도 자동 수집 목록의 저장 경로를 입력하세요.')
    if c['separate_path']:
        if not Path(c['separate_path']).is_absolute():
            raise WolfError('별도 저장 경로는 절대 경로로 입력하세요.')
        base, separate = Path(c['download_path']).resolve(), Path(c['separate_path']).resolve()
        if separate == base or base in separate.parents or separate in base.parents:
            raise WolfError('기본 경로와 별도 경로는 서로 겹치지 않는 폴더로 지정하세요.')
    if set(title_ids(c['titles'])) & set(title_ids(c['separate_titles'])):
        raise WolfError('같은 작품을 기본·별도 목록에 중복 등록할 수 없습니다. 한 목록에만 넣으세요.')
    c['base_url'] = origin(c['base_url']) if str(c['base_url']).strip() else ''
    a = urlsplit(str(c['announcer_url']).strip())
    if a.hostname != 't.me' or not re.fullmatch(r'/s/[A-Za-z0-9_]+', a.path) or a.query:
        raise WolfError('공지 채널은 https://t.me/s/채널명 형식으로 입력하세요.')
    c['announcer_url'] = 'https://t.me' + a.path
    if not re.fullmatch(r'[a-z][a-z0-9-]{1,25}', str(c['domain_family'])):
        raise WolfError('도메인 계열은 wfwf 또는 wftoon처럼 입력하세요.')
    for key, lo, hi in [('max_per_run', 0, 10000), ('episode_digits', 1, 10), ('basic_interval', 10, 10080), ('backup_retention_days', 0, 3650)]:
        try:
            c[key] = int(c[key])
        except (ValueError, TypeError):
            raise WolfError(key + ': 정수를 입력하세요.')
        if not lo <= c[key] <= hi:
            raise WolfError('{}: {}~{} 범위입니다.'.format(key, lo, hi))
    for key, lo, hi in [('request_delay', .5, 60), ('bandwidth_mib', .1, 50)]:
        c[key] = float(c[key])
        if not math.isfinite(c[key]) or not lo <= c[key] <= hi:
            raise WolfError('{}: {}~{} 범위입니다.'.format(key, lo, hi))
    for key in ('auto_resolve_base_url', 'use_compress', 'basic_auto_start'):
        c[key] = c[key] in (True, 'True', 'true', '1')
    c['viewer_selector'] = str(c['viewer_selector']).strip()
    if not c['viewer_selector'] or any(not re.fullmatch(r'[.#]?[\w-]+', s.strip()) for s in c['viewer_selector'].split(',')):
        raise WolfError('뷰어 선택자는 #id, .class 또는 태그를 쉼표로 구분하세요.')
    c['search_path'] = str(c['search_path']).strip()
    if c['search_path'] and (not c['search_path'].startswith('/') or c['search_path'].startswith('//') or '{query}' not in c['search_path']):
        raise WolfError('검색 경로는 /로 시작하고 {query}를 포함해야 합니다.')
    return c


def atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name + '.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_json(path, data):
    atomic_bytes(path, json.dumps(data, ensure_ascii=False).encode('utf-8'))


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (FileNotFoundError, ValueError, OSError):
        return default


def safe_name(value):
    mapping = str.maketrans('\\/:*?"<>|', '＼／：＊？＂＜＞｜')
    value = re.sub(r'[\x00-\x1f\x7f]', '', str(value)).translate(mapping).strip().rstrip('. ')
    if value in ('', '.', '..'):
        value = 'untitled'
    if re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', value):
        value = '_' + value
    # Leave room for suffixes on filesystems with 255-byte names.
    while len(value.encode('utf-8')) > 180:
        value = value[:-1]
    return value


def inside(root, *parts):
    root = Path(root).resolve()
    target = root.joinpath(*parts).resolve()
    if target != root and root not in target.parents:
        raise WolfError('저장 경로가 다운로드 폴더를 벗어났습니다.')
    return target

