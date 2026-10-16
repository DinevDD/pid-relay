# Rules tree

Each top-level directory is a W3ID prefix. `https://w3id.org/<prefix>/a/b` is
resolved by `rules/<prefix>/a/.htaccess` (or the nearest parent with a
`.htaccess`), exactly as on w3id.org. See `docs/STUDENTS.md`.
