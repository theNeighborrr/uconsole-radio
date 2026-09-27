"""Receiver handoff regressions; no hardware, sudo, network, or display needed.

Run from the repository root: python3 -m unittest discover -s tests -v
"""
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from tkinter import ttk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cqc" / "launcher"))
from radio_backend import RadioController, RadioError
from radio_launcher import ACTIVITIES, RadioApp, load_stations
from sdrpp_presets import PresetError


class Process:
    def __init__(self, runner, args, kwargs):
        self.runner, self.args, self.kwargs = runner, args, kwargs
        self.name = Path(args[0]).name
        self.stdout = io.BytesIO() if kwargs.get("stdout") == subprocess.PIPE else None
        self.returncode = None
        self.hung = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.runner.events.append(("terminate", self.name))
        if not self.hung:
            self.returncode = -15

    def kill(self):
        raise AssertionError("Unexpected force-kill")

    def wait(self, timeout):
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.returncode


class Runner:
    def __init__(self):
        self.aircraft = False
        self.powered = True
        self.external = set()
        self.processes = []
        self.events = []
        self.launch_error = None
        self.startup_exit = None
        self.pgrep_error = False
        self.usb_busy = False
        self.gps_busy = False
        self.units = {}
        self.rails = {"LORA": False, "GPS": False}
        self.fail_start = set()
        self.refuse_stop = set()

    def run(self, command, **kwargs):
        self.events.append(tuple(command))
        args = command[2:] if command[:2] == ["sudo", "-n"] else command
        rc, out = 0, ""
        if args == ["systemctl", "is-active", "readsb"]:
            rc, out = (0, "active") if self.aircraft else (3, "inactive")
        elif args[:2] == ["systemctl", "show"]:
            load, active = self.units.get(args[2], ("not-found", "inactive"))
            out = f"LoadState={load}\nActiveState={active}\n"
        elif args[:2] in (["systemctl", "start"], ["systemctl", "stop"]) and args[2:] == ["readsb"]:
            if args[1] == "start":
                assert not any(p.poll() is None and p.name in {"sdrpp", "sdrpp-brown", "rtl_fm"}
                               for p in self.processes), "readsb started over a receiver"
                assert not self.external, "readsb restored over an external receiver"
            self.aircraft = args[1] == "start"
        elif args[:2] in (["systemctl", "start"], ["systemctl", "stop"]):
            for unit in args[2:]:
                load, active = self.units[unit]
                if args[1] == "start":
                    if unit == "meshtasticd":
                        assert self.rails["LORA"] and self.rails["GPS"], "Mesh started before power-up"
                        assert self.units.get("gpsd.service", (None, None))[1] != "active", "Two GPS readers"
                    active = "failed" if unit in self.fail_start else "active"
                elif unit not in self.refuse_stop:
                    active = "inactive"
                self.units[unit] = (load, active)
        elif args[:2] in (["aiov2_ctl", "LORA"], ["aiov2_ctl", "GPS"]):
            if args[1:] == ["LORA", "off"]:
                assert self.units.get("meshtasticd", (None, None))[1] != "active", "LoRa cut while mesh running"
                assert "meshtasticd" not in self.external, "LoRa cut under an external node"
            self.rails[args[1]] = args[2] == "on"
        elif args[:2] == ["aiov2_ctl", "SDR"]:
            if args[2] == "off":
                assert not any(p.poll() is None for p in self.processes), "Power cut before receiver exit"
            self.powered = args[2] == "on"
        elif args == ["aiov2_ctl", "--status"]:
            out = "SDR GPIO7: " + ("ON" if self.powered else "OFF")
            out += "\nLORA GPIO16: " + ("ON" if self.rails["LORA"] else "OFF")
            out += "\nGPS GPIO27: " + ("ON" if self.rails["GPS"] else "OFF")
        elif args == ["lsusb"]:
            out = "Bus 001 Device 004: ID 0bda:2838 Realtek RTL2838"
        elif args[0] == "pgrep":
            names = self.external | {p.name for p in self.processes if p.poll() is None}
            names |= {name for unit, name in (("meshtasticd", "meshtasticd"), ("gpsd.service", "gpsd"))
                      if self.units.get(unit, (None, None))[1] == "active"}
            rc = 2 if self.pgrep_error else (0 if any(re.fullmatch(args[-1], name) for name in names) else 1)
        elif args[0] == "fuser":
            rc = 0 if (self.gps_busy if args[1] == "/dev/ttyS0" else self.usb_busy) else 1
        else:
            raise AssertionError(f"Unexpected command: {command}")
        return subprocess.CompletedProcess(command, rc, out, "")

    def Popen(self, args, **kwargs):
        self.events.append(("launch", *args))
        if self.launch_error:
            raise self.launch_error
        if Path(args[0]).name in {"sdrpp", "sdrpp-brown", "rtl_fm"}:
            assert not self.aircraft, "Receiver started before readsb stopped"
            assert not any(p.poll() is None and p.name in {"sdrpp", "sdrpp-brown", "rtl_fm"}
                           for p in self.processes), "Two receivers started"
        child = Process(self, args, kwargs)
        child.returncode = self.startup_exit
        self.processes.append(child)
        return child


class HandoffTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runner = Runner()
        self.controller = RadioController(runner=self.runner, state_dir=directory.name)
        self.controller._sleep = lambda _seconds: None
        self.addCleanup(self.controller.close)
        for context in (patch("radio_backend.shutil.which", return_value="/usr/bin/sdrpp"),
                        patch("radio_backend.os.geteuid", return_value=1000),
                        patch.dict(os.environ, {"DISPLAY": ":0"})):
            context.start()
            self.addCleanup(context.stop)

    def test_aircraft_to_sdrpp_normal_user_and_repeated_open(self):
        self.runner.aircraft = True
        state = self.controller.start_sdrpp()
        self.assertEqual(state["mode"], "sdrpp")
        self.assertEqual(state["tuner_owner"], "SDR++")
        self.assertFalse(self.runner.aircraft)
        child = self.runner.processes[-1]
        self.assertEqual(child.args, ["/usr/bin/sdrpp"])
        self.assertTrue(child.kwargs["start_new_session"])
        self.assertEqual(child.kwargs["stdin"], subprocess.DEVNULL)
        self.controller.start_sdrpp()
        self.assertEqual(len(self.runner.processes), 1)

    def test_audio_to_sdrpp_to_aircraft_and_back_to_audio(self):
        self.controller.start_audio("91.1", "wbfm", "FM")
        audio = list(self.runner.processes)
        self.controller.start_sdrpp()
        self.assertTrue(all(p.poll() is not None for p in audio))
        sdrpp = self.runner.processes[-1]
        state = self.controller.start_aircraft()
        self.assertIsNotNone(sdrpp.poll())
        self.assertTrue(state["aircraft_active"])
        self.controller.start_sdrpp()
        sdrpp = self.runner.processes[-1]
        state = self.controller.start_audio("162.475", "fm", "Weather")
        self.assertIsNotNone(sdrpp.poll())
        self.assertTrue(state["audio_active"])
        self.assertFalse(state["sdrpp_active"])

    def test_stop_and_close_have_distinct_behavior(self):
        self.controller.start_sdrpp()
        child = self.runner.processes[-1]
        self.assertTrue(self.controller.close()["sdrpp_active"])
        self.assertIsNone(child.poll())
        self.assertFalse(self.controller.stop()["sdrpp_active"])
        self.assertTrue(self.runner.powered)
        self.controller.start_aircraft()
        self.assertTrue(self.controller.stop()["aircraft_active"])

    def test_power_off_closes_sdrpp_before_cutting_power(self):
        self.controller.start_sdrpp()
        self.assertFalse(self.controller.power_off()["powered"])
        self.assertIsNotNone(self.runner.processes[-1].poll())

    def test_missing_binary_or_invalid_desktop_does_not_interrupt(self):
        self.runner.aircraft = True
        contexts = (patch("radio_backend.shutil.which", return_value=None),
                    patch("radio_backend.os.geteuid", return_value=0),
                    patch.dict(os.environ, {"DISPLAY": "", "WAYLAND_DISPLAY": ""}))
        for context in contexts:
            with self.subTest(context=context), context, self.assertRaises(RadioError):
                self.controller.start_sdrpp()
            self.assertTrue(self.runner.aircraft)
            self.assertEqual(self.runner.events, [])

    def test_brown_binary_fallback(self):
        with patch("radio_backend.shutil.which", side_effect=[None, "/usr/bin/sdrpp-brown"]):
            self.controller.start_sdrpp()
        self.assertEqual(self.runner.processes[-1].args, ["/usr/bin/sdrpp-brown"])

    def test_external_sdrpp_is_never_closed_or_duplicated(self):
        self.runner.external.add("sdrpp-brown")
        for action in (self.controller.start_sdrpp, self.controller.start_aircraft,
                       self.controller.power_off):
            with self.subTest(action=action), self.assertRaisesRegex(RadioError, "outside"):
                action()
        self.assertEqual(self.runner.processes, [])
        self.assertTrue(self.runner.powered)
        self.assertEqual(self.controller.status()["tuner_owner"], "SDR++")

    def test_launch_failure_restores_previously_active_aircraft(self):
        self.runner.aircraft = True
        self.runner.launch_error = OSError("cannot launch")
        with self.assertRaisesRegex(RadioError, "cannot launch"):
            self.controller.start_sdrpp()
        self.assertTrue(self.runner.aircraft)
        self.assertFalse(self.controller.status()["sdrpp_active"])

    def test_early_exit_restores_aircraft_and_reports_log(self):
        self.runner.aircraft = True
        self.runner.startup_exit = 1
        with self.assertRaisesRegex(RadioError, "radio.log"):
            self.controller.start_sdrpp()
        self.assertTrue(self.runner.aircraft)

    def test_failed_launch_does_not_restore_over_new_external_receiver(self):
        self.runner.aircraft = True
        def fail_with_new_owner(*args, **kwargs):
            self.runner.external.add("rtl_fm")
            raise OSError("launch failed")
        with patch.object(self.runner, "Popen", side_effect=fail_with_new_owner):
            with self.assertRaises(RadioError):
                self.controller.start_sdrpp()
        self.assertFalse(self.runner.aircraft)

    def test_hung_sdrpp_blocks_switch_and_power_off_without_force_kill(self):
        self.controller.start_sdrpp()
        child = self.runner.processes[-1]
        child.hung = True
        for action in (self.controller.start_aircraft, self.controller.power_off,
                       lambda: self.controller.start_audio("91.1", "wbfm", "FM")):
            with self.subTest(action=action), self.assertRaisesRegex(RadioError, "has not closed"):
                action()
            self.assertIsNone(child.poll())
            self.assertFalse(self.runner.aircraft)
            self.assertTrue(self.runner.powered)
            self.assertEqual(len(self.runner.processes), 1)

    def test_manually_closed_sdrpp_releases_status_and_can_reopen(self):
        self.controller.start_sdrpp()
        self.runner.processes[-1].returncode = 0
        state = self.controller.status()
        self.assertFalse(state["sdrpp_active"])
        self.assertEqual(state["tuner_owner"], "FREE")
        self.assertEqual(state["last_error"], "")
        self.assertTrue(self.controller.start_sdrpp()["sdrpp_active"])

    def test_unknown_process_state_and_busy_usb_fail_closed(self):
        self.runner.pgrep_error = True
        with self.assertRaises(RadioError):
            self.controller.start_sdrpp()
        self.assertEqual(self.runner.processes, [])
        self.runner.pgrep_error = False
        self.runner.usb_busy = True
        with self.assertRaisesRegex(RadioError, "using the SDR"):
            self.controller.power_off()
        self.assertTrue(self.runner.powered)

    def test_preset_launch_and_switch_close_previous_before_preparation(self):
        with patch("radio_backend.prepare_profile", return_value=Path("/profiles/fm")) as prepare:
            state = self.controller.start_sdrpp("fm")
            child = self.runner.processes[-1]
            self.assertEqual(child.args, ["/usr/bin/sdrpp", "--root", "/profiles/fm"])
            self.assertEqual(state["sdrpp_preset"], "fm")
            self.controller.start_sdrpp("fm")
            prepare.assert_called_once_with("fm", frequency_mhz=None)
            self.assertEqual(len(self.runner.processes), 1)
        def prepare_next(preset_id, *, frequency_mhz=None):
            self.assertIsNotNone(child.poll(), "Profile prepared while previous SDR++ was live")
            return Path("/profiles") / preset_id
        with patch("radio_backend.prepare_profile", side_effect=prepare_next):
            self.controller.start_sdrpp("weather")
        self.assertEqual(self.runner.processes[-1].args[-1], "/profiles/weather")
        self.controller.start_sdrpp()  # Return to the user's normal config root.
        self.assertEqual(self.runner.processes[-1].args, ["/usr/bin/sdrpp"])

    def test_fm_station_handoff_restarts_for_new_frequency_and_keeps_one_receiver(self):
        self.controller.start_audio("91.1", "wbfm", "WKNO")
        audio = list(self.runner.processes)
        def prepare_fm(preset_id, *, frequency_mhz=None):
            self.assertFalse(any(p.name == "sdrpp" and p.poll() is None for p in self.runner.processes),
                             "Profile prepared while SDR++ was live")
            return Path("/profiles") / preset_id
        with patch("radio_backend.prepare_profile", side_effect=prepare_fm) as prepare:
            state = self.controller.start_sdrpp("fm", frequency_mhz="103.5")
            first = self.runner.processes[-1]
            self.assertTrue(all(p.poll() is not None for p in audio))
            self.assertEqual(first.args, ["/usr/bin/sdrpp", "--root", "/profiles/fm"])
            self.assertEqual(state["frequency"], "103500000")
            self.assertIn("103.5", state["label"])
            self.controller.start_sdrpp("fm", frequency_mhz=103.5)
            prepare.assert_called_once_with("fm", frequency_mhz="103.5")
            self.assertIs(self.runner.processes[-1], first)
            state = self.controller.start_sdrpp("fm", frequency_mhz="91.1")
            self.assertIsNotNone(first.poll())
            second = self.runner.processes[-1]
            self.assertIsNot(first, second)
            self.assertEqual(second.args, first.args)
            self.assertEqual(state["frequency"], "91100000")
            self.controller.start_sdrpp("fm")  # The default FM preset already starts here.
            self.assertEqual(prepare.call_count, 2)
            self.assertIs(self.runner.processes[-1], second)
        self.assertTrue(self.controller.start_aircraft()["aircraft_active"])
        self.assertIsNotNone(second.poll())

    def test_invalid_station_does_not_close_active_sdrpp(self):
        self.controller.start_sdrpp()
        child = self.runner.processes[-1]
        self.runner.events.clear()
        with patch("radio_backend.prepare_profile") as prepare:
            with self.assertRaises(RadioError):
                self.controller.start_sdrpp("fm", frequency_mhz="NaN")
            prepare.assert_not_called()
        self.assertIsNone(child.poll())
        self.assertEqual(self.runner.events, [])

    def test_invalid_profile_preserves_current_aircraft_service(self):
        self.runner.aircraft = True
        with patch("radio_backend.prepare_profile", side_effect=PresetError("bad settings")):
            with self.assertRaisesRegex(RadioError, "bad settings"):
                self.controller.start_sdrpp("weather")
        self.assertTrue(self.runner.aircraft)
        self.assertEqual(self.runner.processes, [])
        self.assertFalse(any(event[0] == "sudo" for event in self.runner.events))

    def test_hung_or_external_sdrpp_blocks_profile_writes(self):
        self.runner.external.add("sdrpp")
        with patch("radio_backend.prepare_profile") as prepare:
            with self.assertRaises(RadioError):
                self.controller.start_sdrpp("fm", frequency_mhz="103.5")
            prepare.assert_not_called()
        self.runner.external.clear()
        self.controller.start_sdrpp()
        self.runner.processes[-1].hung = True
        with patch("radio_backend.prepare_profile") as prepare:
            with self.assertRaises(RadioError):
                self.controller.start_sdrpp("fm", frequency_mhz="103.5")
            prepare.assert_not_called()


