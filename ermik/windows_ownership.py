"""Fail-closed Windows owner and write-access check for local artifacts."""

from __future__ import annotations

import ctypes
from ctypes import wintypes


def owned_for_cleanup(path: str) -> bool:
    """Require the current user to own a path without third-party write grants."""
    if not hasattr(ctypes, "WinDLL"):
        return False
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    security.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, wintypes.DWORD,
        wintypes.DWORD, ctypes.POINTER(pointer), ctypes.POINTER(pointer),
        ctypes.POINTER(pointer), ctypes.POINTER(pointer), ctypes.POINTER(pointer)]
    security.GetNamedSecurityInfoW.restype = wintypes.DWORD
    security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE)]
    security.OpenProcessToken.restype = wintypes.BOOL
    security.GetTokenInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD,
        pointer, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    security.GetTokenInformation.restype = wintypes.BOOL
    security.EqualSid.argtypes = [pointer, pointer]
    security.EqualSid.restype = wintypes.BOOL
    security.GetAclInformation.argtypes = [pointer, pointer, wintypes.DWORD,
        wintypes.DWORD]
    security.GetAclInformation.restype = wintypes.BOOL
    security.GetAce.argtypes = [pointer, wintypes.DWORD, ctypes.POINTER(pointer)]
    security.GetAce.restype = wintypes.BOOL
    security.ConvertSidToStringSidW.argtypes = [pointer, ctypes.POINTER(pointer)]
    security.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [pointer]

    owner, dacl, descriptor = pointer(), pointer(), pointer()
    token = wintypes.HANDLE()
    try:
        # SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION.
        if security.GetNamedSecurityInfoW(path, 1, 5, ctypes.byref(owner),
                None, ctypes.byref(dacl), None, ctypes.byref(descriptor)) != 0:
            return False
        if not owner or not dacl:
            return False
        if not security.OpenProcessToken(kernel.GetCurrentProcess(), 8,
                                         ctypes.byref(token)):
            return False
        needed = wintypes.DWORD()
        security.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        if needed.value < ctypes.sizeof(pointer):
            return False
        user = ctypes.create_string_buffer(needed.value)
        if not security.GetTokenInformation(token, 1, user, needed,
                                             ctypes.byref(needed)):
            return False
        user_sid = pointer.from_buffer(user).value
        if not security.EqualSid(owner, pointer(user_sid)):
            return False

        class AclSize(ctypes.Structure):
            _fields_ = [("ace_count", wintypes.DWORD),
                        ("acl_bytes_in_use", wintypes.DWORD),
                        ("acl_bytes_free", wintypes.DWORD)]

        size = AclSize()
        if not security.GetAclInformation(dacl, ctypes.byref(size),
                                           ctypes.sizeof(size), 2):
            return False
        for index in range(size.ace_count):
            ace = pointer()
            if not security.GetAce(dacl, index, ctypes.byref(ace)):
                return False
            address = ace.value
            ace_type = ctypes.c_ubyte.from_address(address).value
            if ace_type in (1, 2, 3):  # Deny or audit; they do not grant access.
                continue
            if ace_type != 0:  # Unknown allow/object ACE: cannot prove safety.
                return False
            mask = ctypes.c_uint32.from_address(address + 4).value
            # Generic write/all, file write/append/attributes, delete and ACL edits.
            if not mask & (0x50000000 | 0x000F0000 | 0x00000156):
                continue
            sid = pointer(address + 8)
            if security.EqualSid(sid, owner):
                continue
            text = pointer()
            if not security.ConvertSidToStringSidW(sid, ctypes.byref(text)):
                return False
            try:
                if ctypes.wstring_at(text) not in (
                        "S-1-5-18", "S-1-5-32-544", "S-1-3-4"):
                    return False
            finally:
                kernel.LocalFree(text)
        return True
    finally:
        if token:
            kernel.CloseHandle(token)
        if descriptor:
            kernel.LocalFree(descriptor)
