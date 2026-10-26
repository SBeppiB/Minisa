"""Minimal CLI for Minisa public web research (not a chat replacement)."""
import argparse
import json
import os
from pathlib import Path
import sys

from runtime.research import ResearchError, extract_page, save_research, search_web


def main(argv=None):
    parser = argparse.ArgumentParser(description="Search public webpages and save attributed Minisa research notes.")
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search", help="search via Tavily or configured SearXNG")
    search.add_argument("query")
    fetch = commands.add_parser("fetch", help="extract readable text from one public webpage")
    fetch.add_argument("url")
    save = commands.add_parser("save", help="fetch a public webpage and save a concise note")
    save.add_argument("url")
    save.add_argument("--summary", required=True)
    save.add_argument("--point", action="append", default=[])
    save.add_argument("--title")
    args = parser.parse_args(argv)
    try:
        if args.command == "search":
            result = search_web(args.query)
        elif args.command == "fetch":
            result = extract_page(args.url)
        else:
            root_value = os.environ.get("MINISA_ROOT", "").strip()
            if not root_value:
                raise ResearchError("Set MINISA_ROOT to Minisa's configured storage root.")
            root = Path(root_value).expanduser()
            result = {"saved": str(save_research(root, args.url, args.summary,
                        key_points=args.point, title=args.title))}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ResearchError as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
