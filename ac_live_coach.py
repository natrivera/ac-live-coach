#!/usr/bin/env python3
"""
AC Live Coach -- a live voice coach that learns any track.

A personal live coach for Assetto Corsa (original AC + Content Manager).
It reads the game's shared-memory telemetry (the same feed other apps use),
builds its reference from YOUR OWN laps, and talks to you over Windows
speech only at the corners where your data says time is sitting.

What it does:
  - Lap 1 at any car/track: mapping. It records your lap and builds the
    reference + corner map from it.
  - From lap 2 on: approach cues at your problem corners ("Turn 6: brake
    later"), short feedback after a corner that cost real time, and a
    two-sentence lap summary (lap time, gap to your best, worst corner).
  - Your best lap becomes the standing reference and is saved to disk, so
    next session starts coaching immediately against your standing best.
  - Every session writes a log + debrief text file next to this script.
    Send those back for tuning.

Requirements: Windows, Python 3. Original Assetto Corsa publishes its
shared memory automatically while you're on track -- there is no in-game
setting to enable (that toggle exists in AC EVO, not in AC 2014).
Voice (v2.2): piper-tts for the offline neural voice (pip install
piper-tts); the voice model downloads once, then runs fully offline.
pyttsx3 (the built-in Windows voices) remains as the fallback engine.
With neither installed, cues print to this console instead.

Run:  python ac_live_coach.py
Test the coaching brain without the game:  python ac_live_coach.py --simulate
"""

import ctypes
import hashlib
import re
import html
import json
import math
import os
import queue
import random
import sys
import threading
import time
import webbrowser
from ctypes import c_float, c_int, c_wchar

APP_DIR = os.path.dirname(os.path.abspath(__file__))
TRACKS_DIR = os.path.join(APP_DIR, "tracks")
LOGS_DIR = os.path.join(APP_DIR, "logs")


