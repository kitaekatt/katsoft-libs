"""Windows access control for the relay state directory (stdlib ctypes over advapi32).

POSIX keeps the state directory ``0700`` and every credential ``0600``. On
Windows ``os.chmod`` only toggles the read-only flag, so the same promise is
kept with an owner and a PROTECTED discretionary ACL that grants full control
to the current user and SYSTEM only:

* :func:`provision` sets the owner to the current user and that DACL. A
  directory's ACEs are inheritable, so a file created inside it starts private.
* :func:`validate` reads the security descriptor as SDDL and refuses what
  :func:`provision` would never have written (see :func:`check_sddl`). It never
  repairs: a broad file is refused, as on POSIX.

Importing this module is safe on every platform; the Win32 calls load on first
use and raise :class:`RelayConfigError` anywhere but Windows.
"""
from __future__ import annotations

import ctypes
import os
import re
from pathlib import Path
from typing import Any, Optional

from llm_kit.relay.config import RelayConfigError

#: Allow-ACE types in SDDL (allow, object allow, callback allow, callback object allow).
_ALLOW_TYPES = frozenset({"A", "OA", "XA", "ZA"})
#: SDDL spellings of SYSTEM.
_SYSTEM = frozenset({"SY", "S-1-5-18"})

_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x1
_DACL_SECURITY_INFORMATION = 0x4
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SDDL_REVISION_1 = 1
_TOKEN_QUERY = 0x8
_TOKEN_USER = 1

_api_cache: Optional[Any] = None
_sid_cache: Optional[str] = None


class _Api:
    """The Win32 entry points with explicit prototypes (so 64-bit pointers survive)."""

    def __init__(self) -> None:
        from ctypes import wintypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        self.wintypes = wintypes
        vp = ctypes.c_void_p
        pvp = ctypes.POINTER(ctypes.c_void_p)
        pw = ctypes.POINTER(ctypes.c_wchar_p)
        pdw = ctypes.POINTER(wintypes.DWORD)
        pbool = ctypes.POINTER(wintypes.BOOL)

        self.GetCurrentProcess = kernel32.GetCurrentProcess
        self.GetCurrentProcess.restype = wintypes.HANDLE
        self.CloseHandle = kernel32.CloseHandle
        self.CloseHandle.argtypes = [wintypes.HANDLE]
        self.LocalFree = kernel32.LocalFree
        self.LocalFree.argtypes = [vp]
        self.LocalFree.restype = vp
        self.GetLongPathNameW = kernel32.GetLongPathNameW
        self.GetLongPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        self.GetLongPathNameW.restype = wintypes.DWORD

        self.OpenProcessToken = advapi32.OpenProcessToken
        self.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        self.OpenProcessToken.restype = wintypes.BOOL
        self.GetTokenInformation = advapi32.GetTokenInformation
        self.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, vp, wintypes.DWORD, pdw]
        self.GetTokenInformation.restype = wintypes.BOOL
        self.ConvertSidToStringSidW = advapi32.ConvertSidToStringSidW
        self.ConvertSidToStringSidW.argtypes = [vp, pw]
        self.ConvertSidToStringSidW.restype = wintypes.BOOL
        self.ConvertStringSecurityDescriptorToSecurityDescriptorW = (
            advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
        )
        self.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, pvp, pdw,
        ]
        self.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
        self.ConvertSecurityDescriptorToStringSecurityDescriptorW = (
            advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW
        )
        self.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
            vp, wintypes.DWORD, wintypes.DWORD, pw, pdw,
        ]
        self.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
        self.GetSecurityDescriptorOwner = advapi32.GetSecurityDescriptorOwner
        self.GetSecurityDescriptorOwner.argtypes = [vp, pvp, pbool]
        self.GetSecurityDescriptorOwner.restype = wintypes.BOOL
        self.GetSecurityDescriptorDacl = advapi32.GetSecurityDescriptorDacl
        self.GetSecurityDescriptorDacl.argtypes = [vp, pbool, pvp, pbool]
        self.GetSecurityDescriptorDacl.restype = wintypes.BOOL
        self.SetNamedSecurityInfoW = advapi32.SetNamedSecurityInfoW
        self.SetNamedSecurityInfoW.argtypes = [
            wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, vp, vp, vp, vp,
        ]
        self.SetNamedSecurityInfoW.restype = wintypes.DWORD
        self.GetNamedSecurityInfoW = advapi32.GetNamedSecurityInfoW
        self.GetNamedSecurityInfoW.argtypes = [
            wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, vp, vp, vp, vp, pvp,
        ]
        self.GetNamedSecurityInfoW.restype = wintypes.DWORD


