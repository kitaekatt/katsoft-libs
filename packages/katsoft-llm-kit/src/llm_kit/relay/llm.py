"""The relay's ``ask_llm`` service (unit L4).

``LlmService.ask`` answers one ``ask_llm`` request (``relay-protocol/1``,
``docs/reference/llm-kit-relay-protocol.md`` section 13) against the relay's
own loopback model registry. The order of the checks is the contract:

1. the params are validated (``bad_request`` / ``target_invalid``);
2. the endpoint is resolved with :func:`llm_kit.relay.config.require_loopback_endpoint`
   over the loopback registry: no endpoint named, an unknown name and a
   non-loopback entry are ``endpoint_refused``; there is no default and no
   fallback to the user's fleet registry;
3. the consent layer is checked EXPLICITLY, before any backend exists:
   ``registry.cost.classify(entry)`` then
   ``registry.consent.policy(scope="relay").allows(cost_class)``. A refusal is
   ``consent_refused`` and names the exact ``llm-kit consent set`` command.
   Consent scopes are free-form names (``consent.validate_scope_name``); the
   scope ``relay`` needs no registration, only a ``consent set --scope relay``
   when the global policy does not already allow the class;
4. only then is a backend built and called.

The prior turns of a named conversation come from, and the new user and
assistant pair goes to, the M1 mailbox ledger in one transaction
(``Mailbox.append_turns``), after a completed call only. A failed call stores
nothing.

Registry selection. ``dispatch.factory.create_backend`` reads the endpoint
registry through the process environment (``MODEL_ENDPOINTS_REGISTRY``) and
accepts no environment argument, and the transport resolves the endpoint
again when it completes. The default backend factory therefore sets that one
variable for the duration of build plus call, under a process-wide lock, and
restores the previous value afterwards. It also re-resolves the endpoint
inside that scope and refuses if the layered config (which shadows the
registry) points the name anywhere but the loopback entry. A caller that
injects its own ``backend_factory`` gets no environment change at all.

The backends take one system string and one user string, not a message list,
so a multi-turn exchange is rendered as a role-labelled transcript in the user
string. Security posture is C0's: under alternative A nothing here is a
security boundary against a same-user process.
"""
from __future__ import annotations

import contextlib
import os
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

from llm_kit.completion.types import BackendOptions
from llm_kit.registry import consent as consent_store
from llm_kit.registry.cost import classify
from llm_kit.relay import protocol as P
from llm_kit.relay.config import require_loopback_endpoint
from llm_kit.relay.mailbox import MAX_CONVERSATION_NAME_CHARS, MAX_TURN_CHARS, Mailbox

#: The consent scope the relay opts in under.
CONSENT_SCOPE = "relay"

#: Largest prompt or message content, in characters (the ``ask_llm`` schema cap).
MAX_PROMPT_CHARS = MAX_TURN_CHARS
#: Largest completion text returned (and stored as a conversation turn).
MAX_RESPONSE_CHARS = MAX_TURN_CHARS
#: Most explicit messages in one request (the ``ask_llm`` schema cap).
MAX_MESSAGES = 64
#: Largest ``max_tokens`` (the ``ask_llm`` schema cap).
MAX_MAX_TOKENS = 32768
#: Largest ``temperature`` (the ``ask_llm`` schema cap).
MAX_TEMPERATURE = 2.0
#: Characters of conversation history carried to the model, newest first. The
#: new prompt is always sent; the oldest turns beyond this budget are left out
#: and counted in the result's ``dropped_turns``. Equals ``MAX_REQUEST_BYTES``.
MAX_CONTEXT_CHARS = P.MAX_REQUEST_BYTES
#: Default wall-clock cap for one completion; a cold llama-swap model load is slow.
DEFAULT_TIMEOUT_S = 600.0

_PARAM_KEYS = frozenset({"endpoint", "prompt", "messages", "conversation", "max_tokens", "temperature"})
_ROLE_LABELS = {"user": "User", "assistant": "Assistant"}

#: Serialises the registry-environment scope; re-entrant so a nested scope is safe.
_ENV_LOCK = threading.RLock()


