"""pid-relay management UI.

Browse deployed rules, test identifiers, edit rules with a helper, and submit
changes as git commits / pull requests. Local accounts only (for now).
"""
from __future__ import annotations

import csv
import io
import os
import re
import secrets
import time
from collections import defaultdict
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

import requests
from flask import (Flask, Response, abort, flash, g, redirect, render_template, request,
                   session, url_for)

from pidrelay.tree import (FileSource, Owners, ancestors, effective_owners,
                           list_namespaces, load_platform, norm_ns, parent, valid_segment)
from pidrelay.w3id import relay_url, snippet, w3id_url

from .db import DB, gen_password
from .gitops import GitError, GitOps, Settings

EDITABLE = (".htaccess", "tests.yaml", "README.md")
ACCEPT_PRESETS = [
    ("", "(no Accept header)"),
    ("text/html", "text/html (browser)"),
    ("text/turtle", "text/turtle"),
    ("application/ld+json", "application/ld+json"),
    ("application/rdf+xml", "application/rdf+xml"),
    ("application/n-triples", "application/n-triples"),
    ("application/json", "application/json"),
]


def env(name, default=None):
    return os.environ.get(f"PIDRELAY_{name}", default)


def create_app() -> Flask:
    app = Flask(__name__)
    data = Path(env("DATA", "/data"))
    app.config.update(
        SECRET_KEY=env("SECRET_KEY") or _persistent_secret(data),
        SESSION_COOKIE_NAME="pidrelay_session",
        SESSION_COOKIE_PATH=env("UI_BASE", "/pid-manager").rstrip("/") or "/",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=env("COOKIE_SECURE", "1") == "1",
        PERMANENT_SESSION_LIFETIME=8 * 3600,
        MAX_CONTENT_LENGTH=512 * 1024,
    )
    settings = Settings(
        remote=env("REMOTE", ""),
        branch=env("BRANCH", "main"),
        deploy=Path(env("DEPLOY", "/srv/deploy")),
        work=data / "work",
        state_file=data / "sync.json",
        github_repo=env("GITHUB_REPO"),
        github_token=env("GITHUB_TOKEN"),
    )
    gitops = GitOps(settings)
    db = DB(data / "pid-relay.db")
    redirector = env("REDIRECTOR_URL", "http://redirector")
    failures: dict[str, list[float]] = defaultdict(list)

    if db.count_users() == 0:
        pw = env("ADMIN_PASSWORD") or gen_password()
        db.create_user("admin", "Administrator", pw, is_admin=True, must_change=not env("ADMIN_PASSWORD"))
        if not env("ADMIN_PASSWORD"):
            print(f"[pid-relay] created initial account 'admin' with password: {pw}", flush=True)

    if env("SYNC", "1") == "1" and settings.remote:
        gitops.start_sync_loop(int(env("SYNC_INTERVAL", "60")))

    app.extensions["pidrelay"] = {"db": db, "git": gitops, "settings": settings}

    # ------------------------------------------------------------ helpers
    def root() -> Path:
        return settings.deploy

    def platform():
        try:
            return load_platform(root())
        except Exception:  # noqa: BLE001
            return {"admins": Owners(), "public_url": env("PUBLIC_URL", "")}

    def public_url():
        return env("PUBLIC_URL") or platform().get("public_url") or ""

    def is_rule_admin(user) -> bool:
        return bool(user) and user["username"] in platform()["admins"].accounts

    def owners_of(ns: str) -> Owners:
        return effective_owners(FileSource(root()), ns)

    def can_edit(ns: str) -> bool:
        u = g.user
        if not u or not ns:
            return False
        return is_rule_admin(u) or u["username"] in owners_of(ns).accounts

    def can_manage_children(ns: str) -> bool:
        """May create sub-namespaces of ns / edit their OWNERS (= owner of ns)."""
        u = g.user
        if not u:
            return False
        if not ns:
            return is_rule_admin(u)
        return can_edit(ns)

    def read(ns: str, name: str) -> str | None:
        p = root() / "rules" / ns / name
        return p.read_text(errors="replace") if p.is_file() else None

    def ns_exists(ns: str) -> bool:
        return (root() / "rules" / ns).is_dir()

    @app.context_processor
    def inject():
        return {"user": g.get("user"), "csrf": session.get("csrf"), "public_url": public_url(),
                "is_rule_admin": is_rule_admin(g.get("user")), "pr_mode": settings.pr_mode,
                "w3id_url": w3id_url, "relay_url": lambda ns: relay_url(ns, public_url()),
                "ancestors": ancestors, "fmt_time": _fmt_time}

    @app.before_request
    def load_user():
        g.user = None
        if "u" in session:
            u = db.user(session["u"])
            if u and u["active"]:
                g.user = u
            else:
                session.clear()
        session.setdefault("csrf", secrets.token_urlsafe(24))
        if request.method == "POST":
            if request.form.get("csrf") != session.get("csrf"):
                abort(400, "invalid form token; reload the page and try again")
        if (g.user and g.user["must_change"] and request.endpoint not in
                ("account", "logout", "static")):
            return redirect(url_for("account"))

    def login_required(f):
        @wraps(f)
        def w(*a, **kw):
            if not g.user:
                return redirect(url_for("login", next=request.full_path))
            return f(*a, **kw)
        return w

    def admin_required(f):
        @wraps(f)
        def w(*a, **kw):
            if not g.user:
                return redirect(url_for("login", next=request.full_path))
            if not g.user["is_admin"]:
                abort(403)
            return f(*a, **kw)
        return w

    def get_ns(path) -> str:
        try:
            return norm_ns(path)
        except ValueError:
            abort(404)

    # --------------------------------------------------------------- auth
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            name = request.form.get("username", "").strip().lower()
            key = f"{name}|{request.remote_addr}"
            failures[key] = [t for t in failures[key] if t > time.time() - 900]
            if len(failures[key]) >= 10:
                flash("Too many failed attempts. Try again in 15 minutes.", "error")
                return render_template("login.html"), 429
            u = db.authenticate(name, request.form.get("password", ""))
            if u:
                session.clear()
                session["u"] = u["username"]
                session["csrf"] = secrets.token_urlsafe(24)
                session.permanent = True
                nxt = request.args.get("next", "")
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("index"))
            failures[key].append(time.time())
            flash("Unknown account or wrong password.", "error")
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("index"))

    @app.route("/account", methods=["GET", "POST"])
    @login_required
    def account():
        if request.method == "POST":
            cur, new, rep = (request.form.get(k, "") for k in ("current", "new", "repeat"))
            if not db.authenticate(g.user["username"], cur):
                flash("Current password is wrong.", "error")
            elif len(new) < 10:
                flash("Use at least 10 characters.", "error")
            elif new != rep:
                flash("The two new passwords differ.", "error")
            else:
                db.set_password(g.user["username"], new)
                flash("Password changed.", "ok")
                return redirect(url_for("index"))
        mine = [ns for ns in list_namespaces(root()) if g.user["username"] in
                Owners.from_yaml(read(ns, "OWNERS.yaml")).accounts] if ns_exists("") else []
        return render_template("account.html", mine=mine)

    # ------------------------------------------------------------- browse
    @app.get("/")
    def index():
        all_ns = list_namespaces(root()) if (root() / "rules").is_dir() else []
        prefixes = []
        for p in [n for n in all_ns if "/" not in n]:
            sub = [n for n in all_ns if n.startswith(p + "/")]
            rules = sum(1 for n in [p, *sub] if (root() / "rules" / n / ".htaccess").is_file())
            prefixes.append({"ns": p, "owners": owners_of(p), "children": len(sub), "rules": rules})
        mine = []
        if g.user:
            mine = [n for n in all_ns if g.user["username"] in
                    Owners.from_yaml(read(n, "OWNERS.yaml")).accounts]
        return render_template("index.html", prefixes=prefixes, mine=mine,
                               deployed=gitops.deployed(), state=gitops.state(),
                               settings=settings)

    @app.get("/ns/<path:path>")
    def namespace(path):
        ns = get_ns(path)
        if not ns_exists(ns):
            abort(404)
        base = root() / "rules" / ns
        children = sorted(d.name for d in base.iterdir() if d.is_dir() and not d.name.startswith("."))
        own = Owners.from_yaml(read(ns, "OWNERS.yaml"))
        eff = owners_of(ns)
        last = ""
        try:
            last = gitops.git(root(), "log", "-1", "--format=%h · %ci · %an · %s", "--", f"rules/{ns}")
        except GitError:
            pass
        return render_template(
            "namespace.html", ns=ns, children=children, own=own, eff=eff,
            files={n: read(ns, n) for n in (*EDITABLE, "OWNERS.yaml")}, last=last,
            can_edit=can_edit(ns), can_children=can_manage_children(ns),
            can_owners=can_manage_children(parent(ns)) if "/" in ns else is_rule_admin(g.user),
            w3id_snip=snippet(ns, public_url()) if "/" not in ns else None)

    @app.post("/ns/<path:path>/test")
    def run_ns_tests(path):
        ns = get_ns(path)
        rep = gitops.candidate_report({}).filtered(ns)
        return render_template("report.html", ns=ns, rep=rep, title=f"Tests for {ns}")

    # ------------------------------------------------------------ resolve
    @app.route("/resolve", methods=["GET", "POST"])
    def resolve():
        pid = (request.values.get("pid") or "").strip()
        accept = request.values.get("accept_custom") or request.values.get("accept") or ""
        hops, error = [], None
        if pid:
            try:
                hops = trace(pid, accept)
            except ValueError as e:
                error = str(e)
        return render_template("resolve.html", pid=pid, accept=accept, hops=hops, error=error,
                               presets=ACCEPT_PRESETS)

    def trace(pid: str, accept: str, max_hops=6):
        pub = public_url().rstrip("/")
        if not re.match(r"^https?://", pid):
            pid = f"https://w3id.org/{pid.lstrip('/')}"
        u = urlparse(pid)
        if not u.netloc:
            raise ValueError("not a URL")
        hops, url = [], pid
        headers = {"User-Agent": "pid-relay-resolver"}
        if accept:
            headers["Accept"] = accept
        for _ in range(max_hops):
            fetch, via = url, None
            if not re.match(r"^https?://", url):
                hops.append({"url": url, "status": None, "error": "not an http(s) URL; stopped"})
                break
            if pub and url.startswith(pub + "/pid/"):
                fetch, via = redirector + url[len(pub):], "relay"   # ask the redirector directly
            elif urlparse(url).netloc in ("w3id.org", "www.w3id.org"):
                via = "w3id"
            hop = {"url": url, "via": via}
            if via != "relay" and not _public_host(urlparse(url).hostname):
                hop.update(status=None, error="target is not a public internet address; not fetched")
                hops.append(hop)
                break
            try:
                r = requests.get(fetch, headers=headers, allow_redirects=False, timeout=8, stream=True)
                hop.update(status=r.status_code, location=r.headers.get("Location"),
                           ctype=r.headers.get("Content-Type"), vary=r.headers.get("Vary"))
                r.close()
            except requests.RequestException as e:
                hop.update(status=None, error=str(e.__class__.__name__) + ": " + str(e)[:200])
                hops.append(hop)
                break
            hops.append(hop)
            loc = hop.get("location")
            if not (300 <= (hop["status"] or 0) < 400 and loc):
                break
            url = requests.compat.urljoin(url, loc)
        return hops

    # --------------------------------------------------------------- edit
    @app.route("/edit/<path:path>", methods=["GET", "POST"])
    @login_required
    def edit(path):
        ns = get_ns(path)
        if not ns_exists(ns):
            abort(404)
        if not can_edit(ns):
            abort(403)
        own_ok = can_manage_children(parent(ns)) if "/" in ns else is_rule_admin(g.user)
        names = list(EDITABLE) + (["OWNERS.yaml"] if own_ok else [])
        files = {n: read(ns, n) or "" for n in names}
        rep = None
        if request.method == "POST":
            files = {n: request.form.get(n, "").replace("\r\n", "\n") for n in names}
            changes = {f"rules/{ns}/{n}": (files[n] if files[n].strip() else None) for n in names}
            if "OWNERS.yaml" in names and changes[f"rules/{ns}/OWNERS.yaml"] is None and "/" not in ns:
                changes.pop(f"rules/{ns}/OWNERS.yaml")
            rep = gitops.candidate_report(changes)
            rep_ns = rep.filtered(ns)
            if request.form.get("action") == "submit":
                summary = (request.form.get("summary") or "").strip() or f"Update rules for {ns}"
                if not rep_ns.ok:
                    flash("Not submitted: fix the problems below first.", "error")
                else:
                    return _submit(ns, summary[:120], changes)
            return render_template("edit.html", ns=ns, files=files, names=names, rep=rep_ns,
                                   summary=request.form.get("summary", ""))
        return render_template("edit.html", ns=ns, files=files, names=names, rep=None, summary="")

    def _submit(ns, summary, changes):
        try:
            res = gitops.submit(g.user["username"], g.user["name"], ns, summary, changes)
        except (GitError, ValueError) as e:
            flash(f"Submission failed: {e}", "error")
            return redirect(url_for("namespace", path=ns)) if ns else redirect(url_for("index"))
        db.add_submission(username=g.user["username"], ns=ns or "(root)", summary=summary, **res)
        if res["mode"] == "pr":
            flash(f"Pull request opened: {res['pr_url']}. It goes live once it is merged.", "ok")
        else:
            flash("Committed. It goes live after the next sync (normally within a minute).", "ok")
        return redirect(url_for("submissions"))

    @app.route("/new/<path:path>", methods=["GET", "POST"])
    @app.route("/new/", defaults={"path": ""}, methods=["GET", "POST"])
    @login_required
    def new_child(path):
        ns = get_ns(path)
        if ns and not ns_exists(ns):
            abort(404)
        if not can_manage_children(ns):
            abort(403)
        form = {"name": "", "accounts": "", "github": "", "readme": ""}
        if request.method == "POST":
            form = {k: request.form.get(k, "").strip() for k in form}
            name = form["name"]
            child = f"{ns}/{name}" if ns else name
            if not valid_segment(name):
                flash("Use letters, digits, '.', '_' or '-' (start with a letter or digit).", "error")
            elif ns_exists(child):
                flash("That namespace already exists.", "error")
            else:
                owners = _owners_yaml(_split(form["accounts"]), _split(form["github"]))
                changes = {f"rules/{child}/OWNERS.yaml": owners}
                if form["readme"]:
                    changes[f"rules/{child}/README.md"] = f"# {child}\n\n{form['readme']}\n"
                rep = gitops.candidate_report(changes)
                if not rep.filtered(child).ok:
                    return render_template("report.html", ns=child, rep=rep.filtered(child),
                                           title="Cannot create namespace")
                return _submit(child, f"Create namespace {child}", changes)
        return render_template("new.html", ns=ns, form=form)

    @app.get("/submissions")
    @login_required
    def submissions():
        rows = db.submissions(None if g.user["is_admin"] else g.user["username"])
        status = {}
        for r in rows[:30]:
            if r["pr_number"] and r["state"] not in ("merged", "closed"):
                st = gitops.pr_status(r["pr_number"])
                if st.get("state") in ("merged", "closed"):
                    db.update_submission(r["id"], state=st["state"])
                status[r["id"]] = st
        return render_template("submissions.html", rows=rows, status=status)

    # -------------------------------------------------------------- admin
    @app.route("/admin/users", methods=["GET", "POST"])
    @admin_required
    def admin_users():
        created = []
        if request.method == "POST":
            act = request.form.get("action")
            uname = request.form.get("username", "").strip().lower()
            if act == "create":
                if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,39}", uname):
                    flash("Account names: 2-40 lowercase letters, digits, '.', '_' or '-'.", "error")
                elif db.user(uname):
                    flash("That account already exists.", "error")
                else:
                    pw = db.create_user(uname, request.form.get("name", "").strip(),
                                        is_admin=bool(request.form.get("is_admin")))
                    created.append((uname, pw))
            elif act in ("reset", "toggle", "admin") and db.user(uname):
                if uname == g.user["username"] and act != "reset":
                    flash("You cannot change your own status.", "error")
                elif act == "reset":
                    pw = gen_password()
                    db.set_password(uname, pw, must_change=True)
                    created.append((uname, pw))
                elif act == "toggle":
                    db.update_user(uname, active=0 if db.user(uname)["active"] else 1)
                else:
                    db.update_user(uname, is_admin=0 if db.user(uname)["is_admin"] else 1)
        return render_template("admin_users.html", users=db.users(), created=created)

    @app.route("/admin/groups", methods=["GET", "POST"])
    @admin_required
    def admin_groups():
        """Yearly course set-up: N group namespaces + one UI account per group."""
        form = {"parent": "", "count": "10", "pattern": "group{:02d}", "start": "1", "account_prefix": ""}
        if request.method == "POST":
            form = {k: request.form.get(k, "").strip() for k in form}
            try:
                par = norm_ns(form["parent"])
                count, start = int(form["count"]), int(form["start"])
                if not par or not (1 <= count <= 200):
                    raise ValueError("give a parent namespace and 1-200 groups")
                names = [form["pattern"].format(i) for i in range(start, start + count)]
                if not all(valid_segment(n) for n in names):
                    raise ValueError("the pattern produces invalid names")
            except (ValueError, IndexError, KeyError) as e:
                flash(f"Invalid input: {e}", "error")
                return render_template("admin_groups.html", form=form)
            if not can_edit(par):
                flash(f"Your account is not an owner of {par} (or a parent), so the change would be "
                      f"rejected. Add it to OWNERS.yaml or to admins.accounts in config/platform.yaml.", "error")
                return render_template("admin_groups.html", form=form)
            # Account names must be unique across years: default prefix is the
            # last segment of the parent, e.g. FAIR-course-UT/2026 -> "2026-group01".
            prefix_acct = form["account_prefix"] or par.rsplit("/", 1)[-1] + "-"
            prefix_acct = re.sub(r"[^a-z0-9._-]", "-", prefix_acct.lower())
            changes, accounts = {}, []
            for n in names:
                acct = f"{prefix_acct}{n}".lower()
                child = f"{par}/{n}"
                if ns_exists(child):
                    continue
                if db.user(acct):
                    flash(f"Account '{acct}' already exists. Choose another account prefix.", "error")
                    return render_template("admin_groups.html", form=form)
                changes[f"rules/{child}/OWNERS.yaml"] = _owners_yaml([acct], [])
                accounts.append((acct, child))
            if not changes:
                flash("All of these namespaces already exist.", "error")
                return render_template("admin_groups.html", form=form)
            rep = gitops.candidate_report(changes)
            if not rep.filtered(par).ok:
                return render_template("report.html", ns=par, rep=rep.filtered(par), title="Cannot create groups")
            try:
                res = gitops.submit(g.user["username"], g.user["name"], par,
                                    f"Create {len(changes)} group namespaces under {par}", changes)
            except GitError as e:
                flash(f"Submission failed: {e}", "error")
                return render_template("admin_groups.html", form=form)
            db.add_submission(username=g.user["username"], ns=par,
                              summary=f"Create {len(changes)} group namespaces", **res)
            creds = [(acct, child, db.create_user(acct, f"Group {child}")) for acct, child in accounts]
            session["last_creds"] = creds
            return render_template("admin_groups_done.html", creds=creds, res=res, par=par)
        return render_template("admin_groups.html", form=form)

    @app.get("/admin/groups/credentials.csv")
    @admin_required
    def group_creds_csv():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["account", "namespace", "w3id", "initial_password"])
        for acct, child, pw in session.pop("last_creds", []):
            w.writerow([acct, child, w3id_url(child), pw])
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=pid-relay-accounts.csv"})

    @app.post("/admin/sync")
    @admin_required
    def admin_sync():
        st = gitops.sync()
        flash("Sync finished: " + (st.get("error") or "up to date"), "error" if st.get("error") else "ok")
        return redirect(url_for("index"))

    @app.get("/admin/rejected")
    @admin_required
    def admin_rejected():
        st = gitops.state()
        return render_template("rejected.html", st=st)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "deployed": gitops.deployed().get("sha"), "sync_error": gitops.state().get("error")}

    @app.errorhandler(403)
    def e403(e):
        return render_template("error.html", code=403, msg="You are not an owner of this namespace."), 403

    @app.errorhandler(404)
    def e404(e):
        return render_template("error.html", code=404, msg="Not found."), 404

    @app.errorhandler(400)
    def e400(e):
        return render_template("error.html", code=400, msg=getattr(e, "description", "Bad request")), 400

    return app


def _public_host(host: str | None) -> bool:
    """True if every address of host is a public unicast address (SSRF guard)."""
    import ipaddress
    import socket

    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return True  # let the request itself report the DNS failure
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            return False
    return True


def _split(s: str) -> list[str]:
    return [x for x in re.split(r"[\s,;]+", s.lower()) if x]


def _owners_yaml(accounts, github) -> str:
    fmt = lambda xs: "[" + ", ".join(xs) + "]"  # noqa: E731
    return f"github: {fmt(github)}\naccounts: {fmt(accounts)}\n"


def _fmt_time(ts):
    if not ts:
        return "never"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _persistent_secret(data: Path) -> str:
    p = data / "secret.key"
    if p.exists():
        return p.read_text().strip()
    data.mkdir(parents=True, exist_ok=True)
    s = secrets.token_hex(32)
    p.write_text(s)
    os.chmod(p, 0o600)
    return s
