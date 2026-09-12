# Independent repository split verification

2026-09-12. The user supplied `https://github.com/Crazylobster96/FacetWire-Tools` and authorized source submission using existing Git credentials. Remote initial commit `8517677b322e44f09919d0c13ba0d7a4333945eb` contains only LICENSE; its Apache-2.0 blob exactly matches this project and is preserved.

This is the existing 0.1.0.dev6 implementation, not a new feature or production release. Source/tests are unchanged by the split. Windows / Python 3.12.14 / coverage 7.10.7 Python tracer: independent gate passed 98 tests in 46.546 seconds, exit 0; 967 statements and 344 branches, per-file 100%, no missing/excluded coverage. Command: `python tools/test-core.py --facetwire-schema-root <external-FacetWire>/spec/schema`. External FacetWire revision: `2b545b17cc370a074930577d2242981ff124d6b6`.

Build verification: `python -m pip wheel ./independent/facetwire-tools --no-deps --no-build-isolation --wheel-dir ./work/facetwire-tools-publication-dist` passed with exit 0, producing `facetwire_tools-0.1.0.dev6-py3-none-any.whl`. Generated distributions and metadata are excluded from Git. The source push is not a package release.

Only synthetic temporary data was used. No production model, renderer/UI, real document, macOS/iOS device or cross-machine behavior was validated here. CI is configured for Windows/macOS/Linux and Python 3.11/3.12; remote results are not implied by configuration. Consumer clean-checkout validation is recorded in Pillow's repository handoff document, not assumed here.