def _api() -> _Api:
    global _api_cache
    if os.name != "nt":
        raise RelayConfigError("Windows access control (winacl) is only available on Windows")
    if _api_cache is None:
        _api_cache = _Api()
    return _api_cache


def _winerror(what: str, code: int) -> RelayConfigError:
    return RelayConfigError(f"{what} failed: {ctypes.FormatError(code).strip()} (Windows error {code})")  # type: ignore[attr-defined]


def current_user_sid() -> str:
    """The current process user's SID as a string (``S-1-5-21-...``)."""
    global _sid_cache
    if _sid_cache is not None:
        return _sid_cache
    api = _api()
    wt = api.wintypes
    token = wt.HANDLE()
    if not api.OpenProcessToken(api.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        raise _winerror("OpenProcessToken", ctypes.get_last_error())  # type: ignore[attr-defined]
    try:
        needed = wt.DWORD(0)
        api.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(needed))
        buf = ctypes.create_string_buffer(needed.value)
        if not api.GetTokenInformation(token, _TOKEN_USER, buf, needed.value, ctypes.byref(needed)):
            raise _winerror("GetTokenInformation", ctypes.get_last_error())  # type: ignore[attr-defined]
        sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        text = ctypes.c_wchar_p()
        if not api.ConvertSidToStringSidW(sid_ptr, ctypes.byref(text)):
            raise _winerror("ConvertSidToStringSidW", ctypes.get_last_error())  # type: ignore[attr-defined]
        try:
            _sid_cache = str(text.value)
        finally:
            api.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        api.CloseHandle(token)
    return _sid_cache


def _sddl_for(sid: str, *, directory: bool) -> str:
    """The SDDL :func:`provision` applies: owner ``sid``, protected DACL for ``sid`` and SYSTEM."""
    flags = "OICI" if directory else ""
    return f"O:{sid}D:P(A;{flags};FA;;;{sid})(A;{flags};FA;;;SY)"


def provision(path: "os.PathLike[str] | str", directory: bool = False) -> None:
    """Make ``path`` private: owner = current user, protected DACL = that user + SYSTEM only.

    Idempotent. ``directory`` makes the ACEs inheritable, so files created in
    it start private. A failure (for example an owner this process may not
    assign) raises :class:`RelayConfigError`.
    """
    api = _api()
    wt = api.wintypes
    sd = ctypes.c_void_p()
    if not api.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        _sddl_for(current_user_sid(), directory=directory), _SDDL_REVISION_1, ctypes.byref(sd), None
    ):
        raise _winerror("ConvertStringSecurityDescriptorToSecurityDescriptorW", ctypes.get_last_error())  # type: ignore[attr-defined]
    try:
        owner = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        flag = wt.BOOL()
        present = wt.BOOL()
        if not api.GetSecurityDescriptorOwner(sd, ctypes.byref(owner), ctypes.byref(flag)):
            raise _winerror("GetSecurityDescriptorOwner", ctypes.get_last_error())  # type: ignore[attr-defined]
        if not api.GetSecurityDescriptorDacl(sd, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(flag)):
            raise _winerror("GetSecurityDescriptorDacl", ctypes.get_last_error())  # type: ignore[attr-defined]
        info = _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION
        code = api.SetNamedSecurityInfoW(
            str(path), _SE_FILE_OBJECT, info, owner, None, dacl, None
        )
        if code != 0:
            raise _winerror(f"setting the owner and ACL of {path}", code)
    finally:
        api.LocalFree(sd)


def read_sddl(path: "os.PathLike[str] | str") -> str:
    """The owner and DACL of ``path`` as SDDL text (``O:...D:...``)."""
    api = _api()
    psd = ctypes.c_void_p()
    code = api.GetNamedSecurityInfoW(
        str(path), _SE_FILE_OBJECT, _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
        None, None, None, None, ctypes.byref(psd),
    )
    if code != 0:
        raise _winerror(f"reading the security descriptor of {path}", code)
    try:
        text = ctypes.c_wchar_p()
        if not api.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            psd, _SDDL_REVISION_1, _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
            ctypes.byref(text), None,
        ):
            raise _winerror("ConvertSecurityDescriptorToStringSecurityDescriptorW", ctypes.get_last_error())  # type: ignore[attr-defined]
        try:
            return str(text.value)
        finally:
            api.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        api.LocalFree(psd)


