"""Discover only configured domain families; verify actual catalog structure."""
import re
from urllib.parse import urljoin, urlsplit
from client import Document, parse_catalog
from common import WolfError, Cancelled, origin


def announcements(client, channel, family):
    queue, visited, candidates = [channel], set(), []
    pattern = re.compile(r'^' + re.escape(family) + r'\d+\.(?:com|net|org|me|to)$', re.I)
    while queue and len(visited) < 3:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        html, actual = client.html(url)
        # Telegram HTML preserves chronological message order; newest first.
        for node in reversed(list(Document(html).walk('a'))):
            href = urljoin(actual, node.attrs.get('href', ''))
            p = urlsplit(href)
            if p.scheme != 'https' or p.username or p.password:
                continue
            if p.hostname and pattern.fullmatch(p.hostname):
                candidate = origin(href)
                if candidate not in candidates:
                    candidates.append(candidate)
            if p.hostname == 't.me':
                path = p.path
                # Follow public channel migration links, not individual messages.
                if re.fullmatch(r'/[A-Za-z][A-Za-z0-9_]{3,}', path):
                    path = '/s' + path
                if re.fullmatch(r'/s/[A-Za-z0-9_]+', path):
                    moved = 'https://t.me' + path
                    if moved not in visited and moved not in queue:
                        queue.append(moved)
    return candidates[:12]


def verify(client, base):
    html, actual = client.html(base.rstrip('/') + '/ing')
    rows, _ = parse_catalog(html, actual)
    if not rows:
        raise WolfError('작품 목록 구조를 확인하지 못한 도메인입니다.')
    resolved = origin(actual)
    family = client.config['domain_family']
    if not re.fullmatch(re.escape(family) + r'\d+\.(com|net|org|me|to)', urlsplit(resolved).hostname or ''):
        raise WolfError('설정한 도메인 계열 밖으로 이동했습니다.')
    return resolved


def resolve(client, force=False, progress=lambda message: None):
    base = client.base
    if base and not force:
        try:
            progress('현재 도메인 확인 중')
            return verify(client, base)
        except Cancelled:
            raise
        except WolfError:
            if not client.config['auto_resolve_base_url']:
                raise
    candidates, checked = [], set()
    progress('공지 채널에서 도메인 확인 중')
    try:
        candidates.extend(announcements(client, client.config['announcer_url'], client.config['domain_family']))
    except Cancelled:
        raise
    except WolfError:
        pass
    if base:
        candidates.append(base)
        p = urlsplit(base)
        match = re.fullmatch('(' + re.escape(client.config['domain_family']) + r')(\d+)(\.[a-z]+)', p.hostname or '')
        if match:
            candidates.extend('https://{}{}{}'.format(match[1], int(match[2]) + i, match[3]) for i in range(1, 6))
    for candidate in candidates:
        client.check()
        if candidate in checked:
            continue
        checked.add(candidate)
        progress('도메인 검증: ' + candidate)
        try:
            return verify(client, candidate)
        except Cancelled:
            raise
        except WolfError:
            continue
    raise WolfError('유효한 도메인을 찾지 못했습니다. 공지 채널·도메인 계열·접속 제한을 확인하세요.')
