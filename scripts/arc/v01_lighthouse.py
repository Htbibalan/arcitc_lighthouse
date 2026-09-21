
# based on the v6.2  of the original repo, fixes/enhancements highlighted with # sign.
# Hamid.

import json
import csv
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Callable

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import requests
from phue import Bridge


PRESET_XY = {
    "red":   (0.675, 0.322),
    "green": (0.409, 0.518),
    "blue":  (0.167, 0.040),
    "white": (0.3127, 0.3290),
    "warm":  (0.501, 0.415),
    "cool":  (0.300, 0.300),
}

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def parse_xy(xy_str: str) -> Optional[Tuple[float, float]]:
    if not xy_str:
        return None
    try:
        a, b = xy_str.split(",", 1)
        x = float(a.strip())
        y = float(b.strip())
        return (clamp(x, 0.0, 1.0), clamp(y, 0.0, 1.0))
    except Exception:
        return None


def parse_date(s: str) -> Optional[date]:
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            pass
    return None


def parse_time_hhmm(s: str) -> Optional[Tuple[int, int]]:
    s = (s or "").strip()
    if not s:
        return None
    try:
        parts = s.split(":")
        if len(parts) != 2:
            return None
        hh = int(parts[0])
        mm = int(parts[1])
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return None
        return hh, mm
    except Exception:
        return None


def dt_from_date_time(d: date, hhmm: str) -> datetime:
    hm = parse_time_hhmm(hhmm)
    if hm is None:
        raise ValueError("Invalid time HH:MM")
    return datetime(d.year, d.month, d.day, hm[0], hm[1], 0)


def wavelength_to_xy_nm(wavelength_nm: float) -> Tuple[float, float]:
    wl = clamp(float(wavelength_nm), 380.0, 700.0)

    def wl_to_rgb(w):
        gamma = 0.8
        if 380 <= w < 440:
            r = -(w - 440) / (440 - 380)
            g = 0.0
            b = 1.0
        elif 440 <= w < 490:
            r = 0.0
            g = (w - 440) / (490 - 440)
            b = 1.0
        elif 490 <= w < 510:
            r = 0.0
            g = 1.0
            b = -(w - 510) / (510 - 490)
        elif 510 <= w < 580:
            r = (w - 510) / (580 - 510)
            g = 1.0
            b = 0.0
        elif 580 <= w < 645:
            r = 1.0
            g = -(w - 645) / (645 - 580)
            b = 0.0
        else:
            r = 1.0
            g = 0.0
            b = 0.0

        if 380 <= w < 420:
            factor = 0.3 + 0.7 * (w - 380) / (420 - 380)
        elif 420 <= w <= 645:
            factor = 1.0
        else:
            factor = 0.3 + 0.7 * (700 - w) / (700 - 645)

        r = (r * factor) ** gamma
        g = (g * factor) ** gamma
        b = (b * factor) ** gamma
        return r, g, b

    r, g, b = wl_to_rgb(wl)
    X = r * 0.4124 + g * 0.3576 + b * 0.1805
    Y = r * 0.2126 + g * 0.7152 + b * 0.0722
    Z = r * 0.0193 + g * 0.1192 + b * 0.9505
    denom = X + Y + Z
    if denom <= 1e-9:
        return PRESET_XY["white"]
    x = X / denom
    y = Y / denom
    return (clamp(x, 0.0, 1.0), clamp(y, 0.0, 1.0))


@dataclass
class LightState:
    action: str
    bri: Optional[int] = None
    preset: Optional[str] = None
    ct: Optional[int] = None
    xy: Optional[Tuple[float, float]] = None
    wavelength_nm: Optional[float] = None

    def resolved_xy(self) -> Optional[Tuple[float, float]]:
        xy = self.xy
        if self.wavelength_nm is not None:
            xy = wavelength_to_xy_nm(self.wavelength_nm)
        if self.preset:
            xy = PRESET_XY.get(self.preset, xy)
        return xy


@dataclass
class AdvancedSchedule:
    name: str
    boxes: List[str]
    start_date: str
    start_time: str
    end_date: str
    end_time: str
    recurrence: str
    every_n: int = 1
    weekdays: List[int] = field(default_factory=list)
    until_date: str = ""
    start_state: LightState = field(default_factory=lambda: LightState(action="set", bri=160, preset="white"))
    end_state: LightState = field(default_factory=lambda: LightState(action="off"))
    enabled: bool = True
    _last_start_key: Optional[str] = field(default=None, repr=False)
    _last_end_key: Optional[str] = field(default=None, repr=False)
    # changed: prevents duplicate schedule workers and allows delayed retry after temporary Bridge failures
    _pending_start_key: Optional[str] = field(default=None, repr=False)
    _pending_end_key: Optional[str] = field(default=None, repr=False)
    _start_retry_after: Optional[datetime] = field(default=None, repr=False)
    _end_retry_after: Optional[datetime] = field(default=None, repr=False)

    def _date_range_ok(self, d: date) -> bool:
        sd = parse_date(self.start_date)
        if sd is None:
            return False
        if d < sd:
            return False
        ud = parse_date(self.until_date) if self.until_date.strip() else None
        if ud is not None and d > ud:
            return False
        return True

    def _occurs_on(self, d: date) -> bool:
        if not self._date_range_ok(d):
            return False
        rec = (self.recurrence or "once").lower()
        sd = parse_date(self.start_date)
        if sd is None:
            return False
        if rec == "once":
            return d == sd
        if rec == "daily":
            delta = (d - sd).days
            if delta < 0:
                return False
            n = max(1, int(self.every_n))
            return (delta % n) == 0
        if rec == "weekly":
            if not self.weekdays:
                return False
            if d.weekday() not in self.weekdays:
                return False
            delta = (d - sd).days
            if delta < 0:
                return False
            n = max(1, int(self.every_n))
            weeks = delta // 7
            return (weeks % n) == 0
        return False

    def window_for_date(self, d: date) -> Optional[Tuple[datetime, datetime, str, str]]:
        if not self._occurs_on(d):
            return None
        try:
            st = dt_from_date_time(d, self.start_time)
            ed = dt_from_date_time(d, self.end_time)
        except Exception:
            return None
        if ed <= st:
            ed = ed + timedelta(days=1)
            end_key = ed.strftime("%Y-%m-%d") + " " + self.end_time
        else:
            end_key = d.strftime("%Y-%m-%d") + " " + self.end_time
        start_key = d.strftime("%Y-%m-%d") + " " + self.start_time
        return st, ed, start_key, end_key

    def next_boundaries(self, now: datetime) -> Tuple[Optional[Tuple[datetime, str]], Optional[Tuple[datetime, str]]]:
        if not self.enabled:
            return None, None

        start_candidates_past: List[Tuple[datetime, str]] = []
        start_candidates_future: List[Tuple[datetime, str]] = []
        end_candidates_past: List[Tuple[datetime, str]] = []
        end_candidates_future: List[Tuple[datetime, str]] = []

        for offset in (-1, 0, 1, 2, 3, 4, 5, 6, 7):
            d = now.date() + timedelta(days=offset)
            w = self.window_for_date(d)
            if not w:
                continue

            st, ed, sk, ek = w

            if st <= now:
                start_candidates_past.append((st, sk))
            else:
                start_candidates_future.append((st, sk))

            if ed <= now:
                end_candidates_past.append((ed, ek))
            else:
                end_candidates_future.append((ed, ek))

        best_start = None
        if start_candidates_past:
            max_past = max(start_candidates_past, key=lambda x: x[0])
            if max_past[1] != self._last_start_key:
                best_start = max_past
        if best_start is None and start_candidates_future:
            best_start = min(start_candidates_future, key=lambda x: x[0])

        best_end = None
        if end_candidates_past:
            max_past = max(end_candidates_past, key=lambda x: x[0])
            if max_past[1] != self._last_end_key:
                best_end = max_past
        if best_end is None and end_candidates_future:
            best_end = min(end_candidates_future, key=lambda x: x[0])

        return best_start, best_end


