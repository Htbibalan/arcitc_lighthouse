

from __future__ import annotations

import argparse
import asyncio
import copy
import csv
import inspect
import io
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import re
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

import requests

try:
    import flet as ft
except ImportError:
    ft = None  # Core logic / unit tests can run without a graphical installation.

APP_VERSION = "7.0.0"
FLET_VERSION = "1.0.0"
SCHEMA_VERSION = 3
APP_DIR = Path.home() / ".lighthouse_flet"
AUTH_FILE = Path.home() / ".lighthouse_hue_auth.json"
KINDS = ["Room", "Box", "Cage", "Compartment", "Space"]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
PRESET_XY = {
    "red": (0.675, 0.322), "green": (0.409, 0.518),
    "blue": (0.167, 0.040), "white": (0.3127, 0.3290),
    "warm": (0.501, 0.415), "cool": (0.300, 0.300),
}
SWATCHES = {"red": "#FF626C", "green": "#6BDD98", "blue": "#719CFF",
            "white": "#F1F4F8", "warm": "#FFCE88", "cool": "#B2E5FF"}
BG, PANEL, PANEL_2 = "#0C111B", "#141D2B", "#1D293A"
BORDER, TEXT, MUTED = "#2A374A", "#EFF4FC", "#9BAEC6"
ACCENT, SUCCESS, DANGER = "#FFC778", "#79DDB5", "#FF8F9B"


def new_id() -> str:
    return uuid.uuid4().hex


def clamp(v, low, high):
    return max(low, min(high, v))


def number(value: Any, label: str, low: float, high: float, integer=False):
    try:
        out = float(value)
    except (ValueError, TypeError):
        raise ValueError(f"{label} must be a number.") from None
    if not math.isfinite(out) or not low <= out <= high:
        raise ValueError(f"{label} must be between {low:g} and {high:g}.")
    if integer and not out.is_integer():
        raise ValueError(f"{label} must be a whole number.")
    return int(out) if integer else out


def parse_date(s: str) -> Optional[date]:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(str(s).strip(), fmt).date()
        except (ValueError, TypeError):
            pass
    return None


def required_date(s: str, label="Date") -> date:
    result = parse_date(s)
    if result is None:
        raise ValueError(f"{label}: use YYYY-MM-DD or DD/MM/YYYY.")
    if result.year < 1900 or result.year > 2200:
        raise ValueError(f"{label}: supported years are 1900–2200.")
    return result


def parse_time_hhmm(s: str) -> Optional[tuple[int, int]]:
    try:
        hh, mm = str(s).strip().split(":")
        if not hh.isdigit() or not mm.isdigit():
            return None
        h, m = int(hh), int(mm)
        return (h, m) if 0 <= h <= 23 and 0 <= m <= 59 else None
    except (ValueError, TypeError):
        return None


def normal_time(s: str) -> str:
    hm = parse_time_hhmm(s)
    if hm is None:
        raise ValueError("Time must be HH:MM, between 00:00 and 23:59.")
    return f"{hm[0]:02}:{hm[1]:02}"


def at(d: date, hm: str) -> datetime:
    h, m = parse_time_hhmm(hm) or (0, 0)
    return datetime(d.year, d.month, d.day, h, m)


def parse_xy(raw: Any) -> Optional[tuple[float, float]]:
    if raw is None or raw == "":
        return None
    try:
        vals = raw.split(",") if isinstance(raw, str) else list(raw)
        if len(vals) != 2:
            raise ValueError
        x = number(vals[0], "X", 0, 1)
        y = number(vals[1], "Y", 0, 1)
        if x + y > 1.000001:
            raise ValueError("XY coordinates must have x + y <= 1.")
        return x, y
    except (ValueError, TypeError):
        raise ValueError("XY must be two numbers, e.g. 0.313,0.329; x+y must be <= 1.") from None


def wavelength_to_xy_nm(wavelength_nm: float) -> tuple[float, float]:
    """Original approximate visual colour conversion, NOT spectral calibration.

    Hue lamps mix LEDs; selecting 470 nm does not produce monochromatic 470 nm light. so the users better measure the acutral output and then write dwon the desired values.
    """
    w = number(wavelength_nm, "Wavelength", 380, 700)
    if w < 440:
        r, g, b = -(w - 440) / 60, 0., 1.
    elif w < 490:
        r, g, b = 0., (w - 440) / 50, 1.
    elif w < 510:
        r, g, b = 0., 1., -(w - 510) / 20
    elif w < 580:
        r, g, b = (w - 510) / 70, 1., 0.
    elif w < 645:
        r, g, b = 1., -(w - 645) / 65, 0.
    else:
        r, g, b = 1., 0., 0.
    factor = 0.3 + 0.7 * (w - 380) / 40 if w < 420 else (
        1. if w <= 645 else 0.3 + 0.7 * (700 - w) / 55)
    r, g, b = [(v * factor) ** .8 for v in (r, g, b)]
    X = r * .4124 + g * .3576 + b * .1805
    Y = r * .2126 + g * .7152 + b * .0722
    Z = r * .0193 + g * .1192 + b * .9505
    return (X / (X + Y + Z), Y / (X + Y + Z)) if X + Y + Z else PRESET_XY["white"]


def as_bool(value: Any, default=True) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return value.lower().strip() not in ("false", "0", "no", "off")
    return bool(value)


@dataclass
class LightState:
    action: str = "set"
    bri: Optional[int] = 160
    preset: Optional[str] = None
    ct: Optional[int] = None
    xy: Optional[tuple[float, float]] = None
    wavelength_nm: Optional[float] = None
    transitiontime: int = 4  # Hue units = 0.1 second; original default = 0.4 s.

    def validate(self):
        self.action = str(self.action).strip().lower()
        if self.action not in ("set", "on", "off"):
            raise ValueError("Action must be SET, ON or OFF.")
        self.transitiontime = number(self.transitiontime, "Transition (0.1 s units)", 0, 65535, True)
        if self.action == "off":
            return self
        if self.bri is not None:
            self.bri = number(self.bri, "Brightness", 0, 254, True)
        if self.action == "on":
            return self
        if self.preset:
            self.preset = str(self.preset).lower().strip()
            if self.preset not in PRESET_XY:
                raise ValueError(f"Unknown preset: {self.preset}")
        if self.ct is not None:
            self.ct = number(self.ct, "Colour temperature (mired)", 153, 500, True)
        self.xy = parse_xy(self.xy)
        if self.wavelength_nm is not None:
            self.wavelength_nm = number(self.wavelength_nm, "Wavelength", 380, 700)
        return self

    def resolved_xy(self):
        if self.preset:
            return PRESET_XY[self.preset]
        if self.wavelength_nm is not None:
            return wavelength_to_xy_nm(self.wavelength_nm)
        return self.xy

    def payload(self) -> dict:
        self.validate()
        cmd = {"on": self.action != "off", "transitiontime": self.transitiontime}
        if self.action != "off" and self.bri is not None:
            cmd["bri"] = self.bri
        # ON intentionally preserves colour; only SET changes colour. # Maybe I need to get rid of this in the future versions
        if self.action == "set":
            xy = self.resolved_xy()
            if xy is not None:
                cmd["xy"] = list(xy)
            elif self.ct is not None:
                cmd["ct"] = self.ct
        return cmd

    def summary(self) -> str:
        cmd = self.payload()
        items = [self.action.upper()]
        if "bri" in cmd:
            items.append(f"BRI {self.bri}")
        if "xy" in cmd:
            if self.preset:
                items.append(self.preset.title())
            elif self.wavelength_nm is not None:
                items.append(f"~{self.wavelength_nm:g} nm colour")
            else:
                items.append(f"XY {cmd['xy'][0]:.3f}, {cmd['xy'][1]:.3f}")
        elif "ct" in cmd:
            items.append(f"CT {self.ct}")
        return " · ".join(items)

    @classmethod
    def from_dict(cls, raw: dict):
        if not isinstance(raw, dict):
            raise ValueError("Light state must be an object.")
        fields = cls.__dataclass_fields__
        return cls(**{k: v for k, v in raw.items() if k in fields}).validate()


@dataclass
class LightRef:
    id: str = ""
    uniqueid: str = ""
    name: str = ""

    @property
    def key(self):
        return f"uid:{self.uniqueid}" if self.uniqueid else (
            f"id:{self.id}" if self.id else f"name:{self.name}")


@dataclass
class Space:
    name: str
    kind: str = "Space"
    id: str = field(default_factory=new_id)
    lights: list[LightRef] = field(default_factory=list)
    notes: str = ""


