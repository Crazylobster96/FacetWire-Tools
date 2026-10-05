# SPDX-License-Identifier: Apache-2.0
"""Pinned external content validation for separately installed Renderer types.

This module validates descriptor data only. It neither loads Renderer code nor
copies a FacetWire schema into Tools; the trusted host pins each external schema.
"""
import hashlib
import json
from pathlib import Path
import re

from jsonschema import Draft202012Validator
from referencing import Registry

from .descriptor import DocumentError, _constant, _pairs, encode, _remote_reference


_BUILTINS = frozenset(('text', 'image', 'animated-image', 'video', 'audio',
                       'flow', 'document', 'placeholder'))


class ExtensionContent:
    def __init__(self, specifications=()):
        if type(specifications) is not tuple or len(specifications) > 16:
            raise DocumentError('bounded installed content profiles required')
        self.validators = {}
        hashes = []
        for item in specifications:
            if type(item) is not tuple or len(item) != 2:
                raise DocumentError('exact extension type and schema bytes required')
            kind, raw = item
            if (type(kind) is not str or re.fullmatch(r'[a-z][a-z0-9-]{0,63}', kind) is None
                    or kind in _BUILTINS or kind in self.validators or type(raw) is not bytes
                    or not 1 <= len(raw) <= 65536):
                raise DocumentError('unique bounded extension content schema required')
            try:
                schema = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_constant)
                if (type(schema) is not dict or schema.get('type') != 'object'
                        or schema.get('properties', {}).get('type') != {'const': kind}
                        or 'type' not in schema.get('required', ())
                        or schema.get('additionalProperties') is not False
                        or _has_ref(schema)):
                    raise DocumentError('extension content requires a closed exact-type schema')
                Draft202012Validator.check_schema(schema)
            except Exception:
                raise DocumentError('invalid pinned extension content schema') from None
            self.validators[kind] = Draft202012Validator(schema, registry=Registry(retrieve=_remote_reference))
            hashes.append((kind, hashlib.sha256(raw).hexdigest()))
        self.digest = hashlib.sha256(encode(hashes)).hexdigest()

    @classmethod
    def load(cls, entries):
        if type(entries) is not list or not 1 <= len(entries) <= 16:
            raise DocumentError('explicit installed extension profiles required')
        specs = []
        for entry in entries:
            if type(entry) is not dict or set(entry) != {'type', 'schema_path', 'sha256'}:
                raise DocumentError('exact installed extension profile required')
            path = entry['schema_path']
            digest = entry['sha256']
            if (type(path) is not str or not Path(path).is_absolute() or Path(path).is_symlink()
                    or Path(path).resolve() != Path(path)
                    or type(digest) is not str or re.fullmatch(r'[0-9a-f]{64}', digest) is None):
                raise DocumentError('absolute pinned extension schema required')
            try:
                with Path(path).open('rb') as stream:
                    raw = stream.read(65537)
            except OSError:
                raise DocumentError('installed extension schema unavailable') from None
            if not 1 <= len(raw) <= 65536 or hashlib.sha256(raw).hexdigest() != digest:
                raise DocumentError('installed extension schema digest changed')
            specs.append((entry['type'], raw))
        return cls(tuple(specs))

    def validate(self, content):
        if type(content) is not dict or type(content.get('type')) is not str:
            raise DocumentError('extension content object and type required')
        validator = self.validators.get(content['type'])
        if validator is None:
            raise DocumentError('extension Renderer content profile not installed')
        try:
            validator.validate(content)
        except Exception:
            raise DocumentError('extension Renderer content violates pinned profile') from None


def _has_ref(value):
    if type(value) is dict:
        return '$ref' in value or any(_has_ref(item) for item in value.values())
    if type(value) is list:
        return any(_has_ref(item) for item in value)
    return False
