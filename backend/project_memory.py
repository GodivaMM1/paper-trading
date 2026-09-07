"""Append-only research records. Never writes trades or promotes trading rules."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

KINDS = {'memory', 'decision', 'review', 'learning', 'strategy'}


class ConflictError(ValueError):
    pass


class ProjectMemory:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS project_records (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE,
                idempotency_key TEXT NOT NULL UNIQUE,
                digest TEXT NOT NULL,
                document TEXT NOT NULL)''')

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def append(self, payload):
        if not isinstance(payload, dict):
            raise ValueError('body must be an object')
        allowed = {'kind', 'content', 'source', 'event_time', 'idempotency_key',
                   'related_ids', 'supersedes', 'expected_version', 'verification'}
        if set(payload) - allowed:
            raise ValueError('unknown fields: ' + ', '.join(sorted(set(payload) - allowed)))
        for key in ('kind', 'source', 'event_time', 'idempotency_key'):
            if not isinstance(payload.get(key), str) or not payload[key].strip():
                raise ValueError(key + ' must be a nonempty string')
            if len(payload[key]) > 2048:
                raise ValueError(key + ' too long')
        if payload['kind'] not in KINDS:
            raise ValueError('unsupported kind')
        if not isinstance(payload.get('content'), dict) or not payload['content']:
            raise ValueError('content must be a nonempty object')
        when = datetime.fromisoformat(payload['event_time'].replace('Z', '+00:00'))
        if when.utcoffset() is None:
            raise ValueError('event_time needs timezone')
        verification = payload.get('verification', 'unverified')
        if verification not in {'unverified', 'user_confirmed', 'source_checked'}:
            raise ValueError('unsupported verification')
        related = payload.get('related_ids', [])
        if not isinstance(related, list) or len(related) > 30 or any(not isinstance(x, str) for x in related):
            raise ValueError('related_ids must be up to 30 record IDs')
        if payload['kind'] in {'review', 'learning'} and not related:
            raise ValueError('reviews and learnings require related records')
        previous = payload.get('supersedes')
        expected = payload.get('expected_version')
        if previous is not None and (not isinstance(previous, str) or not previous):
            raise ValueError('supersedes must be a record ID')
        if previous and (type(expected) is not int or expected < 1):
            raise ValueError('correction requires expected_version')
        if not previous and expected is not None:
            raise ValueError('expected_version requires supersedes')
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
        if len(canonical.encode()) > 60000:
            raise ValueError('record too large')
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            old = conn.execute('SELECT * FROM project_records WHERE idempotency_key=?',
                               (payload['idempotency_key'],)).fetchone()
            if old:
                if old['digest'] != digest:
                    raise ConflictError('idempotency key reused with different content')
                return json.loads(old['document']), False
            linked = {}
            for record_id in related + ([previous] if previous else []):
                row = conn.execute('SELECT document FROM project_records WHERE id=?', (record_id,)).fetchone()
                if not row:
                    raise ValueError('unknown related record: ' + record_id)
                linked[record_id] = json.loads(row['document'])
            version = 1
            if previous:
                parent = linked[previous]
                if parent['kind'] != payload['kind'] or parent['version'] != expected:
                    raise ConflictError('correction kind/version mismatch')
                # Prevent two corrections branching from the same old version.
                if any(json.loads(r[0]).get('supersedes') == previous
                       for r in conn.execute('SELECT document FROM project_records')):
                    raise ConflictError('record already superseded')
                version = parent['version'] + 1
            if payload['kind'] == 'review' and not any(linked[x]['kind'] == 'decision' for x in related):
                raise ValueError('review must link a decision')
            if payload['kind'] == 'learning' and not any(linked[x]['kind'] == 'review' for x in related):
                raise ValueError('learning must link a review')
            document = dict(payload, id='pr_' + uuid.uuid4().hex,
                            recorded_at=datetime.now(timezone.utc).isoformat(),
                            version=version, verification=verification)
            # A research note cannot silently become an executable skill.
            if payload['kind'] == 'learning':
                document['learning_status'] = 'candidate'
            cur = conn.execute('INSERT INTO project_records(id,idempotency_key,digest,document) VALUES(?,?,?,?)',
                               (document['id'], payload['idempotency_key'], digest, '{}'))
            document['sequence'] = cur.lastrowid
            conn.execute('UPDATE project_records SET document=? WHERE id=?',
                         (json.dumps(document, ensure_ascii=False, allow_nan=False), document['id']))
            return document, True

    def list(self, after=0, limit=100):
        after, limit = int(after), int(limit)
        if after < 0 or not 1 <= limit <= 500:
            raise ValueError('after >= 0 and limit 1..500 required')
        with self.connection() as conn:
            rows = conn.execute('SELECT document FROM project_records WHERE seq>? ORDER BY seq LIMIT ?',
                                (after, limit + 1)).fetchall()
        docs = [json.loads(r[0]) for r in rows[:limit]]
        return {'records': docs, 'has_more': len(rows) > limit,
                'next_after': docs[-1]['sequence'] if docs else after}

    def current(self):
        # Context includes all unsuperseded background records; paginated log is separate.
        with self.connection() as conn:
            docs = [json.loads(r[0]) for r in conn.execute('SELECT document FROM project_records ORDER BY seq')]
        superseded = {d.get('supersedes') for d in docs if d.get('supersedes')}
        return [d for d in docs if d['id'] not in superseded]
