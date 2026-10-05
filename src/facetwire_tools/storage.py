# SPDX-License-Identifier: Apache-2.0
"""Managed descriptor object storage. Not an arbitrary filesystem package overwrite adapter."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from .descriptor import DescriptorEditor, DocumentError, bounded_int, encode
from .package_editor import PackageEditor
from .journal import digest, identity


class DescriptorStore:
    """Append-only immutable descriptor objects; managed source versions replayed under SQLite CAS."""
    def __init__(self, path, editor, *, scope, actor, authorize, max_events, max_total_bytes, create):
        self.path = Path(path)
        if not self.path.is_absolute() or self.path.is_symlink():
            raise DocumentError("trusted absolute object-store path required")
        if type(editor) not in (DescriptorEditor, PackageEditor) or type(create) is not bool or not callable(authorize):
            raise DocumentError("exact editor and explicit store configuration required")
        self.editor, self.scope, self.authorize = editor, identity(scope), authorize
        self.limits = (bounded_int(max_events, 1, 128), bounded_int(max_total_bytes, 1, 67108864))
        self.config = encode({"scope": self.scope, "schema": editor.schema_digest,
                              "editor_limits": editor.limits, "limits": self.limits})
        self._access(actor, "open", None)
        with closing(self._connect(create)) as db:
            db.execute("BEGIN IMMEDIATE")
            if create:
                db.execute("CREATE TABLE object_meta (id INTEGER PRIMARY KEY CHECK(id=1), config BLOB NOT NULL)")
                db.execute("CREATE TABLE object_events (operation_id TEXT PRIMARY KEY, ordinal INTEGER UNIQUE NOT NULL, request BLOB NOT NULL, descriptor BLOB NOT NULL)")
                db.execute("INSERT INTO object_meta VALUES (1,?)", (self.config,))
            self._replay(db)
            self._access(actor, "open", None)
            db.commit()

    def _connect(self, create=False):
        db = sqlite3.connect(self.path.as_uri() + ("?mode=rwc" if create else "?mode=rw"), uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        return db

    def _access(self, actor, action, source_key):
        identity(actor)
        if self.authorize({"scope": self.scope, "actor": actor, "action": action, "source_key": source_key}) is not True:
            raise DocumentError("document storage not currently authorized")

    def _validate(self, request, raw):
        if type(request) is not dict or set(request) != {"operation_id", "actor", "mode", "source_key", "expected_revision", "digest"}:
            raise DocumentError("invalid stored object request")
        for name in ("operation_id", "actor", "source_key"):
            identity(request[name])
        bounded_int(request["expected_revision"], 0, 128)
        if request["mode"] not in ("candidate", "managed_local"):
            raise DocumentError("unsupported storage mode")
        self.editor.parse(raw)
        if digest(raw) != request["digest"]:
            raise DocumentError("stored object digest mismatch")

    def _advance(self, sources, request, raw):
        key, expected = request["source_key"], request["expected_revision"]
        current = sources.get(key)
        revision = 0 if current is None else current["revision"]
        if expected != revision:
            raise DocumentError("source version conflict")
        if current is not None and self.editor.parse(current["descriptor"])["id"] != self.editor.parse(raw)["id"]:
            raise DocumentError("source document identity conflict")
        if request["mode"] == "managed_local":
            revision += 1
            sources[key] = {"revision": revision, "digest": request["digest"], "descriptor": raw,
                            "operation_id": request["operation_id"], "source_key": key}
        elif current is None:
            raise DocumentError("candidate requires an existing managed source")
        return {"source_key": key, "source_revision": revision, "based_on_revision": expected,
                "mode": request["mode"], "digest": request["digest"], "operation_id": request["operation_id"]}

    def _replay(self, db):
        meta = db.execute("SELECT * FROM object_meta WHERE id=1").fetchone()
        if meta is None or meta["config"] != self.config:
            raise DocumentError("object store configuration corrupt")
        sources, operations, total = {}, {}, 0
        rows = db.execute("SELECT * FROM object_events ORDER BY ordinal").fetchall()
        if len(rows) > self.limits[0]:
            raise DocumentError("object store event capacity exceeded")
        for ordinal, row in enumerate(rows, 1):
            total += len(row["request"]) + len(row["descriptor"])
            try:
                request = json.loads(row["request"])
            except (ValueError, TypeError):
                raise DocumentError("object store request corrupt") from None
            self._validate(request, row["descriptor"])
            if (encode(request) != row["request"] or request["operation_id"] != row["operation_id"]
                    or row["ordinal"] != ordinal):
                raise DocumentError("object event chain corrupt")
            receipt = self._advance(sources, request, row["descriptor"])
            operations[row["operation_id"]] = (row["request"], row["descriptor"], receipt)
        if total > self.limits[1]:
            raise DocumentError("object store byte capacity exceeded")
        return sources, operations, total

    def save(self, raw, *, actor, source_key, expected_revision, operation_id, mode):
        self.editor.parse(raw)
        request = {"actor": actor, "source_key": source_key, "expected_revision": expected_revision,
                   "operation_id": operation_id, "mode": mode, "digest": digest(raw)}
        self._validate(request, raw)
        self._access(actor, mode, source_key)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            result = self._save_transaction(db, request, raw)
            db.commit()
            return result

    def _save_transaction(self, db, request, raw):
        """Trusted caller checks access before opening DB and owns commit/rollback."""
        request = json.loads(encode(request))
        self._validate(request, raw)
        encoded = encode(request)
        sources, operations, total = self._replay(db)
        previous = operations.get(request["operation_id"])
        if previous is not None:
            if previous[0] != encoded or previous[1] != raw:
                raise DocumentError("storage operation identity conflict")
            result = previous[2]
        else:
            result = self._advance(sources, request, raw)
            if len(operations) >= self.limits[0] or total + len(encoded) + len(raw) > self.limits[1]:
                raise DocumentError("object storage full; explicit archival required")
            db.execute("INSERT INTO object_events VALUES (?,?,?,?)", (request["operation_id"], len(operations) + 1, encoded, raw))
            # Verify actual bytes within this transaction before publishing/acknowledging.
            _, persisted, _ = self._replay(db)
            result = persisted[request["operation_id"]][2]
        self._access(request["actor"], request["mode"], request["source_key"])
        return {"duplicate": previous is not None, **result}

    def read(self, *, actor, source_key, expected_revision, max_bytes):
        identity(source_key)
        bounded_int(expected_revision, 1, 128)
        bounded_int(max_bytes, 1, self.editor.limits[0])
        self._access(actor, "read", source_key)
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            sources, _, _ = self._replay(db)
            current = sources.get(source_key)
            if current is None or current["revision"] != expected_revision or len(current["descriptor"]) > max_bytes:
                raise DocumentError("source unavailable, changed or over budget")
            self._access(actor, "read", source_key)
            return dict(current)

    def reconcile(self, *, actor, source_key, operation_id, max_bytes):
        identity(source_key)
        identity(operation_id)
        bounded_int(max_bytes, 1, self.editor.limits[0])
        self._access(actor, "read_candidate", source_key)
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            _, operations, _ = self._replay(db)
            previous = operations.get(operation_id)
            if previous is None or previous[2]["source_key"] != source_key or len(previous[1]) > max_bytes:
                raise DocumentError("object operation unavailable or over budget")
            self._access(actor, "read_candidate", source_key)
            return {"receipt": dict(previous[2]), "descriptor": previous[1]}
