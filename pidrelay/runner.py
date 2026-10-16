"""Run a throw-away Apache httpd on a rules tree and execute tests against it."""
from __future__ import annotations

import http.client
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

CONF = Path(__file__).resolve().parent.parent / "redirector" / "pid-relay.conf"
CANDIDATE_BINARIES = [
    "/usr/local/apache2/bin/httpd",  # official httpd image
    "/usr/sbin/apache2",             # Debian/Ubuntu package
    "/usr/sbin/httpd",               # RHEL family
    "/opt/httpd/bin/httpd",
]
CANDIDATE_MODULES = [
    "/usr/local/apache2/modules", "/usr/lib/apache2/modules",
    "/usr/lib64/httpd/modules", "/usr/lib/httpd/modules", "/opt/httpd/modules",
]


def find_httpd() -> tuple[str, str]:
    binary = os.environ.get("PIDRELAY_HTTPD") or shutil.which("httpd") or shutil.which("apache2")
    if not binary:
        binary = next((b for b in CANDIDATE_BINARIES if os.path.exists(b)), None)
    if not binary:
        raise RuntimeError("Apache httpd not found. Install it or set PIDRELAY_HTTPD.")
    mods = os.environ.get("PIDRELAY_MODULES")
    if not mods:
        guess = Path(binary).resolve().parent.parent / "modules"
        mods = str(guess) if guess.is_dir() else next(
            (m for m in CANDIDATE_MODULES if os.path.isdir(m)), "")
    if not mods:
        raise RuntimeError("Apache modules directory not found. Set PIDRELAY_MODULES.")
    return binary, mods


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Httpd:
    """Context manager: an httpd serving rules_path on a random local port."""

    def __init__(self, rules_path: Path, public_url: str):
        self.rules_path = Path(rules_path).resolve()
        self.public_url = public_url.rstrip("/")
        self.port = _free_port()
        self.tmp = Path(tempfile.mkdtemp(prefix="pidrelay-httpd-"))
        self.proc: subprocess.Popen | None = None
        self._out = None

    def __enter__(self):
        try:
            return self._start()
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _start(self):
        binary, mods = find_httpd()
        (self.tmp / "empty").mkdir()
        (self.tmp / "logs").mkdir()
        # Serve a private copy: isolates the run from concurrent edits and lets
        # httpd (which drops privileges to 'daemon' when started as root) read it.
        src = self.rules_path
        self.rules_path = self.tmp / "rules"
        shutil.copytree(src, self.rules_path, symlinks=True)
        for dirpath, dirnames, filenames in os.walk(self.tmp):
            os.chmod(dirpath, 0o755)
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if not os.path.islink(fp):
                    os.chmod(fp, 0o644)
        env = dict(os.environ,
                   PIDRELAY_MODULES=mods,
                   PIDRELAY_RULES=str(self.rules_path),
                   PIDRELAY_LISTEN=f"127.0.0.1:{self.port}",
                   PIDRELAY_PUBLIC_URL=self.public_url,
                   PIDRELAY_RUNDIR=str(self.tmp),
                   PIDRELAY_ERRORLOG=str(self.tmp / "logs" / "error.log"),
                   PIDRELAY_ACCESSLOG=str(self.tmp / "logs" / "access.log"))
        self._out = open(self.tmp / "logs" / "stdout.log", "w")
        self.proc = subprocess.Popen([binary, "-f", str(CONF), "-DFOREGROUND"], env=env,
                                     stdout=self._out, stderr=subprocess.STDOUT)
        deadline = time.time() + 15
        while time.time() < deadline:
            if self.proc.poll() is not None:
                self._out.close()
                out = (self.tmp / "logs" / "stdout.log").read_text(errors="replace")
                raise RuntimeError(f"httpd failed to start:\n{out}\n{self.error_log()}")
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return self
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("httpd did not start within 15s")

    def __exit__(self, *exc):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self._out and not self._out.closed:
            self._out.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def error_log(self) -> str:
        p = self.tmp / "logs" / "error.log"
        return p.read_text(errors="replace") if p.exists() else ""

    def request(self, path: str, accept: str | None = None, method: str = "GET"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": "localhost", "User-Agent": "pid-relay-test"}
        if accept:
            headers["Accept"] = accept
        conn.request(method, path, headers=headers)
        r = conn.getresponse()
        r.read()
        res = (r.status, r.getheader("Location"), dict(r.getheaders()))
        conn.close()
        return res


@dataclass
class TestResult:
    ns: str
    file: str
    name: str
    request: str
    accept: str | None
    ok: bool
    status: int | None = None
    location: str | None = None
    problems: list[str] = field(default_factory=list)


def run_tests(httpd: Httpd, tests: list[dict]) -> list[TestResult]:
    results = []
    for t in tests:
        accept = t.get("accept")
        try:
            status, loc, _ = httpd.request(t["request"], accept, t.get("method", "GET"))
        except Exception as e:  # noqa: BLE001
            results.append(TestResult(t["ns"], t["file"], str(t["name"]), t["request"], accept,
                                      False, problems=[f"request failed: {e}"]))
            continue
        problems = []
        if "status" in t and status != t["status"]:
            problems.append(f"expected status {t['status']}, got {status}")
        if "location" in t and loc != t["location"]:
            problems.append(f"expected Location {t['location']}, got {loc or '(none)'}")
        if "location_regex" in t and not (loc and re.fullmatch(str(t["location_regex"]), loc)):
            problems.append(f"Location {loc or '(none)'} does not match {t['location_regex']}")
        results.append(TestResult(t["ns"], t["file"], str(t["name"]), t["request"], accept,
                                  not problems, status, loc, problems))
    return results


def probe_namespaces(httpd: Httpd) -> list[tuple[str, str]]:
    """Find directories whose .htaccess makes Apache fail (HTTP 500)."""
    rules_path = httpd.rules_path
    broken = []
    for dirpath, dirnames, filenames in os.walk(rules_path):
        if ".htaccess" not in filenames:
            continue
        ns = os.path.relpath(dirpath, rules_path).replace(os.sep, "/")
        status, _, _ = httpd.request(f"/pid/{ns}/__pid_relay_probe__")
        if status >= 500:
            needle = str(Path(dirpath) / ".htaccess")
            lines = [l for l in httpd.error_log().splitlines() if needle in l]
            msg = lines[-1] if lines else "Apache could not process this .htaccess"
            msg = re.sub(r"^\[.*?\] \[.*?\] \[.*?\] (\[client .*?\] )?", "", msg)
            broken.append((ns, msg.replace(str(rules_path) + "/", "rules/")))
    return broken
