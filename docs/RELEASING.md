# Releasing sia-devtools to PyPI

Publishing is done by `.github/workflows/publish.yml` when you publish a GitHub release. It uses PyPI **trusted
publishing**: GitHub proves to PyPI which workflow is running, so no PyPI password or API token is stored anywhere.

## One-time setup (owner only)

1. Sign in to <https://pypi.org> (create an account and turn on two-factor authentication if you don't have one).
2. Go to **Your account → Publishing → Add a new pending publisher** and fill in:
   - PyPI project name: `sia-devtools`
   - Owner: `SrijithLegend`
   - Repository name: `Self-Improving-Agent`
   - Workflow name: `publish.yml`
   - Environment name: `pypi`
3. On GitHub: **Settings → Environments → New environment** named `pypi`. Optionally add yourself as a required
   reviewer, so every publish waits for your click.

On 2026-10-22 the name `sia-devtools` was not registered on PyPI. PyPI can still refuse a name that is too close to an
existing project; that only shows at the first upload.

## Each release

1. Bump `version` in `pyproject.toml` (and `__version__` in `sia_devtools/client.py`), merge to `main`.
2. On GitHub: **Releases → Draft a new release**, tag `v<version>` (for example `v0.1.0`) on `main`, publish.
3. The workflow checks the tag matches the version, runs the tests, builds, runs `twine check` and uploads.
4. Check <https://pypi.org/project/sia-devtools/>, then in a clean environment:
   `pip install "sia-devtools[server]" && sia --help`.

A PyPI version can never be uploaded twice, even after deleting it, so a mistake means bumping the version.

## Check a build locally

```bash
pip install build twine
python -m build && python -m twine check dist/*
```
