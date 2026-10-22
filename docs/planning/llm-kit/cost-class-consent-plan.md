# llm-kit cost classes and per-machine consent (S1) -- plan

Status: plan by opus 2026-10-21; source design docs/planning/task-kit-dashboard/databench-integration-review.md.


I changed no files. **[E]** means I checked it in the code (file:line). **[H]** means it is a hypothesis.

### Premises, and three corrections to the review

- **[E] The import layering rules out the obvious homes.** `dispatch` may import only `{completion, registry, adapters}` (`docs/reference/llm-kit-architecture.md:116`). So `dispatch` cannot import `frontdoor.acceptance.is_paid` (frontdoor is on the same layer, :117) or `claude_bg.BILLING_DIVERTING_VARS` (pipeline.execution is layer 5, :111). `registry` imports nothing (:124). The classifier therefore goes in **`registry`**, and the two existing copies are tied to it by tests.
- **[E] llm-kit's existing machine state uses the legacy namespace.** The "data dir" state the review cites actually sits under `.../plugins-kit/llm-scripting-kit/usage-verdicts.json` (`dispatch/usage_budget.py:111-114`). Per `registry/constants.py:28-38`, that namespace is kept only for state that already exists. The new file goes under the plugin's own home, `~/.claude/plugins/data/plugins-kit/llm-kit/consent.json` (plugins/CLAUDE.md:400-415).
- **[E] Fixed classes.** `is_paid` is `billing_mode != "unmetered"` (`frontdoor/acceptance.py:90-92`). Front-door tiers are transport entries whose `routing.group` equals the group (`acceptance.py:101-105`), and that registry fact is visible from `discover_model_entries` (`registry/models.py:206`, a dict of `EndpointEntry`). The diverting variables are listed at `claude_bg.py:177-197`, and a variable counts only when set and non-empty (:197). Entry fields are at `model_endpoints.py:147-162`.
- **[E]** Excluded entries are hidden (`declaration.py:93-96`, `:597-599`). `run` calls `describe` again on every attempt (`:979-982`). `Ranking.render` names only the entries it renders (`:412-434`).
- **[H]** Codex has no billing-diversion variable like Claude's. I found no `OPENAI_API_KEY`/`CODEX_API_KEY` handling in `adapters/`, so codex counts as usage regardless of env.
- **[H]** opencode harness entries count as spend (the review's own [H]).
- **[E]** The atomic-write pattern to copy is tmp file plus `replace` (`usage_budget.py:221-222`).

### Interfaces (fixed up front so units can run in parallel)

**`plugins/llm-kit/lib/llm_kit/registry/cost.py`** (new, layer 0):
```python
FREE, USAGE, SPEND = "free", "usage", "spend"
COST_CLASSES = (FREE, USAGE, SPEND)
BILLING_DIVERTING_VARS: tuple[str, ...]   # same 8 names as claude_bg.py:177-186
@dataclass(frozen=True)
class CostVerdict: cost_class: str; reason: str
def is_unmetered(entry: EndpointEntry) -> bool
def classify(entry: EndpointEntry, env: Mapping[str, str] | None = None, *,
             entries: Mapping[str, EndpointEntry] | None = None) -> CostVerdict
def cost_class(entry, env=None, *, entries=None) -> str   # classify(...).cost_class
```
Rules, applied in order. Each one fails closed to spend.
1. A harness entry for `claude` is usage, unless one of the diverting variables is set and non-empty in `env`. Then it is spend, and the reason names the variable. `env=None` means `os.environ`.
2. A harness entry for `codex` is usage.
3. Any other harness entry (opencode, or an unknown harness) is spend.
4. A transport entry with `frontdoor: true` is free only when the group's tiers are listed in `entries` and every one is unmetered. Otherwise it is spend, with the reason "no tiers declared" or "tier X metered". The entry's own `billing` does not override this.
5. Any other transport is free if it is unmetered, and spend otherwise.

**`plugins/llm-kit/lib/llm_kit/registry/consent.py`** (new):
```python
CONSENT_PATH = Path.home()/".claude"/"plugins"/"data"/"plugins-kit"/"llm-kit"/"consent.json"
@dataclass(frozen=True)
class Consent:
    usage: bool; spend: bool
    source: str            # "file" | "absent" | "unreadable"
    path: Path
    note: str              # disclosure when not "file"; "" otherwise
    @property
    def allowed(self) -> frozenset[str]   # {"free"} | usage | spend
def load_consent(path: Path | None = None) -> Consent      # never raises
def save_consent(*, usage: bool, spend: bool, path: Path | None = None) -> Path
```
- **File format:** `{"schema": 1, "usage": bool, "spend": bool, "updated_at": iso8601}`.
- **Fail closed:** a value that is not a real boolean, or a bad schema, gives `source="unreadable"` and free only. So does a missing file (`source="absent"`).
- **Disclosure:** `note` reads like "no consent file at <path>; only free models run (`llm-kit consent set --usage on`)".
- **Write:** `save_consent` creates the parent directory and writes atomically (tmp file plus replace).

**`dispatch/declaration.py` changes:**
- Add `DISPOSITION_SWITCHED_OFF = "switched-off"`.
- `describe(..., allow: Iterable[str] | None = None)` and `run(..., allow: Iterable[str] | None = None)`. `run` passes `allow` to every `describe` it makes.
- `None` means no filter, which is today's behaviour, so interactive callers are unaffected.
- Unknown class names raise `ValueError`.
- **Where the check sits:** after the unroutable checks and before the requirements matcher. An unresolved or unroutable id keeps its true disposition, and a switched-off entry is never probed for reachability.
- `merged is None` (the legacy wrapper path) is classified as spend.
- `detail` is `f"{cls} not allowed: {verdict.reason}"`.
- `Ranking` gains `switched_off: tuple[Disposition, ...] = ()`.
  - `render()` adds a line: `Switched off: or-qwen (spend), sol (usage)`.
  - `to_json()` adds `"switched_off": [{"id", "cost_class", "detail"}]`.
  - The module and Ranking docstrings must state that switched-off entries are shown, not hidden.
- `NoUsableRoutingTarget` gains a `switched_off_ids` property. databench can then report "waiting: only usage entries remain" from the floor's `dispositions`.

**`cli/consent.py`** (new) and **`cli/__init__.py`** (add a `"consent"` entry to `GROUPS` and a branch in `_run_group`):
- `llm-kit consent show [--json]` prints usage, spend, source, path and note.
- `llm-kit consent set [--usage on|off] [--spend on|off]` is a partial update that keeps the other value. Exit code 2 if neither flag is given.
- `cli/models.py` gains `models describe --allow free,usage`, so the filter can be tried by hand.

**`frontdoor/acceptance.py`:** `is_paid(entry)` becomes `not is_unmetered(entry)`. The behaviour is the same and there is one source.

`claude_bg.py` keeps its own tuple, because of the layering. A test pins the two copies equal.

### Units, who owns which files, and parallelism

| Unit | Files owned | Depends on |
|---|---|---|
| U1 classifier | `registry/cost.py`, `frontdoor/acceptance.py`, `tests/llm-kit/test_cost_class.py` | none |
| U2 consent store | `registry/consent.py`, `tests/llm-kit/test_consent.py` | U1's constant names (fixed above) |
| U3 filter | `dispatch/declaration.py`, `cli/models.py`, `tests/llm-kit/test_allow_filter.py` | U1 signature |
| U4 CLI | `cli/consent.py`, `cli/__init__.py`, `tests/llm-kit/test_cli_consent.py`, `tests/llm-kit/test_cli_groups.py` (it pins `GROUPS` at :105) | U2 signature |
| U5 release | `plugins/llm-kit/.claude-plugin/plugin.json`, `plugins/llm-kit/pyproject.toml`, `plugins/llm-kit/CLAUDE.md`, `plugins/llm-kit/README.md` | U1-U4 |

- U1-U4 can all run in parallel against the fixed signatures. U2 and U4 may use the string literals before U1 lands.
- U5 runs last, alone.
- No unit touches `docs/reference/llm-kit-architecture.md`: there are no new import edges (registry->registry, frontdoor->registry, dispatch->registry and cli->registry are all allowed).

### Checks, each shown to fail by a stated revert

| # | Test | Revert that must turn it red |
|---|---|---|
| C1 | `test_cost_class::test_claude_diverted_env_is_spend` (`ANTHROPIC_API_KEY=x`) | drop the env check |
| C2 | `::test_empty_divert_var_is_usage` (`ANTHROPIC_API_KEY=""`) | test `name in env` instead of truthiness |
| C3 | `::test_frontdoor_free_only_when_all_tiers_unmetered` (entry declares unmetered, one tier has no billing) | classify a front door by its own `billing_mode` |
| C4 | `::test_frontdoor_without_tiers_is_spend` | default to free when no tiers are found |
| C5 | `::test_unknown_harness_and_opencode_are_spend` | default a harness entry to usage |
| C6 | `::test_divert_vars_match_claude_bg` | add one name to either tuple |
| C7 | `::test_is_paid_delegates` (no billing gives paid; unmetered gives free) | change `is_paid` to `billing_mode is None` |
| C8 | `test_consent::test_absent_is_free_only_and_discloses` | default `usage=True`, or leave `note` empty |
| C9 | `::test_non_bool_fails_closed` (`"usage": "false"`) | use `bool(value)` |
| C10 | `::test_path_is_user_scoped` (HOME monkeypatched; asserts the literal path) | derive the path from `__file__` |
| C11 | `::test_save_roundtrip_atomic` (no `.tmp` left; a partial write leaves the old file intact) | write straight to the target |
| C12 | `test_allow_filter::test_switched_off_rendered_not_hidden` (render and to_json name the id and class) | settle the entry but leave it out of `switched_off` |
| C13 | `::test_allow_none_unchanged` (a consent file in a tmp HOME says free only; `describe` without `allow` still offers a usage entry) | make `allow` default to `load_consent().allowed` |
| C14 | `::test_run_forwards_allow_and_floors` (an all-harness declaration with `allow={"free"}` raises the floor, every disposition is switched-off, and no backend is called) | have `run` drop `allow` |
| C15 | `::test_switched_off_not_probed` (`check_many` spy) | move the check after probing |
| C16 | `test_cli_consent::test_set_partial_preserves_other` / `::test_show_json_absent` | have `set` write unspecified flags as False |
| C17 | existing `test_import_graph.py` | temporarily import `claude_bg` from `registry/cost.py`; it must go red |

Run them with `uv run --extra dev pytest tests/llm-kit/test_cost_class.py tests/llm-kit/test_consent.py tests/llm-kit/test_allow_filter.py tests/llm-kit/test_cli_consent.py tests/llm-kit/test_cli_groups.py tests/llm-kit/test_import_graph.py tests/llm-kit/test_dispatch_parity.py -v`.

**[H]** `test_dispatch_parity`, `test_final_messages` and the mirror in `tests/llm-scripting-kit/test_declaration.py` may pin the exact `Ranking.to_json` keys or the render text. If so, emit `switched_off` only when it is non-empty, so output with no `allow` stays byte-identical. C13 then covers that too.

### Repo rules

- **Opinion razor:** the only new seam is the consent file, and you ruled on it. Its default (absent means free only) applies only to callers that pass `allow`. `allow=None` keeps every existing opinion. This should be recorded in the change's razor statement.
- **Write paths:** the one new path is user-scoped (`CONSENT_PATH`). It is a single absolute literal that every reader (databench, `task.py`) gets from `llm_kit.registry.consent`, never from the script's location.
- **Shipped model families** (CLAUDE.md:1234): S1 changes no shipped declaration. A caller that opts in and runs an all-harness shipped declaration with no consent file will hit the floor, and the floor discloses it, which is intended. S2 and S3 declarations for databench should list a free entry first.
- **Cross-plugin call shape for S2 and S3 consumers** (plugins/CLAUDE.md:430-446): `describe` and `run` already exist, so a consumer must probe for the `allow` keyword (`inspect.signature(describe).parameters`) and for `llm_kit.registry.consent.load_consent`, not just import the module. "Absent" and "too old" need different messages.

### Version bump and publishing

- Bump llm-kit **0.3.2 -> 0.4.0**, a new public API, in both `.claude-plugin/plugin.json:3` and `pyproject.toml:3` [E].
- `requires_bootstrap` stays at 0.157.0, because no new `bootstrap_lib` call is added [E] (`bootstrap.json:2-3`). bootstrap does not need to ship first.
- Docs: add a "Cost classes and consent" section to `plugins/llm-kit/CLAUDE.md` and the README covering the rules, the file path, the CLI and opt-in per caller. These ship to consumers, so they must not include planning history.
- Publish with `uv run python scripts/publish.py`. llm-kit publishing is pre-authorized (CLAUDE.md:5).
- A `claudx` smoke test is optional. If you run one, exercise `llm-kit consent show`, `llm-kit consent set --usage on`, and `llm-kit models describe <ids> --allow free`.
- Commit message: `llm-kit 0.4.0: cost_class, allow= filter with switched-off disposition, per-machine consent`.

### What S1 leaves to later stages

- databench writes the file through `save_consent` (or `llm-kit consent set`) and passes `allow=load_consent().allowed` for each operation it hosts. Its switch file v2 keeps only `regeneration`, and migrates `harnesses: {claude, codex}` so that either one being true turns `usage` on.
- `task.py answers refresh` calls `load_consent()` and prints `note`.
- Whether every unattended llm-kit caller should honor consent by default is your open decision (review section 6, added in the Addendum). S1 is opt-in only.

### Critical files for implementation
- /Users/christina/Dev/plugins-kit/plugins/llm-kit/lib/llm_kit/dispatch/declaration.py
- /Users/christina/Dev/plugins-kit/plugins/llm-kit/lib/llm_kit/registry/model_endpoints.py
- /Users/christina/Dev/plugins-kit/plugins/llm-kit/lib/llm_kit/frontdoor/acceptance.py
- /Users/christina/Dev/plugins-kit/plugins/llm-kit/lib/llm_kit/pipeline/execution/drivers/claude_bg.py
- /Users/christina/Dev/plugins-kit/plugins/llm-kit/lib/llm_kit/cli/__init__.py
- /Users/christina/Dev/plugins-kit/docs/reference/llm-kit-architecture.md (edge set, read only)
