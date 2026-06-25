"""Signed HTTP upload of recorded routes & logs + model artifact download (aiohttp).

The realtime WebSocket carries small status/command frames; bulk artifacts (drive segment logs,
crash/system logs) and model downloads go over plain authenticated HTTP instead. Each request is
signed with the device's Ed25519 key exactly like the other device-self calls.
"""

from __future__ import annotations

import json

import aiohttp

from mypilot_protocol.signing import build_signed_headers

from .config import AgentConfig
from .identity import Identity


class IngestClient:
    def __init__(self, cfg: AgentConfig, identity: Identity) -> None:
        self.cfg = cfg
        self.identity = identity
        self.http = aiohttp.ClientSession()

    async def close(self) -> None:
        await self.http.close()

    def _headers(self, method: str, path: str, body: bytes) -> dict[str, str]:
        return build_signed_headers(
            self.identity.device_id, self.identity.private_key_b64, method, path, body
        )

    async def _post_json(self, path: str, payload: dict) -> dict:
        body = json.dumps(payload).encode("utf-8")
        headers = self._headers("POST", path, body)
        headers["Content-Type"] = "application/json"
        async with self.http.post(self.cfg.api_base + path, data=body, headers=headers) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _put_bytes(self, path: str, body: bytes, content_type: str) -> dict:
        headers = self._headers("PUT", path, body)
        headers["Content-Type"] = content_type
        async with self.http.put(self.cfg.api_base + path, data=body, headers=headers) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def download_model(self, model_key: str) -> bytes:
        """Signed GET of a model artifact (the device verifies its sha256 before activating)."""
        path = f"/api/devices/self/models/{model_key}/download"
        headers = self._headers("GET", path, b"")
        async with self.http.get(self.cfg.api_base + path, headers=headers) as resp:
            resp.raise_for_status()
            return await resp.read()

    async def upload_route(self, route: dict) -> str:
        decls = [
            {"segment_index": f["segment_index"], "name": f["name"], "kind": f["kind"]}
            for f in route["files"]
        ]
        start = await self._post_json(
            "/api/ingest/routes/start",
            {
                "name": route["name"],
                "alias": route.get("alias"),
                "started_at": route.get("started_at"),
                "ended_at": route.get("ended_at"),
                "duration_s": route.get("duration_s"),
                "distance_m": route.get("distance_m"),
                "segment_count": route.get("segment_count", len(decls)),
                "start_location": route.get("start_location"),
                "end_location": route.get("end_location"),
                "files": decls,
            },
        )
        route_id = start["route_id"]
        for f in route["files"]:
            path = f"/api/ingest/routes/{route_id}/files/{f['segment_index']}/{f['name']}"
            await self._put_bytes(path, f["data"], "application/octet-stream")
        await self._post_json(f"/api/ingest/routes/{route_id}/complete", {})
        return route_id

    async def upload_log(self, log: dict) -> str:
        start = await self._post_json(
            "/api/ingest/logs/start",
            {"kind": log["kind"], "name": log["name"], "route_name": log.get("route_name")},
        )
        log_id = start["id"]
        ctype = "text/plain" if log["kind"] == "crash" else "application/octet-stream"
        await self._put_bytes(f"/api/ingest/logs/{log_id}/content", log["data"], ctype)
        return log_id
