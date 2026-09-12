# SPDX-License-Identifier: Apache-2.0
"""Local descriptor drafts with durable, append-only editing history; never a source save."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .descriptor import DescriptorEditor, DocumentError, bounded_int, encode


def identity(value):
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value) is None:
        raise DocumentError("invalid opaque identity")
    return value


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


class DraftJournal:
    """Trusted host owns path/ACL, scope and authentication. This object is not a public session handle."""
    def __init__(self, path, editor, *, scope, actor, authorize, max_events, max_total_bytes, create, initial):
        self.path = Path(path)
        if not self.path.is_absolute() or self.path.is_symlink():
            raise DocumentError("trusted absolute journal path required")
        if type(editor) is not DescriptorEditor or type(create) is not bool or not callable(authorize):
            raise DocumentError("exact editor and explicit journal configuration required")
        self.editor, self.scope, self.authorize = editor, identity(scope), authorize
        self.limits = (bounded_int(max_events, 1, 128), bounded_int(max_total_bytes, 1, 67108864))
        self.config = encode({"scope": self.scope, "schema": editor.schema_digest,
                              "editor_limits": editor.limits, "limits": self.limits})
        self._access(actor, "open")
        if create:
            editor.parse(initial)
            if len(initial) > self.limits[1]:
                raise DocumentError("journal capacity exceeded")
        elif initial is not None:
            raise DocumentError("recovery must use persisted baseline")
        with closing(self._connect(create)) as db:
            db.execute("BEGIN IMMEDIATE")
            if create:
                db.execute("CREATE TABLE draft_meta (id INTEGER PRIMARY KEY CHECK(id=1), config BLOB NOT NULL, baseline BLOB NOT NULL, digest TEXT NOT NULL)")
                db.execute("CREATE TABLE draft_events (operation_id TEXT PRIMARY KEY, revision INTEGER UNIQUE NOT NULL, request BLOB NOT NULL, before_digest TEXT NOT NULL, after BLOB NOT NULL, after_digest TEXT NOT NULL)")
                db.execute("INSERT INTO draft_meta VALUES (1,?,?,?)", (self.config, initial, digest(initial)))
            self._replay(db)
            self._access(actor, "open")
            db.commit()

    def _connect(self, create=False):
        db = sqlite3.connect(self.path.as_uri() + ("?mode=rwc" if create else "?mode=rw"), uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        return db

    def _access(self, actor, action):
        identity(actor)
        if self.authorize({"scope": self.scope, "actor": actor, "action": action}) is not True:
            raise DocumentError("draft operation not currently authorized")

    def _request(self, value):
        if type(value) is not dict or set(value) != {"operation_id", "actor", "action", "revision", "head", "payload"}:
            raise DocumentError("exact draft command required")
        identity(value["operation_id"])
        identity(value["actor"])
        bounded_int(value["revision"], 1, 129)
        bounded_int(value["head"], 1, 129)
        action, payload = value["action"], value["payload"]
        if action == "patch":
            if type(payload) is not list:
                raise DocumentError("patch operation array required")
        elif action in ("undo", "redo"):
            bounded_int(payload, 1, 128)
        elif action in ("reload", "savepoint"):
            if type(payload) is not dict or set(payload) != {"descriptor_utf8", "source", "dirty_policy", "approval_id"}:
                raise DocumentError("exact reload payload required")
            source = payload["source"]
            if type(source) is not dict or set(source) != {"scope", "source_key", "revision", "digest"}:
                raise DocumentError("exact verified source reference required")
            identity(source["scope"])
            identity(source["source_key"])
            bounded_int(source["revision"], 1, 128)
            if type(payload["descriptor_utf8"]) is not str or payload["dirty_policy"] not in ("reject", "discard"):
                raise DocumentError("invalid reload descriptor or dirty policy")
            if payload["approval_id"] is not None:
                identity(payload["approval_id"])
            raw = payload["descriptor_utf8"].encode("utf-8")
            self.editor.parse(raw)
            if source["digest"] != digest(raw):
                raise DocumentError("reload source digest conflict")
        else:
            raise DocumentError("unsupported draft command")
        return value

    def _reduce(self, state, command):
        self._request(command)
        path, cursor, revision = state["path"], state["cursor"], state["revision"]
        if command["revision"] != revision or command["head"] != path[cursor][0]:
            raise DocumentError("draft revision or history head conflict")
        base, source = state["base"], state.get("source")
        if command["action"] == "patch":
            raw = self.editor.prepare_patch(path[cursor][1], tuple(command["payload"]))
            path = path[:cursor + 1] + [(revision + 1, raw)]
            cursor += 1
        elif command["action"] in ("reload", "savepoint"):
            payload = command["payload"]
            saving = command["action"] == "savepoint"
            if saving and (payload["descriptor_utf8"].encode("utf-8") != self._raw(state)
                           or payload["dirty_policy"] != "reject" or payload["approval_id"] is not None):
                raise DocumentError("savepoint must match current draft exactly without discard")
            if not saving and self._raw(state) != base and payload["dirty_policy"] == "reject":
                raise DocumentError("dirty reload requires explicit discard approval")
            if not saving and self._raw(state) != base and payload["approval_id"] is None:
                raise DocumentError("dirty discard approval identity required")
            new_source = payload["source"]
            if source is not None and (new_source["scope"] != source["scope"] or new_source["source_key"] != source["source_key"]
                                       or new_source["revision"] < source["revision"]
                                       or (new_source["revision"] == source["revision"] and new_source["digest"] != source["digest"])):
                raise DocumentError("reload source identity or revision conflict")
            base = payload["descriptor_utf8"].encode("utf-8")
            source = new_source
            if not saving:
                path = path[:cursor + 1] + [(revision + 1, base)]
                cursor += 1
        else:
            delta = command["payload"] * (-1 if command["action"] == "undo" else 1)
            cursor += delta
            if not 0 <= cursor < len(path):
                raise DocumentError("requested history unavailable")
        return {"path": path, "cursor": cursor, "revision": revision + 1, "base": base, "source": source}

    def _raw(self, state):
        return state["path"][state["cursor"]][1]

    def _receipt(self, state):
        raw = self._raw(state)
        return {"memory_revision": state["revision"], "history_head": state["path"][state["cursor"]][0],
                "document_digest": digest(raw), "dirty": raw != state["base"], "source": state.get("source"),
                "can_undo": state["cursor"] > 0, "can_redo": state["cursor"] + 1 < len(state["path"])}

    def _replay(self, db):
        meta = db.execute("SELECT * FROM draft_meta WHERE id=1").fetchone()
        if meta is None or meta["config"] != self.config or digest(meta["baseline"]) != meta["digest"]:
            raise DocumentError("journal configuration or baseline corrupt")
        self.editor.parse(meta["baseline"])
        state = {"path": [(1, meta["baseline"])], "cursor": 0, "revision": 1, "base": meta["baseline"]}
        rows = db.execute("SELECT * FROM draft_events ORDER BY revision").fetchall()
        total = len(meta["baseline"])
        receipts = {}
        if len(rows) > self.limits[0]:
            raise DocumentError("journal event capacity exceeded")
        for row in rows:
            total += len(row["request"]) + len(row["after"])
            try:
                command = self._request(json.loads(row["request"]))
            except (ValueError, TypeError):
                raise DocumentError("journal request corrupt") from None
            if (encode(command) != row["request"] or command["operation_id"] != row["operation_id"]
                    or row["revision"] != state["revision"] + 1 or row["before_digest"] != digest(self._raw(state))):
                raise DocumentError("journal event chain corrupt")
            state = self._reduce(state, command)
            if row["after"] != self._raw(state) or row["after_digest"] != digest(row["after"]):
                raise DocumentError("journal snapshot corrupt")
            receipts[row["operation_id"]] = (row["request"], self._receipt(state))
        if total > self.limits[1]:
            raise DocumentError("journal byte capacity exceeded")
        return state, receipts, total

    def apply(self, command):
        # Freeze all nested caller data before invoking any host callback.
        raw = encode(command)
        command = self._request(json.loads(raw))
        if command["action"] in ("reload", "savepoint"):
            raise DocumentError("reload requires trusted source and discard verification")
        return self._commit(raw, command, lambda state: None)

    def reload(self, command, *, verify_source, approve_discard):
        raw = encode(command)
        command = self._request(json.loads(raw))
        if command["action"] != "reload" or not callable(verify_source) or not callable(approve_discard):
            raise DocumentError("explicit reload verifiers required")

        def verify(state):
            scope = {"scope": self.scope, "actor": command["actor"], "current": self._receipt(state)}
            # Callbacks get fresh nested copies, never mutable reducer state.
            try:
                if verify_source(raw, json.loads(encode(scope))) is not True:
                    raise DocumentError("reload source not verified")
                if self._raw(state) != state["base"]:
                    if approve_discard(raw, json.loads(encode(scope))) is not True:
                        raise DocumentError("dirty discard not approved")
            except Exception:
                raise DocumentError("reload source or discard approval unavailable") from None

        return self._commit(raw, command, verify)

    def accept_saved(self, command, *, verify_source):
        raw = encode(command)
        command = self._request(json.loads(raw))
        if command["action"] != "savepoint" or not callable(verify_source):
            raise DocumentError("explicit saved source verifier required")

        def verify(state):
            try:
                scope = {"scope": self.scope, "actor": command["actor"], "current": self._receipt(state)}
                if verify_source(raw, json.loads(encode(scope))) is not True:
                    raise DocumentError("saved source not verified")
            except Exception:
                raise DocumentError("saved source unavailable") from None

        return self._commit(raw, command, verify)

    def _commit(self, raw, command, verify):
        self._access(command["actor"], command["action"])
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            result = self._commit_transaction(db, raw, verify)
            db.commit()
            return result

    def _commit_transaction(self, db, raw, verify):
        """Trusted pre-authorized same-database composition; caller owns commit/rollback."""
        command = self._request(json.loads(raw))
        if encode(command) != raw or not callable(verify):
            raise DocumentError("canonical draft bytes and verifier required")
        state, receipts, total = self._replay(db)
        previous = receipts.get(command["operation_id"])
        if previous is not None:
            if previous[0] != raw:
                raise DocumentError("operation identity conflict")
            result = previous[1]
        else:
            next_state = self._reduce(state, command)
            verify(state)
            after = self._raw(next_state)
            if len(receipts) >= self.limits[0] or total + len(raw) + len(after) > self.limits[1]:
                raise DocumentError("journal capacity exhausted; explicit archival required")
            db.execute("INSERT INTO draft_events VALUES (?,?,?,?,?,?)", (
                command["operation_id"], next_state["revision"], raw, digest(self._raw(state)), after, digest(after)))
            result = self._receipt(next_state)
            verify(state)
        self._access(command["actor"], command["action"])
        return {"operation_id": command["operation_id"], "duplicate": previous is not None, **result}

    def snapshot(self, *, actor, expected_revision, max_bytes):
        bounded_int(expected_revision, 1, 129)
        bounded_int(max_bytes, 1, self.editor.limits[0])
        self._access(actor, "snapshot")
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            state, _, _ = self._replay(db)
            raw = self._raw(state)
            if expected_revision != state["revision"] or len(raw) > max_bytes:
                raise DocumentError("snapshot conflict or budget exceeded")
            self._access(actor, "snapshot")
            return {**self._receipt(state), "descriptor": raw}

    def history(self, *, actor, after_revision, limit):
        bounded_int(after_revision, 0, 129)
        bounded_int(limit, 1, 128)
        self._access(actor, "history")
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            state, receipts, _ = self._replay(db)
            entries = []
            for request, receipt in receipts.values():
                if receipt["memory_revision"] > after_revision:
                    cmd = json.loads(request)
                    entries.append({"operation_id": cmd["operation_id"], "actor": cmd["actor"], "action": cmd["action"], **receipt})
            self._access(actor, "history")
            return {"current": self._receipt(state), "entries": entries[:limit], "has_more": len(entries) > limit}

    def reconcile(self, *, actor, operation_id):
        identity(operation_id)
        self._access(actor, "reconcile")
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            state, receipts, _ = self._replay(db)
            previous = receipts.get(operation_id)
            self._access(actor, "reconcile")
            return {"operation_id": operation_id, "recorded": previous is not None,
                    "receipt": None if previous is None else previous[1], "current": self._receipt(state)}
