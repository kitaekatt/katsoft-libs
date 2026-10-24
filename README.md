# Katsoft libraries

Python libraries for LLM access, durable jobs, batch pipelines, and shared contracts.
Install the packages into your own environment:

```sh
pip install 'katsoft-llm-kit[sdk,jobs,config]' katsoft-contracts
```

The libraries work without Claude Code, Codex, bootstrap, or plugins-kit.
An AI harness is one optional backend. Applications choose when to call models.

Sources are under `packages/`. Each distribution has its own version and README.
For development, run `uv run --extra dev pytest`.
For package releases, read [the release procedure](docs/reference/package-release.md).
