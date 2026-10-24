# Package releases

This repository releases the distributions in `packages/registry.toml` to PyPI.
Each distribution has an independent version in its `pyproject.toml`.
`packages/released.json` records verified artifacts and their original source commits.
Historical entries retain their original plugins-kit URLs and commit IDs.

## Prepare a release

1. Update the distribution version after changing its build inputs.
2. Run `uv run --extra dev pytest`.
3. Run `WHEEL_CLOSURE_SOURCE=local uv run --extra dev pytest tests/packaging`.
4. Commit and push the reviewed changes to `dev`.
5. Run `uv run --extra dev python scripts/release.py` to inspect the release plan.
6. Obtain the user's publication approval.
7. Run `uv run --extra dev python scripts/release.py --execute`.

The command requires a clean `dev` checkout. It releases packages in dependency order.
It builds from committed inputs, pushes release tags, and waits for GitHub Actions.
The `release-packages.yml` workflow builds the tagged source and uploads through the `pypi` environment.
Set this repository's default branch to `dev` before its first release.
PyPI trusted publishers identify repository `kitaekatt/katsoft-libs`, workflow `release-packages.yml`, and environment `pypi`.

The command downloads each uploaded wheel and compares it with a deterministic rebuild.
It also performs an isolated import check before recording and pushing the ledger entry.
A failed check stops the release. Tags are never moved or deleted by the command.

## Resume a release

If a workflow fails, inspect its log with `gh run view <id> --log-failed`.
Correct the cause before running `gh run rerun <id>`.
If no workflow run exists, dispatch `gh workflow run release-packages.yml --ref dev -f tag=<tag>`.
Run the release command again after the workflow succeeds.
The command observes the existing tag, workflow, PyPI artifact, and ledger before it changes state.
It never redispatches a failed workflow automatically.

## Build contract

`packages/_build/release_build.py` builds wheels from `git archive` inputs.
Build dependencies are pinned in `packages/_build/constraints.txt`.
`SOURCE_DATE_EPOCH` is the source commit time.
The hatchling hook writes the release marker and source URL into each wheel.
The wheel check requires the distribution name, version, tag, source commit, and URL to agree.

To build a committed package without uploading it:

```sh
uv run --extra dev python packages/_build/release_build.py build --repo . --commit HEAD --dist katsoft-llm-kit --out tmp/wheels
```

## Plugin consumers

Library releases do not publish plugins.
After a verified release, import its target data into the plugins-kit development checkout:

```sh
uv run --extra dev python scripts/import_package_targets.py --library-repo "$DEVROOT/katsoft-libs" --ref <verified-commit>
```

Run that command from plugins-kit. It checks the published versions and generates its local package-target artifact.
Review and push that artifact in plugins-kit. Plugin publication keeps its separate approval requirement.
Applications install the libraries directly from PyPI and need neither repository at runtime.
