"""WebSocket hub: tracks connected clients and keeps every device in sync in real time."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

from .memory import Memory

log = logging.getLogger("jarvis.hub")


@dataclass
class Client:
    ws: WebSocket
    client_id: str
    device_id: str
    device_name: str
    platform: str
    agent: str  # "browser" | "electron"
    session_id: str
    is_local: bool
    connected_at: float = field(default_factory=time.time)
    mic: str = "unknown"  # "listening" | "muted"
    timezone: str | None = None             # IANA zone reported by the device
    location: dict | None = None            # real position {name, admin, country, latitude, longitude, label}

    @property
    def is_desktop(self) -> bool:
        return self.agent == "electron"


class AgentLink:
    """A helper program running on another computer (e.g. mac_agent.js on the MacBook).

    It long-polls the server for jobs, so it works through the same HTTPS port as the HUD."""

    def __init__(self, name: str, platform: str):
        self.name, self.platform = name, platform
        self.queue: asyncio.Queue = asyncio.Queue()
        self.last_seen = time.time()

    @property
    def online(self) -> bool:
        return time.time() - self.last_seen < 45


class Hub:
    def __init__(self, memory: Memory):
        self.memory = memory
        self.clients: dict[str, Client] = {}
        self._pending: dict[str, asyncio.Future] = {}
        self._send_locks: dict[str, asyncio.Lock] = {}
        self.agents: dict[str, AgentLink] = {}

    # ---------- connections ----------
    async def register(self, client: Client) -> None:
        self.clients[client.client_id] = client
        self._send_locks[client.client_id] = asyncio.Lock()
        await self.memory.upsert_device(client.device_id, client.device_name, client.platform, client.agent)
        log.info("+ %s (%s, %s) session=%s", client.device_name, client.platform, client.agent, client.session_id)
        await self.broadcast_devices(exclude=client.client_id)  # the new client gets them in its hello message

    async def unregister(self, client_id: str) -> None:
        client = self.clients.pop(client_id, None)
        self._send_locks.pop(client_id, None)
        for req_id, fut in list(self._pending.items()):
            if req_id.startswith(client_id) and not fut.done():
                fut.set_result({"error": "The device disconnected before completing the action."})
        if client:
            await self.memory.touch_device(client.device_id)
            log.info("- %s", client.device_name)
            await self.broadcast_devices()

    # ---------- sending ----------
    async def send(self, client_id: str, payload: dict[str, Any]) -> None:
        client = self.clients.get(client_id)
        if not client:
            return
        try:
            async with self._send_locks[client_id]:
                await client.ws.send_text(json.dumps(payload, ensure_ascii=False, default=str))
        except Exception:  # dead connection: cleaned up by the receive loop
            log.debug("send failed to %s", client_id)

    async def broadcast(self, payload: dict[str, Any], session_id: str | None = None, exclude: str | None = None) -> None:
        targets = [c.client_id for c in self.clients.values()
                   if (session_id is None or c.session_id == session_id) and c.client_id != exclude]
        await asyncio.gather(*(self.send(cid, payload) for cid in targets))

    # ---------- devices ----------
    async def devices_snapshot(self) -> list[dict]:
        known = await self.memory.devices()
        online: dict[str, Client] = {}
        for c in self.clients.values():
            online.setdefault(c.device_id, c)
        out = []
        for d in known:
            c = online.get(d["device_id"])
            out.append({
                "device_id": d["device_id"],
                "name": d["name"],
                "platform": d["platform"],
                "agent": d["agent"],
                "online": c is not None,
                "mic": c.mic if c else "off",
                "location": (c.location or {}).get("name") if c else None,
                "helper": bool((a := self.agents.get(d["name"].upper())) and a.online),
                "session": c.session_id if c else None,
                "last_seen": d["last_seen"],
            })
        return out

    async def broadcast_devices(self, exclude: str | None = None) -> None:
        await self.broadcast({"type": "devices", "devices": await self.devices_snapshot()}, exclude=exclude)

    # ---------- tools executed on the client (e.g. system_control on the Mac) ----------
    async def request_client_tool(self, client_id: str, name: str, tool_input: dict, timeout: float = 20.0) -> Any:
        req_id = f"{client_id}:{uuid.uuid4().hex[:10]}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        await self.send(client_id, {"type": "client_tool", "request_id": req_id, "name": name, "input": tool_input})
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return {"error": f"The device did not respond within {int(timeout)}s."}
        finally:
            self._pending.pop(req_id, None)

    # ---------- helper agents on other computers ----------
    async def agent_poll(self, name: str, platform: str, wait: float = 25.0) -> dict | None:
        key = name.upper()
        link = self.agents.get(key)
        if link is None:
            link = self.agents[key] = AgentLink(key, platform)
            log.info("+ helper agent %s (%s)", key, platform)
            asyncio.create_task(self.broadcast_devices())
        link.last_seen = time.time()
        try:
            return await asyncio.wait_for(link.queue.get(), wait)
        except asyncio.TimeoutError:
            return None
        finally:
            link.last_seen = time.time()

    def agent_for(self, device_name: str) -> AgentLink | None:
        link = self.agents.get(device_name.upper())
        return link if link and link.online else None

    async def request_agent(self, device_name: str, action: str, params: dict, timeout: float = 25.0) -> Any:
        """Run an action through the helper agent of that computer. None = no agent connected."""
        link = self.agent_for(device_name)
        if link is None:
            return None
        job_id = f"agent:{uuid.uuid4().hex[:10]}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[job_id] = fut
        await link.queue.put({"job_id": job_id, "action": action, "params": params})
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return {"error": f"The {device_name} helper did not answer within {int(timeout)}s."}
        finally:
            self._pending.pop(job_id, None)

    def resolve_client_tool(self, req_id: str, result: Any) -> None:
        fut = self._pending.get(req_id)
        if fut and not fut.done():
            fut.set_result(result)
