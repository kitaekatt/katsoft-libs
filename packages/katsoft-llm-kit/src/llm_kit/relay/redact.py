"""Redaction for ``read_session``: secret shapes in text, secret paths in tool calls.

Two independent guards, both pure and standard-library only.

``redact_text`` replaces known secret SHAPES inside text with
``[REDACTED:<kind>]`` and counts the replacements:

* private key blocks (PEM ``BEGIN ... PRIVATE KEY``; a block with no end
  marker is cut to the end of the text);
* ``Authorization``/``Bearer``/``Basic`` header values;
* provider tokens: ``sk-`` (OpenAI, Anthropic, OpenRouter), ``ghp_``/``gho_``/
  ``ghu_``/``ghs_``/``ghr_``/``github_pat_``, ``xox[abprs]-``, AWS access key
  ids (``AKIA``/``ASIA``...), Google ``AIza`` keys, ``glpat-``, ``hf_``,
  ``npm_``, Stripe ``sk_live_``/``rk_live_``, JWTs;
* assignments whose NAME says secret (``aws_secret_access_key = ...``,
  ``"api_key": "..."``): only the value is replaced;
* ``.env`` lines ``NAME=value`` (optionally ``export``) whose NAME contains a
  secret word (``KEY``, ``TOKEN``, ``SECRET``, ``PASSWORD``, ...): only the
  value is replaced.

Ordinary prose, code and numbers are left alone: a name such as ``max_tokens``
or a short value is not a secret shape.

``is_secret_path_use`` answers a different question: does a tool call READ a
path under a secrets location (``~/.ssh``, ``.env`` files, keychains, cloud
and package-manager credential files, secrets-kit stores)? The transcript
reader withholds the whole output of such a call, because the output of
``cat ~/.ssh/id_rsa`` has no shape a pattern can be trusted to catch.
:data:`SECRET_PATH_PATTERNS` is the list.

This is a guardrail, not a proof: under the relay's alternative-A posture the
same user can read these files directly. The function of redaction is that a
remote reader (the claude.ai connector, another machine's session) of a
transcript does not receive credentials by accident.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping, Optional

REDACTED = "[REDACTED:{kind}]"
WITHHELD_SECRET_PATH = "[tool output withheld: the call touched a secrets location]"
WITHHELD_ORPHAN = "[tool output withheld: its originating call is outside the returned window]"

# --------------------------------------------------------------------------
# secret shapes in text
# --------------------------------------------------------------------------

_SECRET_WORDS = (
    r"(?:api[_-]?key|apikey|secret(?:[_-]?key)?|password|passwd|pwd|token|credential|"
    r"private[_-]?key|access[_-]?key|auth(?:orization)?|client[_-]?secret|signing[_-]?key)"
)

#: (kind, compiled pattern, group (number or name) that holds the secret; 0 is the whole match)
_SHAPES: tuple[tuple[str, "re.Pattern[str]", object], ...] = (
    (
        "private-key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)",
            re.DOTALL,
        ),
        0,
    ),
    (
        "auth-header",
        re.compile(r"(?i)\b(?:bearer|basic)[ \t]+([A-Za-z0-9._~+/=-]{8,})"),
        1,
    ),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), 0),
    ("api-key", re.compile(r"\bsk-(?:ant-|or-|proj-)?[A-Za-z0-9_-]{16,}"), 0),
    ("api-key", re.compile(r"\b[sr]k_live_[A-Za-z0-9]{16,}"), 0),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), 0),
    ("github-token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), 0),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), 0),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA)[A-Z0-9]{16}\b"), 0),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), 0),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"), 0),
    ("hf-token", re.compile(r"\bhf_[A-Za-z0-9]{30,}"), 0),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{30,}"), 0),
    # NAME = value / "name": "value" where the name ends in a secret word.
    (
        "secret-value",
        re.compile(
            rf"""(?ix)
            (?<![A-Za-z0-9])
            ["']?[A-Za-z0-9_.-]*{_SECRET_WORDS}["']?
            [ \t]*[:=][ \t]*
            ["']?
            (?!\[REDACTED:)
            (?P<v>[^\s"',;}}\]\)\(]{{8,}})(?![\w(])
            """
        ),
        "v",
    ),
)

#: ``NAME=value`` env lines. NAME is upper-case and contains a secret word.
_ENV_LINE = re.compile(
    rf"""(?mx)
    ^(?P<head>[ \t]*(?:export[ \t]+)?
        [A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS|PWD|CREDENTIALS?|AUTH|PRIVATE|APIKEY)[A-Z0-9_]*
        [ \t]*=[ \t]*)
    (?P<value>[^\r\n]+)$
    """
)
_ALREADY = re.compile(r"\[REDACTED:[a-z-]+\]")


def redact_text(text: str) -> tuple[str, int]:
    """``text`` with secret shapes replaced, and the number of replacements."""
    if not isinstance(text, str):
        raise TypeError(f"redact_text needs str, got {type(text).__name__}")
    count = 0

    def sub_for(kind: str, group: object):
        def sub(match: "re.Match[str]") -> str:
            nonlocal count
            start, end = match.span(group)
            if _ALREADY.fullmatch(match.string[start:end]):
                return match.group(0)
            count += 1
            offset = match.start()
            whole = match.group(0)
            return whole[: start - offset] + REDACTED.format(kind=kind) + whole[end - offset :]

        return sub

    for kind, pattern, group in _SHAPES:
        text = pattern.sub(sub_for(kind, group), text)

    def env_sub(match: "re.Match[str]") -> str:
        nonlocal count
        value = match.group("value")
        if _ALREADY.search(value):
            return match.group(0)
        count += 1
        return match.group("head") + REDACTED.format(kind="env-value")

    text = _ENV_LINE.sub(env_sub, text)
    return text, count


# --------------------------------------------------------------------------
# secret locations used by tool calls
# --------------------------------------------------------------------------

#: Regexes (searched, case-insensitive) over a tool call's serialized input.
#: A match means the call read, or could have read, a secrets location.
SECRET_PATH_PATTERNS: tuple[str, ...] = (
    r"(?:^|[\s\"'=:(/\\])~?/?\.ssh(?:[/\\\s\"']|$)",          # ~/.ssh and any .ssh directory
    r"(?:^|[\s\"'=:(/\\])\.env(?:\.[A-Za-z0-9_-]+)?(?=$|[\s\"'),;:/\\])(?<!\.env\.example)(?<!\.env\.sample)(?<!\.env\.template)",
    r"\.aws[/\\](?:credentials|config|sso)",
    r"\.netrc\b|\b_netrc\b",
    r"\.npmrc\b|\.pypirc\b",
    r"\.gnupg\b|[\\/][\w.-]+\.(?:gpg|asc)(?=$|[\s\"'),;:])",
    r"\.docker[/\\]config\.json",
    r"\.kube[/\\]config",
    r"\.config[/\\]gh[/\\]hosts",
    r"\bid_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?\b",
    r"\.(?:pem|p12|pfx|keystore|jks)(?=$|[\s\"'),;:])",
    r"[\\/][\w.-]+\.key(?=$|[\s\"'),;:])",                     # /path/NAME.key (incl. session key files)
    r"[Kk]eychains?\b|\.keychain(?:-db)?\b",
    r"\bsecurity[ \t]+(?:find|dump|export|add)-(?:generic|internet|identity|certificate|keychain)",
    r"\bsecurity[ \t]+(?:unlock|list|show)-keychain",
    r"[\\/]data[\\/][^\\/\s]+[\\/]secrets-kit[\\/]|[\\/]\.secrets?(?:[\\/]|$)",
    r"\bsecrets[-_]kit\b[^\n]*\b(?:get|show|print|reveal|export|unlock|dump|read)\b",
    r"\b(?:credentials?|secrets?)\.(?:json|ya?ml|toml|txt|ini)\b",
    r"[\\/]llm-kit[\\/]\.env\b|[\\/]\.claude\.json\b|[\\/]\.credentials\.json\b",
    r"\bauth\.json\b",
)
_SECRET_PATH_RES = tuple(re.compile(p, re.IGNORECASE | re.MULTILINE) for p in SECRET_PATH_PATTERNS)

#: Tool names whose single purpose is to read credentials from the OS.
SECRET_TOOL_NAMES: frozenset[str] = frozenset()


def _flatten(value: Any, out: list[str], depth: int = 0) -> None:
    if depth > 6:
        return
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            out.append(str(key))
            _flatten(item, out, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _flatten(item, out, depth + 1)
    elif value is not None:
        out.append(str(value))


def is_secret_path_use(tool_name: Optional[str], tool_input: Any) -> bool:
    """Whether a tool call (name and input) touches a secrets location.

    ``tool_input`` is the call's arguments: a mapping, a list or a string (a
    shell command, a JavaScript snippet, a JSON text). Every string in it is
    searched, so ``Read(file_path=~/.ssh/id_rsa)``, ``Bash(cat .env)`` and a
    Codex ``exec`` snippet that names ``~/.aws/credentials`` all match.
    """
    if tool_name and tool_name in SECRET_TOOL_NAMES:
        return True
    if isinstance(tool_input, str):
        stripped = tool_input.lstrip()
        if stripped[:1] in "{[":
            try:
                tool_input = json.loads(stripped)
            except ValueError:
                pass
    parts: list[str] = []
    _flatten(tool_input, parts)
    haystack = "\n".join(parts)
    return any(pattern.search(haystack) for pattern in _SECRET_PATH_RES)


def redact_all(texts: Iterable[str]) -> tuple[list[str], int]:
    """Redact each of ``texts``; the redacted list and the total count."""
    out: list[str] = []
    total = 0
    for text in texts:
        redacted, n = redact_text(text)
        out.append(redacted)
        total += n
    return out, total
