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

## npm packages

`packages/llm-kit-config` is the npm package `@katsoft/llm-kit-config`.
`scripts/release.py` and `release-packages.yml` do not release it.
Its version is in `packages/llm-kit-config/package.json`.
Each version has an entry in `packages/llm-kit-config/CHANGELOG.md` that records which quality bar lines pass.

The release tag is `@katsoft/llm-kit-config@<version>`, for example `@katsoft/llm-kit-config@1.0.0`.
A push of this tag starts `.github/workflows/release-npm.yml`.
The tag contains `/`, and `*` in a GitHub filter pattern does not match `/`.
Thus the `*-v*` trigger of `release-packages.yml` does not match an npm tag, and the npm trigger does not match a PyPI tag.

The workflow uses npm trusted publishing (OIDC) from the GitHub environment `npm`, with provenance.
It installs from the lockfile (`npm ci`), runs `npm test` and `npm run check-pack`, and packs one tarball.
It publishes that tarball with `npm publish <tarball> --provenance --access public`.
Then it compares the registry's `dist.integrity` with the integrity of the packed tarball, and fails when they differ.
When the version is already on npm, the workflow does not publish.
It downloads the published tarball and compares its files with the packed files.
On both paths, the registry's `dist.integrity` must equal the packed tarball's integrity, or the job fails.
The job summary records the commit, the result and the integrity.

### Prepare an npm release

1. Change the version in `package.json` and `package-lock.json` (`npm version <new> --no-git-tag-version` in `packages/llm-kit-config`).
2. Add the `CHANGELOG.md` entry, with the result of each quality bar line.
3. In `packages/llm-kit-config`, run `npm ci`, `npm test` and `npm run check-pack`.
4. From the repository root, run `uv run --extra dev pytest`.
5. Commit and push the reviewed changes to `dev`.
6. Obtain the user's publication approval.

### First publish by hand (one time)

Trusted publishing can be set only on a package that exists on npm, so the user publishes version 1.0.0 by hand.

The npm account uses a security key (WebAuthn) for two-factor authentication. A one-time code (`--otp`) does not work: npm rejects it with EOTP. Use the web sign-in flow, in a real terminal. Without a terminal, npm cannot ask for the key. Examples: an agent background shell, or the Claude Code `!` prefix. There, npm exits with EOTP and hides the URL as `***`. An agent shell can get a terminal with `script -q <log> npm publish ...`.

1. On npmjs.com, use an account with two-factor authentication. Create the organization `katsoft` (free plan, public packages), unless the `@katsoft` scope exists.
2. Run `npm login --auth-type=web`. npm prints a URL, `https://www.npmjs.com/auth/cli/<id>`. Open it in a browser and approve with the security key.
3. Use a clean checkout of the reviewed commit: `git status` shows no changes.
4. Publish:

   ```sh
   cd packages/llm-kit-config
   npm ci
   npm test
   npm run check-pack
   npm pack --dry-run --json   # note the "integrity" value
   npm publish --access public --auth-type=web # prepack builds dist/ and copies the schemas
   npm view @katsoft/llm-kit-config@1.0.0 dist.integrity
   ```

   npm prints a new URL. Open it and click "Use security key". You can tick "Do not challenge npm publish, npm trust operations ... for the next 5 minutes". Then the next step needs no new approval.

   The package page can show the new version before the registry API does. `npm view` can return 404 for a few minutes after npm prints `+ @katsoft/llm-kit-config@1.0.0`. Wait, then run it again.

   The integrity from `npm view` must equal the value from `npm pack --dry-run`. If it does not, stop and report it.
5. Set the Trusted Publisher. Use the CLI (it needs the same security-key approval):

   ```sh
   npm trust github @katsoft/llm-kit-config --file release-npm.yml --repo kitaekatt/katsoft-libs --env npm --allow-publish --yes
   ```

   Or use the website: on the package page on npmjs.com, open Settings, Trusted Publisher, GitHub Actions. Enter organization or user `kitaekatt`, repository `katsoft-libs`, workflow filename `release-npm.yml`, environment `npm`.
6. In the same settings, set Publishing access to "Require two-factor authentication and disallow tokens".
7. In the GitHub repository settings, create the environment `npm`.
8. Tag the published commit and push the tag: `git tag @katsoft/llm-kit-config@1.0.0 <commit>` and `git push origin @katsoft/llm-kit-config@1.0.0`. The workflow finds 1.0.0 on npm, compares the files and records the release.

### Later npm releases

After the steps in "Prepare an npm release", tag the reviewed commit on `dev` and push the tag:

```sh
git tag @katsoft/llm-kit-config@<version> <commit>
git push origin @katsoft/llm-kit-config@<version>
```

If the workflow fails, read `gh run view <id> --log-failed`, correct the cause, and run `gh run rerun <id>`.
A rerun is safe: when the version is already on npm, the workflow only compares the files.
Never move or delete a pushed release tag.
