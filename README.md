# pid-relay

Run W3ID-style redirect rules on a server you control.

Each W3ID prefix gets **one** rule at w3id.org that never changes. It forwards
every identifier under the prefix to this service. Here, the rules for that
prefix live in a Git repository, are written in the same `.htaccess` syntax as
on w3id.org, and are checked automatically before they go live. Mistakes stay
on our side and the W3ID maintainers never see them.

```
https://w3id.org/FAIR-course-UT/2026/group07/dataset/rivers
   │  ids/FAIR-course-UT/.htaccess in perma-id/w3id.org  (one catch-all rule, 302)
   ▼
https://fair-testbed.eemcs.utwente.nl/pid/FAIR-course-UT/2026/group07/dataset/rivers
   │  rules/FAIR-course-UT/2026/group07/.htaccess  (the group's own rules)
   ▼
https://group07.example.org/datasets/rivers.ttl
```

One service handles any number of W3ID prefixes (`FAIR-course-UT`, `fdt`, …),
course years and applications.

## What is in here

| Path | What it is |
|---|---|
| `rules/<prefix>/…` | The rules tree. Mirrors w3id.org: `rules/X/a/.htaccess` answers `https://w3id.org/X/a/…` |
| `rules/**/OWNERS.yaml` | Who may change a namespace and everything below it |
| `rules/**/tests.yaml` | Expected redirects. Required next to every `.htaccess` |
| `config/platform.yaml` | Public URL, platform admins, the UI's GitHub bot account |
| `redirector/` | Apache httpd configuration and image for the redirect service |
| `ui/` | Management UI (Flask), with its image |
| `pidrelay/` | Validation toolkit used by CI, the UI and the server |
| `deploy/` | Docker Compose stack with Caddy (Let's Encrypt) for the server |
| `.github/workflows/` | `validate` (lint, tests, ownership) and `automerge` |
| `docs/STUDENTS.md` | Guide to hand to students |
| `docs/w3id/FAIR-course-UT.htaccess` | The file to submit to w3id.org for the course prefix |

## How a change goes live

1. Someone edits rules, either in the management UI or as a pull request on GitHub.
2. **validate** runs on the same Apache build as production. It lints every
   `.htaccess`, also runs [w3id.org's own contribution
   checker](https://github.com/perma-id/w3id.org/tree/master/tools/check) on it
   (so files here meet the rules w3id.org enforces), starts a throw-away httpd,
   runs every `tests.yaml`, probes every directory for files Apache rejects, and
   checks that the author owns the namespaces the change touches.
3. **automerge** merges the pull request if validate passed and nothing outside
   `rules/` changed. Anything else waits for an admin.
4. The server fetches `main` every minute, validates the new commit again,
   and only then checks it out. A broken commit is never deployed. The previous
   version keeps running and the UI shows why.

Apache itself is locked down as a second line of defence. Contributors can only
use redirect directives (`RewriteEngine/Cond/Rule/Base`, `Redirect*`, a few
`Header`s, `AddType`, `<IfModule>`, harmless `Options`). `mod_proxy`, CGI and
file serving are not loaded. A bad file only breaks its own directory (HTTP 500
there), never the rest. Where it matters for rules, the relay behaves like the
w3id.org server configuration in `tools/server/` of that repository. A
directory URL without its trailing slash gets a 301 to the slashed form, every
response is CORS-open, and redirects carry no validators.

### Ownership

`OWNERS.yaml` lists `accounts` (management UI) and `github` (logins for
pull requests). Owners of a namespace may change everything below it. A
namespace's own `OWNERS.yaml` is decided by the owners of its parent, so nobody
can add themselves. New top-level prefixes and anything outside `rules/` are
admin-only. Ownership is always read from the base branch, never from the
change itself.

## Management UI

`https://fair-testbed.eemcs.utwente.nl/pid-manager/`

* **Namespaces**: every prefix, its owners, rules, tests, history, and the rule
  to register at w3id.org.
* **Resolve**: follow an identifier hop by hop (w3id.org, relay, target) with
  any `Accept` header.
* **Edit**: edit `.htaccess`, `tests.yaml`, `README.md` (and `OWNERS.yaml` for
  owners of the parent). A rule helper turns "path `dataset/{id}` as
  `text/turtle` goes to `https://…/{id}.ttl`" into the Apache lines plus a
  test. *Validate* runs the full check on a real httpd. *Submit* opens a pull
  request (or commits directly, if no GitHub token is configured).
* **Course groups** (admins): create `group01…groupNN` under e.g.
  `FAIR-course-UT/2027` in one change, with one account per group
  (`2027-group01`, …) and a CSV of initial passwords.
* **Accounts** (admins): local accounts, password resets, disabling.

Accounts are local for now (passwords hashed in SQLite). The login is isolated
in `ui/app.py`, so OIDC (Keycloak, SURFconext, LS-AAI) can replace it later.

## Setting it up

### 1. GitHub repository

1. Push this repository to GitHub, e.g. `YOUR-ORG/pid-relay`.
2. Edit `config/platform.yaml`. Put your GitHub login under `admins.github`
   and the login of a machine account under `ui_bot`. Also replace
   `CHANGE-ME-your-github-login` in `rules/*/OWNERS.yaml`.
3. Create a fine-grained personal access token for the machine account, for this
   repository only, with **Contents** and **Pull requests** set to read/write.
   (It must be a personal token. Pull requests opened with the built-in
   `GITHUB_TOKEN` do not trigger workflows.)
4. In *Settings → Actions → General*, check the setting for workflows on pull
   requests from forks by outside collaborators. If runs from first-time
   contributors need approval, a student's first pull request waits until you
   approve its run.
5. Optional: protect `main` (require the `validate` check, block direct
   pushes). The UI bot only ever pushes branches.

Students without GitHub accounts can do everything through the UI.

### 2. Server

On `fair-testbed.eemcs.utwente.nl` (needs Docker with the compose plugin):

```sh
git clone https://github.com/YOUR-ORG/pid-relay.git /opt/pid-relay
cd /opt/pid-relay/deploy
cp .env.example .env      # fill in host, e-mail, repository and token
docker compose up -d --build
docker compose logs ui | grep "initial account"   # if no ADMIN_PASSWORD was set
```

**Firewall**: inbound TCP **80** and **443** must be reachable from the
internet. Let's Encrypt validates on port 80, and w3id.org users are
redirected to port 443. Outbound HTTPS to GitHub is needed for syncing.

The rules are pulled from GitHub at run time. The server's own checkout of this
repository is only used to build the images, so update it (`git pull &&
docker compose up -d --build`) when the code changes, not when rules change.

Other services on the same host go into `deploy/Caddyfile` as further
`handle` blocks.

### 3. Register a prefix at w3id.org

Identifiers live under `ids/` in [perma-id/w3id.org](https://github.com/perma-id/w3id.org)
since October 2026. For `FAIR-course-UT` the ready file is
`docs/w3id/FAIR-course-UT.htaccess`. It keeps the existing header and contact
block and replaces the empty rule set with the forwarding rule:

```apache
RewriteEngine on
RewriteRule ^(.*)$ https://fair-testbed.eemcs.utwente.nl/pid/FAIR-course-UT/$1 [R=302,L]
```

Submit it as `ids/FAIR-course-UT/.htaccess`. It passes w3id.org's checker
without findings, and with w3id.org's Apache configuration
`/FAIR-course-UT/2026/group07/x?y=1` answers `302` to
`https://fair-testbed.eemcs.utwente.nl/pid/FAIR-course-UT/2026/group07/x?y=1`.
The only remaining warnings are about the existing `Readme.md` (w3id.org
prefers `README.md`, ending with a newline). Fixing it in the same pull
request is optional.

For another prefix, the prefix page in the UI shows the file, or run
`python3 -m pidrelay w3id-snippet <prefix>`. If a prefix has sub-directories on
w3id.org with their own `.htaccess`, those keep answering for their paths. Move
what should be kept into `rules/` here and delete them from w3id.org in the same
pull request.

### Yearly routine

1. *Course groups* → parent `FAIR-course-UT/<year>`, number of groups.
2. Hand each group its account and the CSV password. They change it at first login.
3. Point students at `docs/STUDENTS.md`.

Last year's identifiers keep resolving. To retire a year, disable its accounts.
The rules stay.

## Working locally

```sh
python3 -m pidrelay test                  # everything, on a local Apache httpd
python3 -m pidrelay test --only FAIR-course-UT/2026/group00
python3 -m pidrelay lint                  # static checks only

# optional: include w3id.org's checker (needs Node 22)
git clone --depth 1 --filter=blob:none --sparse https://github.com/perma-id/w3id.org.git ~/w3id
git -C ~/w3id sparse-checkout set --no-cone /tools/check/ /.w3id-check.yaml
(cd ~/w3id/tools/check && npm ci)
export PIDRELAY_W3ID_CHECK=~/w3id/tools/check/bin/w3id-check.js
python3 -m unittest discover -s tests     # toolkit self-tests
```

The runner finds `httpd`/`apache2` on the `PATH` or in the usual places. Set
`PIDRELAY_HTTPD` and `PIDRELAY_MODULES` otherwise.

Running the UI without Docker, against a local bare repository and no GitHub:

```sh
export PIDRELAY_DATA=/tmp/pr/data PIDRELAY_DEPLOY=/tmp/pr/deploy \
       PIDRELAY_REMOTE=/path/to/bare.git PIDRELAY_COOKIE_SECURE=0 \
       PIDRELAY_PUBLIC_URL=https://fair-testbed.eemcs.utwente.nl
python3 -c "from werkzeug.serving import run_simple; from ui.wsgi import application; run_simple('127.0.0.1', 8000, application)"
# http://127.0.0.1:8000/pid-manager/
```

## UI configuration

| Variable | Default | Meaning |
|---|---|---|
| `PIDRELAY_REMOTE` | (required) | Git URL of this repository |
| `PIDRELAY_BRANCH` | `main` | Branch that is deployed |
| `PIDRELAY_GITHUB_REPO`, `PIDRELAY_GITHUB_TOKEN` | empty | Both set means pull-request mode, otherwise direct commits |
| `PIDRELAY_PUBLIC_URL` | from `platform.yaml` | Public base URL |
| `PIDRELAY_SYNC_INTERVAL` | `60` | Seconds between syncs |
| `PIDRELAY_ADMIN_PASSWORD` | random | Password of the initial `admin` account |
| `PIDRELAY_SECRET_KEY` | generated in `/data` | Session signing key |
| `PIDRELAY_UI_BASE` | `/pid-manager` | URL path of the UI |
| `PIDRELAY_COOKIE_SECURE` | `1` | Set `0` only for local HTTP testing |
| `PIDRELAY_W3ID_CHECK` | set in the image | Path to w3id.org's `w3id-check.js`. Unset means that check is skipped |
