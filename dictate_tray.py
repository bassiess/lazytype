"""
Lazytype — systeemvak-versie.

Zelfde dicteren als dictate.py (houd een toets ingedrukt, spreek, laat los),
maar nu met een icoontje in het systeemvak:

  • Statuskleur:  blauw = klaar · rood = opname · oranje = transcriberen · grijs = gepauzeerd
  • Rechtsklik-menu: engine wisselen, taal wisselen, pauzeren, afsluiten

Starten:  python dictate_tray.py   (of dubbelklik Lazytype.bat)
Test:     python dictate_tray.py --selftest   (bouwt icoon+menu, schrijft preview, sluit af)
"""

import os
import sys
import math
import time
import subprocess
import threading
from pathlib import Path

# Forceer UTF-8 op de uitvoer; in een 'windowed' .exe is stdout None → devnull,
# anders crasht print() (zie ook dictate.py).
for _name in ("stdout", "stderr"):
    _s = getattr(sys, _name, None)
    if _s is None:
        try:
            setattr(sys, _name, open(os.devnull, "w", encoding="utf-8"))
        except Exception:
            pass
    else:
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import pystray
from PIL import Image, ImageDraw

import dictate  # hergebruikt: Recorder, transcribe, finalize_text, paste_text, beep, config
from pynput import keyboard
from pynput.keyboard import Key

IS_WIN = dictate.IS_WIN
IS_MAC = dictate.IS_MAC

APP_VERSION = "1.0.9"
_update_info = None  # None = geen update beschikbaar / niet gecontroleerd; str = nieuwere versie


def _check_update():
    """Achtergrond-check of er een nieuwe versie beschikbaar is op lazytype.com."""
    global _update_info
    try:
        import requests
        r = requests.get("https://lazytype.com/version.json", timeout=8)
        latest = r.json().get("version", "")
        if latest and latest != APP_VERSION:
            _update_info = latest
            refresh()
    except Exception:
        pass

# Sneltoetsen die óók een modifier zijn (ctrl/alt/shift). Hierbij gebruiken we een
# "arming"-mechanisme: kort tikken of een snelkoppeling (bv. Ctrl+C) start géén dictaat.
MODIFIER_KEYS = {
    Key.ctrl, Key.ctrl_l, Key.ctrl_r,
    Key.alt, Key.alt_l, Key.alt_r, getattr(Key, "alt_gr", None),
    Key.shift, Key.shift_l, Key.shift_r,
    getattr(Key, "cmd", None), getattr(Key, "cmd_l", None), getattr(Key, "cmd_r", None),
}
MODIFIER_KEYS.discard(None)
MIN_HOLD_SEC = 0.35  # zo lang schoon ingedrukt houden voordat een modifier als dictaat telt


# ── Gedeelde, muteerbare status ─────────────────────────────────────────
state = {
    "engine": dictate.ENGINE,
    "language": dictate.LANGUAGE,
    "postprocess": dictate.POSTPROCESS,  # off | clean | taalcode (en/nl/de/…)
    "hotkey_name": dictate.HOTKEY_NAME,                    # dicteren
    "hotkey_command": dictate.COMMAND_HOTKEY_NAME,         # command mode
    "hotkey_translate": dictate.TRANSLATE_HOTKEY_NAME,     # dicteren + vertalen
    "translate_target": dictate.TRANSLATE_TARGET,          # doeltaal van de vertaal-toets
    "phase": "idle",     # idle | recording | working
    "enabled": True,
    "last": "",
    "busy": False,
    "history": dictate.HISTORY_ENABLED,   # dicteer-geschiedenis bewaren
    "overlay": os.environ.get("DICTATE_OVERLAY", "1").lower() in ("1", "true", "yes", "on"),
    "active_mode": "dictate",       # dictate | command | translate (welke flow loopt nu)
    "active_matchers": None,        # welke sneltoets(-combo) de huidige opname startte
    "target_hwnd": 0,               # venster dat focus had bij indrukken → herstel vóór paste
}

PHASE_LABEL = {
    "idle": "Klaar — houd de toets ingedrukt",
    "recording": "🔴 Opname…",
    "working": "⏳ Transcriberen…",
    "disabled": "Gepauzeerd",
}

# Merk-icoon in lijn met de website: near-black squircle, witte waveform-balken,
# violette I-beam (tekstcursor). De STATUS zit in de kleur van de I-beam.
INK = (26, 27, 31)        # squircle-achtergrond (#1a1b1f), zoals site --ink
BAR = (245, 246, 250)     # witte balken
ACCENT = {
    "idle":      (90, 70, 224),    # violet #5a46e0 (merkkleur van de site)
    "recording": (226, 74, 74),    # rood = opname
    "working":   (240, 168, 60),   # amber = transcriberen
    "disabled":  (122, 126, 137),  # grijs = gepauzeerd
}
GRADIENTS = ACCENT        # alias voor compat
COLORS = ACCENT

# UI-thema voor de dialogen, in lijn met de website (warm-wit, ink, violet accent)
UI_PAPER, UI_INK, UI_ACCENT, UI_ACCENT2 = "#f7f6f3", "#191a1e", "#5a46e0", "#4a37c8"
UI_SUB, UI_LINE = "#6b6f7b", "#e8e6e0"
UI_FONT, UI_MONO = ("Segoe UI", 10), ("Consolas", 11)

# Keuzelijsten voor het instellingen-venster + onboarding
HOTKEY_CHOICES = [("Right Ctrl", "ctrl_r"), ("Left Ctrl", "ctrl_l"),
                  ("Right Alt", "alt_r"), ("Left Alt", "alt_l"), ("Right Shift", "shift_r"),
                  ("Ctrl + Alt", "ctrl+alt"), ("Ctrl + Shift", "ctrl+shift"),
                  ("Ctrl + Win", "ctrl+win"), ("Win + Alt", "win+alt"),
                  ("Win + Shift", "win+shift"), ("Alt + Shift", "alt+shift"),
                  ("F8", "f8"), ("F9", "f9"), ("F10", "f10"), ("Off", "uit")]


def _hk_display(spec):
    return next((l for l, v in HOTKEY_CHOICES if v == spec), spec)
LANG_CHOICES = [
    ("English", "en"), ("Dutch", "nl"), ("German", "de"), ("French", "fr"),
    ("Spanish", "es"), ("Italian", "it"), ("Portuguese", "pt"),
    ("Polish", "pl"), ("Russian", "ru"), ("Ukrainian", "uk"),
    ("Swedish", "sv"), ("Norwegian", "no"), ("Danish", "da"), ("Finnish", "fi"),
    ("Turkish", "tr"), ("Arabic", "ar"), ("Japanese", "ja"),
    ("Chinese", "zh"), ("Korean", "ko"),
]
SPOKEN_CHOICES = [("Auto-detect", "auto"), ("Dutch", "nl"), ("English", "en"),
                  ("German", "de"), ("French", "fr"), ("Spanish", "es")]
ENGINE_CHOICES = [("Managed (Pro)", "managed"), ("Groq (own key)", "groq"),
                  ("OpenAI", "openai"), ("Local", "local")]

LANG_DISPLAY = {
    "en": "English", "nl": "Dutch", "de": "German", "fr": "French",
    "es": "Spanish", "it": "Italian", "pt": "Portuguese", "pl": "Polish",
    "ru": "Russian", "uk": "Ukrainian", "sv": "Swedish", "no": "Norwegian",
    "da": "Danish", "fi": "Finnish", "tr": "Turkish", "ar": "Arabic",
    "ja": "Japanese", "zh": "Chinese", "ko": "Korean", "auto": "Auto-detect",
}


