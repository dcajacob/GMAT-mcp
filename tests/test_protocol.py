from __future__ import annotations

import json

import httpx2
import pytest

from .protocol_support import call, connected, http_server, wait_run, write_config


@pytest.mark.parametrize("transport", ["stdio", "http"])
async def test_mcp_lifecycle_resources_and_errors(fake_config, tmp_path, transport):
    path = tmp_path / "profiles.toml"
    write_config(fake_config, path)
    async with connected(transport, path, tmp_path) as client:
        tools = await client.list_tools()
        assert {"create_object", "set_field", "run_mission", "load_script"} <= {
            t.name for t in tools.tools
        }
        profiles = await call(client, "list_profiles")
        assert profiles["profiles"][0]["id"] == "fake"
        sid = (await call(client, "create_session", profile_id="fake"))["session_id"]
        await call(client, "create_object", session_id=sid, type="Spacecraft", name="Sat")
        await call(client, "set_field", session_id=sid, name="Sat", field="DryMass", value=250.0)
        await call(
            client, "append_command", session_id=sid, type="Propagate", description="Prop(Sat)"
        )
        commands = await call(client, "list_commands", session_id=sid)
        assert commands["commands"][0]["type"] == "Propagate"
        assert (
            "Spacecraft"
            in (await call(client, "get_help", session_id=sid, topic="Spacecraft"))["text"]
        )
        await call(client, "validate_mission", session_id=sid)
        run = await wait_run(client, await call(client, "run_mission", session_id=sid))
        assert run["status"] == "succeeded"
        artifacts = await call(client, "list_artifacts", run_id=run["run_id"])
        first = artifacts["artifacts"][0]
        await call(client, "read_artifact", run_id=run["run_id"], artifact_id=first["id"], limit=20)
        resource = await client.read_resource(first["uri"])
        assert json.loads(resource.contents[0].text)["path"] == first["path"]
        resource = await client.read_resource("gmat://sessions/" + sid)
        assert json.loads(resource.contents[0].text)["session_id"] == sid
        error = await client.call_tool("get_object", {"session_id": sid, "name": "Missing"})
        assert error.is_error
        assert "GMAT_ERROR" in error.content[0].text
        await call(client, "close_session", session_id=sid)


async def test_http_auth_origin_host(fake_config, tmp_path):
    path = tmp_path / "profiles.toml"
    write_config(fake_config, path)
    async with http_server(path, tmp_path) as (url, token, port):
        async with httpx2.AsyncClient() as http:
            assert (await http.post(url)).status_code == 401
            assert (
                await http.post(url, headers={"Authorization": "Bearer wrong"})
            ).status_code == 401
            assert (
                await http.post(
                    url,
                    headers={"Authorization": f"Bearer {token}", "Origin": "https://evil.example"},
                )
            ).status_code == 403
            assert (
                await http.post(
                    url, headers={"Authorization": f"Bearer {token}", "Host": "evil.example"}
                )
            ).status_code == 403
            # A missing Origin is normal for native MCP clients. An allowed Origin
            # reaches the MCP app, which rejects this deliberately invalid JSON body.
            response = await http.post(
                url,
                headers={"Authorization": f"Bearer {token}", "Origin": f"http://127.0.0.1:{port}"},
            )
            assert response.status_code not in (401, 403)
