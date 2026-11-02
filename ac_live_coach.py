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
import html
import json
import math
import os
import queue
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
                txt = open(vdf, encoding="utf-8", errors="ignore").read()
                for m in re.finditer(r'"path"\s*"([^"]+)"', txt):
                    lib = m.group(1).replace("\\\\", "\\")
                    cands.append(os.path.join(
                        lib, "steamapps", "common", "assettocorsa"))
        except Exception:
            pass
    return cands


def _writable_dir(path):
    try:
        os.makedirs(path, exist_ok=True)
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


def resolve_tracks_dir(cfg):
    """Profiles must survive coach updates, so from v1.7 they live
    OUTSIDE the app folder -- in the Assetto Corsa folder itself
    (Albert's choice, Nov 1; through Oct he kept losing profiles
    because each version was unzipped into a fresh folder and the
    profiles lived in the old one). Resolution order: the
    'profile_dir' config setting (the exact folder for the JSONs),
    then <AC install>\\ACLiveCoach\\tracks, then the Documents
    Assetto Corsa folder, then the legacy app folder. The first
    writable candidate wins and legacy profiles are migrated once.
    """
    global TRACKS_DIR
    legacy = os.path.join(APP_DIR, "tracks")
    candidates = []
    pd = (cfg or {}).get("profile_dir") or os.environ.get("ACLIVECOACH_PROFILE_DIR")
    if pd:
        candidates.append(("profile_dir setting", pd))
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
        if _writable_dir(path):
            TRACKS_DIR = path
            moved = _migrate_profiles(legacy, path)
            # v1.7 put them in ACLiveCoach\tracks for one build; if
            # that folder exists next to the new profiles\ home,
            # migrate from it too.
            if os.path.basename(path) == "profiles":
                moved += _migrate_profiles(
                    os.path.join(os.path.dirname(path), "tracks"), path)
            msg = f"[profiles] storing profiles in: {path} ({label})"
            if moved:
                msg += f" -- migrated {moved} profile file(s)"
            print(msg)
            return path
    TRACKS_DIR = legacy
    print(f"[profiles] storing profiles in: {legacy} (fallback)")
    return legacy

NBINS = 2000  # position bins around the lap (about 2 m per bin at Buttonwillow)

VERSION = "2.4.1"

