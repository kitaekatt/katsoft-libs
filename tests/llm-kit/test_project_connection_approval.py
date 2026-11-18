"""A project file's connection that sends a key is used only when this machine approves it.

A tracked project model file (``.llm-kit/models.yaml``) may connect an endpoint the
machine does not know: ``endpoints.<name>: {base_url, key_env}``. Without a check, a
repository (or a pull request that only touches that file) could name any host and
any environment variable, and a call would send that variable's value to that host
as a bearer token. :func:`llm_kit.registry.project_models.endpoint_connection` now
returns such a connection only when:

* the base URL names a plain host (no user info, no escape that a client could read
  as another host);
* it is https, or plain http to a literal loopback address;
* no endpoint of this machine sends the same key variable to another host;
* the user-level consent file holds an approval of exactly that endpoint name,
  host and key variable (``llm-kit consent connection on ...``).

Every test runs under a throwaway HOME; nothing reads or writes the real user files.
Red on revert: each refusal below is a returned connection before the change.
"""

from __future__ import annotations

import os

import pytest

from llm_kit.registry import consent
from llm_kit.registry import project_models as pm
from test_cli_harness import home, write  # noqa: F401 -- home is a fixture

VENDOR = "https://api.vendor.example/v1"
APPROVE = "llm-kit consent connection on --endpoint direct --host api.vendor.example --key-env VENDOR_API_KEY"


def _document(base_url: str, key_env, name: str = "direct") -> dict:
    """A normalized project document whose one row is on endpoint ``name``, connected at ``base_url``."""
    return pm.validate_portable_document({
        "version": 1,
        "endpoints": {name: {"effort_style": "top-level", "base_url": base_url, "key_env": key_env}},
        "catalog": [{"id": "row", "label": "Row", "endpoint": name, "model": "vendor-model-1"}],
    })


def _approve(endpoint: str, host: str, key_env: str) -> None:
    consent.approve_connection(consent.ConnectionApproval(endpoint, host, key_env))


def _refusal(document: dict, name: str = "direct", **kwargs) -> str:
    with pytest.raises(pm.ProjectModelsError) as caught:
        pm.endpoint_connection(document, name, **kwargs)
    assert caught.value.code == pm.CODE_UNAPPROVED == "unapproved"
    return str(caught.value)


def test_an_unapproved_connection_that_sends_a_key_is_refused_naming_the_endpoint_host_key_and_remedy(home):
    message = _refusal(_document(VENDOR, "VENDOR_API_KEY"))
    for needle in ("'direct'", "api.vendor.example", "VENDOR_API_KEY", APPROVE):
        assert needle in message, needle


def test_the_connection_is_returned_once_the_machine_owner_approves_it(home):
    _approve("direct", "api.vendor.example", "VENDOR_API_KEY")
    got = pm.endpoint_connection(_document(VENDOR, "VENDOR_API_KEY"), "direct")
    assert (got["name"], got["base_url"], got["key_env"], got["key_file"]) == (
        "direct", VENDOR, "VENDOR_API_KEY", None)
    assert got["effort_style"] == "top-level"


@pytest.mark.parametrize("approval", [
    ("direct", "collector.example", "VENDOR_API_KEY"),   # another host
    ("direct", "api.vendor.example", "OTHER_KEY"),       # another key variable
    ("other", "api.vendor.example", "VENDOR_API_KEY"),   # another endpoint name
    ("direct", "api.vendor.example:8443", "VENDOR_API_KEY"),  # the host with a port the URL does not name
])
def test_an_approval_of_another_endpoint_host_or_key_does_not_approve(home, approval):
    _approve(*approval)
    assert APPROVE in _refusal(_document(VENDOR, "VENDOR_API_KEY"))


def test_a_key_this_machine_sends_to_another_host_is_refused_even_when_approved(home, tmp_path, monkeypatch):
    # The shipped openrouter endpoints send OPENROUTER_API_KEY to openrouter.ai.
    _approve("direct", "collector.example", "OPENROUTER_API_KEY")
    message = _refusal(_document("https://collector.example/v1", "OPENROUTER_API_KEY"))
    for needle in ("'direct'", "OPENROUTER_API_KEY", "openrouter.ai", "collector.example", "openrouter"):
        assert needle in message, needle
    assert "llm-kit consent connection" not in message  # no approval can lift it
    # A key variable an endpoint registry entry declares is bound the same way.
    registry = write(tmp_path / "registry.yaml",
                     "version: 1\nmodels:\n  lab: {base_url: https://lab.example/v1, model: m, key_env: LAB_KEY}\n")
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(registry))
    _approve("direct", "collector.example", "LAB_KEY")
    message = _refusal(_document("https://collector.example/v1", "LAB_KEY"))
    assert "LAB_KEY" in message and "lab.example" in message and "'lab'" in message
    # The host the machine itself sends the key to is not another host: the approval decides.
    assert "llm-kit consent connection on" in _refusal(_document("https://lab.example/v1", "LAB_KEY"))
    _approve("direct", "lab.example", "LAB_KEY")
    assert pm.endpoint_connection(_document("https://lab.example/v1", "LAB_KEY"), "direct")["key_env"] == "LAB_KEY"


@pytest.mark.parametrize(("base_url", "host", "allowed"), [
    ("http://api.vendor.example/v1", "api.vendor.example", False),
    ("http://10.0.0.5:8080/v1", "10.0.0.5:8080", False),
    ("http://localhost:8080/v1", "localhost:8080", False),  # a hosts file can send localhost anywhere
    ("http://127.0.0.1:8080/v1", "127.0.0.1:8080", True),
    ("http://[::1]:8080/v1", "[::1]:8080", True),
    ("https://api.vendor.example/v1", "api.vendor.example", True),
])
def test_plain_http_carries_a_key_only_to_a_literal_loopback_address(home, base_url, host, allowed):
    _approve("direct", host, "VENDOR_API_KEY")  # an approval never lifts the http rule
    document = _document(base_url, "VENDOR_API_KEY")
    if allowed:
        assert pm.endpoint_connection(document, "direct")["base_url"] == base_url
        return
    message = _refusal(document)
    assert "plain http" in message and host in message and "VENDOR_API_KEY" in message


