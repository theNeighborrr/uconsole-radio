# Validation status

Source imported September 27, 2026 from the working uConsole Radio installation. All five current launcher files matched the device checksum inventory. Compared with the saved field-kit source, the installed launcher adds a Camera button; that addition is preserved.

The prepared public import passed all 43 existing hardware-mocked tests in an isolated temporary directory on the uConsole. This test run did not install the import or operate real radio hardware. Python syntax, JSON data, local documentation links, and checks for private credentials/device data also passed.

Prior device checks include FM-to-SDR++ opening, SDR++ band presets, PulseAudio playback, and SDR++/Aircraft handoff. Meshtastic's configured HTTPS port was confirmed, but the new dashboard's two-way mesh messaging still needs device verification. Tests cover receiver ownership, subprocess failures, SDR++ profiles, LoRa/GPS lifecycle, and keyboard routing through mocked hardware.

The source and configuration assume the tested CM4/AIO V2/Debian setup. This is not evidence of compatibility with other extension boards, service names, GPS wiring, SDR receivers, or OS releases. The optional classic launcher lacks the newer features. A fresh install on another person's device remains unverified.

The Camera button launches a separate application. Camera video, Ring sign-in, and detection models belong in the camera-hub repository, not this one. External SDR++, readsb, tar1090, and Meshtastic software remain separately installed projects.
