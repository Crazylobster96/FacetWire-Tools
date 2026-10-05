# SPDX-License-Identifier: Apache-2.0
"""Host-configured local CLI adapter; never a network authorization API."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from .descriptor import DescriptorEditor, DocumentError, bounded_int, encode, _constant, _pairs
from .journal import DraftJournal, digest, identity
from .storage import DescriptorStore
from .managed_save import ManagedSaves
from .tool_api import DocumentTools, definitions as draft_definitions
from .storage_tools import StorageTools, definitions as source_definitions
from .managed_tools import ManagedSaveTools, definitions as save_definitions
from .package_workspace import PackageWorkspace
from .package_editor import PackageEditor
from .package_export import PackageExport
from .package_export_tools import PackageExportTools, definitions as export_definitions
from .extension_content import ExtensionContent


DRAFT_ACTIONS = frozenset(("open", "attach", "inspect", "validate", "snapshot", "targets", "preview_patch", "patch", "undo", "redo",
                           "history", "reconcile", "reload", "savepoint", "save", "save_reconcile"))
STORE_ACTIONS = frozenset(("open", "read", "read_candidate", "candidate", "managed_local"))
POLICY_FIELDS = frozenset(("enabled", "allow_initialize", "draft_actions", "store_actions", "discard_approvals"))
PACKAGE_POLICY_FIELDS = frozenset(("allow_export",))
PACKAGE_PROFILES = frozenset(("managed-package-tools-v1", "managed-package-tools-v2"))


def parse(raw, maximum):
    bounded_int(maximum, 1, 4_194_304)
    if type(raw) is not bytes or not 1 <= len(raw) <= maximum:
        raise DocumentError("bounded UTF-8 host input required")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, RecursionError):
        raise DocumentError("invalid host JSON input") from None


def settings(path):
    with path.open("rb") as stream:
        value = parse(stream.read(32_769), 32_768)
    keys = {"profile", "database_path", "schema_root", "actor", "draft_scope", "store_scope", "source_key",
            "editor_limits", "draft_limits", "store_limits", "save_limits", "max_input_bytes", "max_output_bytes"} | POLICY_FIELDS
    if type(value) is not dict or value.get("profile") not in (
            "synthetic-local-tools-v1", "managed-local-tools-v1", *PACKAGE_PROFILES):
        raise DocumentError("explicit supported CLI host profile required")
    package = value["profile"] in PACKAGE_PROFILES
    if value["profile"] in ("managed-local-tools-v1", *PACKAGE_PROFILES):
        keys.add("document_id")
    if package:
        keys.update({"package_path", "export_root", "package_limits", "max_resource_bytes", "max_generations", "allow_export"})
    if value["profile"] == "managed-package-tools-v2":
        keys.add("extension_schemas")
    if set(value) != keys:
        raise DocumentError("exact CLI host configuration required")
    if value["profile"] in ("managed-local-tools-v1", *PACKAGE_PROFILES):
        identity(value["document_id"])
    for field in ("actor", "draft_scope", "store_scope", "source_key"):
        identity(value[field])
    for field in (("database_path", "schema_root", "package_path", "export_root") if package else ("database_path", "schema_root")):
        if (type(value[field]) is not str or "\x00" in value[field] or
                not Path(value[field]).is_absolute() or Path(value[field]).is_symlink()):
            raise DocumentError("trusted absolute host paths required")
    limits = {"editor_limits": ((1, 1_048_576), (1, 10_000), (1, 64)),
              "draft_limits": ((1, 128), (1, 67_108_864)), "store_limits": ((1, 128), (1, 67_108_864)),
              "save_limits": ((1, 128), (1, 8_388_608))}
    for field, ranges in limits.items():
        values = value[field]
        if type(values) is not list or len(values) != len(ranges):
            raise DocumentError("explicit host limits required")
        for item, (minimum, maximum) in zip(values, ranges):
            bounded_int(item, minimum, maximum)
    if package:
        package_limits=value["package_limits"]
        if type(package_limits) is not list or len(package_limits) != 4:
            raise DocumentError("explicit package limits required")
        for item,(minimum,maximum) in zip(package_limits,((1,4096),(1,67_108_864),(0,16),(1,67_108_864))):
            bounded_int(item,minimum,maximum)
        bounded_int(value["max_resource_bytes"],0,1_048_576)
        bounded_int(value["max_generations"],1,128)
        if type(value["allow_export"]) is not bool:
            raise DocumentError("explicit package export authorization required")
    if value["profile"] == "managed-package-tools-v2":
        extensions = value["extension_schemas"]
        if type(extensions) is not list or not 1 <= len(extensions) <= 16:
            raise DocumentError("explicit bounded extension schemas required")
        for item in extensions:
            if type(item) is not dict or set(item) != {"type", "schema_path", "sha256"}:
                raise DocumentError("exact extension schema pin required")
    bounded_int(value["max_input_bytes"], 1, 65_536)
    bounded_int(value["max_output_bytes"], 8192, 4_194_304)
    for field in ("enabled", "allow_initialize"):
        if type(value[field]) is not bool:
            raise DocumentError("explicit host authorization flags required")
    for field, available in (("draft_actions", DRAFT_ACTIONS), ("store_actions", STORE_ACTIONS)):
        actions = value[field]
        if (type(actions) is not list or any(type(action) is not str for action in actions) or
                len(set(actions)) != len(actions) or not set(actions) <= available):
            raise DocumentError("exact host action list required")
    approvals = value["discard_approvals"]
    if type(approvals) is not list or len(approvals) > 128:
        raise DocumentError("bounded explicit discard approvals required")
    ids = set()
    for approval in approvals:
        if type(approval) is not dict or set(approval) != {"approval_id", "command_digest", "scope_digest"}:
            raise DocumentError("bound discard approval required")
        key = identity(approval["approval_id"])
        if key in ids:
            raise DocumentError("duplicate discard approval")
        ids.add(key)
        for field in ("command_digest", "scope_digest"):
            item = approval[field]
            if type(item) is not str or len(item) != 64 or any(char not in "0123456789abcdef" for char in item):
                raise DocumentError("discard approval SHA-256 required")
    return value


def synthetic_document():
    return {"format": "facetwire.agent-scene-package", "version": "0.1", "id": "synthetic-doc", "title": "Synthetic CLI",
            "resources": [], "canvas": {"id": "canvas", "size": {"width": 640, "height": 480}, "pages": [
                {"id": "page", "size": {"width": 640, "height": 480}, "layers": [{"id": "layer", "z": 1, "zones": [
                    {"id": "zone", "bounds": {"x": 0, "y": 0, "width": 640, "height": 100},
                     "content": {"type": "text", "text": "Synthetic content"}}]}]}]}}


class CLIHost:
    def __init__(self, config_path):
        self.path = Path(config_path)
        if not self.path.is_absolute() or self.path.is_symlink():
            raise DocumentError("trusted absolute CLI config path required")
        self.config = settings(self.path)
        self.binding = encode({key: value for key, value in self.config.items()
                               if key not in POLICY_FIELDS | PACKAGE_POLICY_FIELDS})
        self.database = Path(self.config["database_path"])

    def _policy(self):
        current = settings(self.path)
        if current["profile"] == "managed-package-tools-v2":
            # Profile bytes are protected host inputs, not a caller-selectable
            # tool parameter. A changed file revokes authorization mid-call.
            ExtensionContent.load(current["extension_schemas"])
        binding = encode({key: value for key, value in current.items()
                          if key not in POLICY_FIELDS | PACKAGE_POLICY_FIELDS})
        if binding != self.binding or not current["enabled"]:
            raise DocumentError("host disabled or immutable configuration changed")
        return current

    def _authorize(self, scope, store=False):
        policy = self._policy()
        expected_scope = self.config["store_scope" if store else "draft_scope"]
        return (scope["actor"] == self.config["actor"] and scope["scope"] == expected_scope and
                (not store or scope["source_key"] in (None, self.config["source_key"])) and
                scope["action"] in policy["store_actions" if store else "draft_actions"])

    def _discard(self, raw, scope):
        policy = self._policy()
        command = parse(raw, self.config["editor_limits"][0] * 2 + 8192)
        approval = dict(approval_id=command["payload"]["approval_id"], command_digest=digest(raw), scope_digest=digest(encode(scope)))
        return approval in policy["discard_approvals"]

    def _open(self, create, initial=None):
        c = self.config
        descriptor = DescriptorEditor(Path(c["schema_root"]), max_bytes=c["editor_limits"][0],
                                      max_nodes=c["editor_limits"][1], max_depth=c["editor_limits"][2])
        if c["profile"] in PACKAGE_PROFILES:
            extensions = ExtensionContent.load(c["extension_schemas"]) if c["profile"] == "managed-package-tools-v2" else None
            workspace = PackageWorkspace(Path(c["package_path"]), descriptor,
                                         max_files=c["package_limits"][0], max_total_bytes=c["package_limits"][1],
                                         max_depth=c["package_limits"][2], max_file_bytes=c["package_limits"][3],
                                         extensions=extensions)
            snapshot = workspace.read()
            root = parse(dict(snapshot.files)[snapshot.root_name + ".dis.json"], c["editor_limits"][0])
            if root["id"] != c["document_id"]:
                raise DocumentError("trusted package descriptor ID mismatch")
            editor = PackageEditor(snapshot, descriptor, max_bytes=c["editor_limits"][0],
                                   max_resource_bytes=c["max_resource_bytes"], extensions=extensions)
            seed = editor.initial() if create else None
        else:
            editor = descriptor
            seed = (encode(synthetic_document()) if c["profile"] == "synthetic-local-tools-v1" else initial) if create else None
            if create and (type(seed) is not bytes or editor.parse(seed)["id"] != c.get("document_id", "synthetic-doc")):
                raise DocumentError("exact validated initial descriptor required")
        journal = DraftJournal(self.database, editor, scope=c["draft_scope"], actor=c["actor"], authorize=self._authorize,
                               max_events=c["draft_limits"][0], max_total_bytes=c["draft_limits"][1], create=create,
                               initial=seed)
        store = DescriptorStore(self.database, editor, scope=c["store_scope"], actor=c["actor"],
                                authorize=lambda scope: self._authorize(scope, True),
                                max_events=c["store_limits"][0], max_total_bytes=c["store_limits"][1], create=create)
        if create:
            store.save(seed, actor=c["actor"], source_key=c["source_key"], expected_revision=0,
                        operation_id=("synthetic-initial" if c["profile"] == "synthetic-local-tools-v1" else
                                      "package-initial" if c["profile"] in PACKAGE_PROFILES else "managed-initial"),
                        mode="managed_local")
        saves = ManagedSaves(journal, store, actor=c["actor"], source_key=c["source_key"],
                             max_saves=c["save_limits"][0], max_total_bytes=c["save_limits"][1], create=create)
        return journal, store, saves

    def _initialization_allowed(self):
        if not self._policy()["allow_initialize"]:
            raise DocumentError("host initialization not explicitly allowed")

    def initialize(self):
        self._initialization_allowed()
        if self.config["profile"] != "synthetic-local-tools-v1":
            raise DocumentError("synthetic initialization requires its exact profile")
        return self._initialize()

    def initialize_document(self, raw):
        self._initialization_allowed()
        if self.config["profile"] != "managed-local-tools-v1":
            raise DocumentError("managed initialization requires its exact profile")
        value = parse(raw, self.config["editor_limits"][0])
        editor = DescriptorEditor(Path(self.config["schema_root"]), max_bytes=self.config["editor_limits"][0],
                                  max_nodes=self.config["editor_limits"][1], max_depth=self.config["editor_limits"][2])
        editor.parse(raw)
        if value["id"] != self.config["document_id"]:
            raise DocumentError("trusted initial descriptor ID mismatch")
        return self._initialize(raw)

    def initialize_package(self):
        self._initialization_allowed()
        if self.config["profile"] not in PACKAGE_PROFILES:
            raise DocumentError("package initialization requires its exact profile")
        return self._initialize()

    def _initialize(self, initial=None):
        # Exclusive create never overwrites an existing file, even a zero-byte one.
        with self.database.open("xb"):
            pass
        journal, _, _ = self._open(True) if initial is None else self._open(True, initial)
        with closing(journal._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE cli_host_meta (id INTEGER PRIMARY KEY CHECK(id=1), binding BLOB NOT NULL)")
            db.execute("INSERT INTO cli_host_meta VALUES (1,?)", (self.binding,))
            self._initialization_allowed()
            db.commit()
        return dict(status=("initialized_package" if self.config["profile"] in PACKAGE_PROFILES else
                            "initialized_synthetic" if initial is None else "initialized_managed"),
                    source_revision=1, memory_revision=1)

    def attach(self):
        self._policy()
        with closing(sqlite3.connect(self.database.as_uri() + "?mode=rw", uri=True)) as db:
            row = db.execute("SELECT binding FROM cli_host_meta WHERE id=1").fetchone()
            if row is None or row[0] != self.binding:
                raise DocumentError("CLI database binding unavailable")
        journal, store, saves = self._open(False)
        c = self.config
        package = c["profile"] in PACKAGE_PROFILES
        document_tools = DocumentTools(journal, actor=c["actor"], max_input_bytes=c["max_input_bytes"], max_output_bytes=c["max_output_bytes"] - 512)
        source_tools = StorageTools(journal, store, actor=c["actor"], source_key=c["source_key"], approve_discard=self._discard,
                                    max_input_bytes=c["max_input_bytes"], max_output_bytes=c["max_output_bytes"] - 512)
        save_tools = ManagedSaveTools(saves, max_input_bytes=c["max_input_bytes"])
        self.routes, self.definitions = {}, []
        groups = [(document_tools, draft_definitions(package=package,
                    extension_fields=package and journal.editor.extensions is not None)),
                  (source_tools, source_definitions(package=package)),
                  (save_tools, save_definitions(package=package))]
        if package:
            descriptor = journal.editor.descriptor_editor
            workspace = PackageWorkspace(Path(c["package_path"]), descriptor,
                                         max_files=c["package_limits"][0], max_total_bytes=c["package_limits"][1],
                                         max_depth=c["package_limits"][2], max_file_bytes=c["package_limits"][3],
                                         extensions=journal.editor.extensions)
            exporter = PackageExport(workspace, store, actor=c["actor"], source_key=c["source_key"],
                                     export_root=Path(c["export_root"]), max_generations=c["max_generations"],
                                     authorize_write=lambda: self._policy()["allow_export"])
            visible=[item for item in export_definitions() if self._policy()["allow_export"] or
                     item["name"] == "facetwire.package.export_reconcile"]
            groups.append((PackageExportTools(exporter,max_input_bytes=c["max_input_bytes"]),visible))
        for group, items in groups:
            for item in items:
                self.routes[item["name"]] = group
                item["parameters"]["properties"].pop("session_id")
                item["parameters"]["required"].remove("session_id")
                self.definitions.append(item)

    def describe(self):
        self.attach()
        result = dict(status="ok", profile=self.config["profile"], tools=self.definitions)
        if len(encode(result)) > self.config["max_output_bytes"]:
            raise DocumentError("CLI description exceeds output budget")
        return result

    def call(self, raw):
        request = parse(raw, self.config["max_input_bytes"])
        if type(request) is not dict or set(request) != {"tool", "arguments"}:
            raise DocumentError("exact CLI tool envelope required")
        if type(request["tool"]) is not str or type(request["arguments"]) is not dict or "session_id" in request["arguments"]:
            raise DocumentError("CLI owns session binding")
        self.attach()
        group = self.routes.get(request["tool"])
        if group is None:
            raise DocumentError("CLI tool unavailable")
        return group.call(request["tool"], dict(session_id=group.session_id, **request["arguments"]))
