"""Git side of the management UI: deploying main, and submitting changes.

* sync(): fetches the configured branch into the *deploy* checkout (the
  directory the redirector serves). A new commit is only checked out after
  the whole tree passes `pidrelay` validation on a real httpd, so a broken
  commit never goes live, even if it bypassed CI.
* submit(): writes files into a separate *work* clone and either opens a
  pull request (GitHub mode) or pushes straight to the branch (direct mode,
  for setups without GitHub).
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from pidrelay.check import TRAILER, Report, validate


@dataclass
class Settings:
    remote: str
    branch: str
    deploy: Path
    work: Path
    state_file: Path
    github_repo: str | None
    github_token: str | None
    github_api: str = "https://api.github.com"
    commit_email_domain: str = "pid-relay.invalid"

    @property
    def pr_mode(self) -> bool:
        return bool(self.github_repo and self.github_token)


class GitError(RuntimeError):
    pass


class GitOps:
    def __init__(self, s: Settings):
        self.s = s
        self.lock = threading.Lock()   # serialises everything that touches git

    # ------------------------------------------------------------------ git
    def _auth_args(self) -> list[str]:
        if self.s.github_token and self.s.remote.startswith("https://"):
            cred = base64.b64encode(f"x-access-token:{self.s.github_token}".encode()).decode()
            return ["-c", f"http.extraHeader=Authorization: Basic {cred}"]
        return []

    def git(self, cwd: Path | None, *args: str, auth=False) -> str:
        cmd = ["git"] + (self._auth_args() if auth else [])
        if cwd:
            cmd += ["-C", str(cwd)]
        cmd += list(args)
        r = subprocess.run(cmd, capture_output=True, text=True,
                           env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
        if r.returncode:
            msg = (r.stderr or r.stdout).strip()
            if self.s.github_token:
                msg = msg.replace(self.s.github_token, "***")
            raise GitError(f"git {' '.join(args[:2])}: {msg}")
        return r.stdout.strip()

    def _ensure_clone(self, path: Path):
        if (path / ".git").exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and any(path.iterdir()):
            raise GitError(f"{path} exists and is not a git checkout")
        self.git(None, "clone", "--quiet", "--branch", self.s.branch, self.s.remote, str(path), auth=True)

    # --------------------------------------------------------------- state
    def state(self) -> dict:
        try:
            return json.loads(self.s.state_file.read_text())
        except (OSError, ValueError):
            return {}

    def _save_state(self, **kw):
        st = self.state()
        st.update(kw)
        self.s.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.s.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1))
        tmp.replace(self.s.state_file)

    def deployed(self) -> dict:
        try:
            out = self.git(self.s.deploy, "log", "-1", "--format=%H%x00%ci%x00%an%x00%s")
            h, date, author, subj = out.split("\x00")
            return {"sha": h, "date": date, "author": author, "subject": subj}
        except (GitError, ValueError):
            return {}

    # ---------------------------------------------------------------- sync
    def sync(self) -> dict:
        with self.lock:
            now = time.time()
            try:
                self._ensure_clone(self.s.deploy)
                self.git(self.s.deploy, "fetch", "--quiet", "origin", self.s.branch, auth=True)
                new = self.git(self.s.deploy, "rev-parse", "FETCH_HEAD")
                cur = self.git(self.s.deploy, "rev-parse", "HEAD")
                if new == cur:
                    self._save_state(last_check=now, error=None)
                    return self.state()
                if new == self.state().get("rejected_sha"):
                    self._save_state(last_check=now)
                    return self.state()
                rep = self.validate_revision(self.s.deploy, new)
                if rep.ok:
                    self.git(self.s.deploy, "reset", "--quiet", "--hard", new)
                    self._save_state(last_check=now, last_deploy=now, error=None,
                                     rejected_sha=None, rejected_report=None)
                else:
                    self._save_state(last_check=now, rejected_sha=new, rejected_report=rep.text(),
                                     error=f"commit {new[:10]} failed validation and was NOT deployed")
            except Exception as e:  # noqa: BLE001
                self._save_state(last_check=now, error=str(e))
            return self.state()

    def validate_revision(self, repo: Path, rev: str) -> Report:
        tmp = Path(tempfile.mkdtemp(prefix="pidrelay-rev-"))
        try:
            tarpath = tmp / "rev.tar"
            subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", "-o", str(tarpath), rev,
                            "rules", "config"], check=True, capture_output=True)
            with tarfile.open(tarpath) as t:
                try:
                    t.extractall(tmp / "tree", filter="data")
                except TypeError:  # Python without extraction filters
                    t.extractall(tmp / "tree")
            return validate(tmp / "tree")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def start_sync_loop(self, interval: int):
        def loop():
            while True:
                self.sync()
                time.sleep(interval)
        threading.Thread(target=loop, daemon=True, name="pidrelay-sync").start()

    # ----------------------------------------------------- candidate trees
    def candidate_report(self, changes: dict[str, str | None]) -> Report:
        """Validate the deployed tree with `changes` (relpath -> content/None) applied."""
        tmp = Path(tempfile.mkdtemp(prefix="pidrelay-cand-"))
        try:
            tree = tmp / "tree"
            shutil.copytree(self.s.deploy / "rules", tree / "rules", symlinks=True)
            shutil.copytree(self.s.deploy / "config", tree / "config", symlinks=True)
            apply_changes(tree, changes)
            return validate(tree)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # -------------------------------------------------------------- submit
    def submit(self, account: str, display: str, ns: str, summary: str,
               changes: dict[str, str | None]) -> dict:
        with self.lock:
            self._ensure_clone(self.s.work)
            w = self.s.work
            self.git(w, "fetch", "--quiet", "origin", self.s.branch, auth=True)
            if self.s.pr_mode:
                branch = f"pid-relay/{account}/{time.strftime('%Y%m%d-%H%M%S')}"
            else:
                branch = self.s.branch
            self.git(w, "checkout", "--quiet", "-B", branch, "FETCH_HEAD")
            self.git(w, "reset", "--quiet", "--hard", "FETCH_HEAD")
            self.git(w, "clean", "-qfd")
            apply_changes(w, changes)
            self.git(w, "add", "-A", "rules")
            if not self.git(w, "status", "--porcelain"):
                raise GitError("nothing changed compared to the current version")
            msg = f"{summary}\n\nSubmitted through the pid-relay management UI.\n\n{TRAILER}: {account}"
            self.git(w, "-c", f"user.name={display or account}",
                     "-c", f"user.email={account}@{self.s.commit_email_domain}",
                     "commit", "--quiet", "-m", msg)
            sha = self.git(w, "rev-parse", "HEAD")
            self.git(w, "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}", auth=True)
            out = {"mode": "pr" if self.s.pr_mode else "direct", "branch": branch, "commit_sha": sha}
            if self.s.pr_mode:
                r = requests.post(
                    f"{self.s.github_api}/repos/{self.s.github_repo}/pulls",
                    headers=self._gh_headers(),
                    json={"title": f"[{ns}] {summary}", "head": branch, "base": self.s.branch,
                          "body": f"Change to `rules/{ns}` submitted by UI account **{account}**"
                                  f" ({display}).\n\nValidated before submission; CI re-checks "
                                  f"ownership and tests."},
                    timeout=20)
                if r.status_code >= 300:
                    raise GitError(f"GitHub refused to open the pull request: {r.status_code} {r.text[:300]}")
                pr = r.json()
                out.update(pr_number=pr["number"], pr_url=pr["html_url"])
        if not self.s.pr_mode:
            threading.Thread(target=self.sync, daemon=True).start()
        return out

    def _gh_headers(self):
        return {"Authorization": f"Bearer {self.s.github_token}",
                "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

    def pr_status(self, number: int) -> dict:
        """State of a PR plus the combined conclusion of its checks."""
        if not self.s.pr_mode:
            return {}
        try:
            r = requests.get(f"{self.s.github_api}/repos/{self.s.github_repo}/pulls/{number}",
                             headers=self._gh_headers(), timeout=10)
            pr = r.json()
            state = "merged" if pr.get("merged") else pr.get("state", "unknown")
            sha = pr.get("head", {}).get("sha")
            checks = "unknown"
            if sha:
                c = requests.get(f"{self.s.github_api}/repos/{self.s.github_repo}/commits/{sha}/check-runs",
                                 headers=self._gh_headers(), timeout=10).json()
                runs = c.get("check_runs", [])
                if not runs:
                    checks = "pending"
                elif any(x.get("status") != "completed" for x in runs):
                    checks = "running"
                elif all(x.get("conclusion") in ("success", "skipped", "neutral") for x in runs):
                    checks = "passed"
                else:
                    checks = "failed"
            return {"state": state, "checks": checks}
        except Exception:  # noqa: BLE001
            return {"state": "unknown", "checks": "unknown"}


def apply_changes(tree: Path, changes: dict[str, str | None]):
    for rel, content in changes.items():
        p = (tree / rel).resolve()
        if not str(p).startswith(str((tree / "rules").resolve()) + os.sep):
            raise ValueError(f"refusing to write outside rules/: {rel}")
        if content is None:
            if p.exists():
                p.unlink()
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        if not content.endswith("\n"):
            content += "\n"
        p.write_text(content)
