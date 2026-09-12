# SPDX-License-Identifier: Apache-2.0
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

import facetwire_tools
from facetwire_tools.descriptor import DocumentError, encode
from facetwire_tools.managed_save import ManagedSaves, _decode
from facetwire_tools.storage import DescriptorStore
from fwtools_tests import test_journal as fixtures


class ManagedSaveTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.JournalTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.journal = self.fixture.journal()
        self.allowed = True
        self.store = self.open_store(create=True)
        self.store.save(self.fixture.raw, actor="actor", source_key="source", expected_revision=0,
                        operation_id="initial", mode="managed_local")
        self.saves = self.open_saves(create=True)
        self.journal.apply(self.fixture.command())

    def open_store(self, **changes):
        args = dict(scope="store", actor="actor", authorize=lambda scope: self.allowed, max_events=16,
                    max_total_bytes=1_048_576, create=False)
        args.update(changes)
        return DescriptorStore(self.fixture.path, self.fixture.editor, **args)

    def open_saves(self, **changes):
        args = dict(actor="actor", source_key="source", max_saves=8, max_total_bytes=65_536, create=False)
        args.update(changes)
        return ManagedSaves(self.journal, self.store, **args)

    def request(self, **changes):
        args = dict(operation_id="save", expected_revision=2, expected_head=2, expected_source_revision=1)
        args.update(changes)
        return args

    def source(self, revision=1):
        return self.store.read(actor="actor", source_key="source", expected_revision=revision, max_bytes=65_536)

    def test_atomic_save_duplicate_reopen_and_undo_preserve_source_and_history(self):
        raw = self.fixture.snapshot(self.journal, 2)["descriptor"]
        result = self.saves.save(self.request())
        self.assertFalse(result["duplicate"])
        self.assertEqual((3, 2, False), tuple(result["receipt"]["draft"][key] for key in ("memory_revision", "history_head", "dirty")))
        self.assertEqual(raw, self.source(2)["descriptor"])
        self.journal = self.fixture.journal()
        self.store = self.open_store()
        self.saves = self.open_saves()
        self.assertTrue(self.saves.save(self.request())["duplicate"])
        self.assertTrue(self.saves.reconcile("save")["recorded"])
        self.journal.apply(self.fixture.command("undo", 3, 2, "undo", 1))
        state = self.saves.reconcile("save")
        self.assertTrue(state["current_draft"]["dirty"])
        self.assertFalse(state["receipt"]["draft"]["dirty"])
        self.assertEqual(raw, self.source(2)["descriptor"])
        self.journal.apply(self.fixture.command("redo", 4, 1, "redo", 1))
        self.assertFalse(self.saves.reconcile("save")["current_draft"]["dirty"])

    def test_explicit_absence_no_source_and_source_cas_mismatch(self):
        self.assertFalse(self.saves.reconcile("missing")["recorded"])
        with patch.object(self.saves, "source_key", "missing"):
            self.assertIsNone(self.saves.reconcile("missing")["current_source"])
            with self.assertRaises(DocumentError):
                self.saves.save(self.request())
        with self.assertRaises(DocumentError):
            self.saves.save(self.request(expected_source_revision=2))
        self.store.save(self.fixture.raw, actor="actor", source_key="source", expected_revision=1,
                        operation_id="external", mode="managed_local")
        with self.assertRaises(DocumentError):
            self.saves.save(self.request())
        self.assertTrue(self.fixture.snapshot(self.journal, 2)["dirty"])

    def test_strict_request_ids_revisions_and_duplicate_semantics(self):
        for value in (None, {}, {**self.request(), "actor": "other"}):
            with self.assertRaises(DocumentError):
                self.saves.save(value)
        for field, maximum in (("expected_revision", 129), ("expected_head", 129), ("expected_source_revision", 127)):
            for value in (True, 0, maximum + 1):
                with self.assertRaises(DocumentError):
                    self.saves.save(self.request(**{field: value}))
            for value in (1, maximum):
                self.assertEqual(value, self.saves._request(self.request(**{field: value}))[field])
        for value in ("", "a/b", "a" * 129):
            with self.assertRaises(DocumentError):
                self.saves.save(self.request(operation_id=value))
        for changes in (dict(expected_revision=1), dict(expected_head=1)):
            with self.assertRaises(DocumentError):
                self.saves.save(self.request(**changes))
        self.saves.save(self.request())
        with self.assertRaises(DocumentError):
            self.saves.save(self.request(expected_revision=3))

    def test_constructor_requires_exact_types_shared_db_profile_and_fixed_configuration(self):
        args = dict(actor="actor", source_key="source", max_saves=8, max_total_bytes=65_536, create=False)
        for journal, store in ((None, self.store), (self.journal, None)):
            with self.assertRaises(DocumentError):
                ManagedSaves(journal, store, **args)
        for field, value in (("create", 1), ("actor", ""), ("source_key", ""), ("max_saves", 0), ("max_saves", 129),
                             ("max_total_bytes", 0), ("max_total_bytes", 8_388_609), ("actor", "other")):
            with self.assertRaises(DocumentError):
                self.open_saves(**{field: value})
        with patch.object(self.store, "path", self.fixture.path.parent / "other.db"), self.assertRaises(DocumentError):
            self.open_saves()
        other = copy.copy(self.fixture.editor)
        other.schema_digest = "f" * 64
        with patch.object(self.store, "editor", other), self.assertRaises(DocumentError):
            self.open_saves()
        other.schema_digest = self.fixture.editor.schema_digest
        other.limits = (1000, *other.limits[1:])
        with patch.object(self.store, "editor", other), self.assertRaises(DocumentError):
            self.open_saves()

    def test_each_write_failure_rolls_back_source_version_marker_and_receipt(self):
        for table in ("object_events", "draft_events", "managed_save_events"):
            self.fixture.sql(f"CREATE TRIGGER deny BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT,'synthetic'); END")
            with self.assertRaises(sqlite3.IntegrityError):
                self.saves.save(self.request())
            self.fixture.sql("DROP TRIGGER deny")
            self.assertEqual(self.fixture.raw, self.source()["descriptor"])
            self.assertTrue(self.fixture.snapshot(self.journal, 2)["dirty"])
            self.assertFalse(self.saves.reconcile("save")["recorded"])

    def test_current_permission_before_during_and_after_save_blocks_commit(self):
        self.allowed = False
        for call in (lambda: self.saves.save(self.request()), lambda: self.saves.reconcile("save"), self.open_saves):
            with self.assertRaises(DocumentError):
                call()
        self.allowed = True
        original = self.saves._access
        count = [0]
        def access(action):
            count[0] += 1
            if count[0] == 2:
                raise DocumentError("synthetic revoked before commit")
            original(action)
        with patch.object(self.saves, "_access", access), self.assertRaises(DocumentError):
            self.saves.save(self.request())
        self.assertEqual(1, self.source()["revision"])
        self.assertFalse(self.saves.reconcile("save")["recorded"])

    def test_reserved_source_or_marker_operations_cannot_be_adopted(self):
        raw = self.fixture.snapshot(self.journal, 2)["descriptor"]
        source, marker = self.saves._commands(self.request(), raw)
        self.store.save(raw, actor="actor", source_key="source", expected_revision=1,
                        operation_id=source["operation_id"], mode="candidate")
        with self.assertRaises(DocumentError):
            self.saves.save(self.request())
        self.fixture.sql("DELETE FROM object_events WHERE operation_id=?", (source["operation_id"],))
        with patch.object(self.journal, "_replay", wraps=self.journal._replay) as replay:
            original = replay._mock_wraps
            def occupied(db):
                state, entries, total = original(db)
                entries[marker["operation_id"]] = (encode(marker), {})
                return state, entries, total
            replay.side_effect = occupied
            with self.assertRaises(DocumentError):
                self.saves.save(self.request())

    def test_capacity_limits_include_request_and_receipt_with_rollback(self):
        with patch.object(self.saves, "limits", (8, 1)), self.assertRaises(DocumentError):
            self.saves.save(self.request())
        self.assertEqual(1, self.source()["revision"])
        self.saves.save(self.request())
        with closing(self.journal._connect()) as db:
            row = db.execute("SELECT request,receipt FROM managed_save_events").fetchone()
        size = len(row[0]) + len(row[1])
        with patch.object(self.saves, "limits", (1, size)):
            self.assertTrue(self.saves.reconcile("save")["recorded"])
            with self.assertRaises(DocumentError):
                self.saves.save(self.request(operation_id="next", expected_revision=3, expected_source_revision=2))
        self.assertEqual(2, self.source(2)["revision"])
        with patch.object(self.saves, "limits", (0, 65_536)), self.assertRaises(DocumentError):
            self.saves.reconcile("save")

    def test_corrupt_config_request_receipt_or_binding_is_not_recovery_success(self):
        self.saves.save(self.request())
        with closing(self.journal._connect()) as db:
            row = db.execute("SELECT request,receipt FROM managed_save_events").fetchone()
        for encoded in (b"{", b"{}", b" " + row[0], encode({**json.loads(row[0]), "operation_id": "other"}),
                        encode({**json.loads(row[0]), "expected_source_revision": 2}),
                        encode({**json.loads(row[0]), "expected_head": 1})):
            self.fixture.sql("UPDATE managed_save_events SET request=?", (encoded,))
            with self.assertRaises(DocumentError):
                self.saves.reconcile("save")
        self.fixture.sql("UPDATE managed_save_events SET request=?", (row[0],))
        self.fixture.sql("UPDATE managed_save_events SET receipt=?", (b"{}",))
        with self.assertRaises(DocumentError):
            self.saves.reconcile("save")
        self.fixture.sql("UPDATE managed_save_events SET receipt=?", (row[1],))
        self.fixture.sql("DELETE FROM managed_save_meta")
        with self.assertRaises(DocumentError):
            self.saves.reconcile("save")

    def test_private_transaction_ports_validate_and_allow_explicit_rollback(self):
        for raw in (None, "{}", b"{", b" {}"):
            with self.assertRaises(DocumentError):
                _decode(raw)
        command = self.fixture.command("second", 2, 2)
        command["payload"][0].update(expected="changed", value="second")
        with closing(self.journal._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            for raw, verify in ((b" " + encode(command), lambda state: None), (encode(command), None)):
                with self.assertRaises(DocumentError):
                    self.journal._commit_transaction(db, raw, verify)
            self.journal._commit_transaction(db, encode(command), lambda state: None)
            db.rollback()
        self.assertEqual(2, self.fixture.snapshot(self.journal, 2)["memory_revision"])

    def test_source_readback_mismatch_rolls_back_the_entire_save(self):
        original = self.store._save_transaction
        def corrupt(db, request, raw):
            result = original(db, request, raw)
            db.execute("DELETE FROM object_events WHERE operation_id=?", (request["operation_id"],))
            return result
        with patch.object(self.store, "_save_transaction", corrupt), self.assertRaises(DocumentError):
            self.saves.save(self.request())
        self.assertEqual(1, self.source()["revision"])
        self.assertTrue(self.fixture.snapshot(self.journal, 2)["dirty"])

    def test_actual_process_exit_before_commit_preserves_dirty_draft(self):
        self.process_exit(False)

    def test_concurrent_saves_have_one_cas_winner_and_one_source_version(self):
        second = self.open_saves()
        def run(pair):
            instance, operation = pair
            try:
                return instance.save(self.request(operation_id=operation))
            except DocumentError:
                return "conflict"
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(run, ((self.saves, "first"), (second, "second"))))
        self.assertEqual(1, results.count("conflict"))
        self.assertEqual(2, self.source(2)["revision"])
        self.assertFalse(self.fixture.snapshot(self.journal, 3)["dirty"])

    def test_actual_stored_wrong_types_and_after_insert_corruption_fail_closed(self):
        self.saves.save(self.request())
        with closing(self.journal._connect()) as db:
            row = db.execute("SELECT request,receipt FROM managed_save_events").fetchone()
        for column in ("request", "receipt"):
            self.fixture.sql(f"UPDATE managed_save_events SET {column}=7")
            with self.assertRaises(DocumentError):
                self.saves.reconcile("save")
            self.fixture.sql("UPDATE managed_save_events SET request=?,receipt=?", tuple(row))
        self.fixture.sql("CREATE TRIGGER corrupt AFTER INSERT ON managed_save_events BEGIN UPDATE managed_save_events SET receipt=x'7b7d'; END")
        with self.assertRaises(DocumentError):
            self.saves.save(self.request(operation_id="second", expected_revision=3, expected_source_revision=2))
        self.assertEqual(2, self.source(2)["revision"])
        self.assertTrue(self.saves.reconcile("save")["recorded"])

    def test_maximum_opaque_operation_id_has_bounded_reply(self):
        result = self.saves.save(self.request(operation_id="x" * 128))
        self.assertLess(len(encode(result)), 4096)
        self.assertLess(len(encode(self.saves.reconcile("x" * 128))), 4096)

    def test_actual_process_exit_after_commit_reconciles_without_second_save(self):
        self.process_exit(True)

    def process_exit(self, after):
        script = '''
import os, sys
from pathlib import Path
from facetwire_tools.descriptor import DescriptorEditor
from facetwire_tools.journal import DraftJournal
from facetwire_tools.storage import DescriptorStore
from facetwire_tools.managed_save import ManagedSaves
editor = DescriptorEditor(Path(sys.argv[2]), max_bytes=65536, max_nodes=100, max_depth=32)
common = dict(actor="actor", authorize=lambda scope: True, max_events=16, max_total_bytes=1048576, create=False)
journal = DraftJournal(Path(sys.argv[1]), editor, scope="workspace", initial=None, **common)
store = DescriptorStore(Path(sys.argv[1]), editor, scope="store", **common)
saves = ManagedSaves(journal, store, actor="actor", source_key="source", max_saves=8, max_total_bytes=65536, create=False)
connect = journal._connect
class ExitConnection:
    def __init__(self): self.db = connect()
    def execute(self, *args): return self.db.execute(*args)
    def close(self): self.db.close()
    def commit(self):
        if sys.argv[3] == "after":
            self.db.commit()
            os._exit(42)
        os._exit(41)
journal._connect = ExitConnection
saves.save(dict(operation_id="save", expected_revision=2, expected_head=2, expected_source_revision=1))
'''
        env = dict(os.environ, PYTHONPATH=str(Path(facetwire_tools.__file__).resolve().parents[1]))
        result = subprocess.run([sys.executable, "-c", script, str(self.fixture.path), os.environ["FACETWIRE_SCHEMA_ROOT"],
                                 "after" if after else "before"], env=env, capture_output=True, timeout=30)
        self.assertEqual(42 if after else 41, result.returncode, result.stderr.decode(errors="replace"))
        state = self.open_saves().reconcile("save")
        self.assertEqual(after, state["recorded"])
        self.assertEqual(not after, state["current_draft"]["dirty"])
        self.assertEqual(2 if after else 1, state["current_source"]["revision"])
        if after:
            self.assertTrue(self.saves.save(self.request())["duplicate"])