def render_icon(accent, size=64, shape="squircle", bar=BAR, bg=INK):
    """Wave-cursor merkmark (4× supersampling, antialiased): near-black squircle,
    witte waveform-balken, I-beam-cursor in de accent/statuskleur."""
    S = size * 4
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    pad = int(S * 0.035)
    if shape == "circle":
        d.ellipse((pad, pad, S - pad, S - pad), fill=bg + (255,))
    else:
        d.rounded_rectangle((pad, pad, S - pad, S - pad), radius=int(S * 0.235), fill=bg + (255,))

    cx = cy = S // 2
    bw = int(S * 0.08)
    sp = int(S * 0.155)

    def vbar(x, h, color):
        d.rounded_rectangle((x - bw // 2, cy - h // 2, x + bw // 2, cy + h // 2),
                            radius=bw // 2, fill=color + (255,))

    vbar(cx - 2 * sp, int(S * 0.20), bar)   # buiten: kort
    vbar(cx - sp,     int(S * 0.40), bar)   # binnen: lang
    vbar(cx + sp,     int(S * 0.40), bar)
    vbar(cx + 2 * sp, int(S * 0.20), bar)

    # I-beam (tekstcursor) in de accent/statuskleur
    a = accent + (255,)
    H = int(S * 0.50)
    top, bot = cy - H // 2, cy + H // 2
    sw, ct, cw = int(S * 0.085), int(S * 0.055), int(S * 0.10)
    d.rounded_rectangle((cx - sw // 2, top, cx + sw // 2, bot), radius=sw // 2, fill=a)
    d.rounded_rectangle((cx - cw, top, cx + cw, top + ct), radius=ct // 2, fill=a)
    d.rounded_rectangle((cx - cw, bot - ct, cx + cw, bot), radius=ct // 2, fill=a)

    return img.resize((size, size), Image.LANCZOS)


def make_icon(state_or_color, size=64, shape="squircle"):
    if isinstance(state_or_color, str):
        accent = ACCENT.get(state_or_color, ACCENT["idle"])
        bar = (150, 154, 165) if state_or_color == "disabled" else BAR
    elif isinstance(state_or_color, tuple):
        accent, bar = state_or_color, BAR
    else:
        accent, bar = ACCENT["idle"], BAR
    return render_icon(accent, size=size, shape=shape, bar=bar)


ICONS = {k: make_icon(k) for k in ACCENT}

icon = None
recorder = dictate.Recorder()
arm = {"timer": None, "aborted": False}
pressed = set()       # toetsen die nu ingedrukt zijn
HOTKEYS_LIST = []     # lijst van (matchers, mode, all_modifiers); langste combo eerst
_keypicking = False   # True tijdens keypicker-capture → pynput-listener doet niets

# Generieke modifier-groepen — bij een combo maakt links/rechts niet uit.
MOD_GROUPS = {
    "ctrl":  {Key.ctrl, Key.ctrl_l, Key.ctrl_r},
    "alt":   {Key.alt, Key.alt_l, Key.alt_r, getattr(Key, "alt_gr", None)},
    "shift": {Key.shift, Key.shift_l, Key.shift_r},
    "win":   {getattr(Key, "cmd", None), getattr(Key, "cmd_l", None), getattr(Key, "cmd_r", None)},
    "cmd":   {getattr(Key, "cmd", None), getattr(Key, "cmd_l", None), getattr(Key, "cmd_r", None)},
}


def _matcher(token):
    """Eén deel van een sneltoets → set van toetsen die ervoor tellen."""
    token = token.strip().lower()
    if token in MOD_GROUPS:
        return {k for k in MOD_GROUPS[token] if k is not None}
    try:
        return {dictate.resolve_hotkey(token)}
    except Exception:
        return set()


def _parse_hotkey(spec):
    """'ctrl+alt' / 'ctrl_l' / 'win shift' → lijst van matchers (1 = enkel, 2 = combo)."""
    parts = [p for p in spec.strip().lower().replace("+", " ").split() if p]
    return [m for m in (_matcher(p) for p in parts) if m]


def _all_modifiers(matchers):
    return all(all(k in MODIFIER_KEYS for k in m) for m in matchers)


def _satisfied(matchers):
    return bool(matchers) and all(any(k in pressed for k in m) for m in matchers)


def rebuild_hotkeys():
    """(Her)bouw de sneltoets-lijst uit de instellingen. De listener leest 'm live,
    dus losse toetsen én combo's wijzigen werkt zonder de app te herstarten."""
    global HOTKEYS_LIST
    lst = []
    for spec, mode in ((state["hotkey_name"], "dictate"),
                       (state["hotkey_command"], "command"),
                       (state["hotkey_translate"], "translate")):
        spec = (spec or "").strip()
        if not spec or spec.lower() in ("uit", "none", "geen", ""):
            continue
        matchers = _parse_hotkey(spec)
        if matchers:
            lst.append((matchers, mode, _all_modifiers(matchers)))
    lst.sort(key=lambda e: -len(e[0]))   # langste combo eerst → 'ctrl+alt' wint van losse 'ctrl'
    HOTKEYS_LIST = lst


rebuild_hotkeys()


def current_phase():
    return "disabled" if not state["enabled"] else state["phase"]


def refresh():
    if not icon:
        return
    ph = current_phase()
    icon.icon = ICONS[ph]
    pp = state.get("postprocess", "off")
    pp_tag = "" if pp in ("off", "") else (" · opschonen" if pp == "clean" else f" · →{pp}")
    icon.title = f"Lazytype · {PHASE_LABEL[ph]} · {state['engine']}/{state['language']}{pp_tag}"
    try:
        icon.menu = build_menu()   # herbouw zodat o.a. de geschiedenis live meeloopt
        icon.update_menu()
    except Exception:
        pass


def set_phase(p):
    state["phase"] = p
    refresh()


# ── Opname → transcriptie → plakken ─────────────────────────────────────
def handle_release():
    try:
        wav = recorder.stop()
        secs = recorder.duration()
        threading.Thread(target=lambda: dictate.beep("stop"), daemon=True).start()
        if secs < 0.3:
            return
        set_phase("working")
        t0 = time.time()
        mode = state.get("active_mode")
        if mode == "command":
            sel = dictate.copy_selection()
            if not sel:
                state["last"] = "Command: geen tekst geselecteerd"
                threading.Thread(target=lambda: dictate.beep("error"), daemon=True).start()
                return
            text = dictate.transform_command(wav, sel, language=state["language"], engine=state["engine"])
        elif mode == "translate":
            text = dictate.run_pipeline(wav, engine=state["engine"], language=state["language"],
                                        postprocess=state["translate_target"])
        else:
            text = dictate.run_pipeline(wav, engine=state["engine"],
                                        language=state["language"], postprocess=state["postprocess"])
        dt = time.time() - t0
        if not text:
            state["last"] = "(geen spraak herkend)"
            threading.Thread(target=lambda: dictate.beep("error"), daemon=True).start()
            return
        print(f"  ✅ ({dt:.2f}s) → {text}")
        state["last"] = text
        _restore_focus(state.get("target_hwnd", 0))
        dictate.paste_text(text)
        threading.Thread(target=lambda: dictate.beep("done"), daemon=True).start()
        if state.get("history"):
            dictate.add_history(text)
    except Exception as e:
        print(f"  ⚠️  {e}")
        state["last"] = f"Fout: {e}"
        threading.Thread(target=lambda: dictate.beep("error"), daemon=True).start()
    finally:
        state["busy"] = False
        set_phase("idle")


def _confirm_arming():
    """Loopt MIN_HOLD_SEC na het indrukken: promoot 'arming' → echte opname."""
    if state["phase"] == "arming" and not arm["aborted"]:
        dictate.beep("start")
        set_phase("recording")


def _get_foreground_hwnd() -> int:
    if dictate.IS_WIN:
        try:
            import ctypes
            return ctypes.windll.user32.GetForegroundWindow()
        except Exception:
            pass
    return 0


def _restore_focus(hwnd: int):
    if not hwnd or not dictate.IS_WIN:
        return
    try:
        import ctypes
        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        fg = u32.GetForegroundWindow()
        if fg == hwnd:
            return
        fg_tid  = u32.GetWindowThreadProcessId(fg,   None)
        tgt_tid = u32.GetWindowThreadProcessId(hwnd, None)
        our_tid = k32.GetCurrentThreadId()
        u32.AttachThreadInput(our_tid, fg_tid, True)
        u32.BringWindowToTop(hwnd)
        u32.SetForegroundWindow(hwnd)
        u32.AttachThreadInput(our_tid, fg_tid, False)
        import time as _t; _t.sleep(0.08)
    except Exception:
        pass


def _begin(matchers, mode, needs_arming):
    state["target_hwnd"] = _get_foreground_hwnd()
    recorder.start()
    arm["aborted"] = False
    state["active_mode"] = mode
    state["active_matchers"] = matchers
    if needs_arming:
        # nog niet zeker of dit een dictaat/command of een snelkoppeling wordt
        state["phase"] = "arming"
        arm["timer"] = threading.Timer(MIN_HOLD_SEC, _confirm_arming)
        arm["timer"].start()
    else:
        threading.Thread(target=lambda: dictate.beep("start"), daemon=True).start()
        set_phase("recording")


def on_press(key):
    pressed.add(key)
    if _keypicking or not state["enabled"] or state["busy"]:
        return
    hit = next(((mt, md, am) for mt, md, am in HOTKEYS_LIST if _satisfied(mt)), None)
    if state["phase"] == "idle":
        if hit:
            _begin(hit[0], hit[1], hit[2])
        return
    if state["phase"] == "arming":
        active = state.get("active_matchers") or []
        if hit and hit[0] != active and len(hit[0]) >= len(active):
            # tweede toets erbij → schakel over naar de specifiekere combo
            if arm["timer"]:
                arm["timer"].cancel()
            arm["aborted"] = False
            state["active_matchers"] = hit[0]
            state["active_mode"] = hit[1]
            if hit[2]:
                arm["timer"] = threading.Timer(MIN_HOLD_SEC, _confirm_arming)
                arm["timer"].start()
            else:
                threading.Thread(target=lambda: dictate.beep("start"), daemon=True).start()
                set_phase("recording")
            return
        # toets die bij geen actieve hotkey hoort → het was een snelkoppeling
        if not any(key in m for m in active):
            arm["aborted"] = True


def on_release(key):
    pressed.discard(key)
    if state["phase"] not in ("arming", "recording"):
        return
    active = state.get("active_matchers")
    if not active or _satisfied(active):
        return                       # combo nog volledig ingedrukt → niks doen
    if state["phase"] == "arming":
        # te snel losgelaten of afgebroken → stil weggooien
        if arm["timer"]:
            arm["timer"].cancel()
        recorder.stop()
        set_phase("idle")
        return
    state["busy"] = True
    set_phase("working")
    threading.Thread(target=handle_release, daemon=True).start()


# ── Menu-acties ─────────────────────────────────────────────────────────
def choose_engine(e):
    def action(icon_, item):
        state["engine"] = e
        refresh()
    return action


def choose_language(code):
    def action(icon_, item):
        state["language"] = code
        refresh()
    return action


def choose_postprocess(mode):
    def action(icon_, item):
        state["postprocess"] = mode
        refresh()
    return action


def toggle_enabled(icon_, item):
    state["enabled"] = not state["enabled"]
    refresh()


def history_copy(text):
    def action(icon_, item):
        dictate._clipboard_set(text)
        state["last"] = "Gekopieerd ✓"
        refresh()
    return action


def toggle_history(icon_, item):
    state["history"] = not state["history"]
    dictate.save_env_value("DICTATE_HISTORY", "1" if state["history"] else "0")
    refresh()


def clear_history_action(icon_, item):
    dictate.clear_history()
    state["last"] = "Geschiedenis gewist"
    refresh()


def _hist_label(text):
    t = (text or "").replace("\n", "⏎").strip()
    return (t[:42] + "…") if len(t) > 42 else (t or "—")


def do_quit(icon_, item):
    overlay_ui.stop()          # eerst de overlay-Tk netjes sluiten (op zijn eigen thread)
    icon_.visible = False
    icon_.stop()


def short_last():
    t = state["last"]
    if not t:
        return "Laatste: —"
    t = t.replace("\n", "⏎")
    return "Laatste: " + (t[:38] + "…" if len(t) > 38 else t)


# ── Automatisch starten bij inloggen (Windows: register · macOS: LaunchAgent) ──
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "Lazytype"
LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / "com.lazytype.plist"


def autostart_supported():
    return IS_WIN or IS_MAC


def _program_args():
    """Het commando om de tool te starten — gebouwde app of python-script."""
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve())]
    if IS_WIN:
        exe = Path(sys.executable)
        pyw = exe.with_name("pythonw.exe")
        runner = str(pyw if pyw.exists() else exe)
    else:
        runner = sys.executable
    return [runner, str(Path(__file__).resolve())]


def is_autostart():
    if IS_WIN:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                val, _ = winreg.QueryValueEx(k, APP_NAME)
                return bool(val)
        except OSError:
            return False
    if IS_MAC:
        return LAUNCH_AGENT.exists()
    return False


def set_autostart(on: bool):
    if IS_WIN:
        import winreg
        cmd = " ".join(f'"{a}"' for a in _program_args())
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            if on:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(k, APP_NAME)
                except FileNotFoundError:
                    pass
    elif IS_MAC:
        if on:
            args = "".join(f"      <string>{a}</string>\n" for a in _program_args())
            plist = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0"><dict>\n'
                '  <key>Label</key><string>com.lazytype</string>\n'
                '  <key>ProgramArguments</key><array>\n' + args +
                '  </array>\n'
                '  <key>RunAtLoad</key><true/>\n'
                '</dict></plist>\n'
            )
            LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
            LAUNCH_AGENT.write_text(plist, encoding="utf-8")
            subprocess.run(["launchctl", "load", str(LAUNCH_AGENT)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif LAUNCH_AGENT.exists():
            subprocess.run(["launchctl", "unload", str(LAUNCH_AGENT)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            LAUNCH_AGENT.unlink()


def toggle_autostart(icon_, item):
    set_autostart(not is_autostart())
    refresh()


# ── Eigen invoer-dialoog in de site-stijl (vervangt de grijze simpledialog) ──
def _themed_input(title, prompt, initial="", secret=False):
    import tkinter as tk
    result = {"value": None}
    root = tk.Tk()
    root.title(title)
    root.configure(bg=UI_PAPER)
    root.attributes("-topmost", True)
    root.resizable(False, False)
    tk.Label(root, text=prompt, bg=UI_PAPER, fg=UI_INK, font=UI_FONT,
             wraplength=400, justify="left").pack(fill="x", padx=18, pady=(18, 8))
    var = tk.StringVar(value=initial)
    entry = tk.Entry(root, textvariable=var, width=46, font=UI_FONT,
                     show="•" if secret else "", relief="flat", bg="#ffffff",
                     fg=UI_INK, insertbackground=UI_INK, highlightthickness=1,
                     highlightbackground="#dedbd3", highlightcolor=UI_ACCENT)
    entry.pack(padx=18, ipady=6, fill="x")
    entry.focus_set(); entry.icursor("end")

    def ok(_e=None):
        result["value"] = var.get(); root.destroy()

    def cancel(_e=None):
        result["value"] = None; root.destroy()

    bar = tk.Frame(root, bg=UI_PAPER)
    bar.pack(fill="x", padx=18, pady=16)
    tk.Button(bar, text="Annuleer", command=cancel, bg=UI_PAPER, fg=UI_INK,
              relief="flat", font=UI_FONT, cursor="hand2", borderwidth=0,
              activebackground=UI_PAPER, activeforeground=UI_ACCENT).pack(side="right", padx=(8, 0))
    tk.Button(bar, text="OK", command=ok, bg=UI_ACCENT, fg="white", relief="flat",
              activebackground=UI_ACCENT2, activeforeground="white",
              font=("Segoe UI", 10, "bold"), padx=22, pady=6, cursor="hand2",
              borderwidth=0).pack(side="right")
    root.bind("<Return>", ok)
    root.bind("<Escape>", cancel)
    root.mainloop()
    return result["value"]


# ── API-key instellen (eigen dialoog) ───────────────────────────────────
def ask_groq_key(initial: str = "") -> str | None:
    return _themed_input("Lazytype — Groq API-key",
                         "Plak je Groq API-key (gratis via console.groq.com/keys):", initial)


def set_key_action(icon_, item):
    def worker():
        key = ask_groq_key(initial=os.environ.get("GROQ_API_KEY", ""))
        if key:
            dictate.save_env_value("GROQ_API_KEY", key.strip())
            state["last"] = "API-key opgeslagen ✓"
            refresh()
    threading.Thread(target=worker, daemon=True).start()


def _request_trial_key(email: str) -> str:
    """POST naar trial.php → geeft proefsleutel terug of raise RuntimeError."""
    try:
        import requests
        base = dictate.API_URL.rsplit("/", 1)[0]
        r = requests.post(f"{base}/trial.php", data={
            "email": email.strip(),
            "device": dictate.ensure_device_id(),
        }, timeout=15)
        data = r.json()
        if r.ok and data.get("ok"):
            return data["key"]
        raise RuntimeError(data.get("error", f"Serverfout {r.status_code}"))
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Geen verbinding: {e}")


# ── Abonnement (licentiesleutel) ────────────────────────────────────────
def license_status() -> str:
    return "Status: " + dictate.license_state()["label"]


def set_license_action(icon_, item):
    def worker():
        import license as lic
        val = _ask("Lazytype — Abonnement", "Plak je licentiesleutel (begint met LZT.):",
                   os.environ.get("LAZYTYPE_LICENSE", ""))
        if val:
            val = val.strip()
            dictate.save_env_value("LAZYTYPE_LICENSE", val)
            os.environ["LAZYTYPE_LICENSE"] = val
            p = lic.decode(val)
            state["last"] = "Abonnement: " + (lic.describe(p) if p else "ongeldige sleutel")
            if p and lic.TIERS.get(p.get("tier"), {}).get("managed"):
                state["engine"] = "managed"
                dictate.save_env_value("DICTATE_ENGINE", "managed")
            refresh()
    threading.Thread(target=worker, daemon=True).start()


# ── Admin (owner-only: alleen zichtbaar als het geheim lokaal aanwezig is) ──
def is_owner() -> bool:
    return bool(os.environ.get("LAZYTYPE_LICENSE_SECRET")) and not getattr(sys, "frozen", False)


def _ask(title: str, prompt: str, initial: str = ""):
    return _themed_input(title, prompt, initial)


def _show_text(title: str, text: str):
    def worker():
        import tkinter as tk
        from tkinter.scrolledtext import ScrolledText
        root = tk.Tk(); root.title(title); root.attributes("-topmost", True)
        root.configure(bg=UI_PAPER)
        box = ScrolledText(root, width=80, height=22, font=UI_MONO, bg="#ffffff",
                           fg=UI_INK, insertbackground=UI_INK, relief="flat",
                           borderwidth=0, padx=12, pady=10)
        box.pack(fill="both", expand=True, padx=12, pady=12)
        box.insert("1.0", text); box.configure(state="disabled")
        root.mainloop()
    threading.Thread(target=worker, daemon=True).start()


def admin_gen_action(icon_, item):
    def worker():
        import admin, license as lic
        email = _ask("Admin — nieuwe sleutel", "E-mail van de klant:")
        if not email:
            return
        tier = (_ask("Admin — tier", "Tier (personal / pro):", "pro") or "pro").strip().lower()
        days = (_ask("Admin — geldigheid", "Geldig in dagen (0 = lifetime):", "30") or "30").strip()
        try:
            key, payload = admin.gen(email.strip(), tier, int(days or 0))
        except Exception as e:
            _show_text("Admin — fout", str(e)); return
        dictate._clipboard_set(key)
        _show_text("Admin — sleutel aangemaakt",
                   f"{lic.describe(payload)} voor {email}\n(gekopieerd naar klembord)\n\n{key}")
    threading.Thread(target=worker, daemon=True).start()


def _admin_show(fn_name, title):
    def action(icon_, item):
        def worker():
            import admin
            _show_text(title, getattr(admin, fn_name)())
        threading.Thread(target=worker, daemon=True).start()
    return action


def admin_verify_action(icon_, item):
    def worker():
        import admin
        key = _ask("Admin — verifiëren", "Plak een sleutel:")
        if key:
            _show_text("Admin — verificatie", admin.verify_text(key.strip()))
    threading.Thread(target=worker, daemon=True).start()


def admin_revoke_action(icon_, item):
    def worker():
        import admin
        kid = _ask("Admin — intrekken", "ID om in te trekken (zie 'Sleutels tonen'):")
        if kid:
            _show_text("Admin — intrekken", admin.revoke(kid.strip()))
    threading.Thread(target=worker, daemon=True).start()


def _edit_file_action(path, title, label, default=""):
    """Factory: maakt een menu-actie die een tekstbestand in een editor opent."""
    def action(icon_, item):
        def worker():
            import tkinter as tk
            from tkinter.scrolledtext import ScrolledText
            try:
                content = path.read_text(encoding="utf-8")
            except Exception:
                content = default
            root = tk.Tk()
            root.title(title)
            root.attributes("-topmost", True)
            root.configure(bg=UI_PAPER)
            tk.Label(root, text=label, anchor="w", justify="left", bg=UI_PAPER,
                     fg=UI_INK, font=UI_FONT).pack(fill="x", padx=14, pady=(14, 6))
            box = ScrolledText(root, width=58, height=18, font=UI_MONO, bg="#ffffff",
                               fg=UI_INK, insertbackground=UI_INK, relief="flat",
                               borderwidth=0, padx=12, pady=10)
            box.pack(fill="both", expand=True, padx=14)
            box.insert("1.0", content)

            def save():
                try:
                    path.write_text(box.get("1.0", "end-1c"), encoding="utf-8")
                    state["last"] = "Opgeslagen ✓"
                except Exception as e:
                    state["last"] = f"Opslaan mislukt: {e}"
                root.destroy()
                refresh()

            tk.Button(root, text="Opslaan", command=save, bg=UI_ACCENT, fg="white",
                      activebackground=UI_ACCENT2, activeforeground="white", relief="flat",
                      font=("Segoe UI", 10, "bold"), padx=20, pady=7, cursor="hand2",
                      borderwidth=0).pack(pady=12)
            root.mainloop()
        threading.Thread(target=worker, daemon=True).start()
    return action


# ── Instellingen-venster + onboarding (in de site-stijl) ────────────────
def _dropdown(tk, parent, label, choices, current):
    cur = next((l for l, v in choices if v == current), choices[0][0])
    tk.Label(parent, text=label, bg=UI_PAPER, fg=UI_INK, font=UI_FONT, anchor="w").pack(fill="x", padx=18, pady=(10, 2))
    var = tk.StringVar(master=parent, value=cur)
    om = tk.OptionMenu(parent, var, *[l for l, _ in choices])
    om.configure(bg="#ffffff", fg=UI_INK, font=UI_FONT, relief="flat", anchor="w",
                 highlightthickness=1, highlightbackground="#dedbd3", activebackground="#efecfd")
    om["menu"].configure(bg="#ffffff", fg=UI_INK, font=UI_FONT, activebackground=UI_ACCENT, activeforeground="white")
    om.pack(fill="x", padx=18)
    return lambda: next((v for l, v in choices if l == var.get()), choices[0][1])


def _accent_btn(tk, parent, text, cmd):
    return tk.Button(parent, text=text, command=cmd, bg=UI_ACCENT, fg="white", relief="flat",
                     activebackground=UI_ACCENT2, activeforeground="white", borderwidth=0,
                     font=("Segoe UI", 10, "bold"), padx=22, pady=7, cursor="hand2")


def _ghost_btn(tk, parent, text, cmd):
    return tk.Button(parent, text=text, command=cmd, bg=UI_PAPER, fg=UI_INK, relief="flat",
                     activebackground=UI_PAPER, activeforeground=UI_ACCENT, borderwidth=0,
                     font=UI_FONT, cursor="hand2")


_TK_KEY_TO_PART = {
    "Control_R": ("ctrl", "ctrl_r"), "Control_L": ("ctrl", "ctrl_l"),
    "Alt_R":     ("alt",  "alt_r"),  "Alt_L":     ("alt",  "alt_l"),
    "Shift_R":   ("shift","shift_r"),
    "Super_L":   ("win",  "win"),    "Super_R":   ("win",  "win"),
    "Meta_L":    ("win",  "win"),    "Meta_R":    ("win",  "win"),
    "F8": ("f8","f8"), "F9": ("f9","f9"), "F10": ("f10","f10"),
}


def _tk_syms_to_spec(syms):
    """Zet een set Tkinter keysyms om naar een hotkey-spec ('ctrl+win', 'ctrl_r', …)."""
    groups = {}
    for s in syms:
        if s in _TK_KEY_TO_PART:
            g, exact = _TK_KEY_TO_PART[s]
            if g not in groups:
                groups[g] = exact
    if not groups:
        return None
    if len(groups) == 1:
        return list(groups.values())[0]
    order = ["ctrl", "win", "alt", "shift", "f8", "f9", "f10"]
    return "+".join(g for g in order if g in groups) or None


def _keypicker(tk, parent, label, initial):
    """Druk-op-een-toets widget. Klik 'Wijzig…' → druk combo → sla op.
    Op macOS: gebruik dropdown (AppKit-conflict met Tkinter event-capture → crash)."""
    initial = initial or HOTKEY_CHOICES[0][1]
    if IS_MAC:
        return _dropdown(tk, parent, label, HOTKEY_CHOICES, initial)
    val = {"v": initial}
    tk.Label(parent, text=label, bg=UI_PAPER, fg=UI_INK, font=UI_FONT,
             anchor="w").pack(fill="x", padx=18, pady=(10, 2))
    row = tk.Frame(parent, bg=UI_PAPER)
    row.pack(fill="x", padx=18)
    disp_var = tk.StringVar(master=parent, value=_hk_display(initial))
    tk.Label(row, textvariable=disp_var, bg="#ffffff", fg=UI_INK, font=UI_FONT,
             anchor="w", padx=10, pady=5, relief="flat",
             highlightthickness=1, highlightbackground="#dedbd3").pack(side="left", fill="x", expand=True)
    btn = tk.Button(row, text="Change…", bg=UI_PAPER, fg=UI_ACCENT, relief="flat",
                    font=UI_FONT, cursor="hand2", padx=8)
    btn.pack(side="left", padx=(6, 0))
    tk.Button(row, text="Off", bg=UI_PAPER, fg=UI_SUB, relief="flat",
              font=UI_FONT, cursor="hand2", padx=4,
              command=lambda: (val.update(v="uit"), disp_var.set(_hk_display("uit")))).pack(side="left", padx=(2, 0))

    _valid = {v for _, v in HOTKEY_CHOICES}

    def _pynput_to_spec(keys):
        """Zet een set pynput-keys om naar een hotkey-spec ('ctrl+win', 'ctrl_r', …)."""
        from pynput.keyboard import Key as K
        exact_map = {
            K.ctrl_r:  "ctrl_r",  K.ctrl_l:  "ctrl_l",
            K.alt_r:   "alt_r",   K.alt_l:   "alt_l",
            K.shift_r: "shift_r",
        }
        order = ["ctrl", "win", "alt", "shift"]
        group_hit = {}
        for k in keys:
            for g, members in MOD_GROUPS.items():
                if k in members:
                    canonical = "win" if g == "cmd" else g
                    group_hit[canonical] = k
                    break
            else:
                for fn in ("f8", "f9", "f10"):
                    if k == getattr(K, fn, None):
                        group_hit[fn] = k
        unique = [g for g in order + ["f8", "f9", "f10"] if g in group_hit]
        if not unique:
            return None
        if len(unique) == 1:
            g = unique[0]
            if g in ("f8", "f9", "f10"):
                return g
            for k in keys:
                if k in exact_map:
                    return exact_map[k]
        return "+".join(g for g in order if g in group_hit) or None

    def _finish(spec):
        global _keypicking
        _keypicking = False
        pressed.clear()
        btn.config(text="Wijzig…", state="normal")
        if spec and spec in _valid:
            val["v"] = spec
        disp_var.set(_hk_display(val["v"]))

    def _start():
        global _keypicking
        _keypicking = True
        pressed.clear()
        btn.config(text="Druk nu…", state="disabled")
        disp_var.set("—")

        if IS_MAC:
            # Op macOS: gebruik Tkinter-bindings (pynput vereist Accessibility-rechten
            # en conflicteert met de Tk event-loop → crash).
            toplevel = parent.winfo_toplevel()
            _held = set()
            _bid_p = [None]; _bid_r = [None]

            def _cleanup():
                try:
                    if _bid_p[0]: toplevel.unbind("<KeyPress>", _bid_p[0])
                    if _bid_r[0]: toplevel.unbind("<KeyRelease>", _bid_r[0])
                except Exception:
                    pass

            def _on_tk_press(event):
                _held.add(event.keysym)

            def _on_tk_release(event):
                syms = set(_held) | {event.keysym}
                _held.clear()
                _cleanup()
                spec = _tk_syms_to_spec(syms)
                try:
                    toplevel.after(0, lambda: _finish(spec))
                except Exception:
                    _finish(spec)

            _bid_p[0] = toplevel.bind("<KeyPress>", _on_tk_press, add=True)
            _bid_r[0] = toplevel.bind("<KeyRelease>", _on_tk_release, add=True)
            toplevel.focus_force()
        else:
            _captured = set()
            _done = [False]

            def _on_press(key):
                if not _done[0]:
                    _captured.add(key)

            def _on_release(key):
                if _done[0]:
                    return False
                _done[0] = True
                spec = _pynput_to_spec(set(_captured) | {key})
                try:
                    parent.winfo_toplevel().after(0, lambda: _finish(spec))
                except Exception:
                    _finish(spec)
                return False

            keyboard.Listener(on_press=_on_press, on_release=_on_release).start()

    btn.config(command=_start)
    return lambda: val["v"]


def open_dashboard(start_tab="instellingen"):
    """Volledig instellingen-dashboard (tabbladen). Draait op de HUIDIGE thread."""
    import tkinter as tk

    root = tk.Tk()
    root.title("Lazytype")
    root.configure(bg=UI_PAPER)
    root.attributes("-topmost", True)
    root.resizable(False, False)
    W, H = 500, 640
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{W}x{H}+{(sw - W) // 2}+{(sh - H) // 2}")

    # ── Tabbalk ──────────────────────────────────────────────────────────
    tab_bar = tk.Frame(root, bg=UI_PAPER)
    tab_bar.pack(fill="x")
    tk.Frame(tab_bar, bg=UI_LINE, height=1).pack(side="bottom", fill="x")

    content = tk.Frame(root, bg=UI_PAPER)
    content.pack(fill="both", expand=True)

    tab_frames = {}
    tab_btns   = {}
    active     = {"tab": None}

    def show_tab(name):
        for f in tab_frames.values():
            f.pack_forget()
        tab_frames[name].pack(fill="both", expand=True, padx=0, pady=0)
        active["tab"] = name
        for n, b in tab_btns.items():
            if n == name:
                b.config(fg=UI_ACCENT, font=("Segoe UI", 10, "bold"),
                         relief="flat", bd=0, cursor="arrow")
            else:
                b.config(fg=UI_SUB, font=UI_FONT,
                         relief="flat", bd=0, cursor="hand2")

    for tid, tlabel in [("abonnement", "Abonnement"),
                         ("instellingen", "Instellingen"),
                         ("over", "Over & Update")]:
        f = tk.Frame(content, bg=UI_PAPER)
        tab_frames[tid] = f
        b = tk.Button(tab_bar, text=tlabel, bg=UI_PAPER, relief="flat", bd=0,
                      padx=16, pady=10, activebackground=UI_PAPER,
                      activeforeground=UI_ACCENT,
                      command=lambda t=tid: show_tab(t))
        b.pack(side="left")
        tab_btns[tid] = b

    def section(parent, text):
        tk.Label(parent, text=text, bg=UI_PAPER, fg=UI_INK,
                 font=("Segoe UI", 11, "bold"), anchor="w").pack(
                 fill="x", padx=20, pady=(18, 4))

    def divider(parent):
        tk.Frame(parent, bg=UI_LINE, height=1).pack(fill="x", padx=20, pady=(10, 2))

    # ── TAB: Abonnement ───────────────────────────────────────────────────
    ab = tab_frames["abonnement"]
    section(ab, "Abonnement")

    lst = dictate.license_state()
    tier_color = {"pro": "#1f9d57", "trial": UI_ACCENT, "personal": "#1f9d57",
                  "owner": "#1f9d57"}.get(lst["tier"], UI_SUB)
    tier_dot = "●"
    tk.Label(ab, text=f"{tier_dot}  {lst['label']}", bg=UI_PAPER,
             fg=tier_color, font=("Segoe UI", 13, "bold"), anchor="w").pack(
             fill="x", padx=20, pady=(4, 0))

    if lst["tier"] == "trial" and lst.get("days_left") is not None:
        tk.Label(ab, text=f"Nog {lst['days_left']} dag{'en' if lst['days_left'] != 1 else ''} resterend",
                 bg=UI_PAPER, fg=UI_SUB, font=UI_FONT, anchor="w").pack(fill="x", padx=20)

    if not lst["valid"] or lst["tier"] in ("trial",):
        divider(ab)
        section(ab, "Upgraden")
        tk.Label(ab, text="Upgrade naar Personal (eenmalig) of Pro (abonnement)\nvoor onbeperkt gebruik.",
                 bg=UI_PAPER, fg=UI_SUB, font=UI_FONT, justify="left", anchor="w").pack(
                 fill="x", padx=20)
        _accent_btn(tk, ab, "Bekijk abonnementen →",
                    lambda: __import__("webbrowser").open("https://lazytype.com/#pricing")).pack(
                    padx=20, pady=(10, 0), anchor="w")

    divider(ab)
    section(ab, "Licentiesleutel")
    key_val = os.environ.get("LAZYTYPE_LICENSE", "")
    key_disp = (key_val[:28] + "…") if len(key_val) > 30 else (key_val or "—")
    tk.Label(ab, text=key_disp, bg=UI_PAPER, fg=UI_INK,
             font=UI_MONO, anchor="w").pack(fill="x", padx=20)
    _ghost_btn(tk, ab, "Sleutel wijzigen…",
               lambda: set_license_action(None, None)).pack(padx=20, pady=(6, 0), anchor="w")

    if lst["tier"] == "personal":
        divider(ab)
        section(ab, "Groq API-key")
        groq_val = os.environ.get("GROQ_API_KEY", "")
        groq_disp = (groq_val[:20] + "…") if len(groq_val) > 22 else (groq_val or "—")
        tk.Label(ab, text=groq_disp, bg=UI_PAPER, fg=UI_INK,
                 font=UI_MONO, anchor="w").pack(fill="x", padx=20)
        _ghost_btn(tk, ab, "API-key wijzigen…",
                   lambda: set_key_action(None, None)).pack(padx=20, pady=(6, 0), anchor="w")

    # ── TAB: Instellingen ─────────────────────────────────────────────────
    inst = tab_frames["instellingen"]

    def save_settings():
        global _keypicking
        _keypicking = False
        pressed.clear()
        state["hotkey_name"]      = g_dict()
        state["hotkey_command"]   = g_cmd()
        state["hotkey_translate"] = g_tr()
        state["language"]         = g_lang()
        state["translate_target"] = g_tgt()
        state["engine"]           = g_eng()
        for k, envk in (("hotkey_name",      "DICTATE_HOTKEY"),
                        ("hotkey_command",    "DICTATE_COMMAND_HOTKEY"),
                        ("hotkey_translate",  "DICTATE_TRANSLATE_HOTKEY"),
                        ("translate_target",  "DICTATE_TRANSLATE_TARGET"),
                        ("language",          "DICTATE_LANGUAGE"),
                        ("engine",            "DICTATE_ENGINE")):
            dictate.save_env_value(envk, state[k])
        rebuild_hotkeys()
        if icon:
            state["last"] = "Instellingen opgeslagen ✓"
            refresh()
        root.destroy()

    # Save-balk altijd onderaan vastgepind (buiten de scroll-content)
    btn_bar = tk.Frame(inst, bg=UI_PAPER)
    btn_bar.pack(side="bottom", fill="x", padx=20, pady=12)
    tk.Frame(inst, bg=UI_LINE, height=1).pack(side="bottom", fill="x")
    _accent_btn(tk, btn_bar, "Opslaan", save_settings).pack(side="right")
    _ghost_btn(tk, btn_bar, "Annuleren", root.destroy).pack(side="right", padx=(0, 8))

    # Scrollbare inhoud
    scr_frame = tk.Frame(inst, bg=UI_PAPER)
    scr_frame.pack(fill="both", expand=True)

    section(scr_frame, "Sneltoetsen")
    g_dict = _keypicker(tk, scr_frame, "Dicteren — houd in en spreek", state["hotkey_name"])
    g_cmd  = _keypicker(tk, scr_frame, "Command — selecteer tekst + spreek instructie", state["hotkey_command"])
    g_tr   = _keypicker(tk, scr_frame, "Vertalen — dicteren + meteen vertalen", state["hotkey_translate"])
    divider(scr_frame)
    section(scr_frame, "Taal & Engine")
    g_lang = _dropdown(tk, scr_frame, "Spreektaal", SPOKEN_CHOICES, state["language"])
    g_tgt  = _dropdown(tk, scr_frame, "Vertaal naar", LANG_CHOICES, state["translate_target"])
    g_eng  = _dropdown(tk, scr_frame, "Engine", ENGINE_CHOICES, state["engine"])

    # ── TAB: Over & Update ────────────────────────────────────────────────
    ov = tab_frames["over"]
    section(ov, "Over Lazytype")
    tk.Label(ov, text=f"Versie {APP_VERSION}", bg=UI_PAPER, fg=UI_INK,
             font=("Segoe UI", 12, "bold"), anchor="w").pack(fill="x", padx=20)
    tk.Label(ov, text="Dicteersoftware voor Windows en macOS",
             bg=UI_PAPER, fg=UI_SUB, font=UI_FONT, anchor="w").pack(fill="x", padx=20)

    divider(ov)
    section(ov, "Update")
    upd_var = tk.StringVar()
    upd_lbl = tk.Label(ov, textvariable=upd_var, bg=UI_PAPER, fg=UI_SUB,
                       font=UI_FONT, anchor="w")
    upd_lbl.pack(fill="x", padx=20)
    upd_btn_frame = tk.Frame(ov, bg=UI_PAPER)
    upd_btn_frame.pack(fill="x", padx=20, pady=(8, 0))

    def check_now():
        upd_var.set("Controleren…")
        root.update()
        try:
            import requests
            r = requests.get("https://lazytype.com/version.json", timeout=8)
            latest = r.json().get("version", "")
            if latest and latest != APP_VERSION:
                upd_var.set(f"Nieuwe versie beschikbaar: v{latest}")
                upd_lbl.config(fg="#1f9d57")
                if IS_WIN and getattr(sys, "frozen", False):
                    _accent_btn(tk, upd_btn_frame, f"Installeer v{latest}",
                                lambda v=latest: _install_update(v, root)).pack(side="left")
                else:
                    _ghost_btn(tk, upd_btn_frame, "Download →",
                               lambda: __import__("webbrowser").open("https://lazytype.com")).pack(side="left")
            else:
                upd_var.set("Je hebt de nieuwste versie.")
        except Exception:
            upd_var.set("Kan niet controleren — check je internetverbinding.")

    if _update_info:
        upd_var.set(f"Nieuwe versie beschikbaar: v{_update_info}")
        upd_lbl.config(fg="#1f9d57")
        if IS_WIN and getattr(sys, "frozen", False):
            _accent_btn(tk, upd_btn_frame, f"Installeer v{_update_info}",
                        lambda v=_update_info: _install_update(v, root)).pack(side="left")
        else:
            _ghost_btn(tk, upd_btn_frame, "Download →",
                       lambda: __import__("webbrowser").open("https://lazytype.com")).pack(side="left")
    else:
        upd_var.set("Je hebt de nieuwste versie.")
        _ghost_btn(tk, upd_btn_frame, "Controleer op updates",
                   lambda: threading.Thread(target=check_now, daemon=True).start()).pack(side="left")

    divider(ov)
    tk.Label(ov, text="© Lazytype — lazytype.com",
             bg=UI_PAPER, fg=UI_SUB, font=("Segoe UI", 9), anchor="w").pack(
             fill="x", padx=20, pady=(8, 0))
    _ghost_btn(tk, ov, "Website openen",
               lambda: __import__("webbrowser").open("https://lazytype.com")).pack(
               padx=20, pady=(4, 0), anchor="w")

    # ── Afsluiten ─────────────────────────────────────────────────────────
    def on_close():
        global _keypicking
        _keypicking = False
        pressed.clear()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    show_tab(start_tab)
    root.mainloop()


def _install_update(new_version: str, parent_window=None):
    """Download nieuwe exe en vervang na afsluiten (alleen Windows, frozen)."""
    import urllib.request, tempfile
    try:
        tmp = Path(tempfile.gettempdir()) / "Lazytype_update.exe"
        urllib.request.urlretrieve("https://lazytype.com/downloads/Lazytype.exe", tmp)
        exe = Path(sys.executable)
        bat = Path(tempfile.gettempdir()) / "lazytype_update.bat"
        bat.write_text(
            f"@echo off\r\ntimeout /t 2 /nobreak >nul\r\n"
            f"copy /y \"{tmp}\" \"{exe}\"\r\n"
            f"start \"\" \"{exe}\"\r\ndel \"%~f0\"\r\n",
            encoding="utf-8")
        import subprocess
        subprocess.Popen(["cmd", "/c", str(bat)], creationflags=0x08000000)
        if parent_window:
            parent_window.destroy()
        if icon:
            icon.stop()
        sys.exit(0)
    except Exception as e:
        import tkinter.messagebox as mb
        mb.showerror("Update mislukt", str(e))


def _run_settings_window():
    """Achterwaartse compatibiliteit: opent het dashboard op de instellingen-tab."""
    open_dashboard("instellingen")


def _reload_from_env():
    """Herlaad instellingen uit .env na een externe wijziging (bijv. macOS settings-subprocess)."""
    env_path = dictate.ROOT / ".env"
    if not env_path.exists():
        return
    vals = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            vals[k.strip()] = v.strip()
    mapping = {
        "DICTATE_HOTKEY": "hotkey_name",
        "DICTATE_COMMAND_HOTKEY": "hotkey_command",
        "DICTATE_TRANSLATE_HOTKEY": "hotkey_translate",
        "DICTATE_TRANSLATE_TARGET": "translate_target",
        "DICTATE_LANGUAGE": "language",
        "DICTATE_ENGINE": "engine",
    }
    for env_key, state_key in mapping.items():
        if env_key in vals and vals[env_key]:
            state[state_key] = vals[env_key]
            os.environ[env_key] = vals[env_key]
    rebuild_hotkeys()
    if icon:
        state["last"] = "Instellingen opgeslagen ✓"; refresh()


def open_settings(icon_=None, item=None):
    if IS_MAC:
        def _mac():
            import subprocess
            proc = subprocess.Popen([sys.executable, "--settings"])
            proc.wait()
            _reload_from_env()
        threading.Thread(target=_mac, daemon=True).start()
        return
    threading.Thread(target=_run_settings_window, daemon=True).start()


def run_onboarding():
    """First-run wizard (runs on main thread, before the tray loop)."""
    import tkinter as tk
    root = tk.Tk()
    root.title("Welcome to Lazytype")
    root.configure(bg=UI_PAPER)
    root.attributes("-topmost", True)
    root.resizable(False, False)
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    W, H = 480, 510
    root.geometry(f"{W}x{H}+{(sw - W) // 2}+{(sh - H) // 2}")
    body = tk.Frame(root, bg=UI_PAPER)
    body.pack(fill="both", expand=True)
    sel = {"d": state["hotkey_name"], "t": state["hotkey_translate"],
           "g": state["translate_target"], "l": state["language"] or "en"}

    UI_LANG_CHOICES = [
        ("English", "en"), ("Nederlands", "nl"), ("Deutsch", "de"),
        ("Français", "fr"), ("Español", "es"), ("Italiano", "it"),
    ]
    TRANSLATE_TO_CHOICES = [
        ("Dutch", "nl"), ("English", "en"), ("German", "de"), ("French", "fr"),
        ("Spanish", "es"), ("Italian", "it"), ("Portuguese", "pt"),
        ("Polish", "pl"), ("Russian", "ru"), ("Turkish", "tr"),
        ("Arabic", "ar"), ("Japanese", "ja"), ("Chinese", "zh"), ("Korean", "ko"),
    ]

    def clear():
        for w in body.winfo_children():
            w.destroy()

    def logo_row(step_n, total=5):
        hdr = tk.Frame(body, bg=UI_PAPER)
        hdr.pack(fill="x", padx=24, pady=(18, 0))
        c = tk.Canvas(hdr, width=30, height=30, bg=UI_PAPER, highlightthickness=0)
        c.pack(side="left")
        c.create_rectangle(0, 0, 30, 30, fill=UI_INK, outline="")
        for corner in [(0,0,6,6),(24,0,30,6),(0,24,6,30),(24,24,30,30)]:
            c.create_rectangle(*corner, fill=UI_PAPER, outline="")
        c.create_rectangle(6,  12, 9,  20, fill="#f5f6fa", outline="")
        c.create_rectangle(11, 8,  14, 22, fill="#f5f6fa", outline="")
        c.create_rectangle(16, 14, 19, 19, fill="#f5f6fa", outline="")
        c.create_rectangle(21, 6,  23, 24, fill=UI_ACCENT, outline="")
        tk.Label(hdr, text="Lazytype", bg=UI_PAPER, fg=UI_INK,
                 font=("Segoe UI", 13, "bold")).pack(side="left", padx=(8, 0))
        tk.Label(hdr, text=f"Step {step_n} of {total}", bg=UI_PAPER, fg=UI_ACCENT,
                 font=("Segoe UI", 9, "bold")).pack(side="right")

    def head(t, sub):
        tk.Label(body, text=t, bg=UI_PAPER, fg=UI_INK, font=("Segoe UI", 15, "bold"),
                 wraplength=W - 48, justify="left", anchor="w").pack(fill="x", padx=24, pady=(12, 4))
        tk.Label(body, text=sub, bg=UI_PAPER, fg=UI_SUB, font=("Segoe UI", 10),
                 wraplength=W - 48, justify="left", anchor="w").pack(fill="x", padx=24)

    def nav(back_cmd, primary_text, primary_cmd, skip_cmd=None):
        bar = tk.Frame(body, bg=UI_PAPER)
        bar.pack(side="bottom", fill="x", padx=22, pady=16)
        _accent_btn(tk, bar, primary_text, primary_cmd).pack(side="right")
        if skip_cmd:
            _ghost_btn(tk, bar, "Skip", skip_cmd).pack(side="right", padx=(0, 6))
        if back_cmd:
            _ghost_btn(tk, bar, "← Back", back_cmd).pack(side="left")

    # ── Step 1: Language ──────────────────────────────────────────────────
    def s_lang():
        clear()
        logo_row(1)
        head("Choose your language",
             "This sets both the app interface and the language you'll speak when dictating.")
        grid = tk.Frame(body, bg=UI_PAPER)
        grid.pack(fill="x", padx=24, pady=(14, 0))
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)
        btns = {}

        def select_lang(code):
            sel["l"] = code
            for c2, b in btns.items():
                b.config(bg=UI_ACCENT if c2 == code else "#ffffff",
                         fg="#ffffff" if c2 == code else UI_INK,
                         highlightbackground=UI_ACCENT if c2 == code else "#dedbd3")

        for i, (name, code) in enumerate(UI_LANG_CHOICES):
            b = tk.Button(grid, text=name, font=("Segoe UI", 10),
                          bg="#ffffff", fg=UI_INK, relief="flat", bd=0,
                          cursor="hand2", pady=8, highlightthickness=1,
                          highlightbackground="#dedbd3",
                          command=lambda c=code: select_lang(c))
            b.grid(row=i // 2, column=i % 2, padx=4, pady=4, sticky="ew")
            btns[code] = b

        cur = sel["l"] if sel["l"] in dict(UI_LANG_CHOICES).values() else "en"
        select_lang(cur)
        nav(None, "Continue →", s_welcome)

    # ── Step 2: Welcome ───────────────────────────────────────────────────
    def s_welcome():
        clear()
        logo_row(2)
        head("Welcome to Lazytype",
             "Hold a key, speak, and your words appear — in any app.")

        lang_name = LANG_DISPLAY.get(sel["l"], "your language")
        get_name  = LANG_DISPLAY.get(sel["g"], "another language")

        feats = [
            ("♪", "Press & speak  (hold key while speaking)",
             "Release to stop. Text appears instantly in your active app — "
             "Word, Slack, Gmail, anywhere."),
            ("⇄", "Translate as you speak",
             f"Speak {lang_name} and instantly get {get_name} back — AI corrects grammar "
             f"and phrasing so the result reads naturally, not like a machine translation.\n"
             f"You'll choose the target language and set the key in step 4."),
            ("⊞", "Works everywhere",
             "Any text field on your computer — browser, editor, chat, email."),
        ]

        for icon_ch, title, desc in feats:
            row = tk.Frame(body, bg=UI_PAPER)
            row.pack(fill="x", padx=24, pady=(10, 0))
            icon_f = tk.Frame(row, bg="#eeedfe", width=34, height=34)
            icon_f.pack(side="left", anchor="n")
            icon_f.pack_propagate(False)
            tk.Label(icon_f, text=icon_ch, bg="#eeedfe", fg=UI_ACCENT,
                     font=("Segoe UI", 13)).pack(expand=True)
            txt_f = tk.Frame(row, bg=UI_PAPER)
            txt_f.pack(side="left", fill="x", expand=True, padx=(10, 0))
            tk.Label(txt_f, text=title, bg=UI_PAPER, fg=UI_INK,
                     font=("Segoe UI", 10, "bold"), anchor="w",
                     justify="left").pack(fill="x")
            tk.Label(txt_f, text=desc, bg=UI_PAPER, fg=UI_SUB,
                     font=("Segoe UI", 9), anchor="w", justify="left",
                     wraplength=W - 110).pack(fill="x")

        nav(s_lang, "Next →", s_dictkey)

    # ── Step 3: Dictation key ─────────────────────────────────────────────
    def s_dictkey():
        clear()
        logo_row(3)
        head("Choose your dictation key",
             "Hold this key while you speak, then release to stop. "
             "It won't interfere with normal keyboard use.")
        g = _keypicker(tk, body, "Dictation key", sel["d"])
        tk.Label(body, text="Popular: Right Ctrl · Right Alt · F8 · F9 · Ctrl + Alt",
                 bg=UI_PAPER, fg=UI_SUB, font=("Segoe UI", 9),
                 anchor="w").pack(fill="x", padx=24, pady=(4, 0))
        nav(s_welcome, "Next →", lambda: (sel.update(d=g()), s_transkey()))

    # ── Step 4: Translation key + target language ─────────────────────────
    def s_transkey():
        clear()
        logo_row(4)
        head("Translation key",
             "Hold this key to dictate and translate at the same time. "
             "You can skip this and set it up later in Settings.")
        lang_name = LANG_DISPLAY.get(sel["l"], sel["l"])
        info = tk.Frame(body, bg="#f0efe9")
        info.pack(fill="x", padx=24, pady=(10, 0))
        tk.Label(info, text=f"Speak: {lang_name}  ·  set in step 1",
                 bg="#f0efe9", fg=UI_SUB, font=("Segoe UI", 9),
                 anchor="w", padx=10, pady=5).pack(fill="x")
        g2 = _dropdown(tk, body, "Translate to", TRANSLATE_TO_CHOICES, sel["g"])
        g1 = _keypicker(tk, body, "Translation key", sel["t"])
        tk.Label(body, text="Popular: Right Alt · F9 · Ctrl + Alt",
                 bg=UI_PAPER, fg=UI_SUB, font=("Segoe UI", 9),
                 anchor="w").pack(fill="x", padx=24, pady=(4, 0))
        save_and_next = lambda: (sel.update(t=g1(), g=g2()), s_email())
        nav(s_dictkey, "Next →", save_and_next, skip_cmd=save_and_next)

    # ── Step 5: Email + summary + trial ──────────────────────────────────
    def s_email():
        clear()
        logo_row(5)
        try:
            import urllib.request
            urllib.request.urlopen("https://lazytype.com/version.json", timeout=5)
        except Exception:
            head("No internet connection",
                 "An internet connection is required to start the free trial. "
                 "Check your connection and restart Lazytype.")
            bar = tk.Frame(body, bg=UI_PAPER)
            bar.pack(side="bottom", fill="x", padx=22, pady=20)
            _ghost_btn(tk, bar, "← Back", s_transkey).pack(side="left")
            _accent_btn(tk, bar, "Quit", root.destroy).pack(side="right")
            return

        head("You're all set — let's go",
             "Start your 14-day free trial. No credit card needed.")

        lang_name = LANG_DISPLAY.get(sel["l"], sel["l"])
        get_name  = LANG_DISPLAY.get(sel["g"], sel["g"])
        hk_name   = _hk_display(sel["d"])
        tr_name   = _hk_display(sel["t"])

        sum_f = tk.Frame(body, bg="#f0efe9")
        sum_f.pack(fill="x", padx=24, pady=(10, 0))
        tk.Label(sum_f,
                 text=f"Dictation key: {hk_name}  —  hold to speak in {lang_name}",
                 bg="#f0efe9", fg=UI_INK, font=("Segoe UI", 9),
                 anchor="w", padx=10, pady=4).pack(fill="x")
        tk.Frame(sum_f, bg="#dedbd3", height=1).pack(fill="x", padx=10)
        tk.Label(sum_f,
                 text=f"Translation key: {tr_name}  —  {lang_name} → {get_name}, AI-corrected",
                 bg="#f0efe9", fg=UI_INK, font=("Segoe UI", 9),
                 anchor="w", padx=10, pady=4).pack(fill="x")

        tk.Frame(body, bg="#dedbd3", height=1).pack(fill="x", padx=24, pady=(12, 6))

        ef = tk.Frame(body, bg=UI_PAPER)
        ef.pack(fill="x", padx=24)
        tk.Label(ef, text="Email address", bg=UI_PAPER, fg=UI_SUB,
                 font=UI_FONT, anchor="w").pack(fill="x")
        email_var = tk.StringVar()
        email_entry = tk.Entry(ef, textvariable=email_var, font=UI_FONT,
                               relief="flat", bg="#ffffff", fg=UI_INK,
                               insertbackground=UI_INK, highlightthickness=1,
                               highlightbackground="#dedbd3", highlightcolor=UI_ACCENT)
        email_entry.pack(fill="x", ipady=6, pady=(4, 0))
        email_entry.focus_set()

        status_var = tk.StringVar()
        tk.Label(body, textvariable=status_var, bg=UI_PAPER, fg=UI_SUB,
                 font=("Segoe UI", 9), anchor="w").pack(fill="x", padx=24, pady=(4, 0))

        alt = tk.Frame(body, bg=UI_PAPER)
        alt.pack(fill="x", padx=24, pady=(4, 0))
        tk.Label(alt, text="Already have a license or Pro key?",
                 bg=UI_PAPER, fg=UI_SUB, font=("Segoe UI", 9)).pack(side="left")
        _ghost_btn(tk, alt, "Enter it here…",
                   lambda: set_license_action(None, None)).pack(side="left", padx=(6, 0))

        def finish():
            global _keypicking
            _keypicking = False
            pressed.clear()
            state["hotkey_name"]      = sel["d"]
            state["hotkey_translate"] = sel["t"]
            state["translate_target"] = sel["g"]
            state["language"]         = sel["l"]
            for k, envk in (("hotkey_name",     "DICTATE_HOTKEY"),
                            ("hotkey_translate", "DICTATE_TRANSLATE_HOTKEY"),
                            ("translate_target", "DICTATE_TRANSLATE_TARGET"),
                            ("language",         "DICTATE_LANGUAGE")):
                dictate.save_env_value(envk, state[k])
            dictate.save_env_value("DICTATE_ONBOARDED", "1")
            rebuild_hotkeys()
            root.destroy()

        def start_trial():
            email = email_var.get().strip()
            if not email or "@" not in email:
                status_var.set("Please enter a valid email address.")
                return
            status_var.set("Requesting your trial key…")
            root.update()
            try:
                key = _request_trial_key(email)
                dictate.save_env_value("LAZYTYPE_LICENSE", key)
                os.environ["LAZYTYPE_LICENSE"] = key
                state["engine"] = "managed"
                dictate.save_env_value("DICTATE_ENGINE", "managed")
                status_var.set("Done! 14 days free. Check your email for the key.")
                root.after(1200, finish)
            except Exception as e:
                status_var.set(f"Error: {e}")

        bar = tk.Frame(body, bg=UI_PAPER)
        bar.pack(side="bottom", fill="x", padx=22, pady=14)
        _accent_btn(tk, bar, "Start free trial →", start_trial).pack(side="right")
        _ghost_btn(tk, bar, "← Back", s_transkey).pack(side="left")
        _ghost_btn(tk, bar, "Skip for now", finish).pack(side="left", padx=(12, 0))

    def skip():
        global _keypicking
        _keypicking = False
        pressed.clear()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", skip)
    s_lang()
    root.mainloop()


def build_menu():
    engines = [
        ("Managed — abonnement (geen eigen key)", "managed"),
        ("Groq (eigen key)", "groq"),
        ("OpenAI", "openai"),
        ("Lokaal (offline)", "local"),
    ]
    langs = [
        ("Nederlands", "nl"), ("Engels", "en"), ("Duits", "de"),
        ("Frans", "fr"), ("Automatisch", "auto"),
    ]
    engine_menu = pystray.Menu(*[
        pystray.MenuItem(label, choose_engine(code), radio=True,
                         checked=(lambda c: (lambda item: state["engine"] == c))(code))
        for label, code in engines
    ])
    lang_menu = pystray.Menu(*[
        pystray.MenuItem(label, choose_language(code), radio=True,
                         checked=(lambda c: (lambda item: state["language"] == c))(code))
        for label, code in langs
    ])
    postproc = [
        ("Uit", "off"),
        ("Opschonen (zelfde taal)", "clean"),
        ("Vertaal → Engels", "en"),
        ("Vertaal → Nederlands", "nl"),
        ("Vertaal → Duits", "de"),
        ("Vertaal → Frans", "fr"),
        ("Vertaal → Spaans", "es"),
    ]
    postproc_menu = pystray.Menu(*[
        pystray.MenuItem(label, choose_postprocess(code), radio=True,
                         checked=(lambda c: (lambda item: state["postprocess"] == c))(code))
        for label, code in postproc
    ])
    admin_menu = pystray.Menu(
        pystray.MenuItem("Nieuwe sleutel…", admin_gen_action),
        pystray.MenuItem("Sleutels tonen", _admin_show("list_text", "Admin — uitgegeven sleutels")),
        pystray.MenuItem("Sleutel verifiëren…", admin_verify_action),
        pystray.MenuItem("Sleutel intrekken…", admin_revoke_action),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Prijzen & tiers", _admin_show("tiers_text", "Lazytype — prijzen & tiers")),
        pystray.MenuItem("Server-config", _admin_show("server_text", "Admin — server-config")),
    )
    hist = dictate.load_history()
    hist_items = ([pystray.MenuItem(_hist_label(h.get("text", "")), history_copy(h.get("text", "")))
                   for h in hist[:12]]
                  or [pystray.MenuItem("— leeg —", None, enabled=False)])
    hist_menu = pystray.Menu(
        *hist_items,
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Bewaren", toggle_history, checked=lambda item: state["history"]),
        pystray.MenuItem("Wissen", clear_history_action),
    )
    update_items = (
        pystray.MenuItem(
            f"↑ Update beschikbaar: v{_update_info} — lazytype.com",
            lambda icon_, item_: __import__("webbrowser").open("https://lazytype.com"),
        ),
        pystray.Menu.SEPARATOR,
    ) if _update_info else ()
    return pystray.Menu(
        pystray.MenuItem(lambda item: f"● {PHASE_LABEL[current_phase()]}", None, enabled=False),
        pystray.MenuItem(lambda item: short_last(), None, enabled=False),
        pystray.MenuItem(lambda item: license_status(), None, enabled=False),
        *update_items,
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Engine", engine_menu),
        pystray.MenuItem("Taal (spreektaal)", lang_menu),
        pystray.MenuItem("Nabewerking (AI)", postproc_menu),
        pystray.MenuItem("Woordenboek bewerken…", _edit_file_action(
            dictate.DICTIONARY_FILE, "Lazytype — Woordenboek",
            "Eén term per regel (namen/jargon). Verbetert de herkenning.",
            "# Eén term per regel: namen, jargon, afkortingen.\n")),
        pystray.MenuItem("Snippets bewerken…", _edit_file_action(
            dictate.SNIPPETS_FILE, "Lazytype — Snippets",
            "Per regel:  trigger = tekst   (gebruik \\n voor een nieuwe regel).",
            "# trigger = uit te vouwen tekst\n# bijv:  mijn agenda = https://calendly.com/bas\n")),
        pystray.MenuItem("Geschiedenis (klik = kopieer)", hist_menu),
        pystray.MenuItem("Dicteren actief", toggle_enabled,
                         checked=lambda item: state["enabled"]),
        pystray.MenuItem("Overlay-balkje tonen", toggle_overlay,
                         checked=lambda item: state["overlay"]),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Instellingen…", open_settings),
        pystray.MenuItem("Abonnement-sleutel invoeren…", set_license_action),
        pystray.MenuItem("API-key instellen…", set_key_action),
        pystray.MenuItem("Automatisch starten", toggle_autostart,
                         checked=lambda item: is_autostart(),
                         visible=autostart_supported()),
        pystray.MenuItem("Admin", admin_menu, visible=lambda item: is_owner()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lambda item: f"Dicteren: {_hk_display(state['hotkey_name'])}", None, enabled=False),
        pystray.MenuItem(lambda item: f"Command: {_hk_display(state['hotkey_command'])}", None, enabled=False),
        pystray.MenuItem(lambda item: f"Vertaal→{state['translate_target']}: {_hk_display(state['hotkey_translate'])}",
                         None, enabled=False),
        pystray.MenuItem("Afsluiten", do_quit),
    )


# ── Klein drijvend overlay-pilletje ─────────────────────────────────────────
# Layout: [4 bars] | [nl] [→en alleen als actief] [⚙]
# Idle: bars vlak op 3px · Opnemen: bars animeren · Transcriberen: bars bevriezen (amber)
class Overlay:
    PILL     = "#0d0e13"
    PILL_REC = "#130d1a"
    PILL_BSY = "#110d18"
    BORDER   = "#1c1426"
    BAR_IDLE = "#4c1d95"   # dim paars bij rust
    BAR_REC  = "#c084fc"   # helder paars bij opnemen
    BAR_BSY  = "#7c3aed"   # middel paars bij transcriberen

    W = 88    # 6 bars
    H = 36

    def __init__(self):
        self.root = None; self.thread = None; self.cv = None
        self._t = 0; self._sm = 0.0
        self._bar_h = [1.5] * 6
        self._frozen = [4.0, 11.0, 7.0, 9.0, 5.0, 8.0]
        self._chips = []; self._drag = None; self._moved = False
        self._alpha = 0.25

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._safe, daemon=True)
        self.thread.start()

    def stop(self):
        r = self.root; self.root = None
        if r:
            try: r.after(0, r.destroy)
            except Exception: pass

    def _safe(self):
        try: self._run()
        except Exception as e: print(f"  (overlay uit: {e})")

    def _run(self):
        import tkinter as tk
        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        transp = "#000001"
        try:
            root.configure(bg=transp)
            root.attributes("-transparentcolor", transp)
        except Exception:
            transp = self.PILL; root.configure(bg=transp)
        try: root.attributes("-alpha", 0.25)
        except Exception: pass
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{self.W}x{self.H}+{(sw - self.W) // 2}+{sh - self.H - 60}")
        self.cv = tk.Canvas(root, width=self.W, height=self.H,
                            highlightthickness=0, bg=transp, cursor="hand2")
        self.cv.pack()
        self.cv.bind("<ButtonPress-1>",   self._press)
        self.cv.bind("<B1-Motion>",       self._motion)
        self.cv.bind("<ButtonRelease-1>", self._release)
        self.root = root
        self._tick()
        root.mainloop()

    def _tick(self):
        if not self.root: return
        try: self._draw()
        except Exception: pass
        self._t += 1
        try: self.root.after(40, self._tick)
        except Exception: pass

    # ── tekenhelpers ────────────────────────────────────────────────────────

    def _pill(self, x0, y0, x1, y1, r, fill):
        self.cv.create_polygon(
            [x0+r, y0, x1-r, y0, x1, y0, x1, y0+r,
             x1, y1-r, x1, y1, x1-r, y1, x0+r, y1,
             x0, y1, x0, y1-r, x0, y0+r, x0, y0],
            smooth=True, fill=fill, outline=fill)

    def _chip_draw(self, x0, x1, cy, text, fg, bg, cb):
        r = 5
        self._pill(x0, cy-8, x1, cy+8, r, bg)
        self.cv.create_text((x0+x1)//2, cy, text=text, fill=fg,
                            font=("Segoe UI", 8, "bold"), anchor="c")
        self._chips.append((x0, x1, cy-9, cy+9, cb))

    # ── hoofd-renderfunctie ──────────────────────────────────────────────────

    def _draw(self):
        cv = self.cv; cv.delete("all")
        self._chips = []
        ph = "recording" if state["phase"] == "arming" else current_phase()
        W, H = self.W, self.H
        cy = H // 2

        # Audio-niveau smoothen
        self._sm += (recorder.last_level - self._sm) * 0.35
        lv = self._sm

        # Bar-hoogtes — elke bar eigen tempo + fase voor organisch gevoel
        _freqs  = [0.43, 0.55, 0.38, 0.62, 0.48, 0.52]
        _phases = [0.0,  0.9,  1.8,  2.7,  3.6,  4.5]
        if ph == "recording":
            for i in range(6):
                osc    = 0.25 + 0.75 * abs(math.sin(self._t * _freqs[i] + _phases[i]))
                target = 2.0 + lv * 28 * osc + abs(math.sin(self._t * _freqs[i] * 0.6 + i)) * 5
                target = min(target, H - 4)
                speed  = 0.55 if target > self._bar_h[i] else 0.22
                self._bar_h[i] += (target - self._bar_h[i]) * speed
                self._frozen[i] = self._bar_h[i]
        elif ph != "working":
            for i in range(6):
                idle = 1.5 + 0.6 * abs(math.sin(self._t * 0.04 + _phases[i] * 0.4))
                self._bar_h[i] += (idle - self._bar_h[i]) * 0.06

        # Venster-alpha: 25% rust → 100% actief
        target_alpha = 1.0 if ph in ("recording", "working") else 0.25
        self._alpha += (target_alpha - self._alpha) * 0.15
        try: self.root.attributes("-alpha", round(self._alpha, 3))
        except Exception: pass

        # Pill
        bg = self.PILL_REC if ph == "recording" else self.PILL_BSY if ph == "working" else self.PILL
        self._pill(2, 2, W-2, H-2, 14, self.BORDER)
        self._pill(3, 3, W-3, H-3, 13, bg)

        # 6 paarse verticale bars
        bar_col = self.BAR_REC if ph == "recording" else self.BAR_BSY if ph == "working" else self.BAR_IDLE
        CENTERS = [12.0, 24.0, 36.0, 52.0, 64.0, 76.0]
        heights = self._frozen if ph == "working" else self._bar_h
        for bx, bh in zip(CENTERS, heights):
            cv.create_line(bx, cy - bh/2, bx, cy + bh/2,
                           fill=bar_col, width=5, capstyle="round")

    # ── acties ──────────────────────────────────────────────────────────────

    def _cycle_lang(self):
        order = ["nl", "en", "de", "fr", "es", "auto"]
        i = order.index(state["language"]) if state["language"] in order else -1
        state["language"] = order[(i + 1) % len(order)]
        dictate.save_env_value("DICTATE_LANGUAGE", state["language"])

    def _cycle_ai(self):
        order = ["off", "clean", "en", "nl", "de", "fr"]
        i = order.index(state["postprocess"]) if state["postprocess"] in order else -1
        state["postprocess"] = order[(i + 1) % len(order)]
        dictate.save_env_value("DICTATE_POSTPROCESS", state["postprocess"])

    def _open_settings(self):
        threading.Thread(target=_run_settings_window, daemon=True).start()

    # ── drag ────────────────────────────────────────────────────────────────

    def _press(self, e):
        self._drag = (e.x_root, e.y_root, self.root.winfo_x(), self.root.winfo_y())
        self._moved = False

    def _motion(self, e):
        if not self._drag: return
        dx, dy = e.x_root - self._drag[0], e.y_root - self._drag[1]
        if abs(dx) + abs(dy) > 4: self._moved = True
        self.root.geometry(f"+{self._drag[2]+dx}+{self._drag[3]+dy}")

    def _release(self, e):
        moved, self._drag = self._moved, None
        if moved: return
        for x0, x1, y0, y1, cb in self._chips:
            if x0 <= e.x <= x1 and y0 <= e.y <= y1:
                cb(); return


overlay_ui = Overlay()


def toggle_overlay(icon_, item):
    state["overlay"] = not state["overlay"]
    dictate.save_env_value("DICTATE_OVERLAY", "1" if state["overlay"] else "0")
    overlay_ui.start() if state["overlay"] else overlay_ui.stop()
    refresh()


def main():
    global icon

    if "--make-icons" in sys.argv:
        here = Path(__file__).resolve().parent
        accent = ACCENT["idle"]
        # app-icoon (squircle) als .ico in meerdere maten (voor de exe)
        app = render_icon(accent, size=256, shape="squircle")
        app.save(here / "icon.ico",
                 sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
        # macOS app-icoon (.icns) — voor de Mac-build
        try:
            render_icon(accent, size=512, shape="squircle").save(here / "icon.icns")
            print("icon.icns geschreven")
        except Exception as e:
            print(f"(icon.icns overgeslagen — maak op Mac met iconutil: {e})")
        # NB: site/favicon.png en site/icon-512.png bewust NIET overschreven
        # (die horen bij de website; we stemmen alleen de app erop af).
        # preview-montage van app-icoon + tray-statussen (warm-witte achtergrond zoals de site)
        cell = 96
        prev = Image.new("RGBA", (cell * 5 + 30, cell + 10), (247, 246, 243, 255))
        imgs = [("app", render_icon(accent, cell, "squircle"))] + \
               [(k, make_icon(k, cell)) for k in ("idle", "recording", "working", "disabled")]
        for i, (_, im) in enumerate(imgs):
            prev.paste(im, (i * (cell + 5) + 5, 5), im)
        prev.save(here / "icon-preview.png")
        print("app-icons gemaakt: icon.ico, icon.icns, icon-preview.png "
              "(site-favicons ongemoeid gelaten)")
        return

    if "--settings" in sys.argv:
        _run_settings_window()
        return

    if "--selftest" in sys.argv:
        # bouw alles op zonder de blokkerende tray-loop; schrijf een preview
        menu = build_menu()

        # render élk menu-item (tekst + checked) net zoals pystray dat doet,
        # zodat callable-fouten hier opduiken i.p.v. pas in het systeemvak
        def render(items, depth=0):
            for it in items:
                label = it.text  # roept de tekst-callable aan met het item
                mark = ""
                try:
                    if it.checked is not None:
                        mark = " [x]" if it.checked else " [ ]"
                except Exception:
                    pass
                print("   " * depth + f"- {label}{mark}")
                if it.submenu:
                    render(it.submenu, depth + 1)

        # roep ook de fasewissels aan, zodat dynamische labels in elke staat kloppen
        for ph in ("idle", "recording", "working"):
            state["phase"] = ph
            render(menu.items)

        montage = Image.new("RGBA", (64 * 4 + 30, 64), (24, 27, 37, 255))
        for i, k in enumerate(["idle", "recording", "working", "disabled"]):
            montage.paste(ICONS[k], (i * (64 + 10) + 5, 0), ICONS[k])
        montage.save("tray-preview.png")
        state["phase"] = "idle"
        print("selftest OK — alle menu-teksten gerenderd; preview -> tray-preview.png")
        return

    # Onboarding bij eerste start, of als er nog niets geconfigureerd is (gebroken .env).
    _nothing_configured = (
        not os.environ.get("DICTATE_ENGINE") and
        not os.environ.get("LAZYTYPE_LICENSE") and
        not os.environ.get("GROQ_API_KEY")
    )
    if not os.environ.get("DICTATE_ONBOARDED") or _nothing_configured:
        try:
            run_onboarding()
        except Exception as e:
            print(f"  (onboarding overgeslagen: {e})")

    # Groq-key vragen alleen als de gebruiker groq expliciet heeft gekozen (niet standaard bij eerste start).
    if state["engine"] == "groq" and not os.environ.get("GROQ_API_KEY") and os.environ.get("DICTATE_ENGINE"):
        try:
            key = ask_groq_key()
            if key:
                dictate.save_env_value("GROQ_API_KEY", key.strip())
        except Exception as e:
            print(f"  (key-dialoog overgeslagen: {e})")

    icon = pystray.Icon("lazytype", ICONS["idle"], "Lazytype", build_menu())
    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.start()
    threading.Thread(target=_check_update, daemon=True).start()
    threading.Thread(target=dictate.verify_personal_key, daemon=True).start()

    print("=" * 58)
    print("  🎙️  Lazytype (systeemvak) is actief")
    print(f"  Engine     : {state['engine']}")
    print(f"  Taal       : {state['language']}")
    print(f"  Nabewerking: {state['postprocess']}")
    print(f"  {license_status()}")
    if is_owner():
        print("  Admin      : actief (geheim aanwezig) — Admin-menu zichtbaar")
    print(f"  Dicteren   : HOUD '{state['hotkey_name']}' in, spreek, laat los")
    print(f"  Command    : selecteer tekst, HOUD '{state['hotkey_command']}' in, spreek de instructie")
    print(f"  Vertalen   : HOUD '{state['hotkey_translate']}' in → vertaal naar {state['translate_target']}")
    print("  Bediening: rechtsklik op het icoon in het systeemvak")
    print("=" * 58)

    refresh()
    if state["overlay"]:
        overlay_ui.start()
    icon.run()
    listener.stop()
    overlay_ui.stop()
    if overlay_ui.thread:
        overlay_ui.thread.join(timeout=1.5)   # nette Tk-afbouw bij afsluiten


if __name__ == "__main__":
    main()
