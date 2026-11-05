from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client


def write_config(config, path: Path):
    lines = [
        "[server]",
        f"data_dir = {json.dumps(str(config.server.data_dir))}",
        f"desktop_discovery_dir = {json.dumps(str(config.server.desktop_discovery_dir))}",
        f"operation_timeout = {config.server.operation_timeout}",
        f"run_timeout = {config.server.run_timeout}",
        f"max_sessions = {config.server.max_sessions}",
    ]
    for name, profile in config.profiles.items():
        lines += [
            f"[profiles.{json.dumps(name)}]",
            f"variant = {json.dumps(profile.variant)}",
            f"root = {json.dumps(str(profile.root))}",
            f"python_executable = {json.dumps(profile.python_executable)}",
            f"startup_file = {json.dumps(str(profile.startup))}",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")


async def call(client, tool_name, **arguments):
    result = await client.call_tool(tool_name, arguments)
    assert not result.is_error, f"{tool_name}: {result}"
    assert result.structured_content is not None, f"No structured result for {tool_name}"
    return result.structured_content


async def wait_run(client, run):
    async with asyncio.timeout(60):
        while run["status"] in ("queued", "running"):
            await asyncio.sleep(0.05)
            run = await call(client, "get_run", run_id=run["run_id"])
    return run


@asynccontextmanager
async def http_server(config_path, tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    url = f"http://127.0.0.1:{port}/mcp"
    env = {**os.environ, "GMAT_MCP_TOKEN": "test-local-bearer-token"}
    log_path = tmp_path / f"http-{port}.log"
    with log_path.open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "gmat_mcp",
            "--config",
            str(config_path),
            "serve",
            "--transport",
            "streamable-http",
            "--port",
            str(port),
            env=env,
            stdout=log,
            stderr=log,
        )
    try:
        async with httpx2.AsyncClient() as http:
            async with asyncio.timeout(20):
                while True:
                    if process.returncode is not None:
                        raise RuntimeError(log_path.read_text())
                    try:
                        response = await http.get(url)
                        if response.status_code == 401:
                            break
                    except httpx2.ConnectError:
                        pass
                    await asyncio.sleep(0.05)
        yield url, env["GMAT_MCP_TOKEN"], port
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 10)
            except TimeoutError:
                process.kill()
                await process.wait()


@asynccontextmanager
async def connected(transport, config_path, tmp_path):
    if transport == "stdio":
        async with Client(
            StdioServerParameters(
                command=sys.executable,
                args=["-m", "gmat_mcp", "--config", str(config_path), "serve"],
            ),
            cache=None,
        ) as client:
            yield client
    else:
        async with http_server(config_path, tmp_path) as (url, token, _):
            async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http:
                async with Client(
                    streamable_http_client(url, http_client=http), cache=None
                ) as client:
                    yield client
