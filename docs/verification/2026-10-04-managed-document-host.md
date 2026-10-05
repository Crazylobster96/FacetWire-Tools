# Managed self-contained CLI host — Windows synthetic verification

The independent Tools host now has an explicit `managed-local-tools-v1` initialization path for a trusted host-supplied self-contained FacetWire descriptor with an exact configured document ID. The original `synthetic-local-tools-v1` initializer and its initial operation ID remain unchanged. No production document was read or exported.

Windows Python 3.11.11, pinned external FacetWire schema, `python tools/test-core.py --facetwire-schema-root ../../third_party/FacetWire/spec/schema`: **101/101 tests, 992/992 statements, 360/360 branches, exact 100%, no excluded branches** after the compatibility-preserving initial operation-ID adjustment. This is an independent Python source gate; macOS/Linux and visual Renderer behavior were not verified here.

The tests include valid managed initialization, editing, save and reopening, as well as invalid profile/ID/schema/input, existing database, permission and resource rejection. Paths, identities, limits and authorization remain trusted-host configuration, never callable tool arguments. Resource worksets, recursion, arbitrary package file import/export, and production authorization are not provided.
