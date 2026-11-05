# AC Live Coach

A live voice coach for Assetto Corsa (the original AC). It works with
**any car at any track**. It reads the game's shared-memory telemetry,
builds its reference from **your own best lap**, detects the corners
from your speed trace, and speaks short cues only where your data says
time is sitting. Everything runs locally on your PC: no account, no
subscription, no cloud, nothing leaves the machine.

> **Built with AI.** This entire project was coded by an AI assistant
> (Muse), working from the requirements, session logs and feedback of
> the human who drives with it. The human directed, tested and drove;
> the code — and this README — were written by the AI. It works, and
> it is tested in live sessions after every change, but expect rough
> edges and read the changelog for how it got here.

## What it does

- **First lap at a new car/track combo is mapping.** It becomes your
  reference. If a saved reference exists for the combo, coaching
  starts immediately against your standing best.
- **Approach cues** ~260 m before the brake zone of a problem corner:
  the single biggest fix from your recent laps there ("Turn 6: brake
  later"). Max 5 per lap, never two lines within 6 seconds.
- **Exit feedback** right after a corner that cost real time
  (>= 0.2 s, or >= 0.35 s at minor corners), capped at 3 per lap.
- **Section feedback**: if a third of the lap bleeds time across
  several corners without one big leak, you hear one line for the
  section instead.
- **Lap summary** at the line: lap time (spoken "2 oh 4 point 3"),
  the gap to your best, and your biggest leak.
- **Out laps are never coached or counted**: the first lap on a
  loaded profile, any lap 15%+ off your best, and laps after a pit
  stop or reset. Corners where you went off (2+ tyres out, timed)
  are excused, not blamed.
- **Post-session report**: an HTML page with the track outline traced
  from your own driven line, corner markers coloured by average loss,
  the ranked coaching points and the full corner table.

## Requirements

- Windows PC with Assetto Corsa (original AC, e.g. via Steam).
- Python 3 (python.org; tick "Add python.exe to PATH" on install).

## Install (once)

1. Put this folder anywhere on your PC.
2. Open a terminal in the folder and install the voice engines:
   `pip install -r requirements.txt`
   (Skip this and the coach still works — cues print in the console
   window instead of speaking.)
3. No in-game setting is needed. Original AC publishes its
   shared-memory telemetry automatically while you're on track (the
   "Shared Memory" toggle you may see mentioned online is an AC EVO
   thing — it doesn't exist in AC 2014). Menus publish nothing; the
   coach connects once a session loads.

## Run (every session)

1. Start Assetto Corsa and load any track, any car.
2. Double-click **run_coach.bat** (or run `python ac_live_coach.py`).
   If Windows Smart App Control or SmartScreen objects to the .bat,
   run the Python command from a terminal instead. Leave the console
   window open behind the game.
3. On first launch the neural voice model (~63 MB) downloads once
   into a `voices` folder; after that the voice works fully offline.
4. Drive. The first lap at a new combo maps; from lap 2 the coaching
   starts.
5. When you're done, click the console window and press **Ctrl+C**.
   A summary prints and the debrief is finalized. Keep the coach in
   one folder — its `profiles` subfolder holds your saved reference
   laps and corner maps.

## Corner names (optional)

Everywhere, corners are **Turn 1, Turn 2, ...** in driving order by
default, and the coaching is identical. If you want real names at a
track, name them yourself: after a first session, open
`<profiles folder>\<track>__names.json` (the coach writes a starter
file) and put a name against each zone number, e.g.
`{"1": "Sunrise", "2": "Off Ramp"}`. Restart the coach and the names
stick — also across remaps, matched by zone number. (A name table for
Buttonwillow is built in; everywhere else starts as Turn N.)

## Files it writes

- `logs/session_*.jsonl` — every lap, corner analysis and cue,
  timestamped.
- `logs/debrief_*.txt` — the readable version of the session.
- `logs/report_*.html` — the post-session report page.
- Your **profiles** (reference lap + corner map per car/track) live
  in a `profiles` folder inside the coach folder itself, created
  automatically on first run. They survive code updates (including
  `git pull`) because updates never touch that folder; if you move
  the coach folder, the profiles move with it. Profiles saved by
  older versions in the Assetto Corsa folder are copied in on first
  run. The console prints the exact path at startup.
- `voices/` — the downloaded neural voice model (Piper).

Those log files are the tuning loop: every cue the coach spoke, and
the numbers behind it, are in there.

## Tuning knobs (coach_config.json)

- `voice_engine` — `"piper"` (offline neural voice, default) or
  `"sapi"` (built-in Windows voices; also the automatic fallback if
  Piper is unavailable)
- `piper_voice` — any Piper voice model name
  (default `en_US-lessac-medium`); `piper_length_scale` is its rate
  (< 1 faster, > 1 slower)
- `voice` / `voice_rate` — SAPI voice name fragment ("zira", "david")
  and speed when the SAPI engine is used
- `approach_distance_m` — earlier/later approach cues (default 260)
- `min_loss_to_speak` — raise it (e.g. 0.25) if it talks too much
- `exit_min_loss_s` — the exit-feedback threshold (default 0.2)
- `max_approach_cues_per_lap` — default 5
- `cue_cooldown_s` — minimum seconds between spoken lines (default 6)
- `focus` — `"all"`, or `"brake"` for braking fixes only
- `section_feedback` / `section_min_loss_s` — section callouts
- `guide_spoken_tips` — speak each guided corner's first tip
  once per session (default off; see `guides/`)
- `brake_tone` — one short tone at the reference lap's brake
  point for each corner (default on; a training aid — corners
  the reference lap didn't brake for stay silent).
  `brake_tone_volume` sets its level against the voice;
  `brake_tone_lead_m` moves it earlier by that many metres;
  `brake_tone_late_m` is how far past the point a tone held by a
  busy voice channel may still fire (default 25)
- `position_fallback` — when a track mod reports no spline
  position, derive position from the lap timer + odometer so
  coaching still works (default on)
- `talk_level` — how much the coach talks: 2 = full (exit
  feedback ×3/lap, section lines, sass colour), 1 = reduced
  (default: exit ×2, approach cues ×4, no section lines, sass
  silent), 0 = essentials (approach ×3, exit ×1, summaries carry
  the lap time only). Stepping back is what keeps the voice
  channel free for the lines that are only true at one moment
- `sass_level` — the coach's personality spectrum: 0 = pro,
  1 = lap summaries draw from dry variant pools,
  2 = repeat-offender and well-off-pace lines join in and exit
  feedback gets its "again" forms, 3 = the sharpest pool entries
  (default 3).
  Sass never changes the numbers or the fixes, and never enters
  approach cues. The out-lap briefing's phrasing rotates at every
  level — same facts, varied frames
- `voice_enabled` — false = console only

## Known limits

- The benchmark is always your own best lap — never a theoretical or
  someone else's lap. The debrief estimates a theoretical best from
  your sector bests; the live coach chases your best.
- Corner detection reads your speed trace; a very fast sweeper with
  only a small lift may not register as a corner.
- AC's shared memory carries no opponent positions and no track-edge
  data, so there is no spotter and no true racing-line coaching —
  line feedback is symptoms (apex point, mid-corner lifts, steering
  corrections) measured against your own best lap.
- It coaches in practice/hotlap-style running; it knows nothing
  about traffic, flags or race strategy.

## Test without the game

`python ac_live_coach.py --simulate` runs the coaching brain against a
synthetic track with planted driving faults and prints what it would
say.

## Changelog

### v2.16.2
- **Fix: a mid-session corner remap killed every report.** When a much faster lap replaces the reference, the corners are re-detected — Rainbow Road went from 8 corners to 9 when lap 2 (1:40.3) replaced the 1:45.4 reference. The session summary's theoretical-best loop then asked the best lap's record for a corner number it never had and raised StopIteration, which `build_report_html` carried into every report write of the session (and the exit summary too): coaching ran perfectly, no report was ever produced. The loop now skips corners the best lap predates, the approach-cue lookup gets the same guard, and a distilled mixed-map test covers it. 89 checks.

### v2.16.1
- **Fix: the v2.15.0 session rotation muted the coach.** Rotation rebound the coach's logger but not the Speaker's — the Speaker kept logging to the closed first session file, so every `say()` raised "I/O operation on closed file" and no speech survived (the driver's Streets of Willow session: 30 loop errors, no briefing, no cues). Rotation now rebinds the Speaker's logger as well, and speech logging is failure-proofed outright: a dead log target can never mute the coach again. 88 checks.

### v2.16.0
- **Stepped back: `talk_level` (default 1, reduced).** The first full-sass Buttonwillow session ran ~6 speeches a lap plus summaries, and the serial voice channel backed up behind them: approach cues went stale in the queue and 25 of 43 brake tones expired while the coach was mid-sentence — the talking was delaying the coaching. Verbosity is now a level: full (2) restores the old volume; reduced (1, default) caps exit feedback at 2 per lap and approach cues at 4, drops section colour, and silences sass (it speaks only at full level); essentials (0) trims further, to lap-time-only summaries. The analysis is untouched — same cues chosen, fewer of them read out.
- **Approach cues now jump the queue.** Speech is tagged by kind; when a time-critical approach cue is enqueued, any queued-but-unstarted info lines (summaries, exit feedback) are dropped and logged (`cue_dropped_purged`) instead of making the cue wait. A line already speaking is never interrupted, and brake tones keep their own path — but with the channel stepped back, both should stop expiring.
- Tests grow to 87 checks, including the queue purge and the talk-level/sass gating.

### v2.15.0
- **Distance fallback: the learner now anchors to the lap's actual start.** It measured its first candidate lap from the moment the dead-spline verdict landed — typically a few hundred metres into the lap — so that candidate came out short (2,270 m against a true 2,870 m lap on the driver's Rainbow Road return) and could never agree with the next lap; learning silently never completed, so no laps, cues or brake tones armed. The coach now tracks where the current lap began before the verdict (a timer reset, or the stream opening at a lap start) and measures candidates reset-to-reset. Verified against the real session file: length learned 2,894 m, reference built from his 1:38.2 lap, eight corners mapped — where v2.14.1 produced nothing.
- **A combo switch before the first lap starts a fresh session.** Loading one track, sitting in its pits, then switching to another used to stamp the whole session with the first combo (a Rainbow Road drive filed as Laguna Seca). If no lap has been recorded yet, the session files now close and a new set starts under the combo actually driven; a switch after laps exist keeps the single session, as before.
- Tests grow to 84 checks, including a distilled replay of the failure pattern (stream opens at a lap start, verdict lands mid-lap, driver quits early in lap 3).

### v2.14.1
- **New defaults, by the driver's call:** the brake tone is on out of the box (`brake_tone` default true — its two silent sessions traced to an opt-in flag and a busy-channel drop, both now closed), and the sass spectrum defaults to full (`sass_level` 3). Either is still one config line to turn down or off; local configs carrying their own lines keep them.

### v2.14.0
- **Coast is a location, not a diagnosis:** the most-spoken fix used to be "less coasting, commit to a pedal" wherever it appeared, because raw coast time out-costed every other symptom. The zone analysis now splits coast at the apex (rolling in vs waiting after it), measures the speed carried into the zone, and knows each corner's predecessor — so the cues name the mechanism: braking done early + an entry roll becomes "you're done braking early and rolling to the apex — brake later, carry it in"; an exit coast with a late throttle point becomes "you're waiting to get back to power — pick it up at the apex"; an entry-speed deficit is called what it is — "you're arriving slow — that's the previous corner's exit, not this corner". The generic line survives for a coast spread evenly across a zone. Exit feedback's "big coast" line got the same split ("big roll to the apex" / "slow back to power"). Verified on the driver's real Laguna Seca laps, where Turn 6's summary now tells the braking story instead of the coasting one.
- **Sass, as a spectrum:** `sass_level` 0–3 (default 0, the pro voice). Level 1 gives lap summaries rotating dry variant pools (new-best, clean-lap lines — no repeats until a pool is used up); level 2 adds repeat-offender callouts when the same corner tops consecutive summaries, a well-off-pace variant, and "again" forms on a corner's second exit line of the session; level 3 sharpens the pools. Sass wraps facts, never replaces them, and approach cues stay pure function at every level.
- **Briefing variety:** the out-lap briefing's facts are fixed, so its phrasing now rotates per session — segment openers, the best/mapped/leaks frames — with a drier variant joining each pool at sass level 2+.
- Tests grow to 81 checks, including the coast split, the three new cue stories, sass rotation/gating, and briefing pool membership.

### v2.13.0
- **Distance position fallback:** some mod tracks ship no working AI spline, so AC reports the car's track position as a constant (the Rainbow Road conversion: 0.0 in every frame of a six-lap session — no laps registered, no reference, no cues). The coach now detects a dead spline (position never varies over 200 m of travel) and derives position from the game's own lap timer segmented over the odometer, normalised by a lap length learned from two consecutive laps that agree within 2% (a saved profile length validates against a single lap). Frames are buffered while the length is learned, then replayed through the normal pipeline, so bins, zones, cues, tones and the report all work unchanged; the coach says once that it is coaching by distance. Verified by replaying the real Rainbow Road session: length learned 2,902 m, reference and 11 corners built, laps and tones reconstructed. New knob: `position_fallback` (default on).
- **Brake tone, held instead of dropped:** a tone that reached its brake point while the coach was mid-sentence was dropped for the lap — which, per the driver's first live report, is exactly when the beeps were expected. It is now held and fired the moment the voice channel frees, as long as the car is still within `brake_tone_late_m` metres past the point (default 25) and 2 s of the crossing; only past that window is it dropped, still logged. Session logs mark delayed tones.
- Tests grow to 68 checks, including the late-window fire/drop and a full synthetic spline-less session (reference built, laps counted, corners detected).

### v2.12.0
- **Brake-point tone (opt-in):** with `brake_tone` on, the coach plays one short tone (150 ms, 880 Hz) the moment the car crosses the reference lap's brake point for the next corner — an audio marker that survives losing the visual one mid-battle. Corners the reference lap lifted for instead of braking stay silent: no beep means no brake. It runs under the same out-lap and off-pace mute as the voice cues, fires once per corner per lap, and only into an idle audio channel — if the coach is mid-sentence at the brake point, the tone is dropped for that lap rather than landing late and teaching the wrong point (drops are logged). Level is set against the voice with `brake_tone_volume`; `brake_tone_lead_m` moves the tone earlier. The tone bypasses the comms fx — a radio treatment would smear a marker this short. Source: the brake tones were the most praised feature in two independent reviews of rival coaches (the Nov 3 landscape research, and Sim Racing Kez's four-coach comparison, Nov 4).
- Tests grow to 57 checks, including tone targeting, the busy-channel drop and the rendered wav's format.

### v2.11.0
- **Report chart, three laps:** the speed/pedal chart now draws the three laps that matter — the **best lap ever** (the stored reference, blue), the **optimal lap** (purple, dashed: the reference with each zone swapped for the best clean execution of it from this session, its stitched time in the legend), and the **best lap of this session** (orange). Speed on top for all three; below, a brake line per lap in its own color over the reference throttle fill. Corner markers still come from the reference lap. The old last-lap overlay is retired — the session best is the more useful trace. Off zones never enter the optimal: a cut is not a line.
- Tests grow to 50 checks, including zone stitching from complementary laps.

### v2.10.2
- **Comms fx:** hiss bed trimmed about a third (driver's ear: slightly less), everything else unchanged.

### v2.10.1
- **Comms fx, hotter:** the first cut was too polite. The chain now band-limits harder (~400 Hz–2.9 kHz), drives the saturation roughly twice as hard (transmitted radios clip), then crushes the resolution — half-rate, coarsely quantized, stretched back — for the cheap-digital-radio crunch, with a low open-channel hiss under the voice. The click is unchanged. If it overshoots, `voice_comms_fx` still turns the whole treatment off.

### v2.10.0
- **Pit-radio voice (comms fx):** Piper cues now pass through a comms treatment before playback — band-limited to the radio channel (~350 Hz high-pass, two passes; ~3.4 kHz low-pass, two passes), a touch of saturation for grit, level normalized — preceded by a short key-up blip (30 ms tone + 25 ms) like an engineer keying the mic. On by default (`voice_comms_fx`), with the click separately switchable (`voice_comms_click`). Piper path only: SAPI speaks directly and can't be processed. Pure stdlib DSP; any unexpected audio format passes through untouched.
- Tests grow to 43 checks, including band-limit and click-prefix measurements on the effect itself.

### v2.9.0
- **Exit feedback breathes:** the exit line is now spoken as two clauses with a real slice of silence spliced between the measurement and the instruction ("Sunset: lost 3 tenths there" — 220 ms — "brake later"), instead of one welded sentence. The pause is exact on the Piper engine (rendered into the audio) and natural on the SAPI fallback (separate utterances). Tunable via `exit_clause_gap_ms`; the session log keeps the familiar single-line form.
- **New default voice: en_US-lessac-high** — the same voice as before, one quality tier up (114 MB one-time download, SHA-256 pinned like the medium model). Any other Piper voice still works via `piper_voice`.
- Tests grow to 39 checks, including the silence splice.

### v2.8.0
- **Audit first:** `docs/AUDIT-2026-11-03.md` maps the comprehensive development review against the code — what exists, what's missing, what's a deliberate non-goal — and the prioritized plan that follows from it. This release implements its first slice.
- **Version, single source of truth:** the session/report stamp came from a hardcoded constant that releases had stopped bumping (it read 2.5.3 through three releases) while the config carried a second, unread version key. The code constant is now the only version; the config key is removed.
- **Recording at full rate:** session frames are recorded at the acquisition rate (~30 Hz), not 10 Hz — the shortest analyzed events (0.18 s lockups) were two samples wide at 10 Hz, too thin to replay faithfully.
- **Speech scheduler:** deferrable speech (approach cues, guide tips) now waits for a straight — it holds while the car is inside any corner zone and drops silently inside 100 m of the brake point, instead of landing mid-corner in linked sequences.
- **Cue memory:** the same approach instruction at the same corner is suppressed for 3 laps unless the loss worsened by 0.15 s or more — repeating an instruction teaches nothing.
- **Braking depth:** each zone now also measures peak brake pressure and brake-release duration against the reference. Two new spoken symptoms, each gated on a corroborating signal: "you're not using all the brake there" (peak far below reference, no speed gained for it) and "let the brake off more smoothly there" (release far quicker than reference with a slower apex).
- Tests grow to 34 checks, including the scheduler gate, cue memory and the new brake symptoms.

### v2.7.0
- **Lockup and wheelspin cues (external review):** the physics page's wheel speeds were already arriving unused. Each wheel's tangential speed (angular speed x tyre radius) is now compared with the car's speed live: a front wheel turning much slower under braking is a lockup, a rear turning much faster under power is wheelspin. Sustained events (0.18 s+, flicker-tolerant) are attributed to corners and spoken as causes -- "you're locking the fronts there" / "wheelspin there" -- competing in the cost ranking by their duration.
- **Record and replay:** live sessions now record downsampled raw frames (10 Hz: pedals, speed, wheel speeds, flags) into the session log. `python ac_live_coach.py --replay logs/session_....jsonl` re-runs them through the coaching brain against a throwaway profile and prints every cue -- new detectors can be validated on real laps before they coach live. Set `record_frames` false to opt out.
- **Penalty guard:** a lap carrying an AC penalty now blocks/updates the reference exactly like a lap with an off (the validity half of the reference-integrity rule).
- **Report:** the trace chart overlays your last counted lap (dashed gray) on the reference, and the corner table gains a brake-spread column -- how much your brake point wandered at each corner across recent laps, in feet.
- **Profile files carry a schema stamp** from this version on.
- Tests grow to 27 checks, including the slip detector, the penalty guard and a record/replay round-trip.

### v2.6.1
- An empty tyre-compound read no longer drops the profile to the no-compound key: AC clears the field when a session ends, which stamped a spurious fresh profile in the first v2.6.0 log. A change to a different non-empty compound still switches profiles.

### v2.6.0
- **Reference integrity (external review):** a lap containing an off -- including a cut corner -- can no longer become the reference lap. If the standing reference itself came from such a lap (typically the first-ever lap, which has to set something), the next clean lap within 3% takes over as the honest benchmark.
- **Cues ranked by measured cost:** the spoken fix at a corner is now the symptom with the largest estimated time cost (braking/throttle distances at the span's reference average speed, apex-speed deficit over the span's time, coast seconds), instead of the first symptom in a fixed order. A track guide's priority biases its favoured symptoms 1.5x rather than picking the class outright.
- **Exit feedback says why:** the instant post-corner callout appends the winning fix ("lost 3 tenths there -- brake later") instead of saving the cause for next lap's approach cue.
- **Profiles are keyed by tyre compound too:** the key gains the compound AC reports (e.g. `__tc_s`), so a soft-tyre reference no longer benchmarks medium-tyre laps. The first v2.6.0 run adopts your existing profile and history under the new key; other compounds start fresh.
- **Layout guard:** AC reports the track folder, not the layout, so layouts used to share one profile. If a session's first lap measures >3% different from the loaded profile's stored length, the coach switches to a layout-specific profile (`__layout<length>`) before any data lands in the wrong one.
- **Steadier approach cues:** a single lap now triggers a next-lap warning only on a big loss (>= 0.45 s); the old 0.25 s level needs a corroborating loss in another clean lap. The multi-lap average path is unchanged.
- **Names/guide count guard:** name files and track guides are keyed by zone number; if their zone count no longer matches the profile's corner count, they're ignored with a console warning instead of silently naming the wrong corners.
- **Tests ship with the repo:** `tests/run_tests.py` covers the reference rules, cue ranking, compound keys, the layout guard, the names guard and profile recovery -- run it any time, no game required.

**v1.2** — live corner-by-corner coaching, rebuilt from Albert's second
session log (Nov 1, 2026):
- Approach cues no longer require a multi-lap average loss: one real
  loss (>=0.25s) or one big coast (>=1.5s) on the last lap earns the
  next lap a heads-up at that corner. Up to 5 approach cues per lap.
- Exit feedback triggers on *crossing* the corner exit, not on landing
  on one exact position bin (30 Hz sampling could step over it), and
  the threshold dropped 0.3s -> 0.2s. A >=2s coast with >=0.1s loss
  gets its own callout.
- Speech queue drops cues that waited more than 15 s instead of
  reading out stale corners a lap late.
- Corner map stays stable within a session: it re-maps only when a new
  best lap is 3%+ faster than the lap the map was built from, and the
  coach announces the re-map when it happens.

**v1.1** — voice fix: fresh speech engine per cue (a reused pyttsx3
engine goes silent after the first utterance on Windows); spoken
"Voice check" at launch; voice errors logged instead of swallowed.

Run the coach from the same folder every time: your saved reference
lap and corner map live in its `tracks` folder. Unzipping a new copy
elsewhere starts you fresh with a new reference lap.

**v1.3** — lap-time pairing fix (Albert's report, Nov 1: crossing the
line read out the *previous* lap's time). The position wrap that ends
a lap and AC's own lap registration land in either order; the old code
kept one shared "last official time" variable, so once registration
lagged the wrap, every lap inherited the previous lap's time for the
rest of the session. Now finishes and official times are paired
strictly by order, each official value settles ~1.2 s before use, and
every lap record in the session log carries `time_source`, `dur_s`
(the coach's own timer) and `ict_peak_s` (AC's in-car timer peak) so
any remaining mismatch can be diagnosed from the log itself. Side
effect: the lap summary now speaks ~1.5 s after you cross the line,
once the official time has settled. Approach cues still come from the
previous lap's data by design (that's what makes a warning *before*
the corner possible); exit feedback and lap times are same-lap.

**v1.4** — post-session report window. When the coach stops (Ctrl+C),
it builds `logs/report_<session>.html` and opens it in the default
browser (`open_report_window` in coach_config.json turns that off).
The page shows the track outline traced from your own driven line
(heading + distance integration, captured while you drive and stored
in the track profile), with numbered markers at each corner's apex
using the SAME turn numbers the voice used, colored by average time
loss (green <=0.05s, amber <=0.25s, red above), plus the ranked key
coaching points and the full corner table. The outline may be rotated
versus the real circuit; if it comes out mirrored, that's a one-sign
fix once we've seen one real trace.

**v1.5** — three fixes from Albert's Nov 1 evening sessions:
- Spoken lap times: the voice now says "2 oh 6 point 7" instead of
  reading the colon in "2:06.7" ("two colon six..."). Displayed times
  are unchanged.
- Real corner names on Buttonwillow: the seven-zone map was verified
  against Albert's real Oct 24 Dragy GPS lap (outline alignment + his
  real speed dips matched zone by zone), so when detection finds that
  layout the coach speaks and reports real names -- Sunrise, Off Ramp,
  Cotton Corners, Grapevine, Bus Stop, Phil Hill, Sunset. Any other
  corner count keeps Turn N. (His ~1s coast leak is Bus Stop.)
- Report window reliability: the report file is now rewritten after
  every lap, so it exists in `logs` even if the coach is closed
  without Ctrl+C, and at exit it opens via the Windows-native file
  open. Also fixed the report map rendering mirrored -- the flip is
  verified against the same GPS outline.

**v1.5.1** — corner-name correction. The v1.5 name table had the lap
running the wrong way (it assumed the front straight runs westward;
it runs eastward). Corrected against the official Speed Ventures
#13 CW map + GPS positions from Albert's real lap: 1 Sunset,
2 Riverside, 3 Lost Hills (= Phil Hill), 5 Cotton Corners,
6 Grapevine, 7 Sunrise. Turn 4 (south-run bend) is intentionally
left unnamed -- the SV map doesn't label it. His ~1s coast leak is
at Cotton Corners, not Bus Stop as v1.5 said.

**v1.5.2** — corner names corrected by Albert himself (Nov 1): the
Dragy GPS outline renders mirror-flipped, which had poisoned two
assistant attempts at naming from alignment. Per Albert: 1 Sunrise,
2 Off Ramp, 3 Cotton Corners, 4 Riverside Entry, 5 Phil Hill Exit,
6 Esses Entry, 7 Sunset (the turn before the main straight; the
zone spans the straight). The v1.5 report-map x-flip is reverted --
it rested on the same flipped GPS comparison, so the report map's
handedness is unsettled until eyeballed against the official map.

**v1.6** — two of Albert's enhancement requests (Nov 1):
- Off-track awareness: AC publishes numberOfTyresOut natively. When
  2+ tyres leave the track inside a corner zone (or ~30 m of its
  entry, where the veer starts), that corner is flagged "off" for
  the lap: no exit feedback, no approach cue next lap, excluded
  from lap-summary blame, session rankings and report averages.
  The lap summary says "Off track at <name> — that one doesn't
  count." Coasting during a save is no longer coached as a flaw.
- Cross-car corner maps: every profile save also maintains
  tracks/<track>__map.json (corner geometry + names only, never
  reference traces). A car with no map at a known track borrows it
  at launch; its first lap sets its own reference against the
  borrowed zones. A named map is never overwritten by an unnamed
  re-detection.

**v1.6.1** — the report popup no longer depends on shutdown at all.
Evidence: Albert's Nov 1 20:11 session debrief contained no shutdown
summary, i.e. the console window was closed and the exit path never
ran -- yet the report file existed (per-lap writes worked; he read
it). Now the report opens itself in the browser as soon as the
reference lap exists, and the page auto-refreshes every 20 s while
the file keeps updating underneath it. Also verified from his
uploaded report: the map renders with the tall loop on the left,
matching the orientation of the annotated picture he approved, and
the corrected corner names appear throughout.

**v1.6.2** — offs are now measured as excursions, not instants.
Albert confirmed one of the two Riverside Entry flags from the
20:41 session was a real off; the other he couldn't remember, and
the v1.6.1 log recorded only the flag, so it couldn't be audited.
Now each 2+ tyres-off excursion is timed: a corner is excused only
if an excursion overlapping it lasted at least
`off_min_duration_s` (0.35 s), and the session log records the
excursion's length (off_dur_s) per corner, so flags can be checked
after the fact instead of argued from memory.

**v1.7** — profiles move into the Assetto Corsa folder (Albert's
choice). Storage now resolves at startup: `profile_dir` config
setting if set, otherwise <AC install>\ACLiveCoach\tracks (the
Steam default path plus any libraries in libraryfolders.vdf are
detected), otherwise Documents\Assetto Corsa, otherwise the old
app-folder tracks\. The console prints where profiles live.
Profiles in the app folder are migrated (copied, never overwritten)
on first run. Albert's AC path: C:\Program Files (x86)\Steam\
steamapps\common\assettocorsa -- the first candidate checked.
Logs and reports stay in the app folder; only profiles move.

**v1.8** — two Albert requests: (1) the profile subfolder inside
ACLiveCoach is renamed tracks\ -> profiles\ (clearer name for what
it holds); migration now pulls from both previous homes (the app
folder and v1.7's ACLiveCoach\tracks). (2) Voice selection: set
"voice" in coach_config.json to any part of an installed SAPI voice
name ("zira", "david", ...) — empty keeps the system default.
`python ac_live_coach.py --list-voices` prints what the machine has.

**v1.9** — Albert's three complaints from the 21:10 session, fixed:
(1) No coaching on the out lap: the profile loaded from the AC
folder, so coaching was live from lap 1 and the 2:30 out lap drew
exit feedback ("Sunset: lost 984 tenths"). Now the first analysed
lap on a loaded profile gets no live cues, no summary and no
history; any lap 15%+ off the best is treated the same (in-laps).
(2) Sensitivity: a 2-tyre excursion must now last 0.9 s to excuse a
corner (off_min_duration_2tyres_s); 3+ tyres keep the 0.35 s floor.
(3) "Repeats offs from other laps": audited -- flags are computed
per lap from that lap's own excursions, no cross-lap bleed exists.
What he saw was the same corners (Sunset x4, Phil Hill Exit x4)
flagged on successive laps. New repeat rule: the same corner
excused 3 laps running (off_repeat_laps) stops being a save and is
coached as line, with a spoken note saying so.

**v1.9.1** — out-lap briefing (Albert's request). Once per session,
as the car gets rolling on lap 1, the coach explains itself. Loaded
profile: "Out lap — I don't coach this one, and nothing on it
counts. Best use of it: warm the tires and brakes with a few firm
brake zones and progressive throttle, build pace corner by corner,
and be up to speed by the last sector. Coaching starts when you
cross the line." Fresh profile (mapping lap): drive it clean and
representative at ~90%, because every later lap is compared to it.
Toggle: `out_lap_briefing` in coach_config.json.

**v1.9.2** — the briefing now also explains the coaching loop
(Albert asked for it, and asked whether that's really how it works
— it is): measured against your own best lap; a loss told to you
right after the corner; next lap, one fix for that corner given
just before it; lap time + biggest leak at the line. Delivered as a
single utterance so the 15 s stale-cue drop can't eat the second
half while the first half is still playing.

**v2.0** — the detailed map + line cues (Albert's spec, Nov 1 late).
- **Finer detection**: dips to 9 km/h (flagged "minor" under 18);
  minima merge only when the rise between them is <10 km/h, so the
  Sweeper's dip is no longer swallowed into the Esses. On Albert's
  real MX-5 reference lap the detector now finds 8 places:
  Sunrise, Off Ramp, Cotton Corners, Riverside, Phil Hill, Sweeper,
  Esses, Sunset (the double-bottom at Sunset merges — the speed
  only rises ~5 km/h between its two dips, so it's one corner).
- **Cotton Corners = three turns**: it's one speed trough, so zones
  wider than 200 m are split at heading direction changes (the
  reference lap now records heading and steering per bin; seams sit
  midway between heading extrema). Expect Cotton Corners 1/2/3 on
  the first remap, taking the map to ~10 places.
- **Phase coaching**: Riverside = Entry/Exit, Phil Hill = Entry /
  Phil Hill (the top of the hill) / Exit, Sweeper = Entry/Exit.
  Phases get their own loss accounting; when one phase holds most
  of a corner's loss, the cue names the phase ("Riverside Exit:
  get to power earlier").
- **Line cues** (Albert approved after a market check: Track Titan
  and Garmin Catalyst coach the line from position data; AC's
  shared memory has none, so these are symptoms vs his own best
  lap): apex point >=25 m early/late, a lift after committing to
  the throttle, and steering corrections (reversals + total travel
  vs reference, scale-free since steerAngle units vary by car).
- **Not more talking**: minor zones need bigger losses to speak
  (0.35 exit / approach bar raised), exit callouts capped at 3/lap;
  approach cues stay capped at 5 and loss-sorted.
- **`--remap`** (or run_coach_remap.bat): clears the loaded
  car/track's saved reference + the shared track map at startup so
  the next first lap rebuilds them with the fine detector.
- Naming on fine maps is by canonical span (apex fractions from
  Albert's own map), so 8/10/12-zone maps still get his names; an
  exactly-7 detection keeps the original confirmed table.

**v2.0.1** — briefing pacing (Albert: the intro was "a little
jarring when it's all at once"). The session briefing no longer
fires the moment the car rolls: it starts after ~900 m driven
(a couple of turns in; 30% of the lap on short tracks, config
`briefing_start_m`), and it arrives as three short pieces -- the
out-lap rules, the best use of the lap, and how the coaching loop
works -- with ~10 s of quiet between pieces instead of one
~115-word wall. Segments are spoken when they fall due rather
than queued ahead, so the stale-cue drop can't eat them.

**v2.0.3** — three things from Albert's live remap session (Nov 1):
(1) OUT LAP FIX (was the v2.0.2 work): the no-coaching rule only
classified laps after they ended, so LIVE cues still fired through
a slow out lap -- in a remap session lap 1 becomes the reference
and lap 2 is the real out lap, and it got coached in real time.
Now live cues are pace-gated (a lap projecting 15%+ off the
reference pace is silent), any lap after a pit stop or a reset/
teleport is muted outright, and in a fresh/remap profile the
first lap after the mapping lap is classified as an out lap too.
(2) SWEEPER ANATOMY: Albert's fine map resolves the Sweeper into
separate dips -- zone 7 is the Sweeper entrance, zone 8 is the
heart of the Sweeper, and the Esses come after 8. Span naming now
follows that (3+ dips: Sweeper Entry / Sweeper / Esses...); the
two-dip version keeps Sweeper + Esses with entry/exit phases.
(3) NAMES FILE: <track>__names.json in the profiles folder maps
zone number -> name and overrides the built-in tables -- naming a
new track no longer needs a new build. The coach writes a starter
file with the current names when none exists; edit it in Notepad,
restart the coach, names stick (also across remaps, matched by
zone number).

**v2.0.4** — out-lap briefing diet (Albert: "make it less intro...
add what stats you have about the current car and track combo").
The best-use lecture and the how-I-coach explanation are gone
from the spoken briefing. What remains, ~900 m into the out lap:
the one-line rule, then ~14 s later the stats for this car/track
-- your best lap here in this car, how many places are mapped,
and (once laps exist this session) your two biggest leaks so far.
Fresh car/track instead reports that no laps are logged yet, or
that the corner map was borrowed from another car and from which.

**v2.0.5** — the small-fixes bundle from the Nov 1 late sessions.
(1) Brake-point sanity: a corner's reference brake point is now
the brake onset nearest the apex that is COMMITTED (pedal held
~18 m or more), not the furthest tap within 420 m -- a check-up
tap had poisoned one corner's brake deltas (+230 m) for a whole
session. Saved profiles keep their old brake points until a
remap or a 3%-better best rebuilds the map. (2) Apex-shift
guard: a slowest-point reading pinned exactly on the +-90 m
window edge with a big claimed shift is dropped as unmeasured
(a split zone read -87 m every lap). (3) Session logs and the
debrief now open with the track/car combo and coach version
(the report already carried the combo; it gains the version).
(4) Cotton experiment: the heading-split swing threshold is now
config `heading_split_deg`, default 20 (was a hard-coded 25),
edge artefacts no longer anchor the swing chain, and every
remap prints the swings it saw per wide zone ("[coach] split
scan: ...") -- so whether Cotton Corners has a third seam is
now a number in the log, not a guess.

**v2.1.0** — improvements adopted from a review of
SeriousOldMan/Simulator-Controller (Albert's suggestion, Nov 2).
(1) Section feedback, after SC's Driving Coach section reviews:
the track is grouped into three sections; if a section's clean
losses total 0.45 s+ but no single corner reaches its own
callout threshold, you hear one line at the section boundary
("First section: lost 6 tenths there -- mostly Cotton Corners
2"). The last section is left to the lap summary. Config:
section_feedback / section_min_loss_s. (2) Cross-session
history, SC's telemetry database miniaturised: profiles/
<key>__history.json keeps sessions, laps and clean-corner loss
averages per car/track combo, and the out-lap stats line now
speaks from it before you've run a lap today ("Over 4 sessions
in this car here, your biggest leaks are..."). (3) Brake focus
mode (SC's brake coaching): config focus = "brake" makes
approach cues give braking fixes only; summaries still name the
biggest leak. Not adopted: LLM conversation (latency, cost,
chattiness; our cues stay deterministic), the spotter (AC
shared memory carries no opponent positions), and the race
engineer/strategist (race management, not lap coaching).

**v2.1.1** — US display units (Albert, Nov 2). AC publishes km/h
and all internal maths stays metric; only what is SHOWN converts:
the report's corner table now reads Brake delta in feet and Apex
delta in mph, and track length prints in miles (console and
debrief header). Spoken cues never used speed numbers, so nothing
you hear changes. Logs keep raw metric values.

**v2.2.0** — Offline neural voice (Piper). Albert: "It's so
robotic." The SAPI voices (Zira & friends) are the ceiling of the
old engine, so the default voice is now Piper TTS: a neural voice
that runs entirely on the laptop. First launch downloads the voice
model (~63 MB, en_US-lessac-medium) into ACLiveCoach\voices\ --
internet needed once, fully offline after that. Warm-up happens at
launch, in the pits. Measured on a modest CPU: model load ~2.5 s,
short cues render in ~0.3-0.5 s (about 0.2x real time). Config:
"voice_engine": "piper" | "sapi" (SAPI stays as the automatic
fallback if Piper is missing or fails 3 cues running),
"piper_voice" picks any piper-voices model name,
"piper_length_scale" is the rate (<1 faster). requirements.txt
gains piper-tts. Coaching logic unchanged from v2.1.1.

**v2.3.0** — track guides: human-coach knowledge as data. A
per-track <track>__guide.json (looked for next to the profiles,
then in the app folder's guides/) adds three things: (1) the
session report gains a Track guide section pairing each corner's
notes with your data, including corners the source covers that
your map doesn't separate, plus layout notes; (2) cue priority —
a corner whose guide priority is "exit" hears power/speed fixes
ahead of braking fixes, "entry" the reverse; (3) optional spoken
tips (config guide_spoken_tips, default off): the first approach
to a guided corner each session speaks its first tip, once per
corner. Ships with a Buttonwillow guide distilled from a Full
Throttle Driving Academy tutorial (see guides/); its layout
takes Star Mazda instead of the Sweeper, so that zone
intentionally has no tips. No guide file, no change.

**v2.4.0** — symptom-triggered guide tips. A guide tip can now
carry a symptom trigger ("brake", "coast", "power", "speed",
"apex_early", "apex_late", "lift", "hands"). When a data cue
fires at a corner and the winning fix's symptom has a tagged
guide tip there, the human coach's wording is spoken instead of
the generic fix — same cue slot, same budgets, only the words
change. Untagged tips still appear in the report (and in the
optional once-per-session spoken tips). Config kill switch:
guide_symptom_tips (default true). The Buttonwillow guide ships
with 8 tagged tips, e.g. Phil Hill brake -> "Brake as late as
the car allows", Esses coast -> "Flat out until the fourth S".

**v2.4.1** — the session report gains the reference-lap trace
chart: speed (mph) with each corner's brake point, apex and
throttle point marked, brake/throttle traces below, distance in
feet — drawn from the stored reference lap, inline in the report
(no extra files). Omitted until a reference lap exists.

**v2.5.0** — profiles move home (again, and simpler): they now
live in a `profiles` folder inside the app folder — a reference
relative to wherever the coach folder is — created automatically
if missing. First run copies in any profiles from the previous
homes (the AC install's ACLiveCoach\profiles, Documents, the
legacy tracks folder) without overwriting. `profile_dir` still
overrides, and a relative value is taken from the app folder.
With the git-clone workflow this makes one folder the whole
setup: pull updates the code, profiles stay untouched.

**v2.5.1** — the report map now follows your driving instead of
freezing on the first lap ever driven on a combo (which baked an
exploratory lap's wrong turns and recoveries into every report —
spotted at Willow Springs). The stored outline is refreshed by
every clean, counted lap; laps with an off or out laps leave it
alone.

**v2.5.2** — corner-name and track-guide files are now also read
from the pre-v2.5 profile homes (the Assetto Corsa folder's
ACLiveCoach\profiles, Documents, the legacy tracks folder) when
they aren't in the current profiles folder. The v2.5 move made
files dropped in the old location by habit silently do nothing;
the current folder still wins when both exist, and the console
prints the full path of the names file it applied.

**v2.5.3** — a hardening pass from an external code review (every
finding verified against the code first):
- One bad telemetry frame can no longer end the session: the
  per-frame work (static check, lap observation, feed, report) is
  exception-guarded with throttled logging, and non-finite
  position/speed frames are dropped at the door.
- Profile, track-map and history writes are atomic (temp file,
  fsync, os.replace) and keep a .bak of the previous file; a
  corrupted profile now recovers from its backup instead of
  silently starting fresh.
- Corner numbers are int()-coerced before they reach the report
  SVGs, and the report path is built from the log's file name
  only, not by string-replacing the whole path.
- The default Piper voice is verified against pinned SHA-256
  hashes (existing files too — a bad copy is re-downloaded, a bad
  download refuses to load); piper_voice is shape-validated before
  it reaches a URL or file path; dependencies are pinned
  (piper-tts 1.8.0, smoke-tested against the synth path).
- Hygiene: log writes are lock-serialized across the speech and
  main threads, the session JSONL handle is closed at exit, the
  Steam libraryfolders read uses a managed handle, and profile
  folder selection no longer creates folders in unchosen
  locations (including inside the Steam install).
