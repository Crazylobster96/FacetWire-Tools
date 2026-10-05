# SPDX-License-Identifier: Apache-2.0
"""Bounded package workset profile for the existing durable journal.

Path bindings and references remain fixed. Resource and descriptor edits affect
every visible instance of a definition; the caller must state that scope.
"""
import base64
import binascii
import hashlib
import json
from pathlib import PurePosixPath

from .descriptor import DescriptorEditor, DocumentError, _constant, _pairs, bounded_int, encode
from .package_workspace import PackageSnapshot, _relative
from .extension_content import ExtensionContent


def _drop(obj, path):
    branch = obj
    parents = []
    for key in path[:-1]:
        if type(branch) is not dict or key not in branch or type(branch[key]) is not dict:
            return
        parents.append((branch, key))
        branch = branch[key]
    branch.pop(path[-1], None)
    for parent, key in reversed(parents):
        if parent[key]:
            break
        del parent[key]


class PackageEditor:
    def __init__(self, snapshot, descriptor_editor, *, max_bytes, max_resource_bytes, extensions=None):
        if type(snapshot) is not PackageSnapshot or type(descriptor_editor) is not DescriptorEditor:
            raise DocumentError("verified package snapshot and external descriptor editor required")
        if ((extensions is not None and type(extensions) is not ExtensionContent)
                or snapshot.extension_digest != ("" if extensions is None else extensions.digest)):
            raise DocumentError("package extension validation profile changed")
        self.limits = (bounded_int(max_bytes, 1, 1048576), descriptor_editor.limits[1], descriptor_editor.limits[2])
        self.max_resource_bytes = bounded_int(max_resource_bytes, 0, 1048576)
        self.snapshot, self.descriptor_editor, self.extensions = snapshot, descriptor_editor, extensions
        self.paths = tuple(path for path, _ in snapshot.files)
        self.resource_paths = frozenset(self.paths) - frozenset(snapshot.descriptor_paths)
        self.fixed = {path: self._fixed(self._descriptor(raw)) for path, raw in snapshot.files
                      if path in snapshot.descriptor_paths}
        self.schema_digest = hashlib.sha256(encode(dict(profile="facetwire.package-draft.v1",
            schema=descriptor_editor.schema_digest, source=snapshot.digest, extensions=snapshot.extension_digest,
            limits=list(self.limits),
            max_resource_bytes=self.max_resource_bytes))).hexdigest()
        self.parse(self.initial())

    @staticmethod
    def _bytes(value):
        if type(value) is not str:
            raise DocumentError("canonical base64 resource bytes required")
        try:
            raw = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error):
            raise DocumentError("canonical base64 resource bytes required") from None
        if base64.b64encode(raw).decode("ascii") != value:
            raise DocumentError("canonical base64 resource bytes required")
        return raw

    def initial(self):
        return encode(dict(profile="facetwire.package-draft.v1", id=self.snapshot.root_name,
            files=[dict(path=path, base64=base64.b64encode(raw).decode("ascii")) for path, raw in self.snapshot.files]))

    def _descriptor(self, raw):
        if len(raw) > self.descriptor_editor.limits[0]:
            raise DocumentError("package descriptor capacity exceeded")
        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
            self.descriptor_editor._depth(value)
            self.descriptor_editor.base.validate(value)
            for obj, kind in self.descriptor_editor._objects(value):
                if kind == "zone" and obj["content"]["type"] in ("text", "image", "animated-image", "video", "audio"):
                    self.descriptor_editor.content.validate(obj["content"])
                elif kind == "zone" and obj["content"]["type"] not in ("placeholder", "document"):
                    if self.extensions is None:
                        raise DocumentError("extension Renderer content profile not installed")
                    self.extensions.validate(obj["content"])
        except Exception:
            raise DocumentError("package descriptor validation failed") from None
        return value

    def _fixed(self, value):
        value = json.loads(encode(value))
        for obj, kind in self.descriptor_editor._objects(value):
            paths = {"document": (("title",),), "canvas": (("size",),), "page": (("size",),),
                     "layer": (("z",), ("transform", "rotationQuarterTurns")),
                     "zone": (("bounds",),)}[kind]
            if kind == "zone" and obj["content"]["type"] == "text":
                paths += (("content", "text"), ("content", "style", "color"),
                          ("content", "selectable"), ("content", "opacity"))
            for path in paths:
                _drop(obj, path)
        return encode(value)

    def parse(self, raw):
        if type(raw) is not bytes or not 1 <= len(raw) <= self.limits[0]:
            raise DocumentError("bounded package draft bytes required")
        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
            if (type(value) is not dict or set(value) != {"profile", "id", "files"}
                    or value["profile"] != "facetwire.package-draft.v1"
                    or value["id"] != self.snapshot.root_name or type(value["files"]) is not list
                    or len(value["files"]) != len(self.paths) or encode(value) != raw):
                raise DocumentError("package draft structure changed")
            for path, item in zip(self.paths, value["files"]):
                if type(item) is not dict or set(item) != {"path", "base64"} or item["path"] != path:
                    raise DocumentError("package draft file binding changed")
                current = self._bytes(item["base64"])
                if path in self.resource_paths:
                    if len(current) > self.max_resource_bytes:
                        raise DocumentError("resource capacity exceeded")
                elif self._fixed(self._descriptor(current)) != self.fixed[path]:
                    raise DocumentError("package descriptor reference or unsupported field changed")
        except (UnicodeError, ValueError, TypeError, KeyError, RecursionError):
            raise DocumentError("package draft validation failed") from None
        return value

    def inspect(self, raw):
        value = self.parse(raw)
        files = {item["path"]: self._bytes(item["base64"]) for item in value["files"]}
        return dict(profile=value["profile"], schema_digest=self.schema_digest,
                    document_digest=hashlib.sha256(raw).hexdigest(),
                    workset_digest=hashlib.sha256(encode([(path, hashlib.sha256(files[path]).hexdigest())
                        for path in self.paths])).hexdigest(),
                    descriptor_count=len(self.snapshot.descriptor_paths), resource_count=len(self.resource_paths))

    def targets(self, raw, target_id, *, max_matches):
        if type(target_id) is not str or not 1 <= len(target_id) <= 512:
            raise DocumentError("exact package target identity required")
        bounded_int(max_matches, 1, 128)
        workset = self.parse(raw)
        files = {item["path"]: self._bytes(item["base64"]) for item in workset["files"]}
        matches = []

        def visit(path, instance_path):
            descriptor = files[path]
            value = self._descriptor(descriptor)
            for obj, kind in self.descriptor_editor._objects(value):
                if obj["id"] == target_id:
                    if len(matches) >= max_matches:
                        raise DocumentError("package target remains ambiguous beyond read capacity")
                    matches.append(dict(descriptor_path=path, descriptor_digest=hashlib.sha256(descriptor).hexdigest(),
                                        object_id=target_id, kind=kind, instance_path=list(instance_path),
                                        instance_key=hashlib.sha256(encode([path, list(instance_path)])).hexdigest()))
            base = PurePosixPath(path).parent
            for page in value["canvas"]["pages"]:
                for layer in page["layers"]:
                    for zone in layer["zones"]:
                        content = zone["content"]
                        if content["type"] == "document":
                            visit((base / _relative(content["source"])).as_posix(), instance_path + (zone["id"],))

        visit(self.snapshot.root_name + ".dis.json", ())
        return dict(status="not_found" if not matches else "unique" if len(matches) == 1 else "ambiguous",
                    workset_digest=self.inspect(raw)["workset_digest"], targets=matches)

    def prepare_patch(self, raw, operations):
        if type(operations) is not tuple:
            raise DocumentError("immutable package operations required")
        bounded_int(len(operations), 1, 128)
        try:
            requested = json.loads(encode(list(operations)), object_pairs_hook=_pairs)
        except Exception:
            raise DocumentError("invalid package patch operations") from None
        value = self.parse(raw)
        files = {item["path"]: item for item in value["files"]}
        for operation in requested:
            if type(operation) is not dict or operation.get("scope") != "definition_all_instances":
                raise DocumentError("explicit all-instance package scope required")
            if set(operation) == {"path", "expected_digest", "base64", "scope"}:
                if type(operation["path"]) is not str or operation["path"] not in self.resource_paths or type(operation["expected_digest"]) is not str:
                    raise DocumentError("exact scoped resource patch required")
                item = files[operation["path"]]
                before = self._bytes(item["base64"])
                if hashlib.sha256(before).hexdigest() != operation["expected_digest"]:
                    raise DocumentError("resource patch precondition conflict")
                replacement = self._bytes(operation["base64"])
                if len(replacement) > self.max_resource_bytes:
                    raise DocumentError("resource replacement capacity exceeded")
                item["base64"] = base64.b64encode(replacement).decode("ascii")
            elif set(operation) == {"descriptor_path", "expected_descriptor_digest", "target_id", "field", "expected", "value", "scope"}:
                path = operation["descriptor_path"]
                if type(path) is not str or path not in self.fixed or type(operation["expected_descriptor_digest"]) is not str:
                    raise DocumentError("exact scoped descriptor path required")
                item = files[path]
                before = self._bytes(item["base64"])
                if hashlib.sha256(before).hexdigest() != operation["expected_descriptor_digest"]:
                    raise DocumentError("descriptor patch precondition conflict")
                document = self._descriptor(before)
                self.descriptor_editor._apply_ops(document, (dict(target_id=operation["target_id"], field=operation["field"],
                                                           expected=operation["expected"], value=operation["value"]),))
                updated = encode(document)
                item["base64"] = base64.b64encode(updated).decode("ascii")
            else:
                raise DocumentError("exact package patch operation required")
        result = encode(value)
        self.parse(result)
        return result