@pytest.mark.parametrize("base_url", [
    "https://api.vendor.example@collector.example/v1",  # user info: the client sends it to collector.example
    "https://api.vendor.example%2ecollector.example/v1",
    "https://collector.example\\@api.vendor.example/v1",
])
def test_a_base_url_without_a_plain_host_never_matches_an_approval(home, base_url):
    _approve("direct", "api.vendor.example", "VENDOR_API_KEY")
    message = _refusal(_document(base_url, "VENDOR_API_KEY"))
    assert "plain host" in message and "VENDOR_API_KEY" in message


def test_the_host_is_compared_in_lower_case(home):
    _approve("direct", "api.vendor.example", "VENDOR_API_KEY")
    got = pm.endpoint_connection(_document("https://API.Vendor.Example/v1", "VENDOR_API_KEY"), "direct")
    assert got["base_url"] == "https://API.Vendor.Example/v1"


def test_a_keyless_connection_needs_no_approval_and_reads_no_consent_file(home, monkeypatch):
    monkeypatch.setenv("LLM_KIT_CONSENT_FILE", str(write(home / "broken.json", "{broken")))
    got = pm.endpoint_connection(_document("http://10.0.0.5:8080/v1", None), "direct")
    assert (got["base_url"], got["key_env"]) == ("http://10.0.0.5:8080/v1", None)


def test_an_approval_in_the_project_tree_is_not_read(home, tmp_path, monkeypatch):
    """The approval is the machine owner's, outside the repository: a file the project ships approves nothing."""
    project = tmp_path / "proj"
    stored = '{"schema": 2, "connections": [{"endpoint": "direct", "host": "api.vendor.example", ' \
             '"key_env": "VENDOR_API_KEY"}]}'
    for relative in (".llm-kit/consent.json", ".config/llm-kit/consent.json", "consent.json"):
        write(project / relative, stored)
    monkeypatch.chdir(project)
    assert APPROVE in _refusal(_document(VENDOR, "VENDOR_API_KEY"))


def test_an_unreadable_consent_file_is_reported_not_skipped(home, monkeypatch):
    monkeypatch.setenv("LLM_KIT_CONSENT_FILE", str(write(home / "broken.json", "{broken")))
    with pytest.raises(consent.ConsentConfigError, match="not valid JSON"):
        pm.endpoint_connection(_document(VENDOR, "VENDOR_API_KEY"), "direct")


def _windows_env_names(monkeypatch) -> None:
    """Environment names that ignore case: native on Windows, simulated elsewhere."""
    if os.name != "nt":
        monkeypatch.setattr(consent, "ENV_NAMES_IGNORE_CASE", True)


def test_on_windows_a_key_variable_in_another_case_is_the_same_bound_key(home, monkeypatch):
    """``os.environ`` ignores case on Windows, so ``openrouter_api_key`` reads OPENROUTER_API_KEY.

    Red on revert: the lower-case name is not in the machine's key map, the approval
    lifts the connection, and the machine's OpenRouter key goes to collector.example.
    """
    _windows_env_names(monkeypatch)
    if os.name == "nt":  # the hazard is real here: one variable, two spellings
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-key-for-openrouter-only")
        assert os.environ.get("openrouter_api_key") == "or-key-for-openrouter-only"
    _approve("direct", "collector.example", "openrouter_api_key")
    message = _refusal(_document("https://collector.example/v1", "openrouter_api_key"))
    assert "openrouter.ai" in message and "'openrouter'" in message and "no approval lifts this" in message


def test_on_windows_an_approval_matches_a_key_variable_in_another_case(home, monkeypatch):
    """One rule on Windows: a key variable is compared by its folded name, in approvals too."""
    _windows_env_names(monkeypatch)
    _approve("direct", "api.vendor.example", "VENDOR_API_KEY")
    got = pm.endpoint_connection(_document(VENDOR, "vendor_api_key"), "direct")
    assert got["key_env"] == "vendor_api_key"
    # revoking in another case removes that approval, so nothing keeps applying unseen
    _, config = consent.revoke_connection(consent.ConnectionApproval("direct", "api.vendor.example", "vendor_api_key"))
    assert config.connections == ()
    assert "llm-kit consent connection on" in _refusal(_document(VENDOR, "vendor_api_key"))


def test_where_names_keep_their_case_another_case_is_another_variable(home, monkeypatch):
    """POSIX: ``openrouter_api_key`` is not OPENROUTER_API_KEY, so only an exact approval lifts it."""
    monkeypatch.setattr(consent, "ENV_NAMES_IGNORE_CASE", False)
    document = _document("https://collector.example/v1", "openrouter_api_key")
    assert "llm-kit consent connection on" in _refusal(document)  # rule 4, not rule 3
    _approve("direct", "collector.example", "OPENROUTER_API_KEY")  # another variable here
    assert "llm-kit consent connection on" in _refusal(document)
    _approve("direct", "collector.example", "openrouter_api_key")
    assert pm.endpoint_connection(document, "direct")["key_env"] == "openrouter_api_key"


def test_a_name_the_file_does_not_connect_has_no_connection(home):
    document = _document(VENDOR, "VENDOR_API_KEY")
    assert pm.endpoint_connection(document, "openrouter") is None
