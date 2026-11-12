"""Native Windows tests for the relay lifecycle (unit A1): real msvcrt locks and real ACLs.

Skipped everywhere except Windows. They measure the two premises the portable
fakes only assume: that ``msvcrt.locking`` on one byte at ``LOCK_OFFSET`` gives
singleton semantics while the pid at offset 0 stays readable by a second
process, and that ``winacl`` provisions and validates real security
descriptors. Run on a Windows host:

    uv run --extra dev pytest tests/llm-kit/test_relay_windows_native.py -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import psutil
import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows locks and ACLs")

from llm_kit.completion.types import LLMResponse  # noqa: E402
from llm_kit.registry import consent  # noqa: E402
from llm_kit.relay import config as C  # noqa: E402
from llm_kit.relay import discovery as D  # noqa: E402
from llm_kit.relay import lifecycle as L  # noqa: E402
from llm_kit.relay import server as S  # noqa: E402
from llm_kit.relay import winacl  # noqa: E402

SRC = str(Path(__file__).resolve().parents[2] / "packages" / "katsoft-llm-kit" / "src")
EVERYONE = "*S-1-1-0"
USERS = "*S-1-5-32-545"
AUTHENTICATED_USERS = "*S-1-5-11"


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["_BOOTSTRAP_GUARD_SKIP_REEXEC"] = "1"
    return env


def spawn(code: str, *args: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", code, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=child_env(),
    )


def run(code: str, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code, *args], capture_output=True, text=True, env=child_env(), timeout=timeout)


HOLDER = """
import os, sys
from pathlib import Path
from llm_kit.relay import config as C, lifecycle as L
paths = C.StatePaths(Path(sys.argv[1]))
lock = L.RoleLock(paths, sys.argv[2]).acquire()
print(os.getpid(), flush=True)
sys.stdin.read()
"""

CONTENDER = """
import sys
from pathlib import Path
from llm_kit.relay import config as C, lifecycle as L
paths = C.StatePaths(Path(sys.argv[1]))
try:
    L.RoleLock(paths, sys.argv[2]).acquire().release()
    print("acquired")
except L.SingletonHeld as exc:
    print("held", exc.holder_pid, exc.code)
"""

PAUSE_WORKER = """
import sys
from pathlib import Path
from llm_kit.relay import config as C, lifecycle as L
paths = C.StatePaths(Path(sys.argv[1]))
idx, rounds = int(sys.argv[2]), int(sys.argv[3])
store = L.PauseStore(paths)
counter = paths.root / "counter.txt"
for j in range(rounds):
    store.pause("relay", f"w{idx}-{j}")
    store.resume("relay")
    with store._locked():                      # the lock itself: a read-modify-write that loses updates without it
        value = int(counter.read_text()) if counter.exists() else 0
        counter.write_text(str(value + 1))
