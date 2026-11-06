#!/usr/bin/env python3
"""Replay regression set (Nov 6, 2026 — P0 from the external review).

The unit suite (run_tests.py) is synthetic: it proves the code does
what the tests say, not that the coach still reads REAL sessions
the way it did when the behaviour was validated live. This script
replays a pinned set of Albert's recorded sessions through the
current coach and checks the structural facts and headline
diagnoses that were true when the sessions were reviewed by hand:

- how many laps were counted,
- the best lap,
- specific mechanism markers (distance fallback, learned length),
- the summary's biggest-leak corner where one was verified.

If a coaching change alters a pin, the failure here is the prompt
to look at WHY before re-pinning: update the expectation only
when the new behaviour is understood and intended.

Run:  python3 tests/replay_regression.py
Session files are looked for in $COACH_SESSIONS, then the user's
session archive, then the app's own logs/ folder. A missing file
is a SKIP, not a failure.
"""

import contextlib
import io
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import ac_live_coach as m  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.abspath(os.path.join(HERE, "..", "..", "..", "..", ".."))
CANDIDATE_DIRS = [
    os.environ.get("COACH_SESSIONS") or "",
    os.path.join(WORKSPACE, "user", "files"),
    os.path.join(HERE, "..", "logs"),
]

PINS = [
    {
        "file": "session_20261106_081127.jsonl",
        "note": "Buttonwillow MX-5 (Nov 6 morning): out lap, the "
                "2:03.2 clean reference, then the 2:05.4 lap with "
                "the Sweeper Exit off. Summary leak verified by "
                "hand against the session report.",
        "laps": 2,
        "best_s": 123.2,
        "contains": ["Biggest leak: cotton corners 1"],
    },
    {
        "file": "session_20261104_180305.jsonl",
        "note": "Buttonwillow MX-5 (Nov 4 evening): seven counted "
                "laps improving to 2:03.6 — the session the v2.16 "
                "work was validated against.",
        "laps": 7,
        "best_s": 123.6,
        "contains": [],
    },
    {
        "file": "session_20261104_215930.jsonl",
        "note": "Rainbow Road (Nov 4): no spline position — the "
                "distance fallback must latch, learn 2886 m from "
                "the timer, and count all nine laps (the file "
                "behind the v2.16.2/v2.16.3 fixes).",
        "laps": 9,
        "best_s": 96.7,
        "contains": ["coaching by distance",
                     "lap length learned from the lap timer: 2886 m"],
    },
]


def _find(name):
    for d in CANDIDATE_DIRS:
        if d and os.path.exists(os.path.join(d, name)):
            return os.path.join(d, name)
    return None


def _parse_replay_line(out):
    mm = re.search(r"\[replay\] \d+ frames, (\d+) laps, best (\d+):(\d+\.\d+)",
                   out)
    if not mm:
        return None, None
    return int(mm.group(1)), int(mm.group(2)) * 60 + float(mm.group(3))


def main():
    fails = 0
    for pin in PINS:
        path = _find(pin["file"])
        if path is None:
            print(f"SKIP {pin['file']} (not found in any session folder)")
            continue
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            m.run_replay(dict(m.DEFAULT_CONFIG), path)
        out = buf.getvalue()
        laps, best = _parse_replay_line(out)
        problems = []
        if laps != pin["laps"]:
            problems.append(f"laps {laps} != {pin['laps']}")
        if best is None or abs(best - pin["best_s"]) > 0.25:
            problems.append(f"best {best} != {pin['best_s']}")
        for needle in pin["contains"]:
            if needle not in out:
                problems.append(f"missing: {needle!r}")
        if problems:
            fails += 1
            print(f"FAIL {pin['file']}: " + "; ".join(problems))
            print(f"     pin: {pin['note']}")
        else:
            print(f"PASS {pin['file']} ({pin['laps']} laps, "
                  f"best {pin['best_s']:.1f})")
    print()
    if fails:
        print(f"{fails} REGRESSION(S) — investigate before re-pinning")
        return 1
    print("REPLAY REGRESSION CLEAN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
