"""Standalone SQLite history. Each short transaction has its own connection."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone


class History:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS episodes (work_id TEXT NOT NULL, ep_id TEXT NOT NULL, work_title TEXT, title TEXT, status TEXT, path TEXT, pages INTEGER DEFAULT 0, bytes INTEGER DEFAULT 0, error TEXT, updated TEXT, PRIMARY KEY(work_id,ep_id))')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, wid, ep, **values):
        allowed = {'work_title', 'title', 'status', 'path', 'pages', 'bytes', 'error'}
        values = {k: v for k, v in values.items() if k in allowed}
        values['updated'] = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO episodes (work_id, ep_id) VALUES (?, ?)', (str(wid), str(ep)))
            db.execute('UPDATE episodes SET ' + ','.join(k + '=?' for k in values) + ' WHERE work_id=? AND ep_id=?', list(values.values()) + [str(wid), str(ep)])

    def get(self, wid, ep):
        with self.connect() as db:
            row = db.execute('SELECT * FROM episodes WHERE work_id=? AND ep_id=?', (str(wid), str(ep))).fetchone()
        return dict(row) if row else None

    def rows(self, page=1, query='', status=''):
        where, args = ['1=1'], []
        if query:
            where.append('(work_title LIKE ? OR title LIKE ?)')
            args.extend(['%' + query[:200] + '%'] * 2)
        if status:
            where.append('status=?')
            args.append(status)
        with self.connect() as db:
            rows = db.execute('SELECT * FROM episodes WHERE ' + ' AND '.join(where) + ' ORDER BY updated DESC LIMIT 51 OFFSET ?', args + [(max(1, page) - 1) * 50]).fetchall()
        return {'rows': [dict(r) for r in rows[:50]], 'more': len(rows) > 50, 'page': page}

    def interrupt_stale(self):
        with self.connect() as db:
            db.execute("UPDATE episodes SET status='interrupted', error='이전 작업이 종료되었습니다. 다시 선택하여 재시도하세요.' WHERE status='downloading'")