class HueController:
    def __init__(self):
        self.bridge: Optional[Bridge] = None
        self.bridge_ip: str = ""
        self.lights_by_name: Dict[str, dict] = {}
        # changed: RLock allows nested refresh/reconnect calls without deadlock
        self.lock = threading.RLock()
        # changed: local auth storage avoids depending only on phue's hidden token file
        self.username: str = ""
        self.auth_path = Path.home() / ".lighthouse_hue_auth.json"
        self.last_successful_api: Optional[datetime] = None

    def discover_bridge_ip_online(self) -> Optional[str]:
        try:
            r = requests.get("https://discovery.meethue.com", timeout=5)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list) and data:
                return data[0].get("internalipaddress")
        except Exception:
            return None
        return None

    # changed: stores and reuses the Hue username so reconnects do not need the Bridge button unless the Bridge rejects the user
    def _load_auth(self) -> Dict[str, str]:
        try:
            data = json.loads(self.auth_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items() if v}
        except Exception:
            pass
        return {}

    # changed: stores and reuses the Hue username so reconnects do not need the Bridge button unless the Bridge rejects the user
    def _save_auth(self) -> None:
        if not self.bridge_ip or not self.username:
            return
        data = self._load_auth()
        data[self.bridge_ip] = self.username
        self.auth_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # changed: direct Hue API calls use timeouts and do not rely on a stale phue Bridge object
    def _request_json(self, method: str, path: str = "", **kwargs):
        if not self.bridge_ip:
            raise RuntimeError("Bridge IP is empty.")
        if not self.username:
            raise RuntimeError("Hue username is missing. Press the Bridge button and use Connect / Register once.")

        if path and not path.startswith("/"):
            path = "/" + path
        url = f"http://{self.bridge_ip}/api/{self.username}{path}"
        response = requests.request(method, url, timeout=6, **kwargs)
        response.raise_for_status()
        data = response.json()

        if isinstance(data, list):
            errors = [item.get("error") for item in data if isinstance(item, dict) and item.get("error")]
            if errors:
                description = errors[0].get("description", str(errors[0]))
                raise RuntimeError(description)

        if isinstance(data, dict) and data.get("error"):
            description = data["error"].get("description", str(data["error"]))
            raise RuntimeError(description)

        return data

    # changed: connect first tries the saved username, then falls back to Bridge-button registration only if needed
    def connect(self, ip: str) -> None:
        with self.lock:
            self.bridge_ip = ip.strip()
            if not self.bridge_ip:
                raise RuntimeError("Bridge IP is empty.")

            saved_username = self._load_auth().get(self.bridge_ip, "")
            saved_error = None

            if saved_username:
                try:
                    self.username = saved_username
                    self._request_json("GET", "")
                    self.bridge = Bridge(self.bridge_ip, username=self.username)
                    self.last_successful_api = datetime.now()
                    return
                except Exception as e:
                    saved_error = e
                    self.username = ""
                    self.bridge = None

            try:
                b = Bridge(self.bridge_ip)
                b.connect()
                username = str(getattr(b, "username", "") or "")
                if not username:
                    raise RuntimeError("Could not read Hue username after Bridge registration.")
                self.username = username
                self.bridge = b
                self._request_json("GET", "")
                self._save_auth()
                self.last_successful_api = datetime.now()
            except Exception as e:
                if saved_error:
                    raise RuntimeError(f"Saved Hue username failed: {saved_error}. Registration failed: {e}")
                raise

    # changed: verifies the Bridge API before every important operation and rebuilds the connection if possible
    def ensure_connected(self) -> None:
        with self.lock:
            if not self.bridge_ip:
                raise RuntimeError("Bridge IP is empty.")

            if not self.username:
                self.username = self._load_auth().get(self.bridge_ip, "")

            if self.username:
                try:
                    self._request_json("GET", "")
                    if not self.bridge:
                        self.bridge = Bridge(self.bridge_ip, username=self.username)
                    self.last_successful_api = datetime.now()
                    return
                except Exception:
                    self.bridge = None

            b = Bridge(self.bridge_ip)
            b.connect()
            username = str(getattr(b, "username", "") or "")
            if not username:
                raise RuntimeError("Hue username is missing. Press the Bridge button and use Connect / Register once.")

            self.username = username
            self.bridge = b
            self._request_json("GET", "")
            self._save_auth()
            self.last_successful_api = datetime.now()

    # changed: refresh always checks/reconnects first and uses direct API with timeout
    def refresh_lights(self) -> Dict[str, dict]:
        with self.lock:
            self.ensure_connected()
            lights = self._request_json("GET", "/lights")

            counts: Dict[str, int] = {}
            by_name: Dict[str, dict] = {}
            for lid, info in lights.items():
                base = info.get("name", f"Light {lid}")
                counts[base] = counts.get(base, 0) + 1
            for lid, info in lights.items():
                base = info.get("name", f"Light {lid}")
                name = base if counts.get(base, 0) <= 1 else f"{base} (id:{lid})"
                by_name[name] = {"id": int(lid), **info}
            self.lights_by_name = by_name
            return by_name

    # changed: every light command reconnects first and uses direct API with timeout
    def set_light_state(
        self,
        light_name: str,
        on: Optional[bool] = None,
        bri: Optional[int] = None,
        xy: Optional[Tuple[float, float]] = None,
        ct: Optional[int] = None,
        transitiontime: int = 4
    ) -> None:
        with self.lock:
            self.ensure_connected()
            if light_name not in self.lights_by_name:
                self.refresh_lights()
            if light_name not in self.lights_by_name:
                raise RuntimeError(f"Unknown light: {light_name}")
            lid = self.lights_by_name[light_name]["id"]
            cmd = {"transitiontime": int(max(0, transitiontime))}
            if on is not None:
                cmd["on"] = bool(on)
            if bri is not None:
                cmd["bri"] = int(clamp(int(bri), 0, 254))
            if ct is not None:
                cmd["ct"] = int(clamp(int(ct), 153, 500))
            if xy is not None:
                cmd["xy"] = [float(clamp(xy[0], 0.0, 1.0)), float(clamp(xy[1], 0.0, 1.0))]
            self._request_json("PUT", f"/lights/{lid}/state", json=cmd)

    # changed: status lookup reconnects first and uses direct API with timeout
    def get_light_status(self, light_name: str) -> dict:
        with self.lock:
            self.ensure_connected()
            if light_name not in self.lights_by_name:
                self.refresh_lights()
            if light_name not in self.lights_by_name:
                raise RuntimeError(f"Unknown light: {light_name}")
            lid = self.lights_by_name[light_name]["id"]
            return self._request_json("GET", f"/lights/{lid}")


class CalendarPopup(tk.Toplevel):
    def __init__(self, parent: tk.Widget, initial: Optional[date] = None):
        super().__init__(parent)
        self.title("Select date")
        self.configure(bg="#111316")
        self.resizable(False, False)
        self.transient(parent.winfo_toplevel())
        self.grab_set()
        self.selected: Optional[date] = None
        self.current = initial or datetime.now().date()
        self._build()

    def _build(self):
        top = tk.Frame(self, bg="#111316")
        top.pack(fill=tk.X, padx=10, pady=10)

        self.month_lbl = tk.Label(top, text="", fg="#EAEAEA", bg="#111316", font=("Cascadia Code", 11, "bold"))
        self.month_lbl.pack(side=tk.LEFT)

        btns = tk.Frame(top, bg="#111316")
        btns.pack(side=tk.RIGHT)

        tk.Button(btns, text="◀", command=self._prev, bg="#1A1F24", fg="#EAEAEA", relief=tk.FLAT, width=3).pack(side=tk.LEFT, padx=4)
        tk.Button(btns, text="▶", command=self._next, bg="#1A1F24", fg="#EAEAEA", relief=tk.FLAT, width=3).pack(side=tk.LEFT, padx=4)

        self.grid_frm = tk.Frame(self, bg="#111316")
        self.grid_frm.pack(padx=10, pady=(0, 10))

        self._render()

    def _prev(self):
        y, m = self.current.year, self.current.month
        if m == 1:
            y -= 1
            m = 12
        else:
            m -= 1
        self.current = date(y, m, 1)
        self._render()

    def _next(self):
        y, m = self.current.year, self.current.month
        if m == 12:
            y += 1
            m = 1
        else:
            m += 1
        self.current = date(y, m, 1)
        self._render()

    def _render(self):
        for w in self.grid_frm.winfo_children():
            w.destroy()

        y, m = self.current.year, self.current.month
        first = date(y, m, 1)
        start_wd = first.weekday()
        if m == 12:
            next_m = date(y + 1, 1, 1)
        else:
            next_m = date(y, m + 1, 1)
        days_in_month = (next_m - first).days

        self.month_lbl.config(text=first.strftime("%B %Y"))

        hdr = tk.Frame(self.grid_frm, bg="#111316")
        hdr.pack(fill=tk.X)
        for i, wd in enumerate(WEEKDAYS):
            tk.Label(hdr, text=wd, fg="#9BB8FF", bg="#111316", width=4, font=("Cascadia Code", 9)).grid(row=0, column=i, padx=2, pady=2)

        body = tk.Frame(self.grid_frm, bg="#111316")
        body.pack()

        r = 0
        c = 0
        for _ in range(start_wd):
            tk.Label(body, text=" ", bg="#111316", width=4).grid(row=r, column=c, padx=2, pady=2)
            c += 1

        today = datetime.now().date()

        for day in range(1, days_in_month + 1):
            d = date(y, m, day)
            is_today = (d == today)
            bg = "#1A1F24" if not is_today else "#203A5E"
            fg = "#EAEAEA" if not is_today else "#FFFFFF"
            b = tk.Button(body, text=str(day), width=4, bg=bg, fg=fg, relief=tk.FLAT, command=lambda dd=d: self._choose(dd))
            b.grid(row=r, column=c, padx=2, pady=2)
            c += 1
            if c >= 7:
                c = 0
                r += 1

        bottom = tk.Frame(self, bg="#111316")
        bottom.pack(fill=tk.X, padx=10, pady=(0, 10))
        tk.Button(bottom, text="Cancel", command=self._cancel, bg="#1A1F24", fg="#EAEAEA", relief=tk.FLAT).pack(side=tk.RIGHT)

    def _choose(self, d: date):
        self.selected = d
        self.destroy()

    def _cancel(self):
        self.selected = None
        self.destroy()


class LightBoxGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("lighthouse 💡")
        self.root.geometry("1150x780")
        self.root.minsize(1020, 700)

        self.hue = HueController()
        self.connected = tk.BooleanVar(value=False)

        self.boxes_count = tk.IntVar(value=8)
        self.box_assignments: Dict[str, tk.StringVar] = {}

        self.adv_schedules: List[AdvancedSchedule] = []
        # changed: RLock protects schedule retry state used by scheduler thread and GUI callbacks
        self.schedules_lock = threading.RLock()

        self.box_schedule_labels: Dict[str, ttk.Label] = {}

        self.scheduler_running = True
        self.scheduler_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        # changed: tracks heartbeat failures without marking the GUI disconnected after one temporary timeout
        self._heartbeat_fail_count = 0

        self._style_dark()
        self._build_ui()
        self.scheduler_thread.start()
        # changed: heartbeat keeps checking Bridge API health during long unattended runs
        self.root.after(300000, self._bridge_heartbeat)

    def _style_dark(self):
        self.root.configure(bg="#111316")
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure(".", background="#111316", foreground="#EAEAEA", fieldbackground="#2B2F33")
        style.configure("TFrame", background="#111316")
        style.configure("TLabel", background="#111316", foreground="#EAEAEA")
        style.configure("TButton", padding=6, background="#2B2F33", foreground="#EAEAEA")
        style.map("TButton", background=[("active", "#3A4149"), ("pressed", "#203A5E")], foreground=[("active", "#FFFFFF"), ("pressed", "#FFFFFF")])
        style.configure("TLabelframe", background="#111316", foreground="#EAEAEA")
        style.configure("TLabelframe.Label", background="#111316", foreground="#EAEAEA")

        style.configure("TNotebook", background="#111316", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(12, 8))
        style.map("TNotebook.Tab", background=[("selected", "#1A1F24")], foreground=[("selected", "#FFFFFF")])

        style.configure("TEntry", foreground="#09E714", fieldbackground="#1A1F24")
        style.configure("TCombobox", foreground="#09E714", fieldbackground="#1A1F24")
        style.map("TCombobox", fieldbackground=[("readonly", "#1A1F24")], foreground=[("readonly", "#09E714")])

        style.configure("Treeview", background="#0E1012", fieldbackground="#0E1012", foreground="#09E714", rowheight=26, borderwidth=0)
        style.configure("Treeview.Heading", background="#111316", foreground="#EAEAEA", relief=tk.FLAT)
        style.map("Treeview", background=[("selected", "#203A5E")], foreground=[("selected", "#FFFFFF")])

        self.root.option_add("*TCombobox*Listbox*Background", "#0E1012")
        self.root.option_add("*TCombobox*Listbox*Foreground", "#09E714")
        self.root.option_add("*TCombobox*Listbox*selectBackground", "#203A5E")
        self.root.option_add("*TCombobox*Listbox*selectForeground", "#FFFFFF")

    def _state_summary(self, state: LightState) -> str:
        parts = [state.action]
        if state.bri is not None:
            parts.append(f"bri={state.bri}")
        if state.preset:
            parts.append(f"preset={state.preset}")
        if state.ct is not None:
            parts.append(f"ct={state.ct}")
        if state.xy is not None:
            parts.append(f"xy=({state.xy[0]:.3f},{state.xy[1]:.3f})")
        if state.wavelength_nm is not None:
            parts.append(f"wl={state.wavelength_nm:g}nm")
        return " | ".join(parts)

    def _build_ui(self):
        top = ttk.Frame(self.root)
        top.pack(side=tk.TOP, fill=tk.X, padx=12, pady=10)

        ttk.Label(top, text="Bridge IP:").pack(side=tk.LEFT)
        self.ip_entry = ttk.Entry(top, width=18)
        self.ip_entry.pack(side=tk.LEFT, padx=(8, 10))

        ttk.Button(top, text="Discover (online)", command=self._discover_ip).pack(side=tk.LEFT, padx=5)
        ttk.Button(top, text="Connect / Register", command=self._connect_bridge).pack(side=tk.LEFT, padx=5)
        ttk.Button(top, text="Refresh lights", command=self._refresh_lights).pack(side=tk.LEFT, padx=5)

        ttk.Separator(self.root).pack(fill=tk.X, padx=12, pady=(0, 8))

        mid = ttk.Frame(self.root)
        mid.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=12)

        left = ttk.Frame(mid)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))

        ttk.Label(left, text="Project", font=("Cascadia Code", 11, "bold")).pack(anchor="w", pady=(0, 6))
        ttk.Button(left, text="Load config JSON", command=self._load_config).pack(fill=tk.X, pady=3)
        ttk.Button(left, text="Save config JSON", command=self._save_config).pack(fill=tk.X, pady=3)
        ttk.Button(left, text="Load schedules CSV", command=self._load_schedules_csv).pack(fill=tk.X, pady=(10, 3))
        ttk.Button(left, text="Export schedules CSV", command=self._export_schedules_csv).pack(fill=tk.X, pady=3)
        ttk.Button(left, text="Clear schedules", command=self._clear_schedules).pack(fill=tk.X, pady=3)

        ttk.Separator(left).pack(fill=tk.X, pady=12)

        ttk.Label(left, text="Boxes", font=("Cascadia Code", 11, "bold")).pack(anchor="w", pady=(0, 6))
        box_row = ttk.Frame(left)
        box_row.pack(fill=tk.X)
        ttk.Label(box_row, text="Count:").pack(side=tk.LEFT)
        self.box_count_spin = ttk.Spinbox(box_row, from_=1, to=32, textvariable=self.boxes_count, width=5)
        self.box_count_spin.pack(side=tk.LEFT, padx=8)
        ttk.Button(box_row, text="Rebuild tabs", command=self._rebuild_tabs).pack(side=tk.LEFT)

        ttk.Separator(left).pack(fill=tk.X, pady=12)

        ttk.Label(left, text="Status", font=("Cascadia Code", 11, "bold")).pack(anchor="w", pady=(0, 6))
        self.connection_label = ttk.Label(left, text="Bridge: Not connected")
        self.connection_label.pack(anchor="w")
        self.status_label = ttk.Label(left, text="Last event: —")
        self.status_label.pack(anchor="w", pady=(2, 0))

        self.log_text = tk.Text(left, height=18, width=36, bg="#0E1012", fg="#09E714", insertbackground="#CFE6FF", relief=tk.FLAT)
        self.log_text.pack(fill=tk.BOTH, expand=False, pady=(10, 0))
        self._log("Ready. Enter Bridge IP → press Bridge button → Connect/Register.")

        right = ttk.Frame(mid)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.nb = ttk.Notebook(right)
        self.nb.pack(fill=tk.BOTH, expand=True)

        self.overview_tab = ttk.Frame(self.nb)
        self.nb.add(self.overview_tab, text="Overview")
        self._build_overview_tab()

        self.scheduler_tab = ttk.Frame(self.nb)
        self.nb.add(self.scheduler_tab, text="Scheduler")
        self._build_scheduler_tab()

        self.box_tabs: Dict[str, ttk.Frame] = {}
        self._rebuild_tabs()

        bottom = tk.Frame(self.root, bg="#111316", highlightthickness=0, bd=0)
        bottom.pack(side=tk.BOTTOM, fill=tk.X)

        footer = tk.Label(bottom, text="", font=("Cascadia Code", 10), fg="royalblue", bg="#111316")
        footer.pack(pady=(6, 0))

        dev = tk.Label(
            bottom,
            text="Developed by Hamid Taghipourbibalan",
            font=("Cascadia Code", 8, "italic"),
            cursor="hand2",
            fg="#9BB8FF",
            bg="#111316"
        )
        dev.pack(pady=(2, 6))
        dev.bind("<Button-1>", lambda e: self._open_link("https://www.linkedin.com/in/hamid-taghipourbibalan-b7239088/"))

    def _set_status(self, msg: str, when: Optional[datetime] = None):
        ts = (when or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
        self.status_label.config(text=f"Last event: [{ts}] {msg}")

    def _build_overview_tab(self):
        frm = self.overview_tab

        header = ttk.Frame(frm)
        header.pack(fill=tk.X, padx=10, pady=10)

        ttk.Label(header, text="All lights overview", font=("Cascadia Code", 12, "bold")).pack(side=tk.LEFT)
        ttk.Button(header, text="Refresh", command=self._refresh_lights).pack(side=tk.RIGHT)

        self.overview_table = ttk.Treeview(frm, columns=("name", "on", "bri", "mode", "ct", "xy"), show="headings", height=14)
        for col, w, a in [("name", 260, "w"), ("on", 60, "center"), ("bri", 70, "center"), ("mode", 90, "center"), ("ct", 70, "center"), ("xy", 180, "center")]:
            self.overview_table.heading(col, text=col.upper())
            self.overview_table.column(col, width=w, anchor=a)
        self.overview_table.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        self.overview_table.bind("<<TreeviewSelect>>", self._overview_pick_from_table)
        self.overview_table.bind("<Double-1>", self._overview_pick_from_table)

        control = ttk.LabelFrame(frm, text="Quick control (selected light)")
        control.pack(fill=tk.X, padx=10, pady=(0, 10))

        row = ttk.Frame(control)
        row.pack(fill=tk.X, padx=10, pady=8)

        ttk.Label(row, text="Light:").pack(side=tk.LEFT)
        self.ov_selected_light = tk.StringVar(value="")
        self.ov_light_combo = ttk.Combobox(row, textvariable=self.ov_selected_light, width=30, state="readonly")
        self.ov_light_combo.pack(side=tk.LEFT, padx=8)

        ttk.Button(row, text="ON", command=lambda: self._overview_onoff(True)).pack(side=tk.LEFT, padx=4)
        ttk.Button(row, text="OFF", command=lambda: self._overview_onoff(False)).pack(side=tk.LEFT, padx=4)

        ttk.Label(row, text="Brightness:").pack(side=tk.LEFT, padx=(18, 6))

        self.ov_bri = tk.IntVar(value=160)
        self.ov_bri_entry = tk.StringVar(value="160")

        s = ttk.Scale(row, from_=0, to=254, variable=self.ov_bri, command=lambda _=None: self._sync_bri_entry_from_scale(self.ov_bri, self.ov_bri_entry))
        s.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        e = ttk.Entry(row, textvariable=self.ov_bri_entry, width=6)
        e.pack(side=tk.LEFT, padx=(0, 8))
        e.bind("<FocusOut>", lambda ev: self._sync_bri_scale_from_entry(self.ov_bri, self.ov_bri_entry))
        e.bind("<Return>", lambda ev: self._sync_bri_scale_from_entry(self.ov_bri, self.ov_bri_entry))

        ttk.Button(row, text="Apply", command=self._overview_apply_bri).pack(side=tk.LEFT, padx=8)

        row2 = ttk.Frame(control)
        row2.pack(fill=tk.X, padx=10, pady=(0, 10))

        ttk.Label(row2, text="Preset:").pack(side=tk.LEFT)
        self.ov_preset = tk.StringVar(value="white")
        ttk.Combobox(row2, textvariable=self.ov_preset, values=list(PRESET_XY.keys()), width=10, state="readonly").pack(side=tk.LEFT, padx=8)
        ttk.Button(row2, text="Apply preset", command=self._overview_apply_preset).pack(side=tk.LEFT, padx=4)

        ttk.Label(row2, text="CT (153-500):").pack(side=tk.LEFT, padx=(18, 6))
        self.ov_ct = tk.IntVar(value=370)
        ttk.Entry(row2, textvariable=self.ov_ct, width=6).pack(side=tk.LEFT)
        ttk.Button(row2, text="Apply CT", command=self._overview_apply_ct).pack(side=tk.LEFT, padx=6)

        ttk.Label(row2, text="Wavelength (nm):").pack(side=tk.LEFT, padx=(18, 6))
        self.ov_wl = tk.IntVar(value=470)
        ttk.Entry(row2, textvariable=self.ov_wl, width=6).pack(side=tk.LEFT)
        ttk.Button(row2, text="Apply WL→XY", command=self._overview_apply_wavelength).pack(side=tk.LEFT, padx=6)

    def _overview_pick_from_table(self, event=None):
        sel = self.overview_table.selection()
        if not sel:
            return
        vals = self.overview_table.item(sel[0], "values")
        if not vals:
            return
        name = str(vals[0]).strip()
        if not name:
            return
        self.ov_selected_light.set(name)
        try:
            st = self.hue.get_light_status(name).get("state", {})
            bri = int(st.get("bri", 160))
            self.ov_bri.set(bri)
            self.ov_bri_entry.set(str(bri))
        except Exception:
            pass

    def _build_scheduler_tab(self):
        frm = self.scheduler_tab

        top = ttk.Frame(frm)
        top.pack(fill=tk.X, padx=10, pady=10)

        ttk.Label(top, text="Advanced Scheduler", font=("Cascadia Code", 12, "bold")).pack(side=tk.LEFT)

        self.scheduler_table = ttk.Treeview(
            frm,
            columns=("enabled", "name", "boxes", "start", "end", "rec", "until", "start_action", "end_action"),
            show="headings",
            height=10
        )
        for col, w, a in [
            ("enabled", 70, "center"),
            ("name", 140, "w"),
            ("boxes", 220, "w"),
            ("start", 150, "center"),
            ("end", 150, "center"),
            ("rec", 90, "center"),
            ("until", 110, "center"),
            ("start_action", 110, "center"),
            ("end_action", 110, "center"),
        ]:
            self.scheduler_table.heading(col, text=col.upper())
            self.scheduler_table.column(col, width=w, anchor=a)
        self.scheduler_table.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        form = ttk.LabelFrame(frm, text="Create schedule")
        form.pack(fill=tk.X, padx=10, pady=(0, 10))

        r0 = ttk.Frame(form)
        r0.pack(fill=tk.X, padx=10, pady=(10, 6))

        ttk.Label(r0, text="Name:").pack(side=tk.LEFT)
        self.sc_name = tk.StringVar(value="Experiment block")
        ttk.Entry(r0, textvariable=self.sc_name, width=24).pack(side=tk.LEFT, padx=(8, 14))

        ttk.Label(r0, text="Apply to:").pack(side=tk.LEFT)
        ttk.Button(r0, text="Select boxes…", command=self._popup_select_boxes).pack(side=tk.LEFT, padx=8)
        self.sc_boxes_label = ttk.Label(r0, text="(none)")
        self.sc_boxes_label.pack(side=tk.LEFT, padx=(0, 10))

        ttk.Button(r0, text="Use ALL boxes", command=self._use_all_boxes).pack(side=tk.RIGHT)

        r1 = ttk.Frame(form)
        r1.pack(fill=tk.X, padx=10, pady=6)

        ttk.Label(r1, text="Start date:").pack(side=tk.LEFT)
        self.sc_start_date = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d"))
        ttk.Entry(r1, textvariable=self.sc_start_date, width=12).pack(side=tk.LEFT, padx=(8, 4))
        ttk.Button(r1, text="📅", command=lambda: self._pick_date_into(self.sc_start_date)).pack(side=tk.LEFT, padx=(0, 14))

        ttk.Label(r1, text="Start time (HH:MM):").pack(side=tk.LEFT)
        self.sc_start_time = tk.StringVar(value="08:00")
        ttk.Entry(r1, textvariable=self.sc_start_time, width=7).pack(side=tk.LEFT, padx=(8, 14))

        ttk.Label(r1, text="End date:").pack(side=tk.LEFT)
        self.sc_end_date = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d"))
        ttk.Entry(r1, textvariable=self.sc_end_date, width=12).pack(side=tk.LEFT, padx=(8, 4))
        ttk.Button(r1, text="📅", command=lambda: self._pick_date_into(self.sc_end_date)).pack(side=tk.LEFT, padx=(0, 14))

        ttk.Label(r1, text="End time (HH:MM):").pack(side=tk.LEFT)
        self.sc_end_time = tk.StringVar(value="18:00")
        ttk.Entry(r1, textvariable=self.sc_end_time, width=7).pack(side=tk.LEFT, padx=(8, 0))

        r2 = ttk.Frame(form)
        r2.pack(fill=tk.X, padx=10, pady=(6, 2))

        ttk.Label(r2, text="Recurrence:").pack(side=tk.LEFT)
        self.sc_recurrence = tk.StringVar(value="once")
        ttk.Combobox(r2, textvariable=self.sc_recurrence, values=["once", "daily", "weekly"], width=8, state="readonly").pack(side=tk.LEFT, padx=(8, 14))

        ttk.Label(r2, text="Every N:").pack(side=tk.LEFT)
        self.sc_every_n = tk.IntVar(value=1)
        ttk.Spinbox(r2, from_=1, to=30, textvariable=self.sc_every_n, width=5).pack(side=tk.LEFT, padx=(8, 14))

        ttk.Label(r2, text="Until date:").pack(side=tk.LEFT)
        self.sc_until = tk.StringVar(value="")
        ttk.Entry(r2, textvariable=self.sc_until, width=12).pack(side=tk.LEFT, padx=(8, 4))
        ttk.Button(r2, text="📅", command=lambda: self._pick_date_into(self.sc_until)).pack(side=tk.LEFT, padx=(0, 14))

        ttk.Label(r2, text="Weekly days:").pack(side=tk.LEFT)
        self.sc_weekday_vars = [tk.BooleanVar(value=(i < 5)) for i in range(7)]
        wd_frm = ttk.Frame(r2)
        wd_frm.pack(side=tk.LEFT, padx=(8, 0))
        for i, wd in enumerate(WEEKDAYS):
            ttk.Checkbutton(wd_frm, text=wd, variable=self.sc_weekday_vars[i]).pack(side=tk.LEFT)

        r3 = ttk.Frame(form)
        r3.pack(fill=tk.X, padx=10, pady=(8, 6))

        start_box = ttk.LabelFrame(r3, text="Start state (applied at START)")
        start_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10))

        end_box = ttk.LabelFrame(r3, text="End state (applied at END)")
        end_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._build_state_editor(start_box, prefix="start")
        self._build_state_editor(end_box, prefix="end", default_action="off")

        r4 = ttk.Frame(form)
        r4.pack(fill=tk.X, padx=10, pady=(4, 10))

        ttk.Button(r4, text="Add schedule", command=self._add_advanced_schedule).pack(side=tk.LEFT)
        ttk.Button(r4, text="Remove selected", command=self._remove_selected_advanced_schedule).pack(side=tk.LEFT, padx=8)
        ttk.Button(r4, text="Toggle enable", command=self._toggle_enable_selected).pack(side=tk.LEFT, padx=8)
        ttk.Button(r4, text="Apply START now (selected)", command=lambda: self._apply_selected_schedule_boundary("start")).pack(side=tk.RIGHT, padx=6)
        ttk.Button(r4, text="Apply END now (selected)", command=lambda: self._apply_selected_schedule_boundary("end")).pack(side=tk.RIGHT)

        self._scheduler_refresh_table()

        self.selected_boxes: List[str] = []

    def _build_state_editor(self, parent: ttk.LabelFrame, prefix: str, default_action: str = "set"):
        row1 = ttk.Frame(parent)
        row1.pack(fill=tk.X, padx=10, pady=(10, 6))

        ttk.Label(row1, text="Action:").pack(side=tk.LEFT)
        var_action = tk.StringVar(value=default_action)
        setattr(self, f"sc_{prefix}_action", var_action)
        ttk.Combobox(row1, textvariable=var_action, values=["set", "on", "off"], width=6, state="readonly").pack(side=tk.LEFT, padx=(8, 14))

        ttk.Label(row1, text="Brightness:").pack(side=tk.LEFT)
        var_bri = tk.IntVar(value=160)
        var_bri_entry = tk.StringVar(value="160")
        setattr(self, f"sc_{prefix}_bri", var_bri)
        setattr(self, f"sc_{prefix}_bri_entry", var_bri_entry)

        scale = ttk.Scale(row1, from_=0, to=254, variable=var_bri, command=lambda _=None, v=var_bri, e=var_bri_entry: self._sync_bri_entry_from_scale(v, e))
        scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 8))

        entry = ttk.Entry(row1, textvariable=var_bri_entry, width=6)
        entry.pack(side=tk.LEFT)
        entry.bind("<FocusOut>", lambda ev, v=var_bri, e=var_bri_entry: self._sync_bri_scale_from_entry(v, e))
        entry.bind("<Return>", lambda ev, v=var_bri, e=var_bri_entry: self._sync_bri_scale_from_entry(v, e))

        row2 = ttk.Frame(parent)
        row2.pack(fill=tk.X, padx=10, pady=(0, 10))

        ttk.Label(row2, text="Preset:").pack(side=tk.LEFT)
        var_preset = tk.StringVar(value="white")
        setattr(self, f"sc_{prefix}_preset", var_preset)
        ttk.Combobox(row2, textvariable=var_preset, values=[""] + list(PRESET_XY.keys()), width=10, state="readonly").pack(side=tk.LEFT, padx=(8, 14))

        ttk.Label(row2, text="CT:").pack(side=tk.LEFT)
        var_ct = tk.StringVar(value="")
        setattr(self, f"sc_{prefix}_ct", var_ct)
        ttk.Entry(row2, textvariable=var_ct, width=7).pack(side=tk.LEFT, padx=(8, 14))

        ttk.Label(row2, text="XY (x,y):").pack(side=tk.LEFT)
        var_xy = tk.StringVar(value="")
        setattr(self, f"sc_{prefix}_xy", var_xy)
        ttk.Entry(row2, textvariable=var_xy, width=16).pack(side=tk.LEFT, padx=(8, 14))

        ttk.Label(row2, text="Wavelength:").pack(side=tk.LEFT)
        var_wl = tk.StringVar(value="")
        setattr(self, f"sc_{prefix}_wl", var_wl)
        ttk.Entry(row2, textvariable=var_wl, width=7).pack(side=tk.LEFT, padx=(8, 0))

    def _sync_bri_entry_from_scale(self, bri_var: tk.IntVar, entry_var: tk.StringVar):
        try:
            entry_var.set(str(int(bri_var.get())))
        except Exception:
            pass

    def _sync_bri_scale_from_entry(self, bri_var: tk.IntVar, entry_var: tk.StringVar):
        s = (entry_var.get() or "").strip()
        try:
            v = int(float(s))
        except Exception:
            v = int(bri_var.get())
        v = int(clamp(v, 0, 254))
        bri_var.set(v)
        entry_var.set(str(v))

    def _log(self, msg: str, when: Optional[datetime] = None):
        ts = (when or datetime.now()).strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{ts}] {msg}\n")
        self.log_text.see("end")
        self._set_status(msg, when=when)

    # changed: lets worker threads log safely through Tkinter's main thread
    def _threadsafe_log(self, msg: str, when: Optional[datetime] = None):
        try:
            self.root.after(0, lambda m=msg, w=when: self._log(m, when=w))
        except Exception:
            pass

    # changed: heartbeat runs in a worker thread so the GUI does not freeze if the Bridge times out
    def _bridge_heartbeat(self):
        def worker():
            if self.connected.get():
                try:
                    self.hue.ensure_connected()
                    def ok():
                        if self._heartbeat_fail_count > 0:
                            self._log("Bridge heartbeat recovered.")
                        self._heartbeat_fail_count = 0
                        self.connection_label.config(text=f"Bridge: Connected to {self.hue.bridge_ip}")
                    self.root.after(0, ok)
                except Exception as e:
                    def failed(msg=str(e)):
                        self._heartbeat_fail_count += 1
                        self._log(f"Bridge heartbeat failed ({self._heartbeat_fail_count}): {msg}")
                    self.root.after(0, failed)

        threading.Thread(target=worker, daemon=True).start()
        if self.scheduler_running:
            self.root.after(300000, self._bridge_heartbeat)

    def _open_link(self, url: str):
        import webbrowser
        webbrowser.open(url)

    def _discover_ip(self):
        ip = self.hue.discover_bridge_ip_online()
        if ip:
            self.ip_entry.delete(0, "end")
            self.ip_entry.insert(0, ip)
            self._log(f"Discovered Bridge IP: {ip}")
        else:
            messagebox.showwarning("Discovery failed", "Could not discover Bridge IP (needs internet). Use router DHCP list instead.")

    def _connect_bridge(self):
        ip = self.ip_entry.get().strip()
        if not ip:
            messagebox.showerror("Missing IP", "Please enter the Hue Bridge IP address.")
            return
        try:
            self._log("Connecting… If first time, press the physical Bridge button now.")
            self.hue.connect(ip)
            self.connected.set(True)
            self.connection_label.config(text=f"Bridge: Connected to {ip}")
            self._heartbeat_fail_count = 0
            self._log("Connected & registered.")
            self._refresh_lights()
            self._sync_current_schedule_outputs(reason="Bridge connect")
        except Exception as e:
            self.connected.set(False)
            self.connection_label.config(text="Bridge: Not connected")
            messagebox.showerror("Connect failed", f"{e}\n\nTip: Press the Bridge button and try again.")
            self._log(f"Connect failed: {e}")

    # changed: show_errors=False lets scheduler refresh silently after unattended events
    def _refresh_lights(self, show_errors: bool = True):
        if not self.connected.get():
            if show_errors:
                messagebox.showinfo("Not connected", "Connect to the Bridge first.")
            return
        try:
            lights = self.hue.refresh_lights()
            names = sorted(lights.keys())
            self._log(f"Found {len(names)} lights.")

            self.ov_light_combo["values"] = names
            if names and not self.ov_selected_light.get():
                self.ov_selected_light.set(names[0])

            for item in self.overview_table.get_children():
                self.overview_table.delete(item)

            for name in names:
                st = lights[name].get("state", {})
                on = st.get("on", False)
                bri = st.get("bri", "")
                mode = st.get("colormode", "")
                ct = st.get("ct", "")
                xy = st.get("xy", "")
                self.overview_table.insert("", "end", values=(name, str(on), str(bri), str(mode), str(ct), str(xy)))

            for box, var in self.box_assignments.items():
                tab = self.box_tabs.get(box)
                if tab is not None:
                    cb: ttk.Combobox = tab._light_combo
                    cb["values"] = ["(none)"] + names
                    if var.get() and var.get() not in cb["values"]:
                        var.set("(none)")

        except Exception as e:
            if show_errors:
                messagebox.showerror("Refresh failed", str(e))
            self._log(f"Refresh failed: {e}")

    def _get_selected_overview_light(self) -> Optional[str]:
        try:
            sel = self.overview_table.selection()
            if sel:
                vals = self.overview_table.item(sel[0], "values")
                if vals and str(vals[0]).strip():
                    return str(vals[0]).strip()
        except Exception:
            pass
        name = self.ov_selected_light.get().strip()
        return name if name else None

    def _overview_onoff(self, on: bool):
        name = self._get_selected_overview_light()
        if not name:
            return
        try:
            self.hue.set_light_state(name, on=on)
            self._log(f"{name}: on={on}")
            self._refresh_lights()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _overview_apply_bri(self):
        name = self._get_selected_overview_light()
        if not name:
            return
        self._sync_bri_scale_from_entry(self.ov_bri, self.ov_bri_entry)
        try:
            self.hue.set_light_state(name, bri=int(self.ov_bri.get()))
            self._log(f"{name}: bri={int(self.ov_bri.get())}")
            self._refresh_lights()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _overview_apply_preset(self):
        name = self._get_selected_overview_light()
        if not name:
            return
        self._sync_bri_scale_from_entry(self.ov_bri, self.ov_bri_entry)
        preset = self.ov_preset.get().strip().lower()
        xy = PRESET_XY.get(preset, PRESET_XY["white"])
        try:
            self.hue.set_light_state(name, on=True, xy=xy, bri=int(self.ov_bri.get()))
            self._log(f"{name}: preset={preset} xy={xy} bri={int(self.ov_bri.get())}")
            self._refresh_lights()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _overview_apply_ct(self):
        name = self._get_selected_overview_light()
        if not name:
            return
        self._sync_bri_scale_from_entry(self.ov_bri, self.ov_bri_entry)
        try:
            ct = int(self.ov_ct.get())
            self.hue.set_light_state(name, on=True, ct=ct, bri=int(self.ov_bri.get()))
            self._log(f"{name}: ct={ct} bri={int(self.ov_bri.get())}")
            self._refresh_lights()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _overview_apply_wavelength(self):
        name = self._get_selected_overview_light()
        if not name:
            return
        self._sync_bri_scale_from_entry(self.ov_bri, self.ov_bri_entry)
        try:
            wl = float(self.ov_wl.get())
            xy = wavelength_to_xy_nm(wl)
            self.hue.set_light_state(name, on=True, xy=xy, bri=int(self.ov_bri.get()))
            self._log(f"{name}: wavelength={wl}nm → xy={xy} bri={int(self.ov_bri.get())}")
            self._refresh_lights()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _rebuild_tabs(self):
        for box, tab in list(self.box_tabs.items()):
            try:
                self.nb.forget(tab)
            except Exception:
                pass
        self.box_tabs.clear()
        self.box_schedule_labels.clear()
        self.box_assignments.clear()

        n = int(self.boxes_count.get())
        for i in range(1, n + 1):
            box_name = f"Box {i}"
            tab = ttk.Frame(self.nb)
            self.nb.add(tab, text=box_name)
            self.box_tabs[box_name] = tab
            self.box_assignments[box_name] = tk.StringVar(value="(none)")
            self._build_box_tab(tab, box_name)

        self._log(f"Built {n} box tabs.")
        self._sync_scheduler_boxes_label()
        if self.connected.get():
            self._refresh_lights()
        self._refresh_box_schedule_displays()

    def _build_box_tab(self, tab: ttk.Frame, box_name: str):
        header = ttk.Frame(tab)
        header.pack(fill=tk.X, padx=10, pady=10)

        ttk.Label(header, text=box_name, font=("Cascadia Code", 12, "bold")).pack(side=tk.LEFT)

        assign = ttk.LabelFrame(tab, text="Assigned light")
        assign.pack(fill=tk.X, padx=10, pady=(0, 10))

        row = ttk.Frame(assign)
        row.pack(fill=tk.X, padx=10, pady=10)

        ttk.Label(row, text="Light:").pack(side=tk.LEFT)
        var = self.box_assignments[box_name]
        cb = ttk.Combobox(row, textvariable=var, values=["(none)"], width=30, state="readonly")
        cb.pack(side=tk.LEFT, padx=10)
        tab._light_combo = cb

        ttk.Button(row, text="ON", command=lambda: self._box_onoff(box_name, True)).pack(side=tk.LEFT, padx=4)
        ttk.Button(row, text="OFF", command=lambda: self._box_onoff(box_name, False)).pack(side=tk.LEFT, padx=4)

        control = ttk.LabelFrame(tab, text="Controls")
        control.pack(fill=tk.X, padx=10, pady=(0, 10))

        r1 = ttk.Frame(control)
        r1.pack(fill=tk.X, padx=10, pady=10)

        ttk.Label(r1, text="Brightness:").pack(side=tk.LEFT)
        bri_var = tk.IntVar(value=160)
        bri_entry = tk.StringVar(value="160")

        s = ttk.Scale(r1, from_=0, to=254, variable=bri_var, command=lambda _=None, v=bri_var, e=bri_entry: self._sync_bri_entry_from_scale(v, e))
        s.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(10, 8))

        e = ttk.Entry(r1, textvariable=bri_entry, width=6)
        e.pack(side=tk.LEFT, padx=(0, 8))
        e.bind("<FocusOut>", lambda ev, v=bri_var, ent=bri_entry: self._sync_bri_scale_from_entry(v, ent))
        e.bind("<Return>", lambda ev, v=bri_var, ent=bri_entry: self._sync_bri_scale_from_entry(v, ent))

        ttk.Button(r1, text="Apply", command=lambda: self._box_apply_bri(box_name, bri_var.get())).pack(side=tk.LEFT)

        r2 = ttk.Frame(control)
        r2.pack(fill=tk.X, padx=10, pady=(0, 10))

        ttk.Label(r2, text="Preset:").pack(side=tk.LEFT)
        preset_var = tk.StringVar(value="white")
        ttk.Combobox(r2, textvariable=preset_var, values=list(PRESET_XY.keys()), width=10, state="readonly").pack(side=tk.LEFT, padx=8)
        ttk.Button(r2, text="Apply preset", command=lambda: self._box_apply_preset(box_name, preset_var.get(), bri_var.get())).pack(side=tk.LEFT, padx=6)

        ttk.Label(r2, text="CT (153–500):").pack(side=tk.LEFT, padx=(18, 6))
        ct_var = tk.IntVar(value=370)
        ttk.Entry(r2, textvariable=ct_var, width=6).pack(side=tk.LEFT)
        ttk.Button(r2, text="Apply CT", command=lambda: self._box_apply_ct(box_name, ct_var.get(), bri_var.get())).pack(side=tk.LEFT, padx=6)

        ttk.Label(r2, text="Wavelength (nm):").pack(side=tk.LEFT, padx=(18, 6))
        wl_var = tk.IntVar(value=470)
        ttk.Entry(r2, textvariable=wl_var, width=6).pack(side=tk.LEFT)
        ttk.Button(r2, text="Apply WL→XY", command=lambda: self._box_apply_wl(box_name, wl_var.get(), bri_var.get())).pack(side=tk.LEFT, padx=6)

        hint = ttk.LabelFrame(tab, text="Active schedules for this box")
        hint.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        lbl = ttk.Label(hint, text="(no active schedules)", wraplength=280, justify="left", foreground="#BBD1FF", background="#111316")
        lbl.pack(fill=tk.X, padx=10, pady=10)
        self.box_schedule_labels[box_name] = lbl

    def _get_box_light(self, box_name: str) -> Optional[str]:
        name = self.box_assignments[box_name].get().strip()
        if not name or name == "(none)":
            return None
        return name

    def _box_onoff(self, box_name: str, on: bool):
        light = self._get_box_light(box_name)
        if not light:
            messagebox.showinfo("No light assigned", f"Assign a light to {box_name} first.")
            return
        try:
            self.hue.set_light_state(light, on=on)
            self._log(f"{box_name} → {light}: on={on}")
            self._refresh_lights()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _box_apply_bri(self, box_name: str, bri: int):
        light = self._get_box_light(box_name)
        if not light:
            messagebox.showinfo("No light assigned", f"Assign a light to {box_name} first.")
            return
        try:
            self.hue.set_light_state(light, on=True, bri=int(bri))
            self._log(f"{box_name} → {light}: bri={int(bri)}")
            self._refresh_lights()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _box_apply_preset(self, box_name: str, preset: str, bri: int):
        light = self._get_box_light(box_name)
        if not light:
            messagebox.showinfo("No light assigned", f"Assign a light to {box_name} first.")
            return
        preset = (preset or "white").strip().lower()
        xy = PRESET_XY.get(preset, PRESET_XY["white"])
        try:
            self.hue.set_light_state(light, on=True, bri=int(bri), xy=xy)
            self._log(f"{box_name} → {light}: preset={preset} xy={xy} bri={int(bri)}")
            self._refresh_lights()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _box_apply_ct(self, box_name: str, ct: int, bri: int):
        light = self._get_box_light(box_name)
        if not light:
            messagebox.showinfo("No light assigned", f"Assign a light to {box_name} first.")
            return
        try:
            self.hue.set_light_state(light, on=True, bri=int(bri), ct=int(ct))
            self._log(f"{box_name} → {light}: ct={int(ct)} bri={int(bri)}")
            self._refresh_lights()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _box_apply_wl(self, box_name: str, wl_nm: float, bri: int):
        light = self._get_box_light(box_name)
        if not light:
            messagebox.showinfo("No light assigned", f"Assign a light to {box_name} first.")
            return
        xy = wavelength_to_xy_nm(float(wl_nm))
        try:
            self.hue.set_light_state(light, on=True, bri=int(bri), xy=xy)
            self._log(f"{box_name} → {light}: wl={wl_nm}nm → xy={xy} bri={int(bri)}")
            self._refresh_lights()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _pick_date_into(self, var: tk.StringVar):
        init = parse_date(var.get()) or datetime.now().date()
        pop = CalendarPopup(self.root, initial=init)
        self.root.wait_window(pop)
        if pop.selected:
            var.set(pop.selected.strftime("%Y-%m-%d"))

    def _use_all_boxes(self):
        self.selected_boxes = sorted(self.box_tabs.keys())
        self._sync_scheduler_boxes_label()

    def _sync_scheduler_boxes_label(self):
        if not hasattr(self, "sc_boxes_label"):
            return
        if not getattr(self, "selected_boxes", []):
            self.sc_boxes_label.config(text="(none)")
        elif len(self.selected_boxes) == len(self.box_tabs):
            self.sc_boxes_label.config(text="ALL boxes")
        else:
            self.sc_boxes_label.config(text=", ".join(self.selected_boxes[:5]) + (f" (+{len(self.selected_boxes)-5})" if len(self.selected_boxes) > 5 else ""))

    def _popup_select_boxes(self):
        win = tk.Toplevel(self.root)
        win.title("Select boxes")
        win.configure(bg="#111316")
        win.transient(self.root)
        win.grab_set()
        win.resizable(False, False)

        frm = tk.Frame(win, bg="#111316")
        frm.pack(padx=12, pady=12)

        lb = tk.Listbox(frm, selectmode=tk.MULTIPLE, width=24, height=12, bg="#0E1012", fg="#BBD1FF", selectbackground="#203A5E", selectforeground="#FFFFFF", relief=tk.FLAT)
        lb.pack(side=tk.LEFT)

        boxes = sorted(self.box_tabs.keys())
        for b in boxes:
            lb.insert("end", b)

        sel_set = set(getattr(self, "selected_boxes", []))
        for i, b in enumerate(boxes):
            if b in sel_set:
                lb.selection_set(i)

        sb = ttk.Scrollbar(frm, orient="vertical", command=lb.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        lb.config(yscrollcommand=sb.set)

        btns = tk.Frame(win, bg="#111316")
        btns.pack(fill=tk.X, padx=12, pady=(0, 12))

        def select_all():
            lb.selection_set(0, "end")

        def clear_all():
            lb.selection_clear(0, "end")

        def apply():
            idxs = lb.curselection()
            self.selected_boxes = [boxes[i] for i in idxs]
            self._sync_scheduler_boxes_label()
            win.destroy()

        tk.Button(btns, text="Select all", command=select_all, bg="#1A1F24", fg="#EAEAEA", relief=tk.FLAT).pack(side=tk.LEFT)
        tk.Button(btns, text="Clear", command=clear_all, bg="#1A1F24", fg="#EAEAEA", relief=tk.FLAT).pack(side=tk.LEFT, padx=8)
        tk.Button(btns, text="Apply", command=apply, bg="#203A5E", fg="#FFFFFF", relief=tk.FLAT).pack(side=tk.RIGHT)

        self.root.wait_window(win)

    def _state_from_ui(self, prefix: str) -> LightState:
        action = getattr(self, f"sc_{prefix}_action").get().strip().lower()
        bri_var: tk.IntVar = getattr(self, f"sc_{prefix}_bri")
        bri_entry: tk.StringVar = getattr(self, f"sc_{prefix}_bri_entry")
        self._sync_bri_scale_from_entry(bri_var, bri_entry)

        bri = int(bri_var.get())
        preset = getattr(self, f"sc_{prefix}_preset").get().strip().lower()
        preset = preset if preset else None

        ct_raw = getattr(self, f"sc_{prefix}_ct").get().strip()
        ct = int(ct_raw) if ct_raw else None

        xy_raw = getattr(self, f"sc_{prefix}_xy").get().strip()
        xy = parse_xy(xy_raw) if xy_raw else None

        wl_raw = getattr(self, f"sc_{prefix}_wl").get().strip()
        wl = float(wl_raw) if wl_raw else None

        if action == "off":
            return LightState(action="off")
        if action == "on":
            return LightState(action="on", bri=bri)
        return LightState(action="set", bri=bri, preset=preset, ct=ct, xy=xy, wavelength_nm=wl)

    def _add_advanced_schedule(self):
        if not getattr(self, "selected_boxes", []):
            messagebox.showerror("Missing boxes", "Select which boxes to apply this schedule to (or use ALL boxes).")
            return

        sd = parse_date(self.sc_start_date.get())
        ed = parse_date(self.sc_end_date.get())
        if sd is None or ed is None:
            messagebox.showerror("Date error", "Start date / End date must be valid. Use YYYY-MM-DD or DD/MM/YYYY.")
            return

        if parse_time_hhmm(self.sc_start_time.get()) is None or parse_time_hhmm(self.sc_end_time.get()) is None:
            messagebox.showerror("Time error", "Start time / End time must be HH:MM.")
            return

        rec = (self.sc_recurrence.get() or "once").strip().lower()
        if rec not in ("once", "daily", "weekly"):
            messagebox.showerror("Recurrence error", "Recurrence must be once/daily/weekly.")
            return

        until = self.sc_until.get().strip()
        if until:
            ud = parse_date(until)
            if ud is None:
                messagebox.showerror("Until date error", "Until date must be valid (YYYY-MM-DD or DD/MM/YYYY).")
                return

        wds = []
        if rec == "weekly":
            wds = [i for i in range(7) if self.sc_weekday_vars[i].get()]
            if not wds:
                messagebox.showerror("Weekly error", "For weekly recurrence, select at least one weekday.")
                return

        start_state = self._state_from_ui("start")
        end_state = self._state_from_ui("end")

        name = (self.sc_name.get() or "Schedule").strip()
        sch = AdvancedSchedule(
            name=name,
            boxes=list(self.selected_boxes),
            start_date=self.sc_start_date.get().strip(),
            start_time=self.sc_start_time.get().strip(),
            end_date=self.sc_end_date.get().strip(),
            end_time=self.sc_end_time.get().strip(),
            recurrence=rec,
            every_n=int(max(1, int(self.sc_every_n.get()))),
            weekdays=wds,
            until_date=self.sc_until.get().strip(),
            start_state=start_state,
            end_state=end_state,
            enabled=True
        )

        with self.schedules_lock:
            self.adv_schedules.append(sch)
        self._log(f"Added schedule: {name} ({rec}) for {len(sch.boxes)} boxes")
        self._scheduler_refresh_table()
        self._refresh_box_schedule_displays()

    def _get_selected_schedule_index(self) -> Optional[int]:
        sel = self.scheduler_table.selection()
        if not sel:
            return None
        item = sel[0]
        try:
            idx = int(self.scheduler_table.item(item, "tags")[0])
            with self.schedules_lock:
                if 0 <= idx < len(self.adv_schedules):
                    return idx
        except Exception:
            pass
        return None

    def _remove_selected_advanced_schedule(self):
        idx = self._get_selected_schedule_index()
        if idx is None:
            return
        with self.schedules_lock:
            name = self.adv_schedules[idx].name
            self.adv_schedules.pop(idx)
        self._log(f"Removed schedule: {name}")
        self._scheduler_refresh_table()
        self._refresh_box_schedule_displays()

    def _toggle_enable_selected(self):
        idx = self._get_selected_schedule_index()
        if idx is None:
            return
        with self.schedules_lock:
            self.adv_schedules[idx].enabled = not self.adv_schedules[idx].enabled
            name = self.adv_schedules[idx].name
            enabled = self.adv_schedules[idx].enabled
        self._log(f"Schedule '{name}' enabled={enabled}")
        self._scheduler_refresh_table()
        self._refresh_box_schedule_displays()

    def _apply_selected_schedule_boundary(self, which: str):
        idx = self._get_selected_schedule_index()
        if idx is None:
            return
        with self.schedules_lock:
            sch = self.adv_schedules[idx]
            st = sch.start_state if which == "start" else sch.end_state
            boxes = list(sch.boxes)
            name = sch.name
        self._apply_state_to_boxes(st, boxes, label=f"Manual {which.upper()}: {name}")

    def _scheduler_refresh_table(self):
        for item in self.scheduler_table.get_children():
            self.scheduler_table.delete(item)

        with self.schedules_lock:
            schedules_snapshot = list(self.adv_schedules)

        for i, sch in enumerate(schedules_snapshot):
            boxes_txt = "ALL" if len(sch.boxes) == len(self.box_tabs) else (", ".join(sch.boxes[:3]) + (f" (+{len(sch.boxes)-3})" if len(sch.boxes) > 3 else ""))
            start_txt = f"{sch.start_date} {sch.start_time}"
            end_txt = f"{sch.end_date} {sch.end_time}"
            until_txt = sch.until_date if sch.until_date else ""
            sa = self._state_summary(sch.start_state)
            ea = self._state_summary(sch.end_state)
            enabled_txt = "YES" if sch.enabled else "NO"
            iid = self.scheduler_table.insert("", "end", values=(enabled_txt, sch.name, boxes_txt, start_txt, end_txt, sch.recurrence, until_txt, sa, ea), tags=(str(i),))
            if not sch.enabled:
                self.scheduler_table.item(iid, tags=(str(i), "disabled"))

    # changed: when a config is loaded or the Bridge reconnects, sync outputs to the latest schedule state instead of replaying old missed boundaries
    def _mark_past_boundaries_and_get_sync_jobs(self, schedules_snapshot: List[AdvancedSchedule], now: datetime):
        latest_by_box = {}

        for schedule_index, sch in enumerate(schedules_snapshot):
            sch._pending_start_key = None
            sch._pending_end_key = None
            sch._start_retry_after = None
            sch._end_retry_after = None

            latest_start = None
            latest_end = None

            for offset in range(-60, 15):
                w = sch.window_for_date(now.date() + timedelta(days=offset))
                if not w:
                    continue

                st, ed, sk, ek = w

                if st <= now:
                    if latest_start is None or st > latest_start[0]:
                        latest_start = (st, sk)
                    for box in sch.boxes:
                        current = latest_by_box.get(box)
                        candidate_order = (st, schedule_index, 0)
                        if current is None or candidate_order > current["order"]:
                            latest_by_box[box] = {
                                "order": candidate_order,
                                "dt": st,
                                "schedule_index": schedule_index,
                                "sch": sch,
                                "which": "start",
                                "key": sk,
                                "state": sch.start_state,
                            }

                if ed <= now:
                    if latest_end is None or ed > latest_end[0]:
                        latest_end = (ed, ek)
                    for box in sch.boxes:
                        current = latest_by_box.get(box)
                        candidate_order = (ed, schedule_index, 1)
                        if current is None or candidate_order > current["order"]:
                            latest_by_box[box] = {
                                "order": candidate_order,
                                "dt": ed,
                                "schedule_index": schedule_index,
                                "sch": sch,
                                "which": "end",
                                "key": ek,
                                "state": sch.end_state,
                            }

            if latest_start is not None:
                sch._last_start_key = latest_start[1]
            if latest_end is not None:
                sch._last_end_key = latest_end[1]

        grouped = {}
        for box, job in latest_by_box.items():
            group_key = (id(job["sch"]), job["which"], job["key"])
            if group_key not in grouped:
                grouped[group_key] = {
                    "sch": job["sch"],
                    "which": job["which"],
                    "key": job["key"],
                    "dt": job["dt"],
                    "state": job["state"],
                    "boxes": [],
                }
            grouped[group_key]["boxes"].append(box)

        return list(grouped.values())

    # changed: applies the actual desired current schedule state after loading a protocol or reconnecting the Bridge
    def _sync_current_schedule_outputs(self, reason: str = "schedule sync"):
        now = datetime.now()
        with self.schedules_lock:
            schedules_snapshot = list(self.adv_schedules)
            jobs = self._mark_past_boundaries_and_get_sync_jobs(schedules_snapshot, now)

        if not jobs:
            return

        if not self.connected.get():
            self._log(f"Schedule sync skipped: Bridge not connected ({reason}).")
            return

        self._log(f"Schedule sync at current time ({reason}): applying latest valid state.")

        for job in jobs:
            label = f"Schedule SYNC {job['which'].upper()}: {job['sch'].name}"

            def sync_done(success: bool, label_text=label):
                if not success and self.scheduler_running:
                    self._log(f"{label_text} will retry in 2 minutes.")
                    self.root.after(120000, lambda: self._sync_current_schedule_outputs(reason="sync retry"))

            self._apply_state_to_boxes(
                job["state"],
                list(job["boxes"]),
                label=label,
                event_time=job["dt"],
                on_complete=sync_done,
            )

    # changed: prevents stale missed START/END retries from applying outside their intended schedule window
    def _schedule_boundary_still_valid(self, sch: AdvancedSchedule, which: str, boundary_dt: datetime, now: datetime) -> bool:
        if which == "start":
            for offset in (-1, 0, 1):
                w = sch.window_for_date(boundary_dt.date() + timedelta(days=offset))
                if w and w[0] == boundary_dt:
                    return now < w[1]
            return True

        next_start = None
        for offset in range(-1, 10):
            w = sch.window_for_date(boundary_dt.date() + timedelta(days=offset))
            if not w:
                continue
            st = w[0]
            if st > boundary_dt and (next_start is None or st < next_start):
                next_start = st
        if next_start is None:
            return now < boundary_dt + timedelta(hours=12)
        return now < next_start

    # changed: scheduled actions now run in a worker thread, retry failed commands, and report success/failure back to scheduler
    def _apply_state_to_boxes(
        self,
        state: LightState,
        boxes: List[str],
        label: str = "",
        event_time: Optional[datetime] = None,
        on_complete: Optional[Callable[[bool], None]] = None
    ):
        def start_worker():
            if not self.connected.get():
                self._log(f"{label} skipped: not connected.")
                if on_complete:
                    on_complete(False)
                return

            targets: List[Tuple[str, str]] = []
            for box in boxes:
                light = self._get_box_light(box)
                if light:
                    targets.append((box, light))

            def worker():
                applied = 0
                failed: List[Tuple[str, str, str]] = []

                for box, light in targets:
                    success = False
                    last_error = ""
                    for attempt in range(1, 6):
                        try:
                            if state.action == "off":
                                self.hue.set_light_state(light, on=False)
                            elif state.action == "on":
                                self.hue.set_light_state(light, on=True, bri=state.bri)
                            else:
                                xy = state.resolved_xy()
                                self.hue.set_light_state(light, on=True, bri=state.bri, ct=state.ct, xy=xy)
                            applied += 1
                            success = True
                            break
                        except Exception as e:
                            last_error = str(e)
                            self._threadsafe_log(f"{label} attempt {attempt}/5 failed: {box} → {light}: {last_error}")
                            time.sleep(min(5 * attempt, 20))

                    if not success:
                        failed.append((box, light, last_error))

                def finish():
                    for box, light, err in failed:
                        self._log(f"{label} FAILED: {box} → {light}: {err}")
                    self._log(f"{label} applied to {applied}/{len(targets)} assigned lights.", when=event_time)
                    if on_complete:
                        on_complete(len(failed) == 0)
                    try:
                        self._refresh_lights(show_errors=False)
                    except Exception:
                        pass

                self.root.after(0, finish)

            threading.Thread(target=worker, daemon=True).start()

        self.root.after(0, start_worker)

    # changed: failed scheduled START/END actions are retried later, but not after their valid schedule window has passed
    def _scheduler_loop(self):
        while self.scheduler_running:
            try:
                now = datetime.now()
                with self.schedules_lock:
                    schedules_snapshot = list(self.adv_schedules)

                for sch in schedules_snapshot:
                    if not sch.enabled:
                        continue

                    start_bd, end_bd = sch.next_boundaries(now)

                    if start_bd is not None:
                        dt_bd, key = start_bd
                        if dt_bd <= now and sch._last_start_key != key:
                            if not self._schedule_boundary_still_valid(sch, "start", dt_bd, now):
                                with self.schedules_lock:
                                    sch._last_start_key = key
                                    sch._pending_start_key = None
                                continue
                            with self.schedules_lock:
                                if sch._pending_start_key == key:
                                    continue
                                if sch._start_retry_after is not None and now < sch._start_retry_after:
                                    continue
                                sch._pending_start_key = key

                            def start_done(success: bool, s=sch, k=key):
                                with self.schedules_lock:
                                    s._pending_start_key = None
                                    if success:
                                        s._last_start_key = k
                                        s._start_retry_after = None
                                    else:
                                        s._start_retry_after = datetime.now() + timedelta(minutes=2)
                                if not success:
                                    self._log(f"Schedule START will retry in 2 minutes: {s.name}")

                            self._apply_state_to_boxes(sch.start_state, list(sch.boxes), label=f"Schedule START: {sch.name}", event_time=dt_bd, on_complete=start_done)

                    if end_bd is not None:
                        dt_bd, key = end_bd
                        if dt_bd <= now and sch._last_end_key != key:
                            if not self._schedule_boundary_still_valid(sch, "end", dt_bd, now):
                                with self.schedules_lock:
                                    sch._last_end_key = key
                                    sch._pending_end_key = None
                                continue
                            with self.schedules_lock:
                                if sch._pending_end_key == key:
                                    continue
                                if sch._end_retry_after is not None and now < sch._end_retry_after:
                                    continue
                                sch._pending_end_key = key

                            def end_done(success: bool, s=sch, k=key):
                                with self.schedules_lock:
                                    s._pending_end_key = None
                                    if success:
                                        s._last_end_key = k
                                        s._end_retry_after = None
                                    else:
                                        s._end_retry_after = datetime.now() + timedelta(minutes=2)
                                if not success:
                                    self._log(f"Schedule END will retry in 2 minutes: {s.name}")

                            self._apply_state_to_boxes(sch.end_state, list(sch.boxes), label=f"Schedule END: {sch.name}", event_time=dt_bd, on_complete=end_done)

            except Exception as e:
                try:
                    self.root.after(0, lambda msg=str(e): self._log(f"Scheduler error: {msg}"))
                except Exception:
                    pass
            time.sleep(1.0)

    def _refresh_box_schedule_displays(self):
        with self.schedules_lock:
            snapshot = list(self.adv_schedules)
        for box_name, lbl in self.box_schedule_labels.items():
            active = [s.name for s in snapshot if s.enabled and box_name in s.boxes]
            txt = ", ".join(active) if active else "(no active schedules)"
            lbl.config(text=txt)

    def _clear_schedules(self):
        with self.schedules_lock:
            self.adv_schedules.clear()
        self._log("Cleared all schedules.")
        self._scheduler_refresh_table()
        self._refresh_box_schedule_displays()

    def _load_config(self):
        path = filedialog.askopenfilename(title="Load config JSON", filetypes=[("JSON files", "*.json")])
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            ip = data.get("bridge_ip", "")
            boxes_count = int(data.get("boxes_count", 8))
            assigns = data.get("box_assignments", {})
            schedules = data.get("advanced_schedules", [])

            if ip:
                self.ip_entry.delete(0, "end")
                self.ip_entry.insert(0, ip)

            self.boxes_count.set(boxes_count)
            self._rebuild_tabs()

            for box, light_name in assigns.items():
                if box in self.box_assignments:
                    self.box_assignments[box].set(light_name)

            loaded_schedules: List[AdvancedSchedule] = []
            for row in schedules:
                sch = AdvancedSchedule(
                    name=row.get("name", "Schedule"),
                    boxes=row.get("boxes", []),
                    start_date=row.get("start_date", ""),
                    start_time=row.get("start_time", "08:00"),
                    end_date=row.get("end_date", ""),
                    end_time=row.get("end_time", "18:00"),
                    recurrence=row.get("recurrence", "once"),
                    every_n=int(row.get("every_n", 1)),
                    weekdays=list(row.get("weekdays", [])),
                    until_date=row.get("until_date", ""),
                    start_state=LightState(**row.get("start_state", {"action": "set", "bri": 160, "preset": "white"})),
                    end_state=LightState(**row.get("end_state", {"action": "off"})),
                    enabled=bool(row.get("enabled", True)),
                )
                loaded_schedules.append(sch)

            with self.schedules_lock:
                self._mark_past_boundaries_and_get_sync_jobs(loaded_schedules, datetime.now())
                self.adv_schedules = loaded_schedules

            self._scheduler_refresh_table()
            self._refresh_box_schedule_displays()
            self._log(f"Loaded config: {path}")
            if self.connected.get():
                self._refresh_lights()
            self._sync_current_schedule_outputs(reason="config load")
        except Exception as e:
            messagebox.showerror("Load failed", str(e))

    def _save_config(self):
        path = filedialog.asksaveasfilename(
            title="Save config JSON",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json")]
        )
        if not path:
            return
        try:
            with self.schedules_lock:
                schedules_snapshot = list(self.adv_schedules)

            data = {
                "bridge_ip": self.ip_entry.get().strip(),
                "boxes_count": int(self.boxes_count.get()),
                "box_assignments": {box: var.get() for box, var in self.box_assignments.items()},
                "advanced_schedules": [
                    {
                        "name": s.name,
                        "boxes": s.boxes,
                        "start_date": s.start_date,
                        "start_time": s.start_time,
                        "end_date": s.end_date,
                        "end_time": s.end_time,
                        "recurrence": s.recurrence,
                        "every_n": s.every_n,
                        "weekdays": s.weekdays,
                        "until_date": s.until_date,
                        "start_state": {
                            "action": s.start_state.action,
                            "bri": s.start_state.bri,
                            "preset": s.start_state.preset,
                            "ct": s.start_state.ct,
                            "xy": s.start_state.xy,
                            "wavelength_nm": s.start_state.wavelength_nm,
                        },
                        "end_state": {
                            "action": s.end_state.action,
                            "bri": s.end_state.bri,
                            "preset": s.end_state.preset,
                            "ct": s.end_state.ct,
                            "xy": s.end_state.xy,
                            "wavelength_nm": s.end_state.wavelength_nm,
                        },
                        "enabled": s.enabled
                    }
                    for s in schedules_snapshot
                ]
            }
            Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")
            self._log(f"Saved config: {path}")
        except Exception as e:
            messagebox.showerror("Save failed", str(e))

    def _export_schedules_csv(self):
        path = filedialog.asksaveasfilename(
            title="Export schedules CSV",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")]
        )
        if not path:
            return
        try:
            cols = [
                "name", "boxes", "start_date", "start_time", "end_date", "end_time",
                "recurrence", "every_n", "weekdays", "until_date", "enabled",
                "start_action", "start_bri", "start_preset", "start_ct", "start_xy", "start_wavelength_nm",
                "end_action", "end_bri", "end_preset", "end_ct", "end_xy", "end_wavelength_nm",
            ]
            with self.schedules_lock:
                schedules_snapshot = list(self.adv_schedules)

            with open(path, "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=cols)
                w.writeheader()
                for s in schedules_snapshot:
                    w.writerow({
                        "name": s.name,
                        "boxes": "|".join(s.boxes),
                        "start_date": s.start_date,
                        "start_time": s.start_time,
                        "end_date": s.end_date,
                        "end_time": s.end_time,
                        "recurrence": s.recurrence,
                        "every_n": s.every_n,
                        "weekdays": ",".join(str(i) for i in s.weekdays),
                        "until_date": s.until_date,
                        "enabled": str(bool(s.enabled)),
                        "start_action": s.start_state.action,
                        "start_bri": "" if s.start_state.bri is None else s.start_state.bri,
                        "start_preset": "" if s.start_state.preset is None else s.start_state.preset,
                        "start_ct": "" if s.start_state.ct is None else s.start_state.ct,
                        "start_xy": "" if s.start_state.xy is None else f"{s.start_state.xy[0]},{s.start_state.xy[1]}",
                        "start_wavelength_nm": "" if s.start_state.wavelength_nm is None else s.start_state.wavelength_nm,
                        "end_action": s.end_state.action,
                        "end_bri": "" if s.end_state.bri is None else s.end_state.bri,
                        "end_preset": "" if s.end_state.preset is None else s.end_state.preset,
                        "end_ct": "" if s.end_state.ct is None else s.end_state.ct,
                        "end_xy": "" if s.end_state.xy is None else f"{s.end_state.xy[0]},{s.end_state.xy[1]}",
                        "end_wavelength_nm": "" if s.end_state.wavelength_nm is None else s.end_state.wavelength_nm,
                    })
            self._log(f"Exported schedules CSV: {path}")
        except Exception as e:
            messagebox.showerror("Export failed", str(e))

    def _load_schedules_csv(self):
        path = filedialog.askopenfilename(title="Load schedules CSV", filetypes=[("CSV files", "*.csv")])
        if not path:
            return
        try:
            loaded: List[AdvancedSchedule] = []
            added = 0
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    boxes_raw = (row.get("boxes") or "").strip()
                    boxes = [b.strip() for b in boxes_raw.split("|") if b.strip()]
                    boxes = [b for b in boxes if b in self.box_tabs]
                    if not boxes:
                        continue

                    start_state = LightState(
                        action=(row.get("start_action") or "set").strip().lower(),
                        bri=int(float(row.get("start_bri"))) if (row.get("start_bri") or "").strip() else None,
                        preset=(row.get("start_preset") or "").strip().lower() or None,
                        ct=int(float(row.get("start_ct"))) if (row.get("start_ct") or "").strip() else None,
                        xy=parse_xy((row.get("start_xy") or "").strip()) if (row.get("start_xy") or "").strip() else None,
                        wavelength_nm=float(row.get("start_wavelength_nm")) if (row.get("start_wavelength_nm") or "").strip() else None,
                    )

                    end_state = LightState(
                        action=(row.get("end_action") or "off").strip().lower(),
                        bri=int(float(row.get("end_bri"))) if (row.get("end_bri") or "").strip() else None,
                        preset=(row.get("end_preset") or "").strip().lower() or None,
                        ct=int(float(row.get("end_ct"))) if (row.get("end_ct") or "").strip() else None,
                        xy=parse_xy((row.get("end_xy") or "").strip()) if (row.get("end_xy") or "").strip() else None,
                        wavelength_nm=float(row.get("end_wavelength_nm")) if (row.get("end_wavelength_nm") or "").strip() else None,
                    )

                    weekdays_raw = (row.get("weekdays") or "").strip()
                    weekdays = []
                    if weekdays_raw:
                        try:
                            weekdays = [int(x.strip()) for x in weekdays_raw.split(",") if x.strip() != ""]
                            weekdays = [x for x in weekdays if 0 <= x <= 6]
                        except Exception:
                            weekdays = []

                    enabled_raw = (row.get("enabled") or "True").strip().lower()
                    enabled = enabled_raw not in ("0", "false", "no", "off")

                    sch = AdvancedSchedule(
                        name=(row.get("name") or "Schedule").strip(),
                        boxes=boxes,
                        start_date=(row.get("start_date") or "").strip(),
                        start_time=(row.get("start_time") or "08:00").strip(),
                        end_date=(row.get("end_date") or "").strip(),
                        end_time=(row.get("end_time") or "18:00").strip(),
                        recurrence=(row.get("recurrence") or "once").strip().lower(),
                        every_n=int(float(row.get("every_n") or 1)),
                        weekdays=weekdays,
                        until_date=(row.get("until_date") or "").strip(),
                        start_state=start_state,
                        end_state=end_state,
                        enabled=enabled
                    )

                    if parse_date(sch.start_date) is None or parse_date(sch.end_date) is None:
                        continue
                    if parse_time_hhmm(sch.start_time) is None or parse_time_hhmm(sch.end_time) is None:
                        continue

                    loaded.append(sch)
                    added += 1

            with self.schedules_lock:
                self.adv_schedules.extend(loaded)

            self._log(f"Loaded schedules CSV: {path} (added {added})")
            self._scheduler_refresh_table()
            self._refresh_box_schedule_displays()
        except Exception as e:
            messagebox.showerror("Load schedules failed", str(e))


def main():
    root = tk.Tk()
    app = LightBoxGUI(root)

    def on_close():
        app.scheduler_running = False
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
