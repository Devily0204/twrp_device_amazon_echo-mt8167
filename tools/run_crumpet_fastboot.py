#!/usr/bin/env python3
"""Start a self-contained Crumpet TWRP bundle from stock locked LK Fastboot.

--check performs only read-only Fastboot gates. --execute downloads to RAM and
enters the handoff selected by the manifest through lk_fastboot.py. There is
no crumpet.py import, no baseline payload, no BROM, no flash/unlock and no
persistent operation.

Examples (with adb on PATH):
    py tools/run_crumpet_fastboot.py --bundle out/crumpet-twrp-b.bundle.bin --check
    py tools/run_crumpet_fastboot.py --bundle out/crumpet-twrp-b.bundle.bin --execute

Use --adb C:/path/to/adb.exe if the executable is not on PATH. The device must
already be in the documented Fastboot mode on the test unit.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

import lk_fastboot as lk


def adb_environment():
    env = dict(__import__("os").environ)
    env.setdefault("ADB_MDNS", "0")
    return env


def adb_read(adb, serial, *arguments):
    completed = subprocess.run([adb, "-s", serial, *arguments], capture_output=True,
                               text=True, timeout=12, env=adb_environment(), check=False)
    return {"returncode": completed.returncode,
            "stdout": completed.stdout.strip(), "stderr": completed.stderr.strip()}


def adb_devices(adb):
    completed = subprocess.run([adb, "devices", "-l"], capture_output=True,
                               text=True, timeout=12, env=adb_environment(), check=False)
    if completed.returncode != 0 or not completed.stdout.startswith("List of devices attached"):
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
    rows = []
    for line in completed.stdout.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 2:
            continue
        row = {"serial": fields[0], "state": fields[1]}
        for field in fields[2:]:
            if ":" in field:
                key, value = field.split(":", 1)
                row[key] = value
        rows.append(row)
    return rows


def load_bundle(path):
    path = path.resolve()
    metadata = json.loads(path.with_suffix(".json").read_text(encoding="utf-8-sig"))
    payload = path.read_bytes()
    if metadata["persistent_operations"]:
        raise ValueError("Refusing bundle with persistent_operations")
    if len(payload) != metadata["bundle_size"]:
        raise ValueError("Bundle length differs from manifest")
    if hashlib.sha256(payload).hexdigest() != metadata["bundle_sha256"]:
        raise ValueError("Bundle SHA-256 differs from manifest")
    if "entry_thumb" not in metadata:
        raise ValueError("Bundle manifest has no handoff entry")
    if int(metadata["entry_thumb"], 0) not in (lk.LOADER_ENTRY_THUMB, 0x45971001):
        raise ValueError(f"Unknown handoff entry: {metadata['entry_thumb']}")
    return payload, metadata


def wait_for_twrp(adb, expected_serial, expected_version, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    observations = []
    while time.monotonic() < deadline:
        try:
            rows = adb_devices(adb)
            observations.append(rows)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            observations.append([{"error": str(exc)}])
            rows = []
        for row in rows:
            serial = row.get("serial", "")
            if row.get("state") != "recovery" or row.get("device") != "crumpet":
                continue
            if row.get("product", "omni_crumpet") not in ("omni_crumpet", "crumpet"):
                continue
            if expected_serial and serial != expected_serial:
                continue
            props = adb_read(adb, serial, "shell", "getprop", "ro.twrp.version")
            observations.append([{"props": props}])
            version = props.get("stdout", "")
            if props.get("returncode") == 0 and version == expected_version:
                return {"success": True, "serial": serial, "state": row["state"],
                        "product": row.get("product"), "twrp_version": version,
                        "device_row": row, "observation_count": len(observations)}
            return {"success": False, "error": "ADB identity matched but ro.twrp.version did not",
                    "serial": serial, "expected_version": expected_version, "props": props}
        time.sleep(1)
    return {"success": False, "error": "No authorized Crumpet TWRP ADB transport before timeout",
            "timeout_seconds": timeout_seconds, "expected_serial": expected_serial,
            "expected_version": expected_version, "observation_count": len(observations),
            "last_observation": observations[-1] if observations else []}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--adb", help="adb executable; defaults to PATH")
    parser.add_argument("--libusb-dir", type=Path, help="Directory containing the host libusb-1.0.dll")
    parser.add_argument("--serial", default="0123456789ABCDEF")
    parser.add_argument("--twrp-version", default="3.7.0_9-0")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--log", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    result = {"success": False, "mode": "check" if args.check else "execute",
              "bundle": str(args.bundle.resolve()), "persistent_operations": []}
    try:
        if args.libusb_dir:
            if os.name == "nt":
                os.add_dll_directory(str(args.libusb_dir.resolve()))
            os.environ["PATH"] = str(args.libusb_dir.resolve()) + os.pathsep + os.environ.get("PATH", "")
        payload, metadata = load_bundle(args.bundle)
        result["bundle_sha256"] = metadata["bundle_sha256"]
        if args.check:
            device = lk.open_fastboot()
            try:
                result["fastboot"] = lk.gates(device, len(payload))
            finally:
                lk.close_fastboot(device)
            result["success"] = True
        else:
            handoff = lk.execute_bundle(payload, int(metadata["entry_thumb"], 0))
            result.update(handoff)
            if not handoff.get("success"):
                raise RuntimeError(f"Handoff failed during {handoff.get('phase')}: {handoff.get('detail')}")
            adb = args.adb or shutil.which("adb")
            if not adb:
                raise RuntimeError("adb executable not found; pass --adb")
            result["adb"] = wait_for_twrp(adb, args.serial, args.twrp_version, args.timeout)
            result["success"] = bool(result["adb"].get("success"))
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except Exception as exc:  # Host-side protocol/validation failure -> JSON result.
        result.update(success=False, error=type(exc).__name__, detail=str(exc))
        print(json.dumps(result, indent=2, ensure_ascii=False), file=sys.stderr)
    finally:
        if args.log:
            args.log.parent.mkdir(parents=True, exist_ok=True)
            args.log.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
