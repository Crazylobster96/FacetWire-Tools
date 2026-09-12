# SPDX-License-Identifier: Apache-2.0
import unittest

from jsonschema import Draft202012Validator

from facetwire_tools.descriptor import DocumentError, encode
from facetwire_tools.managed_tools import ManagedSaveTools, definitions
from fwtools_tests import test_managed_save as fixtures


class ManagedToolTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ManagedSaveTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.tools = ManagedSaveTools(self.fixture.saves, max_input_bytes=65_536)

    def call(self, name, **args):
        return self.tools.call("facetwire.document." + name, dict(session_id=self.tools.session_id, **args))

    def test_two_fresh_schemas_and_actual_save_reconcile_reopen(self):
        items = definitions()
        self.assertEqual(2, len(items))
        for item in items:
            Draft202012Validator.check_schema(item["parameters"])
        items[0]["parameters"]["properties"].clear()
        self.assertIn("operation_id", definitions()[0]["parameters"]["properties"])
        absent = self.call("save_reconcile", operation_id="save")
        self.assertFalse(absent["recorded"])
        saved = self.call("save", **self.fixture.request())
        self.assertTrue(saved["source_save"])
        self.assertFalse(saved["receipt"]["draft"]["dirty"])
        self.tools = ManagedSaveTools(self.fixture.open_saves(), max_input_bytes=65_536)
        found = self.call("save_reconcile", operation_id="save")
        self.assertTrue(found["recorded"])
        self.assertFalse(found["source_save"])
        self.assertEqual(saved["receipt"], found["receipt"])
        self.assertTrue(self.call("save", **self.fixture.request())["duplicate"])
        self.assertLess(len(encode(found)), 4096)

    def test_constructor_budget_unknown_tool_and_strict_non_authoritative_parameters(self):
        with self.assertRaises(DocumentError):
            ManagedSaveTools(None, max_input_bytes=1)
        for limit in (0, True, 65_537):
            with self.assertRaises(DocumentError):
                ManagedSaveTools(self.fixture.saves, max_input_bytes=limit)
        for name in (None, "facetwire.document.save_as", "facetwire.source.save"):
            with self.assertRaises(DocumentError):
                self.tools.call(name, {})
        args = dict(session_id=self.tools.session_id, **self.fixture.request())
        for value in (None, {}, {**args, "path": "C:/other"}, {**args, "actor": "other"},
                      {**args, "session_id": "x" * 32}, {**args, "expected_revision": True},
                      {**args, "expected_source_revision": 128}):
            with self.assertRaises(DocumentError):
                self.tools.call("facetwire.document.save", value)
        self.tools = ManagedSaveTools(self.fixture.saves, max_input_bytes=1)
        with self.assertRaises(DocumentError):
            self.call("save", **self.fixture.request())
        self.assertFalse(self.fixture.saves.reconcile("save")["recorded"])

    def test_tool_session_does_not_bypass_current_authorization(self):
        self.fixture.allowed = False
        for call in (lambda: self.call("save", **self.fixture.request()),
                     lambda: self.call("save_reconcile", operation_id="save"),
                     lambda: ManagedSaveTools(self.fixture.saves, max_input_bytes=1)):
            with self.assertRaises(DocumentError):
                call()
