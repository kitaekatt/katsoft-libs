# Katsoft libraries

This repository owns standalone Python libraries, their tests, and their PyPI release tools.
The libraries work without an AI harness or plugin installation.

## Development

Work on the existing dev branch. Scope commits to your own files.
Keep the libraries usable from a plain Python installation without an AI harness.
Keep plugin integrations in the plugins-kit repository.

Run `uv run --extra dev pytest` after code changes.
Read `docs/reference/package-release.md` before changing release code or releasing a package.
Package releases require the user's explicit publication approval.

The extraction commit retains plugins-kit ancestry and original release commit IDs.
Historical plugin files remain in that ancestry. The current tree contains library development files only.
