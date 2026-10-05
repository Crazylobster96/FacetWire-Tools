# SPDX-License-Identifier: Apache-2.0
"""Two bounded tools for an explicitly attached single-database managed source."""
import json
import secrets

from jsonschema import Draft202012Validator

from .descriptor import DocumentError, bounded_int, encode
from .managed_save import ManagedSaves
from .package_editor import PackageEditor


def definitions(*, package=False):
    op = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$", "maxLength": 128}
    revision = {"type": "integer", "minimum": 1, "maximum": 129}
    inputs = {
        "save": dict(operation_id=op, expected_revision=revision, expected_head=revision,
                     expected_source_revision={"type": "integer", "minimum": 1, "maximum": 127}),
        "save_reconcile": dict(operation_id=op),
    }
    result = []
    for action, properties in inputs.items():
        properties = dict(session_id={"type": "string", "pattern": "^[A-Za-z0-9_-]{32}$", "minLength": 32, "maxLength": 32}, **properties)
        prefix = "facetwire.package." if package else "facetwire.document."
        result.append(dict(name=prefix + action, version="0.1", source_save=action == "save",
                           effect="managed_source_and_draft" if action == "save" else "read_only",
                           cancel="before_dispatch_only", recovery=prefix + "save_reconcile",
                           parameters=dict(type="object", additionalProperties=False, required=list(properties), properties=properties)))
    return result


class ManagedSaveTools:
    def __init__(self, saves, *, max_input_bytes):
        if type(saves) is not ManagedSaves:
            raise DocumentError("exact protected managed saves required")
        self.saves = saves
        self.package = type(saves.journal.editor) is PackageEditor
        self.max_input_bytes = bounded_int(max_input_bytes, 1, 65_536)
        saves._access("save_reconcile")
        self.session_id = secrets.token_urlsafe(24)
        self.schemas = {item["name"]: Draft202012Validator(item["parameters"])
                        for item in definitions(package=self.package)}

    def call(self, name, arguments):
        if type(name) is not str or name not in self.schemas:
            raise DocumentError("managed save tool unavailable")
        raw = encode(arguments)
        if len(raw) > self.max_input_bytes:
            raise DocumentError("managed save tool input budget exceeded")
        args = json.loads(raw)
        try:
            self.schemas[name].validate(args)
        except Exception:
            raise DocumentError("managed save tool arguments invalid") from None
        if not secrets.compare_digest(args.pop("session_id"), self.session_id):
            raise DocumentError("managed save session expired or unavailable")
        if name == ("facetwire.package.save" if self.package else "facetwire.document.save"):
            return dict(status="ok", effect="managed_source_and_draft", source_save=True, **self.saves.save(args))
        return dict(status="ok", effect="read_only", source_save=False, **self.saves.reconcile(args["operation_id"]))
