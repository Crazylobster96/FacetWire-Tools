# SPDX-License-Identifier: Apache-2.0
"""Bounded, read-only FacetWire package inventory for trusted host adapters.

This is not an editing session or a save. It pins descriptor/resource bytes so
subsequent workset mutations can begin from a complete, verified source.
"""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import unicodedata

from .descriptor import DescriptorEditor, DocumentError, _constant, _pairs, bounded_int, encode
from .extension_content import ExtensionContent


def _relative(value):
    if (type(value) is not str or not value or "\\" in value or "\x00" in value or ":" in value
            or "//" in value or unicodedata.normalize("NFC", value) != value):
        raise DocumentError("portable package path required")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in (".", "..") for part in value.split("/")):
        raise DocumentError("package path leaves its declared scope")
    return path


@dataclass(frozen=True)
class PackageSnapshot:
    root_name: str
    files: tuple[tuple[str, bytes], ...]
    descriptor_paths: tuple[str, ...]
    extension_digest: str = ""

    @property
    def digest(self):
        return hashlib.sha256(encode([(path, hashlib.sha256(raw).hexdigest()) for path, raw in self.files])).hexdigest()

    def summary(self):
        return dict(root_name=self.root_name, digest=self.digest, file_count=len(self.files),
                    descriptor_count=len(self.descriptor_paths), total_bytes=sum(len(raw) for _, raw in self.files),
                    descriptors=list(self.descriptor_paths))


