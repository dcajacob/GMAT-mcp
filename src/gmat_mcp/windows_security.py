"""User-private NTFS discovery records; used only by the server, never workers."""

from __future__ import annotations


def protect(path):
    import ntsecuritycon
    import win32api
    import win32con
    import win32security as security

    token = security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        user = security.GetTokenInformation(token, security.TokenUser)[0]
    finally:
        token.Close()
    acl = security.ACL()
    inheritance = (
        security.OBJECT_INHERIT_ACE | security.CONTAINER_INHERIT_ACE if path.is_dir() else 0
    )
    acl.AddAccessAllowedAceEx(
        security.ACL_REVISION, inheritance, ntsecuritycon.FILE_ALL_ACCESS, user
    )
    security.SetNamedSecurityInfo(
        str(path),
        security.SE_FILE_OBJECT,
        security.OWNER_SECURITY_INFORMATION
        | security.DACL_SECURITY_INFORMATION
        | security.PROTECTED_DACL_SECURITY_INFORMATION,
        user,
        None,
        acl,
        None,
    )


def is_private(path):
    import win32api
    import win32con
    import win32security as security

    token = security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        user = security.GetTokenInformation(token, security.TokenUser)[0]
    finally:
        token.Close()
    descriptor = security.GetNamedSecurityInfo(
        str(path),
        security.SE_FILE_OBJECT,
        security.OWNER_SECURITY_INFORMATION | security.DACL_SECURITY_INFORMATION,
    )
    if descriptor.GetSecurityDescriptorOwner() != user:
        return False
    acl = descriptor.GetSecurityDescriptorDacl()
    if acl is None or not acl.GetAceCount():
        return False
    for index in range(acl.GetAceCount()):
        (kind, flags), mask, sid = acl.GetAce(index)
        # Accept ordinary allow/deny ACEs only; fail closed for unfamiliar ACLs.
        if kind not in (security.ACCESS_ALLOWED_ACE_TYPE, security.ACCESS_DENIED_ACE_TYPE):
            return False
        if kind == security.ACCESS_ALLOWED_ACE_TYPE and mask and sid != user:
            return False
    return True
