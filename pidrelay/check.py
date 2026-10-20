"""High-level checks shared by the CLI, CI and the management UI."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import w3idcheck
from .lint import Issue, lint_tree
from .runner import Httpd, TestResult, probe_namespaces, run_tests
from .tree import (FileSource, Owners, ancestors, effective_owners, governing_ns,
                   load_platform, parent)

TRAILER = "Pid-Relay-Account"


@dataclass
class Report:
    issues: list[Issue] = field(default_factory=list)
    results: list[TestResult] = field(default_factory=list)
    broken: list[tuple[str, str]] = field(default_factory=list)
    fatal: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def errors(self):
        return [i for i in self.issues if i.level == "error"]

    @property
    def failed(self):
        return [r for r in self.results if not r.ok]

    @property
    def ok(self) -> bool:
        return not (self.fatal or self.errors or self.failed or self.broken)

    def filtered(self, prefix: str) -> "Report":
        """Only what concerns namespace `prefix` and below (for UI display)."""
        if not prefix:
            return self
        f = f"rules/{prefix}"
        under = lambda ns: ns == prefix or ns.startswith(prefix + "/")  # noqa: E731
        return Report(
            [i for i in self.issues if i.file == f or i.file.startswith(f + "/")],
            [r for r in self.results if under(r.ns)],
            [b for b in self.broken if under(b[0])],
            self.fatal,
            self.notes,
        )

    def text(self) -> str:
        lines = []
        if self.fatal:
            lines.append(f"FATAL   {self.fatal}")
        lines += [str(i) for i in self.issues]
        for ns, msg in self.broken:
            lines.append(f"ERROR   rules/{ns}/.htaccess  Apache rejects this file: {msg}")
        for r in self.results:
            mark = "PASS" if r.ok else "FAIL"
            acc = f" (Accept: {r.accept})" if r.accept else ""
            lines.append(f"{mark:7} {r.file}  {r.name}{acc} -> {r.status} {r.location or ''}")
            for p in r.problems:
                lines.append(f"          {p}")
        n_pass = sum(r.ok for r in self.results)
        lines.append("")
        lines.append(f"{len(self.errors)} error(s), {len(self.issues) - len(self.errors)} warning(s), "
                     f"{n_pass}/{len(self.results)} test(s) passed, {len(self.broken)} broken file(s)")
        lines += [f"NOTE    {n}" for n in self.notes]
        lines.append("RESULT: " + ("OK" if self.ok else "FAILED"))
        return "\n".join(lines)


def validate(root: Path, public_url: str | None = None, run: bool = True) -> Report:
    """Lint the tree and, unless run=False, execute all tests on a real httpd."""
    root = Path(root)
    platform = load_platform(root)
    public_url = public_url or platform.get("public_url") or "https://localhost"
    rep = Report()
    rep.issues, tests = lint_tree(root)
    w3_issues, note = w3idcheck.run(root)
    rep.issues += w3_issues
    if note:
        rep.notes.append(note)
    if not run:
        return rep
    try:
        with Httpd(root / "rules", public_url) as h:
            rep.broken = probe_namespaces(h)
            rep.results = run_tests(h, tests)
    except RuntimeError as e:
        rep.fatal = str(e)
    return rep


# ---------------------------------------------------------------- ownership

def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout


def changed_files(root: Path, base: str, head: str) -> list[str]:
    out = _git(root, "diff", "--name-only", "--no-renames", f"{base}...{head}")
    return [l for l in out.splitlines() if l.strip()]


def ui_accounts(root: Path, base: str, head: str) -> set[str]:
    """Account names recorded by the management UI in commit trailers."""
    out = _git(root, "log", "--format=%B%x00", f"{base}..{head}")
    accts = set()
    for msg in out.split("\x00"):
        if not msg.strip():
            continue
        m = re.findall(rf"^{TRAILER}:\s*(\S+)\s*$", msg, re.M)
        if not m:
            accts.add("")  # a commit without trailer: not made by the UI
        accts.update(x.lower() for x in m)
    return accts


def check_ownership(root: Path, base: str, head: str, actor: str,
                    notes: list[str] | None = None) -> list[str]:
    """Return a list of permission problems for a change base..head by actor.

    Ownership is always read from the BASE revision, so a change cannot grant
    itself permissions.
    """
    root = Path(root)
    actor = (actor or "").lower()
    src = FileSource(root, base)
    platform_text = src.read("config/platform.yaml")
    import yaml

    pdata = yaml.safe_load(platform_text or "") or {}
    admins_raw = pdata.get("admins") or {}
    admin_gh = {x.lower() for x in admins_raw.get("github") or []}
    admin_acc = {x.lower() for x in admins_raw.get("accounts") or []}
    bot = (pdata.get("ui_bot") or "").lower()

    via_ui = bool(bot) and actor == bot
    accounts = ui_accounts(root, base, head) if via_ui else set()
    if via_ui and ("" in accounts or not accounts):
        return ["commit(s) by the UI bot without a Pid-Relay-Account trailer"]
    if via_ui and len(accounts) != 1:
        return [f"a UI change must come from a single account, got {sorted(accounts)}"]
    account = next(iter(accounts)) if via_ui else None

    def is_admin():
        return (account in admin_acc) if via_ui else (actor in admin_gh)

    problems = []
    changed = changed_files(root, base, head)

    claimed: list[str] = []
    if not via_ui and not is_admin():
        found, cproblems = find_claims(root, base, head, actor, changed)
        problems += cproblems
        claimed = list(found)
        if notes is not None:
            for ns, logins in found.items():
                notes.append(f"rules/{ns} claimed by {', '.join(sorted(logins))}")

    for f in changed:
        if any(f.startswith(f"rules/{ns}/") for ns in claimed):
            continue
        if not f.startswith("rules/"):
            if not is_admin():
                problems.append(f"{f}: only platform admins may change files outside rules/")
            continue
        if is_admin():
            continue
        ns = governing_ns(f)
        owners = effective_owners(src, ns)
        allowed = (account in owners.accounts) if via_ui else (actor in owners.github)
        if not allowed:
            who = f"account '{account}'" if via_ui else f"GitHub user '{actor}'"
            where = f"rules/{ns}" if ns else "the top of rules/ (new W3ID prefixes are admin-only)"
            problems.append(f"{f}: {who} is not an owner of {where}")
    return problems


# ------------------------------------------------------------------ claims

def _claimable_folder(src: FileSource, ns: str) -> tuple[str, Owners] | None:
    """The unclaimed, claimable folder that contains namespace ns, if any.

    A folder is claimable when it already exists with an OWNERS.yaml that
    lists nobody, and its parent's OWNERS.yaml says `claimable: true`.
    """
    for cand in reversed(ancestors(ns)):
        if not cand or "/" not in cand:
            continue
        text = src.read(f"rules/{cand}/OWNERS.yaml")
        if text is None:
            continue
        try:
            own = Owners.from_yaml(text)
            par = Owners.from_yaml(src.read(f"rules/{parent(cand)}/OWNERS.yaml"))
        except ValueError:
            return None
        if own.unclaimed and par.claimable:
            return cand, par
        return None  # nearest OWNERS decides; a claimed or plain folder is not claimable
    return None


def find_claims(root: Path, base: str, head: str, actor: str,
                changed: list[str] | None = None) -> tuple[dict[str, set[str]], list[str]]:
    """Folders this change claims, as {namespace: logins to record}, plus problems.

    Like claiming a namespace on w3id.org: the first pull request into an
    unclaimed group folder makes its author the owner. The author may list a
    partner by editing the folder's OWNERS.yaml in that same pull request.
    """
    actor = (actor or "").lower()
    src, head_src = FileSource(root, base), FileSource(root, head)
    changed = changed if changed is not None else changed_files(root, base, head)
    claims: dict[str, tuple[set[str], Owners]] = {}
    problems: list[str] = []
    for f in changed:
        if not f.startswith("rules/"):
            continue
        inner = f[len("rules/"):]
        d = inner.rsplit("/", 1)[0] if "/" in inner else ""
        hit = _claimable_folder(src, d)
        if not hit:
            continue
        ns, par = hit
        if ns in claims:
            continue
        logins = {actor}
        if f"rules/{ns}/OWNERS.yaml" in changed:
            try:
                after = Owners.from_yaml(head_src.read(f"rules/{ns}/OWNERS.yaml"))
            except ValueError:
                continue  # the linter reports the broken file
            if after.accounts:
                problems.append(f"rules/{ns}/OWNERS.yaml: a claim may only list GitHub logins; "
                                f"leave accounts: empty")
                continue
            logins |= after.github
            if after.github and actor not in after.github:
                problems.append(f"rules/{ns}/OWNERS.yaml: to claim rules/{ns}, your own GitHub "
                                f"login ('{actor}') must be in its github: list")
                continue
        if len(logins) > par.claim_max:
            problems.append(f"rules/{ns}: at most {par.claim_max} GitHub logins may claim one folder")
            continue
        claims[ns] = (logins, par)

    if len(claims) > 1:
        problems.append("a pull request may claim only one folder, but this one would claim "
                        + ", ".join(f"rules/{n}" for n in sorted(claims)))
        return {}, problems

    for ns in list(claims):
        # one folder per person within the same claimable parent
        par_dir = parent(ns)
        for sib in _siblings(root, base, par_dir):
            if sib == ns:
                continue
            try:
                o = Owners.from_yaml(src.read(f"rules/{sib}/OWNERS.yaml"))
            except ValueError:
                continue
            if actor in o.github:
                problems.append(f"rules/{ns}: you ('{actor}') already own rules/{sib}; "
                                f"one folder per person")
                claims.pop(ns)
                break
    return {ns: v[0] for ns, v in claims.items()}, problems


def _siblings(root: Path, rev: str, par_dir: str) -> list[str]:
    out = _git(root, "ls-tree", "-d", "--name-only", rev, f"rules/{par_dir}/")
    return [l[len("rules/"):] for l in out.splitlines() if l.strip()]


def record_claims(root: Path, base: str, head: str, actor: str) -> list[str]:
    """Write the logins of claims made in base..head into the working tree.

    Used by automerge right after merging. Returns the files written.
    """
    platform = load_platform(Path(root))
    a = (actor or "").lower()
    if a == platform["ui_bot"] or a in platform["admins"].github:
        return []  # admins and the UI bot never claim
    found, _ = find_claims(root, base, head, actor)
    written = []
    for ns, logins in found.items():
        p = Path(root) / "rules" / ns / "OWNERS.yaml"
        try:
            current = Owners.from_yaml(p.read_text()) if p.exists() else Owners()
        except ValueError:
            current = Owners()
        logins = logins | current.github
        p.write_text("# Claimed by the first pull request into this folder.\n"
                     "# Only these GitHub users may change it from now on.\n"
                     f"github: [{', '.join(sorted(logins))}]\n"
                     "accounts: []\n")
        written.append(str(p.relative_to(root)))
    return written
