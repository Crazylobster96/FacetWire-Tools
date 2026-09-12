# SPDX-License-Identifier: Apache-2.0
"""Atomic managed-source save and draft savepoint in one protected SQLite database.

No external files, two-database transaction, network publication or automatic retry.
"""
from contextlib import closing
import json

from .descriptor import DocumentError, bounded_int, encode
from .journal import DraftJournal, digest, identity
from .storage import DescriptorStore


def _decode(raw):
    try:
        value = json.loads(raw)
        if type(raw) is not bytes or encode(value) != raw:
            raise ValueError("not canonical bytes")
    except (ValueError, TypeError):
        raise DocumentError("managed save record corrupt") from None
    return value


class ManagedSaves:
    def __init__(self, journal, store, *, actor, source_key, max_saves, max_total_bytes, create):
        if type(journal) is not DraftJournal or type(store) is not DescriptorStore or type(create) is not bool:
            raise DocumentError("typed journal/store and explicit creation required")
        if (journal.path.resolve() != store.path.resolve() or journal.editor.schema_digest != store.editor.schema_digest or
                journal.editor.limits != store.editor.limits):
            raise DocumentError("managed saves require one database and matching descriptor profiles")
        self.journal, self.store = journal, store
        self.actor, self.source_key = identity(actor), identity(source_key)
        self.limits = (bounded_int(max_saves, 1, 128), bounded_int(max_total_bytes, 1, 8_388_608))
        self.config = encode(dict(journal_config=digest(journal.config), store_config=digest(store.config),
                                  actor=self.actor, source_key=self.source_key, limits=self.limits, schema=1))
        self._access("save_reconcile")
        with closing(journal._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            if create:
                db.execute("CREATE TABLE managed_save_meta (id INTEGER PRIMARY KEY CHECK(id=1), config BLOB NOT NULL)")
                db.execute("CREATE TABLE managed_save_events (operation_id TEXT PRIMARY KEY, request BLOB NOT NULL, receipt BLOB NOT NULL)")
                db.execute("INSERT INTO managed_save_meta VALUES (1,?)", (self.config,))
            self._entries(db)
            self._access("save_reconcile")
            db.commit()

    def _access(self, action):
        self.journal._access(self.actor, action)
        self.store._access(self.actor, "read", self.source_key)
        self.store._access(self.actor, "managed_local" if action == "save" else "read_candidate", self.source_key)

    def _request(self, value):
        if type(value) is not dict or set(value) != {"operation_id", "expected_revision", "expected_head", "expected_source_revision"}:
            raise DocumentError("exact managed save request required")
        identity(value["operation_id"])
        bounded_int(value["expected_revision"], 1, 129)
        bounded_int(value["expected_head"], 1, 129)
        bounded_int(value["expected_source_revision"], 1, 127)
        return value

    def _commands(self, request, raw):
        key = digest(encode(request["operation_id"]))
        source = dict(operation_id="managed-source-" + key, actor=self.actor, source_key=self.source_key,
                      expected_revision=request["expected_source_revision"], mode="managed_local", digest=digest(raw))
        command = dict(operation_id="managed-marker-" + key, actor=self.actor, action="savepoint",
                       revision=request["expected_revision"], head=request["expected_head"],
                       payload=dict(descriptor_utf8=raw.decode("utf-8"), dirty_policy="reject", approval_id=None,
                                    source=dict(scope=self.store.scope, source_key=self.source_key,
                                                revision=request["expected_source_revision"] + 1, digest=digest(raw))))
        return source, command

    def _entries(self, db):
        meta = db.execute("SELECT config FROM managed_save_meta WHERE id=1").fetchone()
        if meta is None or meta[0] != self.config:
            raise DocumentError("managed save configuration unavailable")
        state, draft_ops, _ = self.journal._replay(db)
        sources, source_ops, _ = self.store._replay(db)
        rows = db.execute("SELECT operation_id,request,receipt FROM managed_save_events").fetchall()
        if any(type(row[1]) is not bytes or type(row[2]) is not bytes for row in rows):
            raise DocumentError("managed save history requires stored bytes")
        if len(rows) > self.limits[0] or sum(len(row[1]) + len(row[2]) for row in rows) > self.limits[1]:
            raise DocumentError("managed save history capacity exceeded")
        entries = {}
        for operation_id, encoded, saved in rows:
            request, receipt = self._request(_decode(encoded)), _decode(saved)
            source_id = "managed-source-" + digest(encode(request["operation_id"]))
            source = source_ops.get(source_id)
            if operation_id != request["operation_id"] or source is None:
                raise DocumentError("managed save source operation missing")
            source_command, marker_command = self._commands(request, source[1])
            marker = draft_ops.get(marker_command["operation_id"])
            if (source[0] != encode(source_command) or marker is None or marker[0] != encode(marker_command) or
                    receipt != dict(source=source[2], draft=marker[1])):
                raise DocumentError("managed save source and draft binding corrupt")
            entries[operation_id] = (request, receipt)
        return state, sources, source_ops, draft_ops, entries

    def save(self, request):
        encoded = encode(request)
        request = self._request(_decode(encoded))
        self._access("save")
        with closing(self.journal._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            state, sources, source_ops, draft_ops, entries = self._entries(db)
            previous = entries.get(request["operation_id"])
            if previous is not None:
                if previous[0] != request:
                    raise DocumentError("managed save operation identity conflict")
                receipt = previous[1]
            else:
                current = self.journal._receipt(state)
                source_state = sources.get(self.source_key)
                if ((current["memory_revision"], current["history_head"]) != (request["expected_revision"], request["expected_head"]) or
                        source_state is None or source_state["revision"] != request["expected_source_revision"]):
                    raise DocumentError("managed save draft or source revision conflict")
                raw = self.journal._raw(state)
                source_command, marker_command = self._commands(request, raw)
                if source_command["operation_id"] in source_ops or marker_command["operation_id"] in draft_ops:
                    raise DocumentError("managed save reserved operation already occupied")
                self.store._save_transaction(db, source_command, raw)

                def verify(unused_state):
                    actual = self.store._replay(db)[0].get(self.source_key)
                    if (actual is None or actual["revision"] != request["expected_source_revision"] + 1 or
                            actual["descriptor"] != raw or actual["digest"] != digest(raw)):
                        raise DocumentError("managed source not saved exactly")

                self.journal._access(self.actor, "savepoint")
                self.journal._commit_transaction(db, encode(marker_command), verify)
                actual_source = self.store._replay(db)[1][source_command["operation_id"]][2]
                actual_marker = self.journal._replay(db)[1][marker_command["operation_id"]][1]
                receipt = dict(source=actual_source, draft=actual_marker)
                db.execute("INSERT INTO managed_save_events VALUES (?,?,?)", (request["operation_id"], encoded, encode(receipt)))
                receipt = self._entries(db)[4][request["operation_id"]][1]
            self._access("save")
            db.commit()
            return dict(operation_id=request["operation_id"], duplicate=previous is not None, receipt=receipt)

    def reconcile(self, operation_id):
        identity(operation_id)
        self._access("save_reconcile")
        with closing(self.journal._connect()) as db:
            db.execute("BEGIN")
            state, sources, _, _, entries = self._entries(db)
            previous = entries.get(operation_id)
            source = sources.get(self.source_key)
            current_source = None if source is None else {key: value for key, value in source.items() if key != "descriptor"}
            self._access("save_reconcile")
            return dict(operation_id=operation_id, recorded=previous is not None,
                        receipt=None if previous is None else previous[1],
                        current_draft=self.journal._receipt(state), current_source=current_source)
