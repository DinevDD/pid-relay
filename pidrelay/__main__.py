"""Command line: python -m pidrelay {lint,test,check-owners,w3id-snippet}"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .check import check_ownership, validate
from .tree import load_platform, norm_ns


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pidrelay")
    ap.add_argument("--root", default=".", help="repository root (contains rules/ and config/)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("lint", help="static checks only")
    t = sub.add_parser("test", help="static checks + run all tests on a temporary Apache httpd")
    t.add_argument("--only", help="report only this namespace and below, e.g. FAIR-course-UT/2026/group07")
    o = sub.add_parser("check-owners", help="verify the author may change what a commit range touches")
    o.add_argument("--base", required=True)
    o.add_argument("--head", required=True)
    o.add_argument("--actor", required=True, help="GitHub login of the pull request author")
    rc = sub.add_parser("record-claims", help="after a merge: write claimed folders' owners")
    rc.add_argument("--base", required=True)
    rc.add_argument("--head", required=True)
    rc.add_argument("--actor", required=True)
    s = sub.add_parser("w3id-snippet", help="print the .htaccess to submit to w3id.org for a prefix")
    s.add_argument("prefix")
    a = ap.parse_args(argv)
    root = Path(a.root).resolve()

    if a.cmd in ("lint", "test"):
        rep = validate(root, run=(a.cmd == "test"))
        if getattr(a, "only", None):
            rep = rep.filtered(norm_ns(a.only))
        print(rep.text())
        return 0 if rep.ok else 1

    if a.cmd == "check-owners":
        notes: list[str] = []
        problems = check_ownership(root, a.base, a.head, a.actor, notes)
        for n in notes:
            print(f"CLAIM   {n}")
        for p in problems:
            print(f"DENIED  {p}")
        print("RESULT: " + ("OK" if not problems else "FAILED"))
        return 0 if not problems else 1

    if a.cmd == "record-claims":
        from .check import record_claims

        for f in record_claims(root, a.base, a.head, a.actor):
            print(f"RECORDED {f}")
        return 0

    if a.cmd == "w3id-snippet":
        from .w3id import snippet

        print(snippet(norm_ns(a.prefix), load_platform(root).get("public_url")))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
