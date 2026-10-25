"""Append-only, content-checked SQLite records and actual storage clocks."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import json
import os
from pathlib import Path
import sqlite3
import uuid

from one_trading.value_evidence import sha


def utc():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def instant(value):
    if not isinstance(value, str):
        raise ValueError("timestamp_required")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone_required")
    return parsed.astimezone(timezone.utc)


def stamp(value):
    return instant(value).isoformat(timespec="microseconds")


def decimal(value):
    if isinstance(value, (bool, float)) or value is None:
        raise ValueError("decimal_string_or_integer_required")
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("invalid_decimal") from exc
    if not result.is_finite():
        raise ValueError("nonfinite_decimal")
    return result


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    return sha(canonical(value))


def identifier(prefix, value):
    return prefix + "-" + digest(value)


def required(data, *names):
    missing = [name for name in names if name not in data]
    if missing:
        raise ValueError("missing_fields:" + ",".join(missing))


def nonempty(data, *names):
    required(data, *names)
    if any(not data[name] for name in names):
        raise ValueError("empty_fields:" + ",".join(names))


def one_of(value, allowed):
    if value not in allowed:
        raise ValueError("invalid_enum:" + str(value))


def atomic_bytes(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temp.open("wb") as file:
        file.write(content)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp, path)


def write_json(path, value):
    atomic_bytes(path, json.dumps(value, ensure_ascii=False, indent=2,
                                 allow_nan=False).encode("utf-8") + b"\n")


class Store:
    """The ledger is local, not an externally attested timestamp service.

    Every entity is immutable. Domain modules validate cross-record references;
    one generic SQL envelope keeps all revisions and failed attempts queryable.
    """

    def __init__(self, root):
        self.root = Path(root).resolve()
        if any((parent / ".git").exists() for parent in (self.root, *self.root.parents)):
            raise ValueError("runtime_must_be_outside_git")
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "research.sqlite3", isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(Path(__file__).with_name("schema.sql").read_text())

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def put(self, kind, key, body):
        existing = self.get(kind, key, optional=True)
        if existing is not None:
            if canonical(existing) != canonical(body):
                raise ValueError("immutable_id_conflict:" + kind + ":" + key)
            return existing
        encoded = canonical(body)
        self.db.execute("INSERT INTO records(kind,id,body,sha256,recorded_at) VALUES(?,?,?,?,?)",
                        (kind, key, encoded.decode(), sha(encoded), utc()))
        return body

    def get(self, kind, key, optional=False):
        row = self.db.execute("SELECT * FROM records WHERE kind=? AND id=?", (kind, key)).fetchone()
        if row is None:
            if optional:
                return None
            raise ValueError("missing_reference:" + kind + ":" + str(key))
        if sha(row["body"].encode()) != row["sha256"]:
            raise ValueError("record_tampered:" + kind + ":" + str(key))
        return json.loads(row["body"])

    def all(self, kind):
        return [self.get(kind, row[0]) for row in self.db.execute(
            "SELECT id FROM records WHERE kind=? ORDER BY sequence", (kind,)).fetchall()]

    def attempt(self, operation, payload, status, detail):
        key = uuid.uuid4().hex
        self.put("attempt", key, dict(attempt_id=key, operation=operation,
                                     payload_hash=digest(payload), status=status,
                                     detail=detail, recorded_at=utc()))

    def verify(self):
        for row in self.db.execute("SELECT kind,id FROM records").fetchall():
            self.get(row[0], row[1])
        if self.db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("sqlite_integrity")
        return self.db.execute("SELECT COUNT(*) FROM records").fetchone()[0]


def check_revision(store, kind, body, id_field, parent_field, identity_fields):
    parent_id = body.get(parent_field)
    if parent_id:
        parent = store.get(kind, parent_id)
        if parent_id == body[id_field] or any(parent[f] != body[f] for f in identity_fields):
            raise ValueError("revision_identity_mismatch")
        children = [r for r in store.all(kind) if r.get(parent_field) == parent_id]
        if any(r[id_field] != body[id_field] for r in children):
            raise ValueError("revision_branch_requires_new_identity")
    elif any(all(r[f] == body[f] for f in identity_fields) and r[id_field] != body[id_field]
             for r in store.all(kind)):
        raise ValueError("revision_parent_required")