class PackageWorkspace:
    def __init__(self, root, editor, *, max_files, max_total_bytes, max_depth, max_file_bytes, extensions=None):
        root = Path(root)
        if (type(editor) is not DescriptorEditor or not root.is_absolute() or root.is_symlink()
                or not root.is_dir() or root.resolve() != root or not root.name.endswith(".agscene")):
            raise DocumentError("trusted resolved FacetWire package directory required")
        self.root, self.editor = root, editor
        if extensions is not None and type(extensions) is not ExtensionContent:
            raise DocumentError("exact installed extension profiles required")
        self.extensions = extensions
        self.limits = (bounded_int(max_files, 1, 4096), bounded_int(max_total_bytes, 1, 67108864),
                       bounded_int(max_depth, 0, 16), bounded_int(max_file_bytes, 1, 67108864))

    def _read(self, path):
        relative = _relative(path)
        target = self.root.joinpath(*relative.parts)
        resolved = target.resolve()
        if not resolved.is_relative_to(self.root) or not target.is_file():
            raise DocumentError("referenced package file unavailable")
        member = target
        while member != self.root:
            if member.is_symlink() or (hasattr(member, "is_junction") and member.is_junction()):
                raise DocumentError("package links are not accepted")
            member = member.parent
        size = target.stat().st_size
        if not 0 <= size <= self.limits[3]:
            raise DocumentError("package file capacity exceeded")
        with target.open("rb") as stream:
            raw = stream.read(self.limits[3] + 1)
        if len(raw) != size or not 0 <= len(raw) <= self.limits[3] or target.resolve() != resolved:
            raise DocumentError("package file changed during read")
        return raw

    def read(self):
        root_path = self.root.name + ".dis.json"
        files = {}; descriptors = []; active = set(); seen = set(); total = 0

        def capture(path):
            nonlocal total
            if path not in files:
                raw = self._read(path)
                total += len(raw)
                if len(files) >= self.limits[0] or total > self.limits[1]:
                    raise DocumentError("package workset capacity exceeded")
                files[path] = raw
            return files[path]

        def descriptor(path, depth):
            bounded_int(depth, 0, self.limits[2])
            if path in active:
                raise DocumentError("recursive package document cycle")
            if path in seen:
                return
            active.add(path)
            raw = capture(path)
            try:
                value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
                self.editor._depth(value)
                self.editor.base.validate(value)
                self.editor._objects(value)
            except Exception:
                raise DocumentError("package descriptor validation failed") from None
            resources = {}
            base = PurePosixPath(path).parent
            for resource in value["resources"]:
                key = resource["id"]
                if key in resources:
                    raise DocumentError("duplicate package resource identity")
                relative = (base / _relative(resource["source"])).as_posix()
                resources[key] = resource
                capture(relative)
            for page in value["canvas"]["pages"]:
                for layer in page["layers"]:
                    for zone in layer["zones"]:
                        content = zone["content"]
                        if content["type"] == "text":
                            try:
                                self.editor.content.validate(content)
                            except Exception:
                                raise DocumentError("package text content validation failed") from None
                            font = content.get("style", {}).get("fontResource")
                            if font is not None and (font not in resources or not resources[font]["mediaType"].startswith("font/")):
                                raise DocumentError("text font resource unavailable or incompatible")
                        elif content["type"] == "document":
                            child = (base / _relative(content["source"])).as_posix()
                            nested = PurePosixPath(child)
                            if (not nested.parent.name.endswith(".agscene") or
                                    nested.name != nested.parent.name + ".dis.json"):
                                raise DocumentError("nested document descriptor path required")
                            descriptor(child, depth + 1)
                        elif content["type"] in ("image", "animated-image", "video", "audio"):
                            try:
                                self.editor.content.validate(content)
                            except Exception:
                                raise DocumentError("package media content validation failed") from None
                            kind = "image" if content["type"] == "animated-image" else content["type"]
                            ref = resources.get(content["resource"])
                            if ref is None or not ref["mediaType"].startswith(kind + "/"):
                                raise DocumentError("content resource unavailable or incompatible")
                            for optional in ("posterResource", "artworkResource"):
                                if optional in content:
                                    image = resources.get(content[optional])
                                    if image is None or not image["mediaType"].startswith("image/"):
                                        raise DocumentError("media image resource unavailable or incompatible")
                            for track in content.get("tracks", []):
                                caption = resources.get(track["resource"])
                                if caption is None or not caption["mediaType"].startswith("text/"):
                                    raise DocumentError("timed text resource unavailable or incompatible")
                        elif content["type"] != "placeholder":
                            if self.extensions is None:
                                raise DocumentError("unsupported package content requires a negotiated profile")
                            self.extensions.validate(content)
            active.remove(path)
            seen.add(path)
            descriptors.append(path)

        descriptor(root_path, 0)
        for path, raw in files.items():
            if self._read(path) != raw:
                raise DocumentError("package workset changed during inventory")
        return PackageSnapshot(self.root.name, tuple(sorted(files.items())), tuple(sorted(descriptors)),
                               "" if self.extensions is None else self.extensions.digest)

    def targets(self, target_id, *, max_matches):
        """Locate every displayed instance of one object, never guess an edit scope."""
        if type(target_id) is not str or not 1 <= len(target_id) <= 512:
            raise DocumentError("exact bounded package target identity required")
        bounded_int(max_matches, 1, 128)
        snapshot = self.read()
        files = dict(snapshot.files)
        matches = []

        def visit(path, instance_path):
            value = json.loads(files[path].decode("utf-8"))
            for obj, kind in self.editor._objects(value):
                if obj["id"] == target_id:
                    if len(matches) >= max_matches:
                        raise DocumentError("package target remains ambiguous beyond read capacity")
                    matches.append(dict(descriptor_path=path, descriptor_digest=hashlib.sha256(files[path]).hexdigest(),
                                        object_id=target_id, kind=kind, instance_path=list(instance_path),
                                        instance_key=hashlib.sha256(encode([path, list(instance_path)])).hexdigest()))
            base = PurePosixPath(path).parent
            for page in value["canvas"]["pages"]:
                for layer in page["layers"]:
                    for zone in layer["zones"]:
                        content = zone["content"]
                        if content["type"] == "document":
                            visit((base / _relative(content["source"])).as_posix(), instance_path + (zone["id"],))

        visit(self.root.name + ".dis.json", ())
        return dict(snapshot_digest=snapshot.digest,
                    status="not_found" if not matches else "unique" if len(matches) == 1 else "ambiguous",
                    targets=matches)
