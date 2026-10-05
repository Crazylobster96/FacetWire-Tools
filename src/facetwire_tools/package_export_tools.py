# SPDX-License-Identifier: Apache-2.0
"""Host-bound AI Tools for an immutable saved-package generation."""
import json
import secrets

from jsonschema import Draft202012Validator

from .descriptor import DocumentError, bounded_int, encode
from .package_export import PackageExport


def definitions():
    identity = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$", "maxLength": 128}
    digest = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    properties = {"session_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{32}$", "minLength": 32,
                                 "maxLength": 32}, "operation_id": identity,
                  "expected_source_revision": {"type": "integer", "minimum": 1, "maximum": 128},
                  "expected_source_digest": digest, "expected_workset_digest": digest}
    return [dict(name="facetwire.package." + action, version="0.1",
                 effect="immutable_package_generation" if action == "export" else "read_only",
                 original_overwrite=False, cancel="before_dispatch_only",
                 recovery="facetwire.package.export_reconcile",
                 parameters=dict(type="object", additionalProperties=False, required=list(properties),
                                 properties=json.loads(json.dumps(properties))))
            for action in ("export", "export_reconcile")]


class PackageExportTools:
    def __init__(self, exporter, *, max_input_bytes):
        if type(exporter) is not PackageExport:
            raise DocumentError("exact trusted package exporter required")
        self.exporter = exporter
        self.max_input_bytes = bounded_int(max_input_bytes, 1, 65536)
        exporter._check_root()
        self.session_id = secrets.token_urlsafe(24)
        self.schemas = {item["name"]: Draft202012Validator(item["parameters"]) for item in definitions()}

    def call(self, name, arguments):
        if type(name) is not str or name not in self.schemas:
            raise DocumentError("package export tool unavailable")
        raw = encode(arguments)
        if len(raw) > self.max_input_bytes:
            raise DocumentError("package export tool input budget exceeded")
        args = json.loads(raw)
        try:
            self.schemas[name].validate(args)
        except Exception:
            raise DocumentError("package export tool arguments invalid") from None
        if not secrets.compare_digest(args.pop("session_id"), self.session_id):
            raise DocumentError("package export session expired or unavailable")
        action = name.removeprefix("facetwire.package.")
        result = self.exporter.export(**args) if action == "export" else self.exporter.reconcile(**args)
        return dict(status="ok", effect="immutable_package_generation" if action == "export" else "read_only",
                    original_overwrite=False, **result)
