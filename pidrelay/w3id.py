"""Helpers for the w3id.org side."""


def snippet(prefix: str, public_url: str | None) -> str:
    base = (public_url or "https://fair-testbed.eemcs.utwente.nl").rstrip("/")
    return f"""# w3id.org/{prefix}/.htaccess
# All identifiers under https://w3id.org/{prefix}/ are resolved by pid-relay at
# {base}/pid/{prefix}/ . The rules themselves are maintained there.
# Contact: see README.md in this directory.
RewriteEngine on
RewriteRule ^(.*)$ {base}/pid/{prefix}/$1 [R=302,L]
"""


def w3id_url(ns: str) -> str:
    return f"https://w3id.org/{ns}/"


def relay_url(ns: str, public_url: str | None) -> str:
    return f"{(public_url or '').rstrip('/')}/pid/{ns}/"