def _ac_install_candidates():
    """Likely Assetto Corsa install folders: the default Steam
    locations plus any extra Steam libraries listed in
    libraryfolders.vdf."""
    import re
    roots = [r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"]
    cands = []
    for root in roots:
        cands.append(os.path.join(root, "steamapps", "common", "assettocorsa"))
        vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
        try:
            if os.path.exists(vdf):
                with open(vdf, encoding="utf-8", errors="ignore") as fh:
                    txt = fh.read()
                for m in re.finditer(r'"path"\s*"([^"]+)"', txt):
                    lib = m.group(1).replace("\\\\", "\\")
                    cands.append(os.path.join(
                        lib, "steamapps", "common", "assettocorsa"))
        except Exception:
            pass
    return cands


def _writable_dir(path):
    """Probe writability of an EXISTING folder; never creates one
    (v2.5.3 -- probing used to create ACLiveCoach/profiles inside
    the Steam install and every other candidate as a side effect)."""
    try:
        probe = os.path.join(path, ".write_probe")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except Exception:
        return False


def _migrate_profiles(legacy, dest):
    """One-way copy of profile/map JSONs from the old app-folder
    location; never overwrites a file the new location already has."""
    import shutil
    moved = 0
    if os.path.abspath(legacy) == os.path.abspath(dest):
        return 0
    if not os.path.isdir(legacy):
        return 0
    for name in os.listdir(legacy):
        if name.endswith(".json"):
            src, dst = os.path.join(legacy, name), os.path.join(dest, name)
            if not os.path.exists(dst):
                try:
                    shutil.copy2(src, dst)
                    moved += 1
                except Exception:
                    pass
    return moved



def _legacy_data_dirs():
    """Folders profiles lived in before v2.5 moved them into the app
    folder. Profiles themselves stay in the current home, but
    user-edited data files (corner names, track guides) are still
    READ from the old homes when not found in the current one
    (v2.5.2) -- a names file dropped in the pre-v2.5 location by
    habit keeps working instead of silently doing nothing."""
    dirs = []
    for ac in _ac_install_candidates():
        dirs.append(os.path.join(ac, "ACLiveCoach", "profiles"))
    dirs.append(os.path.join(os.path.expanduser("~"), "Documents",
                             "Assetto Corsa", "ACLiveCoach", "profiles"))
    dirs.append(os.path.join(APP_DIR, "tracks"))
    return dirs


def _find_data_file(filename):
    """First existing copy of a user data file: the current profiles
    folder first, then the legacy homes. None if it exists nowhere."""
    for base in [TRACKS_DIR] + _legacy_data_dirs():
        path = os.path.join(base, filename)
        if os.path.exists(path):
            return path
    return None


def resolve_tracks_dir(cfg):
    """Profiles live in a `profiles` folder inside the app folder
    (v2.5: the reference is relative to the app folder, and the
    folder is created if it does not exist -- so the whole setup
    travels as one folder, and a git pull in that folder updates
    the code while profiles stay put). The 'profile_dir' config
    setting overrides; a relative value is taken relative to the
    app folder. On first run, profiles from the previous homes
    (the AC install's ACLiveCoach\\profiles, the Documents
    Assetto Corsa folder, the legacy app-folder tracks\\) are
    copied in -- never overwriting -- so standing data follows.
    If the app folder is not writable, the old locations are
    tried as a fallback."""
    global TRACKS_DIR
    legacy = os.path.join(APP_DIR, "tracks")
    pd = (cfg or {}).get("profile_dir") or os.environ.get("ACLIVECOACH_PROFILE_DIR")
    if pd:
        primary = pd if os.path.isabs(pd) else os.path.join(APP_DIR, pd)
        label = "profile_dir setting"
    else:
        primary = os.path.join(APP_DIR, "profiles")
        label = "app folder"
    candidates = [(label, primary)]
    for ac in _ac_install_candidates():
        if os.path.isdir(ac):
            candidates.append(("Assetto Corsa folder",
                               os.path.join(ac, "ACLiveCoach", "profiles")))
    docs = os.path.join(os.path.expanduser("~"), "Documents", "Assetto Corsa")
    if os.path.isdir(docs):
        candidates.append(("Documents Assetto Corsa folder",
                           os.path.join(docs, "ACLiveCoach", "profiles")))
    candidates.append(("app folder (legacy)", legacy))
    for label, path in candidates:
        if os.path.isdir(path):
            ok = _writable_dir(path)
        else:
            parent = os.path.dirname(path)
            ok = os.path.isdir(parent) and _writable_dir(parent)
        if not ok:
            continue
        try:
            os.makedirs(path, exist_ok=True)
        except Exception:
            continue
        TRACKS_DIR = path
        legacy_homes = [legacy]
        for ac in _ac_install_candidates():
            legacy_homes.append(os.path.join(ac, "ACLiveCoach", "profiles"))
            legacy_homes.append(os.path.join(ac, "ACLiveCoach", "tracks"))
        legacy_homes.append(os.path.join(
            os.path.expanduser("~"), "Documents", "Assetto Corsa",
            "ACLiveCoach", "profiles"))
        moved = 0
        for home in legacy_homes:
            moved += _migrate_profiles(home, path)
        msg = f"[profiles] storing profiles in: {path} ({label})"
        if moved:
            msg += f" -- migrated {moved} profile file(s)"
        print(msg)
        return path
    TRACKS_DIR = legacy
    print(f"[profiles] storing profiles in: {legacy} (fallback)")
    return legacy


NBINS = 2000  # position bins around the lap (about 2 m per bin at Buttonwillow)

VERSION = "2.14.1"  # THE version stamp -- releases bump this constant only;
# coach_config.json deliberately carries no version key (it drifted stale once)

DEFAULT_CONFIG = {
    "voice_enabled": True,
    "voice_engine": "piper",      # v2.2: "piper" = offline neural voice; "sapi" = built-in Windows voice
    "piper_voice": "en_US-lessac-high",  # any piper-voices name; downloaded once into voices/
    "piper_length_scale": 1.0,    # piper speech rate: <1 faster, >1 slower
    "exit_clause_gap_ms": 220,    # v2.9.0: silence spliced between an exit line's measurement and its fix
    "voice_comms_fx": True,       # v2.10.0: pit-radio band-limit + saturation on the piper voice
    "voice_comms_click": True,    # v2.10.0: short key-up blip before each cue (part of comms fx)
    "piper_dir": "",              # voice model folder; empty = <app dir>/voices
    "voice_rate": 178,            # words per minute (SAPI engine)
    "voice": "",                  # SAPI voice name fragment ("zira", "david"); empty = system default
    "approach_distance_m": 260,   # how far before the brake zone the cue fires
    "min_loss_to_speak": 0.15,    # seconds; smaller losses stay silent
    "exit_min_loss_s": 0.2,       # in-corner loss needed for instant feedback
    "record_frames": True,        # raw frames into the session log at full rate (replayable)
    "off_min_duration_s": 0.35,   # shortest 3+ tyres-off excursion that excuses a corner
    "off_min_duration_2tyres_s": 0.9,  # a 2-tyre excursion must last this long to excuse
    "off_repeat_laps": 3,         # same corner excused this many laps running -> it counts as line
    "out_lap_briefing": True,     # explain the out-lap rules (and best use of the lap) once per session
    "profile_dir": "",            # folder for profile JSONs; empty = <app folder>/profiles; relative paths are taken from the app folder
    "max_approach_cues_per_lap": 5,
    "cue_cooldown_s": 6.0,        # minimum gap between spoken lines
    "open_report_window": True,   # pop the post-session map report in a browser
    "heading_split_deg": 20,      # v2.0.5: min heading swing to split a wide zone (was hard-coded 25)
    "focus": "all",               # v2.1.0: "brake" = brake-coaching mode, approach cues give braking fixes only
    "guide_spoken_tips": False,   # v2.3: speak track-guide tips aloud (once per corner per session); the guide always feeds the report and cue priority
    "guide_symptom_tips": True,   # v2.4: when a data cue fires, a guide tip tagged with the same symptom replaces the generic wording
    "section_feedback": True,     # v2.1.0: one line per track section when its small losses add up
    "section_min_loss_s": 0.45,   # section total that earns a section line
    "expected_track_hint": "buttonwillow",  # soft check only; app runs anywhere
    "brake_tone": True,           # v2.12.0: one short tone at the reference brake point per corner (default on since v2.14.1, Albert's call)
    "brake_tone_volume": 0.7,     # tone loudness 0..1
    "brake_tone_lead_m": 0,       # fire the tone this many metres before the brake point
    "brake_tone_late_m": 25,      # v2.13.0: a tone held by a busy channel may still fire up to this many metres past the point
    "position_fallback": True,    # v2.13.0: when a track reports no spline position, coach by distance (lap timer + odometer)
    "sass_level": 3,              # v2.14.0: coach personality spectrum: 0 = pro, 1 = dry summaries, 2 = + repeat-offender/off-pace lines, 3 = sharpest pools (default 3 since v2.14.1, Albert's call)
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    path = os.path.join(APP_DIR, "coach_config.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            print(f"[config] could not read coach_config.json ({e}); using defaults")
    return cfg


# --------------------------------------------------------------------------
# Assetto Corsa shared memory (Windows). Layouts follow Kunos' published
# SPageFilePhysics / SPageFileGraphic / SPageFileStatic structs. Only the
# stable front fields are relied on.
# --------------------------------------------------------------------------

class SPageFilePhysics(ctypes.Structure):
    _fields_ = [
        ("packetId", c_int),
        ("gas", c_float), ("brake", c_float), ("fuel", c_float),
        ("gear", c_int), ("rpms", c_int),
        ("steerAngle", c_float), ("speedKmh", c_float),
        ("velocity", c_float * 3), ("accG", c_float * 3),
        ("wheelSlip", c_float * 4), ("wheelLoad", c_float * 4),
        ("wheelsPressure", c_float * 4), ("wheelAngularSpeed", c_float * 4),
        ("tyreWear", c_float * 4), ("tyreDirtyLevel", c_float * 4),
        ("tyreCoreTemperature", c_float * 4), ("camberRAD", c_float * 4),
        ("suspensionTravel", c_float * 4),
        ("drs", c_float), ("tc", c_float),
        ("heading", c_float), ("pitch", c_float), ("roll", c_float),
        ("cgHeight", c_float),
        ("carDamage", c_float * 5),
        ("numberOfTyresOut", c_int), ("pitLimiterOn", c_int),
        ("abs", c_float),
        ("kersCharge", c_float), ("kersInput", c_float), ("autoShifterOn", c_int),
        ("rideHeight", c_float * 2),
        ("turboBoost", c_float), ("ballast", c_float), ("airDensity", c_float),
        ("airTemp", c_float), ("roadTemp", c_float),
        ("localAngularVelocity", c_float * 3),
        ("finalFF", c_float),
        ("performanceMeter", c_float), ("engineBrake", c_int),
        ("ersRecoveryLevel", c_int), ("ersPowerLevel", c_int),
        ("ersHeatCharging", c_int), ("ersIsCharging", c_int),
        ("kersCurrentKJ", c_float),
        ("drsAvailable", c_int), ("drsEnabled", c_int),
        ("brakeTemp", c_float * 4),
        ("clutch", c_float),
        ("tyreTempI", c_float * 4), ("tyreTempM", c_float * 4), ("tyreTempO", c_float * 4),
        ("isAIControlled", c_int),
        ("tyreContactPoint", (c_float * 3) * 4),
        ("tyreContactNormal", (c_float * 3) * 4),
        ("tyreContactHeading", (c_float * 3) * 4),
        ("brakeBias", c_float),
        ("localVelocity", c_float * 3),
        ("P2PActivations", c_int), ("P2PStatus", c_int),
        ("currentMaxRpm", c_float),
        ("mz", c_float * 4), ("fx", c_float * 4), ("fy", c_float * 4),
        ("slipRatio", c_float * 4), ("slipAngle", c_float * 4),
        ("tcinAction", c_int), ("absInAction", c_int),
        ("suspensionDamage", c_float * 4), ("tyreTemp", c_float * 4),
    ]


class SPageFileGraphic(ctypes.Structure):
    _fields_ = [
        ("packetId", c_int), ("status", c_int), ("session", c_int),
        ("currentTime", c_wchar * 15), ("lastTime", c_wchar * 15),
        ("bestTime", c_wchar * 15), ("split", c_wchar * 15),
        ("completedLaps", c_int), ("position", c_int),
        ("iCurrentTime", c_int), ("iLastTime", c_int), ("iBestTime", c_int),
        ("sessionTimeLeft", c_float), ("distanceTraveled", c_float),
        ("isInPit", c_int), ("currentSectorIndex", c_int),
        ("lastSectorTime", c_int), ("numberOfLaps", c_int),
        ("tyreCompound", c_wchar * 33),
        ("replayTimeMultiplier", c_float), ("normalizedCarPosition", c_float),
        ("activeCars", c_int),
        ("carCoordinates", (c_float * 3) * 60), ("carID", c_int * 60),
        ("playerCarID", c_int),
        ("penaltyTime", c_float), ("flag", c_int),
        ("idealLineOn", c_int), ("isInPitLine", c_int),
        ("surfaceGrip", c_float),
        ("mandatoryPitDone", c_int), ("windSpeed", c_float), ("windDirection", c_float),
        ("isSetupMenuVisible", c_int),
    ]


class SPageFileStatic(ctypes.Structure):
    _fields_ = [
        ("smVersion", c_wchar * 15), ("acVersion", c_wchar * 15),
        ("numberOfSessions", c_int), ("numCars", c_int),
        ("carModel", c_wchar * 33), ("track", c_wchar * 33),
        ("playerName", c_wchar * 33), ("playerSurname", c_wchar * 33),
        ("playerNick", c_wchar * 33),
        ("sectorCount", c_int),
        ("maxTorque", c_float), ("maxPower", c_float),
        ("maxRpm", c_int), ("maxFuel", c_float),
        ("suspensionMaxTravel", c_float * 4), ("tyreRadius", c_float * 4),
    ]


AC_OFF, AC_REPLAY, AC_LIVE, AC_PAUSE = 0, 1, 2, 3


class ACTelemetry:
    """Reads AC shared memory on Windows. Raises on other platforms."""

    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("AC shared memory is only available on Windows")
        import mmap
        self._mm_phys = mmap.mmap(-1, ctypes.sizeof(SPageFilePhysics),
                                  "Local\\acpmf_physics", access=mmap.ACCESS_READ)
        self._mm_gfx = mmap.mmap(-1, ctypes.sizeof(SPageFileGraphic),
                                 "Local\\acpmf_graphics", access=mmap.ACCESS_READ)
        self._mm_stat = mmap.mmap(-1, ctypes.sizeof(SPageFileStatic),
                                  "Local\\acpmf_static", access=mmap.ACCESS_READ)

    def physics(self):
        return SPageFilePhysics.from_buffer_copy(self._mm_phys)

    def graphics(self):
        return SPageFileGraphic.from_buffer_copy(self._mm_gfx)

    def static(self):
        return SPageFileStatic.from_buffer_copy(self._mm_stat)


# --------------------------------------------------------------------------
# Voice output. Two engines:
#   "piper" (v2.2, default) -- offline neural TTS (pip install piper-tts).
#       The voice model (~63 MB for a medium voice) downloads ONCE into
#       <app dir>/voices/ and then runs fully offline. Rendering happens
#       on the speech thread; playback is winsound from memory.
#   "sapi" -- pyttsx3 / the built-in Windows voices (Zira & friends).
#       Kept as the automatic fallback whenever Piper is unavailable,
#       and selectable with "voice_engine": "sapi".
# --------------------------------------------------------------------------

PIPER_BASE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main"


def _piper_urls(voice_name):
    # "en_US-lessac-medium" -> .../en/en_US/lessac/medium/<name>.onnx[.json]
    parts = voice_name.split("-")
    locale = parts[0]
    lang = locale.split("_")[0]
    name = parts[1] if len(parts) > 1 else "lessac"
    quality = parts[2] if len(parts) > 2 else "medium"
    base = f"{PIPER_BASE_URL}/{lang}/{locale}/{name}/{quality}/{voice_name}"
    return base + ".onnx", base + ".onnx.json"



# v2.5.3: the default voice's downloads are verified against these
# SHA-256 hashes (taken from the files as published). Other voices
# are shape-validated but unpinned -- piper-tts prints no hash for
# them here, so they load as downloaded.
PIPER_PINNED_SHA256 = {
    "en_US-lessac-high": {
        "onnx": "4cabf7c3a638017137f34a1516522032d4fe3f38228a843cc9b764ddcbcd9e09",
        "json": "db42b97d9859f257bc1561b8ed980e7fb2398402050a74ddd6cbec931a92412f",
    },
    "en_US-lessac-medium": {
        "onnx": "5efe09e69902187827af646e1a6e9d269dee769f9877d17b16b1b46eeaaf019f",
        "json": "efe19c417bed055f2d69908248c6ba650fa135bc868b0e6abb3da181dab690a0",
    },
}
_PIPER_VOICE_RE = re.compile(r"^[a-z]{2}_[A-Z]{2}-[a-z0-9_]+-(x_low|low|medium|high)$")


def _sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()



def _splice_wavs(a_bytes, b_bytes, gap_s):
    """Join two WAV byte strings with gap_s of true silence between
    them (v2.9.0). Returns None when the formats differ -- the caller
    falls back to speaking the clauses separately."""
    import io
    import wave
    try:
        wa = wave.open(io.BytesIO(a_bytes), "rb")
        wb = wave.open(io.BytesIO(b_bytes), "rb")
        params = wa.getparams()
        if (wb.getframerate() != params.framerate
                or wb.getnchannels() != params.nchannels
                or wb.getsampwidth() != params.sampwidth):
            return None
        frames_a = wa.readframes(wa.getnframes())
        frames_b = wb.readframes(wb.getnframes())
        silence = b"\x00" * (int(params.framerate * gap_s)
                             * params.sampwidth * params.nchannels)
        out = io.BytesIO()
        with wave.open(out, "wb") as wf:
            wf.setparams(params)
            wf.writeframes(frames_a + silence + frames_b)
        return out.getvalue()
    except Exception:
        return None


def _comms_fx(wav_bytes, click=True):
    """Make a rendered cue sound like a pit-radio transmission
    (v2.10.0): band-limit to the comms channel (~300 Hz-3.4 kHz),
    a touch of saturation for grit, level normalized, and a short
    key-up blip in front -- the sound of an engineer keying the
    mic. Pure stdlib, mono 16-bit only; any format surprise returns
    the audio untouched, because the effect must never eat a cue."""
    import io
    import math
    import random
    import sys
    import wave
    from array import array
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            params = wf.getparams()
            raw = wf.readframes(wf.getnframes())
        if params.nchannels != 1 or params.sampwidth != 2:
            return wav_bytes
        rate = params.framerate
        samples = array("h")
        samples.frombytes(raw)
        if sys.byteorder == "big":
            samples.byteswap()
        x = [v / 32768.0 for v in samples]
        dt = 1.0 / rate
        # The comms channel, deliberately overdone (v2.10.1: the
        # first cut was too polite -- the driver heard the click
        # but wanted real radio distortion). Band-limit hard, drive
        # it into saturation, then crush the resolution like a
        # cheap digital radio, with an open-channel hiss under it.
        # high-pass ~400 Hz (one-pole, two passes)
        rc = 1.0 / (2.0 * math.pi * 400.0)
        a_hp = rc / (rc + dt)
        y = list(x)
        for _ in range(2):
            prev_x = 0.0
            prev_y = 0.0
            for i, v in enumerate(y):
                prev_y = a_hp * (prev_y + v - prev_x)
                y[i] = prev_y
                prev_x = v
        # low-pass ~2.9 kHz, two passes
        rc = 1.0 / (2.0 * math.pi * 2900.0)
        a_lp = dt / (rc + dt)
        for _ in range(2):
            acc = 0.0
            for i, v in enumerate(y):
                acc += a_lp * (v - acc)
                y[i] = acc
        # drive: hard into the tanh shoulder -- transmitted radios clip
        y = [math.tanh(3.2 * v) for v in y]
        # resolution crush: half-rate, coarsely quantized, stretched
        # back to full rate -- the digital-radio crunch
        half = [round(v * 48.0) / 48.0 for v in y[::2]]
        crushed = [0.0] * len(y)
        for i, v in enumerate(half):
            crushed[2 * i] = v
            if 2 * i + 1 < len(crushed):
                nxt = half[i + 1] if i + 1 < len(half) else v
                crushed[2 * i + 1] = 0.5 * (v + nxt)
        y = crushed
        # open-channel hiss under the voice (fixed seed, so a cue
        # sounds the same every time it is spoken)
        rng = random.Random(20261103)
        noise = [rng.uniform(-1.0, 1.0) for _ in y]
        acc = 0.0
        for i, v in enumerate(noise):
            acc += a_lp * (v - acc)
            noise[i] = acc
        y = [v + 0.038 * n for v, n in zip(y, noise)]
        # normalize the level
        peak = max((abs(v) for v in y), default=0.0)
        if peak > 1e-4:
            g = 0.89 / peak
            y = [v * g for v in y]
        prefix = []
        if click:
            n_tone = int(rate * 0.030)
            n_gap = int(rate * 0.025)
            fade = max(1.0, rate * 0.005)
            for i in range(n_tone):
                env = min(1.0, i / fade, (n_tone - i) / fade)
                prefix.append(0.30 * env
                              * math.sin(2.0 * math.pi * 1200.0 * i / rate))
            prefix.extend([0.0] * n_gap)
        out = array("h", (max(-32767, min(32767, int(round(v * 32767))))
                          for v in (prefix + y)))
        if sys.byteorder == "big":
            out.byteswap()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setparams(params)
            wf.writeframes(out.tobytes())
        return buf.getvalue()
    except Exception:
        return wav_bytes


class Speaker:
    def __init__(self, cfg, log_fn):
        self.cfg = cfg
        self.log = log_fn
        self.q = queue.Queue()
        self._speaking = False
        self._tone_cache = {}
        self.enabled = bool(cfg.get("voice_enabled", True))
        self._voice_id = None
        self._voice_resolved = False
        self.engine = (cfg.get("voice_engine") or "piper").strip().lower()
        self._piper_voice = None
        self._piper_ready = False
        self._piper_disabled = False
        self._piper_fails = 0
        self._thread = None
        if not self.enabled:
            return
        have_piper = have_sapi = False
        try:
            import piper  # noqa: F401
            have_piper = True
        except Exception:
            pass
        try:
            import pyttsx3  # noqa: F401
            have_sapi = True
        except Exception:
            pass
        if self.engine == "piper" and not have_piper:
            print("[voice] piper-tts is not installed (pip install piper-tts); "
                  "falling back to the Windows voice")
            self.engine = "sapi"
        if self.engine == "sapi" and not have_sapi:
            if have_piper:
                print("[voice] pyttsx3 unavailable; using piper")
                self.engine = "piper"
            else:
                print("[voice] no speech engine available; cues will print instead")
                self.enabled = False
                return
        if self.engine == "piper":
            print(f"[voice] engine: piper "
                  f"({cfg.get('piper_voice') or 'en_US-lessac-medium'}), offline neural")
        else:
            print(f"[voice] engine: sapi ({cfg.get('voice') or 'system default'})")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # -- piper ------------------------------------------------------------

    def warm(self):
        """Load (and, the first time, download) the Piper voice at
        launch, so the cost lands before the out lap instead of on
        the first corner cue. No-op for the SAPI engine."""
        if self.enabled and self.engine == "piper":
            self._piper_ensure()

    def _voices_dir(self):
        d = (self.cfg.get("piper_dir") or "").strip()
        path = d if d else os.path.join(APP_DIR, "voices")
        os.makedirs(path, exist_ok=True)
        return path

    @staticmethod
    def _download(url, dest):
        import shutil
        import urllib.request
        tmp = dest + ".part"
        with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f, length=1024 * 256)
        os.replace(tmp, dest)

    def _piper_ensure(self):
        if self._piper_ready:
            return True
        if self._piper_disabled:
            return False
        try:
            name = (self.cfg.get("piper_voice") or "en_US-lessac-medium").strip()
            if not _PIPER_VOICE_RE.match(name):
                print(f"[voice] piper_voice '{name}' is not a valid voice "
                      f"name; using en_US-lessac-medium")
                name = "en_US-lessac-medium"
            vdir = self._voices_dir()
            model = os.path.join(vdir, name + ".onnx")
            conf = model + ".json"
            pinned = PIPER_PINNED_SHA256.get(name)

            def _hashes_ok():
                if not pinned:
                    return True
                return (os.path.exists(model) and os.path.exists(conf)
                        and _sha256_of(model) == pinned["onnx"]
                        and _sha256_of(conf) == pinned["json"])

            if not (os.path.exists(model) and os.path.exists(conf)):
                onnx_url, json_url = _piper_urls(name)
                print(f"[voice] downloading the piper voice '{name}' "
                      f"(~63 MB, one time only)...")
                self._download(json_url, conf)
                self._download(onnx_url, model)
                print("[voice] voice downloaded; it works offline from here on")
            elif pinned and not _hashes_ok():
                print("[voice] the stored voice files fail their integrity "
                      "check; downloading them again")
                onnx_url, json_url = _piper_urls(name)
                self._download(json_url, conf)
                self._download(onnx_url, model)
            if pinned and not _hashes_ok():
                raise RuntimeError("voice files failed the SHA-256 check")
            from piper import PiperVoice
            self._piper_voice = PiperVoice.load(model, config_path=conf)
            self._piper_ready = True
            print(f"[voice] piper voice ready: {name}")
            return True
        except Exception as e:
            print(f"[voice] piper unavailable ({e}); "
                  f"using the Windows voice this session")
            try:
                self.log("voice_error", {"text": "<piper setup>", "error": str(e)})
            except Exception:
                pass
            self._piper_disabled = True
            return False

    def _piper_render(self, text):
        import io
        import wave
        buf = io.BytesIO()
        syn = None
        try:
            from piper.config import SynthesisConfig
            syn = SynthesisConfig(
                length_scale=float(self.cfg.get("piper_length_scale", 1.0)))
        except Exception:
            syn = None
        with wave.open(buf, "wb") as wf:
            if syn is not None:
                self._piper_voice.synthesize_wav(text, wf, syn_config=syn)
            else:
                self._piper_voice.synthesize_wav(text, wf)
        return buf.getvalue()

    def _speak_piper(self, text):
        if not self._piper_ensure():
            return False
        try:
            data = self._maybe_comms(self._piper_render(text))
            import winsound
            # SND_MEMORY plays the WAV bytes directly. Synchronous (no
            # SND_ASYNC) so cues stay in order on the speech thread.
            winsound.PlaySound(data, winsound.SND_MEMORY)
            self._piper_fails = 0
            return True
        except Exception as e:
            print(f"[voice] piper error on a cue ({e})")
            try:
                self.log("voice_error", {"text": text, "error": str(e)})
            except Exception:
                pass
            return False

    def _maybe_comms(self, data):
        """The pit-radio treatment (v2.10.0), when enabled. Piper
        path only -- SAPI speaks directly and cannot be processed."""
        if self.cfg.get("voice_comms_fx", True):
            return _comms_fx(
                data, click=bool(self.cfg.get("voice_comms_click", True)))
        return data

    def _speak_piper_clauses(self, a, b, gap_ms):
        if not self._piper_ensure():
            return False
        try:
            data = _splice_wavs(self._piper_render(a),
                                self._piper_render(b),
                                max(0, int(gap_ms)) / 1000.0)
            if data is None:
                return False
            data = self._maybe_comms(data)
            import winsound
            winsound.PlaySound(data, winsound.SND_MEMORY)
            self._piper_fails = 0
            return True
        except Exception as e:
            print(f"[voice] piper error on a cue ({e})")
            return False

    # -- sapi ---------------------------------------------------------------

    def _apply_voice(self, engine):
        # Voice choice comes from the 'voice' config string: a
        # case-insensitive substring matched against installed SAPI
        # voice names (e.g. "zira" or "david"). Empty = system
        # default. Resolved once, against the first engine created.
        query = (self.cfg.get("voice") or "").strip().lower()
        if not query:
            return
        if not self._voice_resolved:
            self._voice_resolved = True
            try:
                voices = engine.getProperty("voices")
            except Exception:
                voices = []
            for v in voices:
                if (query in (getattr(v, "name", "") or "").lower()
                        or query in (getattr(v, "id", "") or "").lower()):
                    self._voice_id = v.id
                    print(f"[voice] using voice: {v.name}")
                    break
            if self._voice_id is None:
                names = ", ".join(v.name for v in voices) or "none found"
                print(f"[voice] no installed voice matches '{query}' "
                      f"(available: {names}); using the system default")
        if self._voice_id:
            try:
                engine.setProperty("voice", self._voice_id)
            except Exception:
                pass

    def _speak_sapi(self, text):
        # A single long-lived pyttsx3 engine on Windows speaks the first
        # utterance and then goes silent: runAndWait() leaves the SAPI5
        # event loop in a stopped state, so later say() calls queue into
        # a dead loop and never play. (Exactly what Albert hit in his
        # first real session, Nov 1: heard "coaching starts next lap",
        # then nothing, while the log kept recording cues.) Reliable
        # pattern: fresh engine per cue. The ~0.2s init cost is fine --
        # coaching cues are sparse by design.
        engine = None
        try:
            import pyttsx3
            engine = pyttsx3.init()
            engine.setProperty("rate", int(self.cfg.get("voice_rate", 178)))
            self._apply_voice(engine)
            engine.say(text)
            engine.runAndWait()
            return True
        except Exception as e:
            print(f"[voice] error speaking cue ({e})")
            try:
                self.log("voice_error", {"text": text, "error": str(e)})
            except Exception:
                pass
            return False
        finally:
            if engine is not None:
                try:
                    engine.stop()
                except Exception:
                    pass

    def _speak_clauses(self, a, b, gap_ms):
        if self.engine == "piper" and not self._piper_disabled:
            if self._speak_piper_clauses(a, b, gap_ms):
                return
            self._piper_fails += 1
            if self._piper_fails >= 3:
                self._piper_disabled = True
                print("[voice] piper failed 3 cues in a row; "
                      "the Windows voice takes over for this session")
        # Two separate utterances: the fresh SAPI engine per clause
        # gives the beat on its own.
        self._speak(a)
        self._speak(b)

    # -- queue --------------------------------------------------------------

    def _speak(self, text):
        if self.engine == "piper" and not self._piper_disabled:
            if self._speak_piper(text):
                return
            self._piper_fails += 1
            if self._piper_fails >= 3:
                self._piper_disabled = True
                print("[voice] piper failed 3 cues in a row; "
                      "the Windows voice takes over for this session")
        self._speak_sapi(text)

    def _run(self):
        while True:
            item = self.q.get()
            if item is None:
                break
            ts, text, clauses = item
            if clauses and clauses[0] == "__tone__":
                # A brake tone is only true at its moment: played
                # within ~1.5 s of the brake point or not at all --
                # never as a late surprise behind queued speech.
                if time.time() - ts <= 1.5:
                    self._speaking = True
                    try:
                        self._play_tone()
                    except Exception:
                        pass
                    self._speaking = False
                continue
            if time.time() - ts > 15.0:
                # A cue that waited >15 s in the queue is about a corner
                # the driver has already finished. Speaking it late is
                # worse than dropping it -- this is what made session 2
                # feel "a lap behind."
                print(f"  >> (dropped stale cue: {text})")
                try:
                    self.log("cue_dropped_stale", {"text": text})
                except Exception:
                    pass
                continue
            try:
                self._speaking = True
                if clauses:
                    self._speak_clauses(*clauses)
                else:
                    self._speak(text)
            except Exception:
                pass
            self._speaking = False

    def say(self, text):
        self.log("cue", {"text": text})
        print(f"  >> COACH: {text}")
        if self.enabled:
            self.q.put((time.time(), text, None))

    def say_clauses(self, a, b, gap_ms=220):
        """Speak two clauses with a real beat of silence between
        them (v2.9.0): exit feedback reads as measurement, pause,
        instruction, instead of one welded sentence. The session log
        keeps the familiar single-line 'a -- b' form."""
        text = f"{a} -- {b}"
        self.log("cue", {"text": text})
        print(f"  >> COACH: {text}")
        if self.enabled:
            self.q.put((time.time(), text, (a, b, int(gap_ms))))

    # -- brake tone (v2.12.0) ------------------------------------------

    def tone_ready(self):
        """True when the audio channel is idle: nothing playing,
        nothing queued. A brake tone fired into a busy channel would
        land late or talk over a cue, so the coach drops it instead
        (a late tone teaches the wrong brake point)."""
        return bool(self.enabled and not self._speaking and self.q.empty())

    def tone(self):
        if self.enabled:
            self.q.put((time.time(), "", ("__tone__",)))

    @staticmethod
    def _render_tone_wav(volume):
        """A single 150 ms sine burst at 880 Hz with a raised-cosine
        envelope (no clicks at the ends), 22050 Hz 16-bit mono -- the
        format Piper renders, so it plays through the same winsound
        path. Deliberately NOT run through the comms fx: the radio
        treatment would smear a tone this short, and the tone is a
        marker, not a transmission."""
        import io
        import struct
        import wave
        sr = 22050
        n = int(sr * 0.15)
        vol = min(1.0, max(0.0, float(volume)))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            frames = bytearray()
            for i in range(n):
                env = 0.5 - 0.5 * math.cos(2 * math.pi * i / max(1, n - 1))
                s = math.sin(2 * math.pi * 880 * i / sr) * env * vol * 0.9
                frames += struct.pack("<h", int(32767 * s))
            wf.writeframes(bytes(frames))
        return buf.getvalue()

    def _play_tone(self):
        vol = float(self.cfg.get("brake_tone_volume", 0.7))
        key = round(vol, 2)
        data = self._tone_cache.get(key)
        if data is None:
            data = self._render_tone_wav(vol)
            self._tone_cache[key] = data
        import winsound
        winsound.PlaySound(data, winsound.SND_MEMORY)


# --------------------------------------------------------------------------
# Track profile: reference lap bins + corner map, persisted per track/car.
# --------------------------------------------------------------------------

def smooth(values, window=9):
    n = len(values)
    half = window // 2
    out = [0.0] * n
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        out[i] = sum(values[lo:hi]) / (hi - lo)
    return out


def _zone_from_apex(apex, vmin, spd, ref_brake, ref_gas, bin_m):
    """Build one analysis zone around a speed minimum: brake point by
    scanning back for brake application, throttle point by scanning
    forward for sustained full throttle."""
    n = len(spd)
    back = max(10, int(420 / bin_m))
    lo = max(0, apex - back)
    # v2.0.5 brake-point sanity: the old scan took the FURTHEST brake
    # application within 420 m of the apex, so a check-up tap on the
    # straight became the corner's reference brake point (a Nov 1
    # session logged brake deltas of +230 m at one corner all night).
    # Now collect brake ONSETS in the window and take the one nearest
    # the apex that is COMMITTED -- pedal held past 0.08 for at
    # least ~18 m of travel. Fall back to the nearest onset, then to
    # the old no-brake-zone default.
    sustain = max(3, int(18 / bin_m))
    onsets = []
    for j in range(lo, apex + 1):
        if ref_brake[j] > 0.08 and (j == lo or ref_brake[j - 1] <= 0.08):
            held = 0
            k = j
            while k <= apex and ref_brake[k] > 0.08:
                held += 1
                k += 1
            onsets.append((j, held))
    committed = [j for j, held in onsets if held >= sustain]
    if committed:
        brake_bin = committed[-1]
        has_brake_zone = True
    elif onsets:
        brake_bin = onsets[-1][0]
        has_brake_zone = True
    else:
        brake_bin = max(0, apex - int(100 / bin_m))
        has_brake_zone = False
    thr_bin = min(n - 1, apex + int(80 / bin_m))
    run = 0
    for j in range(apex, min(n, apex + int(400 / bin_m))):
        if ref_gas[j] >= 0.95:
            run += 1
            if run >= 3:
                thr_bin = j - 2
                break
        else:
            run = 0
    return {
        "apex_bin": apex,
        "apex_speed_kmh": round(vmin, 1),
        "brake_bin": brake_bin,
        "has_brake_zone": has_brake_zone,
        "throttle_bin": thr_bin,
        "zone_start": max(0, brake_bin - max(3, int(30 / bin_m))),
        "zone_end": min(n - 1, thr_bin + max(5, int(110 / bin_m))),
    }


def _heading_splits(hdg, z0, z1, bin_m, thresh_deg=25.0, diag=None):
    """Bins inside [z0, z1] where the car's heading reverses trend by
    25+ degrees -- the seams between distinct turns hiding inside one
    wide speed trough (Cotton Corners is three turns but a single
    trough in Albert's speed trace). Heading is per-bin radians and
    may contain None; any gap disables splitting for that zone."""
    vals = hdg[z0:z1 + 1]
    if len(vals) < 40 or any(v is None for v in vals):
        return []
    u = [vals[0]]
    for v in vals[1:]:
        d = (v - u[-1] + math.pi) % (2 * math.pi) - math.pi
        u.append(u[-1] + d)
    w = max(2, int(15 / bin_m))
    sm = []
    for i in range(len(u)):
        seg = u[max(0, i - w):i + w + 1]
        sm.append(sum(seg) / len(seg))
    thresh = math.radians(thresh_deg)
    # Extrema of the smoothed heading (window maxima/minima), merged
    # to alternating kinds, then a swing filter: an extremum counts
    # as a seam only when heading swung at least thresh from the
    # previous kept extremum. (A first version tracked the running
    # extreme with two opposing branches that overwrote each other
    # while no trend was established, and found nothing.)
    w2 = max(5, int(45 / bin_m))
    ext = []
    for i in range(len(sm)):
        seg = sm[max(0, i - w2):i + w2 + 1]
        if sm[i] == max(seg) and sm[i] > min(seg):
            kind = 1
        elif sm[i] == min(seg) and sm[i] < max(seg):
            kind = -1
        else:
            continue
        if ext and i - ext[-1][0] <= 2:
            p, pk = ext[-1]
            if kind == pk and ((kind == 1 and sm[i] > sm[p])
                               or (kind == -1 and sm[i] < sm[p])):
                ext[-1] = (i, kind)
            continue
        ext.append((i, kind))
    merged = []
    for i, kind in ext:
        if merged and merged[-1][1] == kind:
            p = merged[-1][0]
            if (kind == 1 and sm[i] > sm[p]) or (kind == -1 and sm[i] < sm[p]):
                merged[-1] = (i, kind)
        else:
            merged.append((i, kind))
    # Extrema within min_gap of the slice edges are boundary
    # artefacts, not turns -- drop them BEFORE the swing chain
    # (v2.0.5: an edge artefact with a mid-range heading value used
    # to anchor the chain, and every real extremum was then
    # measured against it instead of against its neighbour).
    min_gap = max(4, int(60 / bin_m))
    interior = [i for i, _k in merged
                if min_gap <= i <= len(sm) - 1 - min_gap]
    if diag is not None:
        # v2.0.5 diagnostics: every candidate swing between
        # consecutive interior extrema, threshold or not -- so a
        # session log shows how close a complex came to splitting
        # (the Cotton Corners third-turn question).
        diag.append([round(abs(math.degrees(sm[b] - sm[a])), 1)
                     for a, b in zip(interior, interior[1:])])
    kept = []
    for i in interior:
        if not kept or abs(sm[i] - sm[kept[-1]]) >= thresh:
            kept.append(i)
    # The extrema are the turns' apex regions; the SEAM between two
    # turns is midway between consecutive extrema (where the car is
    # transitioning, steering crossing back).
    splits = [z0 + (a + b) // 2 for a, b in zip(kept, kept[1:])]
    out = []
    for s in splits:
        if (s - z0 >= min_gap and z1 - s >= min_gap
                and (not out or s - out[-1] >= min_gap)):
            out.append(s)
    return out[:3]


def detect_corners(ref_speed, ref_brake, ref_gas, bin_m, ref_hdg=None,
                   split_thresh_deg=25.0, split_diag=None):
    """Find corners in a reference lap's binned traces.

    v2.0 (Albert's request, Nov 1): a FINER map -- more places the
    coach can comment, without commenting everywhere every lap.
    Changes from the v1 detector:
    - dips down to 9 km/h count (was 18); dips under 18 are flagged
      "minor" and held to higher speaking thresholds elsewhere.
    - minima merge only when the speed rise between them is small
      (<10 km/h within 120 m). Two dips with a real rise between
      them are two corners -- the v1 rule swallowed the Sweeper's
      dip into the Esses zone on Albert's own reference lap.
    - a zone wider than 200 m is split at heading direction changes
      when the reference lap's heading was recorded: complexes that
      are several turns but one speed trough become several zones.
    - the map is capped at 16 zones, shallowest minors dropped first.
    """
    spd = smooth(ref_speed, 11)
    n = len(spd)
    minima = []
    for i in range(2, n - 2):
        if spd[i] <= spd[i - 1] and spd[i] <= spd[i + 1] and spd[i] < 210:
            # prominence: how far speed rises within 250 m each side
            reach = max(10, int(250 / bin_m))
            left_max = max(spd[max(0, i - reach):i + 1])
            right_max = max(spd[i:min(n, i + reach)])
            dip = min(left_max - spd[i], right_max - spd[i])
            if dip >= 9:
                minima.append((i, spd[i], dip))
    merged = []
    hard_gap = max(4, int(35 / bin_m))    # same-corner wobble
    soft_gap = max(5, int(120 / bin_m))   # merge only on a small rise
    for idx, v, dip in minima:
        if merged:
            pidx, pv, _pd = merged[-1]
            gap = idx - pidx
            if gap < hard_gap:
                if v < pv:
                    merged[-1] = (idx, v, dip)
                continue
            if gap < soft_gap:
                rise = max(spd[pidx:idx + 1]) - max(v, pv)
                if rise < 10.0:
                    if v < pv:
                        merged[-1] = (idx, v, dip)
                    continue
        merged.append((idx, v, dip))
    corners = []
    for apex, vmin, dip in merged:
        c = _zone_from_apex(apex, vmin, spd, ref_brake, ref_gas, bin_m)
        c["minor"] = dip < 18
        c["dip_kmh"] = round(dip, 1)
        corners.append(c)
    if ref_hdg:
        split_out = []
        for c in corners:
            span_m = (c["zone_end"] - c["zone_start"]) * bin_m
            diag = [] if split_diag is not None else None
            splits = (_heading_splits(ref_hdg, c["zone_start"], c["zone_end"],
                                      bin_m, thresh_deg=split_thresh_deg,
                                      diag=diag)
                      if span_m > 200 else [])
            if split_diag is not None and diag:
                split_diag.append({"apex_bin": c["apex_bin"],
                                   "swings_deg": diag[0],
                                   "splits": len(splits)})
            if not splits:
                split_out.append(c)
                continue
            bounds = [c["zone_start"]] + splits + [c["zone_end"] + 1]
            for k in range(len(bounds) - 1):
                s0, s1 = bounds[k], bounds[k + 1] - 1
                apex = min(range(s0, s1 + 1), key=lambda j: spd[j])
                sub = _zone_from_apex(apex, spd[apex], spd, ref_brake, ref_gas, bin_m)
                sub["zone_start"] = max(sub["zone_start"], s0)
                sub["zone_end"] = min(sub["zone_end"], s1)
                sub["minor"] = c["minor"]
                sub["dip_kmh"] = c["dip_kmh"]
                sub["split"] = True
                split_out.append(sub)
        corners = split_out
    # Overlapping wide zones over one complex (shared brake/throttle
    # points) can split into near-identical children -- keep one zone
    # per apex neighbourhood.
    dedup = []
    for c in sorted(corners, key=lambda c: c["apex_bin"]):
        if dedup and (c["apex_bin"] - dedup[-1]["apex_bin"]) * bin_m < 35:
            continue
        dedup.append(c)
    corners = dedup
    while len(corners) > 16:
        pool = [c for c in corners if c.get("minor")] or corners
        corners.remove(min(pool, key=lambda c: c.get("dip_kmh", 99.0)))
    corners.sort(key=lambda c: c["apex_bin"])
    # keep zones from overlapping the next corner's zone
    for i in range(len(corners) - 1):
        nxt = corners[i + 1]["zone_start"]
        if corners[i]["zone_end"] >= nxt:
            corners[i]["zone_end"] = max(corners[i]["apex_bin"], nxt - 1)
    out = []
    for num, c in enumerate(corners, start=1):
        d = {"num": num, "name": f"Turn {num}"}
        d.update(c)
        out.append(d)
    return out


def fmt_lap(seconds):
    if seconds is None or seconds <= 0:
        return "--:--.-"
    m = int(seconds // 60)
    s = seconds - 60 * m
    return f"{m}:{s:04.1f}"


# v2.1.1 display units (Albert, Nov 2: "I'm in the USA, default to
# mph and anything else we use"). AC publishes km/h and the coach's
# maths stays metric internally; only what is SHOWN converts --
# speeds to mph, short distances to feet, track length to miles.
MPH_PER_KMH = 0.621371
FT_PER_M = 3.28084
M_PER_MILE = 1609.344


def fmt_track_len(m):
    return f"{m / M_PER_MILE:.2f} mi" if m else "unknown length"


def fmt_lap_spoken(seconds):
    """Lap time the way it should be heard: 126.7 -> '2 oh 6 point 7',
    47.6 -> '47 point 6'. SAPI reads the text '2:06.7' as 'two colon
    six point seven' (Albert's report, Nov 1) -- so for speech the
    colon goes away and a leading zero in the seconds is spelled 'oh'.
    Displayed times keep fmt_lap; only spoken strings use this."""
    if seconds is None or seconds <= 0:
        return "no time"
    total_tenths = int(round(seconds * 10))
    m, rem = divmod(total_tenths, 600)
    s, tenth = divmod(rem, 10)
    if m:
        sec_txt = f"oh {s}" if s < 10 else f"{s}"
        return f"{m} {sec_txt} point {tenth}"
    return f"{s} point {tenth}"


def tenths_phrase(loss):
    t = int(round(loss * 10))
    if t <= 0:
        return "nothing"
    if t == 1:
        return "a tenth"
    return f"{t} tenths"


class LapBins:
    """Per-bin accumulation for one lap: speed avg, brake max, gas avg,
    plus heading (circular mean) -- v2.0 records heading per bin on the
    reference lap so wide corners can be split at direction changes."""

    def __init__(self):
        self.spd_sum = [0.0] * NBINS
        self.spd_cnt = [0] * NBINS
        self.brk_max = [0.0] * NBINS
        self.gas_sum = [0.0] * NBINS
        self.hdg_sin = [0.0] * NBINS
        self.hdg_cos = [0.0] * NBINS
        self.hdg_cnt = [0] * NBINS
        self.str_sum = [0.0] * NBINS
        self.str_cnt = [0] * NBINS

    def add(self, pos, speed_kmh, brake, gas, heading=None, steer=None):
        b = int(pos * NBINS) % NBINS
        self.spd_sum[b] += speed_kmh
        self.spd_cnt[b] += 1
        if brake > self.brk_max[b]:
            self.brk_max[b] = brake
        self.gas_sum[b] += gas
        if heading is not None:
            self.hdg_sin[b] += math.sin(heading)
            self.hdg_cos[b] += math.cos(heading)
            self.hdg_cnt[b] += 1
        if steer is not None:
            self.str_sum[b] += steer
            self.str_cnt[b] += 1

    def coverage(self):
        filled = sum(1 for c in self.spd_cnt if c > 0)
        return filled / NBINS

    def arrays(self):
        spd = [0.0] * NBINS
        gas = [0.0] * NBINS
        for i in range(NBINS):
            if self.spd_cnt[i]:
                spd[i] = self.spd_sum[i] / self.spd_cnt[i]
                gas[i] = self.gas_sum[i] / self.spd_cnt[i]
        # fill any gaps from neighbours so traces are continuous
        last = next((v for v in spd if v > 0), 0.0)
        for i in range(NBINS):
            if spd[i] == 0.0:
                spd[i] = last
            else:
                last = spd[i]
        hdg = [None] * NBINS
        for i in range(NBINS):
            if self.hdg_cnt[i]:
                hdg[i] = math.atan2(self.hdg_sin[i], self.hdg_cos[i])
        # steering: per-bin mean, gaps filled from the last value; the
        # whole array stays None when no steering was ever sampled
        # (simulate mode), which disables the steering line cues.
        steer = None
        if any(self.str_cnt):
            steer = [0.0] * NBINS
            last = 0.0
            for i in range(NBINS):
                if self.str_cnt[i]:
                    last = self.str_sum[i] / self.str_cnt[i]
                steer[i] = last
        return spd, list(self.brk_max), gas, hdg, steer


def zone_time(spd, z0, z1, bin_m):
    total = 0.0
    for i in range(z0, z1 + 1):
        v = max(spd[i] / 3.6, 3.0)
        total += bin_m / v
    return total


def _steer_stats(lap_str, ref_str, z0, z1):
    """Steering busy-ness in a span: direction reversals of the
    steering trace and total steering travel, lap vs reference.
    Scale-free on purpose -- AC's steerAngle units vary by car, so
    only counts and ratios are compared, never absolute angles."""
    if not lap_str or not ref_str:
        return None, None

    def measure(tr):
        seg = smooth(tr[z0:z1 + 1], 5)
        if len(seg) < 8:
            return 0, 0.0
        peak = max(abs(v) for v in seg) or 1e-9
        rev, work, prev = 0, 0.0, 0
        for a, b in zip(seg, seg[1:]):
            d = b - a
            work += abs(d)
            s = 1 if d > 0 else (-1 if d < 0 else 0)
            if s and abs(b) > 0.25 * peak:
                if prev and s != prev:
                    rev += 1
                prev = s
        return rev, work

    lr, lw = measure(lap_str)
    rr, rw = measure(ref_str)
    return lr - rr, (lw / rw if rw > 1e-9 else None)


def _span_stats(lap_spd, lap_brk, lap_gas, lap_str, ref, z0, z1,
                apex_bin, ref_brake_bin, ref_throttle_bin, ref_apex_v,
                bin_m, line=True):
    """Time loss and its causes over one span (a corner zone, or one
    phase of a phase-split corner). With line=True it also reads the
    LINE symptoms Albert approved on Nov 1: where the slowest point
    landed vs the reference apex (early/late apex), a lift after
    committing to the throttle, and steering corrections. AC gives
    the coach no car position and no track edges, so these are
    symptoms measured against his own best lap -- never geometry
    claims about distance to an apex."""
    t_lap = zone_time(lap_spd, z0, z1, bin_m)
    t_ref = zone_time(ref["spd"], z0, z1, bin_m)
    win = max(4, int(25 / bin_m))
    lap_apex_v = min(lap_spd[max(0, apex_bin - win):apex_bin + win + 1])

    def brake_point(brk):
        for j in range(z0, apex_bin + 1):
            if brk[j] > 0.08:
                return j
        return None

    lb = brake_point(lap_brk)
    brake_delta_m = None
    if lb is not None and ref_brake_bin is not None:
        brake_delta_m = (lb - ref_brake_bin) * bin_m  # negative = braked earlier

    def throttle_point(gas):
        run = 0
        for j in range(apex_bin, z1 + 1):
            if gas[j] >= 0.95:
                run += 1
                if run >= 3:
                    return j - 2
            else:
                run = 0
        return None

    lt = throttle_point(lap_gas)
    throttle_delta_m = None
    if lt is not None and ref_throttle_bin is not None:
        throttle_delta_m = (lt - ref_throttle_bin) * bin_m  # positive = later to power
    coast_s = 0.0
    coast_entry_s = 0.0
    coast_exit_s = 0.0
    for i in range(z0, z1 + 1):
        if lap_brk[i] < 0.05 and lap_gas[i] < 0.05 and lap_spd[i] > 40:
            c = bin_m / max(lap_spd[i] / 3.6, 3.0)
            coast_s += c
            # v2.14.0: WHERE the coast sits is the actual diagnosis --
            # rolling to the apex (braking finished early / not
            # trailed) and waiting after the apex (late back to
            # power) are different faults with different fixes, and
            # one shared 'less coasting' line was hiding both.
            if i <= apex_bin:
                coast_entry_s += c
            else:
                coast_exit_s += c
    # Speed carried INTO the zone vs the reference: a deficit here
    # was lost at the previous corner's exit, not in this zone.
    _ew = slice(z0, min(z0 + 3, z1 + 1))
    entry_delta_kmh = (sum(lap_spd[_ew]) / len(lap_spd[_ew])
                       - sum(ref["spd"][_ew]) / len(ref["spd"][_ew]))

    def _brake_shape(brk, spd):
        """(peak pedal, release duration s) for one trace's braking
        run into the apex (v2.8.0): how hard the brake was used, and
        how long the release took from half-peak to fully off.
        Release shape is half of braking technique and the point-only
        analysis never saw it."""
        seg = brk[z0:apex_bin + 1]
        if not seg:
            return None, None
        peak = max(seg)
        if peak < 0.2:
            return peak, None
        rel_start = None
        for j in range(apex_bin, z0 - 1, -1):
            if brk[j] >= 0.5 * peak:
                rel_start = j
                break
        if rel_start is None:
            return peak, None
        rel_s = 0.0
        for j in range(rel_start, z1):
            if brk[j] < 0.05:
                break
            rel_s += bin_m / max(spd[j] / 3.6, 3.0)
        return peak, rel_s

    peak_lap, rel_lap = _brake_shape(lap_brk, lap_spd)
    _ref_brk = ref["brk"] if ref.get("brk") else lap_brk
    peak_ref, rel_ref = _brake_shape(_ref_brk, ref["spd"])
    out = {"zone_time": t_lap, "ref_zone_time": t_ref,
           "span_m": round((z1 - z0) * bin_m, 1),
           "ref_apex_kmh": round(ref_apex_v, 1),
           "loss": t_lap - t_ref,
           "brake_delta_m": brake_delta_m,
           "peak_brake": (round(peak_lap, 2) if peak_lap is not None else None),
           "peak_brake_ref": (round(peak_ref, 2) if peak_ref is not None else None),
           "release_s": (round(rel_lap, 2) if rel_lap is not None else None),
           "release_ref_s": (round(rel_ref, 2) if rel_ref is not None else None),
           "apex_delta_kmh": lap_apex_v - ref_apex_v,
           "throttle_delta_m": throttle_delta_m,
           "coast_s": coast_s,
           "coast_entry_s": round(coast_entry_s, 2),
           "coast_exit_s": round(coast_exit_s, 2),
           "entry_delta_kmh": round(entry_delta_kmh, 1),
           "apex_shift_m": None, "mid_lift": False,
           "steer_rev_delta": None, "steer_work_ratio": None}
    if line:
        win2 = max(6, int(90 / bin_m))
        a0, a1 = max(z0, apex_bin - win2), min(z1, apex_bin + win2)
        if a1 > a0 and ref_apex_v < 200:
            lap_apex_bin = min(range(a0, a1 + 1), key=lambda j: lap_spd[j])
            shift_m = (lap_apex_bin - apex_bin) * bin_m
            # v2.0.5: a minimum sitting exactly ON the window edge
            # with a big claimed shift is the speed trough running
            # out of the window, not a measured apex (a split zone
            # read -87 m every lap this way). Drop those; interior
            # measurements stand.
            if lap_apex_bin not in (a0, a1) or abs(shift_m) < 60:
                out["apex_shift_m"] = round(shift_m, 1)
        if lt is not None:
            cut = z1 - max(4, int(50 / bin_m))
            for j in range(lt, max(lt, cut)):
                if lap_gas[j] < 0.4 and lap_spd[j] > ref_apex_v + 15:
                    out["mid_lift"] = True
                    break
        rd, wr = _steer_stats(lap_str, ref.get("str"), z0, z1)
        out["steer_rev_delta"] = rd
        out["steer_work_ratio"] = (round(wr, 2) if wr is not None else None)
    return out


def analyze_lap(lap_spd, lap_brk, lap_gas, ref, corners, bin_m,
                off_events=None, off_min_s=0.35, off_2_min_s=0.9,
                lap_str=None, slip_events=None):
    """Compare one lap against the reference, corner by corner.

    off_events: excursions where AC reported 2+ tyres off the track,
    each {"bins": set, "dur_s": float, "max_tyres": int}. A corner
    whose zone (plus ~30 m of entry, where the veer starts) overlaps
    an excursion lasting at least off_min_s is flagged 'off': its
    numbers are recovery driving, not technique, and the coach
    excludes it from cues, rankings and averages instead of lecturing
    about the coasting that was really a save (Albert, Nov 1). The
    duration floor (v1.6.2) keeps a brief graze across a curb from
    excusing a corner, and each result records the excursion length
    so a flagged corner can be audited later."""
    results = []
    for c in corners:
        z0, z1 = c["zone_start"], c["zone_end"]
        st = _span_stats(lap_spd, lap_brk, lap_gas, lap_str, ref, z0, z1,
                         c["apex_bin"],
                         c["brake_bin"] if c["has_brake_zone"] else None,
                         c["throttle_bin"], c["apex_speed_kmh"], bin_m)
        off = False
        off_dur = 0.0
        if off_events:
            margin = max(4, int(30 / bin_m))
            lo = c["zone_start"] - margin
            for ev in off_events:
                # A 2-tyre excursion has to last longer to count as a
                # save -- two wheels over the exit paint is often just
                # a wide line (Albert: "too sensitive", Nov 1). Three
                # or more tyres out is unambiguous at the short floor.
                floor = off_min_s if ev.get("max_tyres", 2) >= 3 else off_2_min_s
                if (ev.get("dur_s", 0.0) >= floor
                        and any(lo <= b <= c["zone_end"] for b in ev["bins"])):
                    off = True
                    off_dur = max(off_dur, ev["dur_s"])
        res = {"num": c["num"], "name": c["name"]}
        res.update(st)
        res["off"] = off
        res["off_dur_s"] = round(off_dur, 2)
        # v2.7.0: lockup / wheelspin seconds inside this zone, from
        # the physics-page events feed() recorded (same overlap rule
        # as offs, entry margin included).
        lockup_s = spin_s = 0.0
        if slip_events:
            smargin = max(4, int(30 / bin_m))
            slo = c["zone_start"] - smargin
            for ev in slip_events:
                if any(slo <= b <= c["zone_end"] for b in ev["bins"]):
                    if ev["kind"] == "lockup":
                        lockup_s += ev["dur_s"]
                    else:
                        spin_s += ev["dur_s"]
        res["lockup_s"] = round(lockup_s, 2)
        res["spin_s"] = round(spin_s, 2)
        if c.get("phases"):
            phs = []
            for ph in c["phases"]:
                pz0, pz1 = ph["z0"], ph["z1"]
                if pz1 - pz0 < 3:
                    continue
                papex = min(range(pz0, pz1 + 1), key=lambda j: ref["spd"][j])
                pst = _span_stats(
                    lap_spd, lap_brk, lap_gas, lap_str, ref, pz0, pz1, papex,
                    c["brake_bin"] if (c["has_brake_zone"]
                                       and pz0 <= c["brake_bin"] <= pz1) else None,
                    c["throttle_bin"] if pz0 <= c["throttle_bin"] <= pz1 else None,
                    ref["spd"][papex], bin_m, line=False)
                pst["name"] = ph["name"]
                phs.append(pst)
            if phs:
                res["phases"] = phs
        results.append(res)
    # v2.14.0: each corner learns its predecessor's name (the lap is
    # a loop, so corner 1's predecessor is the last corner) -- an
    # entry-speed deficit is the previous corner's exit talking.
    if len(results) > 1:
        for i, r in enumerate(results):
            r["prev_name"] = results[i - 1]["name"]
    return results


def _symptom_costs(s):
    """Estimated seconds behind each measurable symptom of one span,
    from the span's own measurements (v2.6.0 -- a review pointed out
    the cue picker used to take the FIRST applicable symptom in a
    fixed order, so 'brake later' won whenever the driver braked 8 m
    early, even when the time actually went elsewhere). Braking or
    throttle distances convert at the span's reference average speed
    (half weight -- the distance is partly re-covered at speed); an
    apex-speed deficit scales with the span's reference time; coast
    seconds are already time, at half weight (rolling still covers
    ground). Line symptoms (apex shift, lift, hands) have no distance
    or speed to convert, so they carry a nominal 0.15 s -- enough to
    beat trivial pedal deltas, not real ones. The numbers only rank
    symptoms WITHIN one corner; they are never spoken."""
    span_t = s.get("ref_zone_time") or s.get("zone_time") or 0.0
    span_m = s.get("span_m") or 0.0
    v_avg = span_m / span_t if (span_t > 0 and span_m > 0) else 25.0
    v_avg = min(max(v_avg, 8.0), 70.0)
    apex_ref = max(s.get("ref_apex_kmh") or 60.0, 20.0)
    costs = {}
    bd = s.get("brake_delta_m")
    if bd is not None:
        costs["brake"] = abs(bd) / v_avg * 0.5
    if s.get("coast_s"):
        costs["coast"] = s["coast_s"] * 0.5
    if s.get("apex_delta_kmh"):
        costs["speed"] = (abs(s["apex_delta_kmh"]) / apex_ref
                          * max(span_t, 0.0) * 0.5)
    td = s.get("throttle_delta_m")
    if td is not None:
        costs["power"] = abs(td) / v_avg * 0.5
    ed = s.get("entry_delta_kmh")
    if ed is not None and ed < 0:
        costs["entry"] = (abs(ed) / max(v_avg * 3.6, 20.0)
                          * max(span_t, 0.0) * 0.5)
    for kind in ("apex_early", "apex_late", "lift", "hands"):
        costs[kind] = 0.15
    return costs


def _cue_candidates(s):
    """Every applicable fix for one span's stats, as (kind, text,
    est_cost_s) triples in the coach's standing order. _pick_candidate
    re-ranks by measured cost (v2.6.0); the standing order survives as
    the tiebreak and as the fallback when a stat lacks the fields the
    costing needs."""
    name = s["name"]
    costs = _symptom_costs(s)
    out = []
    if s.get("lockup_s", 0) >= 0.25:
        out.append(("lockup", f"{name}: you're locking the fronts there \u2014 come off the first brake hit softer", 0.4 + s["lockup_s"]))
    if s.get("spin_s", 0) >= 0.25:
        out.append(("spin", f"{name}: wheelspin there \u2014 feed the power in, don't grab it", 0.4 + s["spin_s"]))
    if s.get("brake_delta_m") is not None and s["brake_delta_m"] <= -8:
        out.append(("brake", f"{name}: brake later", costs.get("brake", 0.1)))
    # v2.14.0: coast is a location, not a diagnosis. The merged and
    # split candidates below carry the coast's full cost (plus their
    # partner symptom's), so when the data supports a specific story
    # it outranks the generic line, which survives as the fallback
    # for a coast spread evenly across the zone. Kinds reuse
    # brake/coast/power so guide-tip tagging and priority biases
    # keep working.
    _bd = s.get("brake_delta_m")
    _ce = s.get("coast_entry_s") or 0.0
    _cx = s.get("coast_exit_s") or 0.0
    _td = s.get("throttle_delta_m")
    _entry_dom = _ce >= 0.5 and _ce >= 0.6 * max(s["coast_s"], 0.01)
    if _entry_dom and _bd is not None and _bd <= -8:
        out.append(("brake", f"{name}: you're done braking early and rolling to the apex \u2014 brake later, carry it in",
                    costs.get("coast", 0.1) + costs.get("brake", 0.1)))
    elif _entry_dom:
        out.append(("coast", f"{name}: you're rolling to the apex off both pedals \u2014 trail the brake deeper",
                    costs.get("coast", 0.1)))
    if _td is not None and _td >= 10 and _cx >= 0.4:
        out.append(("power", f"{name}: you're waiting to get back to power \u2014 pick it up at the apex",
                    costs.get("coast", 0.1) + costs.get("power", 0.1)))
    if s["coast_s"] >= 0.5:
        out.append(("coast", f"{name}: less coasting, commit to a pedal", costs.get("coast", 0.1)))
    _ed = s.get("entry_delta_kmh")
    if _ed is not None and _ed <= -6:
        _prev = s.get("prev_name")
        _where = (f"that's {_prev}'s exit, not this corner" if _prev
                  else "that was lost before this corner")
        out.append(("entry", f"{name}: you're arriving slow \u2014 {_where}",
                    costs.get("entry", 0.1)))
    if s.get("apex_shift_m") is not None and abs(s["apex_shift_m"]) >= 25:
        if s["apex_shift_m"] > 0:
            out.append(("apex_late", f"{name}: apex is late there \u2014 turn in earlier", costs.get("apex_late", 0.15)))
        else:
            out.append(("apex_early", f"{name}: early apex \u2014 delay the turn-in, don't pinch the exit", costs.get("apex_early", 0.15)))
    if s.get("mid_lift"):
        out.append(("lift", f"{name}: you're lifting mid-corner \u2014 commit, or carry less in", costs.get("lift", 0.15)))
    if s["apex_delta_kmh"] <= -5:
        out.append(("speed", f"{name}: carry more speed in", costs.get("speed", 0.1)))
    if s.get("throttle_delta_m") is not None and s["throttle_delta_m"] >= 10:
        out.append(("power", f"{name}: get to power earlier", costs.get("power", 0.1)))
    if ((s.get("steer_rev_delta") or 0) >= 2
            and (s.get("steer_work_ratio") or 1) >= 1.3):
        out.append(("hands", f"{name}: busy hands there \u2014 one clean steering input", costs.get("hands", 0.15)))
    if s.get("brake_delta_m") is not None and s["brake_delta_m"] >= 10:
        out.append(("brake", f"{name}: you're braking deep, watch the entry", 0.10))
    pb, pbr = s.get("peak_brake"), s.get("peak_brake_ref")
    if (pb is not None and pbr is not None and pbr - pb >= 0.25
            and s.get("brake_delta_m") is not None
            and s["apex_delta_kmh"] <= 0):
        out.append(("brake_soft", f"{name}: you're not using all the brake there", 0.30))
    rl, rr = s.get("release_s"), s.get("release_ref_s")
    if (rl is not None and rr and rl < 0.55 * rr
            and s["apex_delta_kmh"] <= -3):
        out.append(("release", f"{name}: let the brake off more smoothly there", 0.25))
    return out


def _pick_candidate(s, priority=None):
    """The winning (kind, text) fix for one span's stats, or None.
    v2.3: a track guide can declare what matters at a corner --
    priority "exit" elevates power/speed fixes, "entry" elevates
    braking/coast fixes; anything else keeps the standing order."""
    cands = _cue_candidates(s)
    if not cands:
        return None
    # v2.6.0: the biggest measured cost wins. A track guide's
    # per-corner priority (v2.3) no longer picks a class outright --
    # it biases the costs of its favoured symptoms by 1.5x, so a much
    # bigger measured problem elsewhere still takes the cue.
    if priority == "exit":
        cands = [(k, t, c * 1.5 if k in ("power", "speed", "spin") else c)
                 for k, t, c in cands]
    elif priority == "entry":
        cands = [(k, t, c * 1.5 if k in ("brake", "coast", "lockup",
                                          "brake_soft", "release") else c)
                 for k, t, c in cands]
    cands.sort(key=lambda x: -x[2])  # stable: standing order breaks ties
    kind, txt, _cost = cands[0]
    return kind, txt


def _cue_text(name, s, priority=None):
    s = dict(s)
    s["name"] = name
    pick = _pick_candidate(s, priority=priority)
    return pick[1] if pick else None


def corner_cue(res, brake_only=False, priority=None, guide=None, symptom_tips=True):
    """Pick the single most useful spoken fix for a corner from its
    data. v2.0: when the corner has phases (Riverside, Phil Hill,
    Sweeper) and one phase holds most of the loss, the cue names the
    phase -- an exit problem shouldn't be delivered as an entry tip.
    Line symptoms (apex shift, mid-corner lift, steering corrections)
    compete in the same chain, per Albert's call on Nov 1.
    v2.1.0: brake_only is Simulator Controller's brake-coaching
    mode, distilled -- when the session's focus is braking, only
    braking fixes are spoken."""
    if res is None or res.get("off"):
        return None
    if brake_only:
        if res.get("lockup_s", 0) >= 0.25:
            return (f"{res['name']}: you're locking the fronts there "
                    f"\u2014 come off the first brake hit softer")
        bd = res.get("brake_delta_m")
        if bd is not None and bd <= -8:
            return f"{res['name']}: brake later"
        if bd is not None and bd >= 10:
            return f"{res['name']}: you're braking deep, watch the entry"
        return None
    phs = [p for p in res.get("phases", []) if p["loss"] >= 0.15]
    if phs and res["loss"] > 0:
        worst = max(phs, key=lambda p: p["loss"])
        if worst["loss"] >= 0.6 * res["loss"]:
            txt = _cue_text(worst["name"], worst, priority=priority)
            if txt:
                return txt
    s = dict(res)
    s["name"] = res["name"]
    pick = _pick_candidate(s, priority=priority)
    if pick is None:
        return None
    kind, txt = pick
    # v2.4 symptom-triggered guide tips: when the winning fix's
    # symptom has a tagged guide tip at this corner, the human
    # coach's wording replaces the generic fix. Same cue slot, same
    # budgets -- only the words change.
    if symptom_tips and guide is not None and res.get("num") is not None:
        gtip = guide_tip_for(guide, res["num"], kind)
        if gtip:
            return f"{res['name']}: {gtip}"
    return txt



def _atomic_write_json(path, obj):
    """Write JSON so a crash or a console closed mid-write cannot
    truncate the only copy (v2.5.3 review fix): write a temp file in
    the same folder, fsync, keep the previous file as <name>.bak,
    then os.replace over the original -- atomic on the same volume."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
        f.flush()
        os.fsync(f.fileno())
    if os.path.exists(path):
        try:
            import shutil
            shutil.copy2(path, path + ".bak")
        except Exception:
            pass
    os.replace(tmp, path)


class TrackProfile:
    def __init__(self, key):
        self.key = key
        self.path = os.path.join(TRACKS_DIR, key + ".json")
        self.track_length_m = None
        self.best_lap_s = None
        self.ref = None       # {"spd": [...], "brk": [...], "gas": [...]}
        self.corners = []
        self.outline = None   # [[x, y], ...] driven line for the report map
        self.guide = None     # v2.3: track guide dict for this track (see _load_guide)
        self.ref_dirty = False  # v2.6.0: reference lap contained an off
        self._from_disk = False

    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                self.track_length_m = d["track_length_m"]
                self.best_lap_s = d["best_lap_s"]
                self.ref = {"spd": d["ref_spd"], "brk": d["ref_brk"], "gas": d["ref_gas"],
                            "hdg": d.get("ref_hdg"), "str": d.get("ref_str")}
                self.corners = d["corners"]
                self.outline = d.get("path")
                self.ref_dirty = bool(d.get("ref_dirty", False))
                self._from_disk = True
                return True
            except Exception as e:
                print(f"[profile] could not load {self.path} ({e}); "
                      f"trying the backup")
                bak = self.path + ".bak"
                if os.path.exists(bak):
                    try:
                        with open(bak, "r", encoding="utf-8") as f:
                            d = json.load(f)
                        self.track_length_m = d["track_length_m"]
                        self.best_lap_s = d["best_lap_s"]
                        self.ref = {"spd": d["ref_spd"], "brk": d["ref_brk"], "gas": d["ref_gas"],
                                    "hdg": d.get("ref_hdg"), "str": d.get("ref_str")}
                        self.corners = d["corners"]
                        self.outline = d.get("path")
                        self.ref_dirty = bool(d.get("ref_dirty", False))
                        self._from_disk = True
                        print(f"[profile] recovered {self.key} from the backup file")
                        return True
                    except Exception as e2:
                        print(f"[profile] backup unusable too ({e2}); starting fresh")
        return False

    def track_map_path(self):
        """Per-TRACK corner map shared across cars: tracks/<track>__map.json."""
        track = self.key.split("__", 1)[0]
        return os.path.join(TRACKS_DIR, track + "__map.json")

    def history_path(self):
        """Per-COMBO cross-session history (v2.1.0): sessions, laps
        and clean-corner loss averages, so the coach's memory of a
        car/track combo survives the session -- Simulator
        Controller's telemetry database, miniaturised to one file."""
        return os.path.join(TRACKS_DIR, self.key + "__history.json")

    def save(self):
        os.makedirs(TRACKS_DIR, exist_ok=True)
        d = {
            "track_length_m": self.track_length_m,
            "best_lap_s": self.best_lap_s,
            "ref_spd": self.ref["spd"], "ref_brk": self.ref["brk"], "ref_gas": self.ref["gas"],
            "ref_hdg": self.ref.get("hdg"), "ref_str": self.ref.get("str"),
            "corners": self.corners,
            "path": self.outline,
            "ref_dirty": self.ref_dirty,
            "schema": 1,
        }
        _atomic_write_json(self.path, d)
        # Share this corner map at track level so a DIFFERENT car at
        # the same track can borrow the corner positions (and names)
        # before its own first mapping lap (Albert's request, Nov 1).
        # The reference traces stay per-car -- only geometry is shared.
        # A named map is never overwritten by an unnamed one, so the
        # verified Buttonwillow names can't be lost to a re-detection.
        if self.corners:
            try:
                mp = self.track_map_path()
                existing = None
                if os.path.exists(mp):
                    with open(mp, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                named = any(not c["name"].startswith("Turn") for c in self.corners)
                old_named = bool(existing) and any(
                    not c["name"].startswith("Turn")
                    for c in existing.get("corners", []))
                if (existing is None or existing.get("source") == self.key
                        or (named and not old_named)):
                    _atomic_write_json(mp, {"corners": self.corners,
                                            "path": self.outline,
                                            "track_length_m": self.track_length_m,
                                            "source": self.key})
            except Exception:
                pass

    @property
    def bin_m(self):
        return (self.track_length_m or 4780.0) / NBINS


def _load_guide(track):
    """Track guide (v2.3): human-coach knowledge for one track,
    distilled offline into <track>__guide.json -- looked for next to
    the profiles first, then in the app folder's guides/. Returns
    the parsed dict or None. Coaching is unchanged without one."""
    for base in [TRACKS_DIR] + _legacy_data_dirs() + [os.path.join(APP_DIR, "guides")]:
        path = os.path.join(base, track + "__guide.json")
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    g = json.load(f)
                if isinstance(g, dict) and isinstance(g.get("zones"), dict):
                    return g
        except Exception:
            pass
    return None


def guide_zone(guide, num):
    if not guide:
        return None
    return (guide.get("zones") or {}).get(str(num))


def guide_priority(guide, num):
    z = guide_zone(guide, num)
    return (z or {}).get("priority") if z else None


def tip_text(tip):
    """A guide tip is a plain string, or (v2.4) an object with
    "text" and an optional symptom "trigger"."""
    return tip if isinstance(tip, str) else (tip.get("text") or "")


def tip_trigger(tip):
    return None if isinstance(tip, str) else tip.get("trigger")


def guide_tip_for(guide, num, kind):
    """The guide tip tagged for symptom `kind` at zone `num`."""
    z = guide_zone(guide, num)
    if not z:
        return None
    for tip in z.get("tips") or []:
        if tip_trigger(tip) == kind:
            return tip_text(tip)
    return None


class Coach:
    """The coaching brain. Feed it samples; it speaks through the Speaker."""

    def __init__(self, cfg, speaker, log_fn):
        self.cfg = cfg
        self.speaker = speaker
        self.log = log_fn
        self.profile = None
        self.map_borrowed_from = None
        self._outline_borrowed = False
        self.reset_lap()
        self.prev_pos = None
        self.prev_dist = None
        self.lap_start_t = None
        self.lap_start_dist = None
        self.pending_finishes = []   # finished laps awaiting their official time
        self.official_times = []     # settled official times, in lap order
        self._official_watch = None  # official time currently settling
        self._seen_completed = None  # last completedLaps value observed
        self._ict_peak_ms = 0        # peak iCurrentTime this lap (diagnostic)
        self.corner_history = {}   # num -> list of analyze dicts (recent laps)
        self._cue_memory = {}      # v2.8.0: num -> last approach cue {text, lap, loss}
        self.history = None        # v2.1.0: cross-session per-combo history
        self._live_losses = {}     # v2.1.0: this lap's live per-zone results (section feedback)
        self.lap_records = []      # per-lap summaries for the debrief
        self._last_lap_spd = None  # v2.7.0: last counted lap's trace (report)
        self._session_best = None  # v2.11.0: {time, spd, brk} of the session's best lap
        self._optimal = None       # v2.11.0: stitched best-zone lap {spd, brk, zone_times}
        self._optimal_ref_time = None
        self.approach_fired = set()
        self.exit_fired = set()
        self.approach_cues_this_lap = 0
        self._tone_fired = set()
        self._tone_pending = None      # v2.13.0: brake tone held for a busy channel
        self._sass_rot = {}            # v2.14.0: sass pool rotation, per site
        self._last_summary_leak = None  # v2.14.0: last summary's biggest leak (num)
        self._exit_line_count = {}     # v2.14.0: exit lines spoken per corner, session
        self._exit_spoken = 0
        self.last_spoke_t = -1e9
        self.lap_number = 0
        self._prev_bin = None
        self.map_lap_s = None   # lap time the current corner map was built from
        self._loaded_with_ref = False  # profile already had a reference at session start
        self._any_analyzed = False     # any flying lap analysed yet this session
        self._excuse_streak = {}       # corner num -> consecutive excused (off) laps
        self._briefed = False          # out-lap briefing spoken this session
        self._brief_plan = None        # pending briefing segments [(offset_s, text)]
        self._brief_t0 = None          # when the briefing started
        self._dist0 = None             # first distanceTraveled seen this session
        self._stopped_since = None     # when the car last came to a stop
        self._pitted = False           # long stop seen during the current lap
        self._teleported = False       # position reset seen during the current lap
        self._lap_muted = False        # current lap follows a stop/teleport: no live cues
        # v2.13.0 distance position fallback. Some mod tracks ship no
        # working AI spline, so AC reports normalizedCarPosition as a
        # constant (Rainbow Road: exactly 0.0 for a whole session) and
        # position-based coaching starves. The fallback derives
        # position from the game's lap timer plus the odometer --
        # see _position_route.
        self._pos_src = "unknown"      # unknown -> "spline" | "distance"
        self._pos_lo = None            # raw spline range seen pre-verdict
        self._pos_hi = None
        self._pos_travel = 0.0         # moving distance pre-verdict (m)
        self._pos_last_dist = None
        self._fb_buf = None            # buffered feed frames while learning
        self._fb_base_dist = None      # odometer at current lap start
        self._fb_base_t = None
        self._fb_prev_ict = None       # last lap-timer value (reset detect)
        self._fb_cands = []            # plausible candidate lap distances
        self._fb_len = None            # learned lap length (m)
        self._fb_lap_len = None        # length snapshot for current lap
        self._fb_skip_officials = 0    # settled officials for dropped laps

    # -- session wiring ----------------------------------------------------
    # Names for the Buttonwillow seven-zone map, as corrected by
    # Albert himself on Nov 1 (he drove it; two assistant attempts
    # from GPS alignment were wrong because the Dragy GPS outline
    # renders mirror-flipped). His identifications, verbatim sense:
    # 2 is the Off Ramp, 3 is Cotton Corners, 4 is going into
    # Riverside, 5 is after Phil Hill, 6 is after the Sweeper going
    # into the Esses, 7 is the main straight with Sunset the turn
    # before it, and Sunrise/Sunset sit opposite to the v1.5.1 guess.
    # Applied only when detection finds exactly this layout; any
    # other corner count keeps Turn N.
    BUTTONWILLOW_7 = {1: "Sunrise", 2: "Off Ramp", 3: "Cotton Corners",
                      4: "Riverside Entry", 5: "Phil Hill Exit",
                      6: "Esses Entry", 7: "Sunset"}

    # Canonical Buttonwillow layout, from Albert's own stored map
    # (apex fractions of the seven places he named). v2.0's fine
    # maps name a detected corner by which canonical span its apex
    # falls in, so a 9- or 12-zone map still gets his names.
    BW_CANON_APEX = [0.061, 0.139, 0.251, 0.423, 0.627, 0.757, 0.958]
    BW_CANON_NAMES = ["Sunrise", "Off Ramp", "Cotton Corners", "Riverside",
                      "Phil Hill", None, "Sunset"]  # span 6: Sweeper/Esses
    # Long corners Albert wants coached in phases (Nov 1: "entry to
    # Phil Hill, top of the hill is Phil Hill, exit to Phil Hill";
    # same split for Riverside and the Sweeper). The zone keeps the
    # base name; phases get their own loss accounting and cues.
    PHASE_SPLIT = {"Riverside": ("entry", "exit"),
                   "Phil Hill": ("entry", "top", "exit"),
                   "Sweeper": ("entry", "exit")}

    def _bw_span(self, frac):
        apex = self.BW_CANON_APEX
        starts = [(apex[-1] + apex[0] + 1) / 2 - 1]  # Sunset->Sunrise wrap
        starts += [(a + b) / 2 for a, b in zip(apex, apex[1:])]
        if frac < starts[0]:
            return 6
        span = 0
        for i, s in enumerate(starts):
            if frac >= s:
                span = i
        return span

    def _attach_phases(self, c, bin_m):
        kinds = self.PHASE_SPLIT.get(c["name"])
        if not kinds:
            c.pop("phases", None)
            return
        base, apex = c["name"], c["apex_bin"]
        if kinds == ("entry", "exit"):
            c["phases"] = [
                {"name": f"{base} Entry", "z0": c["zone_start"], "z1": apex},
                {"name": f"{base} Exit", "z0": apex, "z1": c["zone_end"]}]
        else:  # entry / top / exit -- the top of the hill IS Phil Hill
            w = max(4, int(45 / bin_m))
            lo, hi = max(c["zone_start"], apex - w), min(c["zone_end"], apex + w)
            c["phases"] = [
                {"name": f"{base} Entry", "z0": c["zone_start"], "z1": lo},
                {"name": base, "z0": lo, "z1": hi},
                {"name": f"{base} Exit", "z0": hi, "z1": c["zone_end"]}]

    def _apply_corner_names(self, corners):
        p = self.profile
        if not (p and "buttonwillow" in p.key):
            return
        if len(corners) == 7:
            for c in corners:
                if c["num"] in self.BUTTONWILLOW_7:
                    c["name"] = self.BUTTONWILLOW_7[c["num"]]
            return
        groups = {}
        for c in corners:
            groups.setdefault(self._bw_span(c["apex_bin"] / NBINS), []).append(c)
        for span, group in groups.items():
            group.sort(key=lambda c: c["apex_bin"])
            base = self.BW_CANON_NAMES[span]
            if span == 5:
                if len(group) == 1:
                    names = ["Esses Entry"]
                elif len(group) == 2:
                    names = ["Sweeper", "Esses"]
                else:
                    # Albert's fine map (Nov 1): the Sweeper resolves
                    # into its entrance and its heart as separate
                    # dips, and the Esses only start after them.
                    names = (["Sweeper Entry", "Sweeper", "Esses"]
                             + [f"Esses {k}" for k in range(2, len(group) - 1)])
            elif base == "Cotton Corners" and len(group) > 1:
                names = [f"Cotton Corners {k}" for k in range(1, len(group) + 1)]
            elif len(group) == 1:
                names = [base]
            elif len(group) == 2:
                names = [f"{base} Entry", base]
            else:
                names = ([f"{base} Entry"]
                         + [f"{base} {k}" for k in range(2, len(group))]
                         + [base])
            for c, nm in zip(group, names):
                c["name"] = nm
        has_sw_entry = any(c["name"] == "Sweeper Entry" for c in corners)
        for c in corners:
            if c["name"] == "Sweeper" and has_sw_entry:
                continue  # the entrance is its own zone here; phase
                # names would collide with it
            self._attach_phases(c, p.bin_m)

    def _apply_user_names(self, corners):
        """Names from <track>__names.json next to the profiles --
        the no-new-app naming path (Albert's question, Nov 1). The
        file maps zone number -> name and wins over the built-in
        tables; when none exists, the app writes a starter file
        holding the current names so it can simply be edited in
        Notepad and the coach restarted. Applied on load and after
        every (re)detection, so names survive remaps (matched by
        zone number)."""
        p = self.profile
        if not p or not corners:
            return
        track = p.key.split("__", 1)[0]
        path = _find_data_file(track + "__names.json") or os.path.join(TRACKS_DIR, track + "__names.json")
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    mapping = json.load(f)
                if len(mapping) != len(corners):
                    # v2.6.0: names are keyed by zone number, so a
                    # file written for a different corner count would
                    # silently name the WRONG corners. Generic names
                    # are the safe fallback.
                    print(f"[coach] names file {os.path.basename(path)} "
                          f"covers {len(mapping)} zones but this map "
                          f"has {len(corners)} -- ignoring it until "
                          f"the counts match")
                    return
                applied = 0
                for c in corners:
                    nm = mapping.get(str(c["num"]))
                    if nm:
                        c["name"] = str(nm)
                        applied += 1
                if applied:
                    print(f"[coach] names file applied: {applied} zone "
                          f"names from {path}")
            else:
                starter = {str(c["num"]): c["name"] for c in corners}
                os.makedirs(TRACKS_DIR, exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(starter, f, indent=1)
                print(f"[coach] wrote starter names file "
                      f"{os.path.basename(path)} -- edit the names in "
                      f"it and they will stick")
        except Exception as e:
            print(f"[coach] names file problem ({e})")

    def _detect(self, p, rec):
        """detect_corners with the configured split threshold, plus
        a console line per wide zone showing the heading swings the
        splitter saw (v2.0.5 -- the Cotton third-turn evidence)."""
        diag = []
        corners = detect_corners(rec["spd"], rec["brk"], rec["gas"],
                                 p.bin_m, rec.get("hdg"),
                                 split_thresh_deg=float(
                                     self.cfg.get("heading_split_deg", 20)),
                                 split_diag=diag)
        for d in diag:
            print(f"[coach] split scan: apex bin {d['apex_bin']} "
                  f"swings {d['swings_deg']} deg -> {d['splits']} split(s) "
                  f"at {self.cfg.get('heading_split_deg', 20)} deg")
        return corners

    def set_profile(self, profile):
        self.profile = profile
        if getattr(self, "_stamped_key", None) != profile.key:
            self._stamped_key = profile.key
            try:
                self.log("session", {"key": profile.key,
                                     "version": VERSION,
                                     "track_length_m": profile.track_length_m})
            except Exception:
                pass
        # v2.1.0 cross-session history for this combo (see
        # history_path): loaded once per profile, sessions counted
        # when a stored reference exists.
        # v2.3: track guide for this profile's track, if one exists
        # (profiles folder first, then the app's guides/ folder).
        track_part = profile.key.split("__", 1)[0]
        profile.guide = _load_guide(track_part)
        self._guide_tipped = set()
        if (profile.guide and profile.corners
                and len((profile.guide.get("zones") or {}))
                != len(profile.corners)):
            # v2.6.0: guide zones are keyed by number like the names;
            # a count mismatch means the map changed under the guide.
            print(f"[coach] track guide for {track_part} covers "
                  f"{len(profile.guide.get('zones') or {})} zones but "
                  f"this profile has {len(profile.corners)} -- "
                  f"ignoring it until the counts match")
            profile.guide = None
        if profile.guide:
            print(f"[coach] track guide loaded: {track_part}")
        self.history = {"sessions": 0, "laps": 0, "corners": {}}
        try:
            hp = profile.history_path()
            if os.path.exists(hp):
                with open(hp, "r", encoding="utf-8") as f:
                    h = json.load(f)
                if isinstance(h, dict) and "corners" in h:
                    self.history = h
        except Exception:
            pass
        if profile.ref is not None:
            self.history["sessions"] = int(self.history.get("sessions", 0)) + 1
            self._save_history()
        self.map_lap_s = profile.best_lap_s if profile.ref else None
        self._loaded_with_ref = profile.ref is not None
        self.map_borrowed_from = None
        self._outline_borrowed = False
        if not profile.corners:
            # No map for this car yet -- borrow the track-level map if
            # another car has already mapped this circuit.
            try:
                mp = profile.track_map_path()
                if os.path.exists(mp):
                    with open(mp, "r", encoding="utf-8") as f:
                        d = json.load(f)
                    if d.get("corners"):
                        profile.corners = d["corners"]
                        if profile.outline is None and d.get("path"):
                            profile.outline = d["path"]
                            self._outline_borrowed = True
                        if profile.track_length_m is None:
                            profile.track_length_m = d.get("track_length_m")
                        self.map_borrowed_from = d.get("source")
                        print(f"[coach] corner map borrowed from "
                              f"{self.map_borrowed_from}: "
                              f"{len(profile.corners)} corners "
                              f"(first lap in this car sets its reference)")
            except Exception as e:
                print(f"[coach] could not borrow corner map ({e})")
        if profile.corners:
            self._apply_corner_names(profile.corners)
            self._apply_user_names(profile.corners)
        if profile.ref:
            print(f"[coach] reference loaded: best {fmt_lap(profile.best_lap_s)}, "
                  f"{len(profile.corners)} corners mapped")
        else:
            print("[coach] no reference yet -- your first full lap becomes the reference")

    def _save_history(self):
        if self.profile is None or not self.history:
            return
        try:
            os.makedirs(TRACKS_DIR, exist_ok=True)
            _atomic_write_json(self.profile.history_path(), self.history)
        except Exception:
            pass

    def observe_official(self, completed, i_last_ms, t):
        """Track AC's official lap times and settle each one.

        The position wrap that ends a lap in feed() and AC's own lap
        registration (completedLaps increment + iLastTime write) are
        not atomic -- in Albert's sessions the increment lands AFTER
        the wrap. v1-v1.2 kept one shared 'last official' variable
        that _finish_lap consumed on the wrap: once the ordering
        flipped, the value sitting there belonged to the previous lap,
        and every finish from then on inherited the previous lap's
        time, all session long (his report, Nov 1: crossing the line
        read out the lap before the one he had just finished).
        v1.3 pairs finishes and official times strictly by order, and
        lets each official value settle ~1.2 s (last write wins)
        before committing it.
        """
        if self._seen_completed is None:
            self._seen_completed = completed
            return
        if completed > self._seen_completed:
            self._seen_completed = completed
            self._official_watch = {"deadline": t + 1.2,
                                    "candidate_ms": i_last_ms}
        if self._official_watch is not None:
            if i_last_ms and i_last_ms > 0:
                self._official_watch["candidate_ms"] = i_last_ms
            if t >= self._official_watch["deadline"]:
                cand = self._official_watch["candidate_ms"]
                if cand and cand > 0:
                    self.official_times.append(cand / 1000.0)
                    # Guard: if a finish was somehow never detected
                    # (crossing below the wrap speed threshold), don't
                    # let unpaired officials accumulate and shift the
                    # pairing for the rest of the session. While the
                    # distance fallback is still learning (v2.13.0),
                    # finishes are DELAYED in the buffer, not missed:
                    # allow for the laps the buffer is holding.
                    cap = len(self.pending_finishes) + 1
                    if self._fb_buf is not None:
                        cap += len(self._fb_cands) + 2
                    while len(self.official_times) > cap:
                        self.official_times.pop(0)
                self._official_watch = None
                self._pump_finishes(t)

    def reset_lap(self):
        self.bins = LapBins()
        self.lap_valid_time = True
        self._off_events = []        # closed 2+ tyres-off excursions this lap
        self._off_cur = None         # excursion in progress, if any
        self._slip_events = []       # closed lockup/wheelspin events this lap
        self._slip_open = {}         # kind -> in-progress slip event
        self._lap_penalty = False    # AC flagged a penalty this lap
        self._px = 0.0
        self._py = 0.0
        self._lap_path = []          # [(pos, x, y)] driven-line samples
        self._last_sample_pos = None

    def _close_off_event(self, t):
        if self._off_cur is None:
            return
        ev = self._off_cur
        ev["dur_s"] = max(0.0, t - ev.pop("start_t"))
        self._off_events.append(ev)
        self._off_cur = None

    def _slip_update(self, t, pos, speed_kmh, brake, gas, wheel_w, wheel_r):
        """Lockup and wheelspin detection from the physics page
        (v2.7.0, from an external review: the coach already received
        wheelAngularSpeed and never used it). Each wheel's tangential
        speed (angular speed x tyre radius, from the static page) is
        compared with the car's speed: a front wheel turning much
        slower than the car under braking is locking; a rear wheel
        turning much faster under power is spinning. Events need
        0.18 s to count and tolerate a 0.12 s flicker gap, so one
        noisy frame can't fake or split an event."""
        if not wheel_w or len(wheel_w) < 4:
            return
        v = max(speed_kmh / 3.6, 3.0)
        radii = wheel_r if (wheel_r and len(wheel_r) >= 4) else [0.33] * 4
        ratios = [(wheel_w[i] * radii[i] - v) / v for i in range(4)]
        conds = {}
        if speed_kmh > 40 and brake > 0.25:
            conds["lockup"] = min(ratios[0], ratios[1]) < -0.22
        if speed_kmh > 40 and gas > 0.65:
            conds["spin"] = max(ratios[2], ratios[3]) > 0.28
        bin_now = int(pos * NBINS) % NBINS
        for kind in ("lockup", "spin"):
            st = self._slip_open.get(kind)
            if conds.get(kind):
                if st is None:
                    st = {"start_t": t, "last_t": t, "bins": set()}
                    self._slip_open[kind] = st
                st["last_t"] = t
                st["bins"].add(bin_now)
            elif st is not None and t - st["last_t"] > 0.12:
                self._close_slip_event(kind)

    def _close_slip_event(self, kind):
        st = self._slip_open.pop(kind, None)
        if st is None:
            return
        dur = st["last_t"] - st["start_t"]
        if dur >= 0.18:
            self._slip_events.append(
                {"kind": kind, "bins": st["bins"], "dur_s": round(dur, 2)})

    def _close_slip_events(self):
        for kind in list(self._slip_open):
            self._close_slip_event(kind)

    # -- main feed ----------------------------------------------------------
    # What the driver hears once per session. History: v1.9.1 added
    # the out-lap rules + best use of the lap, v1.9.2 the coaching-
    # loop explanation; v2.0.1 spaced it out (start ~900 m in, short
    # segments with quiet between). v2.0.4 (Albert, Nov 1 late):
    # "make it less intro ... add what stats you have about the
    # current car and track combo" -- so the lecture segments are
    # gone; what remains is the one-line rule, then the stats the
    # coach actually holds for this car at this track: his best
    # lap, the mapped places, and (once laps exist this session)
    # where the time is going. Segments are say()'d from feed()
    # when they fall due, never queued ahead, so the 15 s
    # stale-cue drop can't eat them.
    # v2.14.0: the briefing's facts never change, so its frames are
    # pools now -- the same wording every session read as mechanical
    # (Albert). One line is drawn per session per site; sass level 2+
    # adds a drier variant to each pool.
    BRIEFING_LOADED_A = (
        "Out lap — I don't coach this one, and nothing on it counts.",
        "Out lap. This one's free — no coaching, nothing counts.",
        "Warm-up lap — I stay quiet on this one, and nothing on it counts.")
    BRIEFING_LOADED_A_SASS = (
        "Out lap. I don't coach it, it doesn't count — enjoy the scenery.",)
    BRIEFING_FRESH_A = (
        "First lap in this car here sets your reference — every "
        "later lap gets compared to it.",
        "New combo here — this lap becomes the reference everything "
        "else is judged against.",
        "First lap in this car at this track: it sets the reference, "
        "so make it a clean one.")
    BRIEFING_FRESH_A_SASS = (
        "First lap here sets the reference. No pressure.",)
    BRIEF_OFFSETS_S = (0.0, 14.0)

    def _vary(self, pool, sass_pool=None):
        opts = list(pool)
        if sass_pool and self._sass_level() >= 2:
            opts += list(sass_pool)
        return random.choice(opts)

    @staticmethod
    def _pretty_key_part(key, which):
        """'nd_buttonwillow__ks_mazda_mx5_cup' -> 'buttonwillow' /
        'mazda mx5 cup' for speech."""
        try:
            part = key.split("__")[which]
        except Exception:
            return ""
        words = [w for w in part.replace("_", " ").split()
                 if w not in ("ks", "nd")]
        return " ".join(words)

    def _stats_line(self):
        p = self.profile
        if p is None:
            return ""
        parts = []
        n = len(p.corners)
        if self._loaded_with_ref and p.best_lap_s:
            best = fmt_lap_spoken(p.best_lap_s)
            parts.append(self._vary((
                f"Your best here in this car is {best}.",
                f"Best in this car here: {best}.",
                f"You've done {best} in this car here.")))
            if n:
                parts.append(self._vary((
                    f"{n} places mapped.",
                    f"{n} corners on the map.",
                    f"I've got {n} places mapped here.")))
        elif self.map_borrowed_from:
            src = self._pretty_key_part(self.map_borrowed_from, 1)
            parts.append(self._vary((
                f"Corner map borrowed from the {src}: "
                f"{n} places. This lap sets this car's reference.",
                f"I'm borrowing the {src} corner map — {n} places. "
                f"This lap sets this car's own reference.")))
        else:
            parts.append(self._vary((
                "No laps logged in this car here yet — "
                "this lap sets the reference.",
                "Nothing logged in this car here yet. "
                "This lap fixes that.")))
        leaks = []
        for num, hist in self.corner_history.items():
            clean = [r for r in hist if not r.get("off")]
            if clean:
                avg = sum(r["loss"] for r in clean) / len(clean)
                if avg >= float(self.cfg.get("min_loss_to_speak", 0.15)):
                    leaks.append((avg, clean[-1]["name"]))
        leaks.sort(key=lambda x: -x[0])
        if leaks:
            names = " and ".join(x[1] for x in leaks[:2])
            parts.append(self._vary((
                f"This session, your biggest leaks are {names}.",
                f"So far today the time's going at {names}.",
                f"Today's repeat offenders: {names}.")))
        elif self.history:
            # v2.1.0: nothing this session yet -- speak from the
            # cross-session record for this combo instead.
            avgs = []
            for nm, e in (self.history.get("corners") or {}).items():
                if e.get("n", 0) >= 2:
                    avg = e["sum"] / e["n"]
                    if avg >= float(self.cfg.get("min_loss_to_speak", 0.15)):
                        avgs.append((avg, nm))
            avgs.sort(key=lambda x: -x[0])
            sess = int(self.history.get("sessions", 0))
            if avgs and sess >= 1:
                names = " and ".join(x[1] for x in avgs[:2])
                parts.append(self._vary((
                    f"Over {sess} sessions in this car here, "
                    f"your biggest leaks are {names}.",
                    f"Across {sess} sessions here, the usual "
                    f"suspects are {names}.",
                    f"History says the time goes at {names} — "
                    f"{sess} sessions of it.")))
        return " ".join(parts)

    def _maybe_brief(self, t, speed_kmh, dist_m):
        if self._brief_plan is not None:
            due = [s for s in self._brief_plan if t - self._brief_t0 >= s[0]]
            for off, text in due:
                self.speaker.say(text)
                self._brief_plan.remove((off, text))
            return
        if (self._briefed or not self.cfg.get("out_lap_briefing", True)
                or self.profile is None or self.lap_number > 1
                or speed_kmh < 45):
            return
        if dist_m is not None:
            if self._dist0 is None:
                self._dist0 = dist_m
            start_m = float(self.cfg.get("briefing_start_m", 900))
            if self.profile.track_length_m:
                start_m = min(start_m, 0.3 * self.profile.track_length_m)
            if dist_m - self._dist0 < start_m:
                return
        self._briefed = True
        self._brief_t0 = t
        if self._loaded_with_ref:
            a = self._vary(self.BRIEFING_LOADED_A, self.BRIEFING_LOADED_A_SASS)
        else:
            a = self._vary(self.BRIEFING_FRESH_A, self.BRIEFING_FRESH_A_SASS)
        stats = self._stats_line()
        segs = [a] + ([stats] if stats else [])
        self._brief_plan = list(zip(self.BRIEF_OFFSETS_S, segs))[1:]
        self.speaker.say(a)

    def feed(self, t, pos, speed_kmh, brake, gas, dist_m, live=True, ict_ms=None,
             heading=None, tyres_out=None, steer=None,
             wheel_w=None, wheel_r=None, flag=None, penalty_s=None):
        # A NaN position raises inside the binning math (int(nan));
        # a non-finite frame is dropped, never fatal (v2.5.3).
        if pos is None or speed_kmh is None or not (
                math.isfinite(pos) and math.isfinite(speed_kmh)):
            return
        if not live:
            return
        if self.cfg.get("position_fallback", True) and self._pos_src != "spline":
            kw = {"brake": brake, "gas": gas, "dist_m": dist_m, "live": live,
                  "ict_ms": ict_ms, "heading": heading, "tyres_out": tyres_out,
                  "steer": steer, "wheel_w": wheel_w, "wheel_r": wheel_r,
                  "flag": flag, "penalty_s": penalty_s}
            if self._position_route(t, pos, speed_kmh, dist_m, ict_ms, kw):
                return
        self._feed_at(t, pos, speed_kmh, brake, gas, dist_m, live=live,
                      ict_ms=ict_ms, heading=heading, tyres_out=tyres_out,
                      steer=steer, wheel_w=wheel_w, wheel_r=wheel_r,
                      flag=flag, penalty_s=penalty_s)

    # -- distance position fallback (v2.13.0) ------------------------------
    # AC computes normalizedCarPosition from the track's AI spline.
    # Mod tracks without a working spline report a constant (the
    # Rainbow Road conversion: 0.0 in every frame of Albert's Nov 4
    # session -- six laps driven, none registered, no reference, no
    # cues, no deltas). The fallback watches the raw value while the
    # car first moves: if it never varies over 200 m of travel, the
    # spline is dead and position is derived instead from the game's
    # own lap timer (iCurrentTime resets at the line) segmented over
    # the odometer (distanceTraveled), normalised by a lap length
    # learned from two consecutive laps that agree within 2% (a
    # saved profile length, when one exists, only needs one lap
    # within 3% to validate). Frames are buffered while learning --
    # a lap cannot be binned before its length is known -- then
    # replayed through _feed_at with synthetic positions, so every
    # downstream system (bins, zones, cues, tone, report) works
    # unchanged. Frames before the first in-buffer timer reset are
    # the tail of a partial lap whose start was never recorded;
    # they are dropped, and their settled official time is skipped
    # at flush so lap/time pairing stays exact.

    def _fb_pos(self, dist_m):
        if self._fb_base_dist is None or not self._fb_lap_len:
            return 0.0
        p = (dist_m - self._fb_base_dist) / self._fb_lap_len
        return min(0.9999, max(0.0, p))

    def _position_route(self, t, raw_pos, speed_kmh, dist_m, ict_ms, kw):
        """Decide where this frame's position comes from. Returns
        True when the frame is fully handled here (buffered, flushed,
        or fed with a synthetic position); False means: feed the raw
        spline position as usual."""
        if self._pos_src == "unknown":
            if self._pos_lo is None or raw_pos < self._pos_lo:
                self._pos_lo = raw_pos
            if self._pos_hi is None or raw_pos > self._pos_hi:
                self._pos_hi = raw_pos
            if self._pos_last_dist is not None and speed_kmh > 10:
                dd = dist_m - self._pos_last_dist
                if 0.0 < dd < 60.0:
                    self._pos_travel += dd
            self._pos_last_dist = dist_m
            rng = (self._pos_hi - self._pos_lo) if self._pos_lo is not None else 0.0
            if rng >= 0.01:
                self._pos_src = "spline"
                return False
            if self._pos_travel < 200.0 or rng >= 0.002:
                return False  # still deciding: spline passthrough
            # Spline is dead: latch distance mode and wipe the
            # partial lap the constant position polluted.
            self._pos_src = "distance"
            self.reset_lap()
            self.prev_pos = None
            self.prev_dist = None
            self.lap_start_t = None
            self.lap_start_dist = None
            seed = None
            if self.profile is not None and self.profile.track_length_m:
                seed = float(self.profile.track_length_m)
            self._fb_len = seed
            self._fb_buf = []
            self._fb_base_dist = dist_m
            self._fb_base_t = t
            self._fb_prev_ict = ict_ms
            self._fb_skip_officials = 1 if (ict_ms or 0) > 3000 else 0
            print("[coach] this track reports no spline position -- "
                  "coaching by distance (lap timer + odometer)")
            try:
                self.log("position_fallback", {"seed_length_m": seed})
            except Exception:
                pass
        # ---- distance mode ----
        crossed = False
        if ict_ms is not None:
            prev = self._fb_prev_ict
            if (prev is not None and prev > 20000
                    and ict_ms < prev - 5000 and ict_ms < 5000):
                crossed = True
            self._fb_prev_ict = ict_ms
        if self._fb_len is not None and self._fb_buf is None:
            # Learned: feed live with a synthetic position.
            if crossed:
                cand = dist_m - (self._fb_base_dist or dist_m)
                cand_t = t - (self._fb_base_t or t)
                if 40.0 <= cand_t <= 420.0 and cand >= 800.0:
                    self._fb_cands.append(cand)
                    self._fb_cands = self._fb_cands[-4:]
                    med = sorted(self._fb_cands)[len(self._fb_cands) // 2]
                    if abs(med - self._fb_len) / self._fb_len <= 0.03:
                        self._fb_len = med
                self._fb_base_dist = dist_m
                self._fb_base_t = t
                self._fb_lap_len = self._fb_len
            elif self._fb_base_dist is None:
                self._fb_base_dist = dist_m
                self._fb_base_t = t
                self._fb_lap_len = self._fb_len
            elif (dist_m - self._fb_base_dist) > (self._fb_lap_len or 0) * 1.05:
                # Missed the timer reset (a glitch): rebase on the
                # odometer so position keeps its phase.
                self._fb_base_dist += self._fb_lap_len
            self._feed_at(t, self._fb_pos(dist_m), speed_kmh,
                          kw["brake"], kw["gas"], dist_m, live=kw["live"],
                          ict_ms=ict_ms, heading=kw["heading"],
                          tyres_out=kw["tyres_out"], steer=kw["steer"],
                          wheel_w=kw["wheel_w"], wheel_r=kw["wheel_r"],
                          flag=kw["flag"], penalty_s=kw["penalty_s"])
            return True
        # Learning: buffer the frame; a timer reset closes a
        # candidate lap.
        self._fb_buf.append((t, speed_kmh, dist_m, ict_ms, kw))
        if crossed:
            cand_d = dist_m - (self._fb_base_dist
                               if self._fb_base_dist is not None else dist_m)
            cand_t = t - (self._fb_base_t
                          if self._fb_base_t is not None else t)
            if 40.0 <= cand_t <= 420.0 and cand_d >= 800.0:
                self._fb_cands.append(cand_d)
                learned = None
                if self._fb_len is not None:
                    if abs(cand_d - self._fb_len) / self._fb_len <= 0.03:
                        learned = self._fb_len
                    else:
                        self._fb_len = None  # seed was wrong; pair afresh
                elif len(self._fb_cands) >= 2:
                    a, b = self._fb_cands[-2], self._fb_cands[-1]
                    if abs(a - b) / max(a, b) <= 0.02:
                        learned = (a + b) / 2.0
                if learned is not None:
                    self._fb_len = learned
                    self._fb_flush()
                    return True
            self._fb_base_dist = dist_m
            self._fb_base_t = t
            if len(self._fb_buf) > 30000:  # ~3.5 laps at full rate; bound it
                self._fb_buf = self._fb_buf[-15000:]
        return True

    def _fb_flush(self):
        """Lap length is known: replay the buffered frames with
        synthetic positions, segmented by the lap-timer resets inside
        the buffer. Frames before the first reset are the tail of a
        partial lap and are dropped (their official time is skipped
        first, so pairing stays exact)."""
        buf, self._fb_buf = self._fb_buf, None
        for _ in range(self._fb_skip_officials):
            if self.official_times:
                self.official_times.pop(0)
        self._fb_skip_officials = 0
        print(f"[coach] lap length learned from the lap timer: "
              f"{self._fb_len:.0f} m -- replaying the buffered laps")
        try:
            self.log("position_fallback_length",
                     {"length_m": round(self._fb_len, 1)})
        except Exception:
            pass
        try:
            self.speaker.say("This track has no position feed, "
                             "so I'm coaching by distance.")
        except Exception:
            pass
        base = None
        prev_ict = None
        started = False
        for (ft, spd, fdist, fict, kw) in buf:
            if fict is not None:
                if (prev_ict is not None and prev_ict > 20000
                        and fict < prev_ict - 5000 and fict < 5000):
                    base = fdist
                    started = True
                prev_ict = fict
            if not started:
                continue
            p = (fdist - base) / self._fb_len
            p = min(0.9999, max(0.0, p))
            self._feed_at(ft, p, spd, kw["brake"], kw["gas"], fdist,
                          live=kw["live"], ict_ms=fict, heading=kw["heading"],
                          tyres_out=kw["tyres_out"], steer=kw["steer"],
                          wheel_w=kw["wheel_w"], wheel_r=kw["wheel_r"],
                          flag=kw["flag"], penalty_s=kw["penalty_s"])
        if buf:
            self._fb_base_dist = base if base is not None else buf[-1][2]
            self._fb_base_t = buf[-1][0]
            self._fb_lap_len = self._fb_len
            self._fb_prev_ict = buf[-1][3]

    def _feed_at(self, t, pos, speed_kmh, brake, gas, dist_m, live=True, ict_ms=None,
                 heading=None, tyres_out=None, steer=None,
                 wheel_w=None, wheel_r=None, flag=None, penalty_s=None):
        # The coaching body, fed with an effective position (the
        # spline value, or a synthetic one from the distance
        # fallback). The NaN/live guards in feed() already ran; they
        # are repeated here so direct callers stay safe.
        if pos is None or speed_kmh is None or not (
                math.isfinite(pos) and math.isfinite(speed_kmh)):
            return
        if not live:
            return
        self._maybe_brief(t, speed_kmh, dist_m)
        if self.prev_pos is not None:
            wrapped = self.prev_pos > 0.85 and pos < 0.15 and speed_kmh > 30
            jumped_back = pos < self.prev_pos - 0.25 and not wrapped
            if wrapped:
                self._finish_lap(t)
            elif jumped_back:
                self.lap_valid_time = False  # reset/teleport: discard this lap's data
                self.reset_lap()
                self.lap_start_t = t
                self.lap_start_dist = dist_m
                self._teleported = True  # car was reset to the pits
        if self.lap_start_t is None:
            self.lap_start_t = t
            self.lap_start_dist = dist_m
        if ict_ms:
            self._ict_peak_ms = max(self._ict_peak_ms, ict_ms)
        if heading is not None and self.prev_dist is not None:
            # Dead-reckon the driven line for the report map: heading is
            # absolute in AC's physics page, distanceTraveled gives the
            # step length. Closure error is distributed in _close_path.
            dd = dist_m - self.prev_dist
            if 0.0 < dd < 60.0:
                self._px += math.sin(heading) * dd
                self._py += math.cos(heading) * dd
                if (self._last_sample_pos is None
                        or pos - self._last_sample_pos >= 0.0015):
                    self._lap_path.append((pos, self._px, self._py))
                    self._last_sample_pos = pos
        self._pump_finishes(t)
        self.bins.add(pos, speed_kmh, brake, gas, heading=heading, steer=steer)
        if tyres_out is not None and tyres_out >= 2:
            if self._off_cur is None:
                self._off_cur = {"start_t": t, "bins": set(), "max_tyres": 0}
            self._off_cur["bins"].add(int(pos * NBINS) % NBINS)
            self._off_cur["max_tyres"] = max(self._off_cur["max_tyres"], tyres_out)
        elif self._off_cur is not None:
            self._close_off_event(t)
        if wheel_w is not None:
            self._slip_update(t, pos, speed_kmh, brake, gas, wheel_w, wheel_r)
        if flag == 6 or (penalty_s is not None and penalty_s > 0.05):
            self._lap_penalty = True
        # A car crawling or stopped for a while is in the pits or
        # recovering, not on a flyer: the lap after it is an out lap.
        if speed_kmh < 5:
            if self._stopped_since is None:
                self._stopped_since = t
            elif t - self._stopped_since >= 10:
                self._pitted = True
        else:
            self._stopped_since = None
        self._prev_bin = (int(self.prev_pos * NBINS) % NBINS
                          if self.prev_pos is not None else None)
        self.prev_pos = pos
        self.prev_dist = dist_m
        if (self.profile and self.profile.ref and self.lap_number >= 1
                and not self._lap_muted):
            # lap_number is 0 throughout the session's first lap --
            # on a loaded profile that's the out lap, and Albert
            # asked for no coaching on it (Nov 1: the out lap drew
            # exit feedback like "Sunset: lost 984 tenths"). The
            # summary-side rule alone wasn't enough (v2.0.2): it only
            # classified the lap AFTER it ended, so live cues still
            # fired all through a slow out lap -- worst in a remap
            # session, where lap 1 becomes the reference and lap 2
            # (his real out lap, reference set at mapping pace) got
            # coached in real time. _lap_muted now silences live
            # cues on any lap that follows a pit stop or a teleport,
            # and on any lap running 15%+ off the reference pace.
            if (self.profile.best_lap_s and self.lap_start_t is not None
                    and pos > 0.02
                    and (t - self.lap_start_t) / pos > 1.15 * self.profile.best_lap_s):
                pass  # this lap is far off the pace -- an out/in lap
            else:
                self._service_tone_pending(t, pos)
                self._maybe_brake_tone(t, pos)
                self._maybe_approach_cue(t, pos)
                self._maybe_guide_tip(t, pos)
                self._maybe_exit_feedback(t, pos)

    @staticmethod
    def _crossed(prev_bin, cur_bin, target):
        """True if the car passed the target bin between the last two
        samples. (v1 checked 'current bin == target exactly'; at 30 Hz
        polling and ~2 m bins, fast sections can step clean over the
        one bin that mattered and the cue never fired.)"""
        if prev_bin is None:
            return False
        span = (cur_bin - prev_bin) % NBINS
        if span == 0 or span > 80:   # stationary, or a jump/teleport
            return False
        return (target - prev_bin) % NBINS <= span

    @staticmethod
    def _close_path(samples):
        """Turn one lap's driven-line samples into a closed outline.

        Dead reckoning drifts, so the lap's end rarely lands exactly on
        its start; spread that error linearly around the lap so the
        loop closes. Returns at most ~400 [x, y] points, or None if the
        lap didn't produce enough samples to draw.
        """
        if not samples or len(samples) < 60:
            return None
        xe, ye = samples[-1][1], samples[-1][2]
        n = len(samples)
        pts = []
        for i, (_pos, x, y) in enumerate(samples):
            f = i / (n - 1)
            pts.append([round(x - xe * f, 1), round(y - ye * f, 1)])
        if len(pts) > 400:
            step = len(pts) / 400.0
            pts = [pts[int(i * step)] for i in range(400)]
        return pts

    # -- lap lifecycle -------------------------------------------------------
    def _finish_lap(self, t):
        # A finish is queued, not finalized: its official time is still
        # settling (see observe_official). Invalid laps queue as 'skip'
        # records so they still consume exactly one official time and
        # the two streams can never drift a lap apart again.
        dur = t - (self.lap_start_t or t)
        dist = None
        if self.prev_dist is not None and self.lap_start_dist is not None:
            dist = self.prev_dist - self.lap_start_dist
        coverage = self.bins.coverage()
        self.reset_next_approach_state()
        valid = coverage >= 0.90 and (40 <= dur <= 420) and self.lap_valid_time
        if not valid:
            print(f"[lap] discarded (coverage {coverage:.0%}, duration {dur:.0f}s)")
        # The lap that just STARTED follows a pit stop or a reset if
        # either happened during the lap just finished: it's an out
        # lap, and live cues stay off for it (v2.0.2).
        self._lap_muted = bool(self._pitted or self._teleported)
        if self._lap_muted:
            print("[lap] out lap after a stop/reset -- live cues muted")
        self._pitted = False
        self._teleported = False
        spd, brk, gas, hdg, steer = self.bins.arrays()
        path_pts = self._close_path(self._lap_path) if valid else None
        self._close_off_event(t)
        self._close_slip_events()
        self.pending_finishes.append({
            "t_finish": t, "dur_s": dur, "dist_m": dist, "skip": not valid,
            "ict_peak_s": (self._ict_peak_ms / 1000.0) if self._ict_peak_ms else None,
            "spd": spd, "brk": brk, "gas": gas, "hdg": hdg, "str": steer,
            "path_pts": path_pts,
            "off_events": list(self._off_events),
            "slip_events": list(self._slip_events),
            "penalty": bool(self._lap_penalty),
            "muted": bool(self._lap_muted),
        })
        self._ict_peak_ms = 0
        self.reset_lap()
        self.lap_start_t = t
        self.lap_start_dist = self.prev_dist
        self._pump_finishes(t)

    def _pump_finishes(self, t):
        while self.pending_finishes:
            rec = self.pending_finishes[0]
            if self.official_times:
                official = self.official_times.pop(0)
                self.pending_finishes.pop(0)
                if not rec["skip"]:
                    self._finalize_lap(rec, official, "official")
            elif t - rec["t_finish"] > 6.0:
                # The game never produced a time for this finish (or
                # this is simulate mode) -- fall back to the coach's
                # own line-to-line timer rather than stalling forever.
                self.pending_finishes.pop(0)
                if not rec["skip"]:
                    self._finalize_lap(rec, rec["dur_s"], "timer_fallback")
            else:
                break

    def flush(self):
        """Finalize everything still pending (shutdown / sim end)."""
        while self.pending_finishes:
            rec = self.pending_finishes.pop(0)
            if rec["skip"]:
                continue
            if self.official_times:
                self._finalize_lap(rec, self.official_times.pop(0), "official")
            else:
                self._finalize_lap(rec, rec["dur_s"], "timer_fallback")

    def _finalize_lap(self, rec, lap_time, source):
        self.lap_number += 1
        spd, brk, gas = rec["spd"], rec["brk"], rec["gas"]
        dist = rec["dist_m"]
        p = self.profile
        if p is not None:
            # v2.6.0 layout guard: AC's shared memory names the track
            # FOLDER, not the layout, so two layouts of one circuit
            # would share a profile and silently benchmark each other.
            # The first lap of the session exposes it -- it measures a
            # materially different distance than the loaded profile
            # stored. Switch to a layout-specific profile before any
            # of this lap's data lands in the wrong one.
            if (dist and getattr(p, "_from_disk", False)
                    and p.track_length_m
                    and not getattr(p, "_layout_checked", False)):
                p._layout_checked = True
                if abs(dist - p.track_length_m) / p.track_length_m > 0.03:
                    new_key = (f"{p.key}__layout"
                               f"{int(round(dist / 10.0) * 10)}")
                    np = TrackProfile(new_key)
                    np.load()
                    print(f"[coach] this lap measures "
                          f"{fmt_track_len(dist)} but the loaded profile "
                          f"is {fmt_track_len(p.track_length_m)} -- a "
                          f"different layout of the same circuit. "
                          f"Switching to its own profile.")
                    self.log("layout_switch", {"from": p.key, "to": new_key,
                                               "dist_m": dist})
                    self.set_profile(np)
                    p = self.profile
            if rec.get("path_pts") and (p.outline is None or self._outline_borrowed):
                p.outline = rec["path_pts"]
                self._outline_borrowed = False
                if p.ref is not None:
                    p.save()
            if dist and 1500 < dist < 9000:
                p.track_length_m = dist
            if p.ref is None:
                p.ref = {"spd": spd, "brk": brk, "gas": gas,
                         "hdg": rec.get("hdg"), "str": rec.get("str")}
                p.best_lap_s = lap_time
                p.ref_dirty = (bool(rec.get("off_events"))
                               or bool(rec.get("penalty")))
                if p.corners and self.map_borrowed_from:
                    # Borrowed corner map (another car, same track):
                    # keep its zones and names; this lap only sets the
                    # reference traces for THIS car.
                    print(f"[coach] corner map borrowed from "
                          f"{self.map_borrowed_from} kept for this car")
                else:
                    p.corners = self._detect(p, rec)
                    self._apply_corner_names(p.corners)
                    self._apply_user_names(p.corners)
                self.map_lap_s = lap_time
                p.save()
                print(f"[coach] reference set: {fmt_lap(lap_time)}, "
                      f"{len(p.corners)} corners, track {fmt_track_len(p.track_length_m)}")
                self.log("reference_set", {"lap_s": lap_time, "corners": len(p.corners),
                                           "track_length_m": p.track_length_m,
                                           "ref_dirty": p.ref_dirty})
                self.speaker.say(f"Reference lap in, {fmt_lap_spoken(lap_time)}. "
                                 f"{len(p.corners)} corners mapped. Coaching starts next lap.")
            else:
                results = analyze_lap(spd, brk, gas, p.ref, p.corners, p.bin_m,
                                      off_events=rec.get("off_events"),
                                      off_min_s=float(self.cfg.get("off_min_duration_s", 0.35)),
                                      off_2_min_s=float(self.cfg.get("off_min_duration_2tyres_s", 0.9)),
                                      lap_str=rec.get("str"),
                                      slip_events=rec.get("slip_events"))
                # An excused corner is a save -- once. The same corner
                # excused off_repeat_laps times running is a pattern,
                # not an accident: from then on it counts as line and
                # gets coached like any other corner (Albert: the
                # summaries "repeat events from other laps", Nov 1).
                lap_had_off = any(r.get("off") for r in results)
                # v2.7.0: an AC penalty on the lap blocks the
                # reference exactly like an off does (the reviewer's
                # "validity data" point; the offs rule stays primary
                # in practice sessions, where penalties are rare).
                lap_blocked = lap_had_off or bool(rec.get("penalty"))
                repeat_limit = int(self.cfg.get("off_repeat_laps", 3))
                for r in results:
                    if r.get("off"):
                        streak = self._excuse_streak.get(r["num"], 0) + 1
                        self._excuse_streak[r["num"]] = streak
                        if streak >= repeat_limit:
                            r["off"] = False
                            r["repeat_off"] = True
                    else:
                        self._excuse_streak[r["num"]] = 0
                # Out/in laps get no coaching: the first analysed lap
                # on a loaded profile is the out lap, and any lap 15%+
                # off the best is warm-up or a pit return (Albert's
                # request, Nov 1). They're logged, but kept out of the
                # corner history, the averages and the spoken summary.
                first_analyzed = not self._any_analyzed
                self._any_analyzed = True
                out_lap = ((first_analyzed and self._loaded_with_ref)
                           or rec.get("muted")
                           or (first_analyzed and not self._loaded_with_ref
                               and self.lap_number > 1)
                           or (p.best_lap_s is not None
                               and lap_time > 1.15 * p.best_lap_s))
                if not out_lap:
                    self._last_lap_spd = spd
                    self._note_counted_lap_traces(lap_time, spd, brk, results)
                    for r in results:
                        self.corner_history.setdefault(r["num"], []).append(r)
                        if len(self.corner_history[r["num"]]) > 8:
                            self.corner_history[r["num"]].pop(0)
                    # v2.1.0: fold the lap into the persistent
                    # per-combo history (clean corners only).
                    if self.history is not None:
                        self.history["laps"] = int(self.history.get("laps", 0)) + 1
                        hc = self.history.setdefault("corners", {})
                        for r in results:
                            if r.get("off"):
                                continue
                            e = hc.setdefault(r["name"], {"sum": 0.0, "n": 0})
                            e["sum"] = round(e["sum"] + r["loss"], 3)
                            e["n"] += 1
                        self._save_history()
                total_loss = sum(max(0.0, r["loss"]) for r in results)
                lap_rec = {"lap": self.lap_number, "time_s": lap_time,
                           "corners": results, "total_corner_loss": total_loss,
                           "time_source": source, "dur_s": rec["dur_s"],
                           "ict_peak_s": rec["ict_peak_s"],
                           "out_lap": bool(out_lap)}
                self.lap_records.append(lap_rec)
                self.log("lap", lap_rec)
                if out_lap:
                    print(f"[lap] out/in lap ({fmt_lap(lap_time)}) -- no coaching")
                else:
                    self._speak_lap_summary(lap_time, results)
                # v2.5.1: the report outline follows the latest
                # CLEAN counted lap. It used to freeze on the first
                # lap ever driven on the combo, so an exploratory
                # first lap -- wrong turn, spin recovery, pit drive
                # -- baked its excursions into every report forever
                # (Willow Springs, Nov 2: a spike that is not on the
                # real circuit outlived four cleaner laps).
                if (not out_lap and not lap_had_off
                        and rec.get("path_pts")):
                    p.outline = rec["path_pts"]
                    self._outline_borrowed = False
                    p.save()
                if lap_time < (p.best_lap_s or 1e9) and lap_blocked:
                    # v2.6.0 (external review): a lap with an off --
                    # including a cut corner -- must not become the
                    # reference, or every clean lap afterward reads as
                    # a loss at that corner. The time stands in the
                    # log; the benchmark stays the clean lap.
                    print(f"[coach] {fmt_lap(lap_time)} was faster but "
                          f"had an off -- the clean reference stays")
                    self.log("reference_kept_off", {"lap_s": lap_time})
                elif (getattr(p, "ref_dirty", False) and not lap_blocked
                        and p.best_lap_s
                        and lap_time <= p.best_lap_s * 1.03):
                    # The standing reference itself came from a lap
                    # with an off (typically the first-ever lap, which
                    # has to set something). A clean lap within 3%
                    # takes over as the honest benchmark; the corner
                    # map does not move.
                    p.ref = {"spd": spd, "brk": brk, "gas": gas,
                         "hdg": rec.get("hdg"), "str": rec.get("str")}
                    p.best_lap_s = lap_time
                    p.ref_dirty = False
                    p.save()
                    print(f"[coach] clean reference in: {fmt_lap(lap_time)} "
                          f"(the old one had an off in it)")
                    self.log("reference_cleaned", {"lap_s": lap_time})
                    self.speaker.say(
                        f"Clean reference in, {fmt_lap_spoken(lap_time)}.")
                if lap_time < (p.best_lap_s or 1e9) and not lap_blocked:
                    p.ref = {"spd": spd, "brk": brk, "gas": gas,
                         "hdg": rec.get("hdg"), "str": rec.get("str")}
                    p.best_lap_s = lap_time
                    p.ref_dirty = False
                    # Keep the corner map stable: v1 re-detected corners
                    # on every new best, so the map grew 6 -> 9 corners
                    # mid-session and "Turn 5" stopped meaning one place.
                    # Re-map only when the new best is 3%+ faster than the
                    # lap the map was built from, and say so out loud.
                    if self.map_lap_s is None or lap_time < 0.97 * self.map_lap_s:
                        p.corners = self._detect(p, rec)
                        self._apply_corner_names(p.corners)
                        self._apply_user_names(p.corners)
                        self.map_borrowed_from = None
                        self.map_lap_s = lap_time
                        self.corner_history.clear()
                        self.approach_fired.clear()
                        self.exit_fired.clear()
                        self.speaker.say(f"Reference updated. "
                                         f"{len(p.corners)} corners mapped.")
                    p.save()
                    print(f"[coach] new reference lap: {fmt_lap(lap_time)}")
                    self.log("reference_updated", {"lap_s": lap_time})

    def reset_next_approach_state(self):
        self.approach_fired = set()
        self.exit_fired = set()
        self.approach_cues_this_lap = 0
        self._tone_fired = set()
        if self._tone_pending is not None:
            try:
                self.log("tone_dropped_busy",
                         {"num": self._tone_pending["num"],
                          "reason": "lap_end"})
            except Exception:
                pass
            self._tone_pending = None
        self._exit_spoken = 0
        self._live_losses = {}

    # -- section feedback (v2.1.0) ------------------------------------
    def _section_groups(self):
        cs = self.profile.corners if self.profile else []
        if len(cs) < 6:
            return []
        n = len(cs)
        sizes = [n // 3 + (1 if i < n % 3 else 0) for i in range(3)]
        groups, idx = [], 0
        for s in sizes:
            groups.append([c["num"] for c in cs[idx:idx + s]])
            idx += s
        return groups

    def _maybe_section_line(self, num):
        """Simulator Controller's Driving Coach reviews the last
        SECTION of track, not just individual corners; this is that
        idea in our terms. When a section's losses are death by a
        thousand cuts -- real in total, but no single corner big
        enough to earn its own callout -- say so once, at the
        section boundary. The final section is the lap summary's
        job, so only the first two boundaries can speak."""
        if not self.cfg.get("section_feedback", True):
            return None
        labels = ["First section", "Middle section"]
        for gi, g in enumerate(self._section_groups()[:2]):
            if num != g[-1]:
                continue
            tot, worst = 0.0, None
            for m in g:
                r = self._live_losses.get(m)
                if r is None or r.get("off"):
                    continue
                tot += max(0.0, r["loss"])
                if worst is None or r["loss"] > worst["loss"]:
                    worst = r
            if tot < float(self.cfg.get("section_min_loss_s", 0.45)):
                return None
            if worst is not None and worst["loss"] >= 0.35:
                return None  # that corner earns its own callout instead
            txt = f"{labels[gi]}: lost {tenths_phrase(tot)} there"
            if worst is not None and worst["loss"] >= 0.1:
                txt += f" — mostly {worst['name']}"
            return txt
        return None

    # -- speaking ------------------------------------------------------------
    def _cooldown_ok(self, t):
        return (t - self.last_spoke_t) >= float(self.cfg["cue_cooldown_s"])

    def _focus_corners(self):
        # v1 required the multi-lap AVERAGE loss to clear the bar before
        # any approach cue existed. On a clean session (Albert's Nov 1
        # session 2: most corner losses negative vs his own best) the
        # focus list stayed empty all session and no live cue ever
        # fired. v1.2: a corner also qualifies off its LATEST lap --
        # one real loss, or one big coast, earns next lap's heads-up.
        scored = []
        for num, hist in self.corner_history.items():
            clean = [r for r in hist if not r.get("off")]
            if not clean:
                continue
            avg = sum(r["loss"] for r in clean) / len(clean)
            latest = hist[-1]
            cue_src = latest if not latest.get("off") else clean[-1]
            # v2.6.0 steadiness (external review): the v1.2 latest-lap
            # trigger fired a next-lap warning off ONE 0.25 s loss, so
            # the coach could chase noise. A single lap now qualifies
            # a corner instantly only on a BIG loss; the old levels
            # still qualify when another clean lap corroborates the
            # corner (loss >= 0.15 there too). The multi-lap average
            # path is unchanged.
            corroborated = any(r["loss"] >= 0.15 for r in clean
                               if r is not cue_src)
            cmap = {c["num"]: c for c in self.profile.corners} if self.profile else {}
            if cmap.get(num, {}).get("minor"):
                # minor zones speak only on a real loss (v2.0)
                qualifies = (
                    avg >= 0.25
                    or (not latest.get("off") and (
                        latest["loss"] >= 0.55
                        or (latest["loss"] >= 0.15 and latest["coast_s"] >= 3.0)
                        or (corroborated and (
                            latest["loss"] >= 0.35
                            or (latest["loss"] >= 0.15 and latest["coast_s"] >= 2.0)))))
                )
            else:
                qualifies = (
                    avg >= float(self.cfg["min_loss_to_speak"])
                    or (not latest.get("off") and (
                        latest["loss"] >= 0.45
                        or (latest["loss"] >= 0.10 and latest["coast_s"] >= 2.5)
                        or (corroborated and (
                            latest["loss"] >= 0.25
                            or (latest["loss"] >= 0.10 and latest["coast_s"] >= 1.5)))))
                )
            if qualifies:
                scored.append((max(avg, cue_src["loss"]), num, cue_src))
        scored.sort(key=lambda x: -x[0])
        return scored

    def _in_any_zone(self, cur_bin):
        """True while the car is inside any corner zone (v2.8.0
        scheduler gate: deferrable speech waits for a straight)."""
        if not self.profile:
            return False
        for c in self.profile.corners:
            span = (c["zone_end"] - c["zone_start"]) % NBINS
            if (cur_bin - c["zone_start"]) % NBINS <= span:
                return True
        return False

    def _maybe_brake_tone(self, t, pos):
        # v2.12.0 brake tone (config "brake_tone", default on
        # since v2.14.1): one short tone as the car crosses the reference lap's
        # brake point for the next corner -- an audio marker that
        # survives losing the visual one (the Bloops lesson from the
        # Kez review, and the brake tones singled out in the Nov 3
        # landscape research). Corners the reference lap did not
        # brake for stay silent: no beep means no brake. Runs under
        # the same out-lap / off-pace mute as the voice cues, fires
        # once per corner per lap, and only into an idle audio
        # channel -- a tone queued behind speech would land late and
        # teach the wrong point. v2.13.0: a busy channel no longer
        # drops it outright; the tone is held and fired the moment
        # the channel frees, within a short late window past the
        # brake point (_service_tone_pending); only past that window
        # is it dropped (logged as tone_dropped_busy).
        if not self.cfg.get("brake_tone", False):
            return
        cur_bin = int(pos * NBINS) % NBINS
        lead_bins = int(float(self.cfg.get("brake_tone_lead_m", 0))
                        / self.profile.bin_m)
        for c in self.profile.corners:
            num = c["num"]
            if num in self._tone_fired or not c.get("has_brake_zone"):
                continue
            target = (c["brake_bin"] - lead_bins) % NBINS
            if self._crossed(self._prev_bin, cur_bin, target):
                self._tone_fired.add(num)
                if self.speaker.tone_ready():
                    self.speaker.tone()
                    self.log("tone", {"num": num,
                                      "name": c.get("name")})
                else:
                    # v2.13.0: hold it, don't drop it yet. Albert's
                    # first live report was that the beeps coincided
                    # with voice cues and vanished -- exactly the
                    # busy-channel case. _service_tone_pending fires
                    # it the moment the channel frees, while the car
                    # is still close enough to the brake point for
                    # the marker to mean the same thing; only past
                    # that window is it dropped (and logged).
                    self._tone_pending = {"num": num,
                                          "name": c.get("name"),
                                          "target": target, "t": t}
                break

    def _service_tone_pending(self, t, pos):
        """v2.13.0: fire a brake tone that was held by a busy channel
        as soon as the channel frees, provided the car is still
        within brake_tone_late_m metres past the brake point (and
        within 2 s of the crossing). Past the window it is dropped
        and logged as tone_dropped_busy: a truly late tone teaches
        the wrong brake point."""
        p = self._tone_pending
        if p is None or not self.profile:
            return
        cur_bin = int(pos * NBINS) % NBINS
        late_m = ((cur_bin - p["target"]) % NBINS) * self.profile.bin_m
        late_limit = float(self.cfg.get("brake_tone_late_m", 25))
        if self.speaker.tone_ready() and late_m <= late_limit:
            self.speaker.tone()
            self.log("tone", {"num": p["num"], "name": p.get("name"),
                              "delayed": True,
                              "late_m": round(late_m, 1)})
            self._tone_pending = None
        elif late_m > late_limit or t - p["t"] > 2.0:
            self.log("tone_dropped_busy", {"num": p["num"]})
            self._tone_pending = None

    def _maybe_approach_cue(self, t, pos):
        if self.approach_cues_this_lap >= int(self.cfg["max_approach_cues_per_lap"]):
            return
        if not self._cooldown_ok(t):
            return
        bin_m = self.profile.bin_m
        approach_bins = int(float(self.cfg["approach_distance_m"]) / bin_m)
        cur_bin = int(pos * NBINS) % NBINS
        for _avg, num, latest in self._focus_corners():
            if num in self.approach_fired:
                continue
            corner = next(c for c in self.profile.corners if c["num"] == num)
            trig = (corner["brake_bin"] - approach_bins) % NBINS
            since_trig_m = ((cur_bin - trig) % NBINS) * bin_m
            if since_trig_m > 430:
                continue  # window not reached yet (or long past it)
            dist_to_brake_m = ((corner["brake_bin"] - cur_bin) % NBINS) * bin_m
            if dist_to_brake_m < 100:
                # Too late to be useful -- dropped, not spoken
                # (v2.8.0 scheduler: a late cue is worse than none).
                self.approach_fired.add(num)
                continue
            if self._in_any_zone(cur_bin):
                # Mid-corner in a linked sequence: hold the cue. It
                # stays eligible until the drop point above.
                continue
            cue = corner_cue(
                latest,
                brake_only=(self.cfg.get("focus") == "brake"),
                priority=guide_priority(getattr(self.profile, "guide", None), num),
                guide=getattr(self.profile, "guide", None),
                symptom_tips=bool(self.cfg.get("guide_symptom_tips", True)))
            if cue:
                mem = self._cue_memory.get(num)
                if (mem and mem["text"] == cue
                        and self.lap_number - mem["lap"] < 3
                        and latest["loss"] <= mem["loss"] + 0.15):
                    # v2.8.0 cue memory: the same instruction at the
                    # same corner, recently, and no worse -- repeating
                    # it teaches nothing. It earns its way back by
                    # getting worse or by three laps passing.
                    self.approach_fired.add(num)
                    self.log("cue_suppressed", {"num": num, "reason": "repeat"})
                else:
                    self.speaker.say(cue)
                    self.last_spoke_t = t
                    self.approach_fired.add(num)
                    self.approach_cues_this_lap += 1
                    self._cue_memory[num] = {
                        "text": cue, "lap": self.lap_number,
                        "loss": latest["loss"]}
            break

    def _maybe_guide_tip(self, t, pos):
        # v2.3 spoken guide tips (config "guide_spoken_tips", default
        # off): the first time the car approaches a guided corner on
        # a coached lap, speak that corner's first guide tip -- once
        # per corner per session. Data cues keep their own budget;
        # tips share only the speech cooldown. Called from feed()
        # under the same out-lap / off-pace mute as the other cues.
        if not self.cfg.get("guide_spoken_tips"):
            return
        guide = getattr(self.profile, "guide", None)
        if not guide or not self._cooldown_ok(t):
            return
        bin_m = self.profile.bin_m
        approach_bins = int(float(self.cfg["approach_distance_m"]) / bin_m)
        cur_bin = int(pos * NBINS) % NBINS
        for c in self.profile.corners:
            num = c["num"]
            if num in self._guide_tipped:
                continue
            tips = (guide_zone(guide, num) or {}).get("tips") or []
            if not tips:
                continue
            trig = (c["brake_bin"] - approach_bins) % NBINS
            delta = (cur_bin - trig) % NBINS
            if delta <= max(6, int(25.0 / bin_m)) and not self._in_any_zone(cur_bin):
                name = c.get("name") or f"Turn {num}"
                self.speaker.say(f"{name}: {tip_text(tips[0])}")
                self.last_spoke_t = t
                self._guide_tipped.add(num)
                break

    def _maybe_exit_feedback(self, t, pos):
        if self._exit_spoken >= 3:
            return  # v2.0: finer map, same voice budget -- cap exit callouts per lap
        if not self._cooldown_ok(t):
            return
        cur_bin = int(pos * NBINS) % NBINS
        spd, brk, gas, _hdg, str_live = self.bins.arrays()
        for c in self.profile.corners:
            num = c["num"]
            if num in self.exit_fired:
                continue
            if self._crossed(self._prev_bin, cur_bin, (c["zone_end"] + 2) % NBINS):
                self.exit_fired.add(num)
                live_events = list(self._off_events)
                if self._off_cur is not None:
                    cur = dict(self._off_cur)
                    cur["dur_s"] = max(0.0, t - cur["start_t"])
                    live_events.append(cur)
                live_slip = list(self._slip_events)
                for kind, st in self._slip_open.items():
                    if st["last_t"] - st["start_t"] >= 0.18:
                        live_slip.append({"kind": kind, "bins": set(st["bins"]),
                                          "dur_s": round(st["last_t"] - st["start_t"], 2)})
                res = analyze_lap(spd, brk, gas, self.profile.ref, [c], self.profile.bin_m,
                                  slip_events=live_slip,
                                  off_events=live_events,
                                  off_min_s=float(self.cfg.get("off_min_duration_s", 0.35)),
                                  off_2_min_s=float(self.cfg.get("off_min_duration_2tyres_s", 0.9)),
                                  lap_str=str_live)[0]
                self._live_losses[num] = res  # v2.1.0: section feedback feedstock
                line = None
                fix_text = None
                # Minor zones (shallow dips) must lose more before
                # they earn a callout -- more places, not more talking.
                minor = bool(c.get("minor"))
                min_loss = 0.35 if minor else float(self.cfg.get("exit_min_loss_s", 0.2))
                if res.get("off"):
                    pass  # he was off the track in that zone -- no feedback, it was a save
                elif res["loss"] >= min_loss:
                    if (self._sass_level() >= 2
                            and self._exit_line_count.get(num, 0) >= 1):
                        line = self._sass_pick(f"exit_again_{num}", [
                            f"{c['name']} again: lost {tenths_phrase(res['loss'])} there",
                            f"{c['name']}: lost {tenths_phrase(res['loss'])} there. Again."])
                    else:
                        line = f"{c['name']}: lost {tenths_phrase(res['loss'])} there"
                    # v2.6.0: say WHY, not just how much -- the live
                    # single-zone analysis already computed the causes
                    # and the old line threw them away until next
                    # lap's approach cue (a review point).
                    fix = corner_cue(
                        res,
                        priority=guide_priority(
                            getattr(self.profile, "guide", None), num),
                        guide=getattr(self.profile, "guide", None),
                        symptom_tips=bool(self.cfg.get(
                            "guide_symptom_tips", True)))
                    if fix and ": " in fix:
                        fix_text = fix.split(": ", 1)[1]
                elif res["loss"] >= (0.20 if minor else 0.10) and res["coast_s"] >= 2.0:
                    _ce = res.get("coast_entry_s") or 0.0
                    _cx = res.get("coast_exit_s") or 0.0
                    if _ce >= 0.6 * res["coast_s"]:
                        _what = "big roll to the apex"
                    elif _cx >= 0.6 * res["coast_s"]:
                        _what = "slow back to power"
                    else:
                        _what = "big coast"
                    line = (f"{c['name']}: {_what} there, costing "
                            f"{tenths_phrase(res['loss'])}")
                if line:
                    self._exit_line_count[num] = \
                        self._exit_line_count.get(num, 0) + 1
                    if fix_text:
                        # v2.9.0: measurement [beat] instruction --
                        # spoken as two clauses with a spliced pause
                        # when the speaker supports it.
                        say_clauses = getattr(self.speaker, "say_clauses", None)
                        if say_clauses:
                            say_clauses(line, fix_text, int(
                                self.cfg.get("exit_clause_gap_ms", 220)))
                        else:
                            self.speaker.say(f"{line} -- {fix_text}")
                    else:
                        self.speaker.say(line)
                    self.last_spoke_t = t
                    self._exit_spoken += 1
                else:
                    sline = self._maybe_section_line(num)
                    if sline:
                        self.speaker.say(sline)
                        self.last_spoke_t = t
                        self._exit_spoken += 1
                break

    def _seed_optimal(self):
        """(Re)build the stitched optimal lap from the current
        reference (v2.11.0): the reference's own traces, with each
        zone's time as the mark to beat. Counted laps then swap in
        any zone they did better. Re-seeded whenever the reference
        changes (the seed mark is the reference's lap time)."""
        p = self.profile
        self._optimal = None
        self._optimal_ref_time = None
        if (p is None or not p.ref or not p.ref.get("spd")
                or not p.corners):
            return
        if any(c.get("zone_start") is None or c.get("zone_end") is None
               for c in p.corners):
            return
        spd = list(p.ref["spd"])
        brk = list(p.ref.get("brk") or [0.0] * len(spd))
        zt = {}
        for c in p.corners:
            zt[c["num"]] = zone_time(p.ref["spd"], c["zone_start"],
                                     c["zone_end"], p.bin_m)
        self._optimal = {"spd": spd, "brk": brk, "zone_times": zt}
        self._optimal_ref_time = p.best_lap_s

    def _note_counted_lap_traces(self, lap_time, spd, brk, results):
        """Retain the traces the report's three-lap chart needs
        (v2.11.0): the session's best lap outright, and the optimal
        lap's zone segments whenever a clean zone beats the mark.
        Off zones never enter the optimal -- a cut is not a line."""
        if (self._session_best is None
                or lap_time < self._session_best["time"]):
            self._session_best = {"time": lap_time, "spd": list(spd),
                                  "brk": list(brk)}
        p = self.profile
        if p is None or not p.corners:
            return
        if (self._optimal is None
                or self._optimal_ref_time != p.best_lap_s):
            self._seed_optimal()
        if self._optimal is None:
            return
        cmap = {c["num"]: c for c in p.corners}
        for r in results:
            if r.get("off") or r.get("zone_time") is None:
                continue
            c = cmap.get(r["num"])
            if c is None:
                continue
            if r["zone_time"] < self._optimal["zone_times"].get(
                    r["num"], 1e9) - 1e-9:
                z0 = c["zone_start"]
                span = (c["zone_end"] - c["zone_start"]) % NBINS
                for k in range(span + 1):
                    b = (z0 + k) % NBINS
                    self._optimal["spd"][b] = spd[b]
                    self._optimal["brk"][b] = brk[b]
                self._optimal["zone_times"][r["num"]] = r["zone_time"]

    # -- sass (v2.14.0) ---------------------------------------------------
    # A spectrum, not a switch (Albert's call): sass_level 0 is the
    # pro voice he has always had; 1 gives lap summaries a dry
    # variant pool; 2 adds repeat-offender and well-off-pace lines
    # plus 'again' forms on exit feedback; 3 sharpens the pools.
    # Sass only ever wraps the facts -- numbers and fixes are
    # untouched -- and it never enters approach cues, which stay
    # pure function at every level. Pools rotate per site so a line
    # cannot repeat until its pool is used up.

    def _sass_level(self):
        try:
            return int(self.cfg.get("sass_level", 0))
        except Exception:
            return 0

    def _sass_pick(self, site, pool):
        if not pool:
            return None
        i = self._sass_rot.get(site, 0)
        self._sass_rot[site] = i + 1
        return pool[i % len(pool)]

    def _speak_lap_summary(self, lap_time, results):
        p = self.profile
        sass = self._sass_level()
        parts = [f"Lap {fmt_lap_spoken(lap_time)}."]
        if p and p.best_lap_s and abs(lap_time - p.best_lap_s) > 0.05:
            if lap_time > p.best_lap_s:
                d = lap_time - p.best_lap_s
                if sass >= 2 and d >= 2.0:
                    parts.append(self._sass_pick("summary_slow", [
                        f"{d:.1f} off your best.",
                        f"{d:.1f} off your best — some sightseeing in that one."]))
                else:
                    parts.append(f"{d:.1f} off your best.")
            else:
                if sass >= 1:
                    pool = ["That's a new best.",
                            "New best. Frame it.",
                            "New best — the reference just moved."]
                    if sass >= 3:
                        pool.append("New best. Don't let it go to your head.")
                    parts.append(self._sass_pick("summary_pb", pool))
                else:
                    parts.append("That's a new best.")
        clean = [r for r in results if not r.get("off")]
        worst = max(clean, key=lambda r: r["loss"]) if clean else None
        leak_spoken = False
        if worst and worst["loss"] >= float(self.cfg["min_loss_to_speak"]):
            brake_only = self.cfg.get("focus") == "brake"
            cue = corner_cue(
                worst, brake_only=brake_only,
                priority=guide_priority(getattr(self.profile, "guide", None),
                                        worst.get("num")),
                guide=getattr(self.profile, "guide", None),
                symptom_tips=bool(self.cfg.get("guide_symptom_tips", True)))
            if cue:
                parts.append(f"Biggest leak: {cue.lower()}, costing {tenths_phrase(worst['loss'])}.")
                leak_spoken = True
            elif brake_only:
                parts.append(f"Biggest leak: {worst['name'].lower()}, costing {tenths_phrase(worst['loss'])}.")
                leak_spoken = True
        if (leak_spoken and sass >= 2
                and worst.get("num") is not None
                and worst.get("num") == self._last_summary_leak):
            pool = ["Same corner as last lap.",
                    "It was that corner last lap, too."]
            if sass >= 3:
                pool.append("We're naming a grandstand after that corner at this rate.")
            parts.append(self._sass_pick("summary_repeat", pool))
        if leak_spoken:
            self._last_summary_leak = worst.get("num")
        offs = [r for r in results if r.get("off")]
        if offs:
            parts.append(f"Off track at {offs[0]['name']} — that one doesn't count.")
        reps = [r for r in results if r.get("repeat_off")]
        if reps:
            parts.append(f"{reps[0]['name']}: wide there every lap — "
                         f"that's a pattern, not a save, so it counts now.")
        if not leak_spoken and not offs and sass >= 1:
            pool = ["Nothing to fix.",
                    "Clean lap — nothing to fix."]
            if sass >= 2:
                pool.append("Nothing to fix. Suspicious, but I'll allow it.")
            parts.append(self._sass_pick("summary_clean", pool))
        self.speaker.say(" ".join(parts))

    # -- debrief --------------------------------------------------------------
    def session_summary_lines(self):
        lines = []
        counted = [r for r in self.lap_records if not r.get("out_lap")]
        if not counted:
            return ["No complete laps analysed this session."]
        best = min(r["time_s"] for r in counted)
        lines.append(f"Laps analysed: {len(counted)}   Best: {fmt_lap(best)}")
        # total loss per corner across the session
        totals = {}
        for r in self.lap_records:
            if r.get("out_lap"):
                continue
            for c in r["corners"]:
                if c.get("off"):
                    continue
                totals.setdefault(c["num"], []).append(c)
        ranked = sorted(totals.items(),
                        key=lambda kv: -sum(x["loss"] for x in kv[1]) / len(kv[1]))
        lines.append("Corners ranked by average time loss vs your reference:")
        for num, lst in ranked:
            avg = sum(x["loss"] for x in lst) / len(lst)
            if avg >= float(self.cfg["min_loss_to_speak"]):
                cue = corner_cue(
                    lst[-1],
                    priority=guide_priority(getattr(self.profile, "guide", None),
                                            num),
                    guide=getattr(self.profile, "guide", None),
                    symptom_tips=bool(self.cfg.get("guide_symptom_tips", True))) or "no single fix stands out"
            else:
                cue = "no consistent leak"
            lines.append(f"  Turn {num}: avg loss {avg:+.2f}s over {len(lst)} laps -- {cue}")
        # theoretical best: best lap minus per-corner (zone time on best lap - best zone time)
        best_rec = min(self.lap_records, key=lambda r: r["time_s"])
        gain = 0.0
        for num, lst in totals.items():
            on_best = next(x["zone_time"] for x in best_rec["corners"] if x["num"] == num)
            best_zone = min(x["zone_time"] for x in lst)
            gain += max(0.0, on_best - best_zone)
        if gain > 0.05:
            lines.append(f"Theoretical best from your own zones: {fmt_lap(best - gain)} "
                         f"({gain:.1f}s sitting in corner consistency)")
        return lines


# --------------------------------------------------------------------------
# Session logging
# --------------------------------------------------------------------------

def make_logger():
    os.makedirs(LOGS_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    jsonl_path = os.path.join(LOGS_DIR, f"session_{stamp}.jsonl")
    txt_path = os.path.join(LOGS_DIR, f"debrief_{stamp}.txt")
    jf = open(jsonl_path, "a", encoding="utf-8")
    tf = open(txt_path, "a", encoding="utf-8")
    lock = threading.Lock()
    tf.write(f"AC Live Coach session {stamp}\n\n")

    def log(event, data):
        # The speech thread and the main thread both write here
        # (v2.5.3: serialize the writes -- interleaved lines could
        # corrupt a record mid-write).
        with lock:
            _log_locked(event, data)

    def _log_locked(event, data):
        rec = {"ts": time.strftime("%H:%M:%S"), "event": event}
        rec.update(data)
        jf.write(json.dumps(rec) + "\n")
        jf.flush()
        if event == "session":
            tf.write(f"Combo: {data.get('key')} | coach v{data.get('version')} "
                     f"| track {fmt_track_len(data.get('track_length_m'))}\n")
        elif event == "cue":
            tf.write(f"  >> {data['text']}\n")
        elif event == "lap":
            tf.write(f"Lap {data['lap']}: {fmt_lap(data['time_s'])} "
                     f"(corner losses total {data['total_corner_loss']:.2f}s)\n")
        elif event in ("reference_set", "reference_updated"):
            tf.write(f"[{event}] {fmt_lap(data['lap_s'])}\n")
        tf.flush()

    return log, txt_path, jsonl_path, tf, jf


# --------------------------------------------------------------------------
# Live mode
# --------------------------------------------------------------------------

def sanitize(s):
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in s).strip("_")


def compound_suffix(raw):
    """AC graphics-page tyre compound -> profile-key suffix (v2.6.0).
    'Soft (S)' -> 'tc_s'; 'Semislicks' -> 'tc_semislicks'; '' when AC
    reports nothing usable. A soft-tyre reference used to benchmark
    every medium-tyre lap as a loss (external review); the compound
    is part of what a lap was, so it is part of the profile key."""
    s = (raw or "").strip()
    if not s:
        return ""
    code = None
    if "(" in s and ")" in s:
        code = s[s.index("(") + 1:s.index(")")].strip()
    slug = sanitize(code if code else s).lower()
    if not slug or slug == "unknown":
        return ""
    return "tc_" + slug



def _trace_svg(p, session_best=None, optimal=None):
    """Speed & brake for three laps (v2.11.0) -- the chart Albert
    asked the report for: the best lap ever (the stored reference),
    the optimal lap stitched from the best zone segments, and the
    best lap of this session. Speed (mph) up top, brake below;
    corner markers come from the reference lap. Returns "" without
    a reference lap."""
    if p is None or not p.ref or not p.ref.get("spd"):
        return ""
    spd_kmh = p.ref["spd"]
    brk = p.ref.get("brk") or [0.0] * len(spd_kmh)
    gas = p.ref.get("gas") or [0.0] * len(spd_kmh)
    n = len(spd_kmh)
    bin_ft = (p.bin_m or 2.2) * FT_PER_M
    total_ft = n * bin_ft

    laps = [("ever", spd_kmh, brk, p.best_lap_s)]
    if optimal and len(optimal.get("spd") or []) == n:
        gain = 0.0
        zt = optimal.get("zone_times") or {}
        for c in (p.corners or []):
            if c.get("num") in zt:
                gain += max(0.0, zone_time(spd_kmh, c["zone_start"],
                                           c["zone_end"], p.bin_m)
                            - zt[c["num"]])
        theo = (p.best_lap_s - gain) if (p.best_lap_s and gain > 0.05) else None
        laps.append(("optimal", optimal["spd"], optimal.get("brk"), theo))
    if session_best and len(session_best.get("spd") or []) == n:
        laps.append(("session", session_best["spd"],
                     session_best.get("brk"), session_best.get("time")))

    all_mph = []
    for _k, s_, _b, _t in laps:
        all_mph.extend(v * MPH_PER_KMH for v in s_[::8])
    mph = [v * MPH_PER_KMH for v in spd_kmh]
    lo = max(0.0, min(all_mph) - 4)
    hi = max(all_mph) + 4
    W, L, R = 960, 46, 10
    ST, SB = 34, 214          # speed panel top/bottom
    PT, PB = 244, 312          # pedal panel top/bottom

    def X(b):
        return L + (W - L - R) * (b / max(1, n - 1))

    def Y(v):
        return SB - (SB - ST) * ((v - lo) / max(1.0, hi - lo))

    style = {"ever": ("#1a56db", "1.6", ""),
             "session": ("#e07b24", "1.4", ""),
             "optimal": ("#7b3fd4", "1.4", ' stroke-dasharray="5 3"')}
    parts = [f'<svg viewBox="0 0 {W} 344" width="100%" '
             f'xmlns="http://www.w3.org/2000/svg" role="img">']
    for frac in (0.0, 0.5, 1.0):
        v = lo + frac * (hi - lo)
        y = Y(v)
        parts.append(f'<line x1="{L}" y1="{y:.0f}" x2="{W - R}" y2="{y:.0f}" '
                     f'stroke="#e3e5e8"/>')
        parts.append(f'<text x="{L - 5}" y="{y + 4:.0f}" font-size="11" '
                     f'fill="#555" text-anchor="end">{v:.0f}</text>')
    parts.append(f'<text x="4" y="{(ST + SB) / 2:.0f}" font-size="11" fill="#555" '
                 f'transform="rotate(-90 12 {(ST + SB) / 2:.0f})" '
                 f'text-anchor="middle">mph</text>')
    for kind, s_, _b, _t in sorted(laps, key=lambda kv: kv[0] == "ever"):
        color, width, dash = style[kind]
        pts = " ".join(f"{X(b):.0f},{Y(s_[b] * MPH_PER_KMH):.0f}"
                       for b in range(0, n, 2))
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{color}" '
                     f'stroke-width="{width}"{dash}/>')
    for i, c in enumerate(p.corners or []):
        ab = c.get("apex_bin")
        if ab is None:
            continue
        ax = X(ab)
        parts.append(f'<line x1="{ax:.0f}" y1="{ST}" x2="{ax:.0f}" y2="{SB}" '
                     f'stroke="#9aa0a6" stroke-dasharray="3 3"/>')
        parts.append(f'<circle cx="{ax:.0f}" cy="{Y(mph[ab]):.0f}" r="3.4" fill="#111"/>')
        bb = c.get("brake_bin")
        if c.get("has_brake_zone") and bb is not None:
            bx, by = X(bb), Y(mph[bb])
            parts.append(f'<path d="M {bx - 5:.0f} {by - 9:.0f} h 10 l -5 9 z" fill="#d64545"/>')
        tb = c.get("throttle_bin")
        if tb is not None:
            tx, ty = X(tb), Y(mph[tb])
            parts.append(f'<path d="M {tx - 5:.0f} {ty + 9:.0f} h 10 l -5 -9 z" fill="#2e9e5b"/>')
        name = c.get("name") or f"Turn {c.get('num')}"
        ly = 14 if i % 2 == 0 else 27
        parts.append(f'<text x="{ax:.0f}" y="{ly}" font-size="10.5" fill="#333" '
                     f'text-anchor="middle">{int(c.get("num") or 0)} {html.escape(str(name))}</text>')
    # pedal panel: the reference throttle as context fill, then a
    # brake line per lap in its speed color.
    pts = " ".join(f"{X(b):.0f},{PB - (PB - PT) * max(0.0, min(1.0, gas[b])):.0f}"
                   for b in range(0, n, 4))
    parts.append(f'<polyline points="{L},{PB} {pts} {X(n - 1):.0f},{PB}" '
                 f'fill="#2e9e5b" opacity="0.35" stroke="none"/>')
    for kind, _s, b_, _t in laps:
        if not b_ or len(b_) != n:
            continue
        color, _w, dash = style[kind]
        pts = " ".join(f"{X(b):.0f},{PB - (PB - PT) * max(0.0, min(1.0, b_[b])):.0f}"
                       for b in range(0, n, 2))
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{color}" '
                     f'stroke-width="1.2"{dash}/>')
    parts.append(f'<line x1="{L}" y1="{PB}" x2="{W - R}" y2="{PB}" stroke="#c9ccd1"/>')
    parts.append(f'<text x="{L - 5}" y="{(PT + PB) / 2 + 4:.0f}" font-size="11" '
                 f'fill="#555" text-anchor="end">pedals</text>')
    tick = 0
    while tick <= total_ft:
        b = tick / bin_ft
        parts.append(f'<text x="{X(b):.0f}" y="334" font-size="11" fill="#555" '
                     f'text-anchor="middle">{tick:,.0f}</text>')
        tick += 2000
    parts.append(f'<text x="{(L + W - R) / 2:.0f}" y="344" font-size="0" fill="#555"></text>')
    parts.append("</svg>")
    labels = {"ever": "Best ever", "optimal": "Optimal (best zones)",
              "session": "Best this session"}
    leg = ['<p class="legend">']
    for kind, _s, _b, t_ in laps:
        color = style[kind][0]
        when = f" {fmt_lap(t_)}" if t_ else ""
        leg.append(f'<span style="color:{color}">&#9644; {labels[kind]}{when}</span>')
    leg.append('<span style="color:#d64545">&#9660; brake point</span>'
               '<span style="color:#111">&#9679; apex</span>'
               '<span style="color:#2e9e5b">&#9650; back to full throttle</span>'
               '<span>Pedals: lines brake, fill throttle (best ever)</span>'
               '<span>Distance in feet</span></p>')
    parts.append("".join(leg))
    return ("<h2>Speed &amp; brake — best ever · optimal · this session</h2>\n"
            + "\n".join(parts) + "\n")


def build_report_html(coach):
    """Post-session report: driven-line map with the coach's own turn
    numbers on it, plus the session's key coaching points. The turn
    numbers are the same ones the voice used all session -- that
    numbering match is the point of the page."""
    p = coach.profile
    esc = html.escape
    stats = []
    if p is not None:
        for c in p.corners:
            hist = coach.corner_history.get(c["num"], [])
            clean = [r for r in hist if not r.get("off")]
            avg = (sum(r["loss"] for r in clean) / len(clean)) if clean else None
            stats.append((c, avg, clean[-1] if clean else None))

    def loss_color(avg):
        if avg is None:
            return "#9aa0a6"
        if avg <= 0.05:
            return "#2e9e5b"
        if avg <= 0.25:
            return "#e0a100"
        return "#d64545"

    map_svg = ""
    if p is not None and p.outline and len(p.outline) >= 20:
        # Rendered as integrated. A v1.5 change negated x based on a
        # comparison against Albert's Dragy GPS outline -- but that
        # GPS trace itself renders mirror-flipped (Albert, Nov 1), so
        # the comparison was void and the flip is reverted. Whether
        # the map matches the real circuit's handedness is unsettled;
        # one look at a report next to the official track map settles
        # it.
        xs = [pt[0] for pt in p.outline]
        ys = [-pt[1] for pt in p.outline]
        minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
        w = max(maxx - minx, 1.0)
        h = max(maxy - miny, 1.0)
        scale = 860.0 / w
        if h * scale > 640.0:
            scale = 640.0 / h
        ox, oy = 70.0, 50.0

        def XY(x, y):
            return (ox + (x - minx) * scale, oy + (y - miny) * scale)

        pts_attr = " ".join(f"{XY(x, y)[0]:.0f},{XY(x, y)[1]:.0f}"
                            for x, y in zip(xs, ys))
        parts = [f'<svg viewBox="0 0 {w * scale + 140:.0f} {h * scale + 100:.0f}" '
                 f'xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto">']
        parts.append(f'<polygon points="{pts_attr}" fill="rgba(30,100,200,0.05)" '
                     f'stroke="#20508f" stroke-width="3" stroke-linejoin="round"/>')
        sx, sy = XY(xs[0], ys[0])
        parts.append(f'<rect x="{sx - 6:.0f}" y="{sy - 6:.0f}" width="12" height="12" '
                     f'fill="#111"/><text x="{sx + 12:.0f}" y="{sy + 4:.0f}" '
                     f'font-size="13" font-weight="bold">S/F</text>')
        n = len(p.outline)
        for c, avg, _latest in stats:
            idx = int((c["apex_bin"] / NBINS) * (n - 1))
            mx, my = XY(xs[idx], ys[idx])
            col = loss_color(avg)
            parts.append(
                f'<circle cx="{mx:.0f}" cy="{my:.0f}" r="13" fill="{col}" '
                f'stroke="#fff" stroke-width="2.5"/>'
                f'<text x="{mx:.0f}" y="{my + 5:.0f}" font-size="14" '
                f'font-weight="bold" fill="#fff" text-anchor="middle">{int(c["num"])}</text>')
        parts.append("</svg>")
        map_svg = "\n".join(parts)
    else:
        map_svg = "<p><i>No driven-line map captured this session.</i></p>"

    def corner_label(c):
        name = c.get("name") or f"Turn {c['num']}"
        if name.startswith("Turn"):
            return name
        return f"{name} (Turn {c['num']})"

    ranked = sorted((s for s in stats if s[1] is not None and s[1] > 0.05),
                    key=lambda s: -s[1])
    points_html = ""
    if ranked:
        items = []
        for c, avg, latest in ranked[:5]:
            cue = corner_cue(
                latest,
                priority=guide_priority(p.guide, c["num"]),
                guide=p.guide,
                symptom_tips=bool(coach.cfg.get("guide_symptom_tips", True))) if latest else None
            fix = cue.split(": ", 1)[1] if cue and ": " in cue else "see data below"
            items.append(
                f"<li><b>{esc(corner_label(c))}</b> — losing <b>{avg:+.2f}s</b> per lap "
                f"on average. Fix: <b>{esc(fix)}</b>.</li>")
        points_html = "<ol>" + "\n".join(items) + "</ol>"
    else:
        points_html = "<p>No corner lost more than 0.05s on average — clean session.</p>"

    spreads = {}
    for c in (p.corners or []):
        vals = [r["brake_delta_m"]
                for r in coach.corner_history.get(c["num"], [])
                if r.get("brake_delta_m") is not None]
        if len(vals) >= 2:
            spreads[c["num"]] = (max(vals) - min(vals)) * FT_PER_M
    rows = []
    for c, avg, latest in stats:
        if latest is None:
            rows.append(f"<tr><td>{esc(corner_label(c))}</td><td>—</td><td>—</td>"
                        f"<td>—</td><td>—</td><td>—</td></tr>")
        else:
            bd = latest["brake_delta_m"]
            bd_s = f"{bd * FT_PER_M:+.0f}" if bd is not None else "—"
            sp = spreads.get(c["num"])
            sp_s = f"{sp:.0f}" if sp is not None else "—"
            rows.append(
                f"<tr><td><b>{esc(corner_label(c))}</b></td>"
                f"<td style='color:{loss_color(avg)}'><b>{avg:+.2f}s</b></td>"
                f"<td>{bd_s}</td><td>{sp_s}</td><td>{latest['apex_delta_kmh'] * MPH_PER_KMH:+.0f}</td>"
                f"<td>{latest['coast_s']:.1f}s</td></tr>")
    table_html = ("<table><tr><th>Corner</th><th>Avg loss</th><th>Brake Δ (last lap, ft)</th>"
                  "<th>Brake spread (ft)</th><th>Apex Δ (mph)</th><th>Coast</th></tr>" + "\n".join(rows) + "</table>")

    # v2.3: track-guide section -- the human coach's notes paired
    # with the corner data above, when a guide exists for this track.
    guide_html = ""
    g = getattr(p, "guide", None) if p else None
    if g:
        blocks = []
        for c in (p.corners or []):
            z = guide_zone(g, c["num"])
            if not z:
                continue
            tips = z.get("tips") or []
            char = z.get("character") or ""
            if not tips and not char:
                continue
            pri = z.get("priority")
            pri_s = f" \u00b7 priority: {esc(pri)}" if pri else ""
            tip_lis = "".join(f"<li>{esc(tip_text(t))}</li>" for t in tips)
            blocks.append(
                f"<p><b>{esc(corner_label(c))}</b>{pri_s}</p>"
                + (f"<p>{esc(char)}</p>" if char else "")
                + (f"<ul>{tip_lis}</ul>" if tip_lis else ""))
        for u in g.get("unmatched") or []:
            tip_lis = "".join(f"<li>{esc(tip_text(t))}</li>" for t in (u.get("tips") or []))
            blocks.append(
                f"<p><b>{esc(u.get('corner', 'Unmatched corner'))}</b> "
                f"<i>(not a separate zone on your map \u2014 "
                f"{esc(u.get('between', ''))})</i></p>"
                + (f"<ul>{tip_lis}</ul>" if tip_lis else ""))
        notes = g.get("layout_notes") or []
        if notes:
            blocks.append("<p><b>Layout notes (from the source)</b></p><ul>"
                          + "".join(f"<li>{esc(n)}</li>" for n in notes) + "</ul>")
        if blocks:
            srcmeta = g.get("source") or {}
            src_line = ""
            if srcmeta.get("title"):
                src_line = (f"<p class='sub'>From: {esc(srcmeta.get('title', ''))}"
                            f" \u2014 {esc(srcmeta.get('channel', ''))}</p>")
            guide_html = "<h2>Track guide</h2>" + src_line + "\n".join(blocks) + "\n"

    trace_html = _trace_svg(p, getattr(coach, "_session_best", None),
                            getattr(coach, "_optimal", None))
    summary_txt = "\n".join(coach.session_summary_lines())
    best = fmt_lap(p.best_lap_s) if p and p.best_lap_s else "—"
    track = esc(p.key) if p else "unknown"
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta http-equiv="refresh" content="20"><title>AC Live Coach — Session Report</title>
<style>
body {{ font-family: 'Segoe UI', Arial, sans-serif; margin: 24px auto; max-width: 980px;
       padding: 0 16px; color: #16181d; background: #fff; }}
h1 {{ font-size: 24px; margin-bottom: 2px; }} h2 {{ font-size: 18px; margin-top: 28px; }}
.sub {{ color: #555; margin-top: 0; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 8px; }}
th, td {{ border-bottom: 1px solid #ddd; padding: 6px 8px; text-align: left; font-size: 15px; }}
pre {{ background: #f4f5f7; padding: 12px; border-radius: 6px; white-space: pre-wrap; }}
.legend span {{ margin-right: 16px; font-size: 13px; }}
.dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 5px; }}
</style></head><body>
<h1>Session Report</h1>
<p class="sub">{track} &middot; Best lap <b>{best}</b> &middot; {len(coach.lap_records)} laps analysed
&middot; Turn numbers match the voice cues. &middot; AC Live Coach v{VERSION}</p>
{map_svg}
<p class="legend"><span><span class="dot" style="background:#2e9e5b"></span>≤0.05s lost</span>
<span><span class="dot" style="background:#e0a100"></span>0.05–0.25s</span>
<span><span class="dot" style="background:#d64545"></span>&gt;0.25s</span>
<span><span class="dot" style="background:#9aa0a6"></span>no data yet</span></p>
<h2>Key coaching points</h2>
{points_html}
<h2>Corner data (averages vs your best lap)</h2>
{table_html}
{trace_html}{guide_html}<h2>Session summary</h2>
<pre>{esc(summary_txt)}</pre>
<p class="sub">Map drawn from your own driven line (heading + distance integration).
Corners where you left the track (2+ tyres off) are excluded from the averages —
the coach doesn't count a save as a technique problem.</p>
</body></html>"""


def run_live(cfg):
    log, txt_path, jsonl_path, tf, jf = make_logger()
    speaker = Speaker(cfg, log)
    coach = Coach(cfg, speaker, log)
    if speaker.enabled:
        # Load/download the neural voice now, in the pits -- not on
        # the first corner cue (v2.2).
        speaker.warm()
        # Speak a check line at launch so a dead voice is discovered
        # while still in the pits, not three laps in.
        speaker.say("Voice check. Coach is live.")
    print(f"AC Live Coach v{VERSION}")
    resolve_tracks_dir(cfg)
    print(f"Session log: {jsonl_path}")
    print(f"Debrief:     {txt_path}")
    print("Waiting for Assetto Corsa... (telemetry goes live once you're "
          "on track; menus publish nothing)")

    report_path = os.path.join(
        os.path.dirname(txt_path),
        os.path.basename(txt_path).replace("debrief_", "report_").replace(".txt", ".html"))
    report_state = {"laps": -1, "opened": False}

    def open_report_once():
        try:
            os.startfile(report_path)  # Windows-native open
        except Exception:
            try:
                webbrowser.open("file:///" + report_path.replace(os.sep, "/"))
            except Exception:
                pass
        report_state["opened"] = True
        print("[report] opened in your browser — the page refreshes "
              "itself every 20 seconds as new laps land")

    def write_report():
        # The report file is rewritten after every finalized lap, not
        # just at exit: v1.4 only built it in the shutdown path, so a
        # session ended by closing the console window (no Ctrl+C)
        # produced no report and no window at all. v1.6.1 goes further
        # and OPENS it as soon as the reference lap exists -- Albert's
        # Nov 1 session debrief had no shutdown summary in it, proving
        # the exit path never runs when the window is closed, so the
        # popup can no longer depend on exit at all. The page carries
        # a 20 s auto-refresh, so the early-opened tab stays current.
        try:
            with open(report_path, "w", encoding="utf-8") as rf:
                rf.write(build_report_html(coach))
            if report_state["laps"] < 0:
                print(f"[report] session report started: {report_path}")
            report_state["laps"] = len(coach.lap_records)
            if (cfg.get("open_report_window", True)
                    and not report_state["opened"]
                    and coach.profile is not None
                    and coach.profile.ref is not None):
                open_report_once()
        except Exception as e:
            print(f"[report] could not write the session report ({e})")

    tele = None
    current_key = None
    cur_compound = ""
    cur_radii = [0.33, 0.33, 0.33, 0.33]
    frame_no = 0
    hint_warned = False
    last_static_check = 0.0
    loop_errors = 0
    last_loop_error_log = 0.0
    try:
        while True:
            if tele is None:
                try:
                    tele = ACTelemetry()
                    st = tele.static()
                    if not st.track:
                        tele = None
                        time.sleep(2)
                        continue
                    print(f"Connected. Track: {st.track}  Car: {st.carModel}")
                except Exception:
                    time.sleep(2)
                    continue
            try:
                phys = tele.physics()
                gfx = tele.graphics()
            except Exception:
                time.sleep(0.05)
                continue
            cur_compound = (gfx.tyreCompound or "").strip()
            try:
                now = time.time()
                if now - last_static_check > 2.0:
                    last_static_check = now
                    st = tele.static()
                    cur_radii = [float(x) for x in st.tyreRadius]
                    base_key = sanitize(f"{st.track}__{st.carModel}")
                    suf = compound_suffix(cur_compound)
                    key = base_key + (f"__{suf}" if suf else "")
                    if (not suf and current_key
                            and current_key.startswith(base_key + "__tc_")):
                        # v2.6.1: an EMPTY compound read is no
                        # information, not a new compound -- AC clears
                        # the field when a session ends (seen in the
                        # Nov 2 15:57 log, which stamped a fresh
                        # no-compound profile on exit). Keep the
                        # compound key; a change to a DIFFERENT
                        # non-empty compound still switches profiles.
                        key = current_key
                    if key != current_key:
                        current_key = key
                        profile = TrackProfile(key)
                        if profile.load():
                            print(f"[profile] loaded saved reference for {key}")
                        elif key != base_key:
                            # v2.6.0 adoption: the first run under the
                            # tyre-aware key takes over the pre-existing
                            # profile (and its history file) instead of
                            # orphaning Albert's references. The legacy
                            # file is removed, so a genuinely different
                            # compound later still starts fresh.
                            legacy = TrackProfile(base_key)
                            if legacy.load():
                                hp_old = legacy.history_path()
                                legacy.key = key
                                legacy.path = os.path.join(
                                    TRACKS_DIR, key + ".json")
                                legacy.save()
                                try:
                                    os.remove(os.path.join(
                                        TRACKS_DIR, base_key + ".json"))
                                except Exception:
                                    pass
                                hp_new = legacy.history_path()
                                if os.path.exists(hp_old) and hp_old != hp_new:
                                    try:
                                        os.replace(hp_old, hp_new)
                                    except Exception:
                                        pass
                                profile = legacy
                                print(f"[profile] adopted your saved "
                                      f"{base_key} reference under the "
                                      f"tyre-aware key {key}")
                        if cfg.get("_remap") and (profile.ref is not None or profile.corners):
                            # One-shot --remap: forget this car/track's
                            # reference and map (plus the shared track
                            # map) so the first full lap rebuilds them
                            # with the finer v2.0 detector. The old file
                            # is overwritten when the new reference lands.
                            profile.ref = None
                            profile.best_lap_s = None
                            profile.corners = []
                            try:
                                mp = profile.track_map_path()
                                if os.path.exists(mp):
                                    os.remove(mp)
                            except Exception:
                                pass
                            print("[coach] --remap: saved reference cleared; "
                                  "the first full lap builds a fresh, finer map")
                        coach.set_profile(profile)
                        if cfg["expected_track_hint"] not in st.track.lower() and not hint_warned:
                            hint_warned = True
                            print(f"[note] this build was tuned for Buttonwillow 13CW; "
                                  f"you're on '{st.track}' -- coaching works on any track, "
                                  f"it just learns this one from your laps too.")
                coach.observe_official(gfx.completedLaps, gfx.iLastTime, now)
                wheel_w = [float(x) for x in phys.wheelAngularSpeed]
                coach.feed(now, gfx.normalizedCarPosition, phys.speedKmh,
                           phys.brake, phys.gas, gfx.distanceTraveled,
                           live=(gfx.status == AC_LIVE), ict_ms=gfx.iCurrentTime,
                           heading=phys.heading,
                           tyres_out=phys.numberOfTyresOut,
                           steer=phys.steerAngle,
                           wheel_w=wheel_w, wheel_r=cur_radii,
                           flag=gfx.flag, penalty_s=gfx.penaltyTime)
                frame_no += 1
                if cfg.get("record_frames", True):
                    # Record-and-replay: raw frames in the session
                    # log, so detectors can be re-run (and regression-
                    # tested) on real laps later with --replay instead
                    # of trusting synthetic ones. v2.8.0: recorded at
                    # the FULL acquisition rate -- at the original
                    # 10 Hz the shortest events the coach analyzes
                    # (0.18 s lockups) were two samples wide.
                    coach.log("frame", {
                        "t": round(now, 3),
                        "pos": round(gfx.normalizedCarPosition, 5),
                        "spd": round(phys.speedKmh, 1),
                        "brk": round(phys.brake, 3), "gas": round(phys.gas, 3),
                        "str": round(phys.steerAngle, 3),
                        "dist": round(gfx.distanceTraveled, 1),
                        "ict": gfx.iCurrentTime,
                        "out": phys.numberOfTyresOut,
                        "ww": [round(x, 1) for x in wheel_w],
                        "wr": [round(x, 3) for x in cur_radii],
                        "flag": gfx.flag, "pen": round(gfx.penaltyTime, 2),
                        "hdg": round(phys.heading, 4)})
                if (len(coach.lap_records) != report_state["laps"]
                        or (not report_state["opened"]
                            and coach.profile is not None
                            and coach.profile.ref is not None)):
                    write_report()
                time.sleep(1 / 30.0)
            except Exception as e:
                # One bad frame must not end the session (v2.5.3
                # review fix): a NaN position, a malformed profile
                # field, an exhausted iterator -- log it, skip the
                # frame, keep coaching.
                loop_errors += 1
                if time.time() - last_loop_error_log > 5.0:
                    last_loop_error_log = time.time()
                    print(f"[coach] telemetry loop error ({e}); continuing")
                    try:
                        log("loop_error", {"error": str(e),
                                           "count": loop_errors})
                    except Exception:
                        pass
                time.sleep(0.1)
    except KeyboardInterrupt:
        print("\nStopping coach...")
    finally:
        try:
            coach.flush()
        except Exception:
            pass
        lines = coach.session_summary_lines()
        tf.write("\nSession summary\n")
        for ln in lines:
            tf.write(ln + "\n")
            print(ln)
        tf.close()
        jf.close()
        print(f"Debrief saved: {txt_path}")
        try:
            write_report()
            print(f"Report: {report_path}")
            if cfg.get("open_report_window", True) and not report_state["opened"]:
                open_report_once()
        except Exception as e:
            print(f"[report] could not build the session report ({e})")


# --------------------------------------------------------------------------
# Simulate mode: proves the coaching brain without the game.
# A synthetic 4,780 m track, 9 corners. Lap 1 is clean and becomes the
# reference. Later laps carry three planted faults:
#   corner 3 -- brakes 25 m early, apex 8 km/h slow
#   corner 5 -- gets back to power late
#   corner 2 -- coasts through the zone (long, wide dip)
# --------------------------------------------------------------------------

SIM_TRACK_M = 4780.0
SIM_CORNERS = [  # (center_m, min_kmh, width_m)
    (350, 95, 90), (800, 140, 105), (1250, 80, 85), (1750, 120, 100),
    (2250, 165, 130), (2750, 90, 90), (3300, 130, 105), (3850, 105, 95),
    (4400, 150, 120),
]
SIM_VMAX = 208.0


def _circ_d(s, c, L):
    d = abs(s - c)
    return min(d, L - d)


def sim_speed(s, lap):
    L = SIM_TRACK_M
    v = SIM_VMAX
    for idx, (c, vmin, w) in enumerate(SIM_CORNERS):
        cc, vv, ww = c, vmin, w
        if lap >= 2:
            if idx == 2:      # corner 3 fault: earlier, slower dip
                cc, vv = c - 25, vmin - 8
            if idx == 1:      # corner 2 fault: wide dip (coasting)
                ww = w * 1.5
        prof = vv + (SIM_VMAX - vv) * (1 - math.exp(-((_circ_d(s, cc, L) / ww) ** 2)))
        v = min(v, prof)
    if lap >= 2:
        # corner 5 fault: lazy recovery after the apex
        c, vmin, _w = SIM_CORNERS[4]
        d = (s - c) % L
        if 0 <= d <= 240:
            base = vmin + (SIM_VMAX - vmin) * (1 - math.exp(-((d / 130.0) ** 2)))
            lazy = vmin + (base - vmin) * 0.70
            v = min(v, lazy)
        v *= 0.997
    return v


def sim_pedals(s, v, lap):
    # derive pedals from the speed gradient, like the real traces behave
    dv = sim_speed(s + 3.0, lap) - sim_speed(s - 3.0, lap)
    dvdt = dv / 6.0 * (v / 3.6)  # km/h per second, roughly
    brake = max(0.0, min(1.0, -dvdt / 22.0)) if dvdt < -0.5 else 0.0
    gas = 1.0 if dvdt > 0.3 else (0.0 if dvdt < -0.5 else 0.85)
    if lap >= 2:
        c2 = SIM_CORNERS[1][0]
        if abs(_circ_d(s, c2, SIM_TRACK_M)) < 130 and _circ_d(s, c2, SIM_TRACK_M) == abs(s - c2) % SIM_TRACK_M:
            if c2 - 40 <= s <= c2 + 120:  # planted coast window, corner 2
                brake, gas = 0.0, 0.0
    return brake, gas


def run_replay(cfg, path):
    """Re-run a recorded session's frames through the coaching brain
    (v2.7.0):  python ac_live_coach.py --replay logs/session_....jsonl

    The frames a live session records (10 Hz, see record_frames) are
    fed back through feed() against a throwaway profile in a temp
    folder -- Albert's real profiles are never touched -- and every
    spoken line and lap is printed. New detectors get validated on
    REAL laps here before they ever coach live."""
    global TRACKS_DIR
    import tempfile
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    frames = [r for r in rows if r.get("event") == "frame"]
    if not frames:
        print(f"[replay] no frame rows in {path} -- the session was "
              f"recorded before v2.7.0 (or record_frames was off)")
        return None
    key = next((r["key"] for r in rows if r.get("event") == "session"),
               "replay__car")
    TRACKS_DIR = tempfile.mkdtemp(prefix="coach_replay_")

    class ReplaySpeaker:
        def __init__(self):
            self.lines = []

        def say(self, text, **kw):
            self.lines.append(text)
            print(f"  [say] {text}")

        def tone(self):
            self.lines.append("<brake tone>")
            print("  [tone]")

        def tone_ready(self):
            return True

    speaker = ReplaySpeaker()
    coach = Coach(cfg, speaker, lambda *a, **k: None)
    prof = TrackProfile(key + "__replay")
    coach.set_profile(prof)
    for fr in frames:
        coach.feed(fr["t"], fr["pos"], fr["spd"], fr["brk"], fr["gas"],
                   fr["dist"], live=True, ict_ms=fr.get("ict"),
                   heading=fr.get("hdg"), tyres_out=fr.get("out"),
                   steer=fr.get("str"), wheel_w=fr.get("ww"),
                   wheel_r=fr.get("wr"), flag=fr.get("flag"),
                   penalty_s=fr.get("pen"))
    if frames:
        coach._pump_finishes(frames[-1]["t"] + 10.0)
    print(f"[replay] {len(frames)} frames, {len(coach.lap_records)} laps, "
          f"best {fmt_lap(prof.best_lap_s) if prof.best_lap_s else '--'}")
    return coach


def run_simulate(cfg):
    print("=== SIMULATE MODE (no game; synthetic Buttonwillow-like track) ===")
    log, txt_path, jsonl_path, tf, jf = make_logger()
    speaker = Speaker({**cfg, "voice_enabled": False}, log)
    coach = Coach(cfg, speaker, log)
    profile = TrackProfile("simulate__test")
    coach.set_profile(profile)
    dt = 0.05
    t = 1000.0
    s_total = 0.0
    for lap in (1, 2, 3, 4):
        s = 0.0
        while s < SIM_TRACK_M:
            v = sim_speed(s, lap)
            brake, gas = sim_pedals(s, v, lap)
            coach.feed(t, (s_total % SIM_TRACK_M) / SIM_TRACK_M, v, brake, gas, s_total, live=True)
            step = (v / 3.6) * dt
            s += step
            s_total += step
            t += dt
        # cross the line
        coach.feed(t, 0.001, sim_speed(0, lap), 0.0, 0.6, s_total, live=True)
    coach.flush()
    print("\n=== SESSION SUMMARY ===")
    for ln in coach.session_summary_lines():
        print(ln)
        tf.write(ln + "\n")
    tf.close()
    jf.close()
    print(f"\nSim debrief: {txt_path}")


def list_voices():
    try:
        import pyttsx3
        engine = pyttsx3.init()
        voices = engine.getProperty("voices")
        print('Installed voices (put any part of a name as "voice" '
              'in coach_config.json):')
        for v in voices:
            print(f"  - {v.name}")
        engine.stop()
    except Exception as e:
        print(f"[voice] could not list voices ({e})")


def main():
    cfg = load_config()
    if "--remap" in sys.argv:
        cfg["_remap"] = True
    if "--no-voice" in sys.argv:
        cfg["voice_enabled"] = False
    if "--list-voices" in sys.argv:
        list_voices()
        return
    if "--replay" in sys.argv:
        i = sys.argv.index("--replay")
        if i + 1 < len(sys.argv):
            run_replay(cfg, sys.argv[i + 1])
        else:
            print("usage: ac_live_coach.py --replay <session.jsonl>")
    elif "--simulate" in sys.argv:
        run_simulate(cfg)
    else:
        run_live(cfg)


if __name__ == "__main__":
    main()
