"""Helpers for the w3id.org side."""


def snippet(prefix: str, public_url: str | None) -> str:
    """The forwarding rule for ids/<prefix>/.htaccess in perma-id/w3id.org.

    Passes the official w3id.org checker (tools/check) without findings once
    the maintainer lines are filled in.
    """
    base = (public_url or "https://fair-testbed.eemcs.utwente.nl").rstrip("/")
    return f"""# # /{prefix}/
#
# Every identifier under this namespace is forwarded to a redirect service
# where its rules are maintained and tested before they go live:
# {base}/pid/{prefix}/
# This file therefore does not change when those rules do.
#
# ## Contact
# This space is administered by:
#
# Your Name
# your.email@example.org
# GitHub username: your-github-username

RewriteEngine on
RewriteRule ^(.*)$ {base}/pid/{prefix}/$1 [R=302,L]
"""


def w3id_url(ns: str) -> str:
    return f"https://w3id.org/{ns}/"


def relay_url(ns: str, public_url: str | None) -> str:
    return f"{(public_url or '').rstrip('/')}/pid/{ns}/"
