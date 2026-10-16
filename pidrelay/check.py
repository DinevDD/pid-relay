"""High-level checks shared by the CLI, CI and the management UI."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .lint import Issue, lint_tree
from .runner import Httpd, TestResult, probe_namespaces, run_tests
from .tree import FileSource, effective_owners, governing_ns, load_platform

TRAILER = "Pid-Relay-Account"


@dataclass
class Report:
    issues: list[Issue] = field(default_factory=list)
    results: list[TestResult] = field(default_factory=list)
    broken: list[tuple[str, str]] = field(default_factory=list)
    fatal: str | None = None

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
        lines.append("RESULT: " + ("OK" if self.ok else "FAILED"))
        return "\n".join(lines)


def validate(root: Path, public_url: str | None = None, run: bool = True) -> Report:
    """Lint the tree and, unless run=False, execute all tests on a real httpd."""
    root = Path(root)
    platform = load_platform(root)
    public_url = public_url or platform.get("public_url") or "https://localhost"
    rep = Report()
    rep.issues, tests = lint_tree(root)
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


def check_ownership(root: Path, base: str, head: str, actor: str) -> list[str]:
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
    for f in changed_files(root, base, head):
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
