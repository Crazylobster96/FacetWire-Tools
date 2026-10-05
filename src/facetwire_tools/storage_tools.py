# SPDX-License-Identifier: Apache-2.0
"""Explicit source read/reload tools. Host binds storage, identity and discard approval."""
import json
import secrets

from jsonschema import Draft202012Validator

from .descriptor import DocumentError, bounded_int, encode
from .journal import DraftJournal, identity
from .package_editor import PackageEditor
from .storage import DescriptorStore


def definitions(*, package=False):
    identity_schema = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$", "maxLength": 128}
    version = {"type": "integer", "minimum": 1, "maximum": 128}
    draft_version = {"type": "integer", "minimum": 1, "maximum": 129}
    source = "facetwire.package_source." if package else "facetwire.source."
    document = "facetwire.package." if package else "facetwire.document."
    inputs = {
        source + "inspect": {"expected_source_revision": version},
        source + "snapshot": {"expected_source_revision": version},
        source + "reconcile": {"operation_id": identity_schema},
        document + "reload": {
            "expected_source_revision": version, "expected_revision": draft_version, "expected_head": draft_version,
            "operation_id": identity_schema, "dirty_policy": {"enum": ["reject", "discard"]},
            "approval_id": {"anyOf": [identity_schema, {"type": "null"}]}},
    }
    result = []
    for name, properties in inputs.items():
        properties = {"session_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{32}$", "minLength": 32, "maxLength": 32}, **properties}
        result.append(dict(name=name, version="0.1", source_save=False,
                           effect="durable_draft_reload" if name == document + "reload" else "read_only",
                           cancel="before_dispatch_only", recovery="reconcile_operation_id",
                           parameters=dict(type="object", additionalProperties=False, required=list(properties), properties=properties)))
    return result


class StorageTools:
    """Opaque session is not authorization; paths, actor and source key are host-only."""
    def __init__(self, journal, store, *, actor, source_key, approve_discard, max_input_bytes, max_output_bytes):
        if type(journal) is not DraftJournal or type(store) is not DescriptorStore or not callable(approve_discard):
            raise DocumentError("typed draft/store and explicit discard verifier required")
        if journal.editor.schema_digest != store.editor.schema_digest or journal.editor.limits != store.editor.limits:
            raise DocumentError("draft/store descriptor profile mismatch")
        self.journal, self.store = journal, store
        self.actor, self.source_key = identity(actor), identity(source_key)
        self.package = type(journal.editor) is PackageEditor
        self.approve_discard = approve_discard
        self.limits = (bounded_int(max_input_bytes, 1, 1_048_576), bounded_int(max_output_bytes, 4096, 4_194_304))
        self._access("attach")
        self.session_id = secrets.token_urlsafe(24)
        self.schemas = {item["name"]: Draft202012Validator(item["parameters"])
                        for item in definitions(package=self.package)}

    def _access(self, action):
        self.journal._access(self.actor, action)
        self.store._access(self.actor, "read", self.source_key)

    def _read(self, revision):
        return self.store.read(actor=self.actor, source_key=self.source_key, expected_revision=revision,
                               max_bytes=self.journal.editor.limits[0])

    def _verify(self, raw, context):
        payload = json.loads(raw)["payload"]
        source = payload["source"]
        actual = self._read(source["revision"])
        return (source == dict(scope=self.store.scope, source_key=self.source_key,
                               revision=actual["revision"], digest=actual["digest"]) and
                payload["descriptor_utf8"].encode("utf-8") == actual["descriptor"])

    def call(self, name, arguments):
        if type(name) is not str or name not in self.schemas:
            raise DocumentError("storage tool capability unavailable")
        raw = encode(arguments)
        if len(raw) > self.limits[0]:
            raise DocumentError("storage tool input budget exceeded")
        args = json.loads(raw)
        try:
            self.schemas[name].validate(args)
        except Exception:
            raise DocumentError("storage tool arguments invalid") from None
        if not secrets.compare_digest(args["session_id"], self.session_id):
            raise DocumentError("storage session expired or unavailable")
        action = name.rsplit(".", 1)[1]
        self._access(action)
        if action == "reconcile":
            value = self.store.reconcile(actor=self.actor, source_key=self.source_key, operation_id=args["operation_id"],
                                          max_bytes=self.journal.editor.limits[0])
            result = {**value["receipt"], "descriptor_utf8": value["descriptor"].decode("utf-8")}
        else:
            value = self._read(args["expected_source_revision"])
            if action == "reload":
                command = dict(operation_id=args["operation_id"], actor=self.actor, action="reload",
                               revision=args["expected_revision"], head=args["expected_head"],
                               payload=dict(descriptor_utf8=value["descriptor"].decode("utf-8"),
                                            source=dict(scope=self.store.scope, source_key=self.source_key,
                                                        revision=value["revision"], digest=value["digest"]),
                                            dirty_policy=args["dirty_policy"], approval_id=args["approval_id"]))
                receipt = self.journal.reload(command, verify_source=self._verify, approve_discard=self.approve_discard)
                return dict(status="ok", effect="durable_draft_reload", source_save=False, **receipt)
            result = {key: item for key, item in value.items() if key != "descriptor"}
            if action == "snapshot":
                result["descriptor_utf8"] = value["descriptor"].decode("utf-8")
            else:
                result["bytes"] = len(value["descriptor"])
        response = dict(status="ok", effect="read_only", source_save=False, **result)
        if len(encode(response)) > self.limits[1]:
            raise DocumentError("storage tool output budget exceeded")
        self._access(action)
        return response
