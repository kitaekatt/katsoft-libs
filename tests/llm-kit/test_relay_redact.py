"""Tests for llm_kit.relay.redact (unit D2): secret shapes in text and secret-path tool calls.

Fixture secrets are assembled from parts so this file itself carries no
credential-shaped literal that a secret scanner would flag.
"""
from __future__ import annotations

import pytest

from llm_kit.relay import redact as R

SK = "sk-" + "ant-api03-" + "A1b2C3d4E5f6G7h8I9j0"
GHP = "gh" + "p_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
XOX = "xo" + "xb-" + "1234567890-abcdefghijkl"
AKIA = "AK" + "IA" + "IOSFODNN7EXAMPLE"
JWT = "eyJ" + "hbGciOiJIUzI1NiJ9." + "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0." + "abcdefghijklmnop"


@pytest.mark.parametrize(
    "text,secret,kind",
    [
        (f"the key is {SK} ok", SK, "api-key"),
        (f"export it: {GHP}", GHP, "github-token"),
        (f"slack {XOX} here", XOX, "slack-token"),
        (f"aws {AKIA} id", AKIA, "aws-access-key"),
        (f"jwt {JWT} end", JWT, "jwt"),
        ("curl -H 'Authorization: Bearer abcDEF123456xyz789'", "abcDEF123456xyz789", "auth-header"),
        ("aws_secret_access_key = wJalrXUtnFEMI/K7MDENGbPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMI", "secret-value"),
        ('{"api_key": "abcd1234efgh5678"}', "abcd1234efgh5678", "secret-value"),
    ],
)
def test_secret_shapes_are_replaced(text: str, secret: str, kind: str) -> None:
    out, count = R.redact_text(text)
    assert secret not in out
    assert f"[REDACTED:{kind}]" in out
    assert count == 1


def test_private_key_block_is_replaced_whole() -> None:
    body = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\nAAAAAAAA\n-----END OPENSSH PRIVATE KEY-----"
    out, count = R.redact_text(f"before\n{body}\nafter")
    assert out == "before\n[REDACTED:private-key]\nafter"
    assert count == 1


def test_unterminated_private_key_block_is_cut_to_the_end() -> None:
    out, count = R.redact_text("x\n-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA")
    assert "MIIEow" not in out and out.startswith("x\n")
    assert count == 1


def test_env_lines_keep_the_name_and_drop_the_value() -> None:
    text = "export OPENAI_API_KEY=" + SK + "\nDB_PASSWORD=hunter2hunter2\nPATH=/usr/bin\nDEBUG=1\n"
    out, count = R.redact_text(text)
    assert "hunter2" not in out and SK not in out
    assert "OPENAI_API_KEY=[REDACTED:" in out and "DB_PASSWORD=[REDACTED:" in out
    assert "PATH=/usr/bin" in out and "DEBUG=1" in out
    assert count == 2


def test_each_secret_is_counted_once() -> None:
    _, count = R.redact_text(f"{SK} and {GHP} and {AKIA}")
    assert count == 3


@pytest.mark.parametrize(
    "text",
    [
        "Normal text: the quick brown fox jumps over the lazy dog.",
        'config = {"max_tokens": 4096, "temperature": 0.2}',
        "token count is 4096; the author: Bob wrote key=1",
        "password = getpass.getpass()",
        "sk-short and ghp_short are too short to be keys",
        "PATH=/usr/bin:/bin\nHOME=/Users/x\nDEBUG=1",
        "see https://example.com/path?x=1&y=2 and /usr/local/bin/tool",
    ],
)
def test_ordinary_text_is_untouched(text: str) -> None:
    assert R.redact_text(text) == (text, 0)


def test_redact_text_rejects_non_text() -> None:
    with pytest.raises(TypeError):
        R.redact_text(b"bytes")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "name,tool_input",
    [
        ("Bash", "cat ~/.ssh/id_rsa"),
        ("Bash", "ls -la ~/.ssh"),
        ("Bash", "cat .env"),
        ("Bash", "source /srv/app/.env.production"),
        ("Read", {"file_path": "/home/u/.aws/credentials"}),
        ("Read", {"file_path": "/home/u/.npmrc"}),
        ("Read", {"file_path": "/keys/deploy.pem"}),
        ("Read", {"file_path": "/Users/u/.claude/sessions/123.0abc.key"}),
        ("Bash", "security find-generic-password -s svc -w"),
        ("Bash", "ls ~/Library/Keychains"),
        ("Read", {"file_path": "/Users/u/.claude/plugins/data/mkt/secrets-kit/store.json"}),
        ("exec", 'const r = await tools.exec_command({cmd: "cat ~/.ssh/config"});'),
        ("Read", {"file_path": "/Users/u/.codex/auth.json"}),
        ("Bash", '{"command": "cat ~/.ssh/id_ed25519"}'),
    ],
)
def test_secret_location_calls_are_detected(name: str, tool_input: object) -> None:
    assert R.is_secret_path_use(name, tool_input) is True


@pytest.mark.parametrize(
    "name,tool_input",
    [
        ("Bash", "ls src && git status"),
        ("Bash", "cat .env.example"),
        ("Bash", "git log -S item.key"),
        ("Read", {"file_path": "/Users/u/Dev/app/environment.py"}),
        ("Read", {"file_path": "/Users/u/Dev/app/.envrc"}),
        ("Read", {"file_path": "/Users/u/Dev/plugins-kit/plugins/secrets-kit/README.md"}),
        ("Edit", {"file_path": "/a/b.py", "old_string": "x", "new_string": "y"}),
    ],
)
def test_ordinary_calls_are_not_secret_locations(name: str, tool_input: object) -> None:
    assert R.is_secret_path_use(name, tool_input) is False


def test_redact_all_sums_counts() -> None:
    out, total = R.redact_all([f"a {SK}", "plain", f"b {GHP}"])
    assert total == 2 and out[1] == "plain" and SK not in out[0]
