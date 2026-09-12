# SPDX-License-Identifier: Apache-2.0
"""Explicit synthetic-only local console entry point; binary streams use UTF-8 JSON."""
import argparse
import sys

from .cli_host import CLIHost
from .descriptor import encode


def main(argv=None, *, stdin=None, stdout=None):
    parser = argparse.ArgumentParser(description="FacetWire Tools synthetic local host (no network/model)")
    parser.add_argument("--config", required=True, help="absolute trusted host configuration path, not an Agent argument")
    parser.add_argument("action", choices=("initialize-synthetic", "describe", "call"))
    args = parser.parse_args(argv)
    source = sys.stdin.buffer if stdin is None else stdin
    sink = sys.stdout.buffer if stdout is None else stdout
    try:
        host = CLIHost(args.config)
        if args.action == "initialize-synthetic":
            response = host.initialize()
        elif args.action == "describe":
            response = host.describe()
        else:
            response = host.call(source.read(host.config["max_input_bytes"] + 1))
        code = 0
    except Exception:
        # A caller cannot infer commit failure from an unacknowledged operation.
        # Never print exception strings that may contain paths or document data.
        response = dict(status="not_acknowledged", action=args.action, error="host_or_tool_unavailable",
                        retry="inspect_current_state_and_reconcile_original_operation_id")
        code = 1
    sink.write(encode(response) + b"\n")
    return code