DEFAULT_CONFIG = {
    "voice_enabled": True,
    "voice_engine": "piper",      # v2.2: "piper" = offline neural voice; "sapi" = built-in Windows voice
    "piper_voice": "en_US-lessac-medium",  # any piper-voices name; downloaded once into voices/
    "piper_length_scale": 1.0,    # piper speech rate: <1 faster, >1 slower
    "piper_dir": "",              # voice model folder; empty = <app dir>/voices
    "voice_rate": 178,            # words per minute (SAPI engine)
    "voice": "",                  # SAPI voice name fragment ("zira", "david"); empty = system default
    "approach_distance_m": 260,   # how far before the brake zone the cue fires
    "min_loss_to_speak": 0.15,    # seconds; smaller losses stay silent
    "exit_min_loss_s": 0.2,       # in-corner loss needed for instant feedback
    "off_min_duration_s": 0.35,   # shortest 3+ tyres-off excursion that excuses a corner
    "off_min_duration_2tyres_s": 0.9,  # a 2-tyre excursion must last this long to excuse
    "off_repeat_laps": 3,         # same corner excused this many laps running -> it counts as line
    "out_lap_briefing": True,     # explain the out-lap rules (and best use of the lap) once per session
    "profile_dir": "",            # exact folder for profile JSONs; empty = auto (Assetto Corsa folder)
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


class Speaker:
    def __init__(self, cfg, log_fn):
        self.cfg = cfg
        self.log = log_fn
        self.q = queue.Queue()
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
            vdir = self._voices_dir()
            model = os.path.join(vdir, name + ".onnx")
            conf = model + ".json"
            if not (os.path.exists(model) and os.path.exists(conf)):
                onnx_url, json_url = _piper_urls(name)
                print(f"[voice] downloading the piper voice '{name}' "
                      f"(~63 MB, one time only)...")
                self._download(json_url, conf)
                self._download(onnx_url, model)
                print("[voice] voice downloaded; it works offline from here on")
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
            data = self._piper_render(text)
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
            ts, text = item
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
                self._speak(text)
            except Exception:
                pass

    def say(self, text):
        self.log("cue", {"text": text})
        print(f"  >> COACH: {text}")
        if self.enabled:
            self.q.put((time.time(), text))


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
    for i in range(z0, z1 + 1):
        if lap_brk[i] < 0.05 and lap_gas[i] < 0.05 and lap_spd[i] > 40:
            coast_s += bin_m / max(lap_spd[i] / 3.6, 3.0)
    out = {"zone_time": t_lap, "ref_zone_time": t_ref,
           "loss": t_lap - t_ref,
           "brake_delta_m": brake_delta_m,
           "apex_delta_kmh": lap_apex_v - ref_apex_v,
           "throttle_delta_m": throttle_delta_m,
           "coast_s": coast_s,
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
                lap_str=None):
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
    return results


def _cue_candidates(s):
    """Every applicable fix for one span's stats, in the coach's
    standing priority order, as (kind, text) pairs. _cue_text picks
    from this list; a track guide's per-corner priority (v2.3) can
    re-rank the pick without changing what's applicable."""
    name = s["name"]
    out = []
    if s.get("brake_delta_m") is not None and s["brake_delta_m"] <= -8:
        out.append(("brake", f"{name}: brake later"))
    if s["coast_s"] >= 0.5:
        out.append(("coast", f"{name}: less coasting, commit to a pedal"))
    if s.get("apex_shift_m") is not None and abs(s["apex_shift_m"]) >= 25:
        if s["apex_shift_m"] > 0:
            out.append(("apex_late", f"{name}: apex is late there \u2014 turn in earlier"))
        else:
            out.append(("apex_early", f"{name}: early apex \u2014 delay the turn-in, don't pinch the exit"))
    if s.get("mid_lift"):
        out.append(("lift", f"{name}: you're lifting mid-corner \u2014 commit, or carry less in"))
    if s["apex_delta_kmh"] <= -5:
        out.append(("speed", f"{name}: carry more speed in"))
    if s.get("throttle_delta_m") is not None and s["throttle_delta_m"] >= 10:
        out.append(("power", f"{name}: get to power earlier"))
    if ((s.get("steer_rev_delta") or 0) >= 2
            and (s.get("steer_work_ratio") or 1) >= 1.3):
        out.append(("hands", f"{name}: busy hands there \u2014 one clean steering input"))
    if s.get("brake_delta_m") is not None and s["brake_delta_m"] >= 10:
        out.append(("brake", f"{name}: you're braking deep, watch the entry"))
    return out


def _pick_candidate(s, priority=None):
    """The winning (kind, text) fix for one span's stats, or None.
    v2.3: a track guide can declare what matters at a corner --
    priority "exit" elevates power/speed fixes, "entry" elevates
    braking/coast fixes; anything else keeps the standing order."""
    cands = _cue_candidates(s)
    if not cands:
        return None
    if priority == "exit":
        for kind, txt in cands:
            if kind in ("power", "speed"):
                return kind, txt
    elif priority == "entry":
        for kind, txt in cands:
            if kind in ("brake", "coast"):
                return kind, txt
    return cands[0]


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
                return True
            except Exception as e:
                print(f"[profile] could not load {self.path} ({e}); starting fresh")
        return False

    def track_map_path(self):
        """Per-TRACK corner map shared across cars: tracks/<track>__map.json."""
        track = self.key.rsplit("__", 1)[0]
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
        }
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(d, f)
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
                    with open(mp, "w", encoding="utf-8") as f:
                        json.dump({"corners": self.corners, "path": self.outline,
                                   "track_length_m": self.track_length_m,
                                   "source": self.key}, f)
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
    for base in (TRACKS_DIR, os.path.join(APP_DIR, "guides")):
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
        self.history = None        # v2.1.0: cross-session per-combo history
        self._live_losses = {}     # v2.1.0: this lap's live per-zone results (section feedback)
        self.lap_records = []      # per-lap summaries for the debrief
        self.approach_fired = set()
        self.exit_fired = set()
        self.approach_cues_this_lap = 0
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
        track = p.key.rsplit("__", 1)[0]
        path = os.path.join(TRACKS_DIR, track + "__names.json")
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    mapping = json.load(f)
                applied = 0
                for c in corners:
                    nm = mapping.get(str(c["num"]))
                    if nm:
                        c["name"] = str(nm)
                        applied += 1
                if applied:
                    print(f"[coach] names file applied: {applied} zone "
                          f"names from {os.path.basename(path)}")
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
        profile.guide = _load_guide(profile.key.rsplit("__", 1)[0])
        self._guide_tipped = set()
        if profile.guide:
            print(f"[coach] track guide loaded: {profile.key.rsplit('__', 1)[0]}")
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
            with open(self.profile.history_path(), "w", encoding="utf-8") as f:
                json.dump(self.history, f)
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
                    # pairing for the rest of the session.
                    while len(self.official_times) > len(self.pending_finishes) + 1:
                        self.official_times.pop(0)
                self._official_watch = None
                self._pump_finishes(t)

    def reset_lap(self):
        self.bins = LapBins()
        self.lap_valid_time = True
        self._off_events = []        # closed 2+ tyres-off excursions this lap
        self._off_cur = None         # excursion in progress, if any
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
    BRIEFING_LOADED_A = "Out lap — I don't coach this one, and nothing on it counts."
    BRIEFING_FRESH_A = (
        "First lap in this car here sets your reference — every "
        "later lap gets compared to it.")
    BRIEF_OFFSETS_S = (0.0, 14.0)

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
            parts.append(f"Your best here in this car is "
                         f"{fmt_lap_spoken(p.best_lap_s)}.")
            if n:
                parts.append(f"{n} places mapped.")
        elif self.map_borrowed_from:
            src = self._pretty_key_part(self.map_borrowed_from, 1)
            parts.append(f"Corner map borrowed from the {src}: "
                         f"{n} places. This lap sets this car's reference.")
        else:
            parts.append("No laps logged in this car here yet — "
                         "this lap sets the reference.")
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
            parts.append(f"This session, your biggest leaks are {names}.")
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
                parts.append(f"Over {sess} sessions in this car here, "
                             f"your biggest leaks are {names}.")
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
        a = self.BRIEFING_LOADED_A if self._loaded_with_ref else self.BRIEFING_FRESH_A
        stats = self._stats_line()
        segs = [a] + ([stats] if stats else [])
        self._brief_plan = list(zip(self.BRIEF_OFFSETS_S, segs))[1:]
        self.speaker.say(a)

    def feed(self, t, pos, speed_kmh, brake, gas, dist_m, live=True, ict_ms=None,
             heading=None, tyres_out=None, steer=None):
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
        self.pending_finishes.append({
            "t_finish": t, "dur_s": dur, "dist_m": dist, "skip": not valid,
            "ict_peak_s": (self._ict_peak_ms / 1000.0) if self._ict_peak_ms else None,
            "spd": spd, "brk": brk, "gas": gas, "hdg": hdg, "str": steer,
            "path_pts": path_pts,
            "off_events": list(self._off_events),
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
                                           "track_length_m": p.track_length_m})
                self.speaker.say(f"Reference lap in, {fmt_lap_spoken(lap_time)}. "
                                 f"{len(p.corners)} corners mapped. Coaching starts next lap.")
            else:
                results = analyze_lap(spd, brk, gas, p.ref, p.corners, p.bin_m,
                                      off_events=rec.get("off_events"),
                                      off_min_s=float(self.cfg.get("off_min_duration_s", 0.35)),
                                      off_2_min_s=float(self.cfg.get("off_min_duration_2tyres_s", 0.9)),
                                      lap_str=rec.get("str"))
                # An excused corner is a save -- once. The same corner
                # excused off_repeat_laps times running is a pattern,
                # not an accident: from then on it counts as line and
                # gets coached like any other corner (Albert: the
                # summaries "repeat events from other laps", Nov 1).
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
                if lap_time < (p.best_lap_s or 1e9):
                    p.ref = {"spd": spd, "brk": brk, "gas": gas,
                         "hdg": rec.get("hdg"), "str": rec.get("str")}
                    p.best_lap_s = lap_time
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
            cmap = {c["num"]: c for c in self.profile.corners} if self.profile else {}
            if cmap.get(num, {}).get("minor"):
                # minor zones speak only on a real loss (v2.0)
                qualifies = (
                    avg >= 0.25
                    or (not latest.get("off") and (
                        latest["loss"] >= 0.35
                        or (latest["loss"] >= 0.15 and latest["coast_s"] >= 2.0)))
                )
            else:
                qualifies = (
                    avg >= float(self.cfg["min_loss_to_speak"])
                    or (not latest.get("off") and (
                        latest["loss"] >= 0.25
                        or (latest["loss"] >= 0.10 and latest["coast_s"] >= 1.5)))
                )
            if qualifies:
                scored.append((max(avg, cue_src["loss"]), num, cue_src))
        scored.sort(key=lambda x: -x[0])
        return scored

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
            # fire when current bin is within a window past the trigger
            # (~25 m -- wide enough that 30 Hz polling can't step over it)
            delta = (cur_bin - trig) % NBINS
            if delta <= max(6, int(25.0 / bin_m)):
                cue = corner_cue(
                    latest,
                    brake_only=(self.cfg.get("focus") == "brake"),
                    priority=guide_priority(getattr(self.profile, "guide", None), num),
                    guide=getattr(self.profile, "guide", None),
                    symptom_tips=bool(self.cfg.get("guide_symptom_tips", True)))
                if cue:
                    self.speaker.say(cue)
                    self.last_spoke_t = t
                    self.approach_fired.add(num)
                    self.approach_cues_this_lap += 1
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
            if delta <= max(6, int(25.0 / bin_m)):
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
                res = analyze_lap(spd, brk, gas, self.profile.ref, [c], self.profile.bin_m,
                                  off_events=live_events,
                                  off_min_s=float(self.cfg.get("off_min_duration_s", 0.35)),
                                  off_2_min_s=float(self.cfg.get("off_min_duration_2tyres_s", 0.9)),
                                  lap_str=str_live)[0]
                self._live_losses[num] = res  # v2.1.0: section feedback feedstock
                line = None
                # Minor zones (shallow dips) must lose more before
                # they earn a callout -- more places, not more talking.
                minor = bool(c.get("minor"))
                min_loss = 0.35 if minor else float(self.cfg.get("exit_min_loss_s", 0.2))
                if res.get("off"):
                    pass  # he was off the track in that zone -- no feedback, it was a save
                elif res["loss"] >= min_loss:
                    line = f"{c['name']}: lost {tenths_phrase(res['loss'])} there"
                elif res["loss"] >= (0.20 if minor else 0.10) and res["coast_s"] >= 2.0:
                    line = (f"{c['name']}: big coast there, costing "
                            f"{tenths_phrase(res['loss'])}")
                if line:
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

    def _speak_lap_summary(self, lap_time, results):
        p = self.profile
        parts = [f"Lap {fmt_lap_spoken(lap_time)}."]
        if p and p.best_lap_s and abs(lap_time - p.best_lap_s) > 0.05:
            if lap_time > p.best_lap_s:
                parts.append(f"{lap_time - p.best_lap_s:.1f} off your best.")
            else:
                parts.append("That's a new best.")
        clean = [r for r in results if not r.get("off")]
        worst = max(clean, key=lambda r: r["loss"]) if clean else None
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
            elif brake_only:
                parts.append(f"Biggest leak: {worst['name'].lower()}, costing {tenths_phrase(worst['loss'])}.")
        offs = [r for r in results if r.get("off")]
        if offs:
            parts.append(f"Off track at {offs[0]['name']} — that one doesn't count.")
        reps = [r for r in results if r.get("repeat_off")]
        if reps:
            parts.append(f"{reps[0]['name']}: wide there every lap — "
                         f"that's a pattern, not a save, so it counts now.")
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
    tf.write(f"AC Live Coach session {stamp}\n\n")

    def log(event, data):
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

    return log, txt_path, jsonl_path, tf


