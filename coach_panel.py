#!/usr/bin/env python3
"""AC Live Coach — control panel (v2.26.0).

The whole interface to the coach in one window that stays open
across sessions: Start/Stop the engine, watch its state, flip the
coach's toggles live, and edit corner names without touching a
JSON file in Notepad.

Design rules (Albert's, Nov 6 2026):
- The panel HOSTS the engine -- run_live runs on a worker thread
  inside this process, so toggles mutate the very config dict the
  coach reads and apply immediately. Everything the coach prints
  lands in the Log tab (stdout is teed there).
- The files stay the storage: toggles persist to
  coach_config.json (only the panel-managed keys are written;
  anything else in the file is preserved), names persist to the
  same <track>__names.json files the coach has always used.
- The console launch (ac_live_coach.py) keeps working unchanged
  as a fallback -- the engine is shared, not forked.
- Reports stay HTML in the browser; the panel just opens them.
- This is a pit-lane tool: status and controls, not a live
  dashboard to watch while driving.
"""

import glob
import json
import os
import queue
import sys
import threading
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ac_live_coach as m  # noqa: E402

try:
    import tkinter as tk
    from tkinter import ttk, scrolledtext
except Exception:  # headless import for tests of the helpers
    tk = None

# ------------------------------------------------------------------
# Toggle spec: (key, label, kind, choices, live?)
# live=False means the value is read when the engine/speaker is
# constructed -- the row is badged "next launch" rather than
# pretending otherwise.
# ------------------------------------------------------------------
TOGGLES = [
    ("talk_level", "Talk level (0 quiet · 1 reduced · 2 full)",
     "choice", [0, 1, 2], True),
    ("sass_level", "Sass level (-1 supportive … 4 mean girl)",
     "choice", [-1, 0, 1, 2, 3, 4], True),
    ("brake_tone", "Brake-point tone", "bool", None, True),
    ("brake_tone_volume", "Brake tone volume (0–1)", "float", None, True),
    ("guide_lap", "Guided lap (guide read on lap 2)", "bool", None, True),
    ("guide_spoken_tips", "Guide tips spoken once per corner",
     "bool", None, True),
    ("guide_symptom_tips", "Guide wording for data cues",
     "bool", None, True),
    ("focus", "Cue focus", "choice", ["all", "brake"], True),
    ("max_approach_cues_per_lap", "Max approach cues per lap",
     "int", None, True),
    ("min_loss_to_speak", "Min loss to speak (s)", "float", None, True),
    ("voice_enabled", "Voice", "bool", None, False),
    ("voice_engine", "Voice engine", "choice", ["piper", "sapi"], False),
    ("piper_voice", "Piper voice", "text", None, False),
]
MANAGED_KEYS = [t[0] for t in TOGGLES]


# ------------------------------------------------------------------
# File helpers (kept GUI-free so the test suite can exercise them)
# ------------------------------------------------------------------
def config_path():
    return os.path.join(m.APP_DIR, "coach_config.json")


def save_managed(cfg):
    """Persist only the panel-managed keys into coach_config.json,
    preserving anything else the file carries."""
    data = {}
    p = config_path()
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    for k in MANAGED_KEYS:
        if k in cfg:
            data[k] = cfg[k]
    m._atomic_write_json(p, data)
    return p


def list_combos():
    """Profile keys with a saved profile file in TRACKS_DIR."""
    out = []
    for path in glob.glob(os.path.join(m.TRACKS_DIR, "*.json")):
        fn = os.path.basename(path)
        if any(fn.endswith(sfx) for sfx in
               ("__history.json", "__names.json", "__guide.json",
                "__map.json")):
            continue
        out.append(fn[:-len(".json")])
    return sorted(out)


def names_stem(key):
    track = key.split("__", 1)[0]
    lay = m._layout_len(key)
    return f"{track}__layout{lay}" if lay else track


def names_path(key):
    stem = names_stem(key) + "__names.json"
    return m._find_data_file(stem) or os.path.join(m.TRACKS_DIR, stem)


def read_names(key):
    p = names_path(key)
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return p, json.load(f)
        except Exception:
            pass
    return p, {}


def write_names(key, mapping):
    p = os.path.join(m.TRACKS_DIR, names_stem(key) + "__names.json")
    m._atomic_write_json(p, mapping)
    return p


def profile_corners(key):
    tp = m.TrackProfile(key)
    if tp.load():
        return tp.corners
    return []


def latest_report():
    cands = glob.glob(os.path.join(m.APP_DIR, "logs", "report_*.html"))
    if not cands:
        return None
    return max(cands, key=os.path.getmtime)


# ------------------------------------------------------------------
# stdout tee: everything the engine prints also lands in the log
# ------------------------------------------------------------------
class _TeeWriter:
    def __init__(self, real, q):
        self.real = real
        self.q = q
        self.buf = ""

    def write(self, s):
        try:
            self.real.write(s)
        except Exception:
            pass
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.q.put(line)

    def flush(self):
        try:
            self.real.flush()
        except Exception:
            pass
        if self.buf:
            self.q.put(self.buf)
            self.buf = ""