def _bad(message: str) -> P.RelayError:
    return P.RelayError("bad_request", message)


@contextlib.contextmanager
def registry_environment(environ: Mapping[str, str]) -> Iterator[None]:
    """Set ``environ`` in the process environment for the block, then restore it exactly."""
    with _ENV_LOCK:
        saved = {key: os.environ.get(key) for key in environ}
        os.environ.update(environ)
        try:
            yield
        finally:
            for key, old in saved.items():
                if old is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old


def default_backend_factory(entry: Any) -> Any:
    """Build the backend for a loopback registry ``entry`` with ``dispatch.factory.create_backend``.

    Must run inside :func:`registry_environment` (the service arranges that).
    Refuses (``endpoint_refused``) a result that is not a transport, or whose
    layered-config resolution does not point at the entry's own loopback URL.
    """
    from llm_kit.dispatch.factory import create_backend
    from llm_kit.registry.models import resolve_endpoint

    selection = create_backend(entry.id)
    resolved = resolve_endpoint(entry.id)
    if selection.kind != "transport" or resolved.get("base_url") != entry.base_url:
        raise P.RelayError(
            "endpoint_refused",
            f"endpoint {entry.id!r} resolves to {resolved.get('base_url')!r}, not the relay's loopback entry "
            f"{entry.base_url!r}; a layered config entry shadows it",
            details={"endpoint": entry.id},
        )
    return selection


