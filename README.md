# uConsole Radio

A desktop radio dashboard for the ClockworkPi uConsole with a Raspberry Pi CM4, Debian 13 ARM64, and HackerGadgets AIO V2 board.

## Features

- NOAA weather, FM presets, tower/ATIS, aircraft tracking, SDR++, and Meshtastic controls.
- Managed handoff of the shared RTL-SDR receiver between radio activities.
- SDR++ band profiles and opening an FM station in SDR++.
- Independent LoRa/Meshtastic operation with GPS resource checks.
- Keyboard navigation and local operating guides.
- **Camera** button opening the separately installed [uConsole Camera Hub](https://github.com/theNeighborrr/uconsole-camera-hub).

## Installation on a configured uConsole

This installs the launcher, not a complete radio stack. The hardware, `aiov2_ctl`, radio services, and optional apps must already be configured. Read [setup and operating notes](docs/SETUP.md) first. The bundled stations and tower presets are Memphis-area examples; customize them for your location.

Close uConsole Radio, then run as the normal desktop user:

```sh
python3 cqc/install-launcher.py
```

If it reports missing Debian packages, review the list and run `python3 cqc/install-launcher.py --install-deps`. The installer asks for sudo when necessary, backs up the previous launcher, and adds desktop/application shortcuts. Installation itself starts no radio program. Keep this repository at its installed path because the shortcut references its guide directory.

Open **uConsole Radio** from the uConsole desktop. Enter the uConsole password in its terminal if prompted; never put that password in Git. An older four-activity interface is retained under `cqc/launcher-classic/` and can be installed with `--classic`; it lacks the current SDR++, Meshtastic, and Camera controls.

## Tests and source baseline

```sh
python3 -m unittest discover -s tests -v
```

Tests use mocked hardware/processes and require Linux Python with Tkinter. They do not start a real radio or require sudo.

The five current launcher files were copied from the working uConsole and verified by checksum. The installed Camera button is included. Installer, classic launcher, and tests were extracted from the existing field-kit source. This is a source snapshot; the prior Git history remains in the original field-kit repository and has not been rewritten. Preparing this import did not modify or restart the installed Radio app.

See [validation and limitations](docs/STATUS.md). No device credentials, mesh keys, private configuration, binaries, or personal media are included. A license for this project's own code has not yet been selected; external tools retain their own licenses.
