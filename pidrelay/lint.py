"""Static checks on the rules tree.

Errors block a change. Warnings are shown but do not block.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .tree import ALLOWED_FILES, Owners, valid_segment

ALLOWED_DIRECTIVES = {
    "rewriteengine", "rewritecond", "rewriterule", "rewritebase",
    "redirect", "redirectmatch", "redirectpermanent", "redirecttemp",
    "header", "addtype", "options",
}
ALLOWED_OPTIONS = {"followsymlinks", "symlinksifownermatch", "multiviews"}
FORBIDDEN_FLAGS = {"p", "proxy", "co", "cookie", "h", "handler"}
ALLOWED_HEADERS = {
    "vary", "link", "cache-control", "access-control-allow-origin",
    "access-control-allow-headers", "access-control-expose-headers",
    "access-control-allow-methods",
}
HEADER_ACTIONS = {"set", "append", "merge", "add", "unset", "setifempty"}
TEST_KEYS = {"pid", "path", "accept", "status", "location", "location_regex", "note", "method"}
MAX_HTACCESS = 64 * 1024
W3ID_BASES = ("https://w3id.org/", "http://w3id.org/")


@dataclass
class Issue:
    level: str          # "error" | "warning"
    file: str           # path relative to repo root
    line: int | None
    msg: str

    def __str__(self):
        loc = f"{self.file}:{self.line}" if self.line else self.file
        return f"{self.level.upper():7} {loc}  {self.msg}"


def tokenize(line: str) -> list[str]:
    """Split an Apache config line into arguments.

    Quotes group words; backslashes are kept verbatim because they are
    regex escapes in RewriteRule/RewriteCond.
    """
    out, cur, q, i = [], "", None, 0
    while i < len(line):
        c = line[i]
        if q:
            if c == "\\" and i + 1 < len(line) and line[i + 1] == q:
                cur += q
                i += 2
                continue
            if c == q:
                q = None
            else:
                cur += c
        elif c in "\"'" and cur == "":
            q = c
        elif c.isspace():
            if cur:
                out.append(cur)
                cur = ""
        else:
            cur += c
        i += 1
    if q:
        raise ValueError("unbalanced quote")
    if cur:
        out.append(cur)
    return out


def logical_lines(text: str):
    """Yield (line_number, content) with comments removed and continuations joined."""
    buf, start = "", None
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if start is None:
            start = n
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        buf += line
        s = buf.strip()
        if s and not s.startswith("#"):
            yield start, s
        buf, start = "", None
    if buf.strip() and not buf.strip().startswith("#"):
        yield start, buf.strip()


def parse_flags(tok: str | None) -> dict[str, str | None]:
    if not tok:
        return {}
    if not (tok.startswith("[") and tok.endswith("]")):
        raise ValueError(f"flags must look like [R=303,L], got {tok!r}")
    flags = {}
    for part in tok[1:-1].split(","):
        part = part.strip()
        if not part:
            continue
        k, _, v = part.partition("=")
        flags[k.strip().lower()] = v.strip() or None
    return flags


def _is_external(target: str) -> bool:
    return bool(re.match(r"^https?://", target, re.I))


def lint_htaccess(text: str, relfile: str, ns: str) -> list[Issue]:
    issues: list[Issue] = []

    def err(line, msg):
        issues.append(Issue("error", relfile, line, msg))

    def warn(line, msg):
        issues.append(Issue("warning", relfile, line, msg))

    if len(text.encode()) > MAX_HTACCESS:
        err(None, f"file larger than {MAX_HTACCESS // 1024} KiB")
        return issues

    engine_on = False
    has_rewrite = False
    own_prefix = f"/pid/{ns}/"
    for n, line in logical_lines(text):
        if line.startswith("<"):
            err(n, "configuration sections such as <If>, <Files> or <IfModule> are not allowed")
            continue
        try:
            toks = tokenize(line)
        except ValueError as e:
            err(n, str(e))
            continue
        d, args = toks[0].lower(), toks[1:]
        if d not in ALLOWED_DIRECTIVES:
            err(n, f"directive '{toks[0]}' is not allowed (allowed: RewriteEngine, RewriteCond, "
                   f"RewriteRule, RewriteBase, Redirect*, Header, AddType, Options)")
            continue

        if d == "rewriteengine":
            if len(args) != 1 or args[0].lower() not in ("on", "off"):
                err(n, "RewriteEngine takes 'on' or 'off'")
            engine_on = bool(args) and args[0].lower() == "on"

        elif d == "options":
            for a in args:
                name = a.lstrip("+-").lower()
                if name not in ALLOWED_OPTIONS:
                    err(n, f"Options '{a}' is not allowed (only FollowSymLinks, SymLinksIfOwnerMatch, MultiViews)")

        elif d == "rewritecond":
            has_rewrite = True
            if len(args) not in (2, 3):
                err(n, "RewriteCond needs a test string, a condition and optional [flags]")
            elif len(args) == 3:
                try:
                    parse_flags(args[2])
                except ValueError as e:
                    err(n, str(e))
            if len(args) >= 2 and not args[1].startswith(("-", "<", ">", "=", "!")):
                try:
                    re.compile(args[1])
                except re.error as e:
                    warn(n, f"condition pattern may be invalid: {e}")

        elif d == "rewriterule":
            has_rewrite = True
            if len(args) not in (2, 3):
                err(n, "RewriteRule needs a pattern, a substitution and optional [flags]")
                continue
            pattern, subst = args[0], args[1]
            if pattern.startswith("/") or pattern.startswith("^/"):
                warn(n, "in .htaccess the pattern is matched against the path relative to this "
                        "directory, without a leading slash. '^/...' will never match")
            try:
                re.compile(pattern.lstrip("!"))
            except re.error as e:
                warn(n, f"pattern may be invalid: {e}")
            try:
                flags = parse_flags(args[2] if len(args) == 3 else None)
            except ValueError as e:
                err(n, str(e))
                continue
            bad = FORBIDDEN_FLAGS & set(flags)
            if bad:
                err(n, f"flag(s) {', '.join(sorted(bad)).upper()} are not allowed on this service")
            redirect = "r" in flags or "redirect" in flags
            code = flags.get("r") or flags.get("redirect")
            if code and not re.fullmatch(r"(30[0-8]|4\d\d|permanent|temp|seeother)", code.lower()):
                err(n, f"unsupported redirect status R={code}")
            if subst == "-":
                pass
            elif _is_external(subst):
                if not redirect:
                    warn(n, "absolute target without [R=...]: Apache will send a 302. "
                            "State the status explicitly, e.g. [R=303,L]")
                if "l" not in flags and "last" not in flags and "end" not in flags:
                    warn(n, "consider adding the L flag so later rules do not also apply")
            elif subst.startswith("/pid/"):
                if not redirect:
                    warn(n, "internal rewrite to another identifier; prefer an explicit redirect")
            else:
                err(n, f"target '{subst}' must be an absolute http(s) URL (or '-'). Relative "
                       f"targets behave differently here than on w3id.org")

        elif d == "rewritebase":
            if not args or not args[0].startswith("/pid/"):
                err(n, f"RewriteBase must start with /pid/ on this service (use {own_prefix})")

        elif d in ("redirect", "redirectmatch", "redirectpermanent", "redirecttemp"):
            a = list(args)
            if d in ("redirect", "redirectmatch") and a and not a[0].startswith(("/", "^")):
                a = a[1:]  # status
            if not a:
                err(n, f"{toks[0]} needs a path")
                continue
            path = a[0]
            target = a[1] if len(a) > 1 else None
            check = path.lstrip("^")
            if not check.startswith(own_prefix):
                err(n, f"{toks[0]} matches the full URL path, which on this service starts with "
                       f"'{own_prefix}'. Use RewriteRule with a relative pattern instead, so the "
                       f"same rules work on w3id.org")
            if target and not (_is_external(target) or target.startswith("/pid/")):
                err(n, f"target '{target}' must be an absolute http(s) URL")

        elif d == "header":
            a = list(args)
            if a and a[0].lower() in ("always", "onsuccess"):
                a = a[1:]
            if len(a) < 2 or a[0].lower() not in HEADER_ACTIONS:
                err(n, "Header: only 'set', 'append', 'merge', 'add', 'setifempty' and 'unset' are allowed")
                continue
            if a[1].lower() not in ALLOWED_HEADERS:
                err(n, f"Header '{a[1]}' may not be set here (allowed: Vary, Link, Cache-Control, Access-Control-*)")

    if has_rewrite and not engine_on:
        err(None, "RewriteRule/RewriteCond used but 'RewriteEngine on' is missing")
    return issues


def lint_tests(text: str, relfile: str, ns: str) -> tuple[list[Issue], list[dict]]:
    issues: list[Issue] = []
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        return [Issue("error", relfile, None, f"invalid YAML: {e}")], []
    tests = data.get("tests") if isinstance(data, dict) else None
    if not isinstance(tests, list) or not tests:
        return [Issue("error", relfile, None, "must contain a non-empty 'tests:' list")], []
    out = []
    for i, t in enumerate(tests, 1):
        where = f"test #{i}"
        if not isinstance(t, dict):
            issues.append(Issue("error", relfile, None, f"{where}: must be a mapping"))
            continue
        unknown = set(t) - TEST_KEYS
        if unknown:
            issues.append(Issue("error", relfile, None, f"{where}: unknown key(s) {', '.join(sorted(unknown))}"))
        if ("pid" in t) == ("path" in t):
            issues.append(Issue("error", relfile, None, f"{where}: give exactly one of 'pid' or 'path'"))
            continue
        if "pid" in t:
            pid = str(t["pid"])
            base = next((b for b in W3ID_BASES if pid.startswith(b)), None)
            if not base:
                issues.append(Issue("error", relfile, None, f"{where}: pid must start with https://w3id.org/"))
                continue
            inner = pid[len(base):]
            if not (inner == ns or inner.startswith(ns + "/")):
                issues.append(Issue("error", relfile, None,
                                    f"{where}: pid must lie inside this namespace (https://w3id.org/{ns}/...)"))
                continue
            req = "/pid/" + inner
        else:
            p = str(t["path"]).lstrip("/")
            req = f"/pid/{ns}/{p}"
        if "status" in t and not (isinstance(t["status"], int) and 100 <= t["status"] <= 599):
            issues.append(Issue("error", relfile, None, f"{where}: status must be an HTTP status code"))
        if "location" in t and "location_regex" in t:
            issues.append(Issue("error", relfile, None, f"{where}: use either location or location_regex"))
        if "location_regex" in t:
            try:
                re.compile(str(t["location_regex"]))
            except re.error as e:
                issues.append(Issue("error", relfile, None, f"{where}: bad location_regex: {e}"))
        if "status" not in t and "location" not in t and "location_regex" not in t:
            issues.append(Issue("error", relfile, None, f"{where}: needs at least an expected status or location"))
        out.append({**t, "request": req, "name": t.get("note") or (t.get("pid") or t.get("path")),
                    "ns": ns, "file": relfile})
    return issues, out


def lint_tree(root: Path) -> tuple[list[Issue], list[dict]]:
    """Lint everything under rules/. Returns (issues, collected tests)."""
    issues: list[Issue] = []
    tests: list[dict] = []
    base = root / "rules"
    if not base.is_dir():
        return [Issue("error", "rules", None, "rules/ directory missing")], []
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        relns = os.path.relpath(dirpath, base).replace(os.sep, "/")
        ns = "" if relns == "." else relns
        for d in list(dirnames):
            full = Path(dirpath) / d
            rel = f"rules/{ns + '/' if ns else ''}{d}"
            if full.is_symlink():
                issues.append(Issue("error", rel, None, "symbolic links are not allowed"))
                dirnames.remove(d)
            elif not valid_segment(d):
                issues.append(Issue("error", rel, None,
                                    "directory names may contain only letters, digits, '.', '_' and '-' "
                                    "and must start with a letter or digit"))
                dirnames.remove(d)
        for f in filenames:
            full = Path(dirpath) / f
            rel = f"rules/{ns + '/' if ns else ''}{f}"
            if full.is_symlink():
                issues.append(Issue("error", rel, None, "symbolic links are not allowed"))
            elif f not in ALLOWED_FILES:
                issues.append(Issue("error", rel, None,
                                    f"unexpected file. Only {', '.join(sorted(ALLOWED_FILES))} may live in the rules tree"))
            elif not ns and f != "README.md":
                issues.append(Issue("error", rel, None,
                                    "the top of rules/ may only contain W3ID prefix directories"))
        if not ns:
            continue
        files = set(filenames)
        hp = Path(dirpath) / ".htaccess"
        tp = Path(dirpath) / "tests.yaml"
        op = Path(dirpath) / "OWNERS.yaml"
        if op.is_file() and not op.is_symlink():
            try:
                Owners.from_yaml(op.read_text())
            except Exception as e:  # noqa: BLE001
                issues.append(Issue("error", f"rules/{ns}/OWNERS.yaml", None, str(e)))
        if "/" not in ns and "OWNERS.yaml" not in files:
            issues.append(Issue("error", f"rules/{ns}", None, "every W3ID prefix directory needs an OWNERS.yaml"))
        if hp.is_file() and not hp.is_symlink():
            issues += lint_htaccess(hp.read_text(errors="replace"), f"rules/{ns}/.htaccess", ns)
            if "tests.yaml" not in files:
                issues.append(Issue("error", f"rules/{ns}", None,
                                    "a directory with a .htaccess needs a tests.yaml describing the expected redirects"))
        if tp.is_file() and not tp.is_symlink():
            ti, tl = lint_tests(tp.read_text(), f"rules/{ns}/tests.yaml", ns)
            issues += ti
            tests += tl
    return issues, tests
