# SPDX-License-Identifier: Apache-2.0
"""Transport-neutral, separately described tools for one host-authenticated draft session."""
import json
import secrets

from jsonschema import Draft202012Validator

from .descriptor import DocumentError, bounded_int, encode
from .journal import DraftJournal, identity


def definitions():
    """Fresh generic JSON Schema descriptions, not provider-specific registration or permission."""
    string_id = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$", "maxLength": 128}
    revision = {"type": "integer", "minimum": 1, "maximum": 129}
    patch_item = {"type": "object", "additionalProperties": False,
                  "required": ["target_id", "field", "expected", "value"],
                  "properties": {"target_id": {"type": "string", "minLength": 1, "maxLength": 256},
                                 "field": {"enum": ["title", "size", "z", "rotation", "bounds", "text", "color", "selectable", "opacity"]},
                                 "expected": {}, "value": {}}}
    versioned = {"expected_revision": revision}
    mutation = {**versioned, "operation_id": string_id, "expected_head": revision}
    inputs = {
        "inspect": versioned, "validate": versioned, "snapshot": versioned,
        "apply_patch": {**mutation, "operations": {"type": "array", "minItems": 1, "maxItems": 128, "items": patch_item}},
        "preview_patch": {**versioned, "expected_head": revision,
                          "operations": {"type": "array", "minItems": 1, "maxItems": 128, "items": patch_item}},
        "undo": {**mutation, "count": {"type": "integer", "minimum": 1, "maximum": 128}},
        "redo": {**mutation, "count": {"type": "integer", "minimum": 1, "maximum": 128}},
        "history": {"after_revision": {"type": "integer", "minimum": 0, "maximum": 129},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 128}},
        "reconcile": {"operation_id": string_id},
    }
    result = []
    for name, properties in inputs.items():
        properties = {"session_id": {"type": "string", "minLength": 32, "maxLength": 32,
                                     "pattern": "^[A-Za-z0-9_-]{32}$"}, **properties}
        result.append({"name": "facetwire.document." + name, "version": "0.1",
                       "effect": "durable_draft_only" if name in ("apply_patch", "undo", "redo") else "read_only",
                       "source_save": False, "cancel": "before_dispatch_only", "recovery": "reconcile_operation_id",
                       "parameters": {"type": "object", "additionalProperties": False,
                                      "required": list(properties), "properties": properties}})
    return result


class DocumentTools:
    """Trusted host attaches/reopens a journal. Caller never supplies paths, actor, scope or authorization."""
    def __init__(self, journal, *, actor, max_input_bytes, max_output_bytes):
        if type(journal) is not DraftJournal:
            raise DocumentError("exact protected draft journal required")
        self.journal, self.actor = journal, identity(actor)
        self.limits = (bounded_int(max_input_bytes, 1, 1048576), bounded_int(max_output_bytes, 4096, 4194304))
        journal._access(actor, "attach")
        self.session_id = secrets.token_urlsafe(24)
        self.schemas = {item["name"]: Draft202012Validator(item["parameters"]) for item in definitions()}

    def call(self, name, arguments):
        if type(name) is not str or name not in self.schemas:
            raise DocumentError("tool capability unavailable")
        raw = encode(arguments)
        if len(raw) > self.limits[0]:
            raise DocumentError("tool input budget exceeded")
        args = json.loads(raw)
        try:
            self.schemas[name].validate(args)
        except Exception:
            raise DocumentError("tool arguments invalid") from None
        if not secrets.compare_digest(args["session_id"], self.session_id):
            raise DocumentError("session expired or unavailable")
        action = name.removeprefix("facetwire.document.")
        self.journal._access(self.actor, "patch" if action == "apply_patch" else action)
        if action in ("apply_patch", "undo", "redo"):
            payload = args["operations"] if action == "apply_patch" else args["count"]
            result = self.journal.apply({"operation_id": args["operation_id"], "actor": self.actor,
                                         "action": "patch" if action == "apply_patch" else action,
                                         "revision": args["expected_revision"], "head": args["expected_head"], "payload": payload})
            # Fixed-size receipt (<4096 bytes by construction) follows durable commit; never a source save.
            return {"status": "ok", "effect": "durable_draft_only", **result}
        if action == "history":
            result = self.journal.history(actor=self.actor, after_revision=args["after_revision"], limit=args["limit"])
        elif action == "reconcile":
            result = self.journal.reconcile(actor=self.actor, operation_id=args["operation_id"])
        else:
            snap = self.journal.snapshot(actor=self.actor, expected_revision=args["expected_revision"],
                                         max_bytes=self.journal.editor.limits[0])
            descriptor = snap.pop("descriptor")
            if action == "preview_patch":
                if args["expected_head"] != snap["history_head"]:
                    raise DocumentError("preview history head conflict")
                candidate = self.journal.editor.prepare_patch(descriptor, tuple(args["operations"]))
                result = {**snap, "candidate": self.journal.editor.inspect(candidate), "persisted": False}
            elif action == "snapshot":
                result = {**snap, "descriptor_utf8": descriptor.decode("utf-8")}
            else:
                inspected = self.journal.editor.inspect(descriptor)
                if action == "validate":
                    inspected.pop("objects")
                result = {**snap, **inspected}
        response = {"status": "ok", "effect": "read_only", **result}
        if len(encode(response)) > self.limits[1]:
            raise DocumentError("tool output budget exceeded")
        self.journal._access(self.actor, action)
        return response
