"""LoRa/GPS lifecycle and keyboard behavior, with independent SDR use."""
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

import test_radio_sdrpp as sdr_tests
from radio_backend import RadioController, RadioError


class MeshTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runner = sdr_tests.Runner()
        self.runner.units = {"meshtasticd": ("loaded", "inactive"),
                             "gpsd.socket": ("loaded", "active"), "gpsd.service": ("loaded", "active")}
        self.controller = RadioController(runner=self.runner, state_dir=folder.name)
        self.controller._sleep = lambda _seconds: None
        self.addCleanup(self.controller.close)
        def connect_web(address, timeout):
            # Match the device: HTTPS listens on 443, with nothing on 9443.
            if address != ("localhost", 443):
                raise ConnectionRefusedError("No web listener on this port")
            return MagicMock()

        web = patch("radio_backend.socket.create_connection", side_effect=connect_web)
        self.web = web.start()
        self.addCleanup(web.stop)
        exists = Path.exists
        gps_path = patch.object(Path, "exists", autospec=True,
                                side_effect=lambda path: str(path) == "/dev/ttyS0" or exists(path))
        gps_path.start()
        self.addCleanup(gps_path.stop)

    def test_start_releases_gps_and_keeps_aircraft_running_repeated_open_reuses_node(self):
        self.runner.aircraft = True
        state = self.controller.start_meshtastic()
        self.assertTrue(state["meshtastic_active"])
        self.assertTrue(state["aircraft_active"])
        self.assertEqual(state["tuner_owner"], "ADS-B")
        self.assertTrue(self.runner.rails["LORA"])
        self.assertTrue(self.runner.rails["GPS"])
        self.assertEqual(self.runner.units["gpsd.service"][1], "inactive")
        self.assertEqual(self.runner.units["gpsd.socket"][1], "inactive")
        events = self.runner.events
        self.assertLess(events.index(("sudo", "-n", "systemctl", "stop", "gpsd.service")),
                        events.index(("sudo", "-n", "systemctl", "start", "meshtasticd")))
        self.runner.events.clear()
        self.controller.start_meshtastic()
        self.assertFalse(any(event[0] == "sudo" for event in self.runner.events))

    def test_mesh_stop_and_power_off_leave_fm_audio_alive(self):
        self.controller.start_audio("91.1", "wbfm", "WKNO")
        children = list(self.runner.processes)
        self.controller.start_meshtastic()
        self.assertTrue(all(child.poll() is None for child in children))
        state = self.controller.stop_meshtastic()
        self.assertFalse(state["meshtastic_active"])
        self.assertTrue(state["audio_active"])
        self.assertTrue(self.runner.rails["LORA"])
        self.controller.start_meshtastic()
        state = self.controller.stop_meshtastic(power_off=True)
        self.assertFalse(self.runner.rails["LORA"])
        self.assertTrue(self.runner.rails["GPS"])
        self.assertTrue(state["audio_active"])
        self.assertTrue(self.runner.powered)

    def test_active_service_with_lora_off_restarts_after_power_up(self):
        self.runner.units["meshtasticd"] = ("loaded", "active")
        self.runner.rails["LORA"] = False
        self.assertTrue(self.controller.start_meshtastic()["meshtastic_active"])
        events = self.runner.events
        stop = events.index(("sudo", "-n", "systemctl", "stop", "meshtasticd"))
        power = events.index(("sudo", "-n", "aiov2_ctl", "LORA", "on"))
        start = events.index(("sudo", "-n", "systemctl", "start", "meshtasticd"))
        self.assertLess(stop, power)
        self.assertLess(power, start)

    def test_mesh_stays_running_across_sdr_actions_and_dashboard_close(self):
        self.controller.start_meshtastic()
        with patch("radio_backend.shutil.which", return_value="/usr/bin/sdrpp"), \
                patch("radio_backend.os.geteuid", return_value=1000), \
                patch.dict("os.environ", {"DISPLAY": ":0"}):
            self.controller.start_sdrpp()
            self.assertTrue(self.controller.status()["meshtastic_active"])
            self.controller.start_aircraft()
            self.controller.start_audio("91.1", "wbfm", "WKNO")
            self.controller.stop()
            self.controller.power_off()
            state = self.controller.close()
        self.assertTrue(state["meshtastic_active"])
        self.assertTrue(self.runner.rails["LORA"])
        self.assertFalse(self.runner.powered)

    def test_missing_service_is_actionable_without_touching_any_power_or_service(self):
        self.runner.units.pop("meshtasticd")
        self.runner.aircraft = True
        with self.assertRaisesRegex(RadioError, "not available"):
            self.controller.start_meshtastic()
        self.assertFalse(any(event[0] == "sudo" for event in self.runner.events))
        self.assertTrue(self.runner.aircraft)
        self.assertEqual(self.controller.status()["meshtastic_state"], "not-found")

    def test_failed_mesh_start_restores_gps_and_keeps_aircraft_running(self):
        self.runner.fail_start.add("meshtasticd")
        self.runner.aircraft = True
        with self.assertRaisesRegex(RadioError, "did not stay active"):
            self.controller.start_meshtastic()
        self.assertEqual(self.runner.units["gpsd.service"][1], "active")
        self.assertEqual(self.runner.units["gpsd.socket"][1], "active")
        self.assertTrue(self.runner.aircraft)

    def test_missing_web_client_keeps_active_mesh_and_does_not_restore_conflicting_gpsd(self):
        self.web.side_effect = OSError("No web listener")
        with self.assertRaisesRegex(RadioError, "port 443 is unavailable"):
            self.controller.start_meshtastic()
        self.assertTrue(self.controller.status()["meshtastic_active"])
        self.assertEqual(self.runner.units["gpsd.service"][1], "inactive")
        self.runner.events.clear()
        with self.assertRaises(RadioError):
            self.controller.start_meshtastic()
        self.assertFalse(any(event[0] == "sudo" for event in self.runner.events))

    def test_external_mesh_and_busy_gps_are_never_killed(self):
        self.runner.external.add("meshtasticd")
        with self.assertRaisesRegex(RadioError, "still running"):
            self.controller.start_meshtastic()
        self.assertFalse(self.runner.rails["LORA"])
        self.runner.external.clear()
        self.runner.gps_busy = True
        with self.assertRaisesRegex(RadioError, "Close the GPS viewer"):
            self.controller.start_meshtastic()
        self.assertFalse(self.runner.rails["LORA"])
        self.assertEqual(self.runner.units["gpsd.service"][1], "inactive")

    def test_refused_stop_or_external_mesh_prevents_power_cut(self):
        self.controller.start_meshtastic()
        self.runner.refuse_stop.add("meshtasticd")
        with self.assertRaises(RadioError):
            self.controller.stop_meshtastic(power_off=True)
        self.assertTrue(self.runner.rails["LORA"])
        self.runner.refuse_stop.clear()
        self.runner.external.add("meshtasticd")
        with self.assertRaises(RadioError):
            self.controller.stop_meshtastic(power_off=True)
        self.assertTrue(self.runner.rails["LORA"])