# ------------------------------------------------------------------
# The panel
# ------------------------------------------------------------------
class Panel:
    def __init__(self, root):
        self.root = root
        self.cfg = m.load_config()
        m.resolve_tracks_dir(self.cfg)
        self.log_q = queue.Queue()
        sys.stdout = _TeeWriter(sys.stdout, self.log_q)
        self.stop_event = threading.Event()
        self.status = {}
        self.thread = None
        self.vars = {}
        self.name_entries = {}
        self.current_combo = None

        root.title(f"AC Live Coach v{m.VERSION} — control panel")
        root.geometry("760x640")
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        head = ttk.Frame(root, padding=10)
        head.pack(fill="x")
        self.state_var = tk.StringVar(value="Stopped")
        ttk.Label(head, textvariable=self.state_var,
                  font=("Segoe UI", 14, "bold")).pack(side="left")
        self.run_btn = ttk.Button(head, text="Start coach",
                                  command=self._toggle_engine)
        self.run_btn.pack(side="right")
        ttk.Button(head, text="Open latest report",
                   command=self._open_report).pack(side="right", padx=6)

        info = ttk.Frame(root, padding=(10, 0))
        info.pack(fill="x")
        self.info_var = tk.StringVar(value="")
        ttk.Label(info, textvariable=self.info_var,
                  font=("Segoe UI", 11)).pack(side="left")

        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=10, pady=10)
        self._build_toggles(nb)
        self._build_names(nb)
        self._build_log(nb)

        self.root.after(120, self._drain_log)
        self.root.after(250, self._poll_status)
        print("[panel] ready — Start coach when you're in the car. "
              "Toggles apply live; corner names save to the names "
              "file and apply to a running session on the spot.")

    # ---- tabs -------------------------------------------------
    def _build_toggles(self, nb):
        tab = ttk.Frame(nb, padding=10)
        nb.add(tab, text="Coach toggles")
        for key, label, kind, choices, live in TOGGLES:
            row = ttk.Frame(tab)
            row.pack(fill="x", pady=3)
            ttk.Label(row, text=label, width=44).pack(side="left")
            if kind == "bool":
                var = tk.BooleanVar(value=bool(self.cfg.get(key)))
                ctl = ttk.Checkbutton(
                    row, variable=var,
                    command=lambda k=key, v=var: self._apply(k, bool(v.get())))
            elif kind == "choice":
                var = tk.StringVar(value=str(self.cfg.get(key)))
                ctl = ttk.Combobox(row, textvariable=var, width=12,
                                   state="readonly",
                                   values=[str(c) for c in choices])
                ctl.bind("<<ComboboxSelected>>",
                         lambda e, k=key, v=var, ch=choices:
                         self._apply(k, _coerce_choice(v.get(), ch)))
            else:
                var = tk.StringVar(value=str(self.cfg.get(key, "")))
                ctl = ttk.Entry(row, textvariable=var, width=14)
                ctl.bind("<Return>", lambda e, k=key, v=var, kd=kind:
                         self._apply_entry(k, v.get(), kd))
                ctl.bind("<FocusOut>", lambda e, k=key, v=var, kd=kind:
                         self._apply_entry(k, v.get(), kd))
            ctl.pack(side="left")
            if not live:
                ttk.Label(row, text="applies next launch",
                          foreground="#888").pack(side="left", padx=6)
            self.vars[key] = var

    def _build_names(self, nb):
        tab = ttk.Frame(nb, padding=10)
        nb.add(tab, text="Corner names")
        top = ttk.Frame(tab)
        top.pack(fill="x")
        ttk.Label(top, text="Combo:").pack(side="left")
        self.combo_var = tk.StringVar()
        self.combo_box = ttk.Combobox(top, textvariable=self.combo_var,
                                      state="readonly", width=52)
        self.combo_box.pack(side="left", padx=6)
        self.combo_box.bind("<<ComboboxSelected>>",
                            lambda e: self._load_combo())
        ttk.Button(top, text="Refresh list",
                   command=self._refresh_combos).pack(side="left")
        ttk.Button(top, text="Save names",
                   command=self._save_names).pack(side="right")
        wrap = ttk.Frame(tab)
        wrap.pack(fill="both", expand=True, pady=8)
        canvas = tk.Canvas(wrap, highlightthickness=0)
        scroll = ttk.Scrollbar(wrap, orient="vertical",
                               command=canvas.yview)
        self.names_frame = ttk.Frame(canvas)
        self.names_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.names_frame, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self._refresh_combos()

    def _build_log(self, nb):
        tab = ttk.Frame(nb, padding=6)
        nb.add(tab, text="Log")
        self.log_text = scrolledtext.ScrolledText(
            tab, font=("Consolas", 9), state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True)

    # ---- toggles ----------------------------------------------
    def _apply(self, key, value):
        self.cfg[key] = value
        save_managed(self.cfg)
        print(f"[panel] {key} = {value!r} (saved)")

    def _apply_entry(self, key, raw, kind):
        try:
            value = float(raw) if kind == "float" else int(raw) \
                if kind == "int" else raw.strip()
        except (TypeError, ValueError):
            print(f"[panel] {key}: {raw!r} is not a valid {kind} — ignored")
            self.vars[key].set(str(self.cfg.get(key, "")))
            return
        self._apply(key, value)

    # ---- corner names ------------------------------------------
    def _refresh_combos(self):
        combos = list_combos()
        self.combo_box["values"] = combos
        if combos and self.combo_var.get() not in combos:
            self.combo_var.set(combos[0])
        if self.combo_var.get():
            self._load_combo()

    def _load_combo(self):
        key = self.combo_var.get()
        if not key:
            return
        self.current_combo = key
        for w in self.names_frame.winfo_children():
            w.destroy()
        self.name_entries = {}
        _, mapping = read_names(key)
        corners = profile_corners(key)
        if not corners:
            ttk.Label(self.names_frame,
                      text="No saved corner map for this combo yet — "
                           "drive a first lap and it appears."
                      ).pack(anchor="w")
            return
        for c in corners:
            row = ttk.Frame(self.names_frame)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=f"{c['num']}.", width=4).pack(side="left")
            var = tk.StringVar(
                value=mapping.get(str(c["num"])) or c.get("name", ""))
            ttk.Entry(row, textvariable=var, width=34).pack(side="left")
            self.name_entries[c["num"]] = var

    def _save_names(self):
        key = self.current_combo
        if not key or not self.name_entries:
            return
        mapping = {}
        for num, var in self.name_entries.items():
            mapping[str(num)] = var.get().strip() or f"Turn {num}"
        p = write_names(key, mapping)
        print(f"[panel] names saved: {os.path.basename(p)} "
              f"({len(mapping)} corners)")
        coach = self.status.get("coach")
        prof = getattr(coach, "profile", None) if coach else None
        if prof is not None and prof.key == key:
            coach._apply_user_names(prof.corners)
            print("[panel] names applied to the running session")

    # ---- engine -------------------------------------------------
    def _toggle_engine(self):
        if self.thread is not None and self.thread.is_alive():
            self.stop_event.set()
            self.run_btn.configure(text="Stopping…", state="disabled")
            return
        self.stop_event = threading.Event()
        self.status = {}
        self.thread = threading.Thread(
            target=self._run_engine, daemon=True, name="coach-engine")
        self.thread.start()
        self.run_btn.configure(text="Stop coach")

    def _run_engine(self):
        try:
            m.run_live(self.cfg, stop_event=self.stop_event,
                       status=self.status)
        except Exception as e:
            print(f"[panel] coach engine stopped on an error: {e}")
        finally:
            self.status["running"] = False

    # ---- housekeeping -------------------------------------------
    def _open_report(self):
        p = self.status.get("report_path") or latest_report()
        if p and os.path.exists(p):
            webbrowser.open("file:///" + p.replace(os.sep, "/"))
        else:
            print("[panel] no session report found yet")

    def _drain_log(self):
        lines = []
        try:
            while True:
                lines.append(self.log_q.get_nowait())
        except queue.Empty:
            pass
        if lines:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", "\n".join(lines) + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.root.after(120, self._drain_log)

    def _poll_status(self):
        alive = self.thread is not None and self.thread.is_alive()
        if not alive and self.run_btn["text"] != "Start coach":
            self.run_btn.configure(text="Start coach", state="normal")
        st = self.status
        if not alive:
            self.state_var.set("Stopped")
            self.info_var.set("")
        elif not st.get("connected"):
            self.state_var.set("Waiting for Assetto Corsa…")
            self.info_var.set("")
        else:
            key = st.get("key") or "connected"
            self.state_var.set("On track")
            last = st.get("last_s")
            best = st.get("best_s")
            self.info_var.set(
                f"{key.replace('__', '  ·  ')}    "
                f"Lap {st.get('lap_number', 0)}    "
                f"Last {m.fmt_time(last) if last else '—'}    "
                f"Best {m.fmt_time(best) if best else '—'}")
        self.root.after(250, self._poll_status)

    def _on_close(self):
        self.stop_event.set()
        self.root.after(600, self.root.destroy)


def _coerce_choice(raw, choices):
    for c in choices:
        if str(c) == raw:
            return c
    return raw


def main():
    if tk is None:
        print("tkinter is not available in this Python install — "
              "the panel cannot open. The console coach "
              "(ac_live_coach.py) works as before.")
        return
    root = tk.Tk()
    Panel(root)
    root.mainloop()


if __name__ == "__main__":
    main()
