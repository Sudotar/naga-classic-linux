# Naga Classic Linux

An experimental Python utility for reading and changing DPI and polling rate on the **Razer Naga Classic Edition, USB `1532:0093`**, entirely on Linux.

It uses Linux hidraw and Python's standard library. It is an independent project, not an official Razer or OpenRazer driver.

## Hardware testing status

| Operation | Status |
| --- | --- |
| Read DPI | Confirmed on one physical `1532:0093`: X=1400, Y=1400 |
| Read polling rate | Confirmed on that mouse: reported 1000 Hz |
| Transaction identifier | `0x1f` worked for both reads |
| Change DPI | Implemented and tested with simulated USB responses; physical testing pending |
| Change polling rate | Implemented and tested with simulated USB responses; physical testing pending |
| Retain settings after unplugging or rebooting | Not tested |

The hardware read was reported on 2026-09-29 (UTC). A successful query does not establish that settings changes work. Device readback reports configured values; it does not measure actual sensor DPI or USB report frequency.

## Requirements

- Linux on x86 or ARM. The script permits `x86_64`, `amd64`, `i386`, `i686`, `aarch64`, and `armv7l`; not every architecture has been tested.
- Python 3; no third-party Python packages.
- One connected Razer Naga Classic Edition with USB ID `1532:0093`.
- Permission to access its hidraw interface; the examples use `sudo`.

Close OpenRGB and other mouse configuration utilities while running this script. Input Remapper can remain running.

## Use

Open a terminal in the folder containing `naga_classic.py`.

Read current settings first:

```sh
sudo python3 naga_classic.py --read
```

Running without arguments also reads settings only. The confirmed hardware result was:

```text
Read-only compatibility check passed using transaction 0x1f.
Current: DPI X=1400, Y=1400; reported polling rate=1000 Hz
No settings changed.
```

### Experimental settings changes

The following commands request settings changes. These paths have not yet been verified on a physical mouse.

Set both DPI axes to 1600:

```sh
sudo python3 naga_classic.py --dpi 1600
```

Request a 500 Hz polling rate:

```sh
sudo python3 naga_classic.py --polling-rate 500
```

Request both settings together:

```sh
sudo python3 naga_classic.py --dpi 1600 --polling-rate 1000
```

The CLI accepts DPI values from **100 to 16000** and polling rates of **125, 500, or 1000 Hz**. Separate X/Y values use `--dpi 1600:1200`.

Before changing anything, the script queries both settings and prints a command to restore those reported values. Keep that command. It validates readback after a change and stops on an error; it does not automatically roll back partial changes. Values that already match the request are not written again.

Show all options:

```sh
python3 naga_classic.py --help
```

## Scope and behavior

- Selects USB interface 0 and checks the opened device's vendor/product ID.
- Restricts commands to DPI and polling queries/setters.
- Checks response framing, command, status, length, checksum, and returned values.
- Tries the known transaction IDs `0x1f`, then `0xff`, using queries only. Both settings must be readable before a setter is allowed.
- Does not install or detach drivers, reset the mouse, modify button bindings, or send firmware-update commands.
- Does not configure RGB, remap side buttons, or install a startup service.

If the compatibility check fails, save the output for diagnosis. It means that no settings-change commands were sent by that attempt. If a settings-change attempt fails later, some values may already have changed; use the printed restore command and read the settings again.

## Tests

Run from the project folder on Linux, without `sudo`:

```sh
python3 -m unittest discover -s tests -v
```

The 13 tests use simulated USB responses and temporary sysfs fixtures. They cover request bytes, read-only operation, device selection, response validation, settings readback, and partial failures. They do not access a physical mouse or establish hardware compatibility.

For a hardware test report, include the USB ID, Linux distribution/kernel, Python version, exact command and output, and whether settings survive unplugging or rebooting. Note whether a requested value differed from the original value: requesting the same value does not test its setter.

## Protocol references

The implementation was written with reference to these projects and documentation:

- [OpenRazer DPI and polling command definitions](https://github.com/openrazer/openrazer/blob/master/driver/razerchromacommon.c): `razer_chroma_misc_get_polling_rate`, `razer_chroma_misc_set_polling_rate`, `razer_chroma_misc_get_dpi_xy`, and `razer_chroma_misc_set_dpi_xy`.
- [OpenRazer mouse driver](https://github.com/openrazer/openrazer/blob/master/driver/razermouse_driver.c): Naga Trinity command selection and transaction `0xff`.
- [OpenRGB Razer device definitions](https://gitlab.com/CalcProgrammer1/OpenRGB/-/blob/master/Controllers/RazerController/RazerDevices.cpp): Naga Classic device `0093` and transaction `0x1f`.
- [OpenRGB Razer device detection](https://gitlab.com/CalcProgrammer1/OpenRGB/-/blob/master/Controllers/RazerController/RazerControllerDetect.cpp): Naga Classic interface 0.
- [Linux hidraw documentation](https://docs.kernel.org/hid/hidraw.html).

References were checked on 2026-09-29. Their presence does not imply endorsement of this utility.
