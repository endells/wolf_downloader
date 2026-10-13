"""Public HTML parser and bounded HTTP client. No FF imports, no encrypted modules."""
import ipaddress
import re
import socket
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, quote
from urllib.request import Request, HTTPRedirectHandler, build_opener

from common import WolfError, Blocked, Cancelled, origin


class Node:
    def __init__(self, tag='', attrs=(), parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.children = []
        self.content = []

    def walk(self, tag=None):
        for child in self.children:
            if tag is None or child.tag == tag:
                yield child
            yield from child.walk(tag)

    def text(self):
        return ' '.join(' '.join(x.text() if isinstance(x, Node) else x for x in self.content).split())

    def matches(self, selector):
        if selector.startswith('#'):
            return self.attrs.get('id') == selector[1:]
        if selector.startswith('.'):
            return selector[1:] in self.attrs.get('class', '').split()
        return self.tag == selector


class Document(HTMLParser):
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.current = self.root
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.current)
        self.current.children.append(node)
        self.current.content.append(node)
        if tag not in self.VOID:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        p = self.current
        while p.parent:
            if p.tag == tag:
                self.current = p.parent
                return
            p = p.parent

    def handle_data(self, data):
        self.current.content.append(data)

    def walk(self, tag=None):
        return self.root.walk(tag)


def link_id(url, key):
    v = parse_qs(urlsplit(url).query).get(key, [''])[0]
    return v if v.isdigit() else ''


def clean_work_title(value):
    value = ' '.join(str(value or '').split())
    value = re.split(r'\s*[-|–]\s*늑대', value)[0].strip()
    # Site-wide metadata must never become a series directory name.
    if not value or '늑대닷컴' in value or re.fullmatch(r'(늑대\s*(웹툰|툰)?|웹툰|무료\s*웹툰|연재|완결|작품\s*\d+)', value):
        return ''
    return value


def parse_work(html, url, work_id):
    doc = Document(html)
    meta = {n.attrs.get('property', n.attrs.get('name')): n.attrs.get('content', '') for n in doc.walk('meta')}
    explicit = [n.text() for n in doc.walk() if
                n.attrs.get('itemprop') == 'name' or
                any(n.matches(s) for s in ('.toon-title', '.webtoon-title', '.comic-title', '.series-title', '#toon-title'))]
    candidates = explicit + [meta.get('og:title'), meta.get('twitter:title')]
    candidates += [n.text() for n in doc.walk('h1')]
    candidates += [n.text() for n in doc.walk('title')]
    title = next((clean_work_title(t) for t in candidates if
                  clean_work_title(t) and t != meta.get('og:site_name')), '')
    episodes, pages = {}, set()
    for a in doc.walk('a'):
        href = urljoin(url, a.attrs.get('href', ''))
        if urlsplit(href).netloc != urlsplit(url).netloc or link_id(href, 'toon') != str(work_id):
            continue
        num = link_id(href, 'num')
        if num and urlsplit(href).path.rstrip('/').endswith('/view'):
            label = a.attrs.get('title') or a.text() or ('회차 ' + num)
            no = re.search(r'(\d+(?:\.\d+)?)\s*[화회]', label)
            episodes.setdefault(num, {'id': num, 'title': label, 'no': float(no.group(1)) if no else int(num)})
        elif urlsplit(href).path.rstrip('/').endswith('/list') and any(k in parse_qs(urlsplit(href).query) for k in ('page', 'pg')):
            pages.add(href)
    if not title:
        # Accept a repeated series prefix only from this work's episode links.
        prefixes = [clean_work_title(re.split(r'\s*\d+(?:\.\d+)?\s*[화회]', e['title'], maxsplit=1)[0])
                    for e in episodes.values() if re.search(r'\d+(?:\.\d+)?\s*[화회]', e['title'])]
        if len(prefixes) >= 2 and prefixes[0] and len(set(prefixes)) == 1:
            title = prefixes[0]
    return {'id': str(work_id), 'title': title,
            'cover': urljoin(url, meta.get('og:image', '')) if meta.get('og:image') else '',
            'description': meta.get('og:description', meta.get('description', '')),
            'episodes': list(episodes.values())}, sorted(pages)


