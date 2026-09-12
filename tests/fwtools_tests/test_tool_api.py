# SPDX-License-Identifier: Apache-2.0
import json
import unittest

from jsonschema import Draft202012Validator

from facetwire_tools.descriptor import DocumentError
from facetwire_tools.tool_api import DocumentTools, definitions
from fwtools_tests import test_journal as fixtures


class ToolApiTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.JournalTests(methodName="test_patch_undo_redo_recovery_and_monotonic_revision")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.journal = self.fixture.journal()
        self.tools = DocumentTools(self.journal, actor="actor", max_input_bytes=65536, max_output_bytes=65536)

    def call(self, name, **args):
        return self.tools.call("facetwire.document." + name, {"session_id": self.tools.session_id, **args})

    def mutation(self, **args):
        defaults = dict(operation_id="op", expected_revision=1, expected_head=1, operations=self.fixture.command()["payload"])
        defaults.update(args)
        return defaults

    def test_each_schema_separate_fresh_valid_and_no_source_write_or_code_tool(self):
        descriptions = definitions()
        self.assertEqual(9, len(descriptions))
        for item in descriptions:
            Draft202012Validator.check_schema(item["parameters"])
            self.assertFalse(item["source_save"])
        descriptions[0]["parameters"]["properties"]["session_id"]["maxLength"] = 0
        self.assertEqual(32, definitions()[0]["parameters"]["properties"]["session_id"]["maxLength"])
        for name in (None, [], "facetwire.document.save", "exec"):
            with self.assertRaises(DocumentError):
                self.tools.call(name, {})

    def test_generic_agent_calls_preview_patch_validate_snapshot_undo_redo_and_history(self):
        inspected = self.call("inspect", expected_revision=1)
        self.assertEqual(5, len(inspected["objects"]))
        preview = self.call("preview_patch", expected_revision=1, expected_head=1, operations=self.fixture.command()["payload"])
        self.assertFalse(preview["persisted"])
        self.assertEqual(1, self.call("snapshot", expected_revision=1)["memory_revision"])
        self.assertNotEqual(inspected["document_digest"], preview["candidate"]["document_digest"])
        patched = self.call("apply_patch", **self.mutation())
        self.assertEqual("durable_draft_only", patched["effect"])
        self.assertTrue(patched["dirty"])
        self.assertTrue(self.call("apply_patch", **self.mutation())["duplicate"])
        self.assertEqual(2, self.call("validate", expected_revision=2)["memory_revision"])
        self.assertNotIn("objects", self.call("validate", expected_revision=2))
        document = json.loads(self.call("snapshot", expected_revision=2)["descriptor_utf8"])
        self.assertEqual("changed", document["canvas"]["pages"][0]["layers"][0]["zones"][0]["content"]["text"])
        undo = self.call("undo", operation_id="undo", expected_revision=2, expected_head=2, count=1)
        self.assertFalse(undo["dirty"])
        self.call("redo", operation_id="redo", expected_revision=3, expected_head=1, count=1)
        history = self.call("history", after_revision=0, limit=128)
        self.assertEqual(["patch", "undo", "redo"], [e["action"] for e in history["entries"]])
        self.assertTrue(self.call("reconcile", operation_id="redo")["recorded"])
        self.assertFalse(self.call("reconcile", operation_id="unknown")["recorded"])

    def test_host_actor_not_overridable_scope_permissions_and_new_handle_on_reopen(self):
        old_session = self.tools.session_id
        reopened = DocumentTools(self.fixture.journal(), actor="actor", max_input_bytes=65536, max_output_bytes=65536)
        self.assertNotEqual(old_session, reopened.session_id)
        with self.assertRaises(DocumentError):
            reopened.call("facetwire.document.inspect", {"session_id": old_session, "expected_revision": 1})
        with self.assertRaises(DocumentError):
            self.call("inspect", expected_revision=1, actor="other")
        self.fixture.allowed = False
        with self.assertRaises(DocumentError):
            self.call("apply_patch", **self.mutation())
        with self.assertRaises(DocumentError):
            DocumentTools(self.journal, actor="actor", max_input_bytes=65536, max_output_bytes=65536)

    def test_invalid_schema_nonjson_bounds_and_preconditions_fail_without_history(self):
        for args in ({}, {"session_id": "short", "expected_revision": 1},
                     {"session_id": "中" * 32, "expected_revision": 1},
                     {"session_id": self.tools.session_id, "expected_revision": True},
                     {"session_id": self.tools.session_id, "expected_revision": 130},
                     {"session_id": self.tools.session_id, "expected_revision": 1, "path": "../secret"}):
            with self.assertRaises(DocumentError):
                self.tools.call("facetwire.document.inspect", args)
        with self.assertRaises(DocumentError):
            self.tools.call("facetwire.document.inspect", object())
        for ops in ([], [self.fixture.command()["payload"][0]] * 129, [{"target_id": "z"}]):
            with self.assertRaises(DocumentError):
                self.call("apply_patch", **self.mutation(operations=ops))
        with self.assertRaises(DocumentError):
            self.call("preview_patch", expected_revision=1, expected_head=2, operations=self.fixture.command()["payload"])
        self.assertEqual([], self.call("history", after_revision=0, limit=128)["entries"])

    def test_explicit_configuration_input_and_output_budgets(self):
        for key, bad in (("max_input_bytes", 0), ("max_input_bytes", 1048577), ("max_output_bytes", 4095),
                         ("max_output_bytes", 4194305), ("max_input_bytes", True), ("actor", "")):
            args = dict(actor="actor", max_input_bytes=65536, max_output_bytes=65536)
            args[key] = bad
            with self.assertRaises(DocumentError):
                DocumentTools(self.journal, **args)
        with self.assertRaises(DocumentError):
            DocumentTools(object(), actor="actor", max_input_bytes=1, max_output_bytes=4096)
        small = DocumentTools(self.journal, actor="actor", max_input_bytes=1, max_output_bytes=4096)
        with self.assertRaises(DocumentError):
            small.call("facetwire.document.inspect", {"session_id": small.session_id, "expected_revision": 1})
        payload = self.fixture.command()["payload"]
        payload[0]["value"] = "x" * 5000
        self.call("apply_patch", **self.mutation(operations=payload))
        small = DocumentTools(self.journal, actor="actor", max_input_bytes=1048576, max_output_bytes=4096)
        with self.assertRaises(DocumentError):
            small.call("facetwire.document.snapshot", {"session_id": small.session_id, "expected_revision": 2})
        self.assertEqual(2, self.call("inspect", expected_revision=2)["memory_revision"])

    def test_callback_arguments_frozen_and_final_read_permission_revocation(self):
        args = {"session_id": self.tools.session_id, "expected_revision": 1}
        def authorize(scope):
            args["expected_revision"] = 999
            return True
        self.journal.authorize = authorize
        self.assertEqual(1, self.tools.call("facetwire.document.inspect", args)["memory_revision"])
        responses = iter((True, True, True, False))
        self.journal.authorize = lambda scope: next(responses)
        with self.assertRaises(DocumentError):
            self.call("inspect", expected_revision=1)