# --------------------------------------------------------------------------
# Live mode
# --------------------------------------------------------------------------

def sanitize(text):
    keep = []
    for ch in text.strip():
        keep.append(ch if (ch.isalnum() or ch in "_-.") else "_")
    return "".join(keep) or "unknown"



def _trace_svg(p):
    """Reference-lap speed + pedal traces as inline SVG (v2.4.1) --
    the chart Albert asked to see in the report: speed (mph) on top,
    brake/throttle below, corner markers at each zone's brake point,
    apex and throttle point. Returns "" without a reference lap."""
    if p is None or not p.ref or not p.ref.get("spd"):
        return ""
    spd_kmh = p.ref["spd"]
    brk = p.ref.get("brk") or [0.0] * len(spd_kmh)
    gas = p.ref.get("gas") or [0.0] * len(spd_kmh)
    n = len(spd_kmh)
    bin_ft = (p.bin_m or 2.2) * FT_PER_M
    total_ft = n * bin_ft
    mph = [v * MPH_PER_KMH for v in spd_kmh]
    lo = max(0.0, min(mph) - 4)
    hi = max(mph) + 4
    W, L, R = 960, 46, 10
    ST, SB = 34, 214          # speed panel top/bottom
    PT, PB = 244, 312          # pedal panel top/bottom

    def X(b):
        return L + (W - L - R) * (b / max(1, n - 1))

    def Y(v):
        return SB - (SB - ST) * ((v - lo) / max(1.0, hi - lo))

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
    pts = " ".join(f"{X(b):.0f},{Y(mph[b]):.0f}" for b in range(0, n, 2))
    parts.append(f'<polyline points="{pts}" fill="none" stroke="#1a56db" '
                 f'stroke-width="1.6"/>')
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
                     f'text-anchor="middle">{c.get("num")} {html.escape(str(name))}</text>')
    for series, color in ((brk, "#d64545"), (gas, "#2e9e5b")):
        pts = " ".join(f"{X(b):.0f},{PB - (PB - PT) * max(0.0, min(1.0, series[b])):.0f}"
                       for b in range(0, n, 4))
        parts.append(f'<polyline points="{L},{PB} {pts} {X(n - 1):.0f},{PB}" '
                     f'fill="{color}" opacity="0.4" stroke="none"/>')
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
    parts.append('<p class="legend"><span style="color:#d64545">&#9660; brake point</span>'
                 '<span style="color:#111">&#9679; apex</span>'
                 '<span style="color:#2e9e5b">&#9650; back to full throttle</span>'
                 '<span>Distance in feet</span></p>')
    return "<h2>Reference lap — speed &amp; pedals</h2>\n" + "\n".join(parts) + "\n"


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
                f'font-weight="bold" fill="#fff" text-anchor="middle">{c["num"]}</text>')
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

    rows = []
    for c, avg, latest in stats:
        if latest is None:
            rows.append(f"<tr><td>{esc(corner_label(c))}</td><td>—</td><td>—</td>"
                        f"<td>—</td><td>—</td></tr>")
        else:
            bd = latest["brake_delta_m"]
            bd_s = f"{bd * FT_PER_M:+.0f}" if bd is not None else "—"
            rows.append(
                f"<tr><td><b>{esc(corner_label(c))}</b></td>"
                f"<td style='color:{loss_color(avg)}'><b>{avg:+.2f}s</b></td>"
                f"<td>{bd_s}</td><td>{latest['apex_delta_kmh'] * MPH_PER_KMH:+.0f}</td>"
                f"<td>{latest['coast_s']:.1f}s</td></tr>")
    table_html = ("<table><tr><th>Corner</th><th>Avg loss</th><th>Brake Δ (last lap, ft)</th>"
                  "<th>Apex Δ (mph)</th><th>Coast</th></tr>" + "\n".join(rows) + "</table>")

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

    trace_html = _trace_svg(p)
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
    log, txt_path, jsonl_path, tf = make_logger()
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

    report_path = txt_path.replace("debrief_", "report_").replace(".txt", ".html")
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
    hint_warned = False
    last_static_check = 0.0
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
            now = time.time()
            if now - last_static_check > 2.0:
                last_static_check = now
                st = tele.static()
                key = sanitize(f"{st.track}__{st.carModel}")
                if key != current_key:
                    current_key = key
                    profile = TrackProfile(key)
                    if profile.load():
                        print(f"[profile] loaded saved reference for {key}")
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
            coach.feed(now, gfx.normalizedCarPosition, phys.speedKmh,
                       phys.brake, phys.gas, gfx.distanceTraveled,
                       live=(gfx.status == AC_LIVE), ict_ms=gfx.iCurrentTime,
                       heading=phys.heading,
                       tyres_out=phys.numberOfTyresOut,
                       steer=phys.steerAngle)
            if (len(coach.lap_records) != report_state["laps"]
                    or (not report_state["opened"]
                        and coach.profile is not None
                        and coach.profile.ref is not None)):
                write_report()
            time.sleep(1 / 30.0)
    except KeyboardInterrupt:
        print("\nStopping coach...")
    finally:
        coach.flush()
        lines = coach.session_summary_lines()
        tf.write("\nSession summary\n")
        for ln in lines:
            tf.write(ln + "\n")
            print(ln)
        tf.close()
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


def run_simulate(cfg):
    print("=== SIMULATE MODE (no game; synthetic Buttonwillow-like track) ===")
    log, txt_path, jsonl_path, tf = make_logger()
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
    if "--simulate" in sys.argv:
        run_simulate(cfg)
    else:
        run_live(cfg)


if __name__ == "__main__":
    main()
