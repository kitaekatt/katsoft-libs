# katsoft-llm-kit

LLM access, durable agent jobs and batch content pipelines in one library.
The import package is `llm_kit`; the console script is `llm-kit`.

```
pip install katsoft-llm-kit
pip install "katsoft-llm-kit[sdk,jobs,config]"
llm-kit --help
```

The library needs no AI harness, Claude Code, bootstrap or plugins-kit to
install and use.

The base install depends on `katsoft-contracts` (execution-event envelope,
interrupt contract, model declaration parser) and nothing else. Extras:
`config`, `jobs` and `pipeline` bring PyYAML; `sdk` brings the OpenAI client
(the OpenRouter HTTP path); `local` brings psutil; `frontdoor` brings
FastAPI, uvicorn and httpx.

## Runtime mode

`llm_kit._runtime.mode()` reports how the library is deployed. This
distribution is `"package"`: the contracts come from `katsoft-contracts`.
`llm_kit._runtime.fleet()` reports whether `bootstrap_lib`, the plugin
marketplace's shared library, imports in the same environment. With it, the
features that need it (the Codex adapter, claudx propagation, skill context,
the code-review lane) work and the model registry reads the layered config.
Without it, those features raise `UnsupportedInPackageMode`, Codex
reachability uses a PATH check, and the model registry reads the shipped
defaults; point `MODEL_ENDPOINTS_REGISTRY` at a registry file to change them.

Do not install this distribution into an environment that also holds a
path-linked copy of the `llm_kit` source tree.

## Consent

The library makes no model calls of its own: the code that uses it does, so the library gates
nothing on a caller's behalf. Every model entry is classified `local`, `subscription` or
`metered` (paid or unverified billing), and the class and the reason for it are always reported
(`llm-kit models catalog`, `models describe`, and protocol 2 responses).

One departure from the design below: while `UNCONDITIONAL_CONSENT` is set
(`llm_kit/dispatch/invoke.py`), a protocol 2 request that sends no `consent` field is refused for
a `subscription` or `metered` entry unless the global consent file states that class `true`;
`local` is always allowed. Setting the constant to `False` removes that refusal.

Apart from that refusal, the optional consent layer applies a policy you configure only when you
opt in, and you may build your own policy instead. To opt in, pass `--consent [--consent-scope NAME]` to
`llm-kit models describe`, send `"consent": true` or `"consent": {"scope": "NAME"}` in a
protocol 2 request, or call `llm_kit.registry.consent.policy(scope=None)`. To apply your own
filter instead, pass `--allow local,subscription` (not combinable with `--consent`).

With the layer applied and no file, `local` and `subscription` are on and `metered` is off.
One file, `$XDG_CONFIG_HOME/llm-kit/consent.json` (`~/.config/llm-kit/consent.json` by
default; `LLM_KIT_CONSENT_FILE` overrides), holds the global policy and optional named scopes.
A scope can only restrict the global policy; a class the scope omits inherits.

```
llm-kit consent show [--scope NAME] [--json]
llm-kit consent set [--scope NAME] --local|--subscription|--metered on|off
```

## Source

The source lives in the plugins-kit repository (`packages/katsoft-llm-kit/src/llm_kit`).
Releases are tagged `katsoft-llm-kit-v<version>` in the repository named by the
`Source` project URL.
