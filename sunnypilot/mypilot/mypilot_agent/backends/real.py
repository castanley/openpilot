"""Real comma-device backend (openpilot/SunnyPilot).

Implements the :class:`DeviceBackend` contract against on-device APIs (Params, cereal, hardware,
SunnyPilot's Model Manager + updater). openpilot imports are **lazy** so the package still imports
on a dev machine; on a comma device the methods do real work over the network — no SSH.

All driving-affecting actions are **offroad-only** (defense in depth on top of the Stack's gate),
and nothing here touches controlsd / pandad / driver monitoring / torque or panda safety.
"""

from __future__ import annotations

import json
import subprocess
import time

from .base import DeviceBackend

PAIRING_ALERT = "Offroad_MyPilotPairing"


def _try_params():
    try:
        from openpilot.common.params import Params  # type: ignore

        return Params()
    except Exception:  # noqa: BLE001 - not on-device / openpilot not importable
        return None


def _try_submaster():
    try:
        from cereal import messaging  # type: ignore

        return messaging.SubMaster(["deviceState", "pandaStates", "gpsLocationExternal"])
    except Exception:  # noqa: BLE001
        return None


def _device_type() -> str | None:
    try:
        from openpilot.system.hardware import HARDWARE  # type: ignore

        return HARDWARE.get_device_type()
    except Exception:  # noqa: BLE001
        return None


