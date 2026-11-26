"""JSON CLI sidecar for the DataGetTool core.

The toolbox calls this file as a subprocess; it does not import the tkinter GUI
or the toolbox engine.
"""
from __future__ import annotations

import argparse
import json
import sys

from datagettool_core import ToolError, extract_ba, inspect_bak, process_xml, restore_tar


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="datagettool-cli")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "tar", "ba", "xml"):
        item = sub.add_parser(name)
        item.add_argument("--path", required=True)
        if name in ("tar", "ba"):
            item.add_argument("--out", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "inspect":
            result = inspect_bak(args.path)
        elif args.command == "tar":
            result = restore_tar(args.path, args.out)
        elif args.command == "ba":
            result = extract_ba(args.path, args.out)
        else:
            result = process_xml(args.path)
    except ToolError as exc:
        print(json.dumps({"ok": False, "error": {"code": exc.code, "message": exc.message}}, ensure_ascii=False))
        return 1
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": {"code": "INTERNAL", "message": str(exc)}}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
