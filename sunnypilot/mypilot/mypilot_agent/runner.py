"""Agent orchestration: pairing, the realtime WebSocket session, and reconnect handling."""

from __future__ import annotations

import asyncio
import json
import os

import aiohttp

from mypilot_protocol.crypto import sign
from mypilot_protocol.messages import FrameType, ws_auth_message

from . import identity as identity_mod
from .backends import make_backend
from .backends.base import DeviceBackend
from .client import StackClient
from .config import AgentConfig
from .identity import Identity
from .uploader import IngestClient


class NeedsRepair(Exception):
    """Raised when the Stack no longer recognizes this device (e.g. it was revoked)."""


class WSClosed(Exception):
    """Raised when the realtime WebSocket closes, to trigger a reconnect."""


async def _recv_json(ws: aiohttp.ClientWebSocketResponse) -> dict:
    msg = await ws.receive()
    if msg.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
        return json.loads(msg.data)
    raise WSClosed(f"websocket closed ({msg.type.name})")


def _print_pairing_code(code: str, expires_at: str) -> None:
    line = "=" * 48
    print(f"\n{line}")
    print("  MyPilot device pairing")
    print(f"  Enter this code in MyPilot Web -> Devices -> Add device:\n")
    print(f"        ┌{'─' * (len(code) + 6)}┐")
    print(f"        │   {code}   │")
    print(f"        └{'─' * (len(code) + 6)}┘")
    print(f"\n  Expires at: {expires_at}")
    print(f"{line}\n")


async def pair(
    client: StackClient, identity: Identity, cfg: AgentConfig, backend: DeviceBackend
) -> dict:
    """Run the pairing handshake until the device is activated; return the device config.

    The code is shown on the device screen (no SSH needed) via the backend, in addition to the
    console log.
    """
    while True:
        start = await client.register_start(identity)
        pairing_id = start["pairing_id"]
        poll = max(1, int(start.get("poll_interval", 3)))
        _print_pairing_code(start["code"], start["expires_at"])
        # Machine-readable line for scripts/log scraping.
        print(f"[agent] pairing_code={start['code']}", flush=True)
        try:
            backend.show_pairing_code(start["code"], start.get("expires_at"))
        except Exception as exc:  # noqa: BLE001 - display is best-effort
            print(f"[agent] could not show pairing code on screen: {exc}", flush=True)

        while True:
            await asyncio.sleep(poll)
            result = await client.register_complete(pairing_id, identity)
            status = result.get("status")
            if status == "active":
                identity.device_id = result["device_id"]
                identity_mod.save(cfg.data_dir, identity)
                try:
                    backend.clear_pairing_code()
                except Exception:  # noqa: BLE001
                    pass
                print(f"[agent] paired! device id: {identity.device_id}")
                return result.get("config") or {}
            if status == "expired":
                print("[agent] pairing code expired; requesting a new one...")
                break  # restart with a fresh code
            # status == "pending": keep waiting for the user to enter the code


async def backfill_artifacts(
    cfg: AgentConfig, identity: Identity, backend: DeviceBackend, client: IngestClient
) -> None:
    """Upload a sample set of recorded routes + logs once (guarded by a marker file)."""
    marker = os.path.join(cfg.data_dir, "artifacts_uploaded")
    if os.path.exists(marker):
        return
    routes, logs = backend.generate_artifacts()
    for route in routes:
        await client.upload_route(route)
    for log in logs:
        await client.upload_log(log)
    with open(marker, "w") as fh:
        fh.write("done\n")
    print(f"[agent] backfilled {len(routes)} routes and {len(logs)} logs", flush=True)


