# Setup and operation

The current target is Debian 13 ARM64 on a Raspberry Pi CM4 uConsole with HackerGadgets AIO V2. The backend assumes `aiov2_ctl`, systemd, an RTL-SDR, the configured `readsb` service and tar1090 viewer, and the board's existing device paths. Treat another board or OS as a port to validate, not a drop-in supported configuration.

Optional integrations are installed separately: SDR++ Brown, meshtasticd and its web client, and the Camera Hub. The launcher does not download or configure those applications. The Camera button opens `http://127.0.0.1:8765/`; the aircraft map opens `http://localhost/tar1090/`; Meshtastic opens `https://localhost/` for the tested deployment.

## Receiver ownership

The RTL-SDR is one receiver. The launcher coordinates its own audio, managed SDR++ window, and readsb. Stop recordings before switching or powering the SDR down. An SDR++ instance opened outside this dashboard must be closed manually before a conflicting activity. The launcher refuses a handoff when an existing process cannot be safely stopped.

Meshtastic uses the separate LoRa radio, so it can remain active alongside SDR work. Its controls preserve the saved mesh configuration; this repo contains no region, channel, encryption-key, or phone-pairing configuration. The tested GPS handoff is specific to the existing direct-device setup.

## Everyday controls

Choose an activity using the keyboard or tabs, then Enter to start. FM has Listen and Open in SDR++ actions; the S key opens the selected FM station in SDR++. The SDR++ tab offers usual settings and saved band profiles. Press Play in SDR++ after it opens. Guide / G opens the current local guide.

Stop / Esc ends the selected managed activity. SDR off also cuts its power rail. Close / Q closes the dashboard and its audio while leaving aircraft tracking and the separately managed SDR++ window in their current state. After reopening the dashboard, close a previously left-open SDR++ instance manually before a conflicting handoff.

Profiles and station examples are configuration, not frequency or mode advice for every location. Review `cqc/launcher/stations.json`, `sdrpp_presets.py`, and the tower/weather entries in `radio_launcher.py` before use elsewhere.

## Updates and recovery

Close Radio before running its installer again. The installer creates a dated copy under `~/.local/share/uconsole-radio-backups/` before replacing the installed app in `~/.local/share/uconsole-radio/`. Keep those backups private if later local edits contain personal configuration. Update guides and source together; merely replacing a guide does not change the installed app.

Public repository preparation does not pull changes onto a running uConsole or change radio state. Verify a release locally before updating a working field setup.
