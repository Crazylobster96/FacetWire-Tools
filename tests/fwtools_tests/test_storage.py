# SPDX-License-Identifier: Apache-2.0
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from facetwire_tools.descriptor import DocumentError, encode
from facetwire_tools.journal import digest
from facetwire_tools.storage import DescriptorStore
from fwtools_tests import test_journal as fixtures


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.JournalTests(methodName="test_patch_undo_redo_recovery_and_monotonic_revision")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.path, self.editor, self.raw = self.fixture.path, self.fixture.editor, self.fixture.raw

    def store(self, **changes):
        args = dict(scope="workspace", actor="actor", authorize=lambda scope: True, max_events=16,
                    max_total_bytes=1048576, create=not self.path.exists())
        args.update(changes)
        return DescriptorStore(self.path, self.editor, **args)

    def save(self, store, raw=None, **changes):
        args = dict(actor="actor", source_key="source", expected_revision=0, operation_id="initial", mode="managed_local")
        args.update(changes)
        return store.save(self.raw if raw is None else raw, **args)

    def read(self, store, revision=1, **changes):
        args = dict(actor="actor", source_key="source", expected_revision=revision, max_bytes=65536)
        args.update(changes)
        return store.read(**args)

    def lookup(self, store, **changes):
        args = dict(actor="actor", source_key="source", operation_id="initial", max_bytes=65536)
        args.update(changes)
        return store.reconcile(**args)

    def test_candidate_is_immutable_not_publication_and_reopen_reads_current(self):
        store = self.store()
        initial = self.save(store)
        self.assertEqual(1, initial["source_revision"])
        changed = self.editor.prepare_patch(self.raw, tuple(self.fixture.command()["payload"]))
        candidate = self.save(store, changed, expected_revision=1, operation_id="candidate", mode="candidate")
        self.assertEqual((1, 1), (candidate["source_revision"], candidate["based_on_revision"]))
        self.assertEqual(self.raw, self.read(store)["descriptor"])
        self.assertEqual(changed, self.lookup(store, operation_id="candidate")["descriptor"])
        published = self.save(store, changed, expected_revision=1, operation_id="publish")
        self.assertEqual(2, published["source_revision"])
        self.assertEqual(changed, self.read(self.store(), 2)["descriptor"])
        self.assertEqual(self.raw, self.lookup(store)["descriptor"])
        self.assertTrue(self.save(store)["duplicate"])

    def test_source_cas_identity_unknown_candidate_and_idempotency_conflicts(self):
        store = self.store()
        with self.assertRaises(DocumentError):
            self.save(store, mode="candidate")
        self.save(store)
        for changes in ({"operation_id": "new"}, {"expected_revision": 2, "operation_id": "new"},
                        {"mode": "candidate"}, {"actor": "other"}):
            with self.assertRaises(DocumentError):
                self.save(store, **changes)
        other = json.loads(self.raw)
        other["id"] = "different"
        with self.assertRaises(DocumentError):
            self.save(store, encode(other), expected_revision=1, operation_id="other")
        self.save(store, encode(other), source_key="other", operation_id="other")
        with self.assertRaises(DocumentError):
            self.lookup(store, source_key="other")

    def test_invalid_input_types_and_explicit_configuration(self):
        for changes in ({"scope": ""}, {"actor": True}, {"authorize": None}, {"create": 1},
                        {"max_events": 0}, {"max_events": 129}, {"max_total_bytes": 0}, {"max_total_bytes": 67108865}):
            with self.assertRaises(DocumentError):
                self.store(**changes)
        with patch.object(self, "path", Path("relative")), self.assertRaises(DocumentError):
            self.store()
        with patch("facetwire_tools.storage.Path.is_symlink", return_value=True), self.assertRaises(DocumentError):
            self.store()
        with patch.object(self, "editor", object()), self.assertRaises(DocumentError):
            self.store()
        store = self.store()
        for changes in ({"mode": None}, {"mode": "filesystem"}, {"expected_revision": True}, {"expected_revision": -1},
                        {"expected_revision": 129}, {"source_key": "../bad"}, {"operation_id": ""}, {"actor": None}):
            with self.assertRaises(DocumentError):
                self.save(store, **changes)
        for raw in (b"", b"\xff", bytearray(self.raw), "text"):
            with self.assertRaises(DocumentError):
                self.save(store, raw)
        with self.assertRaises(DocumentError):
            self.store(scope="different")
        with self.assertRaises(DocumentError):
            store._validate({}, self.raw)

    def test_exact_capacity_bytes_and_events_no_silent_pruning(self):
        request = dict(actor="actor", source_key="source", expected_revision=0, operation_id="initial",
                       mode="managed_local", digest=digest(self.raw))
        size = len(self.raw) + len(encode(request))
        store = self.store(max_events=1, max_total_bytes=size)
        self.save(store)
        self.assertTrue(self.save(store)["duplicate"])
        with self.assertRaises(DocumentError):
            self.save(store, expected_revision=1, operation_id="next")
        original = self.path
        self.path = self.path.parent / "small.sqlite3"
        small = self.store(max_total_bytes=size - 1)
        with self.assertRaises(DocumentError):
            self.save(small)
        with self.assertRaises(DocumentError):
            self.read(small)
        self.path = original

    def test_read_and_operation_scope_revision_and_output_limits(self):
        store = self.store()
        with self.assertRaises(DocumentError):
            self.read(store)
        self.save(store)
        self.assertEqual(self.raw, self.read(store, max_bytes=len(self.raw))["descriptor"])
        self.assertEqual(self.raw, self.lookup(store, max_bytes=len(self.raw))["descriptor"])
        for changes in ({"source_key": "missing"}, {"expected_revision": 2}, {"max_bytes": len(self.raw) - 1},
                        {"source_key": ""}, {"expected_revision": 0}, {"max_bytes": 0}, {"max_bytes": 65537}):
            with self.assertRaises(DocumentError):
                self.read(store, **changes)
        for changes in ({"operation_id": "missing"}, {"source_key": "different"}, {"max_bytes": len(self.raw) - 1},
                        {"operation_id": ""}, {"max_bytes": True}):
            with self.assertRaises(DocumentError):
                self.lookup(store, **changes)

    def test_current_access_revocation_rolls_back_and_reads_recheck(self):
        for allowed in (False, 1, None):
            with self.assertRaises(DocumentError):
                self.store(authorize=lambda scope: allowed)
        store = self.store()
        calls = iter((True, False))
        store.authorize = lambda scope: next(calls)
        with self.assertRaises(DocumentError):
            self.save(store)
        store.authorize = lambda scope: True
        with self.assertRaises(DocumentError):
            self.read(store)
        self.save(store)
        for invoke in (lambda: self.read(store), lambda: self.lookup(store)):
            calls = iter((True, False))
            store.authorize = lambda scope: next(calls)
            with self.assertRaises(DocumentError):
                invoke()

    def test_transaction_fault_and_stored_byte_readback_prevent_false_ack(self):
        store = self.store()
        self.fixture.sql("CREATE TRIGGER corrupt_insert AFTER INSERT ON object_events BEGIN UPDATE object_events SET descriptor=X'78' WHERE operation_id=NEW.operation_id; END")
        with self.assertRaises(DocumentError):
            self.save(store)
        with self.assertRaises(DocumentError):
            self.read(store)
        self.fixture.sql("DROP TRIGGER corrupt_insert")
        self.fixture.sql("CREATE TRIGGER deny_insert BEFORE INSERT ON object_events BEGIN SELECT RAISE(ABORT,'full'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.save(store)
        with self.assertRaises(DocumentError):
            self.read(store)

    def test_lost_ack_and_competing_publish_cas(self):
        first, second = self.store(), self.store()
        self.save(first)
        def attempt(store, op):
            try:
                return self.save(store, expected_revision=1, operation_id=op)
            except DocumentError:
                return "conflict"
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda pair: attempt(*pair), ((first, "one"), (second, "two"))))
        self.assertEqual(1, results.count("conflict"))
        connect = first._connect
        class LostReply:
            def __init__(self):
                self.db = connect()
            def execute(self, *args):
                return self.db.execute(*args)
            def close(self):
                self.db.close()
            def commit(self):
                self.db.commit()
                raise OSError("lost reply")
        with patch.object(first, "_connect", LostReply), self.assertRaises(OSError):
            self.save(first, expected_revision=2, operation_id="last")
        self.assertEqual(3, self.lookup(second, operation_id="last")["receipt"]["source_revision"])
        self.assertTrue(self.save(second, expected_revision=2, operation_id="last")["duplicate"])

    def test_corruption_config_request_sequence_digest_and_budget_refused(self):
        store = self.store()
        self.save(store)
        with closing(sqlite3.connect(self.path)) as db:
            meta = db.execute("SELECT * FROM object_meta").fetchall()
            events = db.execute("SELECT * FROM object_events").fetchall()
        mutations = [("DELETE FROM object_meta", ()), ("UPDATE object_meta SET config=?", (b"{}",)),
                     ("UPDATE object_events SET request=?", (b"x",)),
                     ("UPDATE object_events SET request=?", (b"{}",)),
                     ("UPDATE object_events SET request=?", (b" " + events[0][2],)),
                     ("UPDATE object_events SET operation_id='wrong'", ()),
                     ("UPDATE object_events SET ordinal=2", ()),
                     ("UPDATE object_events SET request=?", (encode({**json.loads(events[0][2]), "digest": "bad"}),))]
        for sql, params in mutations:
            self.fixture.sql(sql, params)
            with self.subTest(sql=sql), self.assertRaises(DocumentError):
                self.read(store)
            with closing(sqlite3.connect(self.path)) as db:
                db.execute("DELETE FROM object_meta")
                db.execute("DELETE FROM object_events")
                db.executemany("INSERT INTO object_meta VALUES (?,?)", meta)
                db.executemany("INSERT INTO object_events VALUES (?,?,?,?)", events)
                db.commit()
        with patch.object(store, "limits", (0, 1048576)), self.assertRaises(DocumentError):
            self.read(store)
        with patch.object(store, "limits", (16, 1)), self.assertRaises(DocumentError):
            self.read(store)
