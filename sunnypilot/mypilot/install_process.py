#!/usr/bin/env python3
"""Idempotently apply the MyPilot overlay onto a fresh upstream openpilot/SunnyPilot tree.

Run from the fork repo root after overlaying ``sunnypilot/mypilot/`` (used when creating/refreshing
the mypilot-* branches and by the publish pipeline). It:
  1. registers the MyPilot agent as a non-critical ``PythonProcess`` (mypilotd) whose launcher
     imports nothing at module load — so the manager's boot-time pre-import can never hang — and
  2. registers the on-screen pairing alert key so the device can show the pairing code with no SSH.

We use a plain ``PythonProcess`` (like sunnypilot's mapd_manager/models_manager), NOT a
``DaemonProcess``: DaemonProcess needs a PID param declared in the compiled ``params_keys.h``, which
can't be added to a *prebuilt* branch. Both steps are idempotent and touch nothing else (no
driving/safety code).
"""

from __future__ import annotations

import json

PROC_PATH = "system/manager/process_config.py"
MODULE = "sunnypilot.mypilot.mypilotd"
LINE = f'  PythonProcess("mypilotd", "{MODULE}", always_run),\n'
COMMENT = (
    "  # MyPilot — self-hosted control plane agent. Non-critical sidecar; import-safe launcher,\n"
    "  # never in the driving path.\n"
)
ANCHOR = '  PythonProcess("mapd_manager", "sunnypilot.mapd.mapd_manager", always_run),\n'

ALERTS_PATH = "selfdrive/selfdrived/alerts_offroad.json"
ALERT_KEY = "Offroad_MyPilotPairing"
ALERT_ENTRY = {
    "text": "MyPilot pairing code: %1\nEnter it in MyPilot Web \u2192 Devices \u2192 Add device.",
    "severity": 1,
}


def _register_process() -> None:
    with open(PROC_PATH) as fh:
        src = fh.read()
    if MODULE in src:
        print("[mypilot] mypilotd already registered")
        return
    if ANCHOR in src:
        src = src.replace(ANCHOR, ANCHOR + "\n" + COMMENT + LINE, 1)
    else:
        src = src.rstrip() + "\n\nprocs += [\n" + COMMENT + LINE + "]\n"
    with open(PROC_PATH, "w") as fh:
        fh.write(src)
    print("[mypilot] registered mypilotd in", PROC_PATH)


def _register_pairing_alert() -> None:
    try:
        with open(ALERTS_PATH) as fh:
            alerts = json.load(fh)
    except FileNotFoundError:
        print("[mypilot] alerts file not found; skipping pairing alert")
        return
    if ALERT_KEY in alerts:
        print("[mypilot] pairing alert already registered")
        return
    alerts[ALERT_KEY] = ALERT_ENTRY
    with open(ALERTS_PATH, "w") as fh:
        json.dump(alerts, fh, indent=2)
        fh.write("\n")
    print("[mypilot] registered", ALERT_KEY, "in", ALERTS_PATH)


def main() -> int:
    _register_process()
    _register_pairing_alert()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
