# SPDX-License-Identifier: Apache-2.0
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import facetwire_tools
from facetwire_tools.descriptor import DescriptorEditor, DocumentError, encode
from facetwire_tools.journal import DraftJournal
from fwtools_tests.test_descriptor import document


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "draft.sqlite3"
        self.editor = DescriptorEditor(Path(os.environ["FACETWIRE_SCHEMA_ROOT"]), max_bytes=65536, max_nodes=100, max_depth=32)
        self.raw = encode(document())
        self.allowed = True

    def journal(self, **changes):
        args = dict(scope="workspace", actor="actor", authorize=lambda scope: self.allowed,
                    max_events=16, max_total_bytes=1048576, create=not self.path.exists(),
                    initial=None if self.path.exists() else self.raw)
        args.update(changes)
        return DraftJournal(self.path, self.editor, **args)

    def command(self, op="op", revision=1, head=1, action="patch", payload=None):
        if payload is None:
            payload = [{"target_id": "zone", "field": "text", "expected": "Synthetic content", "value": "changed"}]
        return dict(operation_id=op, actor="actor", action=action, revision=revision, head=head, payload=payload)

    def snapshot(self, journal, revision):
        return journal.snapshot(actor="actor", expected_revision=revision, max_bytes=65536)

    def sql(self, command, args=()):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(command, args)
            db.commit()

    def test_patch_undo_redo_recovery_and_monotonic_revision(self):
        journal = self.journal()
        self.assertFalse(self.snapshot(journal, 1)["dirty"])
        result = journal.apply(self.command())
        self.assertEqual((2, 2, True), (result["memory_revision"], result["history_head"], result["dirty"]))
        self.assertNotEqual(self.raw, self.snapshot(journal, 2)["descriptor"])
        undo = self.command("undo", 2, 2, "undo", 1)
        result = self.journal().apply(undo)
        self.assertEqual((3, 1, False, True), (result["memory_revision"], result["history_head"], result["dirty"], result["can_redo"]))
        self.assertEqual(self.raw, self.snapshot(journal, 3)["descriptor"])
        result = journal.apply(self.command("redo", 3, 1, "redo", 1))
        self.assertEqual((4, 2, True, False), (result["memory_revision"], result["history_head"], result["dirty"], result["can_redo"]))
        self.assertEqual(4, self.snapshot(self.journal(), 4)["memory_revision"])

    def test_multistep_history_is_atomic_new_branch_retains_audit(self):
        journal = self.journal()
        journal.apply(self.command())
        second = self.command("second", 2, 2)
        second["payload"][0].update(expected="changed", value="second")
        journal.apply(second)
        for action, count in (("undo", 3), ("redo", 1)):
            with self.assertRaises(DocumentError):
                journal.apply(self.command("invalid", 3, 3, action, count))
        journal.apply(self.command("undo", 3, 3, "undo", 2))
        journal.apply(self.command("branch", 4, 1))
        with self.assertRaises(DocumentError):
            journal.apply(self.command("redo", 5, 5, "redo", 1))
        history = journal.history(actor="actor", after_revision=0, limit=2)
        self.assertTrue(history["has_more"])
        self.assertEqual(["op", "second"], [x["operation_id"] for x in history["entries"]])
        history = journal.history(actor="actor", after_revision=3, limit=128)
        self.assertFalse(history["has_more"])
        self.assertEqual(["undo", "branch"], [x["operation_id"] for x in history["entries"]])

    def test_duplicate_receipt_is_historical_not_current(self):
        journal = self.journal()
        first = journal.apply(self.command())
        journal.apply(self.command("undo", 2, 2, "undo", 1))
        duplicate = journal.apply(self.command())
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(first["memory_revision"], duplicate["memory_revision"])
        reconciled = journal.reconcile(actor="actor", operation_id="op")
        self.assertTrue(reconciled["recorded"])
        self.assertEqual(2, reconciled["receipt"]["memory_revision"])
        self.assertEqual(3, reconciled["current"]["memory_revision"])
        self.assertIsNone(journal.reconcile(actor="actor", operation_id="missing")["receipt"])
        bad = self.command()
        bad["payload"][0]["value"] = "different"
        with self.assertRaises(DocumentError):
            journal.apply(bad)

    def test_invalid_requests_bounds_and_patch_rollback(self):
        journal = self.journal()
        for key, values in (("operation_id", (None, True, "", "../x", "x" * 129)),
                            ("actor", (False, "bad actor")), ("revision", (False, 0, 130, 2)),
                            ("head", (None, 0, 130, 2)), ("action", ("save", [], None)),
                            ("payload", (None, {}, [], [1]))):
            for value in values:
                cmd = self.command()
                cmd[key] = value
                with self.subTest(key=key, value=value), self.assertRaises(DocumentError):
                    journal.apply(cmd)
        for cmd in (None, {}, {**self.command(), "extra": 1},
                    self.command(action="undo", payload=True), self.command(action="redo", payload=129)):
            with self.assertRaises(DocumentError):
                journal.apply(cmd)
        cmd = self.command()
        cmd["payload"].append({**cmd["payload"][0], "expected": "wrong"})
        with self.assertRaises(DocumentError):
            journal.apply(cmd)
        self.assertEqual(self.raw, self.snapshot(journal, 1)["descriptor"])

    def test_configuration_and_identity_boundaries(self):
        for key, values in (("scope", (False, "")), ("actor", (None,)), ("authorize", (None,)),
                            ("create", (None, 1)), ("max_events", (0, 129, True)),
                            ("max_total_bytes", (0, 67108865, True, len(self.raw) - 1)), ("initial", (None, b"invalid"))):
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(DocumentError):
                    self.journal(**{key: value})
        with self.assertRaises(DocumentError):
            DraftJournal(Path("relative"), self.editor, scope="s", actor="a", authorize=lambda s: True,
                         max_events=1, max_total_bytes=1, create=True, initial=self.raw)
        with patch("facetwire_tools.journal.Path.is_symlink", return_value=True), self.assertRaises(DocumentError):
            self.journal()
        with patch.object(self, "editor", object()), self.assertRaises(DocumentError):
            self.journal()
        self.journal(max_events=128, max_total_bytes=67108864)
        with self.assertRaises(DocumentError):
            self.journal(max_events=128, max_total_bytes=67108864, initial=self.raw)
        with self.assertRaises(DocumentError):
            self.journal(scope="different", max_events=128, max_total_bytes=67108864)

    def test_capacity_exact_events_and_bytes(self):
        required = len(self.raw) + len(encode(self.command())) + len(self.editor.prepare_patch(self.raw, tuple(self.command()["payload"])))
        journal = self.journal(max_events=1, max_total_bytes=required)
        journal.apply(self.command())
        self.assertTrue(journal.apply(self.command())["duplicate"])
        with self.assertRaises(DocumentError):
            journal.apply(self.command("undo", 2, 2, "undo", 1))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "small.sqlite3"
            with patch.object(self, "path", path):
                journal = self.journal(max_total_bytes=required - 1)
                with self.assertRaises(DocumentError):
                    journal.apply(self.command())
                self.assertEqual(1, self.snapshot(journal, 1)["memory_revision"])

    def test_read_limits_invalid_auth_and_exact_snapshot_bytes(self):
        journal = self.journal()
        self.assertEqual(self.raw, journal.snapshot(actor="actor", expected_revision=1, max_bytes=len(self.raw))["descriptor"])
        for changes in ({"expected_revision": True}, {"expected_revision": 2}, {"max_bytes": len(self.raw) - 1},
                        {"max_bytes": 65537}, {"actor": ""}):
            args = dict(actor="actor", expected_revision=1, max_bytes=65536)
            args.update(changes)
            with self.assertRaises(DocumentError):
                journal.snapshot(**args)
        for changes in ({"after_revision": -1}, {"after_revision": 130}, {"limit": 0}, {"limit": 129}):
            args = dict(actor="actor", after_revision=0, limit=1)
            args.update(changes)
            with self.assertRaises(DocumentError):
                journal.history(**args)
        with self.assertRaises(DocumentError):
            journal.reconcile(actor="actor", operation_id="")
        for allowed in (False, 1, None):
            self.allowed = allowed
            with self.assertRaises(DocumentError):
                self.journal()
            with self.assertRaises(DocumentError):
                journal.apply(self.command())
        self.allowed = True

    def test_final_revocation_rolls_back_journal_and_checks_reads(self):
        journal = self.journal()
        for invoke in (lambda: journal.apply(self.command()), lambda: self.snapshot(journal, 1),
                       lambda: journal.history(actor="actor", after_revision=0, limit=1),
                       lambda: journal.reconcile(actor="actor", operation_id="op")):
            calls = iter((True, False))
            journal.authorize = lambda scope: next(calls)
            with self.assertRaises(DocumentError):
                invoke()
        journal.authorize = lambda scope: True
        self.assertFalse(journal.reconcile(actor="actor", operation_id="op")["recorded"])

    def test_callback_cannot_mutate_frozen_patch_request(self):
        journal = self.journal()
        cmd = self.command()
        def authorize(scope):
            cmd["payload"][0]["value"] = "attacker"
            scope["scope"] = "wrong"
            return True
        journal.authorize = authorize
        journal.apply(cmd)
        value = json.loads(self.snapshot(journal, 2)["descriptor"])
        self.assertEqual("changed", value["canvas"]["pages"][0]["layers"][0]["zones"][0]["content"]["text"])

    def test_database_insert_failure_does_not_ack_or_change_memory(self):
        journal = self.journal()
        self.sql("CREATE TRIGGER deny_insert BEFORE INSERT ON draft_events BEGIN SELECT RAISE(ABORT,'synthetic-full'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            journal.apply(self.command())
        self.assertEqual(self.raw, self.snapshot(journal, 1)["descriptor"])

    def test_committed_but_lost_reply_reconciles_without_reapplication(self):
        journal = self.journal()
        connect = journal._connect
        class LostReply:
            def __init__(self):
                self.db = connect()
            def execute(self, *args):
                return self.db.execute(*args)
            def close(self):
                self.db.close()
            def commit(self):
                self.db.commit()
                raise OSError("synthetic lost reply")
        with patch.object(journal, "_connect", LostReply), self.assertRaises(OSError):
            journal.apply(self.command())
        self.assertTrue(journal.reconcile(actor="actor", operation_id="op")["recorded"])
        self.assertTrue(journal.apply(self.command())["duplicate"])

    def test_two_connections_compete_one_revision_and_same_operation(self):
        first, second = self.journal(), self.journal()
        def attempt(journal, command):
            try:
                return journal.apply(command)
            except DocumentError:
                return "conflict"
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda pair: attempt(*pair), ((first, self.command()), (second, self.command()))))
        self.assertEqual([False, True], sorted(r["duplicate"] for r in results))
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda pair: attempt(*pair), (
                (first, self.command("u1", 2, 2, "undo", 1)), (second, self.command("u2", 2, 2, "undo", 1)))))
        self.assertEqual(1, results.count("conflict"))

    def test_corrupt_baseline_config_event_chain_snapshots_and_limits_rejected(self):
        journal = self.journal()
        journal.apply(self.command())
        mutations = [
            ("DELETE FROM draft_meta", ()), ("UPDATE draft_meta SET config=?", (b"{}",)),
            ("UPDATE draft_meta SET digest='bad'", ()), ("UPDATE draft_events SET request=?", (b"x",)),
            ("UPDATE draft_events SET request=?", (encode({}),)),
            ("UPDATE draft_events SET request=?", (b' '+encode(self.command()),)),
            ("UPDATE draft_events SET operation_id='wrong'", ()),
            ("UPDATE draft_events SET revision=3", ()), ("UPDATE draft_events SET before_digest='bad'", ()),
            ("UPDATE draft_events SET after=?", (self.raw,)), ("UPDATE draft_events SET after_digest='bad'", ())]
        with closing(sqlite3.connect(self.path)) as db:
            backup = (db.execute("SELECT * FROM draft_meta").fetchall(), db.execute("SELECT * FROM draft_events").fetchall())
        for sql, args in mutations:
            self.sql(sql, args)
            with self.subTest(sql=sql), self.assertRaises(DocumentError):
                self.snapshot(journal, 2)
            with closing(sqlite3.connect(self.path)) as db:
                db.execute("DELETE FROM draft_meta")
                db.execute("DELETE FROM draft_events")
                db.executemany("INSERT INTO draft_meta VALUES (?,?,?,?)", backup[0])
                db.executemany("INSERT INTO draft_events VALUES (?,?,?,?,?,?)", backup[1])
                db.commit()
        with patch.object(journal, "limits", (0, 1048576)), self.assertRaises(DocumentError):
            self.snapshot(journal, 2)
        with patch.object(journal, "limits", (16, len(self.raw))), self.assertRaises(DocumentError):
            self.snapshot(journal, 2)

    def test_real_child_exit_after_committed_edit_recovers_unsaved_draft(self):
        self.journal()
        code = '''
import json, os, pathlib, sys
from facetwire_tools.descriptor import DescriptorEditor
from facetwire_tools.journal import DraftJournal
editor = DescriptorEditor(pathlib.Path(os.environ['FACETWIRE_SCHEMA_ROOT']), max_bytes=65536, max_nodes=100, max_depth=32)
journal = DraftJournal(pathlib.Path(sys.argv[1]), editor, scope='workspace', actor='actor', authorize=lambda s: True,
                       max_events=16, max_total_bytes=1048576, create=False, initial=None)
journal.apply(json.loads(sys.argv[2]))
os._exit(29)
'''
        # A separate interpreter imports only this independent project, never Pillow.
        env = dict(os.environ, PYTHONPATH=str(Path(facetwire_tools.__file__).resolve().parent.parent))
        result = subprocess.run([sys.executable, "-c", code, str(self.path), encode(self.command()).decode("utf-8")],
                                env=env, capture_output=True, timeout=20)
        self.assertEqual(29, result.returncode, result.stderr.decode("utf-8", errors="replace"))
        recovered = self.journal()
        self.assertTrue(self.snapshot(recovered, 2)["dirty"])
        self.assertTrue(recovered.apply(self.command())["duplicate"])
        recovered.apply(self.command("undo", 2, 2, "undo", 1))
        self.assertEqual(self.raw, self.snapshot(recovered, 3)["descriptor"])
