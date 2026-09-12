# FacetWire Tools

Independent Apache-2.0 project: https://github.com/Crazylobster96/FacetWire-Tools. No Pillow package, identity, model, UI or Renderer dependency. Version 0.1.0.dev6 is experimental, not a production release.

## Repository and verification

This repository owns the implementation, tests and build. Pillow consumes a pinned Git submodule; commit changes here before updating the consumer pointer. No Pillow source or history is shipped with this package.

Install `requirements-test.txt`, then run the complete per-file statement/branch gate:

```sh
python tools/test-core.py --facetwire-schema-root /absolute/path/to/FacetWire/spec/schema
```

Verification uses external `Crazylobster96/FacetWire` commit `2b545b17cc370a074930577d2242981ff124d6b6`. CI checks out this exact commit into `work/facetwire-schema`. Schemas and renderers are not copied into the package; updates require compatibility verification. CI is configured for Python 3.11/3.12 on Windows/macOS/Linux; configuration is not evidence of successful runs.

Build with `python -m pip wheel . --no-deps --no-build-isolation --wheel-dir dist` after installing `setuptools==80.9.0 wheel==0.45.1`. A wheel grants no document access, enables no model and installs no renderer. See [verification](docs/verification/2026-09-12-repository-split.md).

## Supported behavior and constraints

The supported content profile is self-contained FacetWire 0.1 text/placeholder descriptors. Actual FacetWire schemas are supplied from a trusted external directory, not copied or redefined here. Resources, recursive documents and other content require future workspace adapters and are explicitly rejected.

`DescriptorEditor(schema_root, max_bytes=..., max_nodes=..., max_depth=...)` exposes `inspect(bytes)` and `prepare_patch(bytes, operations_tuple)`. It preserves unknown members, requires stable target IDs and exact preconditions, validates the whole result, and never writes source files. Supported fields: document title, Canvas/Page size, Layer z/rotation, Zone bounds and text color/text/selectable/opacity. Numeric geometry uses FacetWire's finite JSON numbers, not Pillow's integer-only protocol.

A prepared candidate is NOT an acknowledged edit. `DraftJournal` separately provides durable draft patches, history, undo/redo, bounded snapshots and operation reconciliation. Each mutation commits its recovery event before acknowledging; reopening validates and reconstructs the draft. Revision/head CAS and operation identity are independent. New edits after undo retain old audit events but invalidate that redo path. Exact duplicate requests return historical receipts, not proof of the current state.

The trusted host supplies an absolute protected SQLite path, scope, authenticated actor, current authorization callback, explicit capacities and initial descriptor (creation only). A library object is not a public session handle. The host must secure directory/ACL and keep the journal private: it contains full document history, is not encrypted by this library, and is never uploaded automatically. Capacity exhaustion blocks; there is no silent pruning. Cross-database save orchestration, nested worksets, network transport and Renderer integration remain subsequent increments. No filesystem permission is granted by knowing an object ID. Journaling alone does not save the formal `.agscene` source.

`tool_api.definitions()` returns separate versioned JSON Schemas for nine transport-neutral tools: inspect, validate, snapshot, preview_patch, apply_patch, undo, redo, history and reconcile (each prefixed `facetwire.document.`). A trusted host creates `DocumentTools(journal, actor=..., max_input_bytes=..., max_output_bytes=...)` and exposes its random session_id only to the authenticated caller. `call(name, arguments)` never accepts an actor, scope or path from tool arguments. Authorization is still checked for each action; knowing the handle is insufficient. Reopening attaches a new handle to existing verified history. No open/save tool is advertised before its storage/security adapter exists.

Tool results are data, not Agent instructions. Preview is uncommitted and version-bound; apply returns a committed draft receipt, not a saved artifact. Unknown commit outcomes must be reconciled with the same operation_id. Host callbacks cover `attach`, concrete read actions and `snapshot` reads, plus `patch`/`undo`/`redo`; they must authenticate and recheck current scope. This is not an MCP server or a model integration. Python 3.11+ is the declared compatibility target; current local verification is Windows/Python 3.12.14, not proof of other platform runs.

`DescriptorStore` independently stores immutable descriptor objects and managed source versions. Explicit `candidate` mode does not update the current source; `managed_local` mode performs version CAS. Expected source revision zero explicitly initializes a source; a candidate cannot implicitly initialize one. Limits include all candidates, old versions and request bytes. Writes are read back and validated before commit. This is an object-store adapter for self-contained descriptors, not arbitrary-directory overwrite or multi-file `.agscene` export.

`DraftJournal.reload(command, verify_source=..., approve_discard=...)` appends a reversible reload checkpoint. Dirty drafts require explicit discard approval bound to the entire frozen command and current revision/head/digest. Both trusted callbacks recheck before commit. Undo reload restores the old draft against the new source baseline; it never rolls back the store. `accept_saved(command, verify_source=...)` accepts a savepoint only when saved bytes exactly match the current revision; it updates the baseline without adding an edit to the undo path. Savepoints remain audit events. A newer draft must remain dirty/conflicted, not silently marked saved.

`storage_tools.definitions()` adds four tools: `facetwire.source.inspect`, `facetwire.source.snapshot`, `facetwire.source.reconcile`, and `facetwire.document.reload`. The host creates `StorageTools(journal, store, actor=..., source_key=..., approve_discard=..., max_input_bytes=..., max_output_bytes=...)`. Its session is distinct from `DocumentTools`; neither a source key, path nor actor is caller-selectable. Source versions are explicit, not implicitly latest. Reload reads and revalidates the actual source and calls the host's command-bound discard verifier inside the journal's guarded commit. Supplying an approval ID alone is insufficient. Reload commits a reversible draft event, never a store write. To reconcile an uncertain reload, use the journal's document reconcile tool with the original operation ID; source reconcile queries storage operations, not draft operations.

