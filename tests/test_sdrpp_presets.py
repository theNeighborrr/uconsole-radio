"""Real temporary config files exercise profile isolation and SDR++'s schema."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "cqc" / "launcher"))
from sdrpp_presets import PresetError, SDRPP_PRESETS, prepare_profile


class ProfileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.source = self.home / ".config/sdrpp-brown"
        self.source.mkdir(parents=True)
        self.write(self.source, "config.json", {
            "frequency": 100_000_000, "source": "RTL-SDR", "windowSize": {"w": 800},
            "moduleInstances": {"Radio": {"module": "radio", "enabled": True},
                                "Extra": {"module": "radio", "enabled": True},
                                "RTL-SDR Source": {"module": "rtl_sdr_source", "enabled": True}},
            "vfoOffsets": {"Radio": 200_000}, "streams": {"Radio": {"volume": 0.3}},
            "modulesDirectory": "/usr/lib/sdrpp/plugins", "resourcesDirectory": "/usr/share/sdrpp",
        })
        self.write(self.source, "radio_config.json", {"Radio": {"selectedDemodId": 1}})
        self.write(self.source, "rtl_sdr_config.json", {
            "device": "AIO V2", "devices": {"AIO V2": {"gain": 14, "sampleRate": 1_024_000,
                                                       "directSampling": 2, "biasT": True}},
        })
        self.write(self.source, "frequency_manager_config.json", {"lists": {"My stations": {"keep": True}}})
        self.original = {p.name: p.read_bytes() for p in self.source.iterdir()}

    @staticmethod
    def write(root, name, value):
        (root / name).write_text(json.dumps(value))

    @staticmethod
    def read(root, name):
        return json.loads((root / name).read_text())

    def test_each_preset_has_correct_tuning_and_never_changes_normal_config(self):
        for preset in SDRPP_PRESETS[1:]:
            with self.subTest(preset=preset["id"]):
                root = prepare_profile(preset["id"], home=self.home)
                config = self.read(root, "config.json")
                radio = self.read(root, "radio_config.json")["Radio"]
                rtl = self.read(root, "rtl_sdr_config.json")["devices"]["AIO V2"]
                mode = "FM" if preset["mode"] == "NFM" else preset["mode"]
                self.assertEqual(config["frequency"], preset["frequency"])
                self.assertEqual(config["vfoOffsets"]["Radio"], 0)
                self.assertEqual(radio["selectedDemodId"], preset["demod_id"])
                self.assertEqual(radio[mode]["bandwidth"], preset["bandwidth"])
                self.assertFalse(radio[mode]["squelchEnabled"])
                self.assertEqual(config["streams"]["Radio"]["volume"], 0.3)
                self.assertFalse(config["moduleInstances"]["Extra"]["enabled"])
                self.assertEqual(rtl["gain"], 14)
                self.assertEqual(rtl["sampleRate"], 1_024_000)
                self.assertEqual(rtl["directSampling"], 0)
                self.assertFalse(rtl["biasT"])
                self.assertEqual((root / "frequency_manager_config.json").read_bytes(),
                                 self.original["frequency_manager_config.json"])
        self.assertEqual({p.name: p.read_bytes() for p in self.source.iterdir()}, self.original)

    def test_existing_profile_retains_audio_gain_and_bookmarks_but_reapplies_preset(self):
        root = prepare_profile("fm", home=self.home)
        config = self.read(root, "config.json")
        config["frequency"] = 99_900_000
        config["moduleInstances"]["uConsole PulseAudio"] = {"module": "linux_pulseaudio_sink", "enabled": True}
        config["streams"]["Radio"].update(sink="PulseAudio", volume=0.5, muted=False)
        self.write(root, "config.json", config)
        rtl = self.read(root, "rtl_sdr_config.json")
        rtl["devices"]["AIO V2"]["gain"] = 20
        self.write(root, "rtl_sdr_config.json", rtl)
        self.write(root, "frequency_manager_config.json", {"custom": "station"})
        for mhz, hz in (("103.5", 103_500_000), ("87.75", 87_750_000), (None, 91_100_000)):
            with self.subTest(mhz=mhz):
                self.assertEqual(prepare_profile("fm", frequency_mhz=mhz, home=self.home), root)
                config = self.read(root, "config.json")
                self.assertEqual(config["frequency"], hz)
                self.assertEqual(config["streams"]["Radio"], {"sink": "PulseAudio", "volume": 0.5, "muted": False})
                self.assertEqual(config["moduleInstances"]["uConsole PulseAudio"],
                                 {"module": "linux_pulseaudio_sink", "enabled": True})
                radio = self.read(root, "radio_config.json")["Radio"]
                self.assertEqual(radio["selectedDemodId"], 1)
                self.assertEqual(radio["WFM"]["bandwidth"], 200_000)
                self.assertEqual(self.read(root, "rtl_sdr_config.json")["devices"]["AIO V2"]["gain"], 20)
                self.assertEqual(self.read(root, "frequency_manager_config.json"), {"custom": "station"})
        self.assertEqual({p.name: p.read_bytes() for p in self.source.iterdir()}, self.original)

    def test_invalid_station_frequency_never_changes_profile(self):
        root = prepare_profile("fm", home=self.home)
        original = {p.name: p.read_bytes() for p in root.iterdir()}
        for frequency in ("", "not a station", "NaN", "Infinity", "-Infinity", "87.49", "108.01", "91.1000001"):
            with self.subTest(frequency=frequency), self.assertRaises(PresetError):
                prepare_profile("fm", frequency_mhz=frequency, home=self.home)
        for preset_id in (None, "weather", "airband"):
            with self.subTest(preset_id=preset_id), self.assertRaises(PresetError):
                prepare_profile(preset_id, frequency_mhz="103.5", home=self.home)
        self.assertEqual({p.name: p.read_bytes() for p in root.iterdir()}, original)

    def test_malformed_config_leaves_no_half_created_profile(self):
        (self.source / "config.json").write_text("{unfinished")
        with self.assertRaises(PresetError):
            prepare_profile("weather", home=self.home)
        base = self.home / ".config/uconsole-radio/sdrpp-presets"
        self.assertEqual(list(base.iterdir()), [])
        self.assertEqual((self.source / "config.json").read_text(), "{unfinished")

    def test_missing_setup_or_unknown_preset_is_actionable(self):
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaisesRegex(PresetError, "usual settings once"):
                prepare_profile("fm", home=empty)
        with self.assertRaisesRegex(PresetError, "from the list"):
            prepare_profile("../../sdrpp-brown", home=self.home)

    def test_old_module_format_and_renamed_radio_are_supported(self):
        config = self.read(self.source, "config.json")
        config["moduleInstances"] = {"My Radio": "radio", "SDR Source": "rtl_sdr_source"}
        self.write(self.source, "config.json", config)
        root = prepare_profile("weather", home=self.home)
        self.assertEqual(self.read(root, "radio_config.json")["My Radio"]["selectedDemodId"], 0)
        self.assertEqual(self.read(root, "config.json")["vfoOffsets"]["My Radio"], 0)

    def test_linked_profile_cannot_write_back_into_usual_settings(self):
        base = self.home / ".config/uconsole-radio/sdrpp-presets"
        base.mkdir(parents=True)
        (base / "fm").symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(PresetError, "overlaps"):
            prepare_profile("fm", home=self.home)
        self.assertEqual({p.name: p.read_bytes() for p in self.source.iterdir()}, self.original)


class InstallerTests(unittest.TestCase):
    def test_new_module_is_installed_and_classic_still_installs(self):
        spec = importlib.util.spec_from_file_location("install_launcher", REPO / "cqc/install-launcher.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            with patch.object(installer.os, "geteuid", return_value=1000), \
                 patch.object(installer.Path, "home", return_value=home), \
                 patch.object(installer, "dependency_packages", return_value=[]), \
                 patch.object(installer.shutil, "which", side_effect=lambda command: None if command == "xdg-user-dir" else "/usr/bin/" + command), \
                 patch.dict(installer.os.environ, {"XDG_CACHE_HOME": str(home / ".cache")}), \
                 contextlib.redirect_stdout(io.StringIO()):
                with patch.object(sys, "argv", ["install-launcher.py"]):
                    self.assertEqual(installer.main(), 0)
                installed = home / ".local/share/uconsole-radio/sdrpp_presets.py"
                self.assertEqual(installed.read_bytes(), (REPO / "cqc/launcher/sdrpp_presets.py").read_bytes())
                with patch.object(sys, "argv", ["install-launcher.py", "--classic"]):
                    self.assertEqual(installer.main(), 0)


if __name__ == "__main__":
    unittest.main()