def parse_images(html, url, selectors):
    doc = Document(html)
    containers = [n for n in doc.walk() if any(n.matches(s.strip()) for s in selectors.split(','))]
    nodes = [img for c in containers for img in ([c] if c.tag == 'img' else c.walk('img'))]
    # Original public README specifies img[data-src]. Restrict fallback to
    # lazy image nodes outside headers/footers/nav/ad containers.
    if not nodes:
        for img in doc.walk('img'):
            if not (img.attrs.get('data-src') or img.attrs.get('data-original')):
                continue
            p, excluded = img, False
            while p:
                marker = (p.attrs.get('id', '') + ' ' + p.attrs.get('class', '')).lower()
                if p.tag in ('header', 'footer', 'nav', 'aside') or re.search(r'(^|[\s_-])(ad|ads|banner|logo|thumb)([\s_-]|$)', marker):
                    excluded = True
                p = p.parent
            if not excluded:
                nodes.append(img)
    images = []
    for n in nodes:
        src = n.attrs.get('data-src') or n.attrs.get('data-original') or n.attrs.get('data-lazy-src') or n.attrs.get('src', '')
        full = urljoin(url, src.strip())
        if src and urlsplit(full).scheme in ('http', 'https') and full not in images:
            images.append(full)
    if not images:
        raise WolfError('본문 이미지를 찾지 못했습니다. 사이트 구조 또는 뷰어 선택자를 확인하세요.')
    if len(images) > 2000:
        raise WolfError('회차 이미지 수가 안전 한도(2000)를 초과했습니다.')
    return images


def parse_catalog(html, url):
    doc = Document(html)
    results, last_page = {}, 1
    for a in doc.walk('a'):
        href = urljoin(url, a.attrs.get('href', ''))
        if urlsplit(href).netloc != urlsplit(url).netloc:
            continue
        for key in ('page', 'pg'):
            value = link_id(href, key)
            if value:
                last_page = min(1000, max(last_page, int(value)))
        wid = link_id(href, 'toon')
        if not wid or not urlsplit(href).path.rstrip('/').endswith('/list'):
            continue
        img = next(a.walk('img'), None)
        title = a.attrs.get('title') or (img.attrs.get('alt') if img else '') or a.text()
        if title:
            results.setdefault(wid, {'id': wid, 'title': title, 'url': href})
    return list(results.values()), last_page


def public_url(url):
    p = urlsplit(url)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password or p.port not in (None, 80, 443):
        raise WolfError('허용되지 않는 외부 URL입니다.')
    try:
        addresses = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    except OSError:
        raise WolfError('도메인 DNS 조회에 실패했습니다.')
    if not addresses or any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):
        raise WolfError('외부 요청에 내부/예약 주소를 사용할 수 없습니다.')
    return url


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_url(newurl)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        # Never forward credentials on any redirect; caller adds scoped cookies.
        if redirected is not None:
            redirected.remove_header('Cookie')
        return redirected


