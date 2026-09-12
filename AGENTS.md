# FacetWire Tools development

This is an independent Apache-2.0 repository. Read README.md and current verification records before changes. Preserve user changes and the upstream FacetWire format; do not copy schema or renderer implementation here.

- No dependency on Pillow identities, model runtimes, UI or storage. Hosts supply explicit authorization, paths and limits; tool parameters cannot grant authority.
- Each mutation records recoverable history before acknowledgement. Preserve undo/redo, dirty-state protection, immutable receipts and unknown-outcome reconciliation. Never silently discard edits.
- Use synthetic tests and the external schema revision in README.md. No real models/documents without authorization.
- Run `python tools/test-core.py --facetwire-schema-root <external-schema-path>`. Each source file must reach measured 100% statements/branches, covering invalid input, boundaries and errors. Do not exclude branches, narrow scope or skip tests.
- Use explicit UTF-8. Never commit credentials, databases, local configuration, histories, build output or caches.
- Commit/push only with user authorization. Push the independent commit before updating consumer submodule pointers. Do not force-push or update FacetWire's revision during unrelated work.
