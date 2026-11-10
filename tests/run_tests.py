#!/usr/bin/env python3
"""Self-tests for AC Live Coach -- no Assetto Corsa required.

    python tests/run_tests.py

Covers the rules that were each learned from a real session or an
external review: reference-lap integrity (v2.6.0), cue ranking by
measured cost (v2.6.0), names/guide count guards (v2.6.0), the layout
guard (v2.6.0), the report-outline rule (v2.5.1) and atomic profile
recovery (v2.5.3). Exits non-zero on the first failure."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import ac_live_coach as m

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


class FakeSpeaker:
    def __init__(self):
        self.lines = []
        self.tones = 0
        self.busy = False
        self.started_tags = []   # v2.24.0: synchronous playback model
        self.dropped_tags = []

    def say(self, text, **kw):
        self.lines.append(text)
        if kw.get("tag") is not None:
            self.started_tags.append(kw["tag"])

    def say_clauses(self, a, b, gap_ms=220, tag=None):
        self.lines.append(f"{a} -- {b}")
        if tag is not None:
            self.started_tags.append(tag)

    def tone(self):
        self.tones += 1

    def tone_ready(self):
        return not self.busy


TMP = tempfile.mkdtemp(prefix="coach_tests_")
m.TRACKS_DIR = TMP
m.PROFILE_DIR = TMP

CFG = m.load_config()


def make_rec(dist=2511.0, off_events=None):
    """One synthetic lap: 200 km/h with a single braking dip."""
    n = m.NBINS
    spd = [200.0] * n
    brk = [0.0] * n
    gas = [1.0] * n
    for j in range(940, 1000):
        brk[j] = 0.9
        gas[j] = 0.0
    for j in range(970, 1060):
        spd[j] = 80.0 + abs(j - 1015) * 0.4
    for j in range(1000, 1060):
        gas[j] = 0.0
    return {"spd": spd, "brk": brk, "gas": gas, "hdg": None, "str": None,
            "dist_m": dist, "dur_s": 100.0, "ict_peak_s": 0.0,
            "off_events": off_events or [], "path_pts": None}


def new_coach(key="test__car"):
    coach = m.Coach(CFG, FakeSpeaker(), lambda *a, **k: None)
    prof = m.TrackProfile(key)
    coach.set_profile(prof)
    return coach, prof


# ---- compound suffix (v2.6.0) ----
check("compound (S)", m.compound_suffix("Soft (S)") == "tc_s")
check("compound plain", m.compound_suffix("Semislicks") == "tc_semislicks")
check("compound empty", m.compound_suffix("") == "" and m.compound_suffix("  ") == "")

# ---- cue ranking by measured cost (v2.6.0) ----
BASE = {"name": "Turn 1", "brake_delta_m": None, "coast_s": 0.0,
        "apex_shift_m": None, "mid_lift": False, "apex_delta_kmh": 0.0,
        "throttle_delta_m": None, "steer_rev_delta": None,
        "steer_work_ratio": None, "span_m": 250.0, "ref_zone_time": 10.0,
        "ref_apex_kmh": 80.0}
s1 = dict(BASE, brake_delta_m=-9.0, coast_s=2.4)
check("cue: big coast beats small brake", m._pick_candidate(s1)[0] == "coast")
s2 = dict(BASE, brake_delta_m=-70.0, coast_s=0.6)
check("cue: big brake beats small coast", m._pick_candidate(s2)[0] == "brake")
s3 = dict(BASE, brake_delta_m=-20.0, throttle_delta_m=24.0)
check("cue: raw cost picks power", m._pick_candidate(s3)[0] == "power")
check("cue: guide entry bias flips it", m._pick_candidate(s3, priority="entry")[0] == "brake")

# ---- reference integrity (v2.6.0) ----
coach, prof = new_coach()
off = [{"bins": {500}, "dur_s": 1.2, "max_tyres": 3}]
coach._finalize_lap(make_rec(off_events=off), 100.0, "test")
check("first lap sets a reference", prof.ref is not None and prof.best_lap_s == 100.0)
check("first lap with an off marks the reference dirty", prof.ref_dirty is True)
rec2 = make_rec()
coach._finalize_lap(rec2, 102.0, "test")
check("clean lap within 3% cleans the reference",
      prof.best_lap_s == 102.0 and prof.ref_dirty is False
      and prof.ref["spd"] is rec2["spd"])
apex_bin = prof.corners[0]["apex_bin"] if prof.corners else 1015
off2 = [{"bins": {apex_bin}, "dur_s": 1.0, "max_tyres": 3}]
rec3 = make_rec(off_events=off2)
coach._finalize_lap(rec3, 99.0, "test")
check("faster lap with an off cannot take the reference",
      prof.best_lap_s == 102.0 and prof.ref["spd"] is rec2["spd"])
rec4 = make_rec()
coach._finalize_lap(rec4, 98.0, "test")
check("faster clean lap takes the reference",
      prof.best_lap_s == 98.0 and prof.ref["spd"] is rec4["spd"])

# ---- layout guard (v2.6.0) ----
coach2, prof2 = new_coach("test2__car")
coach2._finalize_lap(make_rec(dist=2500.0), 100.0, "test")
prof2.save()
loaded = m.TrackProfile("test2__car")
assert loaded.load()
coach3 = m.Coach(CFG, FakeSpeaker(), lambda *a, **k: None)
coach3.set_profile(loaded)
coach3._finalize_lap(make_rec(dist=4100.0), 100.0, "test")
check("different-length lap switches to a layout profile",
      coach3.profile.key.endswith("__layout4100")
      and coach3.profile.ref is not None)

# ---- names count guard (v2.6.0) ----
coach4, prof4 = new_coach("test4__car")
corners = [{"num": 1, "name": "Turn 1"}, {"num": 2, "name": "Turn 2"}]
import json
with open(os.path.join(TMP, "test4__names.json"), "w", encoding="utf-8") as f:
    json.dump({"1": "A", "2": "B", "3": "C"}, f)
coach4._apply_user_names([dict(c) for c in corners])
cs = [dict(c) for c in corners]
coach4._apply_user_names(cs)
check("names with a wrong zone count are ignored",
      [c["name"] for c in cs] == ["Turn 1", "Turn 2"])
with open(os.path.join(TMP, "test4__names.json"), "w", encoding="utf-8") as f:
    json.dump({"1": "Alpha", "2": "Bravo"}, f)
cs = [dict(c) for c in corners]
coach4._apply_user_names(cs)
check("names with a matching count apply",
      [c["name"] for c in cs] == ["Alpha", "Bravo"])

# ---- atomic profile recovery (v2.5.3) ----
coach5, prof5 = new_coach("test5__car")
coach5._finalize_lap(make_rec(), 100.0, "test")
prof5.save()
prof5.save()  # second save creates the .bak
with open(prof5.path, "w", encoding="utf-8") as f:
    f.write("{corrupt")
recovered = m.TrackProfile("test5__car")
check("corrupt profile recovers from backup",
      recovered.load() and recovered.best_lap_s == 100.0)

# ---- slip detection (v2.7.0) ----
coach6, prof6 = new_coach("test6__car")
R = 0.33
V = 100.0 / 3.6
def ww_for(fl, fr, rl, rr):
    return [fl * V / R, fr * V / R, rl * V / R, rr * V / R]
t = 0.0
for i in range(15):  # 0.5 s of front lockup under braking
    t += 1 / 30
    coach6._slip_update(t, 0.500 + i * 0.0004, 100.0, 0.8, 0.0,
                        ww_for(0.6, 0.62, 1.0, 1.0), [R] * 4)
for i in range(10):  # release; event closes after the grace gap
    t += 1 / 30
    coach6._slip_update(t, 0.510 + i * 0.0004, 100.0, 0.0, 0.2,
                        ww_for(1.0, 1.0, 1.0, 1.0), [R] * 4)
check("lockup event recorded", len(coach6._slip_events) == 1
      and coach6._slip_events[0]["kind"] == "lockup"
      and coach6._slip_events[0]["dur_s"] >= 0.3)
coach6._slip_events.clear()
for i in range(15):  # 0.5 s of rear wheelspin under power
    t += 1 / 30
    coach6._slip_update(t, 0.520 + i * 0.0004, 100.0, 0.0, 0.9,
                        ww_for(1.0, 1.0, 1.45, 1.4), [R] * 4)
coach6._close_slip_events()
check("wheelspin event recorded", len(coach6._slip_events) == 1
      and coach6._slip_events[0]["kind"] == "spin")
coach6._slip_events.clear()
for i in range(20):  # matched wheels: nothing
    t += 1 / 30
    coach6._slip_update(t, 0.530 + i * 0.0004, 100.0, 0.5, 0.5,
                        ww_for(1.0, 1.0, 1.0, 1.0), [R] * 4)
coach6._close_slip_events()
check("matched wheels record nothing", coach6._slip_events == [])

# ---- slip reaches the corner analysis and the cue (v2.7.0) ----
coach7, prof7 = new_coach("test7__car")
coach7._finalize_lap(make_rec(), 100.0, "test")
c0 = prof7.corners[0]
res = m.analyze_lap(*[make_rec()[k] for k in ("spd", "brk", "gas")],
                    prof7.ref, prof7.corners, prof7.bin_m,
                    slip_events=[{"kind": "lockup", "bins": {c0["apex_bin"]},
                                  "dur_s": 0.5}])
check("lockup lands on the corner", res[0]["lockup_s"] == 0.5)
s_lock = dict(BASE, lockup_s=0.5, coast_s=0.2)
cue = m.corner_cue(dict(s_lock, num=1, loss=0.5, off=False))
check("lockup becomes the spoken fix", cue is not None and "locking" in cue)

# ---- penalty blocks the reference like an off (v2.7.0) ----
coach8, prof8 = new_coach("test8__car")
rec_p = make_rec(); rec_p["penalty"] = True
coach8._finalize_lap(rec_p, 100.0, "test")
check("first lap with a penalty marks the reference dirty",
      prof8.ref_dirty is True)
coach8._finalize_lap(make_rec(), 101.0, "test")  # clean, within 3%: cleans it
check("clean lap cleans a penalty-dirty reference",
      prof8.best_lap_s == 101.0 and prof8.ref_dirty is False)
rec_p2 = make_rec(); rec_p2["penalty"] = True
coach8._finalize_lap(rec_p2, 99.0, "test")
check("faster lap with a penalty cannot take the reference",
      prof8.best_lap_s == 101.0)

# ---- report: three-lap chart + brake spread (v2.7.0, v2.11.0) ----
html = m.build_report_html(coach)
check("report has the brake-spread column", "Brake spread" in html)
check("report chart shows the session-best trace", "#e07b24" in html)

# ---- record + replay round-trip (v2.7.0) ----
import math as _math
frames = []
tt = 0.0
for lap in range(3):
    steps = 3000 if lap < 2 else 300  # partial 3rd lap: crosses lap 2's finish
    for i in range(steps):
        tt += 1 / 30
        pos = i / steps
        spd = 200.0 - 120.0 * _math.exp(-((pos - 0.5075) ** 2) / 0.0002)
        vv = max(spd, 30.0) / 3.6
        frames.append({"event": "frame", "t": round(tt, 3), "pos": round(pos, 5),
                       "spd": round(spd, 1), "brk": 0.9 if 0.47 < pos < 0.50 else 0.0,
                       "gas": 0.0 if pos < 0.53 else 1.0, "str": 0.0,
                       "dist": round(lap * 2511.0 + pos * 2511.0, 1),
                       "ict": int((i / 30) * 1000), "out": 0,
                       "ww": [round(vv / R, 1)] * 4, "wr": [R] * 4,
                       "flag": 0, "pen": 0.0, "hdg": None})
sess_path = os.path.join(TMP, "replay_session.jsonl")
with open(sess_path, "w", encoding="utf-8") as f:
    f.write(json.dumps({"event": "session", "key": "test9__car"}) + "\n")
    for fr in frames:
        f.write(json.dumps(fr) + "\n")
rcoach = m.run_replay(CFG, sess_path)
check("replay rebuilds a reference from recorded frames",
      rcoach is not None and rcoach.profile.best_lap_s is not None
      and len(rcoach.lap_records) >= 1)

# ---- scheduler gate + cue memory (v2.8.0) ----
check("version lives in the code only", "version" not in CFG)
coachA, profA = new_coach("testA__car")
profA.track_length_m = 4406.0
profA.corners = [
    {"num": 1, "name": "Turn 1", "zone_start": 1250, "zone_end": 1380,
     "brake_bin": 1300},
    {"num": 2, "name": "Turn 2", "zone_start": 1440, "zone_end": 1650,
     "brake_bin": 1450},
]
resA = {"num": 2, "name": "Turn 2", "loss": 0.5, "off": False,
        "coast_s": 0.0, "brake_delta_m": -20.0, "apex_delta_kmh": 0.0,
        "apex_shift_m": None, "mid_lift": False, "throttle_delta_m": None,
        "steer_rev_delta": None, "steer_work_ratio": None,
        "span_m": 250.0, "ref_zone_time": 10.0, "ref_apex_kmh": 80.0,
        "lockup_s": 0.0, "spin_s": 0.0, "peak_brake": None,
        "peak_brake_ref": None, "release_s": None, "release_ref_s": None}
coachA.corner_history = {2: [resA]}
coachA.last_spoke_t = -1000.0
coachA._maybe_approach_cue(1000.0, 1340 / 2000)   # trigger inside Turn 1's zone
check("approach cue holds while mid-corner", coachA.speaker.lines == [])
coachA._maybe_approach_cue(1000.0, 1400 / 2000)   # on the straight: fires
check("approach cue fires on the straight",
      coachA.speaker.lines == ["Turn 2: brake later"])
coachA.approach_fired.clear(); coachA.approach_cues_this_lap = 0
coachA.lap_number += 1; coachA.last_spoke_t = -1000.0
coachA._maybe_approach_cue(1000.0, 1400 / 2000)
check("same cue next lap is suppressed by cue memory",
      len(coachA.speaker.lines) == 1)
coachA.corner_history = {2: [dict(resA, loss=0.8)]}
coachA.approach_fired.clear(); coachA.approach_cues_this_lap = 0
coachA.lap_number += 1; coachA.last_spoke_t = -1000.0
coachA._maybe_approach_cue(1000.0, 1400 / 2000)
check("a worsened leak earns the cue back", len(coachA.speaker.lines) == 2)

# ---- brake-shape symptoms (v2.8.0) ----
s_soft = dict(BASE, brake_delta_m=-12.0, peak_brake=0.55,
              peak_brake_ref=0.95, apex_delta_kmh=-4.0)
check("soft peak brake is its own symptom",
      m._pick_candidate(s_soft)[0] == "brake_soft")
s_rel = dict(BASE, brake_delta_m=None, release_s=0.2, release_ref_s=0.6,
             apex_delta_kmh=-4.0)
check("abrupt release is its own symptom",
      m._pick_candidate(s_rel)[0] == "release")

# ---- clause gap + voice default (v2.9.0) ----
import io as _io, wave as _wave
def _mk_wav(rate, seconds, val=1000):
    buf = _io.BytesIO()
    with _wave.open(buf, "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(rate)
        wf.writeframes(b"".join(
            int(val).to_bytes(2, "little", signed=True)
            for _ in range(int(rate * seconds))))
    return buf.getvalue()
_wa = _mk_wav(22050, 0.10); _wb = _mk_wav(22050, 0.10)
_joined = m._splice_wavs(_wa, _wb, 0.22)
with _wave.open(_io.BytesIO(_joined), "rb") as wf:
    _n = wf.getnframes(); _rate = wf.getframerate()
    _frames = wf.readframes(_n)
_seg = _frames[int(0.15 * _rate) * 2:int(0.30 * _rate) * 2]
check("clause splice inserts true silence",
      abs(_n / _rate - 0.42) < 0.01 and len(_seg) > 0
      and _seg == b"\x00" * len(_seg))
check("splice refuses mismatched formats",
      m._splice_wavs(_wa, _mk_wav(16000, 0.1), 0.2) is None)
check("default voice is lessac-high", CFG["piper_voice"] == "en_US-lessac-high")
check("clause gap configured", CFG["exit_clause_gap_ms"] == 220)
_logs = []
_sp = m.Speaker(CFG, lambda ev, d: _logs.append((ev, d)))
_sp.enabled = False
_sp.say_clauses("Sunset: lost 3 tenths there", "brake later", 220)
check("say_clauses logs the combined line",
      ("cue", {"text": "Sunset: lost 3 tenths there -- brake later"}) in _logs)

# ---- comms fx (v2.10.0) ----
import math as _math
def _mix_wav():
    rate = 22050
    buf = _io.BytesIO()
    with _wave.open(buf, "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(rate)
        wf.writeframes(b"".join(
            int(0.35 * 32767 * (_math.sin(2 * _math.pi * 150 * i / rate)
                                + _math.sin(2 * _math.pi * 1000 * i / rate))
                ).to_bytes(2, "little", signed=True)
            for i in range(int(rate * 0.30))))
    return buf.getvalue()
def _band_energy(wav_bytes, freq):
    with _wave.open(_io.BytesIO(wav_bytes), "rb") as wf:
        rate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    vals = [int.from_bytes(raw[i:i + 2], "little", signed=True) / 32768.0
            for i in range(0, len(raw), 2)]
    vals = vals[len(vals) // 2:]  # skip the click prefix and transients
    re_ = sum(v * _math.cos(2 * _math.pi * freq * i / rate)
              for i, v in enumerate(vals))
    im_ = sum(v * _math.sin(2 * _math.pi * freq * i / rate)
              for i, v in enumerate(vals))
    return (re_ * re_ + im_ * im_) ** 0.5
_fx = m._comms_fx(_mix_wav(), click=True)
check("comms fx band-limits (150 Hz collapses vs 1 kHz)",
      _band_energy(_fx, 150) < 0.30 * _band_energy(_fx, 1000))
with _wave.open(_io.BytesIO(_fx), "rb") as wf:
    _dur = wf.getnframes() / wf.getframerate()
check("comms fx adds the key-up prefix", 0.34 < _dur < 0.37)
_fx_nc = m._comms_fx(_mix_wav(), click=False)
with _wave.open(_io.BytesIO(_fx_nc), "rb") as wf:
    _dur_nc = wf.getnframes() / wf.getframerate()
check("no prefix without the click", abs(_dur_nc - 0.30) < 0.01)
check("comms fx defaults on",
      CFG["voice_comms_fx"] is True and CFG["voice_comms_click"] is True)

# ---- three-lap report traces (v2.11.0) ----
coachB, profB = new_coach("testB__car")
profB.track_length_m = 4406.0
profB.best_lap_s = 100.0
profB.ref = {"spd": [80.0] * m.NBINS, "brk": [0.0] * m.NBINS,
             "gas": [0.0] * m.NBINS, "str": [0.0] * m.NBINS}
profB.corners = [
    {"num": 1, "name": "Turn 1", "zone_start": 100, "zone_end": 199,
     "brake_bin": 120, "apex_bin": 150, "throttle_bin": 180,
     "has_brake_zone": True},
    {"num": 2, "name": "Turn 2", "zone_start": 500, "zone_end": 599,
     "brake_bin": 520, "apex_bin": 550, "throttle_bin": 580,
     "has_brake_zone": True},
]
coachB.set_profile(profB)
spdA = [80.0] * m.NBINS; spdB = [80.0] * m.NBINS
for b in range(100, 200): spdA[b] = 90.0
for b in range(500, 600): spdB[b] = 95.0
coachB._note_counted_lap_traces(101.0, spdA, [0.0] * m.NBINS, [
    {"num": 1, "zone_time": 9.0, "off": False},
    {"num": 2, "zone_time": 11.0, "off": False}])
coachB._note_counted_lap_traces(100.5, spdB, [0.0] * m.NBINS, [
    {"num": 1, "zone_time": 10.5, "off": False},
    {"num": 2, "zone_time": 9.2, "off": False}])
check("optimal takes zone 1 from lap A", coachB._optimal["spd"][150] == 90.0)
check("optimal takes zone 2 from lap B", coachB._optimal["spd"][550] == 95.0)
check("optimal keeps the reference elsewhere",
      coachB._optimal["spd"][300] == 80.0)
check("session best retained", coachB._session_best["time"] == 100.5)
coachB._note_counted_lap_traces(102.0, [120.0] * m.NBINS, [0.0] * m.NBINS, [
    {"num": 1, "zone_time": 8.0, "off": True},
    {"num": 2, "zone_time": 8.0, "off": True}])
check("off zones never enter the optimal",
      coachB._optimal["zone_times"][1] == 9.0)
_html = m._trace_svg(profB, coachB._session_best, coachB._optimal)
check("chart draws all three laps",
      all(k in _html for k in ("Best ever", "Optimal", "Best this session")))
check("chart falls back to the reference alone", m._trace_svg(profB) != "")

# ---- brake tone (v2.12.0) ----
tcfg = dict(CFG, brake_tone=True)
tcoach = m.Coach(tcfg, FakeSpeaker(), lambda *a, **k: None)
tprof = m.TrackProfile("tone__car")
tcoach.set_profile(tprof)
tprof.track_length_m = 5000.0
tprof.corners = [
    {"num": 1, "name": "Turn 1", "brake_bin": 940, "has_brake_zone": True},
    {"num": 2, "name": "Lift", "brake_bin": 300, "has_brake_zone": False},
]
tcoach._prev_bin = 930
tcoach._maybe_brake_tone(10.0, 941 / m.NBINS)
check("tone: fires at the reference brake point", tcoach.speaker.tones == 1)
tcoach._prev_bin = 941
tcoach._maybe_brake_tone(10.1, 950 / m.NBINS)
check("tone: once per corner per lap", tcoach.speaker.tones == 1)
tcoach.reset_next_approach_state()
tcoach._prev_bin = 930
tcoach._maybe_brake_tone(11.0, 941 / m.NBINS)
check("tone: re-arms next lap", tcoach.speaker.tones == 2)
tcoach.reset_next_approach_state()
tcoach.speaker.busy = True
tcoach._prev_bin = 930
tcoach._maybe_brake_tone(12.0, 941 / m.NBINS)
tcoach.speaker.busy = False
tcoach._prev_bin = 941
tcoach._maybe_brake_tone(12.1, 950 / m.NBINS)
check("tone: busy channel does not fire at the crossing",
      tcoach.speaker.tones == 2)
tcoach.reset_next_approach_state()
tcoach._prev_bin = 290
tcoach._maybe_brake_tone(13.0, 301 / m.NBINS)
check("tone: silent where the reference did not brake",
      tcoach.speaker.tones == 2)
ocoach = m.Coach(CFG, FakeSpeaker(), lambda *a, **k: None)
oprof = m.TrackProfile("tone2__car")
ocoach.set_profile(oprof)
oprof.track_length_m = 5000.0
oprof.corners = [{"num": 1, "brake_bin": 940, "has_brake_zone": True}]
ocoach._prev_bin = 930
ocoach._maybe_brake_tone(14.0, 941 / m.NBINS)
check("tone: on by default since v2.14.1", ocoach.speaker.tones == 1)
ocoach2 = m.Coach(dict(CFG, brake_tone=False), FakeSpeaker(),
                  lambda *a, **k: None)
oprof2 = m.TrackProfile("tone3__car")
ocoach2.set_profile(oprof2)
oprof2.track_length_m = 5000.0
oprof2.corners = [{"num": 1, "brake_bin": 940, "has_brake_zone": True}]
ocoach2._prev_bin = 930
ocoach2._maybe_brake_tone(14.0, 941 / m.NBINS)
check("tone: explicit off still silences it", ocoach2.speaker.tones == 0)
import io
import wave as _wave
_data = m.Speaker._render_tone_wav(0.7)
with _wave.open(io.BytesIO(_data), "rb") as _wf:
    check("tone: wav is 22050 Hz mono, ~150 ms",
          _wf.getframerate() == 22050 and _wf.getnchannels() == 1
          and 3000 < _wf.getnframes() < 3400)

# ---- brake tone late window (v2.13.0) ----
tcoach.reset_next_approach_state()
tcoach.speaker.busy = True
tcoach._prev_bin = 930
tcoach._maybe_brake_tone(20.0, 941 / m.NBINS)
check("tone: busy crossing is held, not dropped",
      tcoach.speaker.tones == 2 and tcoach._tone_pending is not None)
tcoach.speaker.busy = False
tcoach._service_tone_pending(20.4, 945 / m.NBINS)  # 5 bins x 2.5 m = 12.5 m past
check("tone: held tone fires when the channel frees inside the window",
      tcoach.speaker.tones == 3 and tcoach._tone_pending is None)
tcoach.reset_next_approach_state()
tcoach.speaker.busy = True
tcoach._prev_bin = 930
tcoach._maybe_brake_tone(21.0, 941 / m.NBINS)
tcoach.speaker.busy = False
tcoach._service_tone_pending(21.4, 960 / m.NBINS)  # 20 bins x 2.5 m = 50 m past
check("tone: held tone drops once past the late window",
      tcoach.speaker.tones == 3 and tcoach._tone_pending is None)

# ---- distance position fallback (v2.13.0) ----
def drive_distance_session(coach, laps=5, lap_m=2900.0, dt=0.033):
    """Synthetic spline-less session: raw position frozen at 0.0,
    one braking dip per lap, the lap timer resetting at the line.
    Pace works out to ~80.1 s per 2,900 m lap."""
    t = 1000.0
    dist = 0.0
    lap_t0 = t
    total = lap_m * laps
    while dist < total:
        s = dist % lap_m
        if 1600.0 <= s <= 1850.0:
            v = 75.0
        else:
            v = 140.0
        if 1550.0 <= s < 1700.0:
            b, g = 0.85, 0.0
        elif 1600.0 <= s <= 1850.0:
            b, g = 0.0, 0.3
        else:
            b, g = 0.0, 1.0
        coach.feed(t, 0.0, v, b, g, dist, live=True,
                   ict_ms=int((t - lap_t0) * 1000))
        step = (v / 3.6) * dt
        dist += step
        t += dt
        if dist % lap_m < s:
            lap_t0 = t
    coach.flush()

dcoach = m.Coach(CFG, FakeSpeaker(), lambda *a, **k: None)
dprof = m.TrackProfile("dist__car")
dcoach.set_profile(dprof)
drive_distance_session(dcoach)
check("fallback: dead spline latches distance mode",
      dcoach._pos_src == "distance")
check("fallback: lap length learned from the timer",
      dcoach._fb_len is not None and abs(dcoach._fb_len - 2900.0) < 60.0,
      f"len={dcoach._fb_len}")
check("fallback: reference built from buffered laps", dprof.ref is not None)
check("fallback: laps were counted", len(dcoach.lap_records) >= 2,
      f"records={len(dcoach.lap_records)}")
check("fallback: a later lap was fully analysed",
      any(not r["out_lap"] for r in dcoach.lap_records))
check("fallback: corners detected", len(dprof.corners) >= 1,
      f"corners={len(dprof.corners)}")
check("fallback: best lap near the synthetic pace",
      dprof.best_lap_s is not None and abs(dprof.best_lap_s - 80.1) < 3.0,
      f"best={dprof.best_lap_s}")

scoach = m.Coach(CFG, FakeSpeaker(), lambda *a, **k: None)
sprof = m.TrackProfile("spline__car")
scoach.set_profile(sprof)
_t2 = 500.0
for _i in range(300):
    scoach.feed(_t2, (_i % 100) / 100.0, 120.0, 0.0, 1.0, _i * 4.0,
                live=True, ict_ms=(_i * 33) % 90000)
    _t2 += 0.033
check("fallback: live spline stays spline mode", scoach._pos_src == "spline")

# ---- fallback: mid-lap latch anchors to the lap's start (v2.15.0) ----
# Albert's Nov 4 Rainbow Road return, distilled: the stream opens
# AT a lap start, the dead-spline verdict lands ~200 m into lap 1,
# and the driver quits early in lap 3 -- so learning has exactly
# laps 1 and 2 to agree, and lap 1 must be measured whole (2,880 m),
# not from the latch point (~2,680 m), or the pair never agrees and
# nothing arms.
_ac = m.Coach(dict(CFG, brake_tone=False), FakeSpeaker(),
              lambda *a, **k: None)
_ap = m.TrackProfile("anchor__nospline")
_ac.set_profile(_ap)
_v = 25.0            # m/s (90 km/h)
_dt = 1.0 / 30.0
_dist = [0.0]
def _drive_laps(coach, dist, laps_m):
    t = dist[0] / _v
    for lap_m in laps_m:
        end = dist[0] + lap_m
        while dist[0] < end:
            lt = (dist[0] - (end - lap_m)) / _v
            coach.feed(t, 0.0, _v * 3.6, 0.4, 0.8, dist[0],
                       ict_ms=int(lt * 1000))
            dist[0] += _v * _dt
            t += _dt
    return t
_drive_laps(_ac, _dist, [2880.0, 2880.0, 400.0])
check("fallback: mid-lap latch still learns from laps 1+2",
      _ac._fb_len is not None and abs(_ac._fb_len - 2880.0) < 30.0,
      f"len={_ac._fb_len}")
check("fallback: flushed lap 2 becomes the reference",
      _ap.ref is not None and _ap.best_lap_s is not None
      and abs(_ap.best_lap_s - 115.2) < 2.0,
      f"best={_ap.best_lap_s}")

# ---- coast split + specific cue wording (v2.14.0) ----
_n = m.NBINS
_ref = {"spd": [150.0] * _n, "brk": [0.0] * _n, "gas": [1.0] * _n}
_lap_spd = [150.0] * _n
_lap_brk = [0.0] * _n
_lap_gas = [1.0] * _n
for _j in range(120, 141):
    _lap_brk[_j] = 0.9          # lap brakes at bin 120, ref at 150
for _j in range(141, 210):
    _lap_gas[_j] = 0.0          # then rolls, mostly before the apex
_st = m._span_stats(_lap_spd, _lap_brk, _lap_gas, None, _ref,
                    100, 300, 200, 150, 210, 150.0, 2.5)
check("span: coast split puts the roll before the apex",
      _st["coast_entry_s"] > 2.0 and _st["coast_entry_s"] > _st["coast_exit_s"],
      f"entry={_st['coast_entry_s']} exit={_st['coast_exit_s']}")
check("span: brake delta is early by 75 m", _st["brake_delta_m"] == -75.0)
_lap_spd2 = list(_lap_spd)
for _j in range(100, 103):
    _lap_spd2[_j] = 130.0
_st2 = m._span_stats(_lap_spd2, _lap_brk, _lap_gas, None, _ref,
                     100, 300, 200, 150, 210, 150.0, 2.5)
check("span: entry speed deficit measured", _st2["entry_delta_kmh"] < -15.0,
      f"entry={_st2['entry_delta_kmh']}")

def _res(**kw):
    base = {"name": "Turn 6", "num": 6, "off": False, "loss": 0.5,
            "coast_s": 0.0, "coast_entry_s": 0.0, "coast_exit_s": 0.0,
            "brake_delta_m": None, "throttle_delta_m": None,
            "apex_delta_kmh": 0.0, "entry_delta_kmh": 0.0,
            "prev_name": "Turn 5", "ref_zone_time": 12.0,
            "zone_time": 12.5, "span_m": 500.0}
    base.update(kw)
    return base

_c1 = m.corner_cue(_res(coast_s=3.0, coast_entry_s=2.7, coast_exit_s=0.3,
                        brake_delta_m=-30.0))
check("cue: early brake + entry roll names both",
      _c1 is not None and "rolling to the apex" in _c1
      and "brake later, carry it in" in _c1, f"got {_c1}")
_c2 = m.corner_cue(_res(coast_s=2.0, coast_entry_s=0.2, coast_exit_s=1.8,
                        throttle_delta_m=40.0))
check("cue: exit coast + late throttle is a power line",
      _c2 is not None and "waiting to get back to power" in _c2,
      f"got {_c2}")
_c3 = m.corner_cue(_res(entry_delta_kmh=-12.0))
check("cue: entry deficit blames the previous exit",
      _c3 is not None and "arriving slow" in _c3
      and "Turn 5's exit" in _c3, f"got {_c3}")
_c4 = m.corner_cue(_res(coast_s=2.0, coast_entry_s=1.0, coast_exit_s=1.0))
check("cue: evenly spread coast keeps the generic line",
      _c4 is not None and "less coasting, commit to a pedal" in _c4,
      f"got {_c4}")

# ---- sass spectrum (v2.14.0) ----
def _summary_coach(level):
    c = m.Coach(dict(CFG, sass_level=level, talk_level=2), FakeSpeaker(),
                lambda *a, **k: None)
    pr = m.TrackProfile(f"sass{level}__car")
    c.set_profile(pr)
    pr.best_lap_s = 100.0
    return c

_sc0 = _summary_coach(0)
_sc0._speak_lap_summary(99.0, [])
check("sass 0: PB line is the classic wording",
      any("That's a new best." in ln for ln in _sc0.speaker.lines)
      and not any("Frame it" in ln for ln in _sc0.speaker.lines))
_sc1 = _summary_coach(1)
for _ in range(3):
    _sc1._speak_lap_summary(99.0, [])
_pb_clauses = set()
for ln in _sc1.speaker.lines:
    for cand in ("That's a new best.", "New best. Frame it.",
                 "New best — the reference just moved."):
        if cand in ln:
            _pb_clauses.add(cand)
check("sass 1: PB pool rotates without repeating",
      len(_pb_clauses) == 3, f"got {_pb_clauses}")
_sc2 = _summary_coach(2)
_worst = [_res(coast_s=2.0, coast_entry_s=1.0, coast_exit_s=1.0)]
_sc2._speak_lap_summary(101.0, _worst)
_sc2._speak_lap_summary(101.5, _worst)
_rep2 = ("Same corner as last lap.", "It was that corner last lap, too.")
check("sass 2: repeat leak is called out on the second lap",
      any(s in _sc2.speaker.lines[-1] for s in _rep2)
      and not any(s in _sc2.speaker.lines[-2] for s in _rep2))
_sc1b = _summary_coach(1)
_sc1b._speak_lap_summary(105.0, [])
check("sass 1: clean lap gets the dry closer",
      any("nothing to fix" in ln.lower() for ln in _sc1b.speaker.lines))

# ---- talk level + cue priority (v2.16.0) ----
_spk = object.__new__(m.Speaker)
import queue as _queue
_spk.q = _queue.Queue()
_spk.log = lambda *a, **k: None
_spk.enabled = True
_spk.say("Lap 2 oh 7 point 8. 5.5 off your best. Biggest leak: ...")
_spk.say_clauses("Turn 4: lost 4 tenths there", "brake later")
_spk.say("Turn 6: brake later", kind="cue")
_left = list(_spk.q.queue)
check("speaker: an approach cue purges queued info lines",
      len(_left) == 1 and _left[0][1] == "Turn 6: brake later"
      and _left[0][3] == "cue", f"left={_left}")
_tc1 = m.Coach(dict(CFG, sass_level=3, talk_level=1), FakeSpeaker(),
               lambda *a, **k: None)
_tp1 = m.TrackProfile("talk1__car")
_tc1.set_profile(_tp1)
_tp1.best_lap_s = 100.0
_tc1._speak_lap_summary(99.0, [])
check("talk 1: sass is silenced even at sass_level 3",
      any("That's a new best." in ln for ln in _tc1.speaker.lines)
      and not any("Frame it" in ln for ln in _tc1.speaker.lines))
_tc0 = m.Coach(dict(CFG, sass_level=3, talk_level=0), FakeSpeaker(),
               lambda *a, **k: None)
_tp0 = m.TrackProfile("talk0__car")
_tc0.set_profile(_tp0)
_tp0.best_lap_s = 100.0
_tc0._speak_lap_summary(101.0, _worst)
check("talk 0: summary drops the leak line entirely",
      not any("Biggest leak" in ln for ln in _tc0.speaker.lines),
      f"got {_tc0.speaker.lines}")

# ---- sass spectrum extended: supportive -1, mean girl 4 (v2.19.0) ----
def _lvl_coach(level, talk):
    c = m.Coach(dict(CFG, sass_level=level, talk_level=talk),
                FakeSpeaker(), lambda *a, **k: None)
    pr = m.TrackProfile(f"lvl{level}_{talk}__car")
    c.set_profile(pr)
    pr.best_lap_s = 100.0
    return c

check("sass level clamps to -1..4",
      m.Coach(dict(CFG, sass_level=99), FakeSpeaker(),
              lambda *a, **k: None)._sass_level() == 4
      and m.Coach(dict(CFG, sass_level=-7), FakeSpeaker(),
                  lambda *a, **k: None)._sass_level() == -1)
check("sass default is mean girl (v2.19.1, Albert's call)",
      m.DEFAULT_CONFIG["sass_level"] == 4)
check("sass eff: supportive survives talk 1, colour does not",
      _lvl_coach(-1, 1)._sass_eff() == -1
      and _lvl_coach(4, 1)._sass_eff() == 0
      and _lvl_coach(-1, 0)._sass_eff() == 0
      and _lvl_coach(4, 2)._sass_eff() == 4
      and _lvl_coach(-1, 2)._sass_eff() == -1)

_mg = _lvl_coach(4, 2)
_mg._speak_lap_summary(99.0, [])
check("mean girl: PB line is backhanded",
      any("New best." in ln and any(s in ln for s in (
          "broken clock", "find something", "keep up with yourself",
          "do it again")) for ln in _mg.speaker.lines),
      f"got {_mg.speaker.lines}")
_mg2 = _lvl_coach(4, 2)
_mg2._speak_lap_summary(105.0, [])
check("mean girl: slow lap gets the treatment",
      any(any(s in ln for s in (
          "flying lap", "embarrassed", "a tour", "out lap called",
          "rental kart")) for ln in _mg2.speaker.lines),
      f"got {_mg2.speaker.lines}")

_sup = _lvl_coach(-1, 1)   # talk 1 on purpose: supportive must survive it
_sup._speak_lap_summary(99.0, [])
check("supportive: PB praised even at talk 1",
      any(any(s in ln for s in (
          "beautiful lap", "work paying off", "earned every tenth",
          "looked smooth")) for ln in _sup.speaker.lines),
      f"got {_sup.speaker.lines}")
_sup2 = _lvl_coach(-1, 1)
_sup2._speak_lap_summary(105.0, [])
check("supportive: slow lap reframed, not mocked",
      any(any(s in ln for s in (
          "pace is in you", "good ones are still coming",
          "pace is there")) for ln in _sup2.speaker.lines),
      f"got {_sup2.speaker.lines}")
_sup3 = _lvl_coach(-1, 2)
for _ in range(4):
    _sup3._speak_lap_summary(99.0, [])
_pb4 = set()
for ln in _sup3.speaker.lines:
    for cand in ("beautiful lap", "work paying off", "earned every tenth",
                 "looked smooth"):
        if cand in ln:
            _pb4.add(cand)
check("supportive: PB pool rotates without repeating",
      len(_pb4) == 4, f"got {_pb4}")

# Supportive gain callouts: the reference crawls through the zone,
# the live lap doesn't -- at sass -1 that earns a line; at 0, silence.
def _gain_coach(level):
    c = m.Coach(dict(CFG, sass_level=level, talk_level=2),
                FakeSpeaker(), lambda *a, **k: None)
    pr = m.TrackProfile(f"gain{level}__car")
    c.set_profile(pr)
    pr.track_length_m = 5000.0
    pr.corners = [{"num": 1, "name": "Turn 1", "zone_start": 1000,
                   "zone_end": 1200, "brake_bin": 1010, "apex_bin": 1100,
                   "throttle_bin": 1150, "apex_speed_kmh": 60.0,
                   "has_brake_zone": True, "phases": None}]
    rspd = [150.0] * m.NBINS
    for j in range(1000, 1200):
        rspd[j] = 60.0
    pr.ref = {"spd": rspd, "brk": [0.0] * m.NBINS,
              "gas": [1.0] * m.NBINS, "hdg": None, "str": None}
    c.lap_number = 1
    c.last_spoke_t = -1000.0
    for i in range(1000):
        c.bins.add(i / 1000.0, 150.0, 0.0, 1.0)
    c._prev_bin = 1201
    c._maybe_exit_feedback(1000.0, 1203 / m.NBINS)
    return c

_gc = _gain_coach(-1)
check("supportive: a real gain earns a callout",
      any(("gained" in ln or "in hand" in ln or "found there" in ln)
          and "Turn 1" in ln for ln in _gc.speaker.lines),
      f"got {_gc.speaker.lines}")
_gc0 = _gain_coach(0)
check("pro voice: gains stay silent at sass 0",
      not any("gained" in ln or "in hand" in ln
              for ln in _gc0.speaker.lines),
      f"got {_gc0.speaker.lines}")

# ---- Buttonwillow map corrected (v2.20.0, Albert Nov 5 2026) ----
def _bw_coach():
    c = m.Coach(CFG, FakeSpeaker(), lambda *a, **k: None)
    pr = m.TrackProfile("nd_buttonwillow__testcar")
    c.set_profile(pr)
    pr.track_length_m = 4406.0
    return c

def _corner(num, frac):
    b = int(frac * m.NBINS)
    return {"num": num, "name": f"Turn {num}", "apex_bin": b,
            "zone_start": max(0, b - 60), "zone_end": b + 60,
            "brake_bin": b - 30, "throttle_bin": b + 30,
            "apex_speed_kmh": 90.0, "has_brake_zone": True, "phases": None}

_bw = _bw_coach()
_fracs9 = [0.067, 0.137, 0.225, 0.265, 0.418, 0.631, 0.733, 0.760, 0.939]
_cs9 = [_corner(i + 1, f) for i, f in enumerate(_fracs9)]
_bw._apply_corner_names(_cs9)
check("BW 9-zone map: Bus Stop at 5, Sweeper Entry/Exit at 7/8",
      [c["name"] for c in _cs9] == [
          "Sunrise", "Off Ramp", "Cotton Corners 1", "Cotton Corners 2",
          "Bus Stop", "Phil Hill", "Sweeper Entry", "Sweeper Exit",
          "Sunset"],
      f"got {[c['name'] for c in _cs9]}")
_bw7 = _bw_coach()
_cs7 = [_corner(i + 1, f) for i, f in enumerate(
    [0.067, 0.137, 0.254, 0.418, 0.631, 0.757, 0.939])]
_bw7._apply_corner_names(_cs7)
check("BW 7-zone map: Bus Stop / Phil Hill / Sweeper / Sunset",
      [c["name"] for c in _cs7] == [
          "Sunrise", "Off Ramp", "Cotton Corners", "Bus Stop",
          "Phil Hill", "Sweeper", "Sunset"],
      f"got {[c['name'] for c in _cs7]}")
_bw3 = _bw_coach()
_cs3 = [_corner(i + 1, f) for i, f in enumerate(
    [0.067, 0.137, 0.225, 0.265, 0.418, 0.631, 0.733, 0.760, 0.830, 0.939])]
_bw3._apply_corner_names(_cs3)
check("BW span-5 third dip is the Esses, after Sweeper Exit",
      _cs3[8]["name"] == "Esses" and _cs3[7]["name"] == "Sweeper Exit",
      f"got {[c['name'] for c in _cs3]}")

# ---- bin interpolation rescues slow feeds (v2.16.3) ----
_lb = m.LapBins()
_lb.add(0.1000, 100.0, 0.0, 0.5)   # bin 200
_lb.add(0.1030, 100.0, 0.0, 0.5)   # bin 206 -- 19 Hz on a fast section
check("bins: skipped bins between frames get filled",
      _lb.spd_cnt[203] > 0 and abs(_lb.coverage() - 7 / 2000) < 1e-9)
_lb.add(0.5000, 100.0, 0.0, 0.5)   # teleport: 794-bin jump
check("bins: teleport jumps are never filled", _lb.spd_cnt[500] == 0)
_lb2 = m.LapBins()
_lb2.add(0.9995, 90.0, 0.0, 0.4)   # bin 1999
_lb2.add(0.0010, 90.0, 0.0, 0.4)   # bin 2, across the wrap
check("bins: interpolation crosses the start/finish wrap",
      _lb2.spd_cnt[0] > 0 and _lb2.spd_cnt[1] > 0)

# ---- split reset: budget waits for a valid lap (v2.16.3) ----
_rc = m.Coach(CFG, FakeSpeaker(), lambda *a, **k: None)
_rc._exit_spoken = 2
_rc._tone_fired = {3}
_rc.reset_next_approach_state(reset_exit_budget=False)
check("reset: discarded lap keeps the spent exit budget",
      _rc._exit_spoken == 2)
check("reset: tones still re-arm on a discarded lap",
      _rc._tone_fired == set())
_rc.reset_next_approach_state()
check("reset: a valid lap restores the exit budget",
      _rc._exit_spoken == 0)

# ---- summary survives a mid-session remap (v2.16.2) ----
# Rainbow Road, distilled: lap A analysed under an 8-corner map,
# lap B under a 9-corner map (the reference replacement re-detected
# the corners). The best lap's record has no corner 9; the
# theoretical-best loop must skip it, not raise StopIteration and
# take the whole report down with it.
_mc = m.Coach(CFG, FakeSpeaker(), lambda *a, **k: None)
def _fake_lap(t, nums, loss, zt):
    return {"time_s": t, "out_lap": False,
            "corners": [dict(_res(), num=n, loss=loss, zone_time=zt)
                        for n in nums]}
_mc.lap_records = [_fake_lap(100.0, range(1, 9), 0.3, 10.0),
                   _fake_lap(105.0, range(1, 10), 0.3, 9.0)]
try:
    _mlines = _mc.session_summary_lines()
    _mok = any("Theoretical best" in ln for ln in _mlines)
except Exception:
    _mok = False
check("summary: mixed corner maps across a remap don't crash it", _mok)

# ---- a dead log target must never mute the coach (v2.16.1) ----
def _boom(*a, **k):
    raise ValueError("I/O operation on closed file")
_spk2 = object.__new__(m.Speaker)
_spk2.q = _queue.Queue()
_spk2.log = _boom
_spk2.enabled = True
try:
    _spk2.say("Turn 6: brake later", kind="cue")
    _spk2.say_clauses("Turn 4: lost 4 tenths there", "brake later")
    _muted = False
except Exception:
    _muted = True
check("speaker: speech still enqueues when logging raises",
      not _muted and _spk2.q.qsize() == 2)

# ---- briefing variety (v2.14.0) ----
_bcoach = m.Coach(dict(CFG, sass_level=0), FakeSpeaker(),
                  lambda *a, **k: None)
_bprof = m.TrackProfile("brief__car")
_bcoach.set_profile(_bprof)
_bprof.best_lap_s = 100.0
_bprof.track_length_m = 3000.0
_bprof.corners = [{"num": i} for i in range(1, 9)]
_bcoach._loaded_with_ref = True
_bcoach._maybe_brief(1000.0, 100.0, 0.0)
_bcoach._maybe_brief(1001.0, 100.0, 950.0)
check("briefing: opener comes from the loaded pool",
      len(_bcoach.speaker.lines) >= 1
      and _bcoach.speaker.lines[0] in m.Coach.BRIEFING_LOADED_A,
      f"got {_bcoach.speaker.lines[:1]}")
_bcoach._maybe_brief(1016.0, 100.0, 1200.0)
check("briefing: stats line follows with a varied frame",
      len(_bcoach.speaker.lines) >= 2
      and any(k in _bcoach.speaker.lines[1]
              for k in ("places mapped", "corners on the map",
                        "places mapped here")),
      f"got {_bcoach.speaker.lines[1:2]}")

# --- v2.17.0 layout isolation --------------------------------------
# Layouts of one track folder (Streets vs Big Willow in the DDM mod;
# classic vs The Circuit in Nukedrop's Buttonwillow) must not share
# guides, names files, or corner maps.
import json as _json

m.TRACKS_DIR = TMP  # Coach() constructions above re-resolve the global


class _StubProfile:
    def __init__(self, key):
        self.key = key


def _write_guide(stem, zones=2, length=None):
    g = {"track": stem,
         "zones": {str(i): {"name": f"Z{i}", "character": "", "tips": []}
                   for i in range(1, zones + 1)}}
    if length:
        g["layout_length_m"] = length
    with open(os.path.join(TMP, stem + "__guide.json"), "w") as f:
        _json.dump(g, f)


_write_guide("testtrack", zones=2)
_write_guide("testtrack__big", zones=3, length=4023)
_write_guide("testtrack__layout5550", zones=4)

check("layout len: parses the suffix",
      m._layout_len("a__b__layout4020") == 4020
      and m._layout_len("a__b") is None)
check("layout guide: plain profile gets the folder guide",
      (m._load_guide_for_profile(_StubProfile("testtrack__car"))
       or {}).get("track") == "testtrack")
check("layout guide: exact layout file wins",
      (m._load_guide_for_profile(
          _StubProfile("testtrack__car__layout5550"))
       or {}).get("track") == "testtrack__layout5550")
check("layout guide: length tolerance binds the named layout guide",
      (m._load_guide_for_profile(
          _StubProfile("testtrack__car__layout4020"))
       or {}).get("track") == "testtrack__big")
check("layout guide: unmatched layout gets NO folder fallback",
      m._load_guide_for_profile(
          _StubProfile("testtrack__car__layout9990")) is None)

check("layout map: layout profile gets its own map file",
      m.TrackProfile("testtrack__car__layout4020").track_map_path()
      .endswith("testtrack__layout4020__map.json"))
check("layout map: plain profile path unchanged",
      m.TrackProfile("testtrack__car").track_map_path()
      .endswith("testtrack__map.json"))

with open(os.path.join(TMP, "nametrack__names.json"), "w") as f:
    _json.dump({"1": "Wrong One", "2": "Wrong Two"}, f)
_ncoach = m.Coach(CFG, FakeSpeaker(), lambda *a, **k: None)
_ncoach.profile = m.TrackProfile("nametrack__car__layout4020")
_ncs = [{"num": 1, "name": "Turn 1"}, {"num": 2, "name": "Turn 2"}]
_ncoach._apply_user_names(_ncs)
check("layout names: folder names file does not leak into a layout",
      [c["name"] for c in _ncs] == ["Turn 1", "Turn 2"])
check("layout names: starter lands at the layout path",
      os.path.exists(os.path.join(
          TMP, "nametrack__layout4020__names.json")))

_bwcoach = m.Coach(CFG, FakeSpeaker(), lambda *a, **k: None)
_bwcoach.profile = m.TrackProfile("nd_buttonwillow__car__layout5000")
_bwcs = [{"num": i, "name": f"Turn {i}", "apex_bin": int(i * 200)}
         for i in range(1, 8)]
_bwcoach._apply_corner_names(_bwcs)
check("bw names: layout profile keeps Turn N (no classic spans)",
      all(c["name"].startswith("Turn") for c in _bwcs))

# Every guide shipped in the app's guides/ folder must parse (a broken
# Big Willow file shipped in v2.17.0's first commit and was silently
# skipped by the loader -- caught by the binding check, fixed same day),
# and the layout guides must bind to representative layout profiles.
import glob as _glob
for _fn in sorted(_glob.glob(os.path.join(m.APP_DIR, "guides",
                                          "*__guide.json"))):
    try:
        _gd = _json.load(open(_fn, encoding="utf-8"))
        _ok = isinstance(_gd.get("zones"), dict) and len(_gd["zones"]) > 0
    except Exception:
        _ok = False
    check(f"shipped guide parses: {os.path.basename(_fn)}", _ok)
check("shipped guide binds: Big Willow by length",
      (m._load_guide_for_profile(
          _StubProfile("ddm_gts_willow_springs__car__layout4020"))
       or {}).get("layout_length_m") == 4023)
check("shipped guide binds: Circuit by length",
      (m._load_guide_for_profile(
          _StubProfile("nd_buttonwillow__car__layout4130"))
       or {}).get("layout_length_m") == 4131)

# ---- guided lap (v2.18.0) ----
# Albert's idea: on the session's second lap the coach reads the
# track guide corner by corner -- one short line per corner, lowest
# speech priority, skipped rather than spoken late.
GUIDE_T = {"track": "test", "zones": {
    "1": {"name": "Turn 1", "priority": "exit", "tips": [
        {"text": "brake side one", "trigger": "brake"},
        "plain one",
        {"text": "power side one", "trigger": "power"}]},
    "2": {"name": "Turn 2", "priority": "entry", "tips": [
        {"text": "power side two", "trigger": "power"},
        {"text": "brake side two", "trigger": "brake"}]},
    "3": {"name": "Turn 3", "priority": "balanced", "tips": [
        {"text": "trig three", "trigger": "coast"}, "plain three"]},
    "4": {"name": "Turn 4", "priority": "exit", "tips": []}}}
check("tour line: exit priority picks the power tip",
      m.guide_tour_line(GUIDE_T, 1) == "power side one")
check("tour line: entry priority picks the brake tip",
      m.guide_tour_line(GUIDE_T, 2) == "brake side two")
check("tour line: balanced falls back to the first plain tip",
      m.guide_tour_line(GUIDE_T, 3) == "plain three")
check("tour line: no zone / no tips -> None",
      m.guide_tour_line(GUIDE_T, 9) is None
      and m.guide_tour_line(GUIDE_T, 4) is None
      and m.guide_tour_line(None, 1) is None)

# ---- v2.29.1: dedicated tour pacenotes + length cap ----
GUIDE_T2 = {"track": "test", "zones": {
    "1": {"name": "Turn 1", "priority": "exit",
          "tour": "short pacenote wins",
          "tips": [{"text": "power side one", "trigger": "power"}]},
    "2": {"name": "Turn 2", "priority": "entry",
          "tips": [{"text": " ".join(["word"] * 30),
                    "trigger": "brake"}]}}}
check("tour field beats the priority tip",
      m.guide_tour_line(GUIDE_T2, 1) == "short pacenote wins")
_capped = m.guide_tour_line(GUIDE_T2, 2)
check("an over-long tour pick caps at a word boundary",
      _capped is not None and len(_capped) <= 72
      and not _capped.endswith("wor")
      and _capped.split()[-1] == "word")
import glob as _glob
import json as _json
for _gf in _glob.glob(os.path.join(os.path.dirname(m.__file__),
                                   "guides", "*__guide.json")):
    _g = _json.load(open(_gf))
    for _num, _z in (_g.get("zones") or {}).items():
        if "tour" in _z:
            check(f"shipped tour line <= 72 chars "
                  f"({os.path.basename(_gf)} zone {_num})",
                  len(_z["tour"]) <= 72)
for _gf, _n in (("ddm_gts_willow_springs__big__guide.json", 9),
                ("nd_buttonwillow__circuit__guide.json", 10)):
    _g = _json.load(open(os.path.join(
        os.path.dirname(m.__file__), "guides", _gf)))
    check(f"{_gf}: every zone carries a tour line",
          len(_g["zones"]) == _n
          and all("tour" in z for z in _g["zones"].values()))


def tour_coach(key, **cfgkw):
    c, p = new_coach(key)
    if cfgkw:
        c.cfg = dict(CFG, **cfgkw)
    p.track_length_m = 4406.0
    p.corners = [
        {"num": 1, "name": "Turn 1", "zone_start": 1250, "zone_end": 1380,
         "brake_bin": 1300},
        {"num": 2, "name": "Turn 2", "zone_start": 1650, "zone_end": 1800,
         "brake_bin": 1700},
        {"num": 3, "name": "Turn 3", "zone_start": 1850, "zone_end": 1990,
         "brake_bin": 1900},
    ]
    p.guide = GUIDE_T
    c.last_spoke_t = -1000.0
    return c


_ab = int(CFG["approach_distance_m"] / (4406.0 / m.NBINS))
POS1 = (1300 - _ab + 3) / m.NBINS   # inside corner 1's tour window
POS2 = (1700 - _ab + 3) / m.NBINS   # inside corner 2's tour window

gc = tour_coach("testG__car")
gc._maybe_guide_tour(1000.0, POS1)
check("tour silent on lap 1 (no reference yet)", gc.speaker.lines == [])
gc.lap_number = 1
gc._maybe_guide_tour(1000.0, POS1)
check("tour speaks corner 1 on lap 2",
      gc.speaker.lines == ["Turn 1: power side one"])
gc._maybe_guide_tour(1001.0, POS1)
check("tour does not repeat a corner", len(gc.speaker.lines) == 1)
gc.last_spoke_t = -1000.0
gc._maybe_guide_tour(1002.0, POS2)
check("tour speaks corner 2 on lap 2",
      gc.speaker.lines[-1] == "Turn 2: brake side two")
gc.lap_number = 2
gc._tour_corners = set(); gc.last_spoke_t = -1000.0
gc._maybe_guide_tour(1003.0, POS1)
check("tour silent on lap 3 without a rearm", len(gc.speaker.lines) == 2)

gc2 = tour_coach("testG2__car")
gc2.lap_number = 1
gc2.speaker.busy = True
gc2._maybe_guide_tour(1000.0, POS1)
gc2.speaker.busy = False
gc2._maybe_guide_tour(1000.5, POS1)
check("tour skips a busy corner instead of speaking late",
      gc2.speaker.lines == [])

gc3 = tour_coach("testG3__car")
gc3.lap_number = 1
gc3.approach_fired.add(1)
gc3._maybe_guide_tour(1000.0, POS1)
check("tour yields a corner to a data approach cue",
      gc3.speaker.lines == [])

gc4 = tour_coach("testG4__car", talk_level=0)
gc4.lap_number = 1
gc4._maybe_guide_tour(1000.0, POS1)
check("tour silent at talk_level 0", gc4.speaker.lines == [])
gc5 = tour_coach("testG5__car", guide_lap=False)
gc5.lap_number = 1
gc5._maybe_guide_tour(1000.0, POS1)
check("tour silent with guide_lap off", gc5.speaker.lines == [])

# Corner 3's trigger (brake 1900 - approach) sits inside Turn 2's
# zone: the line must hold while mid-corner, then fire once clear
# with >=100 m still to run -- the approach cues' geometry, learned
# from the first replay, where a tight 25 m window inside the
# previous zone consumed every corner as "missed".
gc6 = tour_coach("testG6__car")
gc6.lap_number = 1
gc6._maybe_guide_tour(1000.0, 1790 / m.NBINS)   # in zone 2, past T3's trigger
check("tour holds a line while mid-corner", gc6.speaker.lines == [])
gc6.last_spoke_t = -1000.0
gc6._maybe_guide_tour(1001.0, 1810 / m.NBINS)   # clear of zone 2
check("tour fires once clear of the previous corner",
      gc6.speaker.lines == ["Turn 3: plain three"])

bc, _bp = new_coach("testB__car")
bc._finalize_lap(make_rec(), 100.0, "test")
check("tour armed after lap 1", bc.lap_number == 1
      and not bc._guide_tour_done and bc._guide_tour_active())
_rec_m = make_rec(); _rec_m["muted"] = True
bc._finalize_lap(_rec_m, 102.0, "test")
check("muted tour lap re-arms the tour for lap 3",
      bc.lap_number == 2 and bc._guide_tour_rearm
      and not bc._guide_tour_done and bc._guide_tour_active())
bc._finalize_lap(make_rec(), 101.0, "test")
check("tour closes after the re-armed lap",
      bc.lap_number == 3 and bc._guide_tour_done
      and not bc._guide_tour_active())
dc, _dp = new_coach("testD__car")
dc._finalize_lap(make_rec(), 100.0, "test")
dc._finalize_lap(make_rec(), 101.0, "test")
check("clean lap 2 completes the tour (no rearm)",
      dc.lap_number == 2 and dc._guide_tour_done
      and not dc._guide_tour_rearm)

# ---- history relabel Riverside -> Bus Stop (v2.21.1) ----
_hist = {"sessions": 26, "laps": 100,
         "corners": {"Riverside": {"sum": 12.5, "n": 40},
                     "Bus Stop": {"sum": 1.5, "n": 4},
                     "Phil Hill": {"sum": 20.0, "n": 50}}}
_hp = os.path.join(TMP, "nd_buttonwillow__carx__history.json")
with open(_hp, "w", encoding="utf-8") as f:
    json.dump(_hist, f)
_hc, _hprof = new_coach("nd_buttonwillow__carx")
_hcorners = _hc.history["corners"]
check("history relabel merges Riverside into Bus Stop",
      "Riverside" not in _hcorners
      and _hcorners["Bus Stop"] == {"sum": 14.0, "n": 44}
      and _hcorners["Phil Hill"] == {"sum": 20.0, "n": 50})
with open(_hp, encoding="utf-8") as f:
    _disk = json.load(f)
check("history relabel persists to disk", "Riverside" not in _disk["corners"])
_hp2 = os.path.join(TMP, "othertrack__car__history.json")
with open(_hp2, "w", encoding="utf-8") as f:
    json.dump({"sessions": 3, "laps": 9,
               "corners": {"Riverside": {"sum": 3.0, "n": 7}}}, f)
_oc, _oprof = new_coach("othertrack__car")
check("history relabel leaves other tracks alone",
      _oc.history["corners"].get("Riverside") == {"sum": 3.0, "n": 7})

# ---- guided lap v2.22.0: exclusive tour + reference-lap gate ----
gx = tour_coach("testGX__car")
gx.lap_number = 1
gx.speaker.busy = True
gx._maybe_guide_tour(1000.0, POS1, exclusive=True)
check("exclusive tour speaks even when the channel is busy",
      gx.speaker.lines == ["Turn 1: power side one"])
gx2 = tour_coach("testGX2__car")
gx2.lap_number = 1
gx2.approach_fired.add(1)
gx2._maybe_guide_tour(1000.0, POS1, exclusive=True)
check("exclusive tour does not yield to a data cue",
      gx2.speaker.lines == ["Turn 1: power side one"])
gx3 = tour_coach("testGX3__car")
gx3.lap_number = 1
gx3.last_spoke_t = 1000.0   # speech cooldown active
gx3._maybe_guide_tour(1000.0, POS1, exclusive=True)
check("exclusive tour ignores the speech cooldown",
      gx3.speaker.lines == ["Turn 1: power side one"])
ec, _ep = new_coach("testE__car")
ec._finalize_lap(make_rec(), 100.0, "test")     # sets the reference
_ep.ref_dirty = True                            # lap 2 driven under it
ec._finalize_lap(make_rec(), 101.0, "test")
check("reference-setting lap re-arms the tour",
      ec.lap_number == 2 and ec._guide_tour_rearm
      and not ec._guide_tour_done and ec._guide_tour_active())
_trec = make_rec(); _trec["tour"] = True
ec._finalize_lap(_trec, 102.0, "test")
check("tour closes after the re-armed tour lap runs",
      ec.lap_number == 3 and ec._guide_tour_done
      and not ec._guide_tour_rearm)
sc, _sp = new_coach("testS__car")
sc._finalize_lap(make_rec(), 100.0, "test")
_n0 = len(sc.speaker.lines)
_srec = make_rec(); _srec["tour"] = True
sc._finalize_lap(_srec, 101.0, "test")
check("guided lap gets no spoken summary",
      len(sc.speaker.lines) == _n0)
sc._finalize_lap(make_rec(), 102.0, "test")
check("the lap after the guided lap is summarised again",
      len(sc.speaker.lines) > _n0)

# ---- guided lap v2.22.1: far-ahead corners are not "missed" ----
# Regression: unanchored modular deltas read a corner half a lap
# ahead as long past its trigger, so corners 6-9 of 9 were consumed
# as missed in the lap's first seconds and the guide stopped
# reading about halfway (Albert, Nov 6).
gw = tour_coach("testGW__car")
gw.lap_number = 1
gw._maybe_guide_tour(1000.0, 0.01)   # lap start: every corner ahead
check("tour consumes nothing at lap start",
      gw.speaker.lines == [] and gw._tour_corners == set())
gw._maybe_guide_tour(1000.0, POS1)
check("tour still fires corner 1 at its trigger",
      gw.speaker.lines == ["Turn 1: power side one"])
gw.last_spoke_t = -1000.0
gw._maybe_guide_tour(1001.0, 1450 / m.NBINS)  # between corners 1 and 2
check("tour does not consume corner 2 while it is still ahead",
      gw._tour_corners == {1} and len(gw.speaker.lines) == 1)

# ---- v2.23.0 robustness pack ----
_cfg = dict(m.DEFAULT_CONFIG)
_cfg["cue_cooldown_s"] = "6"
_cfg["sass_level"] = "loud"
m._sanitize_config(_cfg)
check("config: numeric strings are coerced",
      _cfg["cue_cooldown_s"] == 6.0)
check("config: garbage values fall back to the default",
      _cfg["sass_level"] == m.DEFAULT_CONFIG["sass_level"])
_good = {"track_length_m": 4406.0,
         "ref_spd": [100.0] * m.NBINS, "ref_brk": [0.0] * m.NBINS,
         "ref_gas": [1.0] * m.NBINS,
         "corners": [{"name": "Turn 1", "brake_bin": 10,
                      "zone_start": 5, "zone_end": 20}]}
check("profile shape: a well-formed profile passes",
      m._profile_shape_ok(_good))
_bad = dict(_good); _bad["ref_spd"] = [100.0] * 100
check("profile shape: a short trace is rejected",
      not m._profile_shape_ok(_bad))
_bad2 = dict(_good); _bad2["corners"] = [{"name": "Turn 1"}]
check("profile shape: a corner missing bins is rejected",
      not m._profile_shape_ok(_bad2))
oc, _op = new_coach("testOC__car")
oc.observe_official(3, 100000, 1000.0)   # baseline
oc.observe_official(4, 100000, 1000.1)   # increment opens the watch
oc.observe_official(4, 100000, 1002.0)   # settles with no lap behind it
check("official with no matching lap is dropped as an orphan",
      oc.official_times == [])
oc._last_finish_t = 1002.0
oc.observe_official(5, 101000, 1002.1)
oc.observe_official(5, 101000, 1004.0)
check("official behind a recent finish is kept",
      oc.official_times == [101.0])
_zp = os.path.join(TMP, "zonetest__guide.json")
with open(_zp, "w", encoding="utf-8") as f:
    json.dump({"zones": {"1": {"name": "A"}, "2": "oops"}}, f)
_zg = m._load_guide("zonetest")
check("guide load drops a non-object zone",
      _zg is not None and set(_zg["zones"]) == {"1"})
rc, _rp = new_coach("testRC__car")
_rp.corners = [{"num": 1, "name": "A"}, {"num": 2, "name": "B"}]
_rp.guide = {"zones": {"1": {}, "2": {}, "3": {}}}
rc._revalidate_guide()
check("guide is dropped when a remap changes the zone count",
      _rp.guide is None)
pb, _pp = new_coach("testPB__car")
pb._finalize_lap(make_rec(), 100.0, "test")
pb._finalize_lap(make_rec(), 101.0, "test")
_n1 = len(pb.speaker.lines)
pb._finalize_lap(make_rec(off_events=[{"bins": {970}, "dur_s": 1.2,
                                       "max_tyres": 3}]), 99.0, "test")
_pb_lines = pb.speaker.lines[_n1:]
check("a faster lap with an off is not announced as a new best",
      not any("new best" in s.lower() for s in _pb_lines)
      and any("doesn't count" in s for s in _pb_lines))
_ns = [0.0] * m.NBINS
_nb = [0.0] * m.NBINS
for j in range(950, 952):
    _nb[j] = 0.5            # a two-bin check-up tap
for j in range(1000, 1051):
    _nb[j] = 0.9            # the committed stop
_ng = [1.0] * m.NBINS
_nspd = [150.0] * m.NBINS
_ref = {"spd": _nspd, "brk": _nb, "gas": _ng}
_st = m._span_stats(_nspd, _nb, _ng, _ns, _ref, 900, 1100, 1050,
                    1000, 1090, 80.0, 2.2)
check("brake point ignores a check-up tap",
      _st["brake_delta_m"] == 0.0)
fc, _fp = new_coach("testFC__car")
_fp.corners = [
    {"num": 1, "name": "Turn A", "zone_start": 1290, "zone_end": 1310,
     "brake_bin": 1300, "apex_bin": 1305, "has_brake_zone": True},
    {"num": 2, "name": "Turn B", "zone_start": 1440, "zone_end": 1460,
     "brake_bin": 1450, "apex_bin": 1455, "has_brake_zone": True}]
_a = dict(BASE, name="Turn A", loss=0.7, off=False)
_b = dict(BASE, name="Turn B", loss=0.6, off=False, coast_s=0.9)
fc.corner_history = {1: [_a], 2: [_b]}
fc.lap_number = 1
fc.last_spoke_t = -1000.0
fc._maybe_approach_cue(1000.0, 1350 / m.NBINS)
check("a no-cue focus corner does not starve the next one",
      any(s.startswith("Turn B") for s in fc.speaker.lines))

# ---- v2.24.0 coaching-accuracy pack ----
_gd = {"zones": {"7": {"name": "Z", "tips": [
    {"text": "go deeper here", "trigger": "brake_later"},
    {"text": "generic brake tip", "trigger": "brake"}]}}}
_early = dict(BASE, num=7, name="Turn 7", brake_delta_m=-30.0)
_deep = dict(BASE, num=7, name="Turn 7", brake_delta_m=30.0)
check("guide tip: braked early earns the brake-later tip",
      m.corner_cue(_early, guide=_gd) == "Turn 7: go deeper here")
check("guide tip: braked deep falls back to the generic tip",
      m.corner_cue(_deep, guide=_gd) == "Turn 7: generic brake tip")
_mm = dict(BASE, coast_s=1.2, coast_entry_s=1.0, brake_delta_m=-60.0)
_merged = [c for c in m._cue_candidates(_mm)
           if c[0] == "brake" and "rolling to the apex" in c[1]]
check("merged coast+brake is priced at the larger component",
      _merged and abs(_merged[0][2] - 1.2) < 0.01)
bc, _bp = new_coach("testBC__car")
_bp.corners = [{"num": 1, "name": "Turn 1", "zone_start": 900,
                "zone_end": 1200, "brake_bin": 900, "apex_bin": 1000,
                "throttle_bin": 1050, "apex_speed_kmh": 90.0,
                "has_brake_zone": True}]
_brk = [0.0] * m.NBINS
for j in range(950, 1001):
    _brk[j] = 0.9
_bp.ref = {"spd": [150.0] * m.NBINS, "brk": _brk,
           "gas": [1.0] * m.NBINS}
bc._refresh_corner_bins(_bp)
check("brake bins follow a replaced reference",
      _bp.corners[0]["brake_bin"] == 950)
fc2, _fp2 = new_coach("testFC2__car")
_fp2.corners = [
    {"num": 1, "name": "Turn A", "zone_start": 1290, "zone_end": 1310,
     "brake_bin": 1300, "apex_bin": 1305, "has_brake_zone": True},
    {"num": 2, "name": "Turn B", "zone_start": 1440, "zone_end": 1460,
     "brake_bin": 1450, "apex_bin": 1455, "has_brake_zone": True}]
_b2 = dict(BASE, name="Turn B", loss=0.6, off=False, coast_s=0.9)
fc2.corner_history = {2: [_b2]}
fc2.lap_number = 1
fc2.last_spoke_t = -1000.0
fc2._maybe_approach_cue(1000.0, 1350 / m.NBINS)
_tag = next(iter(fc2._pending_marks))
check("a queued cue is enqueued, not yet fired",
      2 in fc2._approach_enqueued and 2 not in fc2.approach_fired)
fc2.speaker.started_tags.remove(_tag)
fc2.speaker.dropped_tags.append(_tag)
fc2._drain_speaker_tags()
_nl = len(fc2.speaker.lines)
fc2._maybe_approach_cue(1001.0, 1350 / m.NBINS)
check("a dropped cue is re-offered, not suppressed",
      len(fc2.speaker.lines) > _nl and 2 not in fc2._cue_memory)
ec, _ep = new_coach("testEC__car")
ec._exit_spoken = 1
check("the last exit slot is reserved from a small non-focus leak",
      ec._speak_exit(1000.0, 9, "Turn 9: lost 3 tenths there",
                     None, 0.3, 2) is False
      and ec.speaker.lines == [])
check("a big leak may spend the reserved slot",
      ec._speak_exit(1000.0, 9, "Turn 9: lost 6 tenths there",
                     None, 0.6, 2) is True
      and ec._exit_pending == 1 and ec._exit_spoken == 1)
ec._drain_speaker_tags()
check("the exit budget is spent when the line is heard",
      ec._exit_spoken == 2 and ec._exit_pending == 0)
dc, _dp = new_coach("testDC__car")
_dp.corners = [{"num": 1, "name": "Turn 1", "zone_start": 940,
                "zone_end": 1000, "brake_bin": 950, "apex_bin": 980,
                "has_brake_zone": True},
               {"num": 2, "name": "Turn 2", "zone_start": 1001,
                "zone_end": 1100, "brake_bin": 1060, "apex_bin": 1080,
                "has_brake_zone": True}]
dc._exit_deferred = {1: {"line": "Turn 1: lost 4 tenths there",
                         "fix_text": None, "loss": 0.4,
                         "end_bin": 1000}}
dc._service_exit_deferred(1000.0, 1050 / m.NBINS, 2)  # inside Turn 2
check("a deferred exit line waits while the car is in a zone",
      dc._exit_deferred and dc.speaker.lines == [])
dc._service_exit_deferred(1001.0, 1300 / m.NBINS, 2)  # clear of all zones
check("a deferred exit line speaks once the car is clear",
      not dc._exit_deferred
      and dc.speaker.lines == ["Turn 1: lost 4 tenths there"])
_spd_ref = [150.0] * m.NBINS
_spd_lap = [150.0] * m.NBINS
for j in range(1100, 1300):
    _spd_lap[j] = 100.0    # the bleed lives on the straight
_cs = [{"num": 1, "name": "One", "zone_start": 900, "zone_end": 1050,
        "brake_bin": 950, "apex_bin": 1000, "throttle_bin": 1030,
        "apex_speed_kmh": 150.0, "has_brake_zone": True},
       {"num": 2, "name": "Two", "zone_start": 1500, "zone_end": 1650,
        "brake_bin": 1550, "apex_bin": 1600, "throttle_bin": 1630,
        "apex_speed_kmh": 150.0, "has_brake_zone": True}]
_rows = m.analyze_lap(_spd_lap, [0.0] * m.NBINS, [1.0] * m.NBINS,
                      {"spd": _spd_ref, "brk": [0.0] * m.NBINS,
                       "gas": [1.0] * m.NBINS}, _cs, 2.2)
check("extended loss charges the straight bleed to the corner before it",
      abs(_rows[0]["loss"]) < 0.05 and _rows[0]["loss_ext"] > 0.3
      and abs(_rows[1]["loss_ext"]) < 0.05)
ag, _ap = new_coach("testAG__car")
_r = lambda bd: dict(BASE, loss=0.5, off=False, brake_delta_m=bd)
ag.corner_history = {5: [_r(-10.0), _r(-12.0), _r(-60.0)]}
_agg = ag._aggregate_stats(5, ag.corner_history[5][-1])
check("cue diagnosis medians out a one-off symptom",
      _agg["brake_delta_m"] == -12.0)

# ---- v2.25.0 coaching objectives (external review P1) ----
ob, _op2 = new_coach("testOB__car")
_brow = lambda bd, lx: {"num": 2, "name": "Turn 2", "off": False,
                        "brake_delta_m": bd, "loss": lx, "loss_ext": lx}
ob.corner_history = {2: [_brow(-40.0, 0.5), _brow(-42.0, 0.5),
                         _brow(-38.0, 0.5)]}
ob._open_objective(2, "brake", "Turn 2: brake later", _brow(-40.0, 0.5))
check("objective opens with a baseline from recent clean laps",
      len(ob._objectives) == 1
      and ob._objectives[0]["base_sev"] == 32.0
      and ob._objectives[0]["status"] == "open")
ob._evaluate_objectives([_brow(-12.0, 0.2)])
check("one follow-up lap leaves the objective pending",
      ob._objectives[0]["status"] == "open")
ob._evaluate_objectives([_brow(-10.0, 0.2)])
check("two follow-up laps still leave it open (three judge)",
      ob._objectives[0]["status"] == "open")
ob._evaluate_objectives([_brow(-11.0, 0.2)])
check("improved behaviour + improved loss classifies effective",
      ob._objectives[0]["status"] == "effective")
check("closed objectives persist into the combo history",
      (ob.history.get("objectives") or [{}])[-1].get("status")
      == "effective")
ob._evaluate_objectives([_brow(-11.0, 0.2)])
ob._evaluate_objectives([_brow(-12.0, 0.2)])
check("staying improved after closing marks the objective retained",
      ob._objectives[0]["retained"] is True)
ob2, _op3 = new_coach("testOB2__car")
ob2.corner_history = {2: [_brow(-40.0, 0.5), _brow(-40.0, 0.5)]}
ob2._open_objective(2, "brake", "Turn 2: brake later", _brow(-40.0, 0.5))
ob2._evaluate_objectives([_brow(-58.0, 0.7)])
ob2._evaluate_objectives([_brow(-60.0, 0.7)])
ob2._evaluate_objectives([_brow(-59.0, 0.7)])
check("a worsened metric classifies negative",
      ob2._objectives[0]["status"] == "negative")
ob3, _op4 = new_coach("testOB3__car")
ob3.corner_history = {2: [_brow(-40.0, 0.5), _brow(-40.0, 0.5)]}
ob3._open_objective(2, "brake", "Turn 2: brake later", _brow(-40.0, 0.5))
ob3._evaluate_objectives([_brow(-38.0, 0.5)])
ob3._evaluate_objectives([_brow(-41.0, 0.5)])
ob3._evaluate_objectives([_brow(-40.0, 0.5)])
check("an unchanged metric classifies no response",
      ob3._objectives[0]["status"] == "no response")
ob4, _op5 = new_coach("testOB4__car")
_lrow = {"num": 3, "name": "Turn 3", "off": False, "loss": 0.4,
         "loss_ext": 0.4}
ob4.corner_history = {3: [_lrow, _lrow]}
ob4._open_objective(3, "lift", "Turn 3: stay in it", _lrow)
ob4._evaluate_objectives([_lrow])
ob4._evaluate_objectives([_lrow])
ob4._evaluate_objectives([_lrow])
check("a metric-less kind can only classify inconclusive",
      ob4._objectives[0]["status"] == "inconclusive")
fc3, _fp3 = new_coach("testFC3__car")
_fp3.corners = [
    {"num": 2, "name": "Turn B", "zone_start": 1440, "zone_end": 1460,
     "brake_bin": 1450, "apex_bin": 1455, "has_brake_zone": True}]
_b3 = dict(BASE, name="Turn B", loss=0.6, off=False, coast_s=0.9)
fc3.corner_history = {2: [_b3]}
fc3.lap_number = 1
fc3.last_spoke_t = -1000.0
fc3._maybe_approach_cue(1000.0, 1355 / m.NBINS)
fc3._drain_speaker_tags()
check("a heard approach cue opens its objective",
      len(fc3._objectives) == 1
      and fc3._objectives[0]["kind"] == "coast")
rc2, _rp2 = new_coach("testRC2__car")
rc2._finalize_lap(make_rec(), 100.0, "test")
rc2._objectives = [{"num": 1, "name": "Turn 1", "kind": "brake",
                    "cue": "Turn 1: brake later", "lap_coached": 1,
                    "status": "effective", "base_sev": 30.0,
                    "base_disp": -38.0, "unit": "m", "base_loss": 0.5,
                    "follow": [], "follow_disp": -10.0,
                    "retained": True}]
_html = m.build_report_html(rc2)
check("report carries the coaching effectiveness section",
      "Coaching effectiveness" in _html and "retained" in _html)
check("report tables lead with turn number, then corner name",
      "<th>#</th><th>Corner</th><th>Avg loss</th>" in _html
      and "<td>1</td><td>" in _html)

# ---- v2.26.0 panel helpers + stoppable engine ----
import coach_panel as panel
import tempfile, threading, time as _time
_tmpd = tempfile.mkdtemp()
_old_app, _old_tracks = m.APP_DIR, m.TRACKS_DIR
m.APP_DIR = _tmpd
with open(os.path.join(_tmpd, "coach_config.json"), "w") as f:
    json.dump({"custom_key": 7, "talk_level": 2}, f)
_pc = dict(m.DEFAULT_CONFIG)
_pc["talk_level"] = 1
panel.save_managed(_pc)
with open(os.path.join(_tmpd, "coach_config.json")) as f:
    _saved = json.load(f)
check("panel saves managed keys and preserves the rest of the file",
      _saved.get("custom_key") == 7 and _saved.get("talk_level") == 1
      and _saved.get("sass_level") == _pc.get("sass_level"))
m.TRACKS_DIR = _tmpd2 = tempfile.mkdtemp()
for _fn in ("zztest__car.json", "zztest__car__history.json",
            "zztest__names.json", "zztrack__map.json"):
    open(os.path.join(_tmpd2, _fn), "w").close()
check("combo list keeps profiles and drops sidecar files",
      panel.list_combos() == ["zztest__car"])
check("names stems: classic uses the track, layouts get their own",
      panel.names_stem("nd_buttonwillow__car") == "nd_buttonwillow"
      and panel.names_stem("ddm_x__car__layout4130")
      == "ddm_x__layout4130")
_wp = panel.write_names("zztest__car", {"1": "Alpha", "2": "Beta"})
_p2, _mp = panel.read_names("zztest__car")
check("names write/read roundtrip",
      _mp == {"1": "Alpha", "2": "Beta"}
      and os.path.basename(_wp) == "zztest__names.json")
check("choice coercion returns the typed value",
      panel._coerce_choice("-1", [-1, 0, 1]) == -1
      and panel._coerce_choice("brake", ["all", "brake"]) == "brake")

# ---- v2.28.0: extended loss, explained-share honesty, outcomes ----
_hf, _hp9 = new_coach("testHF__car")
_unx = {"num": 2, "name": "Turn 2", "loss": 0.75, "off": False,
        "brake_delta_m": -12.0, "throttle_delta_m": None,
        "coast_s": 0.0, "coast_entry_s": 0.0, "coast_exit_s": 0.5,
        "apex_delta_kmh": -1.0, "entry_delta_kmh": 0.0,
        "apex_shift_m": 0.0, "lockup_s": 0.0, "spin_s": 0.0,
        "peak_brake": 0.9, "peak_brake_ref": 1.0, "release_s": 0.2,
        "release_ref_s": 0.2, "mid_lift": False, "steer_rev_delta": 0,
        "ref_apex_kmh": 90.0, "span_m": 200.0}
check("a symptom explaining <40% of the loss is not named as cause",
      _hf._honest_fix(_unx)
      == "Turn 2: the loss is on the exit — cause unclear "
         "from the pedals")
_exp = dict(_unx, brake_delta_m=-48.0, loss=0.7, coast_exit_s=0.0)
check("a symptom explaining the loss keeps its fix",
      _hf._honest_fix(_exp) == "Turn 2: brake later")
_sm, _sp9 = new_coach("testSM__car")
_sm._speak_lap_summary(100.0, [], outcomes=[{
    "name": "Phil Hill", "kind": "entry", "unit": "kmh",
    "base_disp": -22.0, "follow_disp": 3.0}])
check("an objective turning effective is spoken in the summary",
      any("that's better" in ln and "Phil Hill" in ln
          for ln in _sm.speaker.lines))
_ap, _pp9 = new_coach("testAP__car")
_ap._finalize_lap(make_rec(), 100.0, "test")
_ap.corner_history = {1: [dict(BASE, loss=0.10, loss_ext=0.50,
                               off=False)]}
_seg = m.build_report_html(_ap)
_seg = _seg[_seg.find("Corner data"):_seg.find("Session summary")]
check("report average loss is the extended loss",
      "+0.50s" in _seg)

# ---- v2.29.0: geometry self-heal, span guard, speaker stop ----
_gp = m.TrackProfile("testGEO__car")
_gspd = [150.0] * m.NBINS
_gbrk = [0.0] * m.NBINS
_ggas = [1.0] * m.NBINS
for _j in range(250, 291):
    _gbrk[_j] = 1.0
    _ggas[_j] = 0.0
for _j in range(850, 891):
    _gbrk[_j] = 1.0
    _ggas[_j] = 0.0
_gspd[300] = 60.0
_gspd[900] = 70.0
_gp.ref = {"spd": _gspd, "brk": _gbrk, "gas": _ggas}
_gp.track_length_m = 4400.0
_gp.corners = [
    {"num": 1, "name": "Alpha", "apex_bin": 300, "zone_start": 100,
     "zone_end": 400, "brake_bin": 280, "throttle_bin": 380,
     "apex_speed_kmh": 60.0, "has_brake_zone": True, "minor": False},
    {"num": 2, "name": "Beta", "apex_bin": 900, "zone_start": 100,
     "zone_end": 1000, "brake_bin": 280, "throttle_bin": 950,
     "apex_speed_kmh": 70.0, "has_brake_zone": True, "minor": False}]
_gc, _gx = new_coach("testGEO2__car")
_gc.set_profile(_gp)
check("scrambled corner anchors are rebuilt from the apexes",
      _gp.corners[1]["zone_start"] > _gp.corners[0]["zone_start"]
      and _gp.corners[1]["brake_bin"] > 800
      and _gp.corners[0]["name"] == "Alpha")
_cs2 = [dict(_cs[0]), dict(_cs[1])]
_cs2[1]["zone_start"] = 900     # same start as corner One: dead span
_rows2 = m.analyze_lap(_spd_lap, [0.0] * m.NBINS, [1.0] * m.NBINS,
                       {"spd": _spd_ref, "brk": [0.0] * m.NBINS,
                        "gas": [1.0] * m.NBINS}, _cs2, 2.2)
check("a degenerate extended-loss span falls back to zone loss",
      _rows2[0]["loss_ext"] == _rows2[0]["loss"])
import queue as _queue_mod
_spk = object.__new__(m.Speaker)
_spk.q = _queue_mod.Queue(maxsize=48)
_spk._stopped = False
_spk.q.put((0, "x", None, "info", None))
_spk.q.put((0, "y", None, "info", None))
_spk.stop()
check("speaker stop drops the backlog, leaves only the sentinel",
      _spk._stopped and _spk.q.qsize() == 1)
m.APP_DIR, m.TRACKS_DIR = _old_app, _old_tracks
_stop = threading.Event()
_stat = {}
_ecfg = dict(m.DEFAULT_CONFIG)
_ecfg["voice_enabled"] = False
_th = threading.Thread(target=m.run_live,
                       kwargs={"cfg": _ecfg, "stop_event": _stop,
                               "status": _stat}, daemon=True)
_th.start()
_time.sleep(1.5)
_stop.set()
_th.join(timeout=20)
check("run_live stops on the panel's stop event and reports state",
      not _th.is_alive() and _stat.get("running") is False
      and "coach" in _stat)

# ---- v2.30.0: report map -- wider outline + brake-pressure core ----
_mc, _mp = new_coach("testMAP__car")
_mc._finalize_lap(make_rec(), 100.0, "test")
_circ = [[1000.0 * _math.cos(2 * _math.pi * k / 120),
          1000.0 * _math.sin(2 * _math.pi * k / 120)] for k in range(120)]
_mp.outline = _circ
_mspd = [200.0] * m.NBINS
_mbrk = [0.0] * m.NBINS
for _j in range(940, 1000):
    _mbrk[_j] = 0.9
_mc._session_best = {"time": 100.0, "spd": _mspd, "brk": _mbrk}
_mc._lap_paths = {100.0: _circ}
_mhtml = m.build_report_html(_mc)
check("report map: reference ribbon drawn at the wider width",
      'stroke="#b9c9dd" stroke-width="26"' in _mhtml)
check("report map: brake-pressure core drawn from the best lap's brake bins",
      "#ff2e1f" in _mhtml and "brake pressure on the best" in _mhtml)
_mc._session_best = {"time": 100.0, "spd": [200.0] * m.NBINS,
                     "brk": [0.0] * m.NBINS}
_mhtml2 = m.build_report_html(_mc)
check("report map: no brake core when the best lap never braked",
      "#ff2e1f" not in _mhtml2)

# ---- v2.30.1: geometry check accepts shared-brake pairs ----
_bin_m2 = 4405.0 / m.NBINS
_gs2 = [180.0] * m.NBINS
_gb2 = [0.0] * m.NBINS
_gg2 = [1.0] * m.NBINS


def _plant2(frac, arr, val, half=20):
    c = int(frac * m.NBINS)
    for _j in range(c - half, c + half):
        arr[_j] = val


_plant2(0.238, _gb2, 0.9)
_plant2(0.238, _gg2, 0.0)
_plant2(0.715, _gb2, 0.9)
_plant2(0.715, _gg2, 0.0)
_pairs = []
for _fr in (0.254, 0.272, 0.733, 0.760):
    _apx = int(_fr * m.NBINS)
    _zc = m._zone_from_apex(_apx, 70.0, _gs2, _gb2, _gg2, _bin_m2)
    _zc["num"] = len(_pairs) + 1
    _pairs.append(_zc)
check("geometry: paired zones sharing one brake event are sane",
      _pairs[0]["zone_start"] == _pairs[1]["zone_start"]
      and m._geometry_sane(_pairs, _bin_m2,
                           {"spd": _gs2, "brk": _gb2, "gas": _gg2}))
_scr = [dict(c) for c in _pairs]
_scr[0]["brake_bin"] = _scr[0]["apex_bin"] - 600
check("geometry: a brake anchor far from its zone is still corrupt",
      not m._geometry_sane(_scr, _bin_m2))
_scr2 = [dict(c) for c in _pairs]
_scr2[1]["zone_start"] = _scr2[0]["zone_start"] - 50
check("geometry: zone starts running backwards are still corrupt",
      not m._geometry_sane(_scr2, _bin_m2))
_scr3 = [dict(c) for c in _pairs]
_scr3[1]["brake_bin"] = _scr3[1]["brake_bin"] + 30
check("geometry: equal starts with different brake anchors are corrupt",
      not m._geometry_sane(_scr3, _bin_m2))

# ---- v2.30.1: panel report picking skips empty/stale reports ----
_tmpd2 = tempfile.mkdtemp()
os.makedirs(os.path.join(_tmpd2, "logs"), exist_ok=True)
_old_app2 = m.APP_DIR
m.APP_DIR = _tmpd2
_good = os.path.join(_tmpd2, "logs", "report_20260101_100000.html")
_husk = os.path.join(_tmpd2, "logs", "report_20260101_110000.html")
with open(_good, "w") as _f:
    _f.write("<html>good</html>")
with open(_husk, "w") as _f:
    _f.write("")
os.utime(_good, (1000000, 1000000))
os.utime(_husk, (2000000, 2000000))
check("panel latest_report skips zero-byte reports",
      panel.latest_report() == _good)
check("panel pick_report falls back from a stale status path",
      panel.pick_report({"report_path": os.path.join(
          _tmpd2, "logs", "report_20260101_090000.html")}) == _good)
check("panel pick_report prefers the live session's real report",
      panel.pick_report({"report_path": _good}) == _good)
check("panel pick_report skips a zero-byte status path",
      panel.pick_report({"report_path": _husk}) == _good)
m.APP_DIR = _old_app2

print()
if FAILS:
    print(f"{len(FAILS)} FAILURE(S): {', '.join(FAILS)}")
    sys.exit(1)
print("ALL TESTS PASSED")
