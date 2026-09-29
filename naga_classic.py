#!/usr/bin/env python3
"""Experimental Linux DPI/polling control for Razer Naga Classic 1532:0093.

Requires Python 3 and Linux hidraw; no third-party Python packages.
Close OpenRGB and other Razer configuration tools while running this script.
Input Remapper can remain running.

Read settings (the default):
    sudo python3 naga_classic.py --read
Change settings, ONLY after both settings have been read successfully:
    sudo python3 naga_classic.py --dpi 1600 --polling-rate 1000

This is an experimental workaround, not an official Razer/OpenRazer driver.
Reading DPI/polling was confirmed on one physical 1532:0093 on 2026-09-29
(UTC). Settings changes have passed simulated tests but remain unverified
on physical hardware.
It never installs drivers, detaches a driver, resets the mouse, edits button
bindings, or sends firmware-update commands. Device responses must pass
identity, command, status, size, checksum and value checks before a setter is
allowed. A successful read does not guarantee the setter is supported.
Readback confirms device-reported settings, not measured physical performance.
Persistence across unplugging or rebooting is not guaranteed.

Protocol references (checked 2026-09-29):
https://github.com/openrazer/openrazer/blob/master/driver/razerchromacommon.c
  razer_chroma_misc_{get,set}_polling_rate; razer_chroma_misc_{get,set}_dpi_xy
https://github.com/openrazer/openrazer/blob/master/driver/razermouse_driver.c
  Naga Trinity DPI and polling command selection (transaction 0xff).
https://gitlab.com/CalcProgrammer1/OpenRGB/-/blob/master/Controllers/RazerController/RazerDevices.cpp
  naga_classic_device: 0093, transaction 0x1f.
https://gitlab.com/CalcProgrammer1/OpenRGB/-/blob/master/Controllers/RazerController/RazerControllerDetect.cpp
  Naga Classic USB interface 0.
https://docs.kernel.org/hid/hidraw.html
"""

import argparse
import fcntl
import os
from pathlib import Path
import platform
import shlex
import struct
import sys
import time

VID, PID = 0x1532, 0x0093
REPORT_LEN = 90
FEATURE_LEN = REPORT_LEN + 1
POLL_CODES = {125: 8, 500: 2, 1000: 1}
POLL_RATES = {code: hz for hz, code in POLL_CODES.items()}
STATUS = {0: "no completed response", 1: "busy", 3: "command failed",
          4: "device timeout", 5: "command unsupported"}


class MouseError(Exception):
    pass


def ioctl_code(direction, number, length):
    # Linux asm-generic ioctl layout, used on x86/ARM (checked in main).
    return (direction << 30) | (length << 16) | (ord("H") << 8) | number


def crc(report):
    result = 0
    for value in report[2:88]:
        result ^= value
    return result


def packet(transaction, command_class, command_id, payload):
    if (command_class, command_id, len(payload)) not in {
        (0, 0x85, 1), (4, 0x85, 7), (0, 5, 1), (4, 5, 7)
    }:
        raise MouseError("Command outside the DPI/polling allowlist.")
    report = bytearray(REPORT_LEN)
    report[1] = transaction
    report[5:8] = bytes((len(payload), command_class, command_id))
    report[8:8 + len(payload)] = payload
    report[88] = crc(report)
    return report


def checked_response(raw, request):
    # Linux usbhid returns the zero report-ID prefix plus the 90-byte report.
    if len(raw) != FEATURE_LEN or raw[0] != 0:
        raise MouseError(f"Unexpected feature report length/prefix ({len(raw)} bytes).")
    response = raw[1:]
    if response[1] != request[1] or response[6:8] != request[6:8]:
        raise MouseError("Response belongs to a different command; close OpenRGB and retry.")
    if response[2:5] != b"\x00\x00\x00" or response[89] != 0:
        raise MouseError("Unexpected response framing.")
    if response[5] != request[5] or response[88] != crc(response):
        raise MouseError("Response size or checksum did not validate.")
    if response[0] != 2:
        raise MouseError("Mouse response: " + STATUS.get(response[0], f"unknown status {response[0]}"))
    return response[8:8 + response[5]]


def locate_mouse():
    candidates = []
    for entry in sorted(Path("/sys/class/hidraw").glob("hidraw*")):
        try:
            device = (entry / "device").resolve(strict=True)
            props = dict(line.split("=", 1) for line in (device / "uevent").read_text().splitlines()
                         if "=" in line)
            ids = tuple(int(part, 16) for part in props.get("HID_ID", "").split(":"))
            if ids != (3, VID, PID):
                continue
            # Locate the parent USB interface, not another HID collection.
            interface = next((p for p in device.parents if (p / "bInterfaceNumber").is_file()), None)
            if interface is not None and int((interface / "bInterfaceNumber").read_text(), 16) == 0:
                candidates.append(Path("/dev") / entry.name)
        except (OSError, ValueError):
            continue
    if len(candidates) != 1:
        raise MouseError(f"Expected one 1532:0093 mouse on USB interface 0; found {len(candidates)}. "
                         "Check that it is plugged in. If multiple identical mice are connected, unplug the extras.")
    return candidates[0]