async def run_session(
    cfg: AgentConfig, identity: Identity, backend: DeviceBackend, config: dict
) -> None:
    """Connect the realtime WebSocket, authenticate, then stream status and handle commands."""
    heartbeat_interval = max(2, int(config.get("heartbeat_interval", 10)))

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(cfg.ws_url, heartbeat=20) as ws:
            # Handshake: receive challenge, respond with a signature over the nonce.
            challenge = await _recv_json(ws)
            if challenge.get("type") != FrameType.AUTH_CHALLENGE.value:
                raise RuntimeError("unexpected handshake frame")
            signature = sign(identity.private_key_b64, ws_auth_message(challenge["nonce"]))
            await ws.send_str(
                json.dumps(
                    {
                        "type": FrameType.AUTH.value,
                        "device_id": identity.device_id,
                        "signature": signature,
                    }
                )
            )
            ack = await _recv_json(ws)
            if ack.get("type") == FrameType.AUTH_FAIL.value:
                raise NeedsRepair(ack.get("reason", "unauthorized"))
            if ack.get("type") != FrameType.AUTH_OK.value:
                raise RuntimeError(f"unexpected auth response: {ack}")
            print(f"[agent] online — streaming status every {heartbeat_interval}s. Ctrl-C to stop.")

            # Report capabilities + current settings so the web Settings panel populates.
            await ws.send_str(
                json.dumps({"type": FrameType.SETTINGS_SYNC.value, **backend.settings_sync_payload()})
            )

            async def sender() -> None:
                while True:
                    await ws.send_str(
                        json.dumps({"type": FrameType.STATUS.value, "payload": backend.status()})
                    )
                    await asyncio.sleep(heartbeat_interval)

            async def receiver() -> None:
                while True:
                    frame = await _recv_json(ws)
                    ftype = frame.get("type")
                    if ftype == FrameType.COMMAND.value:
                        name = frame.get("name")
                        print(f"[agent] command received: {name}")
                        ok, detail = await backend.execute(name, frame.get("args") or {})
                        await ws.send_str(
                            json.dumps(
                                {
                                    "type": FrameType.COMMAND_RESULT.value,
                                    "id": frame.get("id"),
                                    "ok": ok,
                                    "detail": detail,
                                }
                            )
                        )
                        # Commands that change reported state push a fresh snapshot so the Stack
                        # reconciles immediately rather than waiting for the next heartbeat.
                        if ok and name in ("switch_model", "software_update"):
                            await ws.send_str(
                                json.dumps({"type": FrameType.STATUS.value, "payload": backend.status()})
                            )
                        if ok and name == "restore_settings":
                            await ws.send_str(
                                json.dumps(
                                    {"type": FrameType.SETTINGS_SYNC.value, **backend.settings_sync_payload()}
                                )
                            )
                    elif ftype == FrameType.SET_SETTING.value:
                        key = frame.get("key")
                        ok, detail = backend.apply_setting(key, frame.get("value"))
                        await ws.send_str(
                            json.dumps(
                                {
                                    "type": FrameType.SETTING_RESULT.value,
                                    "change_id": frame.get("change_id"),
                                    "key": key,
                                    "ok": ok,
                                    "value": frame.get("value"),
                                    "detail": detail,
                                }
                            )
                        )

            await asyncio.gather(sender(), receiver(), backend.run_state_cycler())


async def run(cfg: AgentConfig) -> None:
    if cfg.reset:
        identity_mod.reset(cfg.data_dir)
    identity = identity_mod.load_or_create(cfg.data_dir, cfg.hardware_id)
    backend = make_backend(cfg, identity)
    client = StackClient(cfg)
    http: IngestClient | None = None
    print(f"[agent] stack: {cfg.api_base}  data-dir: {cfg.data_dir}")

    try:
        config: dict = {}
        if not identity.is_paired:
            config = await pair(client, identity, cfg, backend)

        # Persistent signed HTTP client for artifact upload + model artifact download.
        http = IngestClient(cfg, identity)
        backend.attach_http(http)

        while True:
            try:
                # One-time backfill of recorded routes/logs (real bytes) over signed HTTP.
                # Marker-guarded, so it succeeds exactly once; retried each connect until the
                # Stack is reachable (handles the agent racing API startup).
                try:
                    await backfill_artifacts(cfg, identity, backend, http)
                except Exception as exc:  # noqa: BLE001 - artifacts are best-effort, never fatal
                    print(f"[agent] artifact backfill deferred: {exc}", flush=True)
                await run_session(cfg, identity, backend, config)
            except NeedsRepair:
                print("[agent] device no longer recognized by the Stack; re-pairing...")
                identity.device_id = None
                identity_mod.save(cfg.data_dir, identity)
                config = await pair(client, identity, cfg, backend)
            except (WSClosed, aiohttp.ClientError, OSError) as exc:
                print(f"[agent] connection lost ({exc}); reconnecting in 5s...")
                await asyncio.sleep(5)
    finally:
        await client.close()
        if http is not None:
            await http.close()