class MeshControlsTests(unittest.TestCase):
    def setUp(self):
        sdr_tests.ControlsTests.setUp(self)

    key = sdr_tests.ControlsTests.key

    def test_mesh_tab_routes_enter_stop_off_and_guide_independently(self):
        self.key("6")
        self.assertEqual(self.app.mode, "Meshtastic")
        self.app.off_button.configure.assert_any_call(text="LoRa off [O]")
        self.key("Return")
        self.app._submit.assert_called_with("meshtastic", self.app.controller.start_meshtastic)
        self.key("Escape")
        self.app._submit.assert_called_with("stop-mesh", self.app.controller.stop_meshtastic)
        self.key("o")
        self.app._submit.call_args.args[1]()
        self.app.controller.stop_meshtastic.assert_called_with(power_off=True)
        self.app.guide_dir = Path(__file__).resolve().parents[1] / "cqc"
        self.app._open = Mock()
        self.key("g")
        self.app._open.assert_called_with((self.app.guide_dir / "meshtastic-gps.html").as_uri())
        self.key("Left")
        self.assertEqual(self.app.mode, "SDR++")
        self.app.off_button.configure.assert_any_call(text="SDR off [O]")
        self.key("Escape")
        self.app._submit.assert_called_with("stop", self.app.controller.stop)

    def test_only_successful_start_opens_local_client(self):
        self.app.mode = "Meshtastic"
        self.app.events = queue.Queue()
        self.app.log_lines = []
        self.app.status_label = Mock()
        self.app._show_status = Mock()
        self.app._open = Mock()
        self.app.events.put(("done", "meshtastic", None, "Service missing"))
        self.app._drain()
        self.app._open.assert_not_called()
        self.app.events.put(("done", "meshtastic", {"meshtastic_active": True}, None))
        self.app._drain()
        self.app._open.assert_called_once_with("https://localhost/")


if __name__ == "__main__":
    unittest.main()
