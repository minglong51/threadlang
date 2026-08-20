# Releasing ThreadLang

ThreadLang publishes to PyPI from a GitHub Release through
[`.github/workflows/publish.yml`](.github/workflows/publish.yml). The workflow
uses trusted publishing; no PyPI API token is stored in the repository.

## Prepare and verify

Merge the release pull request only after all CI checks are green. Then start
from the exact, clean `main` commit that will be tagged. Run the verification
lane with Python 3.12, matching the publish workflow:

```bash
set -euo pipefail

git switch main
git pull --ff-only origin main

VERSION="$(python3.12 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
test "$(PYTHONPATH=src python3.12 -c 'import threadlang; print(threadlang.__version__)')" = "$VERSION"
test -z "$(git status --short)"

RELEASE_ENV="$(mktemp -d)/venv"
python3.12 -m venv "$RELEASE_ENV"
"$RELEASE_ENV/bin/python" -m pip install --upgrade pip
"$RELEASE_ENV/bin/python" -m pip install '.[dev,anthropic]'
"$RELEASE_ENV/bin/python" -m pytest -q
"$RELEASE_ENV/bin/ruff" check .
"$RELEASE_ENV/bin/ruff" format --check .
"$RELEASE_ENV/bin/mypy" src/threadlang
"$RELEASE_ENV/bin/bandit" -q -r src/threadlang
"$RELEASE_ENV/bin/pip-audit"

ARTIFACT_DIR="$(mktemp -d)"
"$RELEASE_ENV/bin/python" -m build --outdir "$ARTIFACT_DIR"
"$RELEASE_ENV/bin/python" -m twine check "$ARTIFACT_DIR"/*

SMOKE_ROOT="$(mktemp -d)"
python3.12 -m venv "$SMOKE_ROOT/venv"
"$SMOKE_ROOT/venv/bin/pip" install \
  "$ARTIFACT_DIR/threadlang-$VERSION-py3-none-any.whl"
(
  cd "$SMOKE_ROOT"
  test "$(env -u PYTHONPATH -u PYTHONHOME "$SMOKE_ROOT/venv/bin/threadlang" --version)" = \
    "threadlang $VERSION"
  env -u PYTHONPATH -u PYTHONHOME \
    "$SMOKE_ROOT/venv/bin/threadlang-serve" --help >/dev/null
  env -u PYTHONPATH -u PYTHONHOME \
    "$SMOKE_ROOT/venv/bin/support-triage" run \
      --ticket "release smoke" --dry-run --store "$SMOKE_ROOT/triage.db" >/dev/null
)
```

The project version in `pyproject.toml` and `threadlang.__version__` must match.
The release tag must exactly equal `v<project version>`; the publish workflow
rejects any other tag or a runtime/project version mismatch.

## Tag and publish

Confirm `HEAD` is the intended commit on `origin/main`, then create and push a
new lightweight tag and create a draft GitHub Release from that existing tag:

```bash
set -euo pipefail

VERSION="$(python3.12 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
test "$(PYTHONPATH=src python3.12 -c 'import threadlang; print(threadlang.__version__)')" = "$VERSION"
test -z "$(git status --short)"
git fetch origin main
test "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)"
git tag "v$VERSION"
git push origin "v$VERSION"
gh release create "v$VERSION" --verify-tag --generate-notes --draft
```

Inspect the draft release's generated notes and tag target:

```bash
set -euo pipefail

VERSION="$(python3.12 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
git show --no-patch --format=fuller "v$VERSION"
gh release view "v$VERSION" --json isDraft,tagName,targetCommitish,name,body
```

If the draft is correct, publish it explicitly:

```bash
set -euo pipefail

VERSION="$(python3.12 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
gh release edit "v$VERSION" --draft=false
```

Publishing starts, but does not immediately complete, the PyPI release. The
workflow runs `verify`, `build`, and `verify_artifact` first. Its final
`publish` job targets the protected `pypi` environment and waits for the
configured maintainer approval before exchanging its OIDC identity for a PyPI
upload.

## Verify the release

Watch the complete `Publish to PyPI` workflow and approve the `pypi`
deployment only after the preceding jobs pass. After PyPI reports the new
version, verify it from a fresh environment outside the repository:

```bash
set -euo pipefail

VERSION="$(python3.12 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
VERIFY_ROOT="$(mktemp -d)"
VERIFY_ENV="$VERIFY_ROOT/venv"
python3.12 -m venv "$VERIFY_ENV"
"$VERIFY_ENV/bin/pip" install "threadlang==$VERSION"
cd "$VERIFY_ROOT"
test "$(env -u PYTHONPATH -u PYTHONHOME "$VERIFY_ENV/bin/threadlang" --version)" = \
  "threadlang $VERSION"
env -u PYTHONPATH -u PYTHONHOME "$VERIFY_ENV/bin/threadlang-serve" --help >/dev/null
env -u PYTHONPATH -u PYTHONHOME "$VERIFY_ENV/bin/support-triage" run \
  --ticket "release smoke" --dry-run --store "$VERIFY_ROOT/triage.db" >/dev/null
```

PyPI versions are immutable. Never move or reuse a release tag. If a published
artifact needs correction, prepare and release a new patch version.
