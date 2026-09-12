# SPDX-License-Identifier: Apache-2.0
import json
import os
from pathlib import Path
import tempfile
import unittest

from facetwire_tools.descriptor import DescriptorEditor, DocumentError, encode, _remote_reference


def document():
    return {"format": "facetwire.agent-scene-package", "version": "0.1", "id": "doc", "title": "Synthetic",
            "extension": {"retain": [1, True, "中文"]}, "resources": [],
            "canvas": {"id": "canvas", "size": {"width": 640, "height": 480},
                       "pages": [{"id": "page", "size": {"width": 640, "height": 480},
                                  "layers": [{"id": "layer", "z": 1, "zones": [
                                      {"id": "zone", "bounds": {"x": 0, "y": 0, "width": 640, "height": 100},
                                       "content": {"type": "text", "text": "Synthetic content"}}]}]}]}}


class DescriptorTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(os.environ["FACETWIRE_SCHEMA_ROOT"])
        self.editor = DescriptorEditor(self.root, max_bytes=65536, max_nodes=100, max_depth=32)
        self.raw = encode(document())

    def op(self, target="zone", field="text", expected="Synthetic content", value="changed"):
        return {"target_id": target, "field": field, "expected": expected, "value": value}

    def test_inspection_external_schema_identity_unknown_members_and_private_candidate(self):
        inspected = self.editor.inspect(self.raw)
        self.assertEqual(["document", "canvas", "page", "layer", "zone"], [i["kind"] for i in inspected["objects"]])
        self.assertEqual(64, len(inspected["schema_digest"]))
        result = self.editor.prepare_patch(self.raw, (self.op(),))
        self.assertEqual(document()["extension"], json.loads(result)["extension"])
        self.assertEqual("changed", json.loads(result)["canvas"]["pages"][0]["layers"][0]["zones"][0]["content"]["text"])
        self.assertEqual(encode(document()), self.raw)
        self.assertNotEqual(inspected["document_digest"], self.editor.inspect(result)["document_digest"])

    def test_geometry_color_interaction_rotation_all_fields(self):
        operations = (
            self.op("doc", "title", "Synthetic", "New"),
            self.op("canvas", "size", {"width": 640, "height": 480}, {"width": 700.5, "height": 400}),
            self.op("page", "size", {"width": 640, "height": 480}, {"width": 700.5, "height": 400}),
            self.op("layer", "z", 1, 2), self.op("layer", "rotation", None, 1),
            self.op("zone", "bounds", {"x": 0, "y": 0, "width": 640, "height": 100},
                    {"x": 1.25, "y": -2, "width": 100, "height": 50}),
            self.op(field="color", expected=None, value={"red": 0.2, "green": 0.3, "blue": 0.4, "alpha": 1}),
            self.op(field="selectable", expected=None, value=False),
            self.op(field="opacity", expected=None, value=0.5))
        result = self.editor.parse(self.editor.prepare_patch(self.raw, operations))
        layer = result["canvas"]["pages"][0]["layers"][0]
        self.assertEqual(1, layer["transform"]["rotationQuarterTurns"])
        self.assertEqual(0.2, layer["zones"][0]["content"]["style"]["color"]["red"])
        self.assertFalse(layer["zones"][0]["content"]["selectable"])

    def test_conflict_or_bad_later_operation_preserves_original(self):
        for op in (self.op(expected="wrong"), self.op("layer", "z", True, 2),
                   self.op("layer", "z", 1, True), self.op(field="opacity", expected=None, value=2)):
            with self.assertRaises(DocumentError):
                self.editor.prepare_patch(self.raw, (self.op(), op))
        self.assertEqual(encode(document()), self.raw)

    def test_explicit_configuration_types_and_boundaries(self):
        for field, low, high in (("max_bytes", 1, 1048576), ("max_nodes", 1, 10000), ("max_depth", 1, 64)):
            for bad in (None, True, low - 1, high + 1, 1.2):
                args = dict(max_bytes=65536, max_nodes=100, max_depth=32)
                args[field] = bad
                with self.assertRaises(DocumentError):
                    DescriptorEditor(self.root, **args)
        with self.assertRaises(DocumentError):
            DescriptorEditor(Path("relative"), max_bytes=1, max_nodes=1, max_depth=1)
        DescriptorEditor(self.root, max_bytes=1048576, max_nodes=10000, max_depth=64)
        with self.assertRaises(FileNotFoundError):
            DescriptorEditor(self.root / "missing", max_bytes=1, max_nodes=1, max_depth=1)

    def test_utf8_finite_numbers_duplicates_and_unserializable_operations(self):
        for raw in (None, "", bytearray(self.raw), b"", b"x" * 65537, b"\xff", b"[]",
                    b'{"id":"a","id":"b"}', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1e999}', b'{"x":"\\ud800"}'):
            with self.assertRaises(DocumentError):
                self.editor.parse(raw)
        for value in (object(), float("nan"), float("inf"), "\ud800"):
            with self.assertRaises(DocumentError):
                encode(value)
        circular = []
        circular.append(circular)
        with self.assertRaises(DocumentError):
            encode(circular)
        with self.assertRaises(DocumentError):
            _remote_reference("https://example.invalid/schema")

    def test_schema_id_depth_node_and_byte_limits(self):
        exact = DescriptorEditor(self.root, max_bytes=len(self.raw), max_nodes=5, max_depth=32)
        self.assertEqual("doc", exact.parse(self.raw)["id"])
        for args in (dict(max_bytes=len(self.raw) - 1, max_nodes=5, max_depth=32),
                     dict(max_bytes=65536, max_nodes=4, max_depth=32), dict(max_bytes=65536, max_nodes=100, max_depth=1)):
            with self.assertRaises(DocumentError):
                DescriptorEditor(self.root, **args).parse(self.raw)
        bad = document()
        bad["canvas"]["id"] = "doc"
        with self.assertRaises(DocumentError):
            self.editor.parse(encode(bad))
        for field in ("version", "title"):
            bad = document()
            del bad[field]
            with self.assertRaises(DocumentError):
                self.editor.parse(encode(bad))
        bad = document()
        bad["canvas"]["pages"][0]["layers"][0]["zones"][0]["bounds"]["width"] = -1
        with self.assertRaises(DocumentError):
            self.editor.parse(encode(bad))

    def test_resources_recursion_media_and_unknown_profiles_are_explicitly_unsupported(self):
        bad = document()
        bad["resources"] = [{"id": "image", "source": "resources/a.png", "mediaType": "image/png"}]
        with self.assertRaises(DocumentError):
            self.editor.parse(encode(bad))
        for content in ({"type": "document", "source": "documents/c.agscene/c.agscene.dis.json"},
                        {"type": "image", "resource": "image", "alt": "x"}, {"type": "custom"},
                        {"type": "text", "text": 1}):
            bad = document()
            bad["canvas"]["pages"][0]["layers"][0]["zones"][0]["content"] = content
            with self.assertRaises(DocumentError):
                self.editor.parse(encode(bad))

    def test_empty_layers_zones_placeholder_and_capability_filter(self):
        empty = document()
        empty["canvas"]["pages"][0]["layers"] = []
        self.assertEqual(3, len(self.editor.inspect(encode(empty))["objects"]))
        empty = document()
        empty["canvas"]["pages"][0]["layers"][0]["zones"] = []
        self.assertEqual(4, len(self.editor.inspect(encode(empty))["objects"]))
        value = document()
        value["canvas"]["pages"][0]["layers"][0]["zones"][0]["content"] = {
            "type": "placeholder", "kind": "synthetic", "reason": "test", "mode": "minimal"}
        self.editor.parse(encode(value))
        with self.assertRaises(DocumentError):
            self.editor.prepare_patch(encode(value), (self.op(),))

    def test_operation_types_keys_targets_and_exact_maximum(self):
        for ops in (None, [], (), (self.op(),) * 129, (object(),), ({"wrong": "field"},), (None,),
                    (self.op(target=None),), (self.op(target="missing"),), (self.op(field=None),),
                    (self.op(field="execute"),)):
            with self.assertRaises(DocumentError):
                self.editor.prepare_patch(self.raw, ops)
        ops = tuple(self.op(expected="Synthetic content" if n == 0 else str(n - 1), value=str(n)) for n in range(128))
        result = self.editor.parse(self.editor.prepare_patch(self.raw, ops))
        self.assertEqual("127", result["canvas"]["pages"][0]["layers"][0]["zones"][0]["content"]["text"])

    def test_schema_identity_changes_and_remote_references_never_fetched(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            for name in ("agent-scene-package-v0.1.schema.json", "core-content-profile-v0.1.schema.json"):
                value = json.loads((self.root / name).read_bytes())
                value["description"] = "synthetic modified schema"
                (target / name).write_bytes(encode(value))
            changed = DescriptorEditor(target, max_bytes=65536, max_nodes=100, max_depth=32)
            self.assertNotEqual(self.editor.schema_digest, changed.schema_digest)
            value = json.loads((target / "agent-scene-package-v0.1.schema.json").read_bytes())
            value["$ref"] = "https://example.invalid/never-fetch"
            (target / "agent-scene-package-v0.1.schema.json").write_bytes(encode(value))
            blocked = DescriptorEditor(target, max_bytes=65536, max_nodes=100, max_depth=32)
            with self.assertRaises(DocumentError):
                blocked.parse(self.raw)
