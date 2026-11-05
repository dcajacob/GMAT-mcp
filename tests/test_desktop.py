from __future__ import annotations

import asyncio
import json
import os

import pytest

from gmat_mcp.core import Manager
from gmat_mcp.errors import GmatError


async def test_desktop_discovery_document_binding_timeout_and_safe_close(fake_config, tmp_path):
    config = fake_config.model_copy(deep=True)
    config.profiles["fake"].variant = "qt"
    config.server.desktop_discovery_dir = tmp_path / "desktops"
    config.server.desktop_discovery_dir.mkdir()
    token = "a" * 64
    state = {
        "desktop_id": "desktop",
        "document_id": "document",
        "model_version": "v1",
        "path": str(tmp_path / "mission.script"),
        "startup": str(config.profiles["fake"].startup),
        "project_dir": str(tmp_path),
        "running": False,
        "dirty": False,
        "run": {},
        "engine": {
            "api": "test",
            "python": "desktop",
            "platform": "test",
            "version": "test",
            "native_libraries": [],
            "classes": [],
            "capabilities": ["live_desktop"],
        },
    }
    hang = False
    handlers = set()

    async def serve(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            while line := await reader.readline():
                request = json.loads(line)
                if hang:
                    await asyncio.Event().wait()
                reply = (
                    {"ok": True, "result": state}
                    if request["token"] == token
                    else {
                        "ok": False,
                        "error": {"code": "AUTHENTICATION_FAILED", "message": "Invalid token"},
                    }
                )
                writer.write(json.dumps(reply).encode() + b"\n")
                await writer.drain()
        finally:
            handlers.discard(task)
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    record = config.server.desktop_discovery_dir / "desktop.json"
    record.write_text(
        json.dumps(
            {
                "protocol": 1,
                "port": port,
                "token": token,
                "desktop_id": "desktop",
                "installation_root": str(config.profiles["fake"].root),
            }
        )
    )
    record.chmod(0o600)
    if os.name == "nt":
        from gmat_mcp.windows_security import protect

        protect(record)
    manager = Manager(config)
    try:
        found = await manager.list_desktops()
        assert len(found["desktops"]) == 1 and token not in json.dumps(found)
        session = await manager.attach_desktop("desktop", "fake")
        sid = session["session_id"]
        backend = manager.session(sid).worker
        with pytest.raises(GmatError, match="1 MiB"):
            await backend.rpc("request", large="x" * (1024 * 1024))
        state["document_id"] = "different"
        with pytest.raises(GmatError, match="document changed"):
            await manager.inspect_session(sid)
        await manager.close_session(sid)
        assert server.is_serving()
        state["document_id"] = "document"
        sid = (await manager.attach_desktop("desktop", "fake"))["session_id"]
        manager.session(sid).worker.timeout = 0.02
        hang = True
        with pytest.raises(GmatError, match="application was preserved"):
            await manager.inspect_session(sid)
        assert manager.session(sid).state == "lost" and server.is_serving()
        await manager.close_session(sid)
    finally:
        await manager.shutdown()
        server.close()
        for task in list(handlers):
            task.cancel()
        await asyncio.gather(*list(handlers), return_exceptions=True)
        await server.wait_closed()


@pytest.mark.skipif(os.name == "nt", reason="POSIX discovery permissions")
async def test_public_discovery_records_are_ignored(fake_config, tmp_path):
    fake_config.server.desktop_discovery_dir = tmp_path / "desktops"
    fake_config.server.desktop_discovery_dir.mkdir()
    path = fake_config.server.desktop_discovery_dir / "untrusted.json"
    path.write_text(json.dumps({"protocol": 1, "port": 1234, "token": "a" * 64}))
    path.chmod(0o644)
    manager = Manager(fake_config)
    try:
        assert (await manager.list_desktops())["desktops"] == []
    finally:
        await manager.shutdown()


@pytest.mark.skipif(os.name != "nt", reason="NTFS ACL contract")
def test_windows_discovery_requires_current_user_acl(tmp_path):
    from gmat_mcp.windows_security import is_private, protect

    path = tmp_path / "private.json"
    path.write_text("{}")
    protect(path)
    assert is_private(path)
    import ntsecuritycon
    import win32security as security

    descriptor = security.GetNamedSecurityInfo(
        str(path), security.SE_FILE_OBJECT, security.DACL_SECURITY_INFORMATION
    )
    acl = descriptor.GetSecurityDescriptorDacl()
    everyone = security.CreateWellKnownSid(security.WinWorldSid, None)
    acl.AddAccessAllowedAce(security.ACL_REVISION, ntsecuritycon.FILE_GENERIC_READ, everyone)
    security.SetNamedSecurityInfo(
        str(path),
        security.SE_FILE_OBJECT,
        security.DACL_SECURITY_INFORMATION,
        None,
        None,
        acl,
        None,
    )
    assert not is_private(path)
