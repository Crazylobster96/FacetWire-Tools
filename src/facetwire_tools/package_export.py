# SPDX-License-Identifier: Apache-2.0
"""Create a new immutable FacetWire package generation from a saved workset.

The original source directory is never overwritten. A missing reply is resolved
against the exact generation name and its fully re-read content, not by retrying.
"""
import base64
import os
from pathlib import Path
import re
import shutil
import tempfile

from .descriptor import DocumentError, bounded_int, encode
from .journal import identity
from .package_editor import PackageEditor
from .package_workspace import PackageWorkspace, _relative
from .storage import DescriptorStore


def _sha(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise DocumentError("exact SHA-256 required")
    return value


class PackageExport:
    def __init__(self, workspace, store, *, actor, source_key, export_root, max_generations,
                 authorize_write=None):
        root = Path(export_root)
        if (type(workspace) is not PackageWorkspace or type(store) is not DescriptorStore
                or type(store.editor) is not PackageEditor
                or store.editor.snapshot.digest != workspace.read().digest
                or store.editor.descriptor_editor.schema_digest != workspace.editor.schema_digest
                or not root.is_absolute() or not root.is_dir() or root.is_symlink() or root.resolve() != root):
            raise DocumentError("trusted package source, saved workset and export directory required")
        name = store.editor.snapshot.root_name
        if not name.endswith(".agscene") or name in (".agscene", "..agscene") or Path(name).name != name:
            raise DocumentError("portable package root name required")
        self.workspace, self.store, self.root = workspace, store, root
        self.actor, self.source_key = identity(actor), identity(source_key)
        self.max_generations = bounded_int(max_generations, 1, 128)
        if authorize_write is not None and not callable(authorize_write):
            raise DocumentError("current package export authorization port required")
        self.authorize_write = authorize_write
        self.store._access(self.actor, "read", self.source_key)

    def _request(self, operation_id, revision, source_digest, workset_digest):
        identity(operation_id)
        bounded_int(revision, 1, 128)
        _sha(source_digest); _sha(workset_digest)
        folder = f"export-{operation_id}-r{revision}-{source_digest[:16]}"
        return folder

    def _check_root(self):
        if not self.root.is_dir() or self.root.is_symlink() or self.root.resolve() != self.root:
            raise DocumentError("export directory changed")
        self.store._access(self.actor, "read", self.source_key)

    def _check_write(self):
        self._check_root()
        if self.authorize_write is not None and self.authorize_write() is not True:
            raise DocumentError("current package export authorization withdrawn")

    def _verify(self, folder, revision, source_digest, workset_digest):
        self._check_root()
        destination = self.root / folder
        if destination.is_symlink():
            raise DocumentError("export generation unavailable")
        if not destination.exists():
            return dict(recorded=False, generation=folder)
        if not destination.is_dir():
            raise DocumentError("export generation unavailable")
        manifest = destination / "manifest.json"
        expected = encode(dict(source_revision=revision, source_digest=source_digest,
                               workset_digest=workset_digest, root_name=self.store.editor.snapshot.root_name))
        if manifest.is_symlink() or not manifest.is_file() or manifest.read_bytes() != expected:
            raise DocumentError("export generation provenance conflict")
        package = destination / self.store.editor.snapshot.root_name
        inspected = PackageWorkspace(package, self.workspace.editor,
                                     max_files=self.workspace.limits[0], max_total_bytes=self.workspace.limits[1],
                                     max_depth=self.workspace.limits[2], max_file_bytes=self.workspace.limits[3]).read()
        if inspected.digest != workset_digest or inspected.root_name != self.store.editor.snapshot.root_name:
            raise DocumentError("export generation content conflict")
        expected_files = {path for path, _ in inspected.files}
        expected_dirs = set()
        for path in expected_files:
            parts = Path(path).parts
            expected_dirs.update(Path(*parts[:index]).as_posix() for index in range(1, len(parts)))
        observed_files, observed_dirs = set(), set()
        for member in package.rglob("*"):
            relative = member.relative_to(package).as_posix()
            if member.is_symlink() or (hasattr(member, "is_junction") and member.is_junction()):
                raise DocumentError("export generation contains a link")
            if member.is_file():
                observed_files.add(relative)
            elif member.is_dir():
                observed_dirs.add(relative)
            else:
                raise DocumentError("export generation contains an unsupported entry")
            if len(observed_files) + len(observed_dirs) > self.workspace.limits[0] * (self.workspace.limits[2] + 3):
                raise DocumentError("export generation entry capacity exceeded")
        if observed_files != expected_files or observed_dirs != expected_dirs or manifest.read_bytes() != expected:
            raise DocumentError("export generation changed or contains extra entries")
        self._check_root()
        return dict(recorded=True, generation=folder, source_revision=revision,
                    source_digest=source_digest, workset_digest=workset_digest,
                    root_name=inspected.root_name, file_count=len(inspected.files))

    def reconcile(self, *, operation_id, expected_source_revision, expected_source_digest, expected_workset_digest):
        folder = self._request(operation_id, expected_source_revision, expected_source_digest, expected_workset_digest)
        return self._verify(folder, expected_source_revision, expected_source_digest, expected_workset_digest)

    def export(self, *, operation_id, expected_source_revision, expected_source_digest, expected_workset_digest):
        folder = self._request(operation_id, expected_source_revision, expected_source_digest, expected_workset_digest)
        self._check_write()
        existing = self._verify(folder, expected_source_revision, expected_source_digest, expected_workset_digest)
        if existing["recorded"]:
            return existing
        current = self.store.read(actor=self.actor, source_key=self.source_key,
                                  expected_revision=expected_source_revision,
                                  max_bytes=self.store.editor.limits[0])
        if current["digest"] != expected_source_digest:
            raise DocumentError("saved package source digest conflict")
        value = self.store.editor.parse(current["descriptor"])
        files = tuple((item["path"], base64.b64decode(item["base64"], validate=True)) for item in value["files"])
        if self.store.editor.inspect(current["descriptor"])["workset_digest"] != expected_workset_digest:
            raise DocumentError("saved package workset digest conflict")
        if len(files) > self.workspace.limits[0] or sum(len(raw) for _, raw in files) > self.workspace.limits[1]:
            raise DocumentError("package export capacity exceeded")
        if any(len(raw) > self.workspace.limits[3] for _, raw in files):
            raise DocumentError("package export file capacity exceeded")
        self._check_write()
        if len([entry for entry in self.root.iterdir() if entry.name.startswith("export-")]) >= self.max_generations:
            raise DocumentError("package generation capacity exhausted")
        stage = Path(tempfile.mkdtemp(prefix=".facetwire-stage-", dir=self.root))
        try:
            package = stage / self.store.editor.snapshot.root_name
            package.mkdir()
            manifest = stage / "manifest.json"
            with manifest.open("xb") as stream:
                stream.write(encode(dict(source_revision=expected_source_revision, source_digest=expected_source_digest,
                                         workset_digest=expected_workset_digest, root_name=self.store.editor.snapshot.root_name)))
                stream.flush()
                os.fsync(stream.fileno())
            for relative, raw in files:
                target = package.joinpath(*_relative(relative).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
            self._check_write()
            if self.workspace.read().digest != self.store.editor.snapshot.digest:
                raise DocumentError("original package changed before export")
            latest = self.store.read(actor=self.actor, source_key=self.source_key,
                                     expected_revision=expected_source_revision,
                                     max_bytes=self.store.editor.limits[0])
            if latest["digest"] != expected_source_digest:
                raise DocumentError("saved package source changed before export")
            if (self.root / folder).exists():
                raise DocumentError("package generation appeared during export; reconcile original operation")
            os.replace(stage, self.root / folder)
        finally:
            if stage.exists() and stage.parent.resolve() == self.root:
                shutil.rmtree(stage)
        return self._verify(folder, expected_source_revision, expected_source_digest, expected_workset_digest)
