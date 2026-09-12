# SPDX-License-Identifier: Apache-2.0
"""Independent exact coverage gate; no Pillow imports or source locations."""
import argparse
import os
from pathlib import Path
import sys
import unittest

import coverage

root = Path(__file__).resolve().parents[1]
args = argparse.ArgumentParser()
args.add_argument("--facetwire-schema-root", required=True, type=Path)
options = args.parse_args()
os.environ["FACETWIRE_SCHEMA_ROOT"] = str(options.facetwire_schema_root.resolve())
sys.path.insert(0, str(root / "src"))
cov = coverage.Coverage(branch=True, source=[str(root / "src")], config_file=False,
                        data_file=str(root / "coverage" / ".coverage"), timid=sys.platform == "win32")
# Keep the complete branch gate while avoiding the observed Windows C-tracer crash.
print("Coverage instrumentation:", "Python tracer (Windows)" if sys.platform == "win32" else "platform default", flush=True)
cov.set_option("report:exclude_lines", [])
cov.set_option("report:partial_branches", [])
cov.start()
suite = unittest.TestLoader().discover(str(root / "tests"), top_level_dir=str(root / "tests"))
result = unittest.TextTestRunner(verbosity=2).run(suite)
cov.stop()
cov.save()
cov.json_report(outfile=str(root / "coverage" / "report.json"))
cov.report()
import json
report = json.loads((root / "coverage" / "report.json").read_text(encoding="utf-8"))
failed = not result.wasSuccessful() or bool(result.skipped) or not result.testsRun
measured = {Path(name).resolve() for name in report["files"]}
for path in (root / "src").rglob("*.py"):
    content = path.read_text(encoding="utf-8")
    failed |= path.resolve() not in measured or "pragma: no cover" in content or "pragma: no branch" in content
for data in report["files"].values():
    summary = data["summary"]
    failed |= bool(summary["missing_lines"] or summary["missing_branches"] or summary["excluded_lines"])
raise SystemExit(1 if failed else 0)
