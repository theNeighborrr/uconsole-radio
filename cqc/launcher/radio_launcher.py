#!/usr/bin/env python3
"""Offline uConsole radio controls. Radio processes always run in one worker."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable

from radio_backend import MESHTASTIC_URL, RadioController
from sdrpp_presets import SDRPP_PRESETS


BASE = Path(__file__).resolve().parent
BG = "#111b27"
PANEL = "#1c2b3b"
FG = "#eff7ff"
MUTED = "#b4c7d9"
CYAN = "#66e3ed"
YELLOW = "#ffdc80"
ACTIVITIES = ("Weather", "FM", "Tower", "Aircraft", "SDR++", "Meshtastic")


def load_stations(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("stations", []) if isinstance(data, dict) else data
    if not isinstance(rows, list) or not rows:
        raise ValueError("The FM station list is empty or invalid.")
    stations = []
    for row in rows:
        frequency = str(row["frequency"])
        if not 87.5 <= float(frequency) <= 108.0:
            raise ValueError("An FM frequency is outside the supported band.")
        scope = str(row.get("scope", "metro"))
        suffix = "" if scope == "metro" else f" [{scope}]"
        label = f"{frequency}  {row.get('callsign', '')} — {row.get('brand', '')}{suffix}"
        list_label = f"{frequency}  " + (f"[{scope}] " if scope != "metro" else "") + f"{row.get('callsign', '')} — {row.get('brand', '')}"
        note = " · ".join(str(row[k]) for k in ("format", "city", "note") if row.get(k))
        stations.append({"frequency": frequency, "mode": "wbfm", "label": label, "list_label": list_label,
                         "note": note, "search": json.dumps(row, ensure_ascii=False).casefold()})
    return stations


WEATHER = [
    {"frequency": f"{freq:.3f}", "mode": "fm",
     "label": f"{freq:.3f} MHz — " + ("Memphis WXK49" if freq == 162.475 else "NOAA weather channel"),
     "note": "Manual listening only; this launcher does not sound automatic weather alarms."}
    for freq in (162.475, 162.400, 162.425, 162.450, 162.500, 162.525, 162.550)
]
TOWER = [
    {"frequency": "119.700", "mode": "am", "label": "119.700 MHz — Memphis tower",
     "note": "AM voice. Aircraft and tower transmissions are intermittent; gaps are normal."},
    {"frequency": "127.750", "mode": "am", "label": "127.750 MHz — Memphis ATIS",
     "note": "AM voice. Repeating airport weather and runway information."},
]


def acquire_lock():
    directory = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "uconsole-radio"
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / "launcher.lock").open("a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


class RadioApp:
    def __init__(self, root: tk.Tk, guide_dir: Path):
        self.root = root
        self.guide_dir = guide_dir.expanduser().resolve()
        self.events: queue.Queue = queue.Queue()
        self.jobs: queue.Queue = queue.Queue()
        self.busy = False
        self.closing = False
        self.status_pending = False
        self.mode = "Weather"
        self.station_rows: list[dict] = []
        self.last_status: dict = {}
        self.log_lines: list[str] = []
        self.log_window = None
        self.log_text = None
        self.fm_error = ""
        try:
            self.fm_stations = load_stations(BASE / "stations.json")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.fm_stations = []
            self.fm_error = f"Cannot load FM stations: {exc}"
        self.selected_by_mode = {
            "Weather": WEATHER[0]["label"],
            "FM": next((row["label"] for row in self.fm_stations if row["frequency"] == "91.1"), ""),
            "Tower": TOWER[0]["label"],
            "SDR++": SDRPP_PRESETS[0]["label"],
        }

        self.controller = RadioController(logger=self._log)
        self._build()
        self._bind_keys()
        self.worker = threading.Thread(target=self._work, name="radio-controls", daemon=True)
        self.worker.start()
        self.select_mode("Weather")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after_idle(self.station_list.focus_set)
        self.root.after(100, self._drain)
        self.root.after(200, self._poll_status)

    def _build(self):
        root = self.root
        root.title("uConsole Radio")
        root.geometry("760x445")
        root.minsize(620, 360)
        root.configure(bg=BG)
        root.option_add("*Font", ("DejaVu Sans", 12))
        style = ttk.Style(root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG, font=("DejaVu Sans", 12))
        style.configure("TButton", background=PANEL, foreground=FG, padding=(7, 5),
                        font=("DejaVu Sans", 11), focuscolor=YELLOW)
        style.map("TButton", background=[("active", "#29465b"), ("disabled", "#17212b")],
                  foreground=[("disabled", "#728499")])
        style.configure("Mode.TButton", padding=(5, 6), font=("DejaVu Sans", 11))
        style.configure("Chosen.TButton", background=CYAN, foreground=BG, font=("DejaVu Sans", 11),
                        padding=(5, 6), focuscolor=BG)
        style.map("Chosen.TButton", background=[("active", "#a3f4f9"), ("disabled", "#3e7480")],
                  foreground=[("disabled", BG)])
        style.configure("Start.TButton", background=CYAN, foreground=BG,
                        font=("DejaVu Sans", 12, "bold"), focuscolor=BG)
        style.map("Start.TButton", background=[("active", "#a3f4f9"), ("disabled", "#3e7480")],
                  foreground=[("disabled", BG)])
        style.configure("TEntry", fieldbackground=PANEL, foreground=FG, insertcolor=FG, padding=3)

        outer = ttk.Frame(root, padding=(10, 7))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)
        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="uConsole Radio", font=("DejaVu Sans", 17, "bold"), foreground=CYAN).grid(row=0, column=0, sticky="w")
        ttk.Button(header, text="Details [D]", command=self.show_details).grid(row=0, column=1, sticky="e")
        ttk.Button(header, text="Camera", command=lambda: self._open("http://127.0.0.1:8765/")).grid(row=0, column=2, sticky="e", padx=(6, 0))
        self.hardware_label = tk.Label(
            header, text="AIO  GPS ?  LoRa ?  SDR ?  USB ?   |   TUNER ?   |   BAT ?",
            background=BG, foreground=MUTED, anchor="w",
            font=("DejaVu Sans Mono", 9))
        self.hardware_label.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(3, 0))
        modes = ttk.Frame(outer)
        modes.grid(row=1, column=0, sticky="ew", pady=(0, 5))
        self.mode_buttons = {}
        for index, mode in enumerate(ACTIVITIES):
            modes.columnconfigure(index, weight=len(mode) + 3)
            button = ttk.Button(modes, text=f"{index + 1}  {mode}", style="Mode.TButton",
                                width=1, command=lambda mode=mode: self.select_mode(mode))
            button.grid(row=0, column=index, sticky="ew", padx=(0 if index == 0 else 5, 0))
            self.mode_buttons[mode] = button

        self.selection = ttk.Frame(outer)
        self.selection.grid(row=2, column=0, sticky="nsew")
        self.selection.columnconfigure(0, weight=1)
        self.selection.rowconfigure(1, weight=1)
        self.filter_frame = ttk.Frame(self.selection)
        self.filter_frame.columnconfigure(1, weight=1)
        ttk.Label(self.filter_frame, text="Find FM [/ or F2]", font=("DejaVu Sans", 10)).grid(row=0, column=0, padx=(0, 8))
        self.query = tk.StringVar()
        self.filter_entry = ttk.Entry(self.filter_frame, textvariable=self.query, font=("DejaVu Sans", 11))
        self.filter_entry.grid(row=0, column=1, sticky="ew")
        self.query.trace_add("write", lambda *_args: self._filter_changed())

        list_frame = ttk.Frame(self.selection)
        list_frame.grid(row=1, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        self.station_list = tk.Listbox(
            list_frame, height=4, bg=PANEL, fg=FG, font=("DejaVu Sans", 12),
            selectmode="browse", exportselection=False, activestyle="none",
            selectbackground=CYAN, selectforeground=BG, disabledforeground="#728499",
            highlightthickness=2, highlightbackground="#344a5e", highlightcolor=YELLOW,
            borderwidth=0, takefocus=True)
        self.station_list.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.station_list.yview, takefocus=False)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.station_list.configure(yscrollcommand=scrollbar.set)
        self.station_list.bind("<<ListboxSelect>>", lambda _event: self._station_changed())
        self.note = tk.Label(self.selection, text="", background=BG, foreground=MUTED, wraplength=710,
                              height=2, font=("DejaVu Sans", 10), justify="left", anchor="nw")
        self.note.grid(row=2, column=0, sticky="ew", pady=(3, 0))
        self.selection.bind("<Configure>", lambda event: self.note.configure(wraplength=max(300, event.width - 4)))

        middle = ttk.Frame(outer)
        middle.grid(row=3, column=0, sticky="ew", pady=(4, 4))
        middle.columnconfigure(2, weight=1)
        self.start_button = ttk.Button(middle, text="Listen", style="Start.TButton", command=self.start)
        self.start_button.grid(row=0, column=0, sticky="nw", padx=(0, 10))
        self.fm_sdrpp_button = ttk.Button(middle, text="Open in SDR++ [S]", command=self.open_fm_sdrpp)
        self.fm_sdrpp_button.grid(row=0, column=1, sticky="nw", padx=(0, 10))
        self.fm_sdrpp_button.grid_remove()
        self.status_label = tk.Label(middle, text="Checking receiver…", background=BG, foreground=CYAN,
                                     wraplength=525, width=1, height=2, justify="left", anchor="nw", font=("DejaVu Sans", 10))
        self.status_label.grid(row=0, column=2, sticky="new")
        self.status_label.bind("<Configure>", lambda event: self.status_label.configure(wraplength=max(1, event.width - 4)))
        bottom = ttk.Frame(outer)
        bottom.grid(row=4, column=0, sticky="ew")
        self.stop_button = ttk.Button(bottom, text="Stop [Esc]", command=self.stop)
        self.off_button = ttk.Button(bottom, text="SDR off [O]", command=self.power_off)
        self.guide_button = ttk.Button(bottom, text="Guide [G]", command=self.open_guide)
        self.close_button = ttk.Button(bottom, text="Close [Q]", command=self.close)
        for index, button in enumerate((self.stop_button, self.off_button, self.guide_button, self.close_button)):
            bottom.columnconfigure(index, weight=1, uniform="bottom")
            button.grid(row=0, column=index, sticky="ew", padx=(0 if index == 0 else 5, 0))
        ttk.Label(outer, text="← → Activity   ↑ ↓ Station   Enter Start   / FM search   Tab Buttons",
                  foreground=MUTED, font=("DejaVu Sans", 10)).grid(row=5, column=0, sticky="w", pady=(4, 0))

    def _bind_keys(self):
        # This tag runs before widget/class bindings, so a Listbox arrow moves once.
        self.key_tag = f"uConsoleRadioKeys{id(self)}"
        self.root.bind_class(self.key_tag, "<KeyPress>", self._on_key)
        def attach(widget):
            widget.bindtags((self.key_tag, *widget.bindtags()))
            for child in widget.winfo_children():
                attach(child)
        attach(self.root)

    def _on_key(self, event):
        if event.widget.winfo_toplevel() != self.root:
            return None
        key = event.keysym
        editing = isinstance(event.widget, (tk.Entry, ttk.Entry, tk.Text, ttk.Combobox))
        if editing:
            if key in ("Escape", "Return", "KP_Enter") and event.widget == self.filter_entry:
                self.station_list.focus_set()
                return "break"
            return None
        if key in ("Return", "KP_Enter"):
            if isinstance(event.widget, (ttk.Button, tk.Button)):
                event.widget.invoke()
            elif not self.busy and not self.closing:
                self.start()
            return "break"
        if key == "Escape":
            self.stop()
            return "break"
        if key in ("Left", "Right"):
            if not self.busy and not self.closing:
                index = ACTIVITIES.index(self.mode)
                self.select_mode(ACTIVITIES[(index + (1 if key == "Right" else -1)) % len(ACTIVITIES)])
            return "break"
        if key in ("Up", "Down"):
            if not self.busy and not self.closing:
                self._move_station(1 if key == "Down" else -1)
            return "break"
        if key in ("slash", "/", "F2"):
            self.focus_search()
            return "break"
        if key in ("1", "2", "3", "4", "5", "6"):
            self.select_mode(ACTIVITIES[int(key) - 1])
            return "break"
        action = {"s": self.open_fm_sdrpp, "o": self.power_off, "g": self.open_guide,
                  "d": self.show_details, "q": self.close}.get(key.lower())
        if action:
            if not self.closing:
                action()
            return "break"
        return None

    def focus_search(self):
        if self.busy or self.closing:
            return
        if self.mode != "FM":
            self.select_mode("FM")
        self.filter_entry.focus_set()
        self.filter_entry.selection_range(0, "end")

    def select_mode(self, mode: str):
        if self.busy or self.closing:
            return
        self.mode = mode
        for name, button in self.mode_buttons.items():
            button.configure(style="Chosen.TButton" if name == mode else "Mode.TButton")
        self.start_button.configure(text={"Aircraft": "Start tracking", "SDR++": "Open SDR++",
                                          "Meshtastic": "Open Meshtastic"}.get(mode, "Listen [Enter]"))
        self.stop_button.configure(text="Stop mesh [Esc]" if mode == "Meshtastic" else "Stop [Esc]")
        self.off_button.configure(text="LoRa off [O]" if mode == "Meshtastic" else "SDR off [O]")
        if mode == "FM":
            self.fm_sdrpp_button.grid()
            self.filter_frame.grid(row=0, column=0, sticky="ew", pady=(0, 3))
            self._filter_changed()
        else:
            self.fm_sdrpp_button.grid_remove()
            self.filter_frame.grid_remove()
            if mode == "Aircraft":
                self._set_rows([])
                self.station_list.insert("end", "Track nearby aircraft — tar1090")
                self.station_list.selection_set(0)
                self.station_list.activate(0)
                self.note.configure(text="Starts your local receiver and opens tar1090. Use the installed OpenFreeMap offline layer with Wi-Fi off.")
            elif mode == "SDR++":
                self._set_rows(SDRPP_PRESETS)
            elif mode == "Meshtastic":
                self._set_rows([{"label": "Meshtastic — messages, nodes and positions",
                                 "note": "Attach LoRa ANT7 and GPS ANT6. Opens the local client; FM and aircraft can keep running. Close other GPS viewers first."}])
            else:
                self._set_rows(WEATHER if mode == "Weather" else TOWER)
        self._sync_controls()
        if self.last_status:
            self._show_status(self.last_status)
        self.station_list.focus_set()

    def _filter_changed(self):
        if self.mode != "FM":
            return
        terms = self.query.get().casefold().split()
        rows = [row for row in self.fm_stations if all(term in row["search"] for term in terms)]
        self._set_rows(rows)
        if not rows:
            self.note.configure(text=self.fm_error or "No matching stations. Try a frequency, station name, music style or city.")
        self._sync_controls()

    def _set_rows(self, rows):
        self.station_rows = list(rows)
        self.station_list.delete(0, "end")
        for row in rows:
            self.station_list.insert("end", row.get("list_label", row["label"]))
        if rows:
            previous = self.selected_by_mode.get(self.mode)
            index = next((index for index, row in enumerate(rows) if row["label"] == previous), 0)
            self._select_station(index)
        else:
            self.note.configure(text="")

    def _current_index(self):
        selected = self.station_list.curselection()
        return int(selected[0]) if selected else -1

    def _select_station(self, index):
        self.station_list.selection_clear(0, "end")
        self.station_list.selection_set(index)
        self.station_list.activate(index)
        self.station_list.see(index)
        self._station_changed()

    def _move_station(self, delta):
        if self.station_rows:
            index = max(0, min(len(self.station_rows) - 1, self._current_index() + delta))
            self._select_station(index)
        self.station_list.focus_set()

    def _station_changed(self):
        index = self._current_index()
        if 0 <= index < len(self.station_rows):
            row = self.station_rows[index]
            self.selected_by_mode[self.mode] = row["label"]
            note = row["note"]
            self.note.configure(text=note if len(note) <= 165 else note[:162].rstrip() + "…")

    def _sync_controls(self):
        disabled = self.busy or self.closing
        state = "disabled" if disabled else "normal"
        for button in (*self.mode_buttons.values(), self.stop_button, self.off_button):
            button.configure(state=state)
        self.start_button.configure(state="disabled" if disabled or (self.mode not in {"Aircraft", "SDR++"} and not self.station_rows) else "normal")
        self.fm_sdrpp_button.configure(state="disabled" if disabled or self.mode != "FM" or not self.station_rows else "normal")
        self.filter_entry.configure(state=state)
        self.station_list.configure(state=state)
        if self.closing:
            self.close_button.configure(state="disabled")
            self.guide_button.configure(state="disabled")

    def _log(self, *parts):
        self.events.put(("log", " ".join(str(part) for part in parts)))

    def _work(self):
        while True:
            name, action = self.jobs.get()
            try:
                result = action()
                self.events.put(("done", name, result, None))
            except Exception as exc:
                self.events.put(("done", name, None, str(exc)))
            finally:
                self.jobs.task_done()
            if name == "close":
                return

    def _submit(self, name: str, action: Callable):
        if self.busy or self.closing:
            return
        self.busy = True
        self.status_label.configure(text="Working…", foreground=CYAN)
        self._sync_controls()
        self.jobs.put((name, action))

    def start(self):
        if self.mode == "Meshtastic":
            self._submit("meshtastic", self.controller.start_meshtastic)
            return
        if self.mode == "Aircraft":
            self._submit("aircraft", self.controller.start_aircraft)
            return
        if self.mode == "SDR++":
            index = self._current_index()
            if not 0 <= index < len(self.station_rows):
                return
            preset_id = self.station_rows[index]["id"]
            self._submit("sdrpp", lambda: self.controller.start_sdrpp(preset_id))
            return
        index = self._current_index()
        if not 0 <= index < len(self.station_rows):
            return
        row = dict(self.station_rows[index])
        self._submit("audio", lambda: self.controller.start_audio(
            frequency=row["frequency"] + "M", mode=row["mode"], label=row["label"]))

    def open_fm_sdrpp(self):
        if self.busy or self.closing or self.mode != "FM":
            return
        index = self._current_index()
        if not 0 <= index < len(self.station_rows):
            return
        frequency = self.station_rows[index]["frequency"]
        self._submit("sdrpp", lambda: self.controller.start_sdrpp("fm", frequency_mhz=frequency))

    def stop(self):
        if self.mode == "Meshtastic":
            self._submit("stop-mesh", self.controller.stop_meshtastic)
        else:
            self._submit("stop", self.controller.stop)

    def power_off(self):
        if self.mode == "Meshtastic":
            self._submit("mesh-off", lambda: self.controller.stop_meshtastic(power_off=True))
        else:
            self._submit("power-off", self.controller.power_off)

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.status_label.configure(text="Closing audio controls…", foreground=CYAN)
        self._sync_controls()
        # FIFO ensures close runs after any action already starting the receiver.
        self.jobs.put(("close", self.controller.close))

    def finish(self):
        """Complete worker cleanup even when the Tk loop exits unexpectedly."""
        if self.worker.is_alive():
            if not self.closing:
                self.closing = True
                self.jobs.put(("close", self.controller.close))
            self.worker.join()

    def _poll_status(self):
        if self.closing:
            return
        if not self.busy and not self.status_pending:
            self.status_pending = True
            self.jobs.put(("status", self.controller.status))
        self.root.after(5000, self._poll_status)

    def _show_status(self, status):
        if not isinstance(status, dict):
            return
        self.last_status = status

        aio = status.get("aio") or {}
        rails = aio.get("rails") or {}
        battery = aio.get("battery") or {}
        owner = str(status.get("tuner_owner") or "?")
        def rail(name):
            return str(rails.get(name, "?"))
        hardware = (
            f"AIO  GPS {rail('GPS')}  LoRa {rail('LORA')}  SDR {rail('SDR')}  USB {rail('USB')}"
            f"   |   TUNER {owner}"
        )
        mesh_state = status.get("meshtastic_state", "unknown")
        hardware += "   |   MESH " + ("ON" if mesh_state == "active" else "?" if mesh_state == "unknown" else "OFF")
        capacity = battery.get("Capacity")
        if capacity:
            hardware += f"   |   BAT {capacity}"
        self.hardware_label.configure(
            text=hardware,
            foreground=YELLOW if owner.startswith("CONFLICT") else MUTED)

        if self.mode == "Meshtastic":
            text = {
                "active": "Meshtastic is running. Enter opens its local client.\nEsc stops mesh · O turns LoRa off · Q leaves mesh running.",
                "not-found": "Meshtastic is not installed as a service.\nGuide [G] has setup checks; other radios still work.",
                "unknown": "Could not check Meshtastic. See Details [D] or Guide [G].",
                "failed": "Meshtastic service failed. See Guide [G] for its log.\nOpen Meshtastic retries the saved setup.",
            }.get(mesh_state, "Meshtastic is stopped. Enter starts LoRa and GPS.\nSDR listening and aircraft tracking can keep running.")
        elif status.get("audio_active"):
            text = "Listening: " + str(status.get("label") or status.get("frequency") or "Radio")
        elif status.get("sdrpp_active"):
            text = str(status.get("label") or "SDR++") + "\nPlay there · Esc stops · Q keeps open"
        elif status.get("aircraft_active"):
            text = "Aircraft tracking active.\nAudio is stopped."
        elif status.get("powered") is False:
            text = "SDR is off. Choose an activity to start."
        elif owner not in {"FREE", "?"}:
            text = f"Tuner in use by {owner}."
        else:
            text = "Ready. Choose an activity and press Enter."
        error = status.get("meshtastic_error" if self.mode == "Meshtastic" else "last_error")
        if error:
            text = str(error)  # Keep failures visible in the two-line status area.
        self.status_label.configure(text=text, foreground=YELLOW if error else CYAN)

    def _drain(self):
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event[0] == "log":
                self.log_lines.append(event[1])
                self.log_lines = self.log_lines[-250:]
                if self.log_window is not None and self.log_window.winfo_exists():
                    self._refresh_details()
                continue
            _, name, result, error = event
            if name == "status":
                self.status_pending = False
                if not self.busy and not self.closing:
                    if error:
                        self.log_lines.append(error)
                        self.status_label.configure(text=error, foreground=YELLOW)
                    else:
                        self._show_status(result)
                continue
            if name == "close":
                if error:
                    messagebox.showwarning("Radio cleanup", error, parent=self.root)
                self.root.destroy()
                return
            self.busy = False
            if self.closing:
                continue
            if error:
                self.log_lines.append(error)
                self.status_label.configure(text=error, foreground=YELLOW)
            else:
                self._show_status(result)
                if name == "aircraft":
                    self._open("http://localhost/tar1090/")
                elif name == "meshtastic":
                    self._open(MESHTASTIC_URL)
            self._sync_controls()
        self.root.after(100, self._drain)

    def _open(self, target: str):
        try:
            subprocess.Popen(["xdg-open", target], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except OSError as exc:
            self.status_label.configure(text=f"Could not open the viewer: {exc}", foreground=YELLOW)

    def open_guide(self):
        path = self.guide_dir / {"SDR++": "sdrpp.html", "FM": "memphis-fm.html",
                                 "Meshtastic": "meshtastic-gps.html"}.get(self.mode, "00-START-HERE.html")
        if not path.is_file():
            messagebox.showinfo("Field guide", f"The guide was not found at:\n{path}", parent=self.root)
            return
        self._open(path.as_uri())

    def show_details(self):
        if self.log_window is not None and self.log_window.winfo_exists():
            self.log_window.lift()
            return
        window = tk.Toplevel(self.root)
        self.log_window = window
        window.title("Radio details")
        window.geometry("690x310")
        window.configure(bg=BG)
        self.log_text = tk.Text(window, bg=BG, fg=FG, insertbackground=FG, wrap="word",
                                font=("DejaVu Sans Mono", 10), padx=8, pady=8)
        self.log_text.pack(fill="both", expand=True)
        ttk.Button(window, text="Close details", command=window.destroy).pack(pady=5)
        self._refresh_details()

    def _refresh_details(self):
        path = self.last_status.get("log_path", "")
        selected = self._current_index()
        station = self.station_rows[selected] if 0 <= selected < len(self.station_rows) else None
        selection = (["Selected station:", station["label"], station["note"], ""] if station else [])
        current_error = self.last_status.get("meshtastic_error" if self.mode == "Meshtastic" else "last_error")
        error_lines = (["Receiver error:", str(current_error), ""] if current_error else [])
        lines = selection + error_lines + ([f"Radio log: {path}", ""] if path else []) + self.log_lines
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("end", "\n".join(lines) or "No diagnostic messages yet.")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


def main():
    parser = argparse.ArgumentParser(description="Offline uConsole radio launcher")
    parser.add_argument("--guide-dir", type=Path, default=Path.home() / "Desktop" / "uConsole-Quick-Commands")
    args = parser.parse_args()
    if os.geteuid() == 0:
        print("Run the launcher as your normal desktop user, not with sudo.", file=sys.stderr)
        return 1
    lock = acquire_lock()
    if lock is None:
        print("uConsole Radio is already open.", file=sys.stderr)
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo("uConsole Radio", "The radio launcher is already open. Switch to its window.")
            root.destroy()
        except tk.TclError:
            pass
        return 0
    app = None
    previous_handlers = {}
    try:
        root = tk.Tk()
        app = RadioApp(root, args.guide_dir)
        def handle_signal(_signum, _frame):
            try:
                root.after(0, app.close)
            except tk.TclError:
                pass
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous_handlers[sig] = signal.signal(sig, handle_signal)
        root.mainloop()
        return 0
    finally:
        if app is not None:
            app.finish()
        for sig, previous in previous_handlers.items():
            signal.signal(sig, previous)
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