store.pause("hub", f"last-{idx}")
print("done")
"""


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    """A private scratch directory under the user profile, replacing pytest's (which lives in %TEMP%).

    Relay state refuses an ancestor that lets another trustee write (see ``winacl.refuse_writable_ancestors``),
    and a stock %TEMP% can carry such ACEs; the profile directory is the one place Windows guarantees
    only the user, SYSTEM and Administrators can change.
    """
    import shutil
    import uuid

    base = Path.home() / f".relay-native-test-{uuid.uuid4().hex[:8]}"
    winacl.create_private_dir(base)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def state(tmp_path: Path) -> C.StatePaths:
    paths = C.StatePaths(tmp_path / "state")
    L.ensure_state_dir(paths)
    return paths


def start_holder(paths: C.StatePaths, role: str = "relay") -> tuple[subprocess.Popen, int]:
    holder = spawn(HOLDER, str(paths.root), role)
    line = holder.stdout.readline()
    assert line.strip().isdigit(), f"holder did not acquire: {line!r} {holder.stderr.read() if holder.poll() is not None else ''}"
    return holder, int(line)


def stop(holder: subprocess.Popen) -> None:
    if holder.poll() is None:
        holder.kill()
    holder.wait(timeout=30)
    for stream in (holder.stdin, holder.stdout, holder.stderr):
        if stream:
            stream.close()


# --------------------------------------------------------------------------
# locks
# --------------------------------------------------------------------------

def test_native_lock_contention(state):
    holder, holder_pid = start_holder(state)
    try:
        second = run(CONTENDER, str(state.root), "relay")
        assert second.returncode == 0, second.stderr
        assert second.stdout.split() == ["held", str(holder_pid), "already_running"]  # pid at offset 0 is readable
        # another role is independent
        other = run(CONTENDER, str(state.root), "hub")
        assert other.stdout.strip() == "acquired", other.stderr
        # in-process: the lock conflicts with a second handle in the same process too
        with pytest.raises(L.SingletonHeld) as caught:
            L.RoleLock(state, "relay").acquire()
        assert caught.value.holder_pid == holder_pid
    finally:
        stop(holder)


def test_native_lock_crash_release(state):
    holder, holder_pid = start_holder(state)
    try:
        with pytest.raises(L.SingletonHeld):
            L.RoleLock(state, "relay").acquire()
        psutil.Process(holder_pid).kill()  # TerminateProcess: no release code runs
        deadline = time.monotonic() + 15
        while True:
            try:
                L.RoleLock(state, "relay").acquire().release()
                break
            except L.SingletonHeld:
                assert time.monotonic() < deadline, "the OS did not release a killed holder's lock within 15 s"
                time.sleep(0.05)
    finally:
        stop(holder)


def test_native_pause_concurrent_updates(state):
    workers, rounds = 6, 15
    procs = [spawn(PAUSE_WORKER, str(state.root), str(i), str(rounds)) for i in range(workers)]
    try:
        for proc in procs:
            out, err = proc.communicate(timeout=180)
            assert proc.returncode == 0 and out.strip() == "done", err
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
    assert int((state.root / "counter.txt").read_text()) == workers * rounds  # no lost update under the lock
    store = L.PauseStore(state)
    flags = store.state()  # the file is valid JSON of exactly the two scopes
    assert flags["relay"]["paused"] is False
    assert flags["hub"]["paused"] is True and flags["hub"]["reason"].startswith("last-")
    json.loads(state.pause_file.read_text(encoding="utf-8"))
    winacl.validate(state.pause_file)


def test_native_pause_readers_during_writes(state):
    """A reader must not fail while a writer replaces pause.json, and the writer must not fail while a reader has it open.

    Windows refuses ``os.replace`` over a file another handle has open and refuses a read mid-replace;
    measured without the reader lock as thousands of ``PermissionError`` in 8 s of this loop.
    """
    import threading

    store = L.PauseStore(state)
    store.pause("relay", "seed")
    store.resume("relay")
    done = threading.Event()
    errors: list[str] = []
    reads = [0]

    def reader() -> None:
        while not done.is_set():
            try:
                store.is_paused("relay")
                reads[0] += 1
            except Exception as exc:  # noqa: BLE001 -- collect and assert below
                errors.append(f"read: {type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=reader) for _ in range(3)]
    for thread in threads:
        thread.start()
    try:
        for i in range(40):
            try:
                store.pause("relay", f"r{i}")
                store.resume("relay")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"write: {type(exc).__name__}: {exc}")
    finally:
        done.set()
        for thread in threads:
            thread.join(timeout=30)
    assert errors == [] and reads[0] > 0, errors[:3]


# --------------------------------------------------------------------------
# ACLs
# --------------------------------------------------------------------------

def icacls(*args: str) -> None:
    result = subprocess.run(["icacls", *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_native_acl_provisioned(state, monkeypatch):
    seen: list[tuple[str, int]] = []
    real_validate = winacl.validate

    def spy(path):
        seen.append((Path(path).name, os.path.getsize(path) if os.path.isfile(path) else -1))
        real_validate(path)

    monkeypatch.setattr(winacl, "validate", spy)
    me = winacl.current_user_sid()
    winacl.validate(state.root)  # the directory made by ensure_state_dir
    sddl = winacl.read_sddl(state.root)
    assert sddl.startswith(f"O:{me}") and "D:P" in sddl and "SY" in sddl and "(A;OICI;FA;;;" in sddl

    token = L.ensure_token(state.relay_token)
    tmp_checks = [size for name, size in seen if name.startswith(".relay.token.")]
    assert tmp_checks == [0]  # the temp file was validated while empty: no secret byte had been written
    winacl.validate(state.relay_token)
    assert L.read_token(state.relay_token) == token

    seen.clear()
    L.write_private_file(state.pause_file, "{}\n")
    assert [size for name, size in seen if name.startswith(".pause.json.")] == [0]
    winacl.validate(state.pause_file)

    peers = state.root / "peers"
    peers.mkdir()
    winacl.provision(peers, True)
    winacl.validate(peers)
    assert "(A;OICI;FA;;;" in winacl.read_sddl(peers)



def test_native_acl_refuses_broad(state):
    secret = state.root / "secret.txt"
    L.write_private_file(secret, "s3cret\n")
    assert L.read_private_file(secret) == "s3cret\n"
    for who, name in ((EVERYONE, "Everyone"), (USERS, "Users"), (AUTHENTICATED_USERS, "Authenticated Users")):
        icacls(str(secret), "/grant", f"{who}:(R)")
        with pytest.raises(C.RelayConfigError, match="grants access to"):
            winacl.validate(secret)
        with pytest.raises(C.RelayConfigError):
            L.read_private_file(secret)  # the credential readers refuse it too, not repair it
        icacls(str(secret), "/remove:g", who)
        winacl.validate(secret)  # the file is exactly as private as before: a refusal changed nothing

    token = state.root / "tok"
    token.write_text("x\n", encoding="ascii")  # created by hand: inherits the directory's ACEs, nothing protected
    with pytest.raises(C.RelayConfigError, match="unprotected DACL"):
        winacl.validate(token)
    with pytest.raises(C.RelayConfigError, match="unprotected DACL"):
        L.read_token(token)  # a pre-existing inherited-only file is refused, not repaired
    assert "P" not in winacl.read_sddl(token).split("D:", 1)[1].split("(", 1)[0]


def test_native_acl_refuses_conditional_and_unknown_aces(state):
    """A conditional allow for Users once hid from an SDDL text parser; the binary reader refuses any ACE it does not know."""
    me = winacl.current_user_sid()
    secret = state.root / "secret.txt"
    L.write_private_file(secret, "s3cret\n")
    winacl.validate(secret)
    winacl._apply_sddl(
        secret, f"O:{me}D:P(A;;FA;;;{me})(A;;FA;;;SY)(XA;;FA;;;WD;(Member_of {{SID(S-1-5-32-545)}}))"
    )
    assert "XA" in winacl.read_sddl(secret)  # Windows really stored a conditional ACE
    with pytest.raises(C.RelayConfigError, match="does not understand"):
        winacl.validate(secret)
    with pytest.raises(C.RelayConfigError, match="does not understand"):
        L.read_private_file(secret)
    # an object ACE for another trustee, likewise
    winacl._apply_sddl(secret, f"O:{me}D:P(A;;FA;;;{me})(OA;;FA;00000000-0000-0000-0000-000000000001;;WD)")
    with pytest.raises(C.RelayConfigError, match="does not understand|grants access"):
        winacl.validate(secret)


def test_native_objects_are_private_from_their_first_instant(tmp_path):
    """Created with the descriptor in the same call: protected, owner+SYSTEM only, with no provision() afterwards."""
    me = winacl.current_user_sid()
    directory = tmp_path / "born-private"
    winacl.create_private_dir(directory)
    file = directory / "born-private.txt"
    fd = winacl.create_private_file(file)
    try:
        for path in (directory, file):
            desc = winacl.read_descriptor(path)
            assert desc.protected and desc.dacl_present
            assert {a.sid for a in desc.aces} == {me, "S-1-5-18"} and all(a.ace_type == 0 for a in desc.aces)
        os.write(fd, b"x")
    finally:
        os.close(fd)
    with pytest.raises(FileExistsError):
        winacl.create_private_dir(directory)
    with pytest.raises(FileExistsError):
        winacl.create_private_file(file)
    # a file made by hand in it inherits the directory's inheritable ACEs only: unprotected, so refused
    with pytest.raises(C.RelayConfigError, match="unprotected"):
        (directory / "by-hand.txt").write_text("y")
        winacl.validate(directory / "by-hand.txt")


def test_native_state_root_refused_under_a_writable_ancestor(tmp_path):
    parent = tmp_path / "shared"
    parent.mkdir()
    icacls(str(parent), "/grant", f"{USERS}:(OI)(CI)(F)")
    root = parent / "state"
    with pytest.raises(C.RelayConfigError, match="lets S-1-5-32-545 write to it"):
        L.ensure_state_dir(C.StatePaths(root))
    assert not root.exists()  # nothing was created under a parent another user controls
    icacls(str(parent), "/remove:g", USERS)
    L.ensure_state_dir(C.StatePaths(root))  # the same path is accepted once the ancestor is clean
    winacl.validate(root)


def test_native_stock_profile_ancestors_are_accepted(tmp_path):
    winacl.refuse_writable_ancestors(tmp_path / "state")  # %TEMP% under the user profile, as Windows ships it
    chain = winacl.ancestor_chain(tmp_path / "state", Path.home())
    assert chain[-1] == Path(os.path.abspath(Path.home())) or os.path.normcase(str(chain[-1])) == os.path.normcase(str(Path.home()))


def test_native_pause_file_is_validated_before_it_is_read(state):
    store = L.PauseStore(state)
    assert store.is_paused("relay") is False  # absent file: unchanged
    store.pause("relay", "keep out")
    assert store.is_paused("relay") is True
    icacls(str(state.pause_file), "/grant", f"{USERS}:(M)")  # another user can now rewrite the pause flags
    with pytest.raises(C.RelayConfigError, match="pause file .*grants access to S-1-5-32-545"):
        store.is_paused("relay")
    with pytest.raises(C.RelayConfigError, match="pause file"):
        store.require_not_paused("relay")
    with pytest.raises(C.RelayConfigError, match="pause file"):
        store.resume("relay")  # a writer reads first and refuses too, so it cannot launder the file
    icacls(str(state.pause_file), "/remove:g", USERS)
    assert store.is_paused("relay") is True
    # a hand-made (inherited, unprotected) pause file is refused as well
    state.pause_file.unlink()
    state.pause_file.write_text(json.dumps({"relay": {"paused": False, "paused_since": None, "reason": None},
                                            "hub": {"paused": False, "paused_since": None, "reason": None}}))
    with pytest.raises(C.RelayConfigError, match="unprotected"):
        store.is_paused("relay")


def test_native_acl_refuses_redirected_root(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(real)], capture_output=True, text=True)
    assert made.returncode == 0, made.stdout + made.stderr
    with pytest.raises(C.RelayConfigError, match="junction or symlink"):
        L.ensure_state_dir(C.StatePaths(link))
    winacl.provision(real, True)
    with pytest.raises(C.RelayConfigError, match="junction or symlink"):
        winacl.validate(link)
    winacl.validate(real)  # the target itself is fine


# --------------------------------------------------------------------------
# a whole relay
# --------------------------------------------------------------------------

class FakeBackend:
    name = "fake"

    def complete(self, system, user, *, model, options=None):
        return LLMResponse(text=f"echo: {user}", model=model, finish_reason="stop", input_tokens=5, output_tokens=2)

    def classify_halt(self, exc):  # pragma: no cover - protocol member
        return None


@pytest.fixture
def relay_parts(tmp_path: Path) -> Iterator[tuple[C.RelayConfig, D.DiscoveryRoots]]:
    env_json = tmp_path / "env.json"
    env_json.write_text(json.dumps({"machines": {"winbox.local": {"os": "windows", "tailscale_hostname": "winbox"}}}),
                        encoding="utf-8")
    relay_yaml = tmp_path / "config" / "relay.yaml"
    relay_yaml.parent.mkdir(parents=True)
    relay_yaml.write_text("enabled: true\n", encoding="utf-8")
    config = C.load_config(env_json=env_json, environ={}, hostname=lambda: "winbox.local", config_path=relay_yaml,
                           state_root=tmp_path / "state")
    claude, codex = tmp_path / "claude-home", tmp_path / "codex-home"
    (claude / "sessions").mkdir(parents=True)
    (claude / "projects").mkdir(parents=True)
    (codex / "sessions").mkdir(parents=True)
    yield config, D.DiscoveryRoots(claude, codex)


def test_native_two_relays_one_state(relay_parts, tmp_path):
    config, roots = relay_parts

    def make() -> S.RelayServer:
        return S.RelayServer(
            config, port=0, roots=roots, probe=D.PsutilProbe(), poll_interval_s=0.05, maintain_interval_s=3600.0,
            backend_factory=lambda entry: SimpleNamespace(backend=FakeBackend(), model=entry.model),
            policy_loader=lambda scope: consent.policy(scope, path=tmp_path / "consent.json"),
        )

    first = make().start()
    try:
        with pytest.raises(L.SingletonHeld) as caught:
            make().start()
        assert caught.value.code == "already_running" and caught.value.holder_pid == os.getpid()
        winacl.validate(config.paths.root)
        for path in (config.paths.relay_token, config.paths.hub_bearer, config.paths.registry_file):
            winacl.validate(path)  # prepare_state's three files, each private at its final path
        winacl.validate(config.paths.pidfile("relay"))
    finally:
        first.stop()
    make().start().stop()  # the lock is released on stop
