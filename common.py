"""Shared, dependency-free configuration and file helpers."""
import json
import math
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


class WolfError(Exception):
    pass


class Cancelled(WolfError):
    pass


class Blocked(WolfError):
    pass


DEFAULTS = {
    'download_path': '', 'titles': '', 'base_url': '',
    'announcer_url': 'https://t.me/s/wftoon', 'auto_resolve_base_url': 'True',
    'domain_family': 'wfwf', 'max_per_run': '5', 'episode_digits': '4',
    'request_delay': '1.5', 'bandwidth_mib': '2', 'use_compress': 'False',
    'basic_auto_start': 'False', 'basic_interval': '360',
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
    c['base_url'] = origin(c['base_url']) if str(c['base_url']).strip() else ''
    a = urlsplit(str(c['announcer_url']).strip())
    if a.hostname != 't.me' or not re.fullmatch(r'/s/[A-Za-z0-9_]+', a.path) or a.query:
        raise WolfError('공지 채널은 https://t.me/s/채널명 형식으로 입력하세요.')
    c['announcer_url'] = 'https://t.me' + a.path
    if not re.fullmatch(r'[a-z][a-z0-9-]{1,25}', str(c['domain_family'])):
        raise WolfError('도메인 계열은 wfwf 또는 wftoon처럼 입력하세요.')
    for key, lo, hi in [('max_per_run', 0, 10000), ('episode_digits', 1, 10), ('basic_interval', 10, 10080)]:
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