@dataclass
class Schedule:
    name: str
    spaces: list[str]
    start_date: str
    start_time: str = "08:00"
    end_date: str = ""
    end_time: str = "18:00"
    recurrence: str = "daily"
    every_n: int = 1
    weekdays: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])
    until_date: str = ""
    start_state: LightState = field(default_factory=lambda: LightState(preset="white"))
    end_state: LightState = field(default_factory=lambda: LightState(action="off", bri=None))
    enabled: bool = True
    id: str = field(default_factory=new_id)
    revision: str = field(default_factory=new_id)

    def validate(self):
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("Please enter a schedule name.")
        if not self.spaces:
            raise ValueError("Select at least one space.")
        self.spaces = list(dict.fromkeys(self.spaces))
        sd = required_date(self.start_date, "Start date")
        self.start_date = sd.isoformat()
        self.end_date = required_date(self.end_date or self.start_date, "End date").isoformat()
        self.start_time, self.end_time = normal_time(self.start_time), normal_time(self.end_time)
        self.recurrence = self.recurrence.lower().strip()
        if self.recurrence not in ("once", "daily", "weekly"):
            raise ValueError("Recurrence must be once, daily or weekly.")
        self.every_n = number(self.every_n, "Every N", 1, 365, True)
        self.weekdays = sorted(set(number(x, "Weekday", 0, 6, True) for x in self.weekdays))
        if self.recurrence == "weekly" and not self.weekdays:
            raise ValueError("Choose at least one weekday.")
        if self.until_date:
            ud = required_date(self.until_date, "Until date")
            if ud < sd:
                raise ValueError("Until date cannot be before the start date.")
            self.until_date = ud.isoformat()
        if self.recurrence == "once" and required_date(self.end_date) < sd:
            raise ValueError("End date cannot be before the start date.")
        self.start_state.validate()
        self.end_state.validate()
        return self

    def occurrence_before(self, d: date) -> Optional[date]:
        """Last occurrence date <= d, using arithmetic rather than a lookback cap."""
        sd = required_date(self.start_date)
        if self.until_date:
            d = min(d, required_date(self.until_date))
        if d < sd:
            return None
        if self.recurrence == "once":
            return sd
        delta = (d - sd).days
        if self.recurrence == "daily":
            return sd + timedelta(days=(delta // self.every_n) * self.every_n)
        block = ((delta // 7) // self.every_n) * self.every_n * 7
        for _ in range(2):
            if block < 0:
                return None
            base = sd + timedelta(days=block)
            days = [base + timedelta(days=i) for i in range(7)]
            valid = [x for x in days if x <= d and x.weekday() in self.weekdays]
            if valid:
                return max(valid)
            block -= self.every_n * 7
        return None

    def occurrence_after(self, d: date) -> Optional[date]:
        """First occurrence date >= d. Weekly blocks are anchored at start_date."""
        sd = required_date(self.start_date)
        d = max(d, sd)
        until = required_date(self.until_date) if self.until_date else None
        if until and d > until:
            return None
        if self.recurrence == "once":
            return sd if d <= sd else None
        delta = (d - sd).days
        if self.recurrence == "daily":
            out = sd + timedelta(days=((delta + self.every_n - 1) // self.every_n) * self.every_n)
            return None if until and out > until else out
        week = delta // 7
        block = ((week + self.every_n - 1) // self.every_n) * self.every_n * 7
        for _ in range(2):
            base = sd + timedelta(days=block)
            days = [base + timedelta(days=i) for i in range(7)]
            valid = [x for x in days if x >= d and x.weekday() in self.weekdays and (not until or x <= until)]
            if valid:
                return min(valid)
            block += self.every_n * 7
        return None

    def window(self, d: date) -> tuple[datetime, datetime]:
        start = at(d, self.start_time)
        end = at(required_date(self.end_date), self.end_time) if self.recurrence == "once" else at(d, self.end_time)
        if end <= start:
            end += timedelta(days=1)
        return start, end

    def boundary(self, now: datetime, future=False):
        """Return nearest past (<= now) or next future (> now) START/END."""
        if not self.enabled:
            return None
        if self.recurrence == "once":
            w = self.window(required_date(self.start_date))
            candidates = [(w[0], "start"), (w[1], "end")]
        else:
            shift = int(self.end_time <= self.start_time)
            candidates = []
            for which, offset in (("start", 0), ("end", shift)):
                ref = now.date() - timedelta(days=offset)
                find = self.occurrence_after if future else self.occurrence_before
                d = find(ref)
                if d is not None:
                    dt = self.window(d)[0 if which == "start" else 1]
                    if (future and dt <= now) or (not future and dt > now):
                        d = find(d + timedelta(days=1 if future else -1))
                        if d is not None:
                            dt = self.window(d)[0 if which == "start" else 1]
                    if d is not None:
                        candidates.append((dt, which))
        candidates = [x for x in candidates if (x[0] > now if future else x[0] <= now)]
        if not candidates:
            return None
        # A new START wins over an immediately preceding END at the same instant
        # (e.g. a daily 08:00–08:00 cycle), avoiding a false all-day OFF state.
        key = lambda x: (x[0], x[1] == ("end" if future else "start"))
        return (min if future else max)(candidates, key=key)

    def recurrence_text(self):
        if self.recurrence == "once":
            return "Once"
        if self.recurrence == "daily":
            return "Daily" if self.every_n == 1 else f"Every {self.every_n} days"
        days = ", ".join(WEEKDAYS[i] for i in self.weekdays)
        return f"Every {self.every_n} week{'s' if self.every_n != 1 else ''} · {days}"


@dataclass
class Project:
    spaces: list[Space] = field(default_factory=list)
    schedules: list[Schedule] = field(default_factory=list)
    bridge_ip: str = ""
    bridge_id: str = ""
    auto_connect: bool = False
    scheduler_enabled: bool = True
    title: str = "My Lighthouse"

    def to_dict(self):
        return {"schema_version": SCHEMA_VERSION, "app_version": APP_VERSION, **asdict(self)}

    @classmethod
    def from_dict(cls, data: dict) -> tuple[Project, list[str]]:
        if not isinstance(data, dict):
            raise ValueError("The JSON file must contain a project object.")
        if not any(k in data for k in ("spaces", "boxes_count", "box_assignments", "advanced_schedules")):
            raise ValueError("This is not a Lighthouse project JSON.")
        if int(data.get("schema_version", 0)) > SCHEMA_VERSION:
            raise ValueError("This project was made by a newer Lighthouse version.")
        p = cls(bridge_ip=str(data.get("bridge_ip", "")), bridge_id=str(data.get("bridge_id", "")),
                auto_connect=as_bool(data.get("auto_connect"), False),
                scheduler_enabled=as_bool(data.get("scheduler_enabled"), True),
                title=str(data.get("title") or "My Lighthouse"))
        warnings = []
        legacy = "spaces" not in data
        rows = data.get("advanced_schedules", []) if legacy else data.get("schedules", [])
        if legacy:
            assigns = data.get("box_assignments") or {}
            count = number(data.get("boxes_count", max(8, len(assigns))), "Box count", 1, 512, True)
            names = [f"Box {i}" for i in range(1, count + 1)]
            names += [n for n in assigns if n not in names]
            for row in rows:
                for name in row.get("boxes", []):
                    if name not in names:
                        names.append(name)
            for name in names:
                light = str(assigns.get(name) or "").strip()
                p.spaces.append(Space(name=name, kind="Box", lights=[LightRef(name=light)] if light and light != "(none)" else []))
            warnings.append("Legacy boxes converted to renameable spaces. Existing light names will resolve when the Bridge connects.")
        else:
            for row in data.get("spaces", []):
                refs = [LightRef(**{k: str(v or "") for k, v in ref.items() if k in ("id", "uniqueid", "name")})
                        if isinstance(ref, dict) else LightRef(name=str(ref)) for ref in row.get("lights", [])]
                p.spaces.append(Space(name=str(row.get("name", "")).strip(), kind=str(row.get("kind", "Space")),
                                      id=str(row.get("id") or new_id()), lights=refs, notes=str(row.get("notes", ""))))
        names = [s.name.casefold() for s in p.spaces]
        ids = [s.id for s in p.spaces]
        if any(not n for n in names) or len(names) != len(set(names)) or len(ids) != len(set(ids)):
            raise ValueError("Space names and IDs must be non-empty and unique.")
        by_name = {s.name: s.id for s in p.spaces}
        for i, row in enumerate(rows, 1):
            try:
                targets = [by_name[n] for n in row.get("boxes", []) if n in by_name] if legacy else list(row.get("spaces", []))
                if any(x not in ids for x in targets):
                    raise ValueError("Schedule refers to an unknown space.")
                s = schedule_from_dict(row, targets)
                if legacy and s.recurrence == "once" and s.start_date != s.end_date:
                    warnings.append(f"'{s.name}': this version honours the explicit one-off end date ({s.end_date}); the old scheduler ignored it.")
                p.schedules.append(s)
            except (ValueError, TypeError, KeyError) as ex:
                raise ValueError(f"Schedule {i}: {ex}") from ex
        if len({s.id for s in p.schedules}) != len(p.schedules):
            raise ValueError("Duplicate schedule IDs in project.")
        return p, warnings


def schedule_from_dict(row: dict, targets: list[str]) -> Schedule:
    return Schedule(name=str(row.get("name") or "Schedule"), spaces=targets,
                    start_date=str(row.get("start_date") or ""),
                    start_time=str(row.get("start_time") or "08:00"),
                    end_date=str(row.get("end_date") or row.get("start_date") or ""),
                    end_time=str(row.get("end_time") or "18:00"),
                    recurrence=str(row.get("recurrence") or "once"), every_n=row.get("every_n") or 1,
                    weekdays=list(row.get("weekdays") or []), until_date=str(row.get("until_date") or ""),
                    start_state=LightState.from_dict(row.get("start_state") or {"action": "set", "bri": 160, "preset": "white"}),
                    end_state=LightState.from_dict(row.get("end_state") or {"action": "off", "bri": None}),
                    enabled=as_bool(row.get("enabled")), id=str(row.get("id") or new_id()),
                    revision=str(row.get("revision") or new_id())).validate()


def csv_import(text: str, project: Project) -> list[Schedule]:
    """Accept original 'boxes' and new 'spaces' CSVs. Fail atomically on bad rows."""
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    headers = set(reader.fieldnames or [])
    if "start_date" not in headers or not headers.intersection({"boxes", "spaces", "space_ids"}):
        raise ValueError("CSV needs start_date and spaces (or legacy boxes) columns.")
    by_name = {s.name: s.id for s in project.spaces}
    ids = {s.id for s in project.spaces}
    result = []
    for line, row in enumerate(reader, 2):
        if not any(str(v or "").strip() for v in row.values()):
            continue
        try:
            saved_ids = [x.strip() for x in (row.get("space_ids") or "").split("|") if x.strip()]
            if saved_ids and all(x in ids for x in saved_ids):
                targets = saved_ids
            else:
                names = [n.strip() for n in (row.get("spaces") or row.get("boxes") or "").split("|") if n.strip()]
                missing = [n for n in names if n not in by_name]
                if missing:
                    raise ValueError("Create or rename these spaces first: " + ", ".join(missing))
                targets = [by_name[n] for n in names]
            sd = dict(row)
            sd["weekdays"] = [int(v.strip()) for v in (row.get("weekdays") or "").split(",") if v.strip()]
            for prefix in ("start", "end"):
                state = {"action": row.get(f"{prefix}_action") or ("set" if prefix == "start" else "off")}
                for name in ("bri", "preset", "ct", "xy", "wavelength_nm", "transitiontime"):
                    v = (row.get(f"{prefix}_{name}") or "").strip()
                    if v:
                        state[name] = v
                    elif name == "bri":
                        state[name] = None
                sd[f"{prefix}_state"] = state
            result.append(schedule_from_dict(sd, targets))
        except (ValueError, TypeError, KeyError) as ex:
            raise ValueError(f"CSV line {line}: {ex}") from ex
    if not result:
        raise ValueError("The CSV contains no schedules.")
    return result


def csv_export(project: Project) -> str:
    out = io.StringIO(newline="")
    base = ["name", "spaces", "space_ids", "start_date", "start_time", "end_date", "end_time",
            "recurrence", "every_n", "weekdays", "until_date", "enabled"]
    fields = ["action", "bri", "preset", "ct", "xy", "wavelength_nm", "transitiontime"]
    writer = csv.DictWriter(out, fieldnames=base + [f"{p}_{f}" for p in ("start", "end") for f in fields])
    writer.writeheader()
    names = {s.id: s.name for s in project.spaces}
    for s in project.schedules:
        row = {k: getattr(s, k) for k in base if k not in ("spaces", "space_ids", "weekdays")}
        row.update(spaces="|".join(names.get(sid, "Missing space") for sid in s.spaces),
                   space_ids="|".join(s.spaces), weekdays=",".join(map(str, s.weekdays)))
        for prefix, state in (("start", s.start_state), ("end", s.end_state)):
            for f in fields:
                value = getattr(state, f)
                if f == "xy" and value is not None:
                    value = f"{value[0]},{value[1]}"
                row[f"{prefix}_{f}"] = "" if value is None else value
        writer.writerow(row)
    return out.getvalue()


def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + new_id() + ".tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class HueError(RuntimeError):
    def __init__(self, message: str, code=None):
        super().__init__(message)
        self.code = code


class HueController:
    """Blocking local-API backend. GUI ALWAYS calls this through asyncio.to_thread."""
    def __init__(self, auth_path: Path = AUTH_FILE, demo=False):
        self.auth_path, self.demo = auth_path, demo
        self.ip, self.username, self.bridge_id = "", "", ""
        self.lights: dict[str, dict] = {}
        self.lock = threading.RLock()
        self.http = requests.Session()
        self.http.trust_env = False  # Never send Bridge LAN traffic via proxy settings.
        self.last_success: Optional[datetime] = None
        if demo:
            for i, name in enumerate(["Ceiling A", "Ceiling B", "Day lamp", "Night lamp", "Bench left", "Bench right", "Cage lamp A", "Cage lamp B"], 1):
                self.lights[str(i)] = {"name": name, "uniqueid": f"demo-{i}", "type": "Extended color light",
                                        "modelid": "DEMO", "state": {"on": i <= 5, "bri": 160, "xy": [0.313, 0.329],
                                                                       "ct": 370, "colormode": "xy", "reachable": True}}
            self.bridge_id, self.ip, self.username = "DEMO", "demo", "demo"

    @staticmethod
    def valid_ip(raw: str) -> str:
        try:
            value = ipaddress.ip_address(raw.strip())
            if value.version != 4 or value.is_unspecified or value.is_multicast:
                raise ValueError
            return str(value)
        except ValueError:
            raise ValueError("Enter the Bridge IPv4 address only, e.g. 192.168.1.20 (no http://).") from None

    @staticmethod
    def check_errors(data):
        entries = data if isinstance(data, list) else [data]
        for item in entries:
            if isinstance(item, dict) and "error" in item:
                err = item["error"]
                raise HueError(str(err.get("description", "Hue API error")), err.get("type"))
        return data

    def request(self, method: str, path="", payload=None, registration=False):
        if not self.ip:
            raise HueError("No Bridge IP. Connect to the Bridge first.")
        if not registration and not self.username:
            raise HueError("No saved Hue credential. Press the Bridge button, then Connect / Register.")
        base = f"http://{self.ip}/api" + ("" if registration else f"/{self.username}")
        try:
            response = self.http.request(method, base + path, json=payload, timeout=(3.5, 6))
            response.raise_for_status()
            data = self.check_errors(response.json())
        except requests.RequestException as ex:
            # Do not expose requests' URL, which contains the secret API username.
            raise HueError(f"Bridge {self.ip} did not respond successfully ({type(ex).__name__}). Check LAN and power.") from None
        except ValueError:
            raise HueError("Bridge returned a non-JSON response.") from None
        self.last_success = datetime.now()
        return data

    def load_auth(self):
        if not self.auth_path.exists():
            return {}
        try:
            raw = json.loads(self.auth_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError
            return raw
        except (ValueError, OSError):
            raise HueError(f"Cannot read Hue credentials at {self.auth_path}; make a backup and inspect the file.") from None

    def connect(self, raw_ip: str, allow_register=True, expected_bridge_id=""):
        with self.lock:
            if self.demo:
                return self.refresh()
            self.ip = self.valid_ip(raw_ip)
            saved = self.load_auth()
            self.username = str(saved.get(self.ip) or "")
            config = None
            if self.username:
                try:
                    config = self.request("GET", "/config")
                except HueError as ex:
                    if ex.code != 1:
                        raise  # A network failure must not trigger fresh registration.
                    self.username = ""
            if not self.username:
                if not allow_register:
                    raise HueError("No valid saved credential. Press the Bridge button and use Connect / Register.")
                data = self.request("POST", payload={"devicetype": "lighthouse#spaces"}, registration=True)
                try:
                    self.username = data[0]["success"]["username"]
                except (KeyError, IndexError, TypeError):
                    raise HueError("Registration did not return a username.") from None
                saved[self.ip] = self.username
                atomic_write(self.auth_path, json.dumps(saved, indent=2))
                try:
                    self.auth_path.chmod(0o600)
                except OSError:
                    pass
            config = config or self.request("GET", "/config")
            self.bridge_id = str(config.get("bridgeid") or config.get("mac") or "")
            if expected_bridge_id and self.bridge_id != expected_bridge_id:
                self.username = ""
                raise HueError("This is a different Bridge from the one bound to this project. Use a separate workspace to avoid controlling the wrong lights.")
            return self.refresh()

    def discover(self):
        
        try:
            r = requests.get("https://discovery.meethue.com", timeout=(4, 6))
            r.raise_for_status()
            data = r.json()
            return [self.valid_ip(x["internalipaddress"]) for x in data if isinstance(x, dict) and x.get("internalipaddress")]
        except (requests.RequestException, ValueError, KeyError):
            raise HueError("Online discovery failed. Enter the IP shown in the router's connected-device list instead.") from None

    def refresh(self):
        with self.lock:
            if not self.demo:
                data = self.request("GET", "/lights")
                if not isinstance(data, dict):
                    raise HueError("Unexpected lights response from the Bridge.")
                self.lights = {str(k): v for k, v in data.items()}
            self.last_success = datetime.now()
            return copy.deepcopy(self.lights)

    def set_state(self, lid: str, state: LightState):
        with self.lock:
            lid = str(lid)
            if lid not in self.lights:
                raise HueError(f"Light ID {lid} is no longer on this Bridge. Refresh lights and review assignments.")
            light = self.lights[lid]
            cached = light.get("state", {})
            if cached.get("reachable") is False:
                raise HueError(f"{light.get('name', lid)} is reported unreachable by the Bridge.")
            cmd = state.payload()
            for field_name in ("bri", "xy", "ct"):
                if field_name in cmd and field_name not in cached:
                    raise HueError(f"{light.get('name', lid)} does not support {field_name}. Select a supported control mode.")
            if "ct" in cmd:
                cap = light.get("capabilities", {}).get("control", {}).get("ct", {})
                low, high = cap.get("min", 153), cap.get("max", 500)
                if not low <= cmd["ct"] <= high:
                    raise HueError(f"{light.get('name', lid)} supports CT {low}–{high}, not {cmd['ct']}.")
            if not self.demo:
                self.request("PUT", f"/lights/{lid}/state", cmd)
            cached.update({k: v for k, v in cmd.items() if k != "transitiontime"})
            if "xy" in cmd:
                cached["colormode"] = "xy"
            elif "ct" in cmd:
                cached["colormode"] = "ct"
            self.last_success = datetime.now()
            
            return copy.deepcopy(light)


def make_ref(lid: str, light: dict):
    return LightRef(id=str(lid), uniqueid=str(light.get("uniqueid") or ""), name=str(light.get("name") or f"Light {lid}"))


def resolve_ref(ref: LightRef, lights: dict[str, dict]) -> Optional[str]:
    if ref.uniqueid:
        return next((lid for lid, info in lights.items() if info.get("uniqueid") == ref.uniqueid), None)
    if ref.id:
        return ref.id if ref.id in lights else None
    # Old Lighthouse ( on tkinter) disambiguated duplicate names as 'Name (id:3)'.
    match = re.fullmatch(r"(.*?) \(id:(\d+)\)", ref.name)
    if match:
        name, lid = match.groups()
        return lid if lid in lights and lights[lid].get("name") == name else None
    matches = [lid for lid, info in lights.items() if info.get("name") == ref.name]
    return matches[0] if len(matches) == 1 else None


@dataclass
class Desired:
    dt: datetime
    which: str
    schedule: Schedule
    space: Space
    order: tuple

    @property
    def state(self):
        return self.schedule.start_state if self.which == "start" else self.schedule.end_state

    @property
    def token(self):
        return (self.schedule.id, self.schedule.revision, self.dt.isoformat(), self.which,
                json.dumps(self.state.payload(), sort_keys=True))


def desired_by_space(project: Project, now: datetime) -> dict[str, Desired]:
    result = {}
    spaces = {s.id: s for s in project.spaces}
    for idx, schedule in enumerate(project.schedules):
        boundary = schedule.boundary(now)
        if not boundary:
            continue
        dt, which = boundary
        order = (dt, idx, int(which == "end"))
        for sid in schedule.spaces:
            if sid in spaces and (sid not in result or order > result[sid].order):
                result[sid] = Desired(dt, which, schedule, spaces[sid], order)
    return result


def desired_by_light(project: Project, lights: dict, now: datetime):
    result, missing = {}, []
    for job in desired_by_space(project, now).values():
        if not job.space.lights:
            missing.append(f"{job.space.name}: no lights assigned")
        for ref in job.space.lights:
            lid = resolve_ref(ref, lights)
            if lid is None:
                missing.append(f"{job.space.name}: cannot find '{ref.name}'")
            elif lid not in result or job.order > result[lid].order:
                result[lid] = job
    return result, missing


class ScheduleEngine:
    
    def __init__(self, app):
        self.app = app
        self.applied: dict[str, tuple] = {}
        self.retries: dict[str, tuple] = {}
        self.warned: set[str] = set()

    def invalidate(self):
        self.applied.clear()
        self.retries.clear()
        self.warned.clear()

    def manual_override(self, lids):
        jobs, _ = desired_by_light(self.app.project, self.app.lights, datetime.now())
        for lid in lids:
            if lid in jobs:
                self.applied[lid] = jobs[lid].token
            self.retries.pop(lid, None)

    async def tick(self, now: Optional[datetime] = None):
        a = self.app
        if not a.project.scheduler_enabled or not a.connected or a.closing:
            return
        jobs, missing = desired_by_light(a.project, a.lights, now or datetime.now())
        for msg in missing:
            if msg not in self.warned:
                a.log("Schedule not applied — " + msg, "warning")
                self.warned.add(msg)
        self.warned.intersection_update(missing)
        for lid in list(jobs):
            async with a.io_lock:
                if not a.project.scheduler_enabled or not a.connected or a.closing:
                    return
                # Re-evaluate after acquiring the lock: the user or clock may have changed.
                current, _ = desired_by_light(a.project, a.lights, now or datetime.now())
                job = current.get(lid)
                if job is None or self.applied.get(lid) == job.token:
                    continue
                retry = self.retries.get(lid)
                if retry and retry[0] == job.token and time.monotonic() < retry[1]:
                    continue
                sent_token = job.token
                sent_state = copy.deepcopy(job.state)
                try:
                    updated = await asyncio.to_thread(a.hue.set_state, lid, sent_state)
                    a.lights[lid] = updated
                    self.applied[lid] = sent_token
                    self.retries.pop(lid, None)
                    a.log(f"{job.schedule.name} · {job.which.upper()} → {updated.get('name', lid)}: {job.state.summary()}")
                    a.live_dirty = True
                except Exception as ex:
                    attempts = retry[2] + 1 if retry and retry[0] == job.token else 1
                    delay = [2, 5, 10, 20, 120][min(attempts - 1, 4)]
                    self.retries[lid] = (sent_token, time.monotonic() + delay, attempts)
                    a.log(f"Schedule command failed: {job.space.name} · {ex}. Recheck in {delay}s.", "warning")
            if now is None:
                await asyncio.sleep(.12)  # Modest pacing for individual-light Hue commands.
        # Do not retain failed work for removed lights/schedules forever.
        self.retries = {k: v for k, v in self.retries.items() if k in jobs}


class WorkspaceLock:
    """Prevent two instances of THIS edition from writing/running the same workspace."""
    def __init__(self, path: Path):
        self.path, self.handle = path, None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0)
        if self.path.stat().st_size == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            self.handle.close()
            self.handle = None
            raise RuntimeError("This Lighthouse workspace is already open. Close the other Flet instance first.") from None
        return self

    def release(self):
        if self.handle:
            self.handle.close()
            self.handle = None

#flet UI helpers API 1.0.0
def text(value, size=14, color=TEXT, weight=None, **kwargs):
    return ft.Text(str(value), size=size, color=color, weight=weight, **kwargs)


def small(value):
    return text(value, 12, MUTED)


def button(label, handler=None, icon=None, primary=False, danger=False, **kwargs):
    return ft.Button(content=label, icon=icon, on_click=handler,
                     bgcolor=ACCENT if primary else PANEL_2,
                     color=BG if primary else (DANGER if danger else TEXT),
                     style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=12),
                                          padding=ft.Padding.symmetric(horizontal=16, vertical=13)),
                     **kwargs)


def pill(label, color=ACCENT):
    return ft.Container(content=text(label, 11, color, ft.FontWeight.W_600),
                        padding=ft.Padding.symmetric(horizontal=11, vertical=6),
                        border_radius=20, bgcolor=PANEL_2)


def card(content, **kwargs):
    kwargs.setdefault("padding", 22)
    return ft.Container(content=content, bgcolor=PANEL, border=ft.Border.all(1, BORDER),
                        border_radius=20, **kwargs)


def input_border():
    return {
        ft.ControlState.DEFAULT: ft.OutlineInputBorder(border_radius=12, side=ft.BorderSide(1, BORDER)),
        ft.ControlState.FOCUSED: ft.OutlineInputBorder(border_radius=12, side=ft.BorderSide(2, ACCENT)),
    }


def field(label, value="", **kwargs):
    # FormFieldControl in Flet 1.0 uses helper/bgcolor, not helper_text/fill_color. bummer.
    if "helper_text" in kwargs:
        kwargs["helper"] = kwargs.pop("helper_text")
        kwargs.setdefault("helper_max_lines", 3)
    return ft.TextField(label=label, value=str(value), color=TEXT, border=input_border(),
                        filled=True, bgcolor=BG, text_size=14, **kwargs)


def dropdown(label, value, options, **kwargs):
    return ft.Dropdown(label=label, value=value,
                       options=[ft.DropdownOption(key=k, text=v) for k, v in options],
                       border=input_border(), filled=True, fill_color=BG, color=TEXT,
                       text_size=14, **kwargs)


def kind_icon(kind):
    return {"Room": ft.Icons.MEETING_ROOM_OUTLINED, "Box": ft.Icons.INVENTORY_2_OUTLINED,
            "Cage": ft.Icons.GRID_VIEW_ROUNDED, "Compartment": ft.Icons.VIEW_QUILT_OUTLINED
            }.get(kind, ft.Icons.SPACE_DASHBOARD_OUTLINED)


class StateEditor:
    """Reusable form, with exactly one colour mode selected at a time."""
    def __init__(self, app, title: str, initial: Optional[LightState] = None):
        self.app = app
        state = copy.deepcopy(initial or LightState(preset="white"))
        mode = "preset" if state.preset else "wavelength" if state.wavelength_nm is not None else (
            "xy" if state.xy is not None else "ct" if state.ct is not None else "keep")
        self.action = dropdown("Action", state.action, [("set", "SET · brightness + colour"),
                               ("on", "ON · keep previous colour"), ("off", "OFF")], on_select=self.changed)
        self.use_bri = ft.Checkbox(label="Set brightness", value=state.bri is not None, on_change=self.changed)
        self.bri = field("Brightness · 0–254", state.bri if state.bri is not None else 160,
                         width=155, on_change=self.entry_changed)
        self.bri_slider = ft.Slider(min=0, max=254, divisions=254,
                                    value=state.bri if state.bri is not None else 160,
                                    label="{value}", active_color=ACCENT, expand=True,
                                    on_change=self.slider_changed)
        self.brightness_row = ft.Row([self.bri_slider, self.bri])
        self.mode = dropdown("Colour mode", mode, [("keep", "Keep existing colour"), ("preset", "Colour preset"),
                              ("ct", "Colour temperature (CT)"), ("xy", "XY coordinates"),
                              ("wavelength", "Approximate wavelength colour")], on_select=self.changed)
        self.preset = dropdown("Preset", state.preset or "white", [(k, k.title()) for k in PRESET_XY])
        self.ct = field("Colour temperature · mired (153–500)", state.ct if state.ct is not None else 370,
                        helper_text="CT is in mired, not Kelvin. Supported range also depends on the lamp.")
        self.xy = field("XY coordinates · x,y", f"{state.xy[0]},{state.xy[1]}" if state.xy else "0.3127,0.3290")
        self.wavelength = field("Approximate wavelength colour · nm", state.wavelength_nm or 470,
                                helper_text="380–700 nm visual approximation; not monochromatic or calibrated output.")
        self.transition = field("Transition · seconds", f"{state.transitiontime / 10:g}",
                               helper_text="0 = immediate; 0.4 s matches your original default.")
        self.hint = small("")
        swatches = []
        for name, colour in SWATCHES.items():
            swatches.append(ft.Container(content=text(name.title(), 11, BG if name in ("white", "warm", "cool", "green") else BG,
                                                        ft.FontWeight.W_600),
                                          bgcolor=colour, border_radius=10, padding=10,
                                          on_click=self.choose_preset(name)))
        self.swatches = ft.Row(swatches, wrap=True, spacing=7, run_spacing=7)
        self.content = ft.Column([text(title, 18, weight=ft.FontWeight.W_600), self.action, self.hint,
                                  self.use_bri, self.brightness_row, self.mode, self.swatches,
                                  self.preset, self.ct, self.xy, self.wavelength, self.transition], spacing=13)
        self.changed(update=False)

    def choose_preset(self, name):
        def select(e):
            self.action.value, self.mode.value, self.preset.value = "set", "preset", name
            self.changed()
        return select

    def slider_changed(self, e):
        self.bri.value = str(round(self.bri_slider.value))
        self.app.update()

    def entry_changed(self, e):
        try:
            self.bri_slider.value = number(self.bri.value, "Brightness", 0, 254, True)
            self.app.update()
        except ValueError:
            pass  # Allow partial typing. Apply/Save performs strict validation.

    def changed(self, e=None, update=True):
        off, colour = self.action.value == "off", self.action.value == "set"
        self.use_bri.disabled = off
        self.brightness_row.disabled = off or not self.use_bri.value
        self.mode.disabled = not colour
        self.swatches.visible = colour and self.mode.value == "preset"
        for name, control in [("preset", self.preset), ("ct", self.ct), ("xy", self.xy), ("wavelength", self.wavelength)]:
            control.visible = colour and self.mode.value == name
        self.hint.value = ("Turns lights off. Colour and brightness entries are ignored." if off else
                           "ON preserves the lamp's existing colour. Use SET when a protocol changes colour."
                           if not colour else "SET turns lights on and applies the selected brightness and colour.")
        if update:
            self.app.update()

    def value(self) -> LightState:
        tt = number(self.transition.value, "Transition seconds", 0, 6553.5)
        if abs(tt * 10 - round(tt * 10)) > .0001:
            raise ValueError("Transition must use steps of 0.1 seconds.")
        s = LightState(action=self.action.value, bri=None, transitiontime=round(tt * 10))
        if s.action != "off" and self.use_bri.value:
            s.bri = number(self.bri.value, "Brightness", 0, 254, True)
        if s.action == "set":
            mode = self.mode.value
            if mode == "preset":
                s.preset = self.preset.value
            elif mode == "ct":
                s.ct = number(self.ct.value, "CT", 153, 500, True)
            elif mode == "xy":
                s.xy = parse_xy(self.xy.value)
                if s.xy is None:
                    raise ValueError("Enter XY coordinates.")
            elif mode == "wavelength":
                s.wavelength_nm = number(self.wavelength.value, "Wavelength", 380, 700)
        return s.validate()


class LighthouseApp:
    def __init__(self, page, workspace: Path, demo=False):
        self.page, self.workspace, self.demo = page, workspace, demo
        self.hue = HueController(demo=demo)
        self.project = Project()
        self.lights = {}
        self.connected, self.closing = False, False
        self.io_lock, self.save_lock = asyncio.Lock(), asyncio.Lock()
        self.engine = ScheduleEngine(self)
        self.view, self.space_id = "overview", None
        self.live_updaters = []
        self.live_dirty, self.logs_dirty, self.save_pending = True, True, False
        self.logs = []
        self.manual_busy = False
        self.connection_wanted = False
        self.failures = 0
        self.last_refresh = 0.
        self.saved_at = ""
        self.save_error = ""
        self.ip_field = None
        self.activity_list = None
        self.busy_count = 0
        self.startup_warnings = []
        self.logger = logging.getLogger("lighthouse." + str(id(self)))
        self.logger.setLevel(logging.INFO)
        try:
            self.workspace.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(self.workspace.with_suffix(".log"), maxBytes=2_000_000,
                                           backupCount=3, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            self.logger.addHandler(handler)
        except OSError:
            self.logger.addHandler(logging.StreamHandler())
        if workspace.exists():
            try:
                self.project, self.startup_warnings = Project.from_dict(json.loads(workspace.read_text(encoding="utf-8-sig")))
            except Exception as ex:
                # Do NOT overwrite a broken workspace by starting a fresh autosave.
                raise RuntimeError(f"Could not load {workspace}. Your file has not been changed.\n\n{ex}\n\n"
                                   "Inspect the file or start with --workspace followed by a new JSON path.") from ex
        if demo and workspace.exists() and (self.project.bridge_id not in ("", "DEMO") or
                                            self.project.bridge_ip not in ("", "demo")):
            raise RuntimeError("Demo mode will not overwrite a real Bridge workspace. Run --demo without --workspace "
                               "to use the separate demo workspace.")
        if demo:
            self.connected, self.connection_wanted = True, False
            self.lights = copy.deepcopy(self.hue.lights)
            self.project.bridge_ip, self.project.bridge_id = "demo", "DEMO"
            if not self.project.spaces:
                for i, (name, kind) in enumerate([("Observation room", "Room"), ("Day compartment", "Compartment"),
                                                ("Work bench", "Space"), ("Cage A", "Cage")]):
                    lids = [str(2 * i + 1), str(2 * i + 2)]
                    self.project.spaces.append(Space(name=name, kind=kind,
                                                      lights=[make_ref(lid, self.lights[lid]) for lid in lids]))
                today = date.today().isoformat()
                self.project.schedules = [Schedule(name="Day / night cycle", spaces=[s.id for s in self.project.spaces[:2]],
                                                   start_date=today, end_date=today, start_time="06:00", end_time="18:00",
                                                   start_state=LightState(preset="white", bri=200),
                                                   end_state=LightState(preset="red", bri=40)).validate()]
            self.project.scheduler_enabled = False  # Preview never arms itself.
        self._build_shell()

    def bind(self, fn: Callable, *args, **kwargs):
        async def handler(e=None):
            try:
                result = fn(*args, **kwargs)
                if inspect.isawaitable(result):
                    await result
            except Exception as ex:
                self.log(str(ex), "error")
                self.notice(str(ex), error=True)
            self.update()
        return handler

    def update(self):
        if self.closing:
            return
        try:
            self.page.update()
        except Exception:
            # A client may disappear while an HTTP request is finishing.
            self.logger.exception("UI update could not be delivered")

    def log(self, message, level="info"):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.logs.append((stamp, level, str(message)))
        self.logs = self.logs[-1500:]
        getattr(self.logger, level if level in ("info", "warning", "error") else "info")(message)
        self.logs_dirty = True
        if hasattr(self, "last_event"):
            self.last_event.value = f"{stamp}  {message}"

    def notice(self, message, error=False):
        # Use a dialog for errors: unlike a short toast, details remain readable.
        if error:
            self.show_dialog("Please check", ft.Container(content=text(message), width=540),
                             [button("Close", self.bind(self.close_dialog))])
        else:
            self.page.show_dialog(ft.SnackBar(content=text(message, color=BG), bgcolor=ACCENT,
                                              duration=4500, show_close_icon=True))

    def show_dialog(self, title, content, actions, width=None):
        if width:
            content = ft.Container(content=content, width=width)
        dialog = ft.AlertDialog(title=text(title, 24, weight=ft.FontWeight.W_600),
                                content=content, actions=actions, modal=True,
                                bgcolor=PANEL, shape=ft.RoundedRectangleBorder(radius=22),
                                actions_alignment=ft.MainAxisAlignment.END,
                                scrollable=False)
        self.page.show_dialog(dialog)
        self.update()
        return dialog

    def close_dialog(self):
        self.page.pop_dialog()
        self.update()

    def confirm(self, title, message, on_yes, yes_label="Confirm", danger=False):
        async def yes():
            self.close_dialog()
            result = on_yes()
            if inspect.isawaitable(result):
                await result
        self.show_dialog(title, ft.Container(content=text(message), width=560),
                         [button("Cancel", self.bind(self.close_dialog)),
                          button(yes_label, self.bind(yes), primary=not danger, danger=danger)])

    def persist(self):
        self.save_pending = True
        if hasattr(self, "save_label"):
            self.save_label.value = "Saving…"

    async def save_workspace(self):
        async with self.save_lock:
            self.save_pending = False
            payload = json.dumps(self.project.to_dict(), ensure_ascii=False, indent=2)
            try:
                await asyncio.to_thread(atomic_write, self.workspace, payload)
                self.saved_at = datetime.now().strftime("%H:%M:%S")
                self.save_error = ""
            except Exception as ex:
                self.save_error = str(ex)
                self.log(f"AUTOSAVE FAILED: {ex}. Export a project backup from Bridge & files.", "error")
            if hasattr(self, "save_label"):
                self.save_label.value = "Autosave failed · see Activity" if self.save_error else "Saved " + self.saved_at
                self.save_label.color = DANGER if self.save_error else MUTED

    def _build_shell(self):
        p = self.page
        p.title = "Lighthouse 💡" + (" · DEMO" if self.demo else "")
        p.bgcolor, p.padding, p.spacing = BG, 0, 0
        p.theme_mode = ft.ThemeMode.DARK
        p.theme = ft.Theme(color_scheme_seed=ACCENT, use_material3=True)
        if not p.web:
            p.window.width, p.window.height = 1360, 900
            p.window.min_width, p.window.min_height = 900, 640
            p.window.prevent_close = True
            p.window.on_event = self.on_window_event
        p.on_resize = self.on_resize
        p.on_close = self.on_session_close
        self.file_picker = ft.FilePicker()
        p.services.append(self.file_picker)
        self.bridge_badge = text("Not connected", 12, MUTED)
        self.bridge_dot = ft.Container(width=8, height=8, border_radius=4, bgcolor=MUTED)
        self.scheduler_badge = text("Scheduler ready", 12, MUTED)
        self.clock = text(datetime.now().strftime("%a %d %b  ·  %H:%M:%S"), 12, MUTED)
        self.progress = ft.ProgressBar(height=2, color=ACCENT, bgcolor=BG, visible=False)
        header = ft.Container(padding=ft.Padding.symmetric(horizontal=26, vertical=20),
            border=ft.Border(bottom=ft.BorderSide(1, BORDER)),
            content=ft.Row([
                ft.Container(content=ft.Icon(ft.Icons.LIGHTBULB_OUTLINE_ROUNDED, color=BG, size=27),
                             width=46, height=46, alignment=ft.Alignment.CENTER, bgcolor=ACCENT, border_radius=14),
                ft.Column([text("Lighthouse", 23, weight=ft.FontWeight.W_600), small("SPACES  /  LIGHT CONTROL")], spacing=1),
                ft.Container(expand=True),
                pill("DEMO · no real lights", SUCCESS) if self.demo else ft.Container(),
                ft.Container(content=ft.Row([self.bridge_dot, self.bridge_badge], spacing=8),
                             padding=12, bgcolor=PANEL, border_radius=12,
                             on_click=self.bind(self.go, "settings")),
                self.clock,
            ], spacing=14))
        self.sidebar = ft.Container(width=214, bgcolor=BG, padding=ft.Padding.only(left=14, right=14, top=20),
                                    border=ft.Border(right=ft.BorderSide(1, BORDER)))
        self.body = ft.Container(expand=True, padding=ft.Padding.symmetric(horizontal=28, vertical=24))
        self.last_event = text("Ready. Create spaces, connect the Bridge, then assign your lights.", 11, MUTED,
                               max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, expand=True)
        self.save_label = text("Autosave on", 11, MUTED)
        footer = ft.Container(padding=ft.Padding.symmetric(horizontal=22, vertical=10), bgcolor=BG,
            border=ft.Border(top=ft.BorderSide(1, BORDER)),
            content=ft.Row([self.last_event, self.scheduler_badge, self.save_label], spacing=20))
        p.add(ft.Column([header, self.progress,
                         ft.Row([self.sidebar, self.body], expand=True, spacing=0,
                                vertical_alignment=ft.CrossAxisAlignment.STRETCH), footer], expand=True, spacing=0))
        self.go("overview")

    def render_sidebar(self):
        compact = (self.page.width or 1360) < 1050
        self.sidebar.width = 76 if compact else 214
        items = [("overview", "Overview", ft.Icons.DASHBOARD_OUTLINED),
                 ("spaces", "Spaces", ft.Icons.SPACE_DASHBOARD_OUTLINED),
                 ("lights", "All lights", ft.Icons.LIGHTBULB_OUTLINE_ROUNDED),
                 ("schedules", "Schedules", ft.Icons.CALENDAR_MONTH_OUTLINED),
                 ("settings", "Bridge & files", ft.Icons.SETTINGS_OUTLINED),
                 ("activity", "Activity", ft.Icons.RECEIPT_LONG_OUTLINED)]
        controls = []
        for key, label, icon in items:
            selected = self.view == key or (key == "spaces" and self.view == "space_detail")
            colour = ACCENT if selected else MUTED
            row = [ft.Icon(icon, color=colour, size=23)]
            if not compact:
                row.append(text(label, 14, colour, ft.FontWeight.W_600 if selected else None))
            controls.append(ft.Container(content=ft.Row(row, spacing=13),
                                           padding=14, border_radius=13, bgcolor=PANEL_2 if selected else BG,
                                           tooltip=label, on_click=self.bind(self.go, key)))
        controls += [ft.Container(expand=True)]
        if not compact:
            controls.append(ft.Container(content=ft.Column([text("LOCAL CONTROL", 11, SUCCESS, ft.FontWeight.W_600),
                                                             small("Keep this app open and your computer awake.")], spacing=8),
                                           bgcolor=PANEL, border_radius=14, padding=14))
            controls.append(ft.TextButton(content=text("Dev by Hamid Taghipourbibalan", 10, MUTED),
                                           url="https://www.linkedin.com/in/hamid-taghipourbibalan-b7239088/"))
        self.sidebar.content = ft.Column(controls, spacing=6, expand=True)

    def go(self, view, space_id=None):
        self.view, self.space_id = view, space_id
        self.live_updaters, self.activity_list = [], None
        self.ip_field = None
        builders = {"overview": self.overview_view, "spaces": self.spaces_view, "lights": self.lights_view,
                    "schedules": self.schedules_view, "settings": self.settings_view, "activity": self.activity_view,
                    "space_detail": lambda: self.space_detail_view(space_id)}
        self.body.content = builders.get(view, self.overview_view)()
        self.render_sidebar()
        self.update_status()
        self.update()

    def on_resize(self, e):
        self.clock.visible = (self.page.width or 1360) >= 1050
        self.render_sidebar()
        self.update()

    def screen(self, controls):
        return ft.Column(controls, expand=True, scroll=ft.ScrollMode.AUTO, spacing=20)

    def heading(self, title, subtitle, actions=None):
        buttons = actions if isinstance(actions, (list, tuple)) else ([actions] if actions is not None else [])
        controls = [ft.Column([text(title, 30, weight=ft.FontWeight.W_600), text(subtitle, 13, MUTED)], spacing=5)]
        if buttons:
            controls.append(ft.Row(buttons, spacing=8, wrap=True, run_spacing=8))
        return ft.Column(controls, spacing=16)

    def empty(self, title, subtitle, action=None, icon=None):
        if isinstance(action, str):
            action, icon = button(action, icon, primary=True), None
        return ft.Container(
            content=ft.Column([ft.Icon(icon or ft.Icons.SPACE_DASHBOARD_OUTLINED, color=ACCENT, size=44),
                               text(title, 24, weight=ft.FontWeight.W_600),
                               ft.Container(content=text(subtitle, 14, MUTED, text_align=ft.TextAlign.CENTER), width=610),
                               action or ft.Container()], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=20),
            bgcolor=PANEL, border_radius=24, border=ft.Border.all(1, BORDER), padding=42)

    def stat(self, title, value_fn, note, icon):
        val = text(value_fn(), 31, weight=ft.FontWeight.W_600)
        self.live_updaters.append(lambda: setattr(val, "value", str(value_fn())))
        return card(ft.Column([ft.Row([small(title.upper()), ft.Container(expand=True), ft.Icon(icon, size=20, color=ACCENT)]),
                                val, small(note)], spacing=8), col={"xs": 12, "sm": 6, "lg": 3})

    def overview_view(self):
        items = [self.heading("Your light environment", "Organise spaces. Set the light. Keep your protocols in view.",
                               [button("Create spaces", self.bind(self.create_spaces_dialog), ft.Icons.ADD, primary=True)])]
        if not self.project.spaces:
            items.append(self.empty("Start with your spaces", "Create rooms, boxes, cages or compartments, give them meaningful names, "
                                    "then assign one or more Hue lights to each space. Nothing is created automatically.",
                                    button("Create my first spaces", self.bind(self.create_spaces_dialog), ft.Icons.ADD, primary=True)))
            items.append(card(ft.Column([text("Already use Lighthouse?", 18, weight=ft.FontWeight.W_600),
                                          small("Import the JSON configuration saved by your old GUI. Its boxes become spaces that you can rename."),
                                          button("Import existing configuration", self.bind(self.import_project), ft.Icons.UPLOAD_FILE)], spacing=12)))
            return self.screen(items)
        items.append(ft.ResponsiveRow([
            self.stat("Spaces", lambda: len(self.project.spaces), "Named for the way you work", ft.Icons.SPACE_DASHBOARD_OUTLINED),
            self.stat("Assigned lights", lambda: len({r.key for s in self.project.spaces for r in s.lights}), "One or more per space", ft.Icons.LIGHTBULB_OUTLINE_ROUNDED),
            self.stat("Lights on", lambda: sum(bool(l.get("state", {}).get("on")) for l in self.lights.values()) if self.connected else "—",
                      "Latest Bridge-reported state", ft.Icons.WB_SUNNY_OUTLINED),
            self.stat("Enabled schedules", lambda: sum(s.enabled for s in self.project.schedules),
                      "Local scheduler · app must stay open", ft.Icons.SCHEDULE)], spacing=16, run_spacing=16))
        if not self.connected:
            items.append(card(ft.Row([ft.Icon(ft.Icons.LINK_OFF, color=ACCENT),
                                        ft.Column([text("Connect your Hue Bridge", 17, weight=ft.FontWeight.W_600),
                                                   small("Your spaces are saved. Connect to discover lights and assign them.")], expand=True),
                                        button("Bridge setup", self.bind(self.go, "settings"), ft.Icons.ARROW_FORWARD)])))
        items.append(ft.Row([text("Spaces", 21, weight=ft.FontWeight.W_600), ft.Container(expand=True),
                              button("Manage spaces", self.bind(self.go, "spaces"), ft.Icons.ARROW_FORWARD)]))
        items.append(ft.ResponsiveRow([self.space_card(s) for s in self.project.spaces], spacing=16, run_spacing=16))
        items.append(self.upcoming_panel())
        return self.screen(items)

    def space_status(self, space):
        if not space.lights:
            return "No lights assigned", MUTED
        if not self.connected:
            return "Bridge not connected", MUTED
        ids = [resolve_ref(r, self.lights) for r in space.lights]
        missing = sum(lid is None for lid in ids)
        states = [self.lights[lid].get("state", {}) for lid in ids if lid]
        unreachable = sum(st.get("reachable") is False for st in states)
        if missing or unreachable:
            return f"{missing + unreachable} unavailable · check lights", DANGER
        on = sum(bool(st.get("on")) for st in states)
        return ("All lights off", MUTED) if on == 0 else (
            ("All lights on", SUCCESS) if on == len(states) else (f"{on} of {len(states)} on", ACCENT))

    def space_card(self, space):
        status, colour = self.space_status(space)
        label = text(status, 12, colour)
        enabled = sum(s.enabled and space.id in s.spaces for s in self.project.schedules)
        light_names = ", ".join(r.name or "Unknown light" for r in space.lights) or "Choose lights for this space"
        def live():
            label.value, label.color = self.space_status(space)
            for power_button in content.controls[-1].controls[1:]:
                power_button.disabled = not self.connected or not space.lights
        self.live_updaters.append(live)
        content = ft.Column([
            ft.Row([ft.Container(content=ft.Icon(kind_icon(space.kind), color=ACCENT, size=25), bgcolor=PANEL_2,
                                  padding=12, border_radius=14), ft.Container(expand=True), pill(space.kind, MUTED),
                     ft.IconButton(icon=ft.Icons.EDIT_OUTLINED, icon_color=MUTED, tooltip="Rename / edit space",
                                    on_click=self.bind(self.edit_space_dialog, space.id))]),
            ft.Container(content=text(space.name, 21, weight=ft.FontWeight.W_600, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
                         on_click=self.bind(self.go, "space_detail", space.id)),
            text(light_names, 12, MUTED, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
            label,
            ft.Row([small(f"{len(space.lights)} light{'s' if len(space.lights) != 1 else ''}"),
                     ft.Container(expand=True), small(f"{enabled} schedule{'s' if enabled != 1 else ''}")]),
            ft.Divider(color=BORDER, height=1),
            ft.Row([button("Open space", self.bind(self.go, "space_detail", space.id), expand=True),
                     ft.IconButton(icon=ft.Icons.POWER_SETTINGS_NEW, icon_color=SUCCESS, tooltip="Turn space ON",
                                    on_click=self.bind(self.space_command, space.id, LightState(action="on", bri=None)),
                                    disabled=not self.connected or not space.lights),
                     ft.IconButton(icon=ft.Icons.POWER_OFF_OUTLINED, icon_color=MUTED, tooltip="Turn space OFF",
                                    on_click=self.bind(self.space_command, space.id, LightState(action="off", bri=None)),
                                    disabled=not self.connected or not space.lights)], spacing=5),
        ], spacing=15)
        return card(content, col={"xs": 12, "md": 6, "xl": 4})

    def spaces_view(self):
        listing = ft.ResponsiveRow([self.space_card(s) for s in self.project.spaces], spacing=16, run_spacing=16)
        query = field("Find a space", "", prefix_icon=ft.Icons.SEARCH)
        def search(e):
            q = (query.value or "").casefold()
            # Keep existing cards/controls: changing visibility does not discard user edits.
            for s, control in zip(self.project.spaces, listing.controls):
                control.visible = q in (s.name + " " + s.kind).casefold()
            self.update()
        query.on_change = search
        return self.screen([self.heading("Spaces", "Use your own names. Add spaces individually or create a numbered batch.",
                                           [button("Create spaces", self.bind(self.create_spaces_dialog), ft.Icons.ADD, primary=True)]),
                            query, listing if self.project.spaces else self.empty("No spaces yet", "Start with a room, a box, a cage or a compartment.",
                                                                                button("Create spaces", self.bind(self.create_spaces_dialog), primary=True))])

    def space(self, sid):
        found = next((s for s in self.project.spaces if s.id == sid), None)
        if not found:
            raise ValueError("This space no longer exists.")
        return found

    def space_detail_view(self, sid):
        space = self.space(sid)
        editor = StateEditor(self, "Light settings", LightState(preset="white"))
        status = text(self.space_status(space)[0], 13, self.space_status(space)[1])
        self.live_updaters.append(lambda: (setattr(status, "value", self.space_status(space)[0]),
                                          setattr(status, "color", self.space_status(space)[1])))
        controls = [button("All spaces", self.bind(self.go, "spaces"), ft.Icons.ARROW_BACK),
            self.heading(space.name, f"{space.kind} · {len(space.lights)} assigned light(s)",
                         [button("Rename / edit", self.bind(self.edit_space_dialog, sid), ft.Icons.EDIT_OUTLINED),
                          button("Assign lights", self.bind(self.assign_dialog, sid), ft.Icons.ADD_LINK, primary=True)]),
            card(ft.Column([status, text(", ".join(r.name for r in space.lights) or "No lights assigned", 14),
                             small(space.notes) if space.notes else ft.Container()], spacing=10)),
            ft.ResponsiveRow([
                card(ft.Column([editor.content,
                     ft.Row([button("Apply settings", self.bind(self.apply_editor_to_space, sid, editor), ft.Icons.CHECK, primary=True),
                             button("ON", self.bind(self.space_command, sid, LightState(action="on", bri=None))),
                             button("OFF", self.bind(self.space_command, sid, LightState(action="off", bri=None)))], wrap=True),
                     small("Manual settings remain until the next schedule boundary or an explicit schedule sync.")], spacing=18),
                     col={"xs": 12, "lg": 7}),
                card(ft.Column([text("Schedules for this space", 19, weight=ft.FontWeight.W_600),
                                 *[self.schedule_mini(s) for s in self.project.schedules if sid in s.spaces],
                                 button("Add schedule", self.bind(self.schedule_dialog, None, sid), ft.Icons.ADD),
                                 ft.Divider(color=BORDER),
                                 small("Removing a space does not switch its lamps off or remove them from the Bridge."),
                                 button("Remove space", self.bind(self.remove_space, sid), ft.Icons.DELETE_OUTLINE, danger=True)], spacing=18),
                     col={"xs": 12, "lg": 5})], spacing=18, run_spacing=18)]
        return self.screen(controls)

    @staticmethod
    def valid_name(raw):
        out = str(raw or "").strip()
        if not out or len(out) > 80 or "|" in out or "\n" in out:
            raise ValueError("Use a name of 1–80 characters, without line breaks or |.")
        return out

    def create_spaces_dialog(self):
        kind = dropdown("Space type", "Room", [(k, k) for k in KINDS], width=220)
        prefix = field("Name or naming prefix", "Room")
        count = field("How many spaces?", 4, width=210)
        start = field("Start numbering at", 1, width=210)
        preview = text("Room 1, Room 2, Room 3, Room 4", 13, ACCENT)
        error = text("", 12, DANGER)
        def names():
            n = number(count.value, "Number of spaces", 1, 512, True)
            first = number(start.value, "First number", 1, 9999, True)
            base = self.valid_name(prefix.value)
            return [base] if n == 1 else [self.valid_name(f"{base} {i}") for i in range(first, first + n)]
        def preview_change(e=None):
            try:
                proposed = names()
                preview.value = ", ".join(proposed[:6]) + (f" … +{len(proposed)-6} more" if len(proposed) > 6 else "")
                start.disabled = len(proposed) == 1
                error.value = ""
            except ValueError as ex:
                error.value = str(ex)
            self.update()
        def type_change(e):
            if prefix.value in KINDS:
                prefix.value = kind.value
            preview_change()
        kind.on_select = type_change
        prefix.on_change = count.on_change = start.on_change = preview_change
        def create():
            try:
                proposed = names()
                existing = {s.name.casefold() for s in self.project.spaces}
                clashes = [n for n in proposed if n.casefold() in existing]
                if clashes:
                    raise ValueError("Already exists: " + ", ".join(clashes[:5]) + ". Change the prefix or starting number.")
                if len(self.project.spaces) + len(proposed) > 512:
                    raise ValueError("Maximum 512 spaces in one workspace.")
                self.project.spaces.extend(Space(name=n, kind=kind.value) for n in proposed)
                self.persist()
                self.close_dialog()
                self.log(f"Created {len(proposed)} {kind.value.lower()} space(s). Assign lights from any space card.")
                self.go("spaces")
            except ValueError as ex:
                error.value = str(ex)
                self.update()
        self.show_dialog("Create your spaces", ft.Container(width=620, content=ft.Column([
            text("Choose a type and your own names. You can mix different types and rename any space later.", 14, MUTED),
            kind, prefix, ft.Row([count, start], wrap=True), small("With a count of 1, the exact name is used without a number."),
            ft.Divider(color=BORDER), small("PREVIEW"), preview, error], spacing=17, tight=True)),
            [button("Cancel", self.bind(self.close_dialog)), button("Create spaces", self.bind(create), ft.Icons.ADD, primary=True)])

    def edit_space_dialog(self, sid):
        s = self.space(sid)
        name = field("Space name", s.name)
        options = list(dict.fromkeys(KINDS + [s.kind]))
        kind = dropdown("Space type", s.kind, [(k, k) for k in options])
        notes = field("Notes (optional)", s.notes, multiline=True, min_lines=2, max_lines=4)
        error = text("", 12, DANGER)
        def save():
            try:
                value = self.valid_name(name.value)
                if any(x.id != sid and x.name.casefold() == value.casefold() for x in self.project.spaces):
                    raise ValueError("Another space already uses this name.")
                old = s.name
                s.name, s.kind, s.notes = value, kind.value, notes.value or ""
                self.persist()
                self.close_dialog()
                self.log(f"Updated space: {old} → {s.name}. Assignments and schedule links preserved.")
                self.go(self.view, self.space_id)
            except ValueError as ex:
                error.value = str(ex)
                self.update()
        self.show_dialog("Edit space", ft.Container(width=540, content=ft.Column([name, kind, notes,
                              small("Renaming never changes the space's internal ID or its schedule links."), error], spacing=16, tight=True)),
                         [button("Cancel", self.bind(self.close_dialog)), button("Save changes", self.bind(save), primary=True)])

    def remove_space(self, sid):
        space = self.space(sid)
        related = sum(sid in s.spaces for s in self.project.schedules)
        def remove():
            self.project.spaces = [s for s in self.project.spaces if s.id != sid]
            # Delete only schedules that would otherwise have no targets. Inform before confirmation to ensure the user understands the implications.
            kept = []
            for s in self.project.schedules:
                s.spaces = [x for x in s.spaces if x != sid]
                if s.spaces:
                    kept.append(s)
            self.project.schedules = kept
            self.persist()
            self.log(f"Removed space '{space.name}'. No light command was sent.")
            self.go("spaces")
        self.confirm("Remove " + space.name + "?", f"This removes the space and its assignments, not the physical lights. "
                     f"It is targeted by {related} schedule(s). Those schedules lose this target; schedules with no remaining targets are deleted. "
                     "The lights retain their current state.", remove, "Remove space", danger=True)

    def assign_dialog(self, sid):
        if not self.connected:
            self.notice("Connect the Bridge under Bridge & files before assigning lights.", error=True)
            return
        s = self.space(sid)
        selected = {resolve_ref(r, self.lights) for r in s.lights}
        owners = {}
        for other in self.project.spaces:
            if other.id != sid:
                for ref in other.lights:
                    lid = resolve_ref(ref, self.lights)
                    if lid:
                        owners.setdefault(lid, []).append(other.name)
        choices, choice_rows = {}, []
        for lid, info in sorted(self.lights.items(), key=lambda kv: (kv[1].get("name", "").casefold(), kv[0])):
            name = info.get("name", lid)
            check = ft.Checkbox(label=f"{name}  ·  ID {lid}", value=lid in selected)
            choices[lid] = check
            extra = "Currently assigned to: " + ", ".join(owners[lid]) if lid in owners else (
                "Unreachable" if info.get("state", {}).get("reachable") is False else info.get("type", "Hue light"))
            choice_rows.append(ft.Container(content=ft.Column([check, small(extra)], spacing=0), padding=10,
                                            border_radius=12, bgcolor=PANEL_2))
        unresolved = [(r, ft.Checkbox(label=f"Keep missing assignment: {r.name}", value=True))
                      for r in s.lights if resolve_ref(r, self.lights) is None]
        move = ft.Checkbox(label="Move selected lights out of their other spaces", value=False)
        error = text("", 12, DANGER)
        def save():
            wanted = [lid for lid, cb in choices.items() if cb.value]
            conflicts = [lid for lid in wanted if lid in owners]
            if conflicts and not move.value:
                error.value = "Some selected lights belong to other spaces. Deselect them or enable the move option."
                self.update()
                return
            affected = {lid for r in s.lights if (lid := resolve_ref(r, self.lights)) is not None} | set(wanted)
            if move.value:
                for other in self.project.spaces:
                    if other.id != sid:
                        other.lights = [r for r in other.lights if resolve_ref(r, self.lights) not in wanted]
            s.lights = [make_ref(lid, self.lights[lid]) for lid in wanted] + [r for r, cb in unresolved if cb.value]
            # Reconcile changed assignments without wiping unrelated manual overrides.
            for lid in affected:
                self.engine.applied.pop(lid, None)
                self.engine.retries.pop(lid, None)
            self.engine.warned.clear()
            self.persist()
            self.close_dialog()
            self.log(f"Assigned {len(s.lights)} light(s) to '{s.name}'.")
            self.go("space_detail", sid)
        def all_choices(value):
            for lid, cb in choices.items():
                cb.value = value and lid not in owners
            self.update()
        self.show_dialog("Assign lights · " + s.name, ft.Container(width=670, content=ft.Column([
            text("Select one or more lights. A space may contain as many lamps as you need.", 14, MUTED),
            ft.Row([button("All available", self.bind(all_choices, True)), button("Clear", self.bind(all_choices, False))]),
            ft.Column(choice_rows + [cb for _, cb in unresolved], height=300, scroll=ft.ScrollMode.AUTO, spacing=7),
            move, small("An active schedule can affect newly assigned lamps immediately after saving."), error], spacing=13, tight=True)),
            [button("Cancel", self.bind(self.close_dialog)), button("Save assignments", self.bind(save), ft.Icons.CHECK, primary=True)])

    # manual control of individual lights or spaces, outside of schedules
    def lights_view(self):
        controls = [self.heading("All lights", "Live bridge inventory · control lamps individually or assign them to spaces.",
                                 button("Refresh lights", self.bind(self.refresh_lights), ft.Icons.REFRESH))]
        if not self.lights:
            controls.append(self.empty("Connect your Hue Bridge", "Your available lights will appear here after connecting.",
                                       "Bridge settings", self.bind(self.go, "settings")))
            return self.screen(controls)
        search = field("Find a light", prefix_icon=ft.Icons.SEARCH)
        items = []
        for lid, info in self.lights.items():
            status = small("")
            details = small("")
            def refresh(lid=lid, status=status, details=details):
                data = self.lights.get(lid, {})
                st = data.get("state", {})
                state = "Unreachable" if st.get("reachable") is False else "On" if st.get("on") else "Off"
                status.value = state if self.connected else "Offline · last known " + state.lower()
                status.color = DANGER if st.get("reachable") is False else SUCCESS if st.get("on") else MUTED
                owners = [s.name for s in self.project.spaces if any(resolve_ref(r, self.lights) == lid for r in s.lights)]
                details.value = (f"ID {lid}  ·  Brightness {st.get('bri', '—')}  ·  Mode {st.get('colormode', '—')}\n"
                                 f"CT {st.get('ct', '—')}  ·  XY {st.get('xy', '—')}\n"
                                 + ("Space: " + ", ".join(owners) if owners else "Not assigned to a space"))
            refresh()
            self.live_updaters.append(refresh)
            lamp = card(ft.Column([
                ft.Row([ft.Icon(ft.Icons.LIGHTBULB_OUTLINE_ROUNDED, color=ACCENT, size=25),
                        text(info.get("name", lid), 18, weight=ft.FontWeight.W_600, expand=True), status]),
                small(info.get("type", "Hue light")), details,
                ft.Row([button("Controls", self.bind(self.light_control_dialog, lid), ft.Icons.TUNE),
                        button("On", self.bind(self.run_lights, [lid], LightState(action="on", bri=None), "Manual ON")),
                        button("Off", self.bind(self.run_lights, [lid], LightState(action="off", bri=None), "Manual OFF"))],
                       wrap=True, spacing=7, run_spacing=7)], spacing=13), col={"xs": 12, "md": 6, "xl": 4})
            items.append((str(info.get("name", "")).casefold() + " " + lid, lamp))
        def filter_lights(e):
            q = (search.value or "").casefold()
            for name, control in items:
                control.visible = q in name
            self.update()
        search.on_change = filter_lights
        controls.extend([search, ft.ResponsiveRow([c for _, c in items], spacing=16, run_spacing=16)])
        return self.screen(controls)

    def light_control_dialog(self, lid):
        info = self.lights.get(lid)
        if not info:
            raise ValueError("That light is no longer in the bridge inventory. Refresh the lights.")
        st = info.get("state", {})
        initial = LightState(action="set", bri=st.get("bri"),
                             xy=tuple(st["xy"]) if st.get("colormode") == "xy" and st.get("xy") else None,
                             ct=st.get("ct") if st.get("colormode") == "ct" else None)
        editor = StateEditor(self, "Light settings", initial)
        error = text("", 12, DANGER)
        async def apply():
            try:
                state = editor.value()
            except ValueError as ex:
                error.value = str(ex)
                self.update()
                return
            await self.run_lights([lid], state, "Manual control")
        self.show_dialog(info.get("name", lid), ft.Container(width=570, content=ft.Column([
            editor.content, error], height=min(610, max(360, (self.page.height or 900) - 260)),
            scroll=ft.ScrollMode.AUTO, spacing=15)),
            [button("Close", self.bind(self.close_dialog)), button("Apply to light", self.bind(apply),
                                                                 ft.Icons.CHECK, primary=True)])

    def space_targets(self, ids):
        targets, missing = [], []
        for sid in ids:
            s = self.space(sid)
            if not s.lights:
                missing.append(f"{s.name}: no lights assigned")
            for ref in s.lights:
                lid = resolve_ref(ref, self.lights)
                if lid is None:
                    missing.append(f"{s.name}: missing light {ref.name}")
                elif lid not in targets:
                    targets.append(lid)
        if missing:
            raise ValueError("Check these assignments before applying the command:\n" + "\n".join(missing))
        if not targets:
            raise ValueError("There are no assigned lights to control.")
        return targets

    async def space_command(self, sid, state):
        await self.run_lights(self.space_targets([sid]), state, self.space(sid).name)

    async def apply_editor_to_space(self, sid, editor):
        await self.space_command(sid, editor.value())

    def set_busy(self, change):
        self.busy_count = max(0, self.busy_count + change)
        self.progress.visible = self.busy_count > 0
        self.update()

    async def run_lights(self, lids, state, label="Manual control"):
        if not self.connected:
            raise ValueError("Connect to the Hue Bridge before controlling lights.")
        if self.manual_busy:
            self.notice("A manual command is already being sent.")
            return
        state = copy.deepcopy(state).validate()
        targets = list(dict.fromkeys(lids))
        if not targets:
            raise ValueError("Select at least one light.")
        self.manual_busy = True
        self.set_busy(1)
        successes, failures = [], []
        try:
            # One lock for the whole batch prevents schedule commands interleaving
            
            async with self.io_lock:
                for lid in targets:
                    if self.closing or not self.connected:
                        failures.append("Operation stopped: bridge disconnected or app closing.")
                        break
                    try:
                        updated = await asyncio.to_thread(self.hue.set_state, lid, state)
                        self.lights[lid] = updated
                        successes.append(lid)
                        self.log(f"{label} → {updated.get('name', lid)}: {state.summary()}")
                    except Exception as ex:
                        failures.append(f"{self.lights.get(lid, {}).get('name', lid)}: {ex}")
                    await asyncio.sleep(.12)
                # Manual overrides last until the next boundary, explicit sync,
                # reconnect, or a relevant schedule/assignment change.
                self.engine.manual_override(successes)
            self.live_dirty = True
            if failures:
                self.log(f"{label}: {len(successes)}/{len(targets)} commands accepted; " + "; ".join(failures), "warning")
                self.notice(f"{len(successes)}/{len(targets)} commands were accepted by the bridge.\n\n"
                            + "\n".join(failures), error=True)
            else:
                self.notice(f"{label} · applied to {len(successes)} light(s).")
        finally:
            self.manual_busy = False
            self.set_busy(-1)

    # Schedule and timing settings where the user can define start and end states for spaces, with optional recurrence.
    def schedule(self, schedule_id):
        for s in self.project.schedules:
            if s.id == schedule_id:
                return s
        raise ValueError("This schedule no longer exists.")

    def schedule_mini(self, s):
        names = {x.id: x.name for x in self.project.spaces}
        return ft.Container(content=ft.Column([
            ft.Row([text(s.name, 15, weight=ft.FontWeight.W_600, expand=True),
                    pill("Enabled" if s.enabled else "Disabled", SUCCESS if s.enabled else MUTED)]),
            small(s.recurrence_text()), small(" → ".join([s.start_time, s.end_time])),
            small(", ".join(names.get(i, i) for i in s.spaces)),
            button("Edit schedule", self.bind(self.schedule_dialog, s.id), ft.Icons.EDIT_OUTLINED)], spacing=9),
            padding=15, bgcolor=PANEL_2, border_radius=14)

    def upcoming_panel(self):
        rows = ft.Column(spacing=12)
        def refresh():
            now = datetime.now()
            events = [(b[0], s, b[1]) for s in self.project.schedules
                      if (b := s.boundary(now, future=True)) is not None]
            events.sort(key=lambda v: v[0])
            rows.controls = [ft.Row([
                ft.Container(content=ft.Icon(ft.Icons.SCHEDULE, color=ACCENT), padding=10,
                             bgcolor=PANEL_2, border_radius=12),
                ft.Column([text(s.name, 14, weight=ft.FontWeight.W_600),
                           small(f"{when:%a %d %b · %H:%M}  ·  {which.upper()}"),
                           small((s.start_state if which == "start" else s.end_state).summary())], expand=True)])
                for when, s, which in events[:5]] or [small("No upcoming enabled schedule boundaries.")]
        refresh()
        self.live_updaters.append(refresh)
        return card(ft.Column([ft.Row([text("Coming up", 19, weight=ft.FontWeight.W_600, expand=True),
                                        button("Schedules", self.bind(self.go, "schedules"))]),
                               small("Local computer time. Paused schedules are displayed but will not execute."), rows], spacing=16))

    def schedules_view(self):
        controls = [self.heading("Schedules", "Start and end states for your named spaces.",
                                 button("New schedule", self.bind(self.schedule_dialog), ft.Icons.ADD, primary=True))]
        enabled = self.project.scheduler_enabled
        controls.append(card(ft.Column([
            ft.Row([pill("RUNNING" if enabled else "PAUSED", SUCCESS if enabled else ACCENT),
                    text("Scheduler is armed" if enabled else "Automatic light changes are paused", 16, expand=True),
                    button("Pause" if enabled else "Resume", self.bind(self.toggle_scheduler),
                           ft.Icons.PAUSE if enabled else ft.Icons.PLAY_ARROW, primary=not enabled)]),
            small("The application must stay open and the computer awake. An enabled scheduler synchronizes to the latest "
                  "boundary on connect/resume; it does not replay every missed event."),
            ft.Row([button("Synchronize now", self.bind(self.sync_now), ft.Icons.SYNC),
                    button("Import CSV", self.bind(self.import_csv), ft.Icons.UPLOAD_FILE),
                    button("Export CSV", self.bind(self.export_csv), ft.Icons.DOWNLOAD),
                    button("Clear schedules", self.bind(self.clear_schedules), ft.Icons.DELETE_OUTLINE, danger=True)],
                   wrap=True, spacing=8, run_spacing=8)], spacing=15)))
        if not self.project.schedules:
            controls.append(self.empty("Build your first lighting cycle", "Choose the spaces, timing, and light state at each boundary.",
                                       "Create schedule", self.bind(self.schedule_dialog)))
        for index, s in enumerate(self.project.schedules):
            names = {x.id: x.name for x in self.project.spaces}
            timing = small("")
            def refresh(sid=s.id, label=timing):
                try:
                    sch = self.schedule(sid)
                except ValueError:
                    return
                bd = sch.boundary(datetime.now(), future=True)
                label.value = (f"Next: {bd[0]:%d %b %Y · %H:%M} · {bd[1].upper()}" if bd else
                               "No upcoming boundary" if sch.enabled else "Disabled")
            refresh()
            self.live_updaters.append(refresh)
            toggle = ft.Switch(value=s.enabled, label="Enabled", active_color=ACCENT,
                               on_change=self.bind(self.toggle_schedule, s.id))
            date_detail = (f"{s.start_date} {s.start_time} → {s.end_date} {s.end_time}" if s.recurrence == "once" else
                           f"From {s.start_date} · {s.start_time} → {s.end_time}" +
                           (f" · until {s.until_date}" if s.until_date else ""))
            controls.append(card(ft.Column([
                ft.Row([pill(f"{index + 1:02d}"), text(s.name, 21, weight=ft.FontWeight.W_600, expand=True), toggle]),
                text(", ".join(names.get(i, i) for i in s.spaces), 14, ACCENT),
                small(s.recurrence_text() + "  ·  " + date_detail), timing,
                ft.ResponsiveRow([
                    ft.Container(content=ft.Column([small("START STATE"), text(s.start_state.summary())], spacing=6),
                                 bgcolor=PANEL_2, padding=15, border_radius=12, col={"xs": 12, "md": 6}),
                    ft.Container(content=ft.Column([small("END STATE"), text(s.end_state.summary())], spacing=6),
                                 bgcolor=PANEL_2, padding=15, border_radius=12, col={"xs": 12, "md": 6})], spacing=12, run_spacing=12),
                ft.Row([button("Edit", self.bind(self.schedule_dialog, s.id), ft.Icons.EDIT_OUTLINED),
                        button("Duplicate", self.bind(self.duplicate_schedule, s.id), ft.Icons.CONTENT_COPY),
                        button("Apply START now", self.bind(self.apply_schedule, s.id, "start")),
                        button("Apply END now", self.bind(self.apply_schedule, s.id, "end")),
                        button("Remove", self.bind(self.remove_schedule, s.id), ft.Icons.DELETE_OUTLINE, danger=True)],
                       wrap=True, spacing=8, run_spacing=8)], spacing=15)))
        controls.append(small("Overlapping schedules: the most recent boundary wins for each physical lamp. "
                              "At identical times, the later schedule in this list wins. Avoid conflicting protocols."))
        return self.screen(controls)

    def date_control(self, label, value=""):
        entry = field(label, value, expand=True)
        def pick():
            def changed(e):
                if e.control.value:
                    entry.value = e.control.value.strftime("%Y-%m-%d")
                    self.update()
            picker = ft.DatePicker(value=parse_date(entry.value) or date.today(),
                                   first_date=date(1900, 1, 1), last_date=date(2200, 12, 31),
                                   on_change=changed)
            self.page.show_dialog(picker)
        return entry, ft.Row([entry, ft.IconButton(icon=ft.Icons.CALENDAR_MONTH_OUTLINED,
                                                   icon_color=ACCENT, on_click=self.bind(pick))])

    def schedule_dialog(self, schedule_id=None, space_id=None):
        if not self.project.spaces:
            self.notice("Create your spaces before adding a schedule.")
            self.create_spaces_dialog()
            return
        original = self.schedule(schedule_id) if schedule_id else None
        today = date.today().isoformat()
        s = copy.deepcopy(original) if original else Schedule(name="Day / night cycle", spaces=[space_id] if space_id else [],
                                                              start_date=today, end_date=today)
        name = field("Schedule name", s.name)
        enabled = ft.Switch(value=s.enabled, label="Enable this schedule", active_color=ACCENT)
        choices = {sp.id: ft.Checkbox(label=sp.name, value=sp.id in s.spaces) for sp in self.project.spaces}
        def select_all(value):
            for cb in choices.values():
                cb.value = value
            self.update()
        start_date, start_date_row = self.date_control("Start date · YYYY-MM-DD", s.start_date)
        end_date, end_date_row = self.date_control("End date · one-off schedules", s.end_date)
        until, until_row = self.date_control("Last occurrence start date · optional", s.until_date)
        start_time, end_time = field("Start time · HH:MM", s.start_time), field("End time · HH:MM", s.end_time)
        every = field("Every N days / weeks", s.every_n)
        weekdays = [ft.Checkbox(label=d, value=i in s.weekdays) for i, d in enumerate(WEEKDAYS)]
        weekly_row = ft.Row(weekdays, wrap=True, spacing=3, run_spacing=3)
        recurrence = dropdown("Repeat", s.recurrence, [("once", "Once"), ("daily", "Daily"), ("weekly", "Weekly")])
        hint = small("")
        def rec_changed(e=None):
            once = recurrence.value == "once"
            end_date_row.visible = once
            until_row.visible = not once
            every.visible = not once
            weekly_row.visible = recurrence.value == "weekly"
            hint.value = ("Once uses both dates. A same-date end time at/before the start rolls over to the next day."
                          if once else "Each repeated window ends the same day, or the next day when its end time is at/before its start. "
                          "The last-occurrence date limits starts, not an overnight end.")
            self.update()
        recurrence.on_select = rec_changed
        start_editor = StateEditor(self, "At START", s.start_state)
        end_editor = StateEditor(self, "At END", s.end_state)
        error = text("", 13, DANGER)
        def save():
            try:
                targets = [sid for sid, cb in choices.items() if cb.value]
                edited = Schedule(name=name.value, spaces=targets, start_date=start_date.value,
                                  start_time=start_time.value, end_date=end_date.value if recurrence.value == "once" else start_date.value,
                                  end_time=end_time.value, recurrence=recurrence.value,
                                  every_n=number(every.value, "Every N", 1, 365, True) if recurrence.value != "once" else 1,
                                  weekdays=[i for i, cb in enumerate(weekdays) if cb.value],
                                  until_date=until.value if recurrence.value != "once" else "",
                                  start_state=start_editor.value(), end_state=end_editor.value(), enabled=bool(enabled.value),
                                  id=s.id if original else new_id(), revision=new_id()).validate()
            except (ValueError, TypeError) as ex:
                error.value = str(ex)
                self.update()
                return
            if original:
                index = next(i for i, candidate in enumerate(self.project.schedules) if candidate.id == original.id)
                self.project.schedules[index] = edited
            else:
                self.project.schedules.append(edited)
            self.persist()
            self.close_dialog()
            self.log(f"{'Updated' if original else 'Created'} schedule '{edited.name}'.")
            self.go("schedules")
        content = ft.Column([
            ft.Row([name, enabled]),
            ft.Row([text("Apply to spaces", 16, weight=ft.FontWeight.W_600, expand=True),
                    button("Select all", self.bind(select_all, True)), button("Clear", self.bind(select_all, False))]),
            ft.Container(content=ft.Column(list(choices.values()), height=145, scroll=ft.ScrollMode.AUTO),
                         bgcolor=PANEL_2, padding=10, border_radius=12),
            ft.ResponsiveRow([ft.Container(recurrence, col={"xs": 12, "md": 6}),
                              ft.Container(every, col={"xs": 12, "md": 6})], spacing=14, run_spacing=14),
            weekly_row,
            ft.ResponsiveRow([ft.Container(start_date_row, col={"xs": 12, "md": 6}),
                              ft.Container(start_time, col={"xs": 12, "md": 6}),
                              ft.Container(end_date_row, col={"xs": 12, "md": 6}),
                              ft.Container(end_time, col={"xs": 12, "md": 6})], spacing=14, run_spacing=14),
            until_row, hint, ft.Divider(color=BORDER),
            ft.ResponsiveRow([card(start_editor.content, col={"xs": 12, "md": 6}),
                              card(end_editor.content, col={"xs": 12, "md": 6})], spacing=14, run_spacing=14),
            small("Saving an enabled schedule while the scheduler is running may immediately apply its current state. "
                  "Pause the scheduler first to prepare a protocol without changing lights."), error],
            height=min(650, max(360, (self.page.height or 900) - 240)), scroll=ft.ScrollMode.AUTO, spacing=17)
        name.expand = True
        rec_changed()
        self.show_dialog("Edit schedule" if original else "New schedule", ft.Container(content=content, width=1020),
                         [button("Cancel", self.bind(self.close_dialog)),
                          button("Save schedule", self.bind(save), ft.Icons.CHECK, primary=True)])

    def toggle_schedule(self, schedule_id):
        original = self.schedule(schedule_id)
        changed = copy.deepcopy(original)
        changed.enabled = not original.enabled
        changed.revision = new_id()
        self.project.schedules[self.project.schedules.index(original)] = changed
        self.persist()
        self.log(f"Schedule '{changed.name}' {'enabled' if changed.enabled else 'disabled'}.")
        self.go("schedules")

    def duplicate_schedule(self, schedule_id):
        s = copy.deepcopy(self.schedule(schedule_id))
        s.id, s.revision, s.name, s.enabled = new_id(), new_id(), s.name + " · copy", False
        self.project.schedules.append(s)
        self.persist()
        self.log(f"Duplicated '{s.name}' (disabled until reviewed).")
        self.go("schedules")

    def remove_schedule(self, schedule_id):
        s = self.schedule(schedule_id)
        def remove():
            self.project.schedules = [x for x in self.project.schedules if x.id != schedule_id]
            self.persist()
            self.log(f"Removed schedule '{s.name}'.")
            self.go("schedules")
        self.confirm("Remove this schedule?", f"Remove '{s.name}'? Lights are not turned off automatically. "
                     "Another applicable schedule may take over.", remove, danger=True)

    def clear_schedules(self):
        def clear():
            self.project.schedules.clear()
            self.engine.invalidate()
            self.persist()
            self.log("Cleared all schedules. Existing light states were left unchanged.")
            self.go("schedules")
        self.confirm("Clear all schedules?", "This removes every schedule, but does not turn off any lights.", clear, danger=True)

    async def apply_schedule(self, schedule_id, which):
        s = self.schedule(schedule_id)
        await self.run_lights(self.space_targets(s.spaces), s.start_state if which == "start" else s.end_state,
                              f"{s.name} · manual {which.upper()}")

    def toggle_scheduler(self):
        if self.project.scheduler_enabled:
            self.project.scheduler_enabled = False
            self.persist()
            self.log("Scheduler paused. Light states left unchanged.")
            self.go("schedules")
            return
        def resume():
            self.project.scheduler_enabled = True
            self.engine.invalidate()
            self.persist()
            self.log("Scheduler resumed; latest applicable states will be synchronized.")
            self.go("schedules")
        self.confirm("Resume automatic lighting?", "The latest scheduled state for each light will be applied when connected, "
                     "including an END state whose boundary is already past. Manual overrides will be replaced.", resume)

    async def sync_now(self):
        if not self.connected:
            raise ValueError("Connect to the Bridge first.")
        if not self.project.scheduler_enabled:
            raise ValueError("Resume the scheduler before synchronizing. Manual START/END buttons work while paused.")
        self.engine.invalidate()
        await self.engine.tick()
        self.log("Schedule synchronization checked; failed/missing targets remain visible in Activity.")

    # bridge settings...and import json file....might move it up? later.
    def settings_view(self):
        self.ip_field = field("Hue Bridge IP address", self.project.bridge_ip if not self.demo else "Demo bridge", expand=True)
        self.ip_field.disabled = self.demo
        auto = ft.Checkbox(label="Reconnect to this Bridge when Lighthouse starts", value=self.project.auto_connect,
                           disabled=self.demo)
        def auto_changed(e):
            self.project.auto_connect = bool(auto.value)
            self.persist()
        auto.on_change = auto_changed
        title = field("Workspace name", self.project.title, expand=True)
        def save_title():
            self.project.title = (title.value or "My Lighthouse").strip()[:100]
            self.persist()
            self.go("settings")
        return self.screen([
            self.heading("Bridge & project", "Local light control, saved spaces and portable protocols."),
            card(ft.Column([
                text("Hue Bridge", 20, weight=ft.FontWeight.W_600),
                small("Connect the computer and Bridge to the same trusted local network. For first-time registration, "
                      "press the Bridge's physical link button immediately before Connect / Register."),
                ft.Row([self.ip_field, button("Discover online", self.bind(self.discover_bridge), ft.Icons.SEARCH,
                                              disabled=self.demo)]),
                ft.Row([button("Connect / Register", self.bind(self.connect), ft.Icons.LINK, primary=True, disabled=self.demo),
                        button("Refresh lights", self.bind(self.refresh_lights), ft.Icons.REFRESH),
                        button("Disconnect", self.bind(self.disconnect), ft.Icons.LINK_OFF, disabled=self.demo)],
                       wrap=True, spacing=8, run_spacing=8), auto,
                small("Discovery needs internet. Normal control uses the Bridge IP locally; no cloud account is required by this code. "
                      "Previously saved Lighthouse credentials are reused when available."),
                text("Bound Bridge: " + (self.project.bridge_id or "Not bound yet"), 12, MUTED, selectable=True)], spacing=16)),
            card(ft.Column([
                text("Workspace", 20, weight=ft.FontWeight.W_600), ft.Row([title, button("Save name", self.bind(save_title))]),
                small("Spaces, light assignments and schedules are saved automatically. Export JSON for a portable backup. "
                      "Import accepts your original Box-based Lighthouse configuration and pauses the scheduler for review."),
                ft.Row([button("Import JSON", self.bind(self.import_project), ft.Icons.UPLOAD_FILE),
                        button("Export JSON", self.bind(self.export_project), ft.Icons.DOWNLOAD),
                        button("Import schedules CSV", self.bind(self.import_csv)),
                        button("Export schedules CSV", self.bind(self.export_csv))], wrap=True, spacing=8, run_spacing=8),
                text(str(self.workspace), 12, MUTED, selectable=True),
                small("Renaming a space does not rename a Hue lamp or create/modify rooms in the official Hue app.")], spacing=16)),
            card(ft.Column([
                text("Before an unattended protocol", 20, weight=ft.FontWeight.W_600),
                text("Keep this application open, keep the computer awake, and verify the computer's local date and time."),
                text("Brightness uses Hue's 0–254 scale. ON keeps the previous colour; SET applies a colour explicitly. "
                     "The wavelength option is an approximate display colour, not a calibrated or monochromatic light source."),
                
                small(f"Lighthouse Spaces {APP_VERSION} · Flet {FLET_VERSION} .")], spacing=15))])

    def normalize_assignments(self):
        changed = False
        for s in self.project.spaces:
            updated = []
            for ref in s.lights:
                lid = resolve_ref(ref, self.lights)
                replacement = make_ref(lid, self.lights[lid]) if lid else ref
                changed |= asdict(replacement) != asdict(ref)
                updated.append(replacement)
            s.lights = updated
        if changed:
            self.persist()

    async def connect(self, raw_ip=None, allow_register=True):
        if self.demo:
            return
        ip = raw_ip or (self.ip_field.value if self.ip_field is not None else self.project.bridge_ip)
        ip = self.hue.valid_ip(ip)
        self.set_busy(1)
        self.log(f"Connecting to Hue Bridge at {ip}…")
        try:
            async with self.io_lock:
                self.connected = False
                lights = await asyncio.to_thread(self.hue.connect, ip, allow_register, self.project.bridge_id)
                self.lights = lights
                self.project.bridge_ip, self.project.bridge_id = ip, self.hue.bridge_id
                self.connected, self.connection_wanted, self.failures = True, True, 0
                self.last_refresh = time.monotonic()
                self.normalize_assignments()
                self.engine.invalidate()
                self.persist()
            self.log(f"Bridge connected · {len(self.lights)} light(s).")
            self.live_dirty = True
            self.go(self.view, self.space_id)
        except Exception:
            self.connected = False
            self.connection_wanted = bool(self.hue.username) and self.hue.ip == self.project.bridge_ip
            raise
        finally:
            self.set_busy(-1)

    async def refresh_lights(self, silent=False):
        if not self.demo and not self.hue.username:
            if silent:
                return
            raise ValueError("Connect / Register with the Bridge first.")
        if not silent:
            self.set_busy(1)
        try:
            async with self.io_lock:
                was_connected = self.connected
                lights = await asyncio.to_thread(self.hue.refresh)
                self.lights = lights
                self.connected, self.failures = True, 0
                self.last_refresh = time.monotonic()
                self.normalize_assignments()
                if not was_connected:
                    self.engine.invalidate()
                    self.log("Bridge connection recovered. Schedule state will be resynchronized.")
            self.live_dirty = True
            if not silent:
                self.log(f"Refreshed {len(self.lights)} lights.")
                self.go(self.view, self.space_id)
        except Exception as ex:
            self.failures += 1
            if self.failures >= 3:
                self.connected = False
            self.log(f"Bridge refresh failed ({self.failures}): {ex}", "warning")
            if not silent:
                raise
        finally:
            if not silent:
                self.set_busy(-1)

    async def disconnect(self):
        async with self.io_lock:
            self.connection_wanted, self.connected = False, False
            self.hue.username = ""
        self.log("Disconnected. Light states were left unchanged.")
        self.live_dirty = True
        self.go("settings")

    async def discover_bridge(self):
        self.set_busy(1)
        try:
            bridges = await asyncio.to_thread(self.hue.discover)
            if not bridges:
                raise ValueError("No Bridge was discovered. Online discovery needs internet; otherwise look up "
                                 "the Bridge IP in the router's DHCP/client list and enter it directly.")
            def choose(ip):
                if self.ip_field is not None:
                    self.ip_field.value = ip
                self.log(f"Discovered {ip}. Press Connect / Register to use it.")
            if len(bridges) == 1:
                choose(bridges[0])
            else:
                def choose_close(ip):
                    choose(ip)
                    self.close_dialog()
                self.show_dialog("Select a Bridge", ft.Column([button(ip, self.bind(choose_close, ip)) for ip in bridges], tight=True),
                                 [button("Cancel", self.bind(self.close_dialog))])
        finally:
            self.set_busy(-1)

    async def pick_text(self, extension):
        files = await self.file_picker.pick_files(allow_multiple=False, with_data=True,
                                                  file_type=ft.FilePickerFileType.CUSTOM, allowed_extensions=[extension])
        if not files:
            return None
        chosen = files[0]
        data = chosen.bytes
        if data is None and chosen.path:
            data = await asyncio.to_thread(Path(chosen.path).read_bytes)
        if data is None:
            raise ValueError("The selected file could not be read.")
        return data.decode("utf-8-sig")

    async def save_text(self, name, extension, contents):
        # Flet 1.0 writes src_bytes itself on desktop; do not write twice afterward.
        path = await self.file_picker.save_file(file_name=name, file_type=ft.FilePickerFileType.CUSTOM,
                                                allowed_extensions=[extension], src_bytes=contents.encode("utf-8"))
        if path:
            self.log(f"Exported {Path(path).name}.")
            self.notice("Export saved.")

    async def import_project(self):
        contents = await self.pick_text("json")
        if contents is None:
            return
        incoming, warnings = Project.from_dict(json.loads(contents))
        incoming.scheduler_enabled = False
        async def replace_project():
            async with self.io_lock:
                backup = self.workspace.with_name(self.workspace.stem + ".before_import_" +
                                                  datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".json")
                await asyncio.to_thread(atomic_write, backup, json.dumps(self.project.to_dict(), indent=2, ensure_ascii=False))
                same_bridge = self.connected and incoming.bridge_ip == self.project.bridge_ip and (
                    not incoming.bridge_id or incoming.bridge_id == self.project.bridge_id)
                if same_bridge:
                    incoming.bridge_id = self.project.bridge_id
                self.project = incoming
                if self.demo:
                    self.project.bridge_ip, self.project.bridge_id = "demo", "DEMO"
                elif not same_bridge:
                    self.connected, self.connection_wanted = False, False
                    self.hue.http.close()
                    self.hue = HueController()
                    self.lights = {}
                self.engine.invalidate()
                if self.connected:
                    self.normalize_assignments()
                self.persist()
            for warning in warnings:
                self.log(warning, "warning")
            self.log("Imported project; scheduler paused. A backup of the previous workspace was saved.")
            self.go("overview")
        detail = f"Replace the current workspace with {len(incoming.spaces)} spaces and {len(incoming.schedules)} schedules? "
        detail += "The scheduler will be paused. Review assignments and timing, then Resume it. The source file is not changed."
        if warnings:
            detail += "\n\n" + "\n".join(warnings[:8])
        self.confirm("Import project?", detail, replace_project)

    async def export_project(self):
        await self.save_text("lighthouse_spaces.json", "json", json.dumps(self.project.to_dict(), indent=2, ensure_ascii=False))

    async def import_csv(self):
        contents = await self.pick_text("csv")
        if contents is None:
            return
        loaded = csv_import(contents, self.project)
        def append_schedules():
            self.project.scheduler_enabled = False
            self.project.schedules.extend(loaded)
            self.engine.invalidate()
            self.persist()
            self.log(f"Imported {len(loaded)} schedules. Scheduler paused for review.")
            self.go("schedules")
        self.confirm("Import schedule CSV?", f"Append {len(loaded)} schedules and pause automatic lighting? "
                     "Existing schedules will not be deleted. Review imported dates/states before resuming.", append_schedules)

    async def export_csv(self):
        if not self.project.schedules:
            raise ValueError("There are no schedules to export.")
        await self.save_text("lighthouse_schedules.csv", "csv", csv_export(self.project))

    # clean shut down section
    def activity_view(self):
        self.activity_list = ft.ListView(expand=True, spacing=6, auto_scroll=True)
        self.render_logs()
        return ft.Column([
            self.heading("Activity", "Commands, connection checks, imports and validation messages.",
                         button("Export log", self.bind(self.export_log), ft.Icons.DOWNLOAD)),
            small("A successful command means the Bridge accepted it; it is not an independent measurement of emitted light."),
            ft.Container(content=self.activity_list, expand=True, bgcolor=PANEL, padding=18, border_radius=18,
                         border=ft.Border.all(1, BORDER))], expand=True, spacing=16)

    def render_logs(self):
        if self.activity_list is None:
            return
        self.activity_list.controls = [ft.Row([
            text(stamp, 11, MUTED, width=65),
            text(message, 12, DANGER if level == "error" else ACCENT if level == "warning" else TEXT, expand=True)],
            vertical_alignment=ft.CrossAxisAlignment.START) for stamp, level, message in self.logs[-500:]]
        self.logs_dirty = False

    async def export_log(self):
        content = "\n".join(f"[{stamp}] {level.upper()} {message}" for stamp, level, message in self.logs)
        await self.save_text("lighthouse_activity.txt", "txt", content)

    def update_status(self):
        self.clock.value = datetime.now().strftime("%a %d %b  ·  %H:%M:%S")
        if self.demo:
            label, colour = "Demo bridge", ACCENT
        elif self.connected:
            label = f"Bridge connected · {len(self.lights)} lights" if not self.failures else "Bridge connection unstable"
            colour = SUCCESS if not self.failures else ACCENT
        else:
            label, colour = "Bridge not connected", MUTED
        self.bridge_badge.value, self.bridge_badge.color = label, colour
        self.bridge_dot.bgcolor = colour
        self.scheduler_badge.value = "Scheduler running" if self.project.scheduler_enabled else "Scheduler paused"
        self.scheduler_badge.color = SUCCESS if self.project.scheduler_enabled else ACCENT
        self.progress.visible = self.busy_count > 0

    async def start(self):
        self.log("Lighthouse Spaces ready." + (" DEMO — no real lights will be contacted." if self.demo else ""))
        for warning in self.startup_warnings:
            self.log(warning, "warning")
        self._tasks = [self.page.run_task(self.engine_loop), self.page.run_task(self.heartbeat_loop),
                       self.page.run_task(self.ui_loop)]
        if self.demo:
            self.persist()
        elif self.project.auto_connect and self.project.bridge_ip:
            try:
                await self.connect(self.project.bridge_ip, allow_register=False)
            except Exception as ex:
                self.log(f"Automatic connection failed: {ex}. Use Connect / Register in Bridge settings.", "warning")
        if not self.project.spaces:
            self.create_spaces_dialog()
        self.update()

    async def engine_loop(self):
        while not self.closing:
            try:
                await self.engine.tick()
            except Exception as ex:
                self.log(f"Scheduler check failed: {ex}", "error")
            await asyncio.sleep(1)

    async def heartbeat_loop(self):
        while not self.closing:
            await asyncio.sleep(60)
            if self.connection_wanted and not self.closing:
                await self.refresh_lights(silent=True)

    async def ui_loop(self):
        while not self.closing:
            if self.save_pending:
                await self.save_workspace()
            self.update_status()
            # Only live readouts change here; forms and manually typed values are
            # never rebuilt by a timer, slider update
            for updater in tuple(self.live_updaters):
                try:
                    updater()
                except (KeyError, ValueError):
                    pass
            if self.logs_dirty and self.activity_list is not None:
                self.render_logs()
            self.live_dirty = False
            self.update()
            await asyncio.sleep(1)

    async def on_window_event(self, e):
        event_type = getattr(e, "type", None)
        if event_type not in (ft.WindowEventType.CLOSE, "close") or self.closing:
            return
        async def close():
            await self.save_workspace()
            if self.save_error:
                self.notice("Closing was cancelled because the workspace could not be saved. "
                            "Export a JSON backup or fix the save-path permissions before closing.\n\n" + self.save_error,
                            error=True)
                return
            await self.shutdown()
            await self.page.window.destroy()
        self.confirm("Close Lighthouse?", "Scheduled changes stop when this application closes. "
                     "Your workspace will be saved; lights will remain in their current state.", close)

    async def on_session_close(self, e):
        await self.shutdown()

    async def shutdown(self):
        if self.closing:
            return
        self.closing = True
        self.project.scheduler_enabled = bool(self.project.scheduler_enabled)
        self.save_pending = True
        await self.save_workspace()
        # Let any already-sent request finish before releasing the HTTP session.
        async with self.io_lock:
            self.hue.http.close()
        for task in getattr(self, "_tasks", []):
            task.cancel()
        self.logger.info("Lighthouse closed. No shutdown light commands were sent.")
        for handler in self.logger.handlers[:]:
            handler.close()
            self.logger.removeHandler(handler)


def main():
    parser = argparse.ArgumentParser(description="Lighthouse Spaces — modern Flet desktop Hue controller")
    parser.add_argument("--demo", action="store_true", help="Simulated bridge; does not control real lights")
    parser.add_argument("--workspace", type=Path, help="Autosaved workspace JSON path; defaults to ~/.lighthouse_flet/workspace.json")
    args = parser.parse_args()
    if ft is None:
        raise SystemExit('Flet is not installed. Run: python -m pip install "flet[desktop]==1.0.0" "requests>=2.32,<3"')
    from importlib.metadata import version
    installed = version("flet")
    if installed != FLET_VERSION:
        raise SystemExit(f"This edition targets Flet {FLET_VERSION}; installed version is {installed}.\n"
                         f'Run: python -m pip install --upgrade "flet[desktop]=={FLET_VERSION}"')
    workspace = (args.workspace or APP_DIR / ("demo_workspace.json" if args.demo else "workspace.json")).expanduser().resolve()
    lock = WorkspaceLock(workspace.with_suffix(".lock")).acquire()
    async def start_page(page):
        try:
            if page.web:
                page.add(ft.Text("This controller is designed as a single-user desktop application. "
                                 "Run: python lighthouse_flet.py (without a web-server mode)."))
                return
            app = LighthouseApp(page, workspace, demo=args.demo)
            await app.start()
        except Exception as ex:
            page.title = "Lighthouse · startup error"
            page.bgcolor = BG
            page.add(ft.Text("Lighthouse could not start", size=25, color=DANGER),
                     ft.Text(str(ex), selectable=True, color=TEXT),
                     ft.Text("Check your Python/Flet version and workspace file. No replacement workspace was written.", color=MUTED))
            page.update()
            logging.exception("Lighthouse startup failed")
    try:
        ft.run(start_page)
    finally:
        lock.release()


if __name__ == "__main__":
    main()
