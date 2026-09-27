"""SDR++ band launch profiles, kept apart from the user's normal SDR++ root.

Schema: SDRPlusPlusBrown core/src/command_args.cpp (root), core/src/core.cpp
(frequency / vfoOffsets), and decoder_modules/radio/src/{radio_module.h,
radio_module_interface.h,demodulators/nfm.h}. NFM is stored under "FM".
Only the launcher's own profile copies are written, while SDR++ is closed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
from decimal import Decimal, InvalidOperation


SDRPP_PRESETS = (
    {"id": None, "name": "Usual settings", "label": "Open SDR++ — usual settings",
     "note": "Uses your normal SDR++ settings. Stop recordings before changing presets. Guide [G] opens SDR++ help."},
    {"id": "fm", "name": "FM broadcast", "label": "FM broadcast — 88–108 MHz · WFM",
     "frequency": 91_100_000, "mode": "WFM", "demod_id": 1, "bandwidth": 200_000, "step": 100_000,
     "note": "Starts at WKNO 91.1 MHz, WFM / 200 kHz. For individual stations, choose FM → Open in SDR++ [S]. Stop recordings before switching."},
    {"id": "weather", "name": "NOAA weather", "label": "NOAA weather — 162.400–162.550 MHz · NFM",
     "frequency": 162_475_000, "mode": "NFM", "demod_id": 0, "bandwidth": 15_000, "step": 25_000,
     "note": "Starts at WXK49 162.475 MHz, NFM / 15 kHz. Seven channels; no automatic alerts. Stop recordings before switching."},
    {"id": "airband", "name": "Airband", "label": "Airband — 118–137 MHz · AM",
     "frequency": 119_700_000, "mode": "AM", "demod_id": 2, "bandwidth": 10_000, "step": 25_000,
     "note": "Starts at Memphis tower, 119.700 MHz, AM / 10 kHz. Tune within the band manually. Stop recordings before switching."},
    {"id": "tower", "name": "Memphis tower", "label": "Memphis tower — 119.700 MHz · AM",
     "frequency": 119_700_000, "mode": "AM", "demod_id": 2, "bandwidth": 10_000, "step": 25_000,
     "note": "Memphis tower, AM / 10 kHz. Voice traffic is intermittent. Stop recordings before changing presets."},
    {"id": "atis", "name": "Memphis ATIS", "label": "Memphis ATIS — 127.750 MHz · AM",
     "frequency": 127_750_000, "mode": "AM", "demod_id": 2, "bandwidth": 10_000, "step": 25_000,
     "note": "Memphis ATIS, AM / 10 kHz. Repeating airport weather and runway information. Stop recordings before switching."},
)


class PresetError(ValueError):
    """A profile cannot be prepared without guessing at the saved setup."""


def get_preset(preset_id, *, frequency_mhz=None):
    for preset in SDRPP_PRESETS:
        if preset["id"] == preset_id:
            break
    else:
        raise PresetError("Choose an SDR++ preset from the list.")
    if frequency_mhz is None:
        return preset
    if preset_id != "fm":
        raise PresetError("Station selection is only available for the FM preset.")
    try:
        frequency = Decimal(str(frequency_mhz))
    except InvalidOperation as exc:
        raise PresetError("Choose a valid FM station between 87.5 and 108 MHz.") from exc
    if not frequency.is_finite() or not Decimal("87.5") <= frequency <= Decimal("108"):
        raise PresetError("Choose a valid FM station between 87.5 and 108 MHz.")
    hertz = frequency * 1_000_000
    if hertz != hertz.to_integral_value():
        raise PresetError("The FM station frequency must resolve to whole Hz.")
    return dict(preset, frequency=int(hertz), name=f"FM {frequency.normalize():f} MHz")


def _read_json(root, name, *, required=False):
    path = root / name
    if not path.exists() and not required:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PresetError(f"Could not read {path}. Open SDR++ with usual settings, then close it and retry.") from exc
    if not isinstance(value, dict):
        raise PresetError(f"Unsupported settings in {path}; use Open SDR++ — usual settings.")
    return value


def _object(parent, key):
    value = parent.setdefault(key, {})
    if not isinstance(value, dict):
        raise PresetError(f"Unsupported SDR++ {key} settings; use Open SDR++ — usual settings.")
    return value


def _write_json(root, name, value):
    data = json.dumps(value, indent=4, ensure_ascii=False, allow_nan=False) + "\n"
    fd, temporary = tempfile.mkstemp(prefix="." + name + ".", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
        os.replace(temporary, root / name)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _apply(root, preset):
    config = _read_json(root, "config.json", required=True)
    radio = _read_json(root, "radio_config.json")
    rtl = _read_json(root, "rtl_sdr_config.json")
    modules = _object(config, "moduleInstances")
    # Older SDR++ versions stored the module name directly as a string.
    for name, entry in list(modules.items()):
        if isinstance(entry, str):
            modules[name] = {"module": entry, "enabled": True}
        elif not isinstance(entry, dict):
            raise PresetError("Unsupported SDR++ module list; use usual settings.")
    radios = [name for name, entry in modules.items() if entry.get("module") == "radio"]
    sources = [name for name, entry in modules.items() if entry.get("module") == "rtl_sdr_source"]
    if not radios or not sources:
        raise PresetError("Open SDR++ with usual settings and enable the Radio and RTL-SDR Source modules first.")
    chosen = "Radio" if "Radio" in radios else radios[0]
    for name in radios:
        modules[name]["enabled"] = name == chosen
    for name in sources:
        modules[name]["enabled"] = True
    config.update(frequency=preset["frequency"], source="RTL-SDR", selectedOffset="None",
                  manualOffset=0.0, decimation=1, zoomBw=1.0)
    _object(config, "vfoOffsets")[chosen] = 0.0
    channel = _object(radio, chosen)
    channel["selectedDemodId"] = preset["demod_id"]
    settings = _object(channel, "FM" if preset["mode"] == "NFM" else preset["mode"])
    settings.update(bandwidth=preset["bandwidth"], snapInterval=preset["step"],
                    squelchEnabled=False, deempMode="75us" if preset["mode"] == "WFM" else "None")
    if preset["mode"] == "WFM":
        settings["stereo"] = False
    for device in _object(rtl, "devices").values():
        if not isinstance(device, dict):
            raise PresetError("Unsupported RTL-SDR device settings; use usual settings.")
        device.update(directSampling=0, biasT=False)
    # Validate all settings before replacing any files in an existing profile.
    for value in (config, radio, rtl):
        json.dumps(value, allow_nan=False)
    for name, value in (("config.json", config), ("radio_config.json", radio),
                        ("rtl_sdr_config.json", rtl)):
        _write_json(root, name, value)


def prepare_profile(preset_id, *, frequency_mhz=None, home=None):
    """Return a --root directory, seeded once from the existing working setup.

    Every launch reapplies the advertised tuning/mode. Audio, gain, bookmarks,
    and other settings in an existing profile are retained. FM station launches
    reuse the FM profile with the selected frequency. No normal-root file is
    modified. The caller must first ensure SDR++ is no longer running.
    """
    preset = get_preset(preset_id, frequency_mhz=frequency_mhz)
    if preset_id is None:
        raise PresetError("Usual settings do not need a preset profile.")
    home = Path(home) if home is not None else Path.home()
    base = home / ".config" / "uconsole-radio" / "sdrpp-presets"
    target = base / preset_id
    source = next((home / ".config" / name for name in ("sdrpp-brown", "sdrpp")
                   if (home / ".config" / name / "config.json").is_file()), None)
    if target.is_symlink() or (source and (target.resolve() == source.resolve() or source.resolve() in target.resolve().parents)):
        raise PresetError("Preset folder overlaps the normal SDR++ settings. Use usual settings and check the profile path.")
    if target.exists():
        # Do not follow profile-file symlinks back into the normal SDR++ root.
        if any(path.is_symlink() for path in target.glob("*.json")):
            raise PresetError("Preset settings contain linked files; use usual settings and check the profile folder.")
        _apply(target, preset)
        return target
    if source is None:
        raise PresetError("Open SDR++ with usual settings once, choose RTL-SDR and your audio output, then close it before trying a band preset.")
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="." + preset_id + "-", dir=base) as staging:
        staging = Path(staging)
        for path in source.glob("*.json"):
            if path.is_file():
                shutil.copyfile(path, staging / path.name)
        _apply(staging, preset)
        staging.rename(target)
    return target
