# Persistent identifiers for your project

Your group gets its own namespace of W3ID identifiers, for example

```
https://w3id.org/FAIR-course-UT/2026/group07/
```

Everything after that base is yours to design (`dataset/rivers`,
`vocab/River`, …). You decide where each identifier redirects to, and
whether the target depends on what the client asks for (HTML for people, RDF
for machines).

You write the rules in exactly the syntax w3id.org uses (Apache `.htaccess`).
They are not sent to w3id.org. They run on the course's own redirect service,
which w3id.org forwards your namespace to. Your rules are tested automatically
before they go live, so a mistake cannot break anybody else's identifiers.

## 1. Log in

`https://fair-testbed.eemcs.utwente.nl/pid-manager/`

Use the account and initial password you received. You are asked to choose
a new password first. Under *Namespaces*, open your group's namespace and choose
**Edit rules**.

## 2. Write rules

You edit two files.

* **`.htaccess`**: the redirect rules.
* **`tests.yaml`**: the redirects you expect. Required. Every rule should
  have at least one test.

The **rule helper** at the top of the edit page writes both for you. Describe
the identifier path, an optional content type, the target, and the status
code. Then press *Add rule and test*.

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
`Access-Control-*`), `AddType` and `Options +FollowSymLinks`.

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

## 4. Validate and publish

* **Validate** runs everything on a real Apache server with your files and
  shows each problem with its line number, plus the result of every test.
* **Validate and open pull request** (or *publish*) only works when everything
  passes. Your change is then merged and goes live within a few minutes. Follow
  it under *Submissions*.

## 5. Check the result

Use **Resolve** with your identifier and an `Accept` header. It shows every
hop: w3id.org, then this service, then your target. From a terminal:

```sh
curl -sI -H "Accept: text/turtle" https://w3id.org/FAIR-course-UT/2026/group07/dataset/rivers
curl -sIL -H "Accept: text/turtle" https://w3id.org/FAIR-course-UT/2026/group07/dataset/rivers   # follow all hops
```

## Using GitHub instead of the UI

The rules live in a GitHub repository. If your GitHub login is listed in your
group's `OWNERS.yaml`, you can fork it, edit
`rules/FAIR-course-UT/<year>/<group>/`, and open a pull request. The same
checks run there. Pull requests that only touch namespaces you own are merged
automatically when the checks pass. To try your rules locally:

```sh
python3 -m pidrelay test --only FAIR-course-UT/2026/group07
```

This needs Apache httpd and PyYAML installed.