class RealDevice(DeviceBackend):
    def __init__(self, hardware_id: str, hostname: str) -> None:
        self.hardware_id = hardware_id
        self.hostname = hostname
        self._params = _try_params()
        self._sm = _try_submaster()
        self._http = None
        self._started = time.time()
        self._device_type = _device_type()
        self.update_channel = self._param_str("UpdaterTargetBranch") or self._param_str("GitBranch")

    # --- wiring -------------------------------------------------------------------------------
    def attach_http(self, client) -> None:
        self._http = client

    def _param_str(self, key: str) -> str | None:
        if self._params is None:
            return None
        try:
            val = self._params.get(key, encoding="utf8")
            return val or None
        except Exception:  # noqa: BLE001
            return None

    def _read_json_param(self, key: str):
        if self._params is None:
            return None
        try:
            val = self._params.get(key)
        except Exception:  # noqa: BLE001
            return None
        if val is None:
            return None
        if isinstance(val, (bytes, str)):
            try:
                return json.loads(val)
            except Exception:  # noqa: BLE001
                return None
        return val  # fork's Params.get may already decode JSON to dict/list

    @staticmethod
    def _bundles(cache) -> list:
        if isinstance(cache, list):
            return [b for b in cache if isinstance(b, dict)]
        if isinstance(cache, dict):
            if isinstance(cache.get("bundles"), list):
                return [b for b in cache["bundles"] if isinstance(b, dict)]
            vals = [v for v in cache.values() if isinstance(v, dict)]
            if vals:
                return vals
        return []

    def _active_model_ref(self) -> str | None:
        active = self._read_json_param("ModelManager_ActiveBundle")
        if isinstance(active, dict):
            return active.get("ref") or active.get("displayName")
        return None

    def _available_models(self) -> list[dict]:
        bundles = self._bundles(self._read_json_param("ModelManager_ModelsCache"))
        out = []
        for b in bundles:
            key = b.get("ref") or b.get("displayName") or (str(b["index"]) if "index" in b else None)
            if not key:
                continue
            out.append(
                {
                    "key": key,
                    "name": b.get("displayName") or b.get("ref") or key,
                    "generation": str(b["generation"]) if b.get("generation") is not None else None,
                    "runner": b.get("runner"),
                }
            )
        return out

    # --- state --------------------------------------------------------------------------------
    @property
    def onroad(self) -> bool:
        if self._params is not None:
            try:
                return bool(self._params.get_bool("IsOnroad"))
            except Exception:  # noqa: BLE001
                pass
        if self._sm is not None:
            try:
                self._sm.update(0)
                if self._sm.updated.get("deviceState"):
                    return bool(self._sm["deviceState"].started)
            except Exception:  # noqa: BLE001
                pass
        return False

    def status(self) -> dict:
        onroad = self.onroad
        thermal = None
        storage_pct = None
        gps = None
        if self._sm is not None:
            try:
                self._sm.update(0)
                if self._sm.updated.get("deviceState"):
                    ds = self._sm["deviceState"]
                    storage_pct = round(100.0 - float(ds.freeSpacePercent), 1)
                    thermal = {0: "green", 1: "yellow", 2: "red", 3: "red"}.get(int(ds.thermalStatus))
                if self._sm.updated.get("gpsLocationExternal"):
                    gps = "has_fix" if getattr(self._sm["gpsLocationExternal"], "hasFix", False) else "searching"
            except Exception:  # noqa: BLE001
                pass
        active = self._active_model_ref()
        available = self._available_models()
        return {
            "onroad": onroad,
            "storage_pct": storage_pct,
            "thermal_status": thermal,
            "panda_status": "connected" if onroad else "available",
            "gps_status": gps,
            "software_version": self._param_str("Version"),
            "branch": self._param_str("GitBranch"),
            "platform": self._param_str("CarName") or self._param_str("CarModel"),
            "active_model": active,
            "installed_models": [active] if active else [],
            "update_channel": self.update_channel,
            "update_state": self._param_str("UpdaterState") or "idle",
            "target_version": None,
            "extra": {
                "uptime_s": int(time.time() - self._started),
                "device_type": self._device_type,
                "available_models": available,
            },
        }

    # --- pairing (on-screen, no SSH) ----------------------------------------------------------
    def show_pairing_code(self, code: str, expires_at: str | None = None) -> None:
        try:
            from openpilot.selfdrive.selfdrived.alertmanager import set_offroad_alert  # type: ignore

            set_offroad_alert(PAIRING_ALERT, True, extra_text=code)
        except Exception as exc:  # noqa: BLE001
            print(f"[mypilot] could not show pairing code on screen: {exc}", flush=True)

    def clear_pairing_code(self) -> None:
        try:
            from openpilot.selfdrive.selfdrived.alertmanager import set_offroad_alert  # type: ignore

            set_offroad_alert(PAIRING_ALERT, False)
        except Exception:  # noqa: BLE001
            pass

    # --- settings -----------------------------------------------------------------------------
    def settings_sync_payload(self) -> dict:
        caps: dict = {"protocol_version": 1}
        if self._device_type:
            caps["device_type"] = self._device_type
        brand = self._param_str("CarName")
        if brand:
            caps["brand"] = brand.split()[0].lower()
        return {"capabilities": caps, "values": {}}

    def apply_setting(self, key: str, value) -> tuple[bool, str]:
        if self.onroad:
            return False, "refused: device is onroad"
        if self._params is None:
            return False, "params unavailable"
        try:
            if isinstance(value, bool):
                self._params.put_bool(key, value)
            else:
                self._params.put(key, str(value))
            return True, "applied"
        except Exception as exc:  # noqa: BLE001
            return False, f"params write failed: {exc}"

    # --- commands -----------------------------------------------------------------------------
    async def execute(self, name: str, args: dict) -> tuple[bool, str]:
        if name == "reboot":
            if self.onroad:
                return False, "refused: device is onroad"
            try:
                from openpilot.system.hardware import HARDWARE  # type: ignore

                HARDWARE.reboot()
                return True, "rebooting"
            except Exception as exc:  # noqa: BLE001
                return False, f"reboot unavailable: {exc}"

        if name == "switch_model":
            return self._switch_model(args)

        if name == "software_update":
            return self._software_update(args)

        if name == "restore_settings":
            if self.onroad:
                return False, "refused: device is onroad"
            applied = sum(1 for k, v in (args.get("settings") or {}).items() if self.apply_setting(k, v)[0])
            return True, f"restored {applied} settings"

        return False, f"unknown command: {name}"

    def _switch_model(self, args: dict) -> tuple[bool, str]:
        # Real activation via SunnyPilot's Model Manager: set ModelManager_DownloadIndex; the
        # models_manager process downloads + SHA256-verifies the bundle, then sets the active
        # bundle. Reverting to stock removes ModelManager_ActiveBundle. Offroad-only.
        if self.onroad:
            return False, "refused: device is onroad"
        if self._params is None:
            return False, "params unavailable"
        ref = args.get("model_key")
        if not ref:
            return False, "no model_key"
        if str(ref).lower() in ("default", "stock", "default-stock"):
            try:
                self._params.remove("ModelManager_ActiveBundle")
            except Exception as exc:  # noqa: BLE001
                return False, f"failed: {exc}"
            return True, "reverting to stock model"
        bundles = self._bundles(self._read_json_param("ModelManager_ModelsCache"))
        match = next(
            (b for b in bundles if ref in (b.get("ref"), b.get("displayName"), str(b.get("index")))),
            None,
        )
        if match is None or "index" not in match:
            return False, f"model '{ref}' not found in the on-device catalog"
        try:
            self._params.put("ModelManager_DownloadIndex", int(match["index"]))
        except Exception as exc:  # noqa: BLE001
            return False, f"failed to request model: {exc}"
        return True, f"requested {match.get('displayName', ref)} — Model Manager will download + verify"

    def _software_update(self, args: dict) -> tuple[bool, str]:
        # Real update via SunnyPilot's updater: set the target branch and nudge `updated` to
        # fetch it. The device shows "Update available" and applies on the next reboot (offroad).
        if self.onroad:
            return False, "refused: device is onroad"
        if self._params is None:
            return False, "params unavailable"
        branch = args.get("branch") or self._branch_from_url(args.get("install_url"))
        if not branch:
            return False, "no target branch"
        try:
            self._params.put("UpdaterTargetBranch", branch)
        except Exception as exc:  # noqa: BLE001
            return False, f"failed to set target branch: {exc}"
        # Nudge the updater: SIGUSR1 = check, SIGHUP = download/fetch the target branch.
        for sig in ("-SIGUSR1", "-SIGHUP"):
            subprocess.run(["pkill", sig, "-f", "system.updated.updated"], check=False)
        self.update_channel = args.get("channel") or branch
        return True, f"update to {branch} started; device will fetch it and apply on next reboot"

    @staticmethod
    def _branch_from_url(url: str | None) -> str | None:
        if not url:
            return None
        return url.rstrip("/").split("/")[-1] or None

    # --- uploads ------------------------------------------------------------------------------
    def generate_artifacts(self) -> tuple[list, list]:
        # Route/log upload is OFF by default (privacy). Wire to the loggerd realdata root on opt-in.
        return [], []

    async def run_state_cycler(self) -> None:
        return None