@dataclass(frozen=True)
class _Request:
    endpoint_name: str
    address: str
    conversation: Optional[str]
    prompt: Optional[str]
    messages: Optional[list[P.ChatMessage]]
    max_tokens: Optional[int]
    temperature: Optional[float]


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_content(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise _bad(f"{what} must be a string")
    if len(value) > MAX_PROMPT_CHARS:
        raise P.RelayError("too_large", f"{what} is over {MAX_PROMPT_CHARS} characters", details={"limit": MAX_PROMPT_CHARS})
    return value


def _parse(params: Mapping[str, Any], machine: str) -> _Request:
    if not isinstance(params, Mapping):
        raise _bad("ask_llm params must be an object")
    unknown = sorted(set(params) - _PARAM_KEYS)
    if unknown:
        raise _bad(f"unknown ask_llm params: {', '.join(map(str, unknown))}")

    endpoint = params.get("endpoint")
    if endpoint is None or (isinstance(endpoint, str) and not endpoint.strip()):
        # No explicit endpoint: refused, never defaulted.
        raise P.RelayError("endpoint_refused", "no explicit endpoint was named; the relay has no default endpoint")
    if not isinstance(endpoint, str):
        raise _bad("endpoint must be a string")
    address = P.parse_address(endpoint)
    if address.harness != "llm":
        raise P.RelayError("endpoint_refused", f"{endpoint!r} is not an llm endpoint address", details={"endpoint": endpoint})
    if address.machine != machine:
        raise P.RelayError(
            "endpoint_refused",
            f"endpoint {endpoint!r} names machine {address.machine!r}; this relay reaches {machine!r} only",
            details={"endpoint": endpoint},
        )

    has_prompt, has_messages = "prompt" in params, "messages" in params
    if has_prompt == has_messages:
        raise _bad("give exactly one of prompt or messages")

    conversation = params.get("conversation")
    if conversation is not None:
        if has_messages:
            raise _bad("conversation goes with prompt, not messages")
        if not isinstance(conversation, str) or not (1 <= len(conversation) <= MAX_CONVERSATION_NAME_CHARS):
            raise _bad(f"conversation must be a string of 1 to {MAX_CONVERSATION_NAME_CHARS} characters")

    prompt: Optional[str] = None
    messages: Optional[list[P.ChatMessage]] = None
    if has_prompt:
        prompt = _validate_content(params["prompt"], "prompt")
        if not prompt:
            raise _bad("prompt must not be empty")
    else:
        raw = params["messages"]
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not (1 <= len(raw) <= MAX_MESSAGES):
            raise _bad(f"messages must be a list of 1 to {MAX_MESSAGES} items")
        messages = []
        for index, item in enumerate(raw):
            if not isinstance(item, Mapping) or set(item) != {"role", "content"}:
                raise _bad(f"messages[{index}] must be an object with role and content")
            if item["role"] not in P.CHAT_ROLES:
                raise _bad(f"messages[{index}].role must be one of {P.CHAT_ROLES}")
            messages.append({"role": item["role"], "content": _validate_content(item["content"], f"messages[{index}].content")})
        if not any(m["role"] == "user" for m in messages):
            raise _bad("messages needs at least one user message")

    max_tokens = params.get("max_tokens")
    if max_tokens is not None and (not _is_int(max_tokens) or not (1 <= max_tokens <= MAX_MAX_TOKENS)):
        raise _bad(f"max_tokens must be an integer from 1 to {MAX_MAX_TOKENS}")
    temperature = params.get("temperature")
    if temperature is not None and (
        isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not (0 <= temperature <= MAX_TEMPERATURE)
    ):
        raise _bad(f"temperature must be a number from 0 to {MAX_TEMPERATURE:g}")

    return _Request(
        endpoint_name=address.session,
        address=str(address),
        conversation=conversation,
        prompt=prompt,
        messages=messages,
        max_tokens=max_tokens,
        temperature=None if temperature is None else float(temperature),
    )


def render_messages(messages: Sequence[P.ChatMessage]) -> tuple[str, str]:
    """``(system, user)`` strings for a backend from a chat message list.

    System messages are joined into ``system``. A lone user message is sent
    verbatim; several turns become a role-labelled transcript ending with an
    open ``Assistant:`` line, because the completion backends take no message list.
    """
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    turns = [m for m in messages if m["role"] != "system"]
    if len(turns) == 1 and turns[0]["role"] == "user":
        return system, turns[0]["content"]
    lines = [f"{_ROLE_LABELS[m['role']]}: {m['content']}" for m in turns]
    lines.append("Assistant:")
    return system, "\n\n".join(lines)


def _fit_history(history: list[P.ChatMessage], new_chars: int) -> tuple[list[P.ChatMessage], int]:
    """The newest suffix of ``history`` that fits :data:`MAX_CONTEXT_CHARS` beside the new prompt, and the count dropped."""
    budget = MAX_CONTEXT_CHARS - new_chars
    kept: list[P.ChatMessage] = []
    used = 0
    for message in reversed(history):
        used += len(message["content"])
        if used > budget:
            break
        kept.append(message)
    kept.reverse()
    return kept, len(history) - len(kept)


class LlmService:
    """The ``ask_llm`` verb's implementation: loopback endpoint, explicit consent, named conversations.

    ``registry`` comes from :func:`llm_kit.relay.config.load_loopback_registry`;
    ``machine`` is the relay's machine label; ``mailbox`` keeps the
    conversations. ``registry_environ`` (``PreparedState.registry_environ()``)
    points the default backend factory at the relay's own registry file.
    ``backend_factory(entry)`` replaces that default with any callable returning
    an object that has ``.backend`` (an ``LLMBackend``) and ``.model``; it is
    called only after consent allows the call, and no environment is touched for it.
    ``policy_loader(scope)`` defaults to ``consent.policy``.
    """

    def __init__(
        self,
        *,
        machine: str,
        registry: Any,
        mailbox: Mailbox,
        registry_environ: Optional[Mapping[str, str]] = None,
        backend_factory: Optional[Callable[[Any], Any]] = None,
        policy_loader: Optional[Callable[[str], Any]] = None,
        timeout_s: Optional[float] = DEFAULT_TIMEOUT_S,
    ) -> None:
        if backend_factory is None and not registry_environ:
            raise ValueError("registry_environ is required when no backend_factory is injected: the default factory must be told the relay's registry file")
        self._machine = machine
        self._registry = registry
        self._mailbox = mailbox
        self._environ = dict(registry_environ or {})
        self._factory = backend_factory
        self._policy_loader = policy_loader or consent_store.policy
        self._timeout_s = timeout_s

    @classmethod
    def from_state(cls, prepared: Any, mailbox: Mailbox, **kw: Any) -> "LlmService":
        """A service over a C0 ``PreparedState``: its loopback registry, machine label and registry environment."""
        from llm_kit.relay.config import load_loopback_registry

        return cls(
            machine=prepared.config.machine_label,
            registry=load_loopback_registry(prepared.registry_file),
            mailbox=mailbox,
            registry_environ=prepared.registry_environ(),
            **kw,
        )

    # -- checks ------------------------------------------------------------

    def _require_consent(self, entry: Any) -> str:
        """Classify ``entry`` and require the relay scope to allow its class; returns the class."""
        verdict = classify(entry, entries=self._registry.entries)
        try:
            policy = self._policy_loader(CONSENT_SCOPE)
            allowed = policy.allows(verdict.cost_class)
        except (consent_store.ConsentConfigError, OSError, ValueError) as exc:
            raise P.RelayError(
                "consent_refused",
                f"the consent policy for scope {CONSENT_SCOPE!r} could not be read, so nothing was dispatched: {exc}",
                details={"endpoint": entry.id, "cost_class": verdict.cost_class},
            ) from exc
        if not allowed:
            commands = list(policy.classes[verdict.cost_class].commands)
            raise P.RelayError(
                "consent_refused",
                f"{entry.id!r} is a {verdict.cost_class} entry ({verdict.reason}) and {policy.refusal(verdict.cost_class)}; "
                "nothing was dispatched",
                details={"endpoint": entry.id, "cost_class": verdict.cost_class, "commands": commands},
            )
        return verdict.cost_class

    # -- the verb ------------------------------------------------------------

    def ask(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Answer one ``ask_llm`` request; returns an ``AskLlmResult`` or raises ``RelayError``."""
        request = _parse(params, self._machine)
        entry = require_loopback_endpoint(self._registry, request.endpoint_name)
        self._require_consent(entry)

        dropped = 0
        if request.messages is not None:
            messages = list(request.messages)
        else:
            history: list[P.ChatMessage] = []
            if request.conversation is not None:
                history = self._mailbox.load_conversation(request.address, request.conversation)
                history, dropped = _fit_history(history, len(request.prompt or ""))
            messages = history + [{"role": "user", "content": request.prompt or ""}]
        system, user = render_messages(messages)

        options = BackendOptions(max_tokens=request.max_tokens, temperature=request.temperature, timeout_s=self._timeout_s)
        scope = registry_environment(self._environ) if self._factory is None else contextlib.nullcontext()
        with scope:
            selection = (self._factory or default_backend_factory)(entry)
            try:
                response = selection.backend.complete(system, user, model=selection.model, options=options)
            except Exception as exc:  # noqa: BLE001 -- every backend failure is the protocol's one upstream error
                raise P.RelayError(
                    "upstream_unavailable",
                    f"endpoint {entry.id!r} did not answer: {type(exc).__name__}: {str(exc)[:500]}",
                    details={"endpoint": request.address, "exception": type(exc).__name__},
                ) from exc

        text = response.text
        if len(text) > MAX_RESPONSE_CHARS:
            raise P.RelayError(
                "too_large",
                f"the completion is {len(text)} characters, over the {MAX_RESPONSE_CHARS} limit; nothing was stored",
                details={"limit": MAX_RESPONSE_CHARS},
            )

        result: dict[str, Any] = {
            "endpoint": request.address,
            "text": text,
            "finish_reason": (response.finish_reason or None),
            "usage": {"input_tokens": int(response.input_tokens), "output_tokens": int(response.output_tokens)},
        }
        if request.conversation is not None:
            result["conversation"] = request.conversation
            result["turn"] = self._mailbox.append_turns(
                request.address,
                request.conversation,
                [("user", request.prompt or ""), ("assistant", text)],
            )
            if dropped:
                result["dropped_turns"] = dropped
        return result


__all__ = [
    "CONSENT_SCOPE",
    "DEFAULT_TIMEOUT_S",
    "LlmService",
    "MAX_CONTEXT_CHARS",
    "default_backend_factory",
    "registry_environment",
    "render_messages",
]
