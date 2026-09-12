# SPDX-License-Identifier: Apache-2.0
import copy
import json
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator

from facetwire_tools.descriptor import DocumentError, encode
from facetwire_tools.storage import DescriptorStore
from facetwire_tools.storage_tools import StorageTools, definitions
from fwtools_tests import test_journal as fixtures


class StorageToolTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.JournalTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.journal = self.fixture.journal()
        self.allowed = True
        self.store = DescriptorStore(self.fixture.path.parent / "store.db", self.fixture.editor, scope="store", actor="actor",
                                     authorize=lambda scope: self.allowed, max_events=16, max_total_bytes=1_048_576, create=True)
        self.store.save(self.fixture.raw, actor="actor", source_key="source", expected_revision=0,
                        operation_id="initial", mode="managed_local")
        self.tools = self.open_tools()

    def open_tools(self, **changes):
        args = dict(actor="actor", source_key="source", approve_discard=lambda raw, scope: False,
                    max_input_bytes=65_536, max_output_bytes=65_536)
        args.update(changes)
        return StorageTools(self.journal, self.store, **args)

    def call(self, name, **args):
        return self.tools.call("facetwire." + name, dict(session_id=self.tools.session_id, **args))

    def reload(self, **changes):
        args = dict(expected_source_revision=1, expected_revision=1, expected_head=1,
                    operation_id="reload", dirty_policy="reject", approval_id=None)
        args.update(changes)
        return self.call("document.reload", **args)

    def publish(self, raw, revision=1, operation="publish"):
        return self.store.save(raw, actor="actor", source_key="source", expected_revision=revision,
                               operation_id=operation, mode="managed_local")

    def test_definitions_are_fresh_exact_and_do_not_advertise_source_save(self):
        items = definitions()
        self.assertEqual(4, len(items))
        for item in items:
            Draft202012Validator.check_schema(item["parameters"])
            self.assertFalse(item["source_save"])
            self.assertFalse(item["parameters"]["additionalProperties"])
        items[0]["parameters"]["properties"].clear()
        self.assertIn("session_id", definitions()[0]["parameters"]["properties"])

    def test_read_source_snapshot_and_original_operation_without_writing(self):
        result = self.call("source.inspect", expected_source_revision=1)
        self.assertNotIn("descriptor_utf8", result)
        self.assertEqual(len(self.fixture.raw), result["bytes"])
        for result in (self.call("source.snapshot", expected_source_revision=1),
                       self.call("source.reconcile", operation_id="initial")):
            self.assertEqual(self.fixture.raw.decode(), result["descriptor_utf8"])
            self.assertEqual("read_only", result["effect"])
        self.assertEqual([], self.journal.history(actor="actor", after_revision=0, limit=128)["entries"])

    def test_clean_reload_is_reversible_and_reopen_does_not_repeat_edit(self):
        changed = self.fixture.editor.prepare_patch(self.fixture.raw, tuple(self.fixture.command()["payload"]))
        self.publish(changed)
        receipt = self.reload(expected_source_revision=2)
        self.assertEqual((2, False), (receipt["memory_revision"], receipt["dirty"]))
        self.assertFalse(receipt["source_save"])
        self.journal = self.fixture.journal()
        self.tools = self.open_tools()
        self.assertTrue(self.reload(expected_source_revision=2)["duplicate"])
        self.journal.apply(self.fixture.command("undo", 2, 2, "undo", 1))
        self.assertTrue(self.fixture.snapshot(self.journal, 3)["dirty"])
        self.journal.apply(self.fixture.command("redo", 3, 1, "redo", 1))
        self.assertFalse(self.fixture.snapshot(self.journal, 4)["dirty"])
        self.assertEqual(changed.decode(), self.call("source.snapshot", expected_source_revision=2)["descriptor_utf8"])

    def test_dirty_reload_requires_current_bound_approval_not_just_supplied_id(self):
        self.journal.apply(self.fixture.command())
        for changes in ({}, dict(dirty_policy="discard"), dict(dirty_policy="discard", approval_id="approval")):
            with self.assertRaises(DocumentError):
                self.reload(expected_revision=2, expected_head=2, **changes)
        seen = []
        def approved(raw, scope):
            command = json.loads(raw)
            seen.append(scope)
            return (command["payload"]["approval_id"] == "approval" and command["revision"] == 2 and
                    scope["current"]["document_digest"] == self.fixture.snapshot(self.journal, 2)["document_digest"])
        self.tools = self.open_tools(approve_discard=approved)
        self.assertFalse(self.reload(expected_revision=2, expected_head=2, dirty_policy="discard", approval_id="approval")["dirty"])
        self.assertEqual(2, len(seen))

    def test_configuration_types_identity_limits_and_profile_mismatch(self):
        for journal, store, verifier in ((None, self.store, lambda a, b: True), (self.journal, None, lambda a, b: True),
                                         (self.journal, self.store, None)):
            with self.assertRaises(DocumentError):
                StorageTools(journal, store, actor="actor", source_key="source", approve_discard=verifier,
                             max_input_bytes=4096, max_output_bytes=4096)
        for field, values in (("actor", ("", "x/y")), ("source_key", ("",)),
                               ("max_input_bytes", (0, True, 1_048_577)), ("max_output_bytes", (4095, True, 4_194_305))):
            for value in values:
                with self.assertRaises(DocumentError):
                    self.open_tools(**{field: value})
        self.open_tools(max_input_bytes=1, max_output_bytes=4096)
        self.open_tools(max_input_bytes=1_048_576, max_output_bytes=4_194_304)
        # Same concrete editor type, but mismatched descriptor budget/schema is not attachable.
        other = copy.copy(self.fixture.editor)
        other.limits = (32_768, *other.limits[1:])
        with patch.object(self.store, "editor", other), self.assertRaises(DocumentError):
            self.open_tools()
        other.limits = self.fixture.editor.limits
        other.schema_digest = "f" * 64
        with patch.object(self.store, "editor", other), self.assertRaises(DocumentError):
            self.open_tools()

    def test_unknown_session_and_strict_arguments_never_mutate(self):
        for name in (None, "facetwire.source.save", "facetwire.document.apply_patch"):
            with self.assertRaises(DocumentError):
                self.tools.call(name, {})
        base = dict(session_id=self.tools.session_id, expected_source_revision=1)
        for args in ({}, None, {**base, "actor": "other"}, {**base, "path": "C:/other"},
                     {**base, "expected_source_revision": 0}, {**base, "expected_source_revision": True},
                     {**base, "expected_source_revision": 129}, {**base, "session_id": "x" * 32}):
            with self.assertRaises(DocumentError):
                self.tools.call("facetwire.source.inspect", args)
        with self.assertRaises(DocumentError):
            self.reload(dirty_policy="automatic")
        with self.assertRaises(DocumentError):
            self.reload(approval_id=1)
        self.assertEqual([], self.journal.history(actor="actor", after_revision=0, limit=128)["entries"])

    def test_input_and_output_budgets_and_current_authorization(self):
        self.tools = self.open_tools(max_input_bytes=1)
        with self.assertRaises(DocumentError):
            self.call("source.inspect", expected_source_revision=1)
        self.tools = self.open_tools(max_output_bytes=4096)
        large = json.loads(self.fixture.raw)
        large["canvas"]["pages"][0]["layers"][0]["zones"][0]["content"]["text"] = "x" * 5000
        self.publish(encode(large))
        with self.assertRaises(DocumentError):
            self.call("source.snapshot", expected_source_revision=2)
        self.allowed = False
        with self.assertRaises(DocumentError):
            self.open_tools()
        with self.assertRaises(DocumentError):
            self.call("source.inspect", expected_source_revision=2)

    def test_source_changes_or_revocation_during_reload_roll_back_draft(self):
        original = self.tools._read
        count = [0]
        def changed(revision):
            value = original(revision)
            count[0] += 1
            if count[0] == 1:
                self.publish(self.fixture.raw)
            return value
        with patch.object(self.tools, "_read", changed), self.assertRaises(DocumentError):
            self.reload()
        self.assertEqual(1, self.fixture.snapshot(self.journal, 1)["memory_revision"])
        with self.assertRaises(DocumentError):
            self.call("source.reconcile", operation_id="missing")

    def test_internal_verifier_binds_exact_source_and_actual_bytes(self):
        value = self.tools._read(1)
        payload = dict(descriptor_utf8=value["descriptor"].decode(),
                       source=dict(scope="store", source_key="source", revision=1, digest=value["digest"]))
        self.assertTrue(self.tools._verify(encode(dict(payload=payload)), {}))
        for field in ("scope", "source_key", "digest"):
            changed = copy.deepcopy(payload)
            changed["source"][field] = "other"
            self.assertFalse(self.tools._verify(encode(dict(payload=changed)), {}))
        payload["descriptor_utf8"] += " "
        self.assertFalse(self.tools._verify(encode(dict(payload=payload)), {}))

    def test_read_rechecks_authorization_after_read_before_return(self):
        original = self.tools._read
        def revoked(revision):
            result = original(revision)
            self.allowed = False
            return result
        with patch.object(self.tools, "_read", revoked), self.assertRaises(DocumentError):
            self.call("source.inspect", expected_source_revision=1)