class HidMouse:
    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
        try:
            info = bytearray(8)
            fcntl.ioctl(self.fd, ioctl_code(2, 3, 8), info, True)
            if struct.unpack("=IHH", info) != (3, VID, PID):
                raise MouseError("The opened device is not USB 1532:0093; refusing access.")
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.fd)
            raise

    def close(self):
        os.close(self.fd)

    def exchange(self, transaction, command_class, command_id, payload):
        request = packet(transaction, command_class, command_id, payload)
        transfer = bytearray(b"\x00") + request
        sent = fcntl.ioctl(self.fd, ioctl_code(3, 6, FEATURE_LEN), transfer, True)
        if sent != FEATURE_LEN:
            raise MouseError(f"Short feature-report send: {sent} bytes.")
        time.sleep(0.030)
        # Get replies only; never automatically repeat a settings-change command.
        for attempt in range(5):
            response = bytearray(FEATURE_LEN)
            count = fcntl.ioctl(self.fd, ioctl_code(3, 7, FEATURE_LEN), response, True)
            if count != FEATURE_LEN:
                raise MouseError(f"Short feature-report response: {count} bytes.")
            if response[1] == 1 and response[2] == transaction and response[7:9] == request[6:8]:
                time.sleep(0.050)
                continue
            return checked_response(response, request)
        raise MouseError("Mouse remained busy; close other configuration tools and retry.")


def read_settings(exchange, transaction):
    dpi = exchange(transaction, 4, 0x85, bytes(7))
    x, y = int.from_bytes(dpi[1:3], "big"), int.from_bytes(dpi[3:5], "big")
    if not (100 <= x <= 16000 and 100 <= y <= 16000):
        raise MouseError(f"DPI reply is outside this model's expected range: {x}:{y}.")
    poll = exchange(transaction, 0, 0x85, bytes(1))
    if poll[0] not in POLL_RATES:
        raise MouseError(f"Unknown polling-rate reply 0x{poll[0]:02x}.")
    return (x, y), POLL_RATES[poll[0]]


def preflight(exchange):
    errors = []
    # Both identifiers come from existing drivers, not an arbitrary scan.
    # Only queries are sent while determining compatibility.
    for transaction in (0x1F, 0xFF):
        try:
            settings = read_settings(exchange, transaction)
            return transaction, settings
        except (MouseError, OSError) as error:
            errors.append(f"0x{transaction:02x}: {error}")
    raise MouseError("Read-only compatibility check failed. No settings-change commands were sent.\n"
                     + "\n".join(errors))


def configure(exchange, transaction, before, dpi=None, polling=None):
    expected = (dpi if dpi is not None else before[0],
                polling if polling is not None else before[1])
    # One change at a time, with readback before attempting a second change.
    if dpi is not None and dpi != before[0]:
        payload = b"\x01" + struct.pack(">HH", *dpi) + b"\x00\x00"
        exchange(transaction, 4, 5, payload)
        current = read_settings(exchange, transaction)
        if current != (dpi, before[1]):
            raise MouseError(f"DPI change did not read back as expected: {current}.")
    if polling is not None and polling != before[1]:
        exchange(transaction, 0, 5, bytes((POLL_CODES[polling],)))
    after = read_settings(exchange, transaction)
    if after != expected:
        raise MouseError(f"Settings did not read back as requested: {after}.")
    return after


def dpi_argument(value):
    try:
        parts = value.split(":")
        if len(parts) == 1:
            parts *= 2
        result = tuple(map(int, parts))
        if len(result) != 2 or any(n < 100 or n > 16000 for n in result):
            raise ValueError
        return result
    except ValueError:
        raise argparse.ArgumentTypeError("DPI must be 100..16000, either one number or X:Y.")


def show_settings(label, settings):
    (x, y), hz = settings
    print(f"{label}: DPI X={x}, Y={y}; reported polling rate={hz} Hz", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--read", action="store_true", help="read settings only (also the default)")
    parser.add_argument("--dpi", type=dpi_argument, help="set both axes, e.g. 1600; optional X:Y format")
    parser.add_argument("--polling-rate", type=int, choices=sorted(POLL_CODES), help="set report rate in Hz")
    args = parser.parse_args()
    changing = args.dpi is not None or args.polling_rate is not None
    if args.read and changing:
        parser.error("--read cannot be combined with a settings change")
    if sys.platform != "linux" or platform.machine().lower() not in {
        "x86_64", "amd64", "i386", "i686", "aarch64", "armv7l"
    }:
        parser.error("This script requires Linux on x86 or ARM.")
    mouse = None
    try:
        path = locate_mouse()
        print(f"Experimental Naga Classic control: {path} (1532:0093)", flush=True)
        mouse = HidMouse(path)
        transaction, before = preflight(mouse.exchange)
        print(f"Read-only compatibility check passed using transaction 0x{transaction:02x}.", flush=True)
        show_settings("Before" if changing else "Current", before)
        if not changing:
            print("No settings changed.")
            return 0
        (x, y), hz = before
        restore = (f"sudo python3 {shlex.quote(str(Path(__file__).resolve()))} "
                   f"--dpi {x}:{y} --polling-rate {hz}")
        print("To restore these reported values later:\n" + restore, flush=True)
        try:
            after = configure(mouse.exchange, transaction, before, args.dpi, args.polling_rate)
        except (MouseError, OSError) as error:
            raise MouseError(f"A settings change was attempted, but could not be verified: {error}\n"
                             "Some settings may already have changed. Keep the restore command above.") from error
        show_settings("Verified readback", after)
        print("Done. Persistence across unplugging/rebooting has not been tested.")
        return 0
    except PermissionError:
        print("Permission denied. Run with sudo to access this mouse's hidraw interface.", file=sys.stderr)
        return 1
    except (MouseError, OSError) as error:
        print(f"Stopped: {error}", file=sys.stderr)
        return 1
    finally:
        if mouse is not None:
            mouse.close()


if __name__ == "__main__":
    sys.exit(main())