When the store and journal are separate databases, a successful store save alone does not mean a savepoint was acknowledged. That arrangement still requires explicit caller reconciliation and has no automatic cross-database save tool. It is not silently migrated, merged or overwritten.

Local dev5 adds `ManagedSaves(journal, store, actor=..., source_key=..., max_saves=..., max_total_bytes=..., create=...)` for an explicitly provisioned **single protected database** containing both table sets and a pre-existing managed source. Source version update, journal savepoint and immutable operation receipt commit in one transaction, with actual data readback and authorization checks before commit. Mismatched database paths or descriptor profiles are rejected. Initialization is host-only; no tool selects paths, changes actor/source, or initializes a source at revision zero. Existing separate databases are not automatically moved.

`managed_tools.definitions()` provides `facetwire.document.save` and `facetwire.document.save_reconcile`. Attach `ManagedSaveTools(saves, max_input_bytes=...)` with its own authenticated session. Save requires original operation ID, draft revision/head and source revision, all explicit. Reconcile queries the same original ID after an unknown outcome. A repeated exact save returns its historical receipt, while reconcile separately reports current draft/source state; historical success is not proof that no later edit occurred. Saved markers do not add artificial undo steps. Undo/redo changes the draft only, not the saved source. Replies contain bounded metadata, no document body. This managed object save is not arbitrary filesystem `.agscene` export or network publication.

Public tools now total fifteen across three host-attached groups: nine draft tools, four source/reload tools and two single-database save tools. dev6 adds the synthetic-only CLI described below. Production host policies, MCP transport, package export, recursive resources and Renderer integration remain pending. dev6 is a local development build, not a published release.

## Synthetic-only standalone CLI

Install the locally built wheel to obtain the `facetwire-tools` console command. Copy the bundled `facetwire_tools/examples/host.synthetic.json` (under `src/` in a checkout) to a protected location and explicitly edit its absolute database/schema paths and all desired limits/permissions. On macOS/Linux replace the sample Windows paths with absolute native paths. The database parent directory must already exist and be protected by the host's filesystem ACL. This example contains synthetic demonstration values, not production defaults or Pillow authorization policy.

Run `facetwire-tools --config /absolute/path/host.json initialize-synthetic` once. Initialization exclusively creates a new database and inserts a fixed synthetic descriptor; it never imports user documents or overwrites an existing file, even an empty one. Failure can leave a partial newly created database with no ready marker. It is not automatically retried, deleted or repaired; inspect it explicitly. Existing separate databases or library-created files are not adopted by this CLI.

Run `facetwire-tools --config /absolute/path/host.json describe` to obtain the fifteen CLI-adapted tool schemas. The CLI binds each internal tool session itself; callers must not send `session_id`, paths or identity overrides. Supply one UTF-8 JSON envelope on stdin to `facetwire-tools --config /absolute/path/host.json call`:

```json
{"tool":"facetwire.document.apply_patch","arguments":{"operation_id":"synthetic-edit-1","expected_revision":1,"expected_head":1,"operations":[{"target_id":"zone","field":"text","expected":"Synthetic content","value":"Synthetic updated"}]}}
```

Then save that revision with:

```json
{"tool":"facetwire.document.save","arguments":{"operation_id":"synthetic-save-1","expected_revision":2,"expected_head":2,"expected_source_revision":1}}
```

If a reply is lost, call `facetwire.document.save_reconcile` with `{"operation_id":"synthetic-save-1"}`. Do not issue a new save ID automatically. Tools return UTF-8 JSON data, not model instructions. For portable binary stdin, a host can use Python `subprocess.run([...], input=json.dumps(envelope, ensure_ascii=False).encode("utf-8"), check=True)` rather than relying on shell-specific text encodings.

Configuration is trusted host input, not a remotely callable tool argument. The host must fix the config path in any Agent wrapper and prevent untrusted callers from rewriting it. `enabled`, `allow_initialize`, exact draft/store action lists and discard approvals are read again at authorization boundaries; all other config values are bound to the initialized database. Changing an immutable value requires explicit new provisioning, not automatic migration. After initialization, the host can set `allow_initialize` to false. There are no wildcard grants or implicit write defaults.

Dirty reload still requires an exact configured approval entry containing `approval_id`, SHA-256 `command_digest` of the frozen internal reload command and SHA-256 `scope_digest` of its current host verification scope. The CLI does not self-approve; `discard_approvals: []` authorizes no dirty discard. Merely passing an approval ID cannot bypass this. Production approval UI/distribution is not implemented.

Nonzero exit returns a payload-free `not_acknowledged` result, never exception text or private paths. An error is not proof that a mutation failed before commit: reconcile the original operation and inspect current state. There is no model call, network server, remote publish, filesystem package export or background retry. Automated tests use temporary synthetic databases only; real-document use and production policy require separate review.

Build in an environment with the pinned build dependencies:

`python -m pip wheel . --no-deps --no-build-isolation --wheel-dir dist`

Standalone tests (set FACETWIRE_SCHEMA_ROOT to the absolute external FacetWire spec/schema directory):

`PYTHONPATH=src:tests python -m unittest discover -s tests -t tests -v`

On Windows use a semicolon for PYTHONPATH. Pillow's coverage gate additionally measures this independent source root without excluding branches. No remote schema resolution, model calls or plugin installation. FacetWire remains an external MPL-2.0 dependency with its own license; none of its source or schema is included in this distribution.

Independent exact coverage gate (requires coverage==7.10.7):

`python tools/test-core.py --facetwire-schema-root <absolute-external-spec/schema>`

This gate rejects missing files, missing statements/branches, skips and coverage suppression. Generated wheel/build/coverage artifacts are not source deliverables.
