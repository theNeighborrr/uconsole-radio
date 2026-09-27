"""uConsole AIO V2 radio control; Python 3.11 standard library only.

RadioController actions are serialized by the caller (one GUI worker).
No command uses a shell. This module never acquires sudo authorization: launch
the GUI with its supplied wrapper, which maintains a limited sudo credential.
close() stops this launcher's audio children only, preserving aircraft tracking
and the separate SDR++ window. Stop / mode switches close owned SDR++ instances.
Meshtastic uses the independent LoRa radio and has separate stop/power controls.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from sdrpp_presets import PresetError, get_preset, prepare_profile

# The installed Trixie Webserver.Port is 443; 4403 is its separate node API.
MESHTASTIC_URL = "https://localhost/"


class RadioError(RuntimeError):
    """An actionable receiver, permission, or playback error."""


class RadioController:
    def __init__(self, logger=None, runner=None, state_dir=None):
        self._runner = runner or subprocess
        self._logger = logger or (lambda message: None)
        self._sleep = time.sleep
        base = Path(state_dir) if state_dir else Path(
            os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "uconsole-radio"
        self.log_path = base / "radio.log"
        self._log = None
        self._children = []
        self._sdrpp = None
        self._sdrpp_preset = None
        self._mode = "idle"
        self._label = "Ready"
        self._frequency = ""
        self._powered = None
        self._last_error = ""
        self._mesh_error = ""

    def _note(self, message):
        try:
            self._logger(message)
        except Exception:
            pass  # A presentation callback must not strand receiver processes.
        if self._log:
            self._log.write((time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n").encode("utf-8"))
            self._log.flush()

    def _open_log(self):
        if self._log is None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            # Keep the most recent log between launches; avoid unlimited growth.
            if self.log_path.exists() and self.log_path.stat().st_size > 2_000_000:
                self.log_path.replace(self.log_path.with_suffix(".previous.log"))
            self._log = self.log_path.open("ab", buffering=0)

    def _run(self, args, *, sudo=False, check=True):
        command = ["sudo", "-n", *args] if sudo else list(args)
        try:
            result = self._runner.run(command, capture_output=True, text=True,
                                      timeout=12, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RadioError(f"Could not run {args[0]}: {exc}") from exc
        if check and result.returncode:
            detail = (result.stderr or result.stdout or "command failed").strip()
            hint = " Reopen using the Radio Launcher desktop shortcut to authorize sudo." if sudo else ""
            raise RadioError(f"{' '.join(args)}: {detail}{hint}")
        return result

    def _service_active(self):
        result = self._run(["systemctl", "is-active", "readsb"], check=False)
        return result.returncode == 0 and result.stdout.strip() == "active"

    def _unit_state(self, unit):
        result = self._run(["systemctl", "show", unit, "-p", "LoadState", "-p", "ActiveState"], check=False)
        fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if ("LoadState" not in fields or "ActiveState" not in fields
                or (result.returncode and fields["LoadState"] != "not-found")):
            raise RadioError(f"Could not check {unit}. See Guide [G] before changing Meshtastic power.")
        return fields

    def _stop_mesh_service(self):
        state = self._unit_state("meshtasticd")
        if state["LoadState"] != "not-found":
            self._run(["systemctl", "stop", "meshtasticd"], sudo=True)
            state = self._unit_state("meshtasticd")
        if state["ActiveState"] not in {"inactive", "failed"} or self._process_active("meshtasticd"):
            raise RadioError("A Meshtastic process is still running. Close it before switching LoRa off. Power stays on.")

    def _wait_mesh_web(self):
        endpoint = urlsplit(MESHTASTIC_URL)
        port = endpoint.port or 443
        for _ in range(10):
            if self._unit_state("meshtasticd")["ActiveState"] == "active":
                try:
                    with socket.create_connection((endpoint.hostname, port), timeout=0.3):
                        return
                except OSError:
                    pass
            self._sleep(0.5)
        state = self._unit_state("meshtasticd")["ActiveState"]
        if state == "active":
            raise RadioError(f"Meshtastic is running, but its local web client on port {port} is unavailable. See Guide [G] for checks.")
        raise RadioError("Meshtastic did not stay active. See Guide [G] or journalctl -u meshtasticd -b for details.")

    def _assert_gps_free(self):
        if self._process_active("gpsd"):
            raise RadioError("A separate gpsd process is running. Close that GPS session before opening Meshtastic.")
        if Path("/dev/ttyS0").exists():
            gps = self._run(["fuser", "/dev/ttyS0"], sudo=True, check=False)
            if gps.returncode == 0:
                raise RadioError("A GPS program is using /dev/ttyS0. Close the GPS viewer before opening Meshtastic.")
            if gps.returncode != 1 or gps.stderr.strip():
                raise RadioError("Could not check the GPS port. See Guide [G] before opening Meshtastic.")

    def start_meshtastic(self):
        """Start the saved AIO LoRa/GPS setup without changing the SDR receiver."""
        self._open_log()
        paused_gpsd = []
        starting = False
        try:
            state = self._unit_state("meshtasticd")
            if state["LoadState"] != "loaded":
                raise RadioError("Meshtastic service is not available. See Guide [G] to check the existing Trixie installation.")
            rails = self._aio_snapshot()["rails"]
            if state["ActiveState"] == "active" and rails.get("LORA") != "OFF":
                self._wait_mesh_web()  # Reopen the client without restarting an active node.
                self._mesh_error = ""
                return self.status()
            # Stop a retrying service (or one started with its rail off) before power-up.
            self._stop_mesh_service()
            # The saved CM4 setup lets Meshtastic read /dev/ttyS0 directly.
            # Stop managed gpsd readers; never kill a manually opened GPS program.
            for unit in ("gpsd.socket", "gpsd.service"):
                state = self._unit_state(unit)
                if state["LoadState"] == "loaded":
                    if state["ActiveState"] not in {"inactive", "failed"}:
                        paused_gpsd.append(unit)
                    self._run(["systemctl", "stop", unit], sudo=True)
                    if self._unit_state(unit)["ActiveState"] not in {"inactive", "failed"}:
                        raise RadioError("gpsd has not stopped. Close the GPS viewer before opening Meshtastic.")
            self._assert_gps_free()
            self._run(["aiov2_ctl", "LORA", "on"], sudo=True)
            self._run(["aiov2_ctl", "GPS", "on"], sudo=True)
            self._sleep(2)
            starting = True
            self._run(["systemctl", "start", "meshtasticd"], sudo=True)
            self._wait_mesh_web()
            self._mesh_error = ""
            self._note("Meshtastic is active. Opening its local client; SDR activities keep running.")
        except RadioError as exc:
            self._mesh_error = str(exc)
            self._note(self._mesh_error)
            if paused_gpsd:
                try:
                    # Restore a paused GPS service only after a failed node start,
                    # never over a node still running without its web client.
                    if self._unit_state("meshtasticd")["ActiveState"] != "active":
                        if starting:
                            self._stop_mesh_service()
                        if not self._process_active("meshtasticd"):
                            self._assert_gps_free()
                            self._run(["systemctl", "start", *paused_gpsd], sudo=True)
                except RadioError as restore_error:
                    self._note(f"GPS service was not restored: {restore_error}")
            raise
        return self.status()

    def stop_meshtastic(self, *, power_off=False):
        """Stop the mesh service; optionally cut only the LoRa rail, leaving GPS on."""
        self._open_log()
        try:
            self._stop_mesh_service()
            if power_off:
                self._run(["aiov2_ctl", "LORA", "off"], sudo=True)
            self._mesh_error = ""
            self._note("Meshtastic stopped" + ("; LoRa powered off." if power_off else "; LoRa power stays on."))
        except RadioError as exc:
            self._mesh_error = str(exc)
            self._note(self._mesh_error)
            raise
        return self.status()

    def _usb_paths(self):
        result = self._run(["lsusb"])
        return [f"/dev/bus/usb/{bus}/{device}" for bus, device in re.findall(
            r"^Bus (\d{3}) Device (\d{3}): ID 0bda:2838\b", result.stdout,
            flags=re.MULTILINE | re.IGNORECASE)]

    def _process_active(self, name):
        result = self._run(["pgrep", "-x", name], check=False)
        if result.returncode not in (0, 1):
            raise RadioError("Could not check receiver processes. No new receiver will be started.")
        return result.returncode == 0

    def _aio_snapshot(self):
        snapshot = {"rails": {}, "battery": {}}
        try:
            result = self._run(["aiov2_ctl", "--status"], check=False)
        except RadioError:
            return snapshot
        if result.returncode:
            return snapshot
        for raw in result.stdout.splitlines():
            line = raw.strip()
            rail = re.match(r"^(GPS|LORA|SDR|USB)\s+GPIO\d+:\s+(ON|OFF)$", line, re.IGNORECASE)
            if rail:
                snapshot["rails"][rail.group(1).upper()] = rail.group(2).upper()
                continue
            battery = re.match(
                r"^(Source|Status|Capacity|Direction|Mode|Voltage|Current|Power)\s*:\s*(.+)$",
                line, re.IGNORECASE)
            if battery:
                snapshot["battery"][battery.group(1).title()] = battery.group(2).strip()
        return snapshot

    def _tuner_owner(self, *, audio_active=False, aircraft_active=False, sdrpp_active=False):
        owners = []
        if audio_active:
            owners.append("RTL_FM")
        if aircraft_active:
            owners.append("ADS-B")
        if sdrpp_active or self._process_active("sdrpp|sdrpp-brown"):
            owners.append("SDR++")
        if not audio_active and self._process_active("rtl_fm"):
            owners.append("RTL_FM")
        owners = list(dict.fromkeys(owners))
        if not owners:
            return "FREE"
        if len(owners) > 1:
            return "CONFLICT " + " + ".join(owners)
        return owners[0]

    def _assert_external_tuner_free(self):
        if self._process_active("sdrpp|sdrpp-brown"):
            raise RadioError("SDR++ is already running outside this launcher's control. Close its window before starting this mode.")
        if self._process_active("rtl_fm"):
            raise RadioError("Another rtl_fm process is running. Stop it before starting this mode.")

    def _power_on(self):
        self._run(["aiov2_ctl", "SDR", "on"], sudo=True)
        self._powered = True
        self._sleep(2)
        paths = self._usb_paths()
        if not paths:
            raise RadioError("The RTL-SDR did not appear on USB after power-on. Check the AIO board and try again.")
        if len(paths) > 1:
            raise RadioError("More than one matching RTL-SDR is connected. Unplug the extra receiver before using this launcher.")

    @staticmethod
    def _validate(frequency, mode):
        if mode not in {"wbfm", "fm", "am"}:
            raise RadioError("Choose FM broadcast (wbfm), NOAA weather (fm), or airband (am).")
        value = str(frequency).strip()
        if value.endswith("M"):
            value = value[:-1]
        if not re.fullmatch(r"\d{2,3}(?:\.\d{1,6})?", value):
            raise RadioError("Frequency must be a number in MHz, such as 91.1 or 162.475.")
        try:
            number = Decimal(value)
        except InvalidOperation as exc:
            raise RadioError("Invalid frequency.") from exc
        valid = (mode == "wbfm" and Decimal("87.5") <= number <= Decimal("108")) or (
            mode == "fm" and Decimal("162.400") <= number <= Decimal("162.550")) or (
            mode == "am" and number in {Decimal("119.7"), Decimal("127.75")})
        if not valid:
            raise RadioError("That frequency is outside this launcher's supported FM, weather, or Memphis airband settings.")
        return format(number.normalize(), "f") + "M"

    def _stop_children(self):
        children, self._children = self._children, []
        for child in children:
            if child.poll() is None:
                try:
                    child.terminate()
                except ProcessLookupError:
                    pass
        for child in children:
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)
            if child.stdout is not None:
                child.stdout.close()

    def _restore_aircraft(self, was_active):
        if not was_active:
            return
        try:
            # A failed switch must not restart readsb over a lingering receiver.
            if self._sdrpp is not None and self._sdrpp.poll() is None:
                raise RadioError("SDR++ is still running.")
            self._assert_external_tuner_free()
            self._run(["systemctl", "start", "readsb"], sudo=True)
            self._mode, self._label = "aircraft", "Aircraft tracking"
            self._note("Restored previously running aircraft tracking after the failed switch.")
        except RadioError as exc:
            self._note(f"Could not restore aircraft tracking: {exc}")

    def start_audio(self, frequency: str, mode: str, label: str):
        frequency = self._validate(frequency, mode)  # Reject before changing hardware.
        self._open_log()
        was_active = self._service_active()
        self._stop_sdrpp()
        self.stop_audio()
        try:
            self._assert_external_tuner_free()
            self._run(["systemctl", "stop", "readsb"], sudo=True)
            self._power_on()
            # rtl_fm's wbfm preset provides 170 kHz sampling, 32 kHz output,
            # deemphasis, and offset tuning. All playback here is mono.
            rate = "32000" if mode == "wbfm" else "24000"
            args = ["rtl_fm", "-f", frequency, "-M", mode]
            if mode != "wbfm":
                args += ["-s", rate]
            self._note(f"Starting {label}: {frequency} {mode}; mono audio.")
            receiver = self._runner.Popen(args, stdout=subprocess.PIPE,
                                          stderr=self._log, start_new_session=True)
            self._children.append(receiver)
            try:
                player = self._runner.Popen(
                    ["aplay", "-t", "raw", "-r", rate, "-f", "S16_LE", "-c", "1"],
                    stdin=receiver.stdout, stdout=subprocess.DEVNULL,
                    stderr=self._log, start_new_session=True)
                self._children.append(player)
            finally:
                receiver.stdout.close()  # The player owns its copy of the pipe.
            self._sleep(0.7)
            if any(child.poll() is not None for child in self._children):
                raise RadioError(f"The radio or audio player stopped during startup. See {self.log_path} for details.")
            self._mode, self._label, self._frequency = "audio", str(label), frequency
            self._last_error = ""
        except (OSError, RadioError) as exc:
            self._stop_children()
            self._mode, self._label, self._frequency = "idle", "Ready", ""
            self._last_error = str(exc)
            self._note(self._last_error)
            self._restore_aircraft(was_active)
            raise RadioError(str(exc)) from exc
        return self.status()

    def stop_audio(self):
        self._stop_children()
        if self._mode == "audio":
            self._mode, self._label, self._frequency = "idle", "Ready", ""
            self._note("Audio stopped; the SDR remains powered.")
        return self.status()

    def _stop_sdrpp(self):
        """Close only our SDR++ process; never force-kill a hung GUI."""
        child = self._sdrpp
        if child is None:
            return
        if child.poll() is None:
            self._note("Closing SDR++ to release the receiver.")
            try:
                child.terminate()
            except ProcessLookupError:
                pass
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            raise RadioError("SDR++ has not closed. Stop any recording and close its window, then try again. The receiver stays powered.") from exc
        self._sdrpp = None
        if self._mode == "sdrpp":
            self._mode, self._label, self._frequency = "idle", "Ready", ""
        # Brown's decoder helper can take a moment to exit with its parent.
        for _ in range(10):
            if not self._process_active("sdrpp|sdrpp-brown"):
                return
            self._sleep(0.2)
        raise RadioError("An SDR++ process is still running. Close SDR++ completely before switching or powering off.")

    def start_sdrpp(self, preset_id=None, *, frequency_mhz=None):
        try:
            preset = get_preset(preset_id, frequency_mhz=frequency_mhz)
        except PresetError as exc:
            raise RadioError(str(exc)) from exc
        owned_active = self._sdrpp is not None and self._sdrpp.poll() is None
        if (owned_active and preset_id == self._sdrpp_preset
                and str(preset.get("frequency", "")) == self._frequency):
            return self.status()  # Repeated Open presses must not launch duplicates.
        # Validate the desktop and optional application before stopping anything.
        binary = shutil.which("sdrpp") or shutil.which("sdrpp-brown")
        if not binary:
            raise RadioError("SDR++ was not found. Install SDR++ Brown, then reopen uConsole Radio. Other activities are still available.")
        if os.geteuid() == 0:
            raise RadioError("Open uConsole Radio as your normal desktop user, without sudo.")
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            raise RadioError("Open uConsole Radio from your desktop session to launch SDR++.")
        if owned_active:
            self._stop_sdrpp()
        if self._process_active("sdrpp|sdrpp-brown"):
            raise RadioError("SDR++ is already running outside this launcher's control. Use or close that window first.")
        args = [binary]
        if preset_id is not None:
            try:
                # Build the profile before stopping dashboard audio or readsb.
                args += ["--root", str(prepare_profile(preset_id, frequency_mhz=frequency_mhz))]
            except (OSError, ValueError) as exc:
                raise RadioError(f"Could not prepare SDR++ preset: {exc}") from exc
        self._open_log()
        was_active = self._service_active()
        self.stop_audio()
        try:
            self._assert_external_tuner_free()
            self._run(["systemctl", "stop", "readsb"], sudo=True)
            self._power_on()
            self._note(f"Opening SDR++: {preset['name']}. Press Play in its window.")
            self._sdrpp = self._runner.Popen(
                args, stdin=subprocess.DEVNULL, stdout=self._log,
                stderr=subprocess.STDOUT, start_new_session=True)
            self._sleep(1)
            if self._sdrpp.poll() is not None:
                self._sdrpp = None
                raise RadioError(f"SDR++ exited during startup. See {self.log_path} for details.")
            self._sdrpp_preset = preset_id
            self._mode = "sdrpp"
            self._label = "SDR++" if preset_id is None else "SDR++ · " + preset["name"]
            self._frequency = str(preset.get("frequency", ""))
            self._last_error = ""
        except (OSError, RadioError) as exc:
            self._last_error = str(exc)
            self._note(self._last_error)
            self._restore_aircraft(was_active)
            raise RadioError(str(exc)) from exc
        return self.status()

    def stop(self):
        """Stop launcher audio and owned SDR++, preserving readsb and power."""
        self._stop_sdrpp()
        return self.stop_audio()

    def start_aircraft(self):
        self._open_log()
        self._stop_sdrpp()
        self.stop_audio()
        self._assert_external_tuner_free()
        self._power_on()
        self._run(["systemctl", "start", "readsb"], sudo=True)
        for _ in range(10):
            if self._service_active():
                self._mode, self._label, self._frequency = "aircraft", "Aircraft tracking", ""
                self._last_error = ""
                self._note("readsb is active. Open tar1090 to check aircraft reception.")
                return self.status()
            self._sleep(0.5)
        self._last_error = "readsb did not become active. Check: systemctl status readsb --no-pager"
        raise RadioError(self._last_error)

    def power_off(self):
        self._open_log()
        self._stop_sdrpp()
        self.stop_audio()
        self._assert_external_tuner_free()
        self._run(["systemctl", "stop", "readsb"], sudo=True)
        for path in self._usb_paths():
            result = self._run(["fuser", path], sudo=True, check=False)
            if result.returncode == 0:
                raise RadioError("Another program is using the SDR. Close that program before powering it off.")
            if result.returncode != 1 or result.stderr.strip():
                raise RadioError("Could not check whether another program is using the SDR. Power remains on; reopen the launcher and try again.")
        self._run(["aiov2_ctl", "SDR", "off"], sudo=True)
        self._powered = False
        self._mode, self._label, self._frequency = "idle", "SDR powered off", ""
        self._last_error = ""
        self._note("SDR powered off.")
        return self.status()

    def status(self):
        sdrpp_active = self._sdrpp is not None and self._sdrpp.poll() is None
        if self._sdrpp is not None and not sdrpp_active:
            returncode = self._sdrpp.returncode
            self._sdrpp = None
            if self._mode == "sdrpp":
                self._mode, self._label, self._frequency = "idle", "Ready", ""
            if returncode:
                self._last_error = f"SDR++ exited (status {returncode}). See {self.log_path} for details."
                self._note(self._last_error)
        active = bool(self._children) and all(child.poll() is None for child in self._children)
        failure = bool(self._children) and not active
        if failure:
            # Status checks share the GUI's single worker with all actions, so
            # it is safe to reap a broken pipeline and release the receiver.
            self._stop_children()
            self._mode, self._label, self._frequency = "idle", "Playback stopped", ""
            self._last_error = (
                "Radio playback stopped unexpectedly; the remaining audio process was closed. "
                f"See {self.log_path} for details."
            )
            self._note(self._last_error)
        try:
            aircraft = self._service_active()
        except RadioError:
            aircraft = False
        try:
            mesh_unit = self._unit_state("meshtasticd")
            mesh_state = "not-found" if mesh_unit["LoadState"] == "not-found" else mesh_unit["ActiveState"]
        except RadioError:
            mesh_state = "unknown"
        aio = self._aio_snapshot()
        sdr_state = aio.get("rails", {}).get("SDR")
        if sdr_state in {"ON", "OFF"}:
            self._powered = sdr_state == "ON"
        owner = self._tuner_owner(audio_active=active, aircraft_active=aircraft, sdrpp_active=sdrpp_active)
        error = self._last_error
        mode = "audio" if active else "sdrpp" if sdrpp_active else "aircraft" if aircraft else "idle"
        return {"mode": mode, "label": self._label, "frequency": self._frequency,
                "audio_active": active, "aircraft_active": aircraft,
                "sdrpp_active": sdrpp_active,
                "sdrpp_preset": self._sdrpp_preset if sdrpp_active else None,
                "meshtastic_state": mesh_state, "meshtastic_active": mesh_state == "active",
                "meshtastic_error": self._mesh_error,
                "powered": self._powered, "aio": aio, "tuner_owner": owner,
                "log_path": str(self.log_path), "last_error": error}

    def close(self):
        self._stop_children()
        if self._mode == "audio":
            self._mode, self._label, self._frequency = "idle", "Ready", ""
        if self._log:
            self._log.close()
            self._log = None
        return self.status()