class ControlsTests(unittest.TestCase):
    def setUp(self):
        self.app = RadioApp.__new__(RadioApp)
        self.app.root = Mock()
        self.app.busy = self.app.closing = False
        self.app.mode = "Aircraft"
        self.app.station_rows = []
        self.app.selected_by_mode = {}
        self.app.last_status = {}
        self.app.controller = Mock()
        self.app._submit = Mock()
        self.app.fm_stations = load_stations(Path(__file__).resolve().parents[1] / "cqc/launcher/stations.json")
        self.app.fm_error = ""
        self.app.query = Mock()
        self.app.query.get.return_value = ""
        for name in ("start_button", "fm_sdrpp_button", "stop_button", "off_button", "close_button",
                     "guide_button", "filter_entry", "station_list", "filter_frame", "note"):
            setattr(self.app, name, Mock())
        self.app.mode_buttons = {mode: Mock() for mode in ACTIVITIES}
        self.app.station_list.curselection.return_value = (0,)
        self.app.station_list.selection_set.side_effect = lambda index: setattr(
            self.app.station_list.curselection, "return_value", (index,))

    def key(self, name, widget=None):
        widget = widget if widget is not None else Mock()
        widget.winfo_toplevel.return_value = self.app.root
        return self.app._on_key(SimpleNamespace(widget=widget, keysym=name))

    def test_sdrpp_list_opens_usual_settings_then_selected_preset(self):
        self.assertEqual(self.key("5"), "break")
        self.assertEqual(self.app.mode, "SDR++")
        self.app.start_button.configure.assert_any_call(state="normal")
        self.key("Return")
        self.assertEqual(self.app._submit.call_args.args[0], "sdrpp")
        self.app._submit.call_args.args[1]()
        self.app.controller.start_sdrpp.assert_called_with(None)
        self.key("Down")
        self.key("Return")
        self.app._submit.call_args.args[1]()
        self.app.controller.start_sdrpp.assert_called_with("fm")
        self.assertIn("91.1", self.app.note.configure.call_args.kwargs["text"])
        self.key("Escape")
        self.app._submit.assert_called_with("stop", self.app.controller.stop)
        self.key("Right")
        self.assertEqual(self.app.mode, "Meshtastic")
        self.key("Right")
        self.assertEqual(self.app.mode, "Weather")

    def test_busy_mode_cannot_start_or_select_sdrpp(self):
        self.app.busy = True
        self.key("5")
        self.key("Return")
        self.assertEqual(self.app.mode, "Aircraft")
        self.app._submit.assert_not_called()

    def test_fm_search_selects_station_for_sdrpp_and_keeps_enter_as_listen(self):
        self.key("2")
        self.app.fm_sdrpp_button.grid.assert_called_once_with()
        self.app.query.get.return_value = "WRBO"
        self.app._filter_changed()
        self.assertEqual(len(self.app.station_rows), 1)
        self.key("s")
        self.app._submit.call_args.args[1]()
        self.app.controller.start_sdrpp.assert_called_once_with("fm", frequency_mhz="103.5")
        self.key("Return")
        self.app._submit.call_args.args[1]()
        self.app.controller.start_audio.assert_called_once_with(
            frequency="103.5M", mode="wbfm", label=self.app.station_rows[0]["label"])
        self.key("5")
        self.app.fm_sdrpp_button.grid_remove.assert_called_once_with()

    def test_fm_action_ignores_empty_selection_other_tabs_and_busy_or_closing_app(self):
        self.key("s")
        self.app._submit.assert_not_called()
        self.key("2")
        self.app.busy = True
        self.key("s")
        self.app.busy, self.app.closing = False, True
        self.key("s")
        self.app.closing = False
        self.app.query.get.return_value = "no such station xyz"
        self.app._filter_changed()
        self.app.fm_sdrpp_button.configure.assert_called_with(state="disabled")
        self.key("s")
        self.app._submit.assert_not_called()

    def test_s_in_search_is_text_and_enter_returns_to_station_list(self):
        self.key("2")
        self.app.filter_entry = Mock(spec=ttk.Entry)
        self.assertIsNone(self.key("s", self.app.filter_entry))
        self.assertEqual(self.key("Return", self.app.filter_entry), "break")
        self.app.station_list.focus_set.assert_called()
        self.app._submit.assert_not_called()

    def test_sdrpp_guide_is_local_and_contextual(self):
        self.app.guide_dir = Path(__file__).resolve().parents[1] / "cqc"
        self.app._open = Mock()
        self.app.mode = "SDR++"
        self.app.open_guide()
        self.app._open.assert_called_once_with((self.app.guide_dir / "sdrpp.html").as_uri())
        self.app.mode = "FM"
        self.app.open_guide()
        self.app._open.assert_called_with((self.app.guide_dir / "memphis-fm.html").as_uri())


if __name__ == "__main__":
    unittest.main()
