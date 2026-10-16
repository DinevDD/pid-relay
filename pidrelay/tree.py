"""Model of the rules tree: namespaces, OWNERS files, permissions.

Layout::

    rules/
      <w3id-prefix>/            e.g. FAIR-course-UT, fdt
        OWNERS.yaml             who may change this subtree
        .htaccess               optional rules for the prefix itself
        tests.yaml              required whenever .htaccess exists
        <sub>/...               any depth, same files allowed at every level

A path inside the tree is written POSIX-style relative to rules/, e.g.
``FAIR-course-UT/2026/group07``. The empty string is the root of the tree.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ALLOWED_FILES = {".htaccess", "tests.yaml", "OWNERS.yaml", "README.md"}
SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def repo_root(start: str | os.PathLike | None = None) -> Path:
    """The repository root is the directory that contains rules/ and config/."""
    p = Path(start or os.environ.get("PIDRELAY_REPO", ".")).resolve()
    return p


def rules_dir(root: Path) -> Path:
    return root / "rules"


def valid_segment(name: str) -> bool:
    return bool(SEGMENT_RE.match(name)) and ".." not in name


def norm_ns(path: str) -> str:
    """Normalise a namespace path; raise ValueError if it is not acceptable."""
    path = (path or "").strip().strip("/")
    if not path:
        return ""
    parts = path.split("/")
    for seg in parts:
        if not valid_segment(seg):
            raise ValueError(f"invalid path segment {seg!r}")
    return "/".join(parts)


def ancestors(ns: str) -> list[str]:
    """'' , 'a', 'a/b', ... up to and including ns."""
    out = [""]
    if not ns:
        return out
    parts = ns.split("/")
    for i in range(1, len(parts) + 1):
        out.append("/".join(parts[:i]))
    return out


def parent(ns: str) -> str:
    return ns.rsplit("/", 1)[0] if "/" in ns else ""


@dataclass
class Owners:
    github: set[str] = field(default_factory=set)
    accounts: set[str] = field(default_factory=set)

    def merge(self, other: "Owners") -> "Owners":
        return Owners(self.github | other.github, self.accounts | other.accounts)

    @classmethod
    def from_yaml(cls, text: str | None) -> "Owners":
        if not text:
            return cls()
        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            raise ValueError("OWNERS.yaml must be a mapping")

        def _set(key):
            v = data.get(key) or []
            if isinstance(v, str):
                v = [v]
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise ValueError(f"OWNERS.yaml: '{key}' must be a list of strings")
            return {x.strip().lower() for x in v if x.strip()}

        return cls(_set("github"), _set("accounts"))


def load_platform(root: Path) -> dict:
    p = root / "config" / "platform.yaml"
    data = yaml.safe_load(p.read_text()) if p.exists() else {}
    data = data or {}
    admins = data.get("admins") or {}
    data["admins"] = Owners(
        {x.lower() for x in admins.get("github") or []},
        {x.lower() for x in admins.get("accounts") or []},
    )
    data["ui_bot"] = (data.get("ui_bot") or "").lower()
    return data


class FileSource:
    """Reads tree files either from a working directory or from a git revision."""

    def __init__(self, root: Path, rev: str | None = None):
        self.root = root
        self.rev = rev

    def read(self, relpath: str) -> str | None:
        if self.rev is None:
            p = self.root / relpath
            return p.read_text() if p.is_file() else None
        import subprocess

        r = subprocess.run(
            ["git", "-C", str(self.root), "show", f"{self.rev}:{relpath}"],
            capture_output=True, text=True,
        )
        return r.stdout if r.returncode == 0 else None


def effective_owners(src: FileSource, ns: str, admins: Owners | None = None) -> Owners:
    """Union of OWNERS.yaml from the root of the tree down to ns, plus admins."""
    o = admins or Owners()
    for a in ancestors(ns):
        rel = f"rules/{a}/OWNERS.yaml" if a else "rules/OWNERS.yaml"
        o = o.merge(Owners.from_yaml(src.read(rel)))
    return o


def governing_ns(relpath: str) -> str:
    """Namespace whose owners decide on a change to a file under rules/.

    Changing OWNERS.yaml of a namespace is decided by the owners of its
    parent, so nobody can add themselves to a namespace they do not own.
    """
    assert relpath.startswith("rules/")
    inner = relpath[len("rules/"):]
    d = inner.rsplit("/", 1)[0] if "/" in inner else ""
    name = inner.rsplit("/", 1)[-1]
    if name == "OWNERS.yaml":
        return parent(d) if d else ""
    return d


def list_namespaces(root: Path) -> list[str]:
    """All directories under rules/ (relative paths), sorted."""
    base = rules_dir(root)
    out = []
    for dirpath, dirnames, _ in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        rel = os.path.relpath(dirpath, base)
        if rel != ".":
            out.append(rel.replace(os.sep, "/"))
    return sorted(out)
