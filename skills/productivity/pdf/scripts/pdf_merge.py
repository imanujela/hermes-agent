#!/usr/bin/env python3
"""Merge multiple PDFs into one, optionally adding a bookmark per source file."""
from __future__ import annotations
import logging

import argparse
import json
import os
import sys
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parents[3] / "_lib"))
from skill_cli import setup_cli  # noqa: E402
logger = logging.getLogger(__name__)


def main() -> int:
    setup_cli()
    parser = argparse.ArgumentParser(description="Merge PDFs (pypdf).")
    parser.add_argument("inputs", nargs="+", help="Input PDF paths, in order")
    parser.add_argument("-o", "--output", required=True, help="Output PDF path")
    parser.add_argument("--bookmarks", action="store_true",
                        help="Add a top-level bookmark per input file (its basename)")
    args = parser.parse_args()

    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        print("Missing dependency: install with 'python3 -m pip install pypdf'", file=sys.stderr)
        return 2

    writer = PdfWriter()
    total = 0
    for path in args.inputs:
        reader = PdfReader(path)
        if reader.is_encrypted:
            print(f"Error: {path} is encrypted; decrypt it first with pdf_secure.py --decrypt", file=sys.stderr)
            return 3
        start = total
        for page in reader.pages:
            writer.add_page(page)
            total += 1
        if args.bookmarks:
            writer.add_outline_item(os.path.splitext(os.path.basename(path))[0], start)
    with open(args.output, "wb") as fh:
        writer.write(fh)
    print(json.dumps({"output": args.output, "inputs": len(args.inputs), "page_count": total}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