class Client:
    def __init__(self, config, cancelled=lambda: False):
        self.config, self.cancelled = config, cancelled
        self.base = config.get('base_url', '')
        self.last_request = 0
        self.opener = build_opener(SafeRedirect())

    def check(self):
        if self.cancelled():
            raise Cancelled('사용자가 작업을 중지했습니다.')

    def wait(self, seconds):
        end = time.monotonic() + max(0, seconds)
        while time.monotonic() < end:
            self.check()
            time.sleep(min(.1, max(0, end - time.monotonic())))
        self.check()

    def request(self, url, referer=None, data=None, headers=None):
        public_url(url)
        self.wait(self.config.get('request_delay', 1.5) - (time.monotonic() - self.last_request))
        h = {'User-Agent': 'Mozilla/5.0', 'Accept-Encoding': 'identity'}
        if referer:
            h['Referer'] = referer
        if self.base and urlsplit(url).hostname == urlsplit(self.base).hostname and self.config.get('cookie') and self.config.get('cookie_origin') == origin(url):
            h['Cookie'] = self.config['cookie']
        h.update(headers or {})
        self.last_request = time.monotonic()
        try:
            return self.opener.open(Request(url, data=data, headers=h), timeout=15)
        except HTTPError as e:
            e.close()
            if e.code in (401, 403, 429):
                raise Blocked('HTTP {}: 접속 제한/쿠키 만료입니다. 요청을 중단합니다.'.format(e.code))
            raise WolfError('HTTP {} 응답입니다.'.format(e.code))
        except (URLError, OSError):
            raise WolfError('사이트 연결 실패 또는 응답 시간 초과입니다.')

    def html(self, url):
        with self.request(url) as r:
            data = bytearray()
            while True:
                self.check()
                chunk = r.read(65536)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > 8 * 1024 * 1024:
                    raise WolfError('HTML 크기가 8MiB를 초과했습니다.')
            charset = r.headers.get_content_charset() or 'utf-8'
            try:
                text = data.decode(charset, errors='replace')
            except LookupError:
                text = data.decode('utf-8', errors='replace')
            if any(s in text.lower() for s in ('cf-chl-', '<title>just a moment', 'site unavailable', 'checking your browser')):
                raise Blocked('접속 제한 페이지입니다. 도메인/쿠키를 확인하세요.')
            return text, r.geturl()

    def image(self, url, path, referer):
        path = Path(path)
        temp = path.with_name(path.name + '.part')
        for attempt in range(2):
            try:
                with self.request(url, referer) as r, temp.open('wb') as f:
                    total, started, first = 0, time.monotonic(), b''
                    while True:
                        self.check()
                        block = r.read(65536)
                        if not block:
                            break
                        if not first:
                            first = block[:32]
                        total += len(block)
                        if total > 32 * 1024 * 1024:
                            raise WolfError('이미지 하나의 크기가 32MiB를 초과했습니다.')
                        f.write(block)
                        self.wait(total / (self.config.get('bandwidth_mib', 2) * 1024 * 1024) - (time.monotonic() - started))
                    expected = r.headers.get('Content-Length')
                    if expected and total != int(expected):
                        raise WolfError('이미지 응답이 중간에 끊겼습니다.')
                    ext = image_extension(first)
                    if not ext:
                        raise WolfError('응답이 지원하는 이미지(JPEG/PNG/GIF/WebP/AVIF)가 아닙니다.')
                final = path.with_suffix(ext)
                temp.replace(final)
                return final, total
            except (Blocked, Cancelled):
                raise
            except (WolfError, OSError, ValueError):
                if attempt == 1:
                    raise
                self.wait(2)
            finally:
                temp.unlink(missing_ok=True)

    def work(self, wid):
        first = self.base + '/list?' + urlencode({'toon': wid})
        pending, visited, episodes, work = [first], set(), {}, None
        while pending:
            self.check()
            url = pending.pop(0)
            if url in visited:
                continue
            if len(visited) >= 100:
                raise WolfError('회차 목록이 100페이지를 초과했습니다. 분할 지원이 필요합니다.')
            visited.add(url)
            html, actual = self.html(url)
            parsed, pages = parse_work(html, actual, wid)
            if work is None:
                work = parsed
            elif not work['title'] and parsed['title']:
                work['title'] = parsed['title']
            episodes.update({e['id']: e for e in parsed['episodes']})
            for p in pages:
                if p not in visited and p not in pending:
                    pending.append(p)
        if not episodes:
            raise WolfError('회차 목록이 없습니다. 작품 번호/사이트 구조/도메인을 확인하세요.')
        if not work['title']:
            raise WolfError('작품명을 확인하지 못했습니다. 사이트명으로 폴더를 만들지 않았습니다. 작품 상세 페이지 HTML 확인이 필요합니다.')
        work['episodes'] = sorted(episodes.values(), key=lambda x: (x['no'], int(x['id'])))
        return work

    def browse(self, args):
        page = max(1, min(1000, int(args.get('page', 1))))
        query = str(args.get('query', '')).strip()
        if query:
            pattern = self.config.get('search_path', '')
            if pattern:
                url = self.base + pattern.replace('{query}', quote(query))
            else:
                html, actual = self.html(self.base + '/ing')
                doc = Document(html)
                form = next((f for f in doc.walk('form') if any(i.attrs.get('type') == 'search' or i.attrs.get('name') in ('search', 'search_name', 'q', 'keyword', 'stx') for i in f.walk('input'))), None)
                if form is None or form.attrs.get('method', 'get').lower() != 'get':
                    raise WolfError('검색 폼을 찾지 못했습니다. 고급 설정의 검색 경로를 지정하세요.')
                inputs = list(form.walk('input'))
                key = next(i.attrs.get('name') for i in inputs if i.attrs.get('name') and (i.attrs.get('type') == 'search' or i.attrs.get('name') in ('search', 'search_name', 'q', 'keyword', 'stx')))
                values = {i.attrs['name']: i.attrs.get('value', '') for i in inputs if i.attrs.get('name') and i.attrs.get('type') == 'hidden'}
                values[key] = query
                action = urljoin(actual, form.attrs.get('action', ''))
                if origin(action) != origin(self.base):
                    raise WolfError('검색 폼이 다른 사이트를 가리킵니다.')
                url = action + ('&' if '?' in action else '?') + urlencode(values)
            url += ('&' if '?' in url else '?') + urlencode({'page': page})
        else:
            kind = 'end' if args.get('kind') == 'end' else 'ing'
            params = {k: args[k] for k in ('t1', 't2', 't3', 'o') if args.get(k)}
            params['page'] = page
            url = self.base + '/' + kind + '?' + urlencode(params)
        html, actual = self.html(url)
        rows, last = parse_catalog(html, actual)
        return {'rows': rows, 'page': page, 'last_page': max(page, last)}


def image_extension(data):
    if data.startswith(b'\xff\xd8\xff'): return '.jpg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'): return '.png'
    if data.startswith((b'GIF87a', b'GIF89a')): return '.gif'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP': return '.webp'
    if data[4:8] == b'ftyp' and data[8:12] in (b'avif', b'avis'): return '.avif'
    return ''
