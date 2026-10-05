# SPDX-License-Identifier: Apache-2.0
"""Self-contained text/placeholder descriptor preparation using external FacetWire schemas."""
from pathlib import Path
import hashlib
import json

from jsonschema import Draft202012Validator
from referencing import Registry


class DocumentError(ValueError):
    """Payload-free preparation error; no document or filesystem details."""


def bounded_int(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise DocumentError("invalid explicit integer bound")
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DocumentError("duplicate JSON key")
        result[key] = value
    return result


def _constant(value):
    raise DocumentError("non-finite JSON number")


def encode(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError):
        raise DocumentError("unsupported JSON value") from None


def _remote_reference(uri):
    raise DocumentError("remote schema resolution disabled")


class DescriptorEditor:
    """Prepares immutable candidates only; not an acknowledged editing session or save."""
    def __init__(self, schema_root, *, max_bytes, max_nodes, max_depth):
        self.root = Path(schema_root)
        if not self.root.is_absolute():
            raise DocumentError("absolute trusted external schema directory required")
        self.limits = (bounded_int(max_bytes, 1, 1048576), bounded_int(max_nodes, 1, 10000),
                       bounded_int(max_depth, 1, 64))
        schemas, hashes = [], []
        for filename in ("agent-scene-package-v0.1.schema.json", "core-content-profile-v0.1.schema.json"):
            raw = (self.root / filename).read_bytes()
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
            Draft202012Validator.check_schema(value)
            schemas.append(Draft202012Validator(value, registry=Registry(retrieve=_remote_reference)))
            hashes.append(hashlib.sha256(raw).hexdigest())
        self.base, self.content = schemas
        self.schema_digest = hashlib.sha256(encode(hashes)).hexdigest()

    def _depth(self, value, depth=0):
        bounded_int(depth, 0, self.limits[2])
        if type(value) is dict:
            for item in value.values():
                self._depth(item, depth + 1)
        elif type(value) is list:
            for item in value:
                self._depth(item, depth + 1)

    def _objects(self, value):
        result = [(value, "document"), (value["canvas"], "canvas")]
        for page in value["canvas"]["pages"]:
            result.append((page, "page"))
            for layer in page["layers"]:
                result.append((layer, "layer"))
                for zone in layer["zones"]:
                    result.append((zone, "zone"))
        bounded_int(len(result), 1, self.limits[1])
        ids = [obj["id"] for obj, _ in result]
        if len(set(ids)) != len(ids):
            raise DocumentError("duplicate document object identity")
        return result

    def parse(self, raw):
        if type(raw) is not bytes or not 1 <= len(raw) <= self.limits[0]:
            raise DocumentError("bounded UTF-8 descriptor bytes required")
        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
            self._depth(value)
            encode(value)
            self.base.validate(value)
        except Exception:
            raise DocumentError("descriptor validation failed") from None
        if value["resources"]:
            raise DocumentError("resource-bearing packages require a workspace adapter")
        for obj, kind in self._objects(value):
            if kind == "zone":
                content = obj["content"]
                if content["type"] == "text":
                    try:
                        self.content.validate(content)
                    except Exception:
                        raise DocumentError("text profile validation failed") from None
                elif content["type"] != "placeholder":
                    raise DocumentError("content outside self-contained editing profile")
        return value

    def inspect(self, raw):
        value = self.parse(raw)
        return {"profile": "self-contained-text-placeholder-v0.1", "schema_digest": self.schema_digest,
                "document_digest": hashlib.sha256(raw).hexdigest(),
                "objects": [{"id": obj["id"], "kind": kind} for obj, kind in self._objects(value)]}

    def prepare_patch(self, raw, operations):
        if type(operations) is not tuple:
            raise DocumentError("immutable explicit patch operations required")
        bounded_int(len(operations), 1, 128)
        # Freeze nested caller values too, before applying to a private parsed descriptor.
        try:
            ops = json.loads(encode(list(operations)), object_pairs_hook=_pairs)
        except Exception:
            raise DocumentError("invalid patch operations") from None
        value = self.parse(raw)
        self._apply_ops(value, ops)
        result = encode(value)
        self.parse(result)
        return result

    def _apply_ops(self, value, ops):
        """Apply the same bounded field capability to a verified package descriptor."""
        objects = {obj["id"]: (obj, kind) for obj, kind in self._objects(value)}
        for operation in ops:
            if type(operation) is not dict or set(operation) != {"target_id", "field", "expected", "value"}:
                raise DocumentError("exact typed patch fields required")
            target, field = operation["target_id"], operation["field"]
            if type(target) is not str or target not in objects or type(field) is not str:
                raise DocumentError("patch target or field unavailable")
            obj, kind = objects[target]
            paths = {"document": {"title": ("title",)}, "canvas": {"size": ("size",)},
                     "page": {"size": ("size",)}, "layer": {"z": ("z",), "rotation": ("transform", "rotationQuarterTurns")},
                     "zone": {"bounds": ("bounds",)}}
            if kind == "zone" and obj["content"]["type"] == "text":
                paths["zone"].update({"text": ("content", "text"), "color": ("content", "style", "color"),
                                      "selectable": ("content", "selectable"), "opacity": ("content", "opacity")})
            if field not in paths[kind]:
                raise DocumentError("field capability unavailable")
            path = paths[kind][field]
            parent = obj
            for key in path[:-1]:
                parent = parent.setdefault(key, {})
            existing = parent.get(path[-1])
            # JSON canonical equality distinguishes booleans from numeric values.
            if encode(existing) != encode(operation["expected"]):
                raise DocumentError("patch precondition conflict")
            parent[path[-1]] = operation["value"]