#: A SID in SDDL is ``S-...`` or a two-letter alias such as ``SY``.
_TRUSTEE = r"(?:S-[0-9]+(?:-[0-9]+)+|[A-Z]{2})"
_SDDL_RE = re.compile(rf"^O:(?P<owner>{_TRUSTEE})(?:G:{_TRUSTEE})?(?:D:(?P<dacl>.*))?$", re.DOTALL)
_ACE_RE = re.compile(r"\(([^()]*)\)")


def check_sddl(sddl: str, user_sid: str, path: object) -> None:
    """Refuse ``sddl`` unless it is what :func:`provision` writes: pure and portable.

    Refused: an owner other than ``user_sid``; no DACL or a null DACL; a DACL
    that is not protected (it would follow its parent); an allow ACE for any
    trustee other than ``user_sid`` or SYSTEM; and a DACL with no allow ACE for
    ``user_sid`` at all. Deny and audit ACEs are not grants and are ignored.
    """
    match = _SDDL_RE.match(sddl.strip())
    if match is None:
        raise RelayConfigError(f"{path} has a security descriptor this relay cannot read: {sddl!r}")
    owner = match["owner"]
    if owner != user_sid:
        raise RelayConfigError(f"{path} is not owned by the current user (owner {owner}, expected {user_sid})")
    dacl = match["dacl"]
    if dacl is None or dacl.startswith("NO_ACCESS_CONTROL") or not dacl:
        raise RelayConfigError(f"{path} has a null or missing DACL, which grants everyone access")
    flags = dacl.split("(", 1)[0]
    if "P" not in flags:
        raise RelayConfigError(f"{path} has an unprotected DACL (flags {flags!r}); it inherits access from its parent")
    allowed = {user_sid} | _SYSTEM
    user_granted = False
    for ace in _ACE_RE.findall(dacl):
        fields = ace.split(";")
        if fields[0] not in _ALLOW_TYPES:
            continue
        trustee = fields[5] if len(fields) > 5 else ""
        if trustee not in allowed:
            raise RelayConfigError(f"{path} grants access to {trustee or 'an unnamed trustee'}; only the current user and SYSTEM may have it")
        if trustee == user_sid:
            user_granted = True
    if not user_granted:
        raise RelayConfigError(f"{path} grants the current user no access")


def _long_abs(path: str, api: _Api) -> str:
    """The absolute path with 8.3 short names expanded, so it is comparable to a real path."""
    absolute = os.path.abspath(path)
    buf = ctypes.create_unicode_buffer(32768)
    n = api.GetLongPathNameW(absolute, buf, len(buf))
    return buf.value if 0 < n < len(buf) else absolute


def _strip_prefix(path: str) -> str:
    return path[4:] if path.startswith("\\\\?\\") and not path.startswith("\\\\?\\UNC\\") else path


def refuse_redirected(path: "os.PathLike[str] | str") -> None:
    """Refuse ``path`` when its real path differs from its absolute path (a junction or symlink)."""
    api = _api()
    absolute = _strip_prefix(_long_abs(str(path), api))
    real = _strip_prefix(os.path.realpath(str(path)))
    if os.path.normcase(os.path.normpath(real)) != os.path.normcase(os.path.normpath(absolute)):
        raise RelayConfigError(
            f"{path} resolves to {real}, not {absolute}; a junction or symlink may not redirect relay state"
        )


def validate(path: "os.PathLike[str] | str") -> None:
    """Refuse ``path`` unless it is private to the current user (see :func:`check_sddl`).

    Also refuses a path whose real path differs from its absolute path.
    Raises :class:`RelayConfigError`; never repairs.
    """
    refuse_redirected(path)
    check_sddl(read_sddl(path), current_user_sid(), Path(path))


__all__ = ["check_sddl", "current_user_sid", "provision", "read_sddl", "refuse_redirected", "validate"]
