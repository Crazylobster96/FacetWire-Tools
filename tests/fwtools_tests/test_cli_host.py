# SPDX-License-Identifier: Apache-2.0
from contextlib import closing
import copy
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import facetwire_tools
from facetwire_tools.cli import main
from facetwire_tools.cli_host import CLIHost, DRAFT_ACTIONS, STORE_ACTIONS, parse, settings, synthetic_document
from facetwire_tools.descriptor import DocumentError, encode
from facetwire_tools.journal import digest
from facetwire_tools.tool_api import definitions as draft_definitions


class CLIHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "host.json"
        self.config = dict(profile="synthetic-local-tools-v1", database_path=str(self.root / "synthetic.db"),
                           schema_root=os.environ["FACETWIRE_SCHEMA_ROOT"], actor="actor", draft_scope="draft", store_scope="store",
                           source_key="source", editor_limits=[65_536, 100, 32], draft_limits=[32, 1_048_576],
                           store_limits=[32, 1_048_576], save_limits=[16, 1_048_576], max_input_bytes=65_536,
                           max_output_bytes=65_536, enabled=True, allow_initialize=True, draft_actions=sorted(DRAFT_ACTIONS),
                           store_actions=sorted(STORE_ACTIONS), discard_approvals=[])
        self.rewrite()

    def rewrite(self, **changes):
        config = copy.deepcopy(self.config)
        config.update(changes)
        self.path.write_bytes(encode(config))

    def host(self):
        return CLIHost(self.path)

    def initialized(self):
        host = self.host()
        self.assertEqual("initialized_synthetic", host.initialize()["status"])
        return host

    def call(self, host, tool, **args):
        return host.call(encode(dict(tool="facetwire." + tool, arguments=args)))

    def edit(self, host, **changes):
        args = dict(operation_id="edit", expected_revision=1, expected_head=1,
                    operations=[dict(target_id="zone", field="text", expected="Synthetic content", value="Synthetic updated")])
        args.update(changes)
        return self.call(host, "document.apply_patch", **args)

    def managed(self):
        self.config.update(profile="managed-local-tools-v1", document_id="managed-doc")
        self.rewrite()
        descriptor = synthetic_document()
        descriptor["id"] = "managed-doc"
        descriptor["title"] = "User selected source"
        return encode(descriptor)

    def test_trusted_managed_descriptor_initialize_edit_save_and_reopen(self):
        raw = self.managed()
        host = self.host()
        with self.assertRaises(DocumentError):
            host.initialize()
        self.assertFalse(host.database.exists())
        self.assertEqual("initialized_managed", host.initialize_document(raw)["status"])
        self.assertEqual("managed-local-tools-v1", host.describe()["profile"])
        self.assertEqual(raw.decode("utf-8"), self.call(host, "source.snapshot", expected_source_revision=1)["descriptor_utf8"])
        self.assertTrue(self.edit(host)["dirty"])
        self.call(host, "document.save", operation_id="save", expected_revision=2, expected_head=2,
                  expected_source_revision=1)
        reopened = self.host()
        self.assertIn("Synthetic updated", self.call(reopened, "source.snapshot", expected_source_revision=2)["descriptor_utf8"])
        with self.assertRaises(FileExistsError):
            reopened.initialize_document(raw)

    def test_managed_profile_and_initial_descriptor_reject_unsafe_input_before_file_creation(self):
        raw = self.managed()
        for changes in (dict(document_id=""), dict(document_id="wrong/id"), dict(document_id=None),
                        dict(profile="managed-local-tools-v1", document_id="managed-doc", extra=1)):
            self.rewrite(**changes)
            with self.assertRaises(DocumentError):
                settings(self.path)
        self.rewrite()
        host = self.host()
        for candidate in (b"", b"x" * 65_537, b"{}", encode(synthetic_document()),
                          raw.replace(b"managed-doc", b"other-doc"),
                          raw.replace(b'"resources":[]', b'"resources":[{}]')):
            with self.subTest(candidate=candidate[:25]), self.assertRaises(DocumentError):
                host.initialize_document(candidate)
            self.assertFalse(host.database.exists())
        with self.assertRaises(DocumentError):
            host._open(True)
        with self.assertRaises(DocumentError):
            host._open(True, encode(synthetic_document()))
        self.rewrite(enabled=False)
        with self.assertRaises(DocumentError):
            self.host().initialize_document(raw)
        self.assertFalse(host.database.exists())
        self.rewrite()
        self.assertEqual("initialized_managed", self.host().initialize_document(raw)["status"])

    def test_cli_managed_initialization_reads_bounded_binary_stdin_and_rejects_wrong_profile(self):
        raw = self.managed()
        sink = io.BytesIO()
        self.assertEqual(0, main(["--config", str(self.path), "initialize-document"],
                                 stdin=io.BytesIO(raw), stdout=sink))
        self.assertIn(b"initialized_managed", sink.getvalue())
        self.assertEqual(1, main(["--config", str(self.path), "initialize-document"],
                                 stdin=io.BytesIO(raw), stdout=io.BytesIO()))
        self.config.pop("document_id")
        self.config["profile"] = "synthetic-local-tools-v1"
        self.rewrite()
        with self.assertRaises(DocumentError):
            self.host().initialize_document(raw)
        self.assertEqual(1, main(["--config", str(self.path), "initialize-document"],
                                 stdin=io.BytesIO(raw), stdout=io.BytesIO()))

    def test_independent_fifteen_tools_full_edit_save_reopen_reload_history_flow(self):
        host = self.initialized()
        definitions = host.describe()["tools"]
        self.assertEqual(15, len(definitions))
        self.assertNotIn("session_id", str(definitions))
        self.assertTrue(self.edit(host)["dirty"])
        self.call(host, "document.undo", operation_id="undo", expected_revision=2, expected_head=2, count=1)
        self.call(host, "document.redo", operation_id="redo", expected_revision=3, expected_head=1, count=1)
        save = dict(operation_id="save", expected_revision=4, expected_head=2, expected_source_revision=1)
        self.assertFalse(self.call(host, "document.save", **save)["receipt"]["draft"]["dirty"])
        host = self.host()
        self.assertTrue(self.call(host, "document.save_reconcile", operation_id="save")["recorded"])
        self.assertTrue(self.call(host, "document.save", **save)["duplicate"])
        raw = self.call(host, "source.snapshot", expected_source_revision=2)["descriptor_utf8"]
        self.assertIn("Synthetic updated", raw)
        self.call(host, "document.reload", expected_source_revision=2, expected_revision=5, expected_head=2,
                  operation_id="reload", dirty_policy="reject", approval_id=None)
        history = self.call(host, "document.history", after_revision=0, limit=128)
        self.assertEqual(["patch", "undo", "redo", "savepoint", "reload"], [item["action"] for item in history["entries"]])
        self.assertEqual(6, history["current"]["memory_revision"])

    def test_settings_strict_json_profile_keys_ids_paths_and_flags(self):
        for raw in (b"", b"x" * 32_769, b"\xff", b'{"x":1,"x":2}', b'{"x":NaN}', b"[" * 2000):
            self.path.write_bytes(raw)
            with self.assertRaises(DocumentError):
                settings(self.path)
        for value in ([], {}, {**self.config, "extra": 1}, {**self.config, "profile": "production"}):
            self.path.write_bytes(encode(value))
            with self.assertRaises(DocumentError):
                settings(self.path)
        for field, values in (("actor", ("", "a/b")), ("draft_scope", (None,)), ("source_key", ("",)),
                               ("database_path", (None, "relative.db", "C:/bad\x00name")), ("schema_root", ("relative",)),
                               ("enabled", (1, None)), ("allow_initialize", ("yes",))):
            for value in values:
                self.rewrite(**{field: value})
                with self.assertRaises(DocumentError):
                    settings(self.path)
        self.rewrite()
        with patch.object(Path, "is_symlink", return_value=True), self.assertRaises(DocumentError):
            settings(self.path)
        for path in (Path("relative.json"),):
            with self.assertRaises(DocumentError):
                CLIHost(path)
        with patch.object(Path, "is_symlink", return_value=True), self.assertRaises(DocumentError):
            self.host()

    def test_explicit_limit_vectors_and_integer_boundaries(self):
        for field, ranges in (("editor_limits", ((1, 1_048_576), (1, 10_000), (1, 64))),
                               ("draft_limits", ((1, 128), (1, 67_108_864))), ("store_limits", ((1, 128), (1, 67_108_864))),
                               ("save_limits", ((1, 128), (1, 8_388_608)))):
            for value in (None, [], [1] * (len(ranges) + 1)):
                self.rewrite(**{field: value})
                with self.assertRaises(DocumentError):
                    settings(self.path)
            for index, (minimum, maximum) in enumerate(ranges):
                for value in (True, minimum - 1, maximum + 1):
                    vector = list(self.config[field])
                    vector[index] = value
                    self.rewrite(**{field: vector})
                    with self.assertRaises(DocumentError):
                        settings(self.path)
                for value in (minimum, maximum):
                    vector = list(self.config[field])
                    vector[index] = value
                    self.rewrite(**{field: vector})
                    self.assertEqual(value, settings(self.path)[field][index])
        for field, minimum, maximum in (("max_input_bytes", 1, 65_536), ("max_output_bytes", 8192, 4_194_304)):
            for value in (True, minimum - 1, maximum + 1):
                self.rewrite(**{field: value})
                with self.assertRaises(DocumentError):
                    settings(self.path)
            for value in (minimum, maximum):
                self.rewrite(**{field: value})
                self.assertEqual(value, settings(self.path)[field])

    def test_explicit_actions_and_approval_shapes(self):
        for field in ("draft_actions", "store_actions"):
            for value in (None, [None], ["open", "open"], ["*"]):
                self.rewrite(**{field: value})
                with self.assertRaises(DocumentError):
                    settings(self.path)
            self.rewrite(**{field: []})
            self.assertEqual([], settings(self.path)[field])
        valid = dict(approval_id="approval", command_digest="0" * 64, scope_digest="1" * 64)
        for approvals in (None, [valid] * 129, [None], [{}], [valid, valid], [{**valid, "approval_id": ""}]):
            self.rewrite(discard_approvals=approvals)
            with self.assertRaises(DocumentError):
                settings(self.path)
        for field in ("command_digest", "scope_digest"):
            for value in (None, "0" * 63, "A" * 64):
                self.rewrite(discard_approvals=[{**valid, field: value}])
                with self.assertRaises(DocumentError):
                    settings(self.path)
        approvals = [{**valid, "approval_id": "a" + str(n)} for n in range(128)]
        self.rewrite(discard_approvals=approvals)
        self.assertEqual(128, len(settings(self.path)["discard_approvals"]))

    def test_disabled_or_no_initialize_does_not_create_file(self):
        for changes in (dict(enabled=False), dict(allow_initialize=False)):
            self.rewrite(**changes)
            with self.assertRaises(DocumentError):
                self.host().initialize()
            self.assertFalse(Path(self.config["database_path"]).exists())

    def test_exclusive_initialization_never_overwrites_existing_even_empty_file(self):
        database = Path(self.config["database_path"])
        database.write_bytes(b"")
        with self.assertRaises(FileExistsError):
            self.host().initialize()
        self.assertEqual(b"", database.read_bytes())
        database.write_bytes(b"synthetic existing data")
        with self.assertRaises(FileExistsError):
            self.host().initialize()
        self.assertEqual(b"synthetic existing data", database.read_bytes())

    def test_partial_initialization_not_marked_ready_or_retried_over_file(self):
        self.rewrite(store_actions=["open", "read", "read_candidate"])
        host = self.host()
        with self.assertRaises(DocumentError):
            host.initialize()
        self.assertTrue(host.database.exists())
        self.rewrite()
        with self.assertRaises(sqlite3.OperationalError):
            self.host().attach()
        with self.assertRaises(FileExistsError):
            self.host().initialize()

    def test_initialization_permission_rechecked_before_ready_marker(self):
        host = self.host()
        original = host._open
        def revoked(create):
            result = original(create)
            self.rewrite(allow_initialize=False)
            return result
        with patch.object(host, "_open", revoked), self.assertRaises(DocumentError):
            host.initialize()
        with self.assertRaises(sqlite3.OperationalError):
            self.host().attach()

    def test_live_permission_and_immutable_host_configuration(self):
        host = self.initialized()
        self.rewrite(draft_actions=sorted(DRAFT_ACTIONS - {"patch"}))
        with self.assertRaises(DocumentError):
            self.edit(host)
        self.rewrite(enabled=False)
        with self.assertRaises(DocumentError):
            host.describe()
        self.rewrite(actor="other")
        with self.assertRaises(DocumentError):
            host._policy()
        self.rewrite()
        for scope, store in ((dict(actor="other", scope="draft", action="open"), False),
                             (dict(actor="actor", scope="other", action="open"), False),
                             (dict(actor="actor", scope="store", action="read", source_key="other"), True)):
            self.assertFalse(host._authorize(scope, store))

    def test_policy_revoked_during_actual_mutation_rolls_back(self):
        host = self.initialized()
        original = host._authorize
        count = [0]
        def authorize(scope, store=False):
            result = original(scope, store)
            if not store and scope["action"] == "patch":
                count[0] += 1
                if count[0] == 2:
                    self.rewrite(enabled=False)
            return result
        with patch.object(host, "_authorize", authorize), self.assertRaises(DocumentError):
            self.edit(host)
        self.rewrite()
        self.assertEqual(1, self.call(host, "document.inspect", expected_revision=1)["memory_revision"])

    def test_database_ready_binding_cannot_be_missing_or_changed(self):
        host = self.initialized()
        with closing(sqlite3.connect(host.database)) as db, db:
            db.execute("UPDATE cli_host_meta SET binding=?", (b"{}",))
        with self.assertRaises(DocumentError):
            host.attach()
        with closing(sqlite3.connect(host.database)) as db, db:
            db.execute("DELETE FROM cli_host_meta")
        with self.assertRaises(DocumentError):
            host.attach()

    def test_discard_approval_requires_exact_command_and_current_scope_hash(self):
        host = self.host()
        raw, scope = encode(dict(payload=dict(approval_id="approval"))), dict(actor="actor", current=dict(memory_revision=2))
        self.assertFalse(host._discard(raw, scope))
        self.rewrite(discard_approvals=[dict(approval_id="approval", command_digest=digest(raw), scope_digest=digest(encode(scope)))])
        self.assertTrue(host._discard(raw, scope))
        self.assertFalse(host._discard(raw, dict(actor="other")))
        self.assertFalse(host._discard(encode(dict(payload=dict(approval_id="other"))), scope))

    def test_cli_envelope_never_allows_path_actor_or_caller_session(self):
        host = self.initialized()
        for request in (None, [], {}, dict(tool="x", arguments={}, actor="other"),
                        dict(tool=1, arguments={}), dict(tool="x", arguments=[]),
                        dict(tool="x", arguments=dict(session_id="x" * 32)), dict(tool="unknown", arguments={}),
                        dict(tool="facetwire.document.inspect", arguments=dict(expected_revision=1, path="C:/other"))):
            with self.assertRaises(DocumentError):
                host.call(encode(request))
        for raw in (b"", b"x" * 65_537):
            with self.assertRaises(DocumentError):
                host.call(raw)
        with self.assertRaises(DocumentError):
            parse("{}", 10)

    def test_description_output_budget_is_explicit(self):
        self.rewrite(max_output_bytes=8192)
        host = self.initialized()
        self.assertLessEqual(len(encode(host.describe())), 8192)
        large = draft_definitions()
        large[0]["description"] = "x" * 8192
        with patch("facetwire_tools.cli_host.draft_definitions", return_value=large), self.assertRaises(DocumentError):
            host.describe()

    def test_parse_limit_validation_and_approval_accounts_for_json_string_escaping(self):
        for maximum in (None, True, 0, 4_194_305):
            with self.assertRaises(DocumentError):
                parse(b"{}", maximum)
        host = self.host()
        raw = encode(dict(payload=dict(approval_id="approval", descriptor_utf8='"' * 60_000)))
        scope = dict(actor="actor", current=dict(memory_revision=2))
        self.rewrite(discard_approvals=[dict(approval_id="approval", command_digest=digest(raw), scope_digest=digest(encode(scope)))])
        self.assertTrue(host._discard(raw, scope))

    def run_cli(self, action, raw=b""):
        output = io.BytesIO()
        code = main(["--config", str(self.path), action], stdin=io.BytesIO(raw), stdout=output)
        return code, json.loads(output.getvalue())

    def test_main_three_commands_and_payload_free_error(self):
        self.assertEqual(0, self.run_cli("initialize-synthetic")[0])
        self.assertEqual(15, len(self.run_cli("describe")[1]["tools"]))
        result = self.run_cli("call", encode(dict(tool="facetwire.document.inspect", arguments=dict(expected_revision=1))))
        self.assertEqual(0, result[0])
        self.assertEqual(1, result[1]["memory_revision"])
        code, failure = self.run_cli("call", b"private synthetic malformed content")
        self.assertEqual(1, code)
        self.assertEqual("not_acknowledged", failure["status"])
        self.assertNotIn("private", str(failure))
        self.assertNotIn(str(self.root), str(failure))

    def test_main_default_binary_streams_and_required_arguments(self):
        output = io.BytesIO()
        with patch.object(sys, "argv", ["facetwire-tools", "--config", str(self.path), "initialize-synthetic"]), \
                patch.object(sys, "stdin", SimpleNamespace(buffer=io.BytesIO())), \
                patch.object(sys, "stdout", SimpleNamespace(buffer=output)):
            self.assertEqual(0, main())
        self.assertEqual("initialized_synthetic", json.loads(output.getvalue())["status"])
        with patch.object(sys, "stderr", io.StringIO()), self.assertRaises(SystemExit) as raised:
            main([], stdin=io.BytesIO(), stdout=io.BytesIO())
        self.assertEqual(2, raised.exception.code)

    def test_real_separate_cli_processes_reopen_and_use_only_installed_package_root(self):
        env = dict(os.environ, PYTHONPATH=str(Path(facetwire_tools.__file__).resolve().parents[1]))
        entry = [sys.executable, "-c", "from facetwire_tools.cli import main; raise SystemExit(main())", "--config", str(self.path)]
        first = subprocess.run([*entry, "initialize-synthetic"], env=env, capture_output=True, timeout=30)
        self.assertEqual(0, first.returncode, first.stderr.decode(errors="replace"))
        request = encode(dict(tool="facetwire.document.apply_patch", arguments=dict(operation_id="edit", expected_revision=1,
                              expected_head=1, operations=[dict(target_id="zone", field="text", expected="Synthetic content", value="合成测试")])) )
        second = subprocess.run([*entry, "call"], input=request, env=env, capture_output=True, timeout=30)
        self.assertEqual(0, second.returncode, second.stderr.decode(errors="replace"))
        third = subprocess.run([*entry, "call"], input=encode(dict(tool="facetwire.document.snapshot", arguments=dict(expected_revision=2))),
                               env=env, capture_output=True, timeout=30)
        self.assertEqual(0, third.returncode, third.stderr.decode(errors="replace"))
        self.assertIn("合成测试", json.loads(third.stdout)["descriptor_utf8"])
