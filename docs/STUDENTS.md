# Persistent identifiers for your project

Your group gets its own namespace of W3ID identifiers, for example

```
https://w3id.org/FAIR-course-UT/2026/group07/
```

Everything after that base is yours to design (`dataset/rivers`,
`vocab/River`, …). You decide where each identifier redirects to, and
whether the target depends on what the client asks for (HTML for people, RDF
for machines).

You write the rules in exactly the syntax w3id.org uses (Apache `.htaccess`),
and you submit them the way you would to w3id.org, as a pull request on GitHub.
The difference is where they go. Instead of
[perma-id/w3id.org](https://github.com/perma-id/w3id.org), your pull request
goes to the course repository
[luizbonino/pid-relay](https://github.com/luizbonino/pid-relay). w3id.org
forwards the whole `FAIR-course-UT` namespace there. Your rules are checked
automatically, including with w3id.org's own checker, and go live as soon as
they pass. Nobody has to review them by hand, and a mistake cannot break
anybody else's identifiers.

Your folder is `rules/FAIR-course-UT/2026/groupNN/`, where `NN` is your Canvas
group number with two digits. Canvas "Group 7" is `group07`.

## 1. Claim your folder with your first pull request

1. Fork [luizbonino/pid-relay](https://github.com/luizbonino/pid-relay) on
   GitHub, and clone your fork.
2. In `rules/FAIR-course-UT/2026/groupNN/`, add a `.htaccess` with your rules
   and a `tests.yaml` with the redirects you expect (sections 2 and 3).
3. **If you work in a pair**, also edit `OWNERS.yaml` in that folder and put
   both GitHub logins in the list, e.g. `github: [your-login, partner-login]`.
   Working alone, leave `OWNERS.yaml` as it is.
4. Commit, push to your fork, and open a pull request to `luizbonino/pid-relay`.

The first pull request into an unclaimed group folder **claims** it, just like
claiming a namespace on w3id.org. When it is merged, your GitHub login (and your
partner's, if you listed it) is recorded in the folder's `OWNERS.yaml`. From
then on, only you can change that folder. Claim your own group number only,
one folder per person.

## 2. Write rules

Your folder needs two files.

* **`.htaccess`**: the redirect rules.
* **`tests.yaml`**: the redirects you expect. Required. Every rule should
  have at least one test.

A complete worked example is in `rules/FAIR-course-UT/2026/group00/`.

### How rules match

A `RewriteRule` pattern is matched against the part of the identifier
**after your namespace**, without a leading slash.

| Identifier | Text the pattern sees |
|---|---|
| `https://w3id.org/FAIR-course-UT/2026/group07/` | (empty) |
| `https://w3id.org/FAIR-course-UT/2026/group07/dataset/rivers` | `dataset/rivers` |

```apache
RewriteEngine on

# Turtle for clients that ask for it
RewriteCond %{HTTP_ACCEPT} text/turtle
RewriteRule ^dataset/([a-z0-9-]+)$ https://example.org/data/$1.ttl [R=303,L]

# JSON-LD
RewriteCond %{HTTP_ACCEPT} application/ld\+json
RewriteRule ^dataset/([a-z0-9-]+)$ https://example.org/data/$1.jsonld [R=303,L]

# Everyone else (browsers) gets the HTML page
RewriteRule ^dataset/([a-z0-9-]+)$ https://example.org/data/$1.html [R=303,L]
```

A request for your namespace without the trailing slash
(`…/group07`) is first answered with a 301 to `…/group07/`, and then your `^$`
rule runs. w3id.org behaves the same way.

Rules are tried from top to bottom and the first match wins, because of
`L`. Put the content-type specific rules **before** the generic one.
A `RewriteCond` applies only to the `RewriteRule` right after it.

`$1`, `$2`, … insert what the parentheses in the pattern captured.

### Status codes

| Code | Use it for |
|---|---|
| `303` | The identifier names a thing (a dataset, a concept) and you redirect to a document *about* it. The usual choice for linked data. |
| `302` | A temporary redirect to the same resource |
| `307` | Like 302, but the client must keep the HTTP method |
| `301` | Permanent. Clients cache it, possibly forever. Avoid it while you are still developing. |

### What is not allowed

To keep the service safe, only redirect-related directives are accepted:
`RewriteEngine`, `RewriteCond`, `RewriteRule`, `RewriteBase`, `Redirect`,
`RedirectMatch`, `Header` (only `Vary`, `Link`, `Cache-Control`,
`Access-Control-*`), `AddType` and `<IfModule>`. You do not need any `Options`
lines. They are accepted, but they do nothing for redirects.

Your files are also checked with [w3id.org's own
checker](https://w3id.org/docs/rules/). Its findings appear in the check's
output marked `[w3id.org …]`, each with a link explaining the rule. Errors make
the check fail, and warnings are worth fixing too. The most common ones are
comments at the end of a rule line (Apache has no inline comments), `http://`
targets, and `^(.+)/?$` patterns that swallow the slash.

Targets must be **absolute URLs** (`https://…`). Prefer `RewriteRule` over
`Redirect`/`RedirectMatch`. Those match the full URL path, which differs
between this service (`/pid/…`) and w3id.org.

## 3. Write tests

```yaml
tests:
  - pid: https://w3id.org/FAIR-course-UT/2026/group07/dataset/rivers
    accept: text/turtle
    status: 303
    location: https://example.org/data/rivers.ttl

  - path: dataset/rivers            # same thing, relative to your namespace
    accept: text/html
    status: 303
    location: https://example.org/data/rivers.html

  - path: dataset/Not_Valid
    note: names with capitals are not identifiers   # shown in reports
    status: 404
```

Each test is one request. Use either `pid` (the full identifier) or
`path` (relative to your namespace), optionally `accept`. Give the expected
`status` and/or `location` (exact) or `location_regex`.

## 4. Follow your pull request

The pull request runs a check called **validate**. It lints your `.htaccess`,
runs w3id.org's checker on it, starts a real Apache server with your rules,
runs every test in your `tests.yaml`, and checks that you own the folder.

* **Green:** the pull request is merged automatically within a minute or two,
  and your identifiers work a minute after that.
* **Red:** open *Details* next to the failed check, then the step **Lint and
  test all rules on Apache httpd**. Each problem is listed with its file and
  line number, and failing tests show what they expected and what they got. Fix
  the files on the same branch and push again. The pull request is checked
  again on its own.

The first time you open a pull request to this repository, GitHub may hold the
check until the course team approves it. That only happens once.

For your next change, first bring your fork up to date (*Sync fork* on your
fork's GitHub page, then `git pull`), so you start from what is live. Then open
a new pull request the same way.

## 5. Check the result

The relay's **Resolve** page follows an identifier hop by hop, with any
`Accept` header. It shows w3id.org, then the course service, then your target:
`https://fair-testbed.eemcs.utwente.nl/pid-manager/resolve`

From a terminal:

```sh
curl -sI -H "Accept: text/turtle" https://w3id.org/FAIR-course-UT/2026/group07/dataset/rivers
curl -sIL -H "Accept: text/turtle" https://w3id.org/FAIR-course-UT/2026/group07/dataset/rivers   # follow all hops
```

## Trying your rules locally (optional)

If you have Apache httpd and Python with PyYAML installed:

```sh
python3 -m pidrelay test --only FAIR-course-UT/2026/group07
```

This runs the same checks as the pull request, except w3id.org's checker,
which needs extra setup (see the README).
