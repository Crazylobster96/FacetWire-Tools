# SPDX-License-Identifier: Apache-2.0
"""Synthetic real-file workset tests; no user documents or model calls."""
import json
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from facetwire_tools.cli_host import synthetic_document
from facetwire_tools.descriptor import DescriptorEditor, DocumentError, encode
from facetwire_tools.package_workspace import PackageWorkspace, _relative


class PackageWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "example.agscene"
        self.root.mkdir()
        self.editor = DescriptorEditor(Path(os.environ["FACETWIRE_SCHEMA_ROOT"]),
                                       max_bytes=65536, max_nodes=100, max_depth=32)
        self.parent = synthetic_document()
        self.parent["id"] = "root"
        self.parent["resources"] = [{"id":"note","source":"resources/note.txt","mediaType":"text/plain"}]
        self.parent["canvas"]["pages"][0]["layers"][0]["zones"].append({
            "id":"nested", "bounds":{"x":0,"y":120,"width":300,"height":200},
            "content":{"type":"document","source":"documents/child.agscene/child.agscene.dis.json"}})
        self.child = synthetic_document()
        self.child["id"] = "child"
        self.child["resources"] = [{"id":"image:child","source":"resources/pixel.bin","mediaType":"image/png"}]
        self.child["canvas"]["pages"][0]["layers"][0]["zones"].append({
            "id":"picture", "bounds":{"x":0,"y":120,"width":100,"height":100},
            "content":{"type":"image","resource":"image:child","alt":"synthetic"}})
        self.save()

    def save(self):
        paths={"example.agscene.dis.json":encode(self.parent),
               "resources/note.txt":b"synthetic note",
               "documents/child.agscene/child.agscene.dis.json":encode(self.child),
               "documents/child.agscene/resources/pixel.bin":b"synthetic pixel"}
        for name,raw in paths.items():
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)

    def open(self, **changes):
        limits = dict(max_files=8, max_total_bytes=65536, max_depth=3, max_file_bytes=65536)
        limits.update(changes)
        return PackageWorkspace(self.root, self.editor, **limits)

    def test_complete_recursive_resource_snapshot_and_stable_digest(self):
        snapshot=self.open().read()
        self.assertEqual((4,2),(len(snapshot.files),len(snapshot.descriptor_paths)))
        self.assertEqual("example.agscene",snapshot.root_name)
        self.assertEqual(snapshot.digest,self.open().read().digest)
        self.assertEqual(4,snapshot.summary()["file_count"])
        (self.root/"resources/note.txt").write_bytes(b"changed")
        self.assertNotEqual(snapshot.digest,self.open().read().digest)
        self.assertEqual(b"synthetic note",dict(snapshot.files)["resources/note.txt"])
        (self.root/"resources/note.txt").write_bytes(b"")
        self.assertEqual(b"",dict(self.open().read().files)["resources/note.txt"])

    def test_bad_paths_scope_capacity_and_content_fail_closed(self):
        for value in ("", "../outside", "a//b", "a\\b", "C:/x", "/root", ".", "a/./b", "a/../b", "a\x00b"):
            with self.assertRaises(DocumentError):_relative(value)
        for options in (dict(max_files=3),dict(max_total_bytes=1),dict(max_depth=0),dict(max_file_bytes=1)):
            with self.assertRaises(DocumentError):self.open(**options).read()
        with self.assertRaises(DocumentError):PackageWorkspace(Path("relative.agscene"),self.editor,
            max_files=8,max_total_bytes=65536,max_depth=3,max_file_bytes=65536)
        with self.assertRaises(DocumentError):PackageWorkspace(self.root,None,
            max_files=8,max_total_bytes=65536,max_depth=3,max_file_bytes=65536)
        self.parent["resources"][0]["source"]="../outside"
        self.save()
        with self.assertRaises(DocumentError):self.open().read()

    def test_missing_duplicate_cycle_schema_and_reference_are_rejected(self):
        (self.root/"resources/note.txt").unlink()
        with self.assertRaises(DocumentError):self.open().read()
        self.save();self.parent["resources"].append(dict(self.parent["resources"][0]));self.save()
        with self.assertRaises(DocumentError):self.open().read()
        self.parent["resources"].pop()
        self.child["canvas"]["pages"][0]["layers"][0]["zones"][-1]["content"]["resource"]="missing"
        self.save()
        with self.assertRaises(DocumentError):self.open().read()
        self.child["canvas"]["pages"][0]["layers"][0]["zones"][-1]["content"]["resource"]="image:child"
        self.child["canvas"]["pages"][0]["layers"][0]["zones"].append({
            "id":"cycle","bounds":{"x":0,"y":250,"width":100,"height":100},
            "content":{"type":"document","source":"child.agscene.dis.json"}})
        self.save()
        with self.assertRaises(DocumentError):self.open().read()
        self.child["canvas"]["pages"][0]["layers"][0]["zones"].pop()
        self.parent["canvas"]["pages"][0]["layers"][0]["zones"][-1]["content"]["source"]="documents/child.json"
        self.save()
        with self.assertRaises(DocumentError):self.open().read()
        self.parent["canvas"]["pages"][0]["layers"][0]["zones"][-1]["content"]["source"]="documents/child.agscene/child.agscene.dis.json"
        (self.root/"example.agscene.dis.json").write_bytes(b"{bad")
        with self.assertRaises(DocumentError):self.open().read()

    def test_shared_resource_and_child_are_inventoried_once(self):
        self.parent["resources"].append({"id":"second-note","source":"resources/note.txt","mediaType":"text/plain"})
        second = dict(self.parent["canvas"]["pages"][0]["layers"][0]["zones"][-1])
        second["id"] = "second-nested"
        self.parent["canvas"]["pages"][0]["layers"][0]["zones"].append(second)
        self.save()
        self.assertEqual((4, 2), (len(self.open().read().files), len(self.open().read().descriptor_paths)))
        located=self.open().targets("picture",max_matches=2)
        self.assertEqual("ambiguous",located["status"])
        self.assertEqual({("nested",),("second-nested",)},
            {tuple(item["instance_path"]) for item in located["targets"]})
        self.assertEqual(1,len({item["descriptor_path"] for item in located["targets"]}))
        self.assertEqual(2,len({item["instance_key"] for item in located["targets"]}))
        with self.assertRaises(DocumentError):self.open().targets("picture",max_matches=1)

    def test_exact_target_location_never_selects_a_guess(self):
        workspace=self.open()
        unique=workspace.targets("picture",max_matches=1)
        self.assertEqual("unique",unique["status"])
        self.assertEqual(["nested"],unique["targets"][0]["instance_path"])
        self.assertEqual("zone",unique["targets"][0]["kind"])
        self.assertEqual(workspace.read().digest,unique["snapshot_digest"])
        root=workspace.targets("root",max_matches=1)
        self.assertEqual(("document",[]),(root["targets"][0]["kind"],root["targets"][0]["instance_path"]))
        self.assertEqual("not_found",workspace.targets("missing",max_matches=1)["status"])
        for value in (None, "", "x"*513):
            with self.assertRaises(DocumentError):workspace.targets(value,max_matches=1)
        for maximum in (0, True, 129):
            with self.assertRaises(DocumentError):workspace.targets("picture",max_matches=maximum)

    def test_in_scope_link_and_short_read_rejected(self):
        target = self.root / "resources/note.txt"
        original = Path.is_symlink
        with patch.object(Path, "is_symlink", lambda path: path == target or original(path)):
            with self.assertRaises(DocumentError):
                self.open().read()
        with patch.object(Path, "open", return_value=io.BytesIO(b"short")):
            with self.assertRaises(DocumentError):
                self.open()._read("resources/note.txt")

    def test_source_change_between_captures_and_final_check_rejected(self):
        workspace=self.open()
        original=workspace._read
        changed=False
        def read(path):
            nonlocal changed
            raw=original(path)
            if path=="resources/note.txt" and not changed:
                changed=True
                (self.root/path).write_bytes(b"new synthetic note")
            return raw
        with patch.object(workspace,"_read",side_effect=read):
            with self.assertRaises(DocumentError):workspace.read()

    def test_invalid_text_content_profile_rejected(self):
        self.parent["canvas"]["pages"][0]["layers"][0]["zones"][0]["content"]["style"] = {"fontSize":0}
        self.save()
        with self.assertRaises(DocumentError):
            self.open().read()

    def test_document_naming_identity_profile_and_normalized_paths(self):
        with self.assertRaises(DocumentError):
            _relative("cafe\u0301.txt")
        self.parent["canvas"]["pages"][0]["layers"][0]["zones"][0]["id"]="nested"
        self.save()
        with self.assertRaises(DocumentError):self.open().read()
        self.parent["canvas"]["pages"][0]["layers"][0]["zones"][0]["id"]="zone"
        nested=self.parent["canvas"]["pages"][0]["layers"][0]["zones"][-1]["content"]
        nested["source"]="documents/other.agscene/child.agscene.dis.json"
        self.save()
        with self.assertRaises(DocumentError):self.open().read()
        nested["source"]="documents/child.agscene/child.agscene.dis.json"
        self.parent["canvas"]["pages"][0]["layers"][0]["zones"][0]["content"]={"type":"flow"}
        self.save()
        with self.assertRaises(DocumentError):self.open().read()
        self.parent["canvas"]["pages"][0]["layers"][0]["zones"][0]["content"]={
            "type":"placeholder","kind":"unsupported","reason":"synthetic","mode":"minimal"}
        self.save()
        self.assertEqual(4,len(self.open().read().files))

    def test_media_font_and_secondary_resources_are_checked(self):
        def media(kind, content, mime, extra=None):
            self.parent["resources"]=[{"id":"primary","source":"resources/media.bin","mediaType":mime}]
            self.parent["canvas"]["pages"][0]["layers"][0]["zones"][0]["content"]={"type":kind,"resource":"primary",**content}
            if extra:self.parent["resources"].extend(extra)
            self.save()
            (self.root/"resources/media.bin").write_bytes(b"synthetic media")
            for item in extra or []:
                path=self.root/item["source"];path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b"synthetic secondary")
        media("image",{"alt":"sample"},"image/png")
        self.assertEqual(4,len(self.open().read().files))
        media("animated-image",{"alt":"sample"},"image/gif")
        self.open().read()
        media("video",{"label":"sample","posterResource":"poster",
            "tracks":[{"resource":"caption","kind":"captions","language":"en","label":"English"}]},
            "video/mp4",[{"id":"poster","source":"resources/poster.png","mediaType":"image/png"},
                         {"id":"caption","source":"resources/caption.vtt","mediaType":"text/vtt"}])
        self.assertEqual(6,len(self.open().read().files))
        self.parent["resources"][-1]["mediaType"]="image/png";self.save()
        with self.assertRaises(DocumentError):self.open().read()
        self.parent["resources"][-1]["mediaType"]="text/vtt";self.save()
        self.parent["resources"][-2]["mediaType"]="audio/wav";self.save()
        with self.assertRaises(DocumentError):self.open().read()
        media("audio",{"label":"sample","artworkResource":"art"},"audio/wav",
            [{"id":"art","source":"resources/art.png","mediaType":"image/png"}])
        self.open().read()
        self.parent["resources"][-1]["mediaType"]="video/mp4";self.save()
        with self.assertRaises(DocumentError):self.open().read()
        media("image",{"alt":"sample"},"video/mp4")
        with self.assertRaises(DocumentError):self.open().read()
        media("image",{},"image/png")
        with self.assertRaises(DocumentError):self.open().read()
        self.parent["resources"]=[{"id":"font","source":"resources/font.bin","mediaType":"font/woff2"}]
        self.parent["canvas"]["pages"][0]["layers"][0]["zones"][0]["content"]={"type":"text","text":"sample","style":{"fontResource":"font"}}
        self.save();(self.root/"resources/font.bin").write_bytes(b"synthetic font")
        self.open().read()
        self.parent["resources"][0]["mediaType"]="image/png";self.save()
        with self.assertRaises(DocumentError):self.open().read()
