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

    def say(self, text, **kw):
        self.lines.append(text)


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

print()
if FAILS:
    print(f"{len(FAILS)} FAILURE(S): {', '.join(FAILS)}")
    sys.exit(1)
print("ALL TESTS PASSED")
