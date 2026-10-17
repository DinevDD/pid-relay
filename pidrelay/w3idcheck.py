"""Run w3id.org's own contribution checker (tools/check in perma-id/w3id.org)
over the rules tree, so files here also meet the rules w3id.org enforces.

The checker is optional. It is used when PIDRELAY_W3ID_CHECK points at
tools/check/bin/w3id-check.js of a w3id.org checkout (with `npm ci` done) and
`node` (>= 22) is available. Without it the rest of the validation still runs.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .lint import Issue

# Rules about the w3id.org repository itself (git history, maintainer
# metadata, file layout) rather than about redirect rules. Ownership here is
# handled by OWNERS.yaml, and only .htaccess / README.md are mirrored.
SKIP_RULES = {
    "git/minimal-commits", "git/no-merge-commits", "git/branch-not-stale",
    "git/descriptive-commit-message", "git/no-session-link",
    "tree/only-own-identifier", "tree/identifier-under-ids",
    "meta/document-identifier-root", "meta/maintainer-github-username",
    "meta/rule-docs-exist", "files/htaccess-required", "files/only-allowed-names",
}


def checker() -> tuple[str, str] | None:
    js = os.environ.get("PIDRELAY_W3ID_CHECK")
    node = os.environ.get("PIDRELAY_NODE") or shutil.which("node")
    if js and node and Path(js).is_file():
        return node, js
    return None


def run(root: Path) -> tuple[list[Issue], str | None]:
    """Returns (issues, note). note explains when the checker did not run."""
    found = checker()
    if not found:
        return [], "w3id.org checker not configured (set PIDRELAY_W3ID_CHECK); skipped"
    node, js = found
    rules = Path(root) / "rules"
    tmp = Path(tempfile.mkdtemp(prefix="pidrelay-w3id-"))
    try:
        ids = tmp / "ids"
        ids.mkdir()
        for dirpath, _, files in os.walk(rules):
            rel = os.path.relpath(dirpath, rules)
            if rel == ".":
                continue
            for f in files:
                if f in (".htaccess", "README.md"):
                    dst = ids / rel / f
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(Path(dirpath) / f, dst)
        if not any(ids.iterdir()):
            return [], None
        cfg = Path(js).resolve().parents[3] / ".w3id-check.yaml"
        if cfg.is_file():
            shutil.copyfile(cfg, tmp / ".w3id-check.yaml")
        subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
        r = subprocess.run([node, js, "ids", "--format", "json"], cwd=tmp,
                           capture_output=True, text=True, timeout=120,
                           env=dict(os.environ, NO_COLOR="1"))
        if r.returncode not in (0, 1):
            return [], f"w3id.org checker failed (exit {r.returncode}): {r.stderr.strip()[:300]}"
        data = json.loads(r.stdout)
    except (subprocess.SubprocessError, ValueError, OSError) as e:
        return [], f"w3id.org checker could not run: {e}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    issues = []
    for f in data.get("findings", []):
        rule = f.get("ruleId") or ""
        sev = f.get("severity")
        if rule in SKIP_RULES or sev not in ("error", "warning"):
            continue
        file = f.get("file") or ""
        file = "rules/" + file[len("ids/"):] if file.startswith("ids/") else file
        msg = f"[w3id.org {rule}] {f.get('message', '').strip()}"
        if f.get("docsUrl"):
            msg += f" ({f['docsUrl']})"
        issues.append(Issue(sev, file, f.get("line"), msg))
    return issues, None
