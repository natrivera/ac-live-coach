# Track guides

A track guide is human-coach knowledge for one track, distilled into a
small JSON file the coach can use: what each corner *is*, what matters
there, and a few short tips. The coach itself stays generic — guides
are data, one file per track.

## Using a guide

The coach looks for `<track>__guide.json` in two places, in order:

1. your profiles folder (next to your saved profiles), then
2. this `guides/` folder, next to the app.

The `<track>` part is the track's internal name as the coach sees it
(Buttonwillow is `nd_buttonwillow`). Drop the file in and restart the
coach; the console prints when a guide loads. With a guide, the
session report gains a Track guide section pairing the notes with
your data, and each corner's stated priority steers which fix the
coach picks when a corner has several problems. Optionally,
`guide_spoken_tips` in coach_config.json speaks each guided corner's
first tip once per session as you approach it (default off). No guide
file, no change in behaviour.

## Making a guide for your track

1. **Map the track first.** Drive it with the coach: your first lap
   builds the zone map. Note your zone numbers (the report shows
   them); name corners via the names file if you want real names.
2. **Get corner-by-corner knowledge**: a coaching video's transcript,
   a written track guide, an instructor's notes — anything that walks
   the lap in order.
3. **Distill it into the schema below, keyed by YOUR zone numbers.**
   Keep tips short (about 8 words, imperative) and transferable:
   line, apex, kerb use, throttle/brake intent, corner character,
   dangers. Drop source-car specifics (gears, another car's brake
   markers).
4. **Never force-fit.** If the source covers a corner your map
   doesn't separate, put it in `unmatched` with where it sits in lap
   order. If the source runs a different layout or configuration,
   say so in `source.layout_note` and leave the affected zones empty
   — the Buttonwillow guide in this folder does exactly that for the
   Sweeper (the source video takes Star Mazda instead).

## Schema

```json
{
  "track": "nd_buttonwillow",
  "source": {"title": "...", "channel": "...", "url": "...",
             "car": "...", "layout_note": "..."},
  "zones": {
    "1": {"name": "Sunrise",
          "character": "What the corner is, in a sentence or two.",
          "priority": "exit | entry | balanced",
          "tips": ["Short imperative tip", "..."]}
  },
  "unmatched": [{"corner": "Bus Stop",
                 "between": "Zone 4 and Zone 5",
                 "tips": ["..."]}],
  "layout_notes": ["Anything about layout differences that a reader needs."]
}
```

A tip may also be an object with a symptom `trigger`:
`{"text": "Flat out until the fourth S", "trigger": "coast"}`.
When a data cue fires at that corner and the winning fix's symptom
matches the trigger, the coach speaks the tip instead of the generic
fix. Triggers: `brake`, `coast`, `power`, `speed`, `apex_early`,
`apex_late`, `lift`, `hands`. Tag only tips that genuinely address
that symptom; untagged tips still show in the report.

`priority` is what the corner rewards: `exit` (speed onto what
follows), `entry` (braking and turn-in), or `balanced`.
