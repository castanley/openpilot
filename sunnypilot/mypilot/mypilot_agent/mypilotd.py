"""On-device entrypoint: the process the openpilot manager launches for MyPilot.

Registered in `system/manager/process_config.py` as
``PythonProcess("mypilotd", "openpilot.sunnypilot.mypilot.mypilotd", always_run)``.

Reads `/data/mypilot/config.json` (created on first run with sensible defaults) for the Stack URL
and an enable flag, uses the comma DongleId as the MyPilot hardware id, and runs the real-device
agent. It is non-critical: a crash only stops MyPilot management (the manager restarts it); driving
is unaffected.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

DATA_DIR = "/data/mypilot"
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
FORK_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fork.json")
DEFAULT_STACK_URL = "https://mypilot.me"  # ultimate fallback; forks edit fork.json


def _ensure_vendored_on_path() -> None:
    """When vendored in the fork, the protocol + agent live next to this file."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)


def _fork_default_stack_url() -> str:
    """Build-time default from the fork knob (fork.json). One file for forks to change."""
    try:
        with open(FORK_JSON) as fh:
            return (json.load(fh) or {}).get("stack_url") or DEFAULT_STACK_URL
    except Exception:  # noqa: BLE001
        return DEFAULT_STACK_URL


def _load_config() -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    default_url = _fork_default_stack_url()
    cfg = {"enabled": True, "stack_url": default_url}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as fh:
                cfg.update(json.load(fh) or {})
        except Exception:  # noqa: BLE001 - tolerate a malformed file; fall back to defaults
            pass
    else:
        try:
            with open(CONFIG_PATH, "w") as fh:
                json.dump(cfg, fh, indent=2)
        except Exception:  # noqa: BLE001
            pass
    # Precedence: env override > /data config > fork.json default.
    cfg["stack_url"] = os.environ.get("MYPILOT_STACK_URL", cfg.get("stack_url", default_url))
    return cfg


def _dongle_id() -> str | None:
    try:
        from openpilot.common.params import Params  # type: ignore

        return Params().get("DongleId", encoding="utf8") or None
    except Exception:  # noqa: BLE001
        return None


def main() -> None:
    _ensure_vendored_on_path()

    cfg_file = _load_config()
    if not cfg_file.get("enabled", True):
        print("[mypilotd] disabled via /data/mypilot/config.json; exiting.")
        return

    from mypilot_agent.config import AgentConfig
    from mypilot_agent.runner import run

    cfg = AgentConfig(
        stack_url=cfg_file["stack_url"],
        data_dir=DATA_DIR,
        alias="comma device",
        onroad=False,
        cycle=False,
        hardware_id=_dongle_id(),
        reset=False,
        real=True,
    )
    print(f"[mypilotd] starting MyPilot agent -> {cfg.api_base}")
    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
