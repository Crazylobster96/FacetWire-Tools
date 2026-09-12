# SPDX-License-Identifier: Apache-2.0
import json
import unittest

from facetwire_tools.descriptor import DocumentError
from facetwire_tools.storage import DescriptorStore
from fwtools_tests import test_reload as fixtures


class SavepointTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ReloadTests(methodName="test_clean_reload_changes_baseline_and_undo_only_restores_draft")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.journal = self.fixture.journal

    def command(self, **changes):
        cmd = self.fixture.command(**changes)
        cmd["action"] = "savepoint"
        return cmd

    def accept(self, command, **changes):
        args = dict(verify_source=lambda raw, scope: True)
        args.update(changes)
        return self.journal.accept_saved(command, **args)

    def test_saved_checkpoint_does_not_enter_undo_path_but_updates_dirty_baseline(self):
        self.journal.apply(self.fixture.fixture.command())
        cmd = self.command(revision=2, head=2, op="save")
        receipt = self.accept(cmd)
        self.assertEqual((3, 2, False), (receipt["memory_revision"], receipt["history_head"], receipt["dirty"]))
        self.assertTrue(self.accept(cmd)["duplicate"])
        undo = self.journal.apply(self.fixture.fixture.command("undo", 3, 2, "undo", 1))
        self.assertTrue(undo["dirty"])
        self.assertEqual(self.fixture.fixture.raw, self.fixture.snapshot(4)["descriptor"])
        redo = self.journal.apply(self.fixture.fixture.command("redo", 4, 1, "redo", 1))
        self.assertFalse(redo["dirty"])
        self.assertEqual(2, redo["source"]["revision"])
        recovered = self.fixture.fixture.journal()
        self.assertEqual(5, self.fixture.fixture.snapshot(recovered, 5)["memory_revision"])
        self.assertEqual(["patch", "savepoint", "undo", "redo"],
                         [e["action"] for e in recovered.history(actor="actor", after_revision=0, limit=128)["entries"]])

    def test_clean_savepoint_preserves_redo_and_does_not_add_edit_step(self):
        cmd = self.command(raw=self.fixture.fixture.raw)
        result = self.accept(cmd)
        self.assertFalse(result["can_undo"])
        self.assertFalse(result["dirty"])
        self.journal.apply(self.fixture.fixture.command("patch", 2, 1))
        self.journal.apply(self.fixture.fixture.command("undo", 3, 3, "undo", 1))
        result = self.accept(self.command(revision=4, head=1, raw=self.fixture.fixture.raw, op="save2", source_revision=3))
        self.assertTrue(result["can_redo"])
        self.assertEqual(1, result["history_head"])

    def test_modified_draft_wrong_verifier_or_discard_cannot_be_marked_clean(self):
        self.journal.apply(self.fixture.fixture.command())
        for cmd in (self.command(), self.command(revision=2, head=2, raw=self.fixture.fixture.raw),
                    self.command(revision=2, head=2, policy="discard"),
                    self.command(revision=2, head=2, approval="not-needed")):
            with self.assertRaises(DocumentError):
                self.accept(cmd)
        valid = self.command(revision=2, head=2)
        with self.assertRaises(DocumentError):
            self.journal.apply(valid)
        with self.assertRaises(DocumentError):
            self.accept(valid, verify_source=None)
        with self.assertRaises(DocumentError):
            self.accept(self.fixture.command())
        self.assertTrue(self.fixture.snapshot(2)["dirty"])

    def test_source_recheck_failure_keeps_draft_dirty_without_saved_marker(self):
        self.journal.apply(self.fixture.fixture.command())
        for answers in ((False,), (True, False)):
            calls = iter(answers)
            with self.assertRaises(DocumentError):
                self.accept(self.command(revision=2, head=2), verify_source=lambda raw, scope: next(calls))
        self.assertTrue(self.fixture.snapshot(2)["dirty"])
        self.assertFalse(self.journal.reconcile(actor="actor", operation_id="reload")["recorded"])

    def test_real_managed_save_accept_undo_and_reload_without_external_rollback(self):
        original = self.fixture.fixture
        store = DescriptorStore(original.path.parent / "objects.sqlite3", original.editor, scope="store", actor="actor",
                                authorize=lambda scope: True, max_events=16, max_total_bytes=1048576, create=True)
        store.save(original.raw, actor="actor", source_key="source", expected_revision=0, operation_id="initial", mode="managed_local")
        self.journal.apply(original.command())
        snapshot = self.fixture.snapshot(2)
        candidate = store.save(snapshot["descriptor"], actor="actor", source_key="source", expected_revision=1,
                               operation_id="candidate", mode="candidate")
        self.assertEqual(1, candidate["source_revision"])
        self.assertTrue(self.fixture.snapshot(2)["dirty"])
        saved = store.save(snapshot["descriptor"], actor="actor", source_key="source", expected_revision=1,
                           operation_id="saved", mode="managed_local")
        self.assertEqual(2, saved["source_revision"])
        def verify(raw, scope):
            source = json.loads(raw)["payload"]["source"]
            actual = store.read(actor=scope["actor"], source_key=source["source_key"], expected_revision=source["revision"], max_bytes=65536)
            return source["scope"] == store.scope and source["digest"] == actual["digest"]
        accepted = self.accept(self.command(revision=2, head=2, op="saved-marker"), verify_source=verify)
        self.assertFalse(accepted["dirty"])
        self.journal.apply(original.command("undo", 3, 2, "undo", 1))
        self.assertTrue(self.fixture.snapshot(4)["dirty"])
        self.assertEqual(self.fixture.changed, store.read(actor="actor", source_key="source", expected_revision=2, max_bytes=65536)["descriptor"])
        loaded = self.journal.reload(self.fixture.command(revision=4, head=1, op="reload-saved", policy="discard", approval="approval"),
                                     verify_source=verify, approve_discard=lambda raw, scope: True)
        self.assertFalse(loaded["dirty"])
        self.assertEqual(self.fixture.changed, self.fixture.snapshot(5)["descriptor"])
