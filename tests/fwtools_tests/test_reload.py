# SPDX-License-Identifier: Apache-2.0
import copy
import json
import unittest

from facetwire_tools.descriptor import DocumentError, encode
from facetwire_tools.journal import digest
from facetwire_tools.storage import DescriptorStore
from fwtools_tests import test_journal as fixtures


class ReloadTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.JournalTests(methodName="test_patch_undo_redo_recovery_and_monotonic_revision")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.journal = self.fixture.journal()
        self.changed = self.fixture.editor.prepare_patch(self.fixture.raw, tuple(self.fixture.command()["payload"]))

    def command(self, *, revision=1, head=1, raw=None, source_revision=2, op="reload", policy="reject", approval=None):
        raw = self.changed if raw is None else raw
        return self.fixture.command(op, revision, head, "reload", {
            "descriptor_utf8": raw.decode("utf-8"), "dirty_policy": policy, "approval_id": approval,
            "source": {"scope": "store", "source_key": "source", "revision": source_revision, "digest": digest(raw)}})

    def reload(self, command=None, **changes):
        args = dict(verify_source=lambda raw, scope: True, approve_discard=lambda raw, scope: False)
        args.update(changes)
        return self.journal.reload(self.command() if command is None else command, **args)

    def snapshot(self, revision):
        return self.fixture.snapshot(self.journal, revision)

    def test_clean_reload_changes_baseline_and_undo_only_restores_draft(self):
        result = self.reload()
        self.assertEqual((2, False, 2), (result["memory_revision"], result["dirty"], result["source"]["revision"]))
        self.assertEqual(self.changed, self.snapshot(2)["descriptor"])
        self.assertTrue(self.reload()["duplicate"])
        result = self.journal.apply(self.fixture.command("undo", 2, 2, "undo", 1))
        self.assertTrue(result["dirty"])
        self.assertEqual(2, result["source"]["revision"])
        self.assertEqual(self.fixture.raw, self.snapshot(3)["descriptor"])
        result = self.journal.apply(self.fixture.command("redo", 3, 1, "redo", 1))
        self.assertFalse(result["dirty"])
        recovered = self.fixture.journal()
        self.assertEqual(2, self.fixture.snapshot(recovered, 4)["source"]["revision"])

    def test_dirty_reload_rejects_missing_or_denied_approval_keeps_history(self):
        self.journal.apply(self.fixture.command())
        for command in (self.command(revision=2, head=2),
                        self.command(revision=2, head=2, policy="discard"),
                        self.command(revision=2, head=2, policy="discard", approval="approval")):
            with self.assertRaises(DocumentError):
                self.reload(command)
            self.assertTrue(self.snapshot(2)["dirty"])
        cmd = self.command(revision=2, head=2, raw=self.fixture.raw, policy="discard", approval="approval")
        seen = []
        def approve(raw, scope):
            seen.append((json.loads(raw), scope))
            return True
        result = self.reload(cmd, approve_discard=approve)
        self.assertEqual(2, len(seen))
        self.assertEqual(2, seen[0][1]["current"]["memory_revision"])
        self.assertEqual(digest(self.changed), seen[0][1]["current"]["document_digest"])
        self.assertFalse(result["dirty"])
        undo = self.journal.apply(self.fixture.command("undo", 3, 3, "undo", 1))
        self.assertTrue(undo["dirty"])
        self.assertEqual(self.changed, self.snapshot(4)["descriptor"])
        self.assertEqual(3, len(self.journal.history(actor="actor", after_revision=0, limit=128)["entries"]))

    def test_plain_apply_cannot_bypass_reload_verifiers_and_invalid_payloads(self):
        with self.assertRaises(DocumentError):
            self.journal.apply(self.command())
        for changes in ({"verify_source": None}, {"approve_discard": None}):
            with self.assertRaises(DocumentError):
                self.reload(**changes)
        with self.assertRaises(DocumentError):
            self.reload(self.fixture.command())
        invalid = [None, {}, {**self.command()["payload"], "extra": 1}]
        for key, values in (("source", (None, {}, {"scope": "s"})),
                            ("descriptor_utf8", (None, 1, "bad")), ("dirty_policy", (None, "save")),
                            ("approval_id", ("", False))):
            for value in values:
                bad = copy.deepcopy(self.command()["payload"])
                bad[key] = value
                invalid.append(bad)
        for payload in invalid:
            command = self.command()
            command["payload"] = payload
            with self.assertRaises(DocumentError):
                self.reload(command)
        for key, value in (("scope", ""), ("source_key", "../bad"), ("revision", True),
                           ("revision", 0), ("revision", 129), ("digest", "bad")):
            cmd = self.command()
            cmd["payload"]["source"][key] = value
            with self.assertRaises(DocumentError):
                self.reload(cmd)

    def test_source_identity_revision_and_digest_cannot_regress_after_reload(self):
        self.reload()
        for key, value in (("scope", "other"), ("source_key", "other"), ("revision", 1)):
            command = self.command(revision=2, head=2, op="next")
            command["payload"]["source"][key] = value
            with self.assertRaises(DocumentError):
                self.reload(command)
        with self.assertRaises(DocumentError):
            self.reload(self.command(revision=2, head=2, raw=self.fixture.raw, op="same-revision"))
        self.reload(self.command(revision=2, head=2, op="same-content"))
        result = self.reload(self.command(revision=3, head=3, op="new-source", source_revision=3, raw=self.fixture.raw))
        self.assertEqual(3, result["source"]["revision"])

    def test_source_or_approval_revoked_at_final_check_rolls_back(self):
        for returns in ((False,), (1,), (True, False)):
            calls = iter(returns)
            with self.assertRaises(DocumentError):
                self.reload(verify_source=lambda raw, scope: next(calls))
            self.assertEqual(self.fixture.raw, self.snapshot(1)["descriptor"])
        def throwing(raw, scope):
            raise OSError("synthetic private path not exposed")
        with self.assertRaisesRegex(DocumentError, "unavailable"):
            self.reload(verify_source=throwing)
        self.journal.apply(self.fixture.command())
        approvals = iter((True, False))
        with self.assertRaises(DocumentError):
            self.reload(self.command(revision=2, head=2, policy="discard", approval="a"),
                        approve_discard=lambda raw, scope: next(approvals))
        self.assertEqual(2, self.snapshot(2)["memory_revision"])

    def test_callback_receives_frozen_request_and_cannot_mutate_live_state(self):
        command = self.command()
        def verify(raw, scope):
            command["payload"]["source"]["revision"] = 99
            scope["current"]["source"] = {"revision": 999}
            return True
        self.reload(command, verify_source=verify)
        self.assertEqual(2, self.snapshot(2)["source"]["revision"])

    def test_real_object_store_verification_and_source_change_preserves_draft(self):
        store = DescriptorStore(self.fixture.path.parent / "objects.sqlite3", self.fixture.editor, scope="store", actor="actor",
                                authorize=lambda scope: True, max_events=16, max_total_bytes=1048576, create=True)
        store.save(self.fixture.raw, actor="actor", source_key="source", expected_revision=0, operation_id="initial", mode="managed_local")
        store.save(self.changed, actor="actor", source_key="source", expected_revision=1, operation_id="new", mode="managed_local")
        def verify(raw, scope):
            source = json.loads(raw)["payload"]["source"]
            actual = store.read(actor=scope["actor"], source_key=source["source_key"], expected_revision=source["revision"], max_bytes=65536)
            return source["scope"] == store.scope and actual["digest"] == source["digest"]
        self.reload(verify_source=verify)
        self.journal.apply(self.fixture.command("undo", 2, 2, "undo", 1))
        self.assertEqual(self.changed, store.read(actor="actor", source_key="source", expected_revision=2, max_bytes=65536)["descriptor"])
        store.save(self.fixture.raw, actor="actor", source_key="source", expected_revision=2, operation_id="later", mode="managed_local")
        with self.assertRaises(DocumentError):
            self.reload(self.command(revision=3, head=1, op="stale", policy="discard", approval="approval"),
                        verify_source=verify, approve_discard=lambda raw, scope: True)
        self.assertEqual(self.fixture.raw, self.snapshot(3)["descriptor"])
