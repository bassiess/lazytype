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

APP_VERSION = "1.0.0"
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
HOTKEY_CHOICES = [("Rechter Ctrl", "ctrl_r"), ("Linker Ctrl", "ctrl_l"),
                  ("Rechter Alt", "alt_r"), ("Linker Alt", "alt_l"), ("Rechter Shift", "shift_r"),
                  ("Ctrl + Alt", "ctrl+alt"), ("Ctrl + Shift", "ctrl+shift"),
                  ("Ctrl + Windows", "ctrl+win"), ("Windows + Alt", "win+alt"),
                  ("Windows + Shift", "win+shift"), ("Alt + Shift", "alt+shift"),
                  ("F8", "f8"), ("F9", "f9"), ("F10", "f10"), ("Uit", "uit")]


def _hk_display(spec):
    return next((l for l, v in HOTKEY_CHOICES if v == spec), spec)
LANG_CHOICES = [("Engels", "en"), ("Nederlands", "nl"), ("Duits", "de"), ("Frans", "fr"),
                ("Spaans", "es"), ("Italiaans", "it"), ("Portugees", "pt")]
SPOKEN_CHOICES = [("Automatisch", "auto"), ("Nederlands", "nl"), ("Engels", "en"),
                  ("Duits", "de"), ("Frans", "fr"), ("Spaans", "es")]
ENGINE_CHOICES = [("Managed (Pro)", "managed"), ("Groq (eigen key)", "groq"),
                  ("OpenAI", "openai"), ("Lokaal", "local")]


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
        dictate.beep("stop")
        if secs < 0.3:
            return
        set_phase("working")
        t0 = time.time()
        mode = state.get("active_mode")
        if mode == "command":
            sel = dictate.copy_selection()
            if not sel:
                state["last"] = "Command: geen tekst geselecteerd"
                dictate.beep("error")
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
            dictate.beep("error")
            return
        print(f"  ✅ ({dt:.2f}s) → {text}")
        state["last"] = text
        dictate.paste_text(text)
        dictate.beep("done")
        if state.get("history"):
            dictate.add_history(text)
    except Exception as e:
        print(f"  ⚠️  {e}")
        state["last"] = f"Fout: {e}"
        dictate.beep("error")
    finally:
        state["busy"] = False
        set_phase("idle")


def _confirm_arming():
    """Loopt MIN_HOLD_SEC na het indrukken: promoot 'arming' → echte opname."""
    if state["phase"] == "arming" and not arm["aborted"]:
        dictate.beep("start")
        set_phase("recording")


def _begin(matchers, mode, needs_arming):
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
        dictate.beep("start")
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
                dictate.beep("start")
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
                state["engine"] = "managed"  # meteen op managed zetten bij een geldige tier
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
    Vervangt de dropdown voor sneltoetsen in Instellingen en onboarding."""
    initial = initial or HOTKEY_CHOICES[0][1]
    val = {"v": initial}
    tk.Label(parent, text=label, bg=UI_PAPER, fg=UI_INK, font=UI_FONT,
             anchor="w").pack(fill="x", padx=18, pady=(10, 2))
    row = tk.Frame(parent, bg=UI_PAPER)
    row.pack(fill="x", padx=18)
    disp_var = tk.StringVar(master=parent, value=_hk_display(initial))
    tk.Label(row, textvariable=disp_var, bg="#ffffff", fg=UI_INK, font=UI_FONT,
             anchor="w", padx=10, pady=5, relief="flat",
             highlightthickness=1, highlightbackground="#dedbd3").pack(side="left", fill="x", expand=True)
    btn = tk.Button(row, text="Wijzig…", bg=UI_PAPER, fg=UI_ACCENT, relief="flat",
                    font=UI_FONT, cursor="hand2", padx=8)
    btn.pack(side="left", padx=(6, 0))
    tk.Button(row, text="Uit", bg=UI_PAPER, fg=UI_SUB, relief="flat",
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


def open_settings(icon_=None, item=None):
    def worker():
        import tkinter as tk
        root = tk.Tk(); root.title("Lazytype — Instellingen")
        root.configure(bg=UI_PAPER); root.attributes("-topmost", True); root.resizable(False, False)
        tk.Label(root, text="Sneltoetsen", bg=UI_PAPER, fg=UI_INK,
                 font=("Segoe UI", 12, "bold"), anchor="w").pack(fill="x", padx=18, pady=(16, 0))
        g_dict = _keypicker(tk, root, "Dicteren — houd in en spreek", state["hotkey_name"])
        g_cmd  = _keypicker(tk, root, "Command — selecteer tekst + spreek instructie", state["hotkey_command"])
        g_tr   = _keypicker(tk, root, "Vertalen — dicteren + meteen vertalen", state["hotkey_translate"])
        g_tgt = _dropdown(tk, root, "Vertaal-doeltaal", LANG_CHOICES, state["translate_target"])
        tk.Frame(root, bg=UI_LINE, height=1).pack(fill="x", padx=18, pady=(12, 2))
        g_lang = _dropdown(tk, root, "Spreektaal", SPOKEN_CHOICES, state["language"])
        g_eng = _dropdown(tk, root, "Engine", ENGINE_CHOICES, state["engine"])
        keys = tk.Frame(root, bg=UI_PAPER); keys.pack(fill="x", padx=18, pady=(14, 0))
        _ghost_btn(tk, keys, "Groq-key…", lambda: set_key_action(None, None)).pack(side="left")
        _ghost_btn(tk, keys, "Abonnement-sleutel…", lambda: set_license_action(None, None)).pack(side="left", padx=10)

        def _close():
            global _keypicking
            _keypicking = False   # altijd resetten bij sluiten, ook als Wijzig… open was
            pressed.clear()
            root.destroy()

        def save():
            global _keypicking
            _keypicking = False
            pressed.clear()
            state["hotkey_name"] = g_dict(); state["hotkey_command"] = g_cmd()
            state["hotkey_translate"] = g_tr(); state["translate_target"] = g_tgt()
            state["language"] = g_lang(); state["engine"] = g_eng()
            for k, envk in (("hotkey_name", "DICTATE_HOTKEY"), ("hotkey_command", "DICTATE_COMMAND_HOTKEY"),
                            ("hotkey_translate", "DICTATE_TRANSLATE_HOTKEY"), ("translate_target", "DICTATE_TRANSLATE_TARGET"),
                            ("language", "DICTATE_LANGUAGE"), ("engine", "DICTATE_ENGINE")):
                dictate.save_env_value(envk, state[k])
            rebuild_hotkeys(); state["last"] = "Instellingen opgeslagen ✓"; refresh()
            root.destroy()

        root.protocol("WM_DELETE_WINDOW", _close)
        _accent_btn(tk, root, "Opslaan", save).pack(pady=16)
        root.mainloop()
    threading.Thread(target=worker, daemon=True).start()


def run_onboarding():
    """Wizard bij eerste start (draait op de main thread, vóór de tray-loop)."""
    import tkinter as tk
    root = tk.Tk(); root.title("Welkom bij Lazytype")
    root.configure(bg=UI_PAPER); root.attributes("-topmost", True); root.resizable(False, False)
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    W, H = 460, 440
    root.geometry(f"{W}x{H}+{(sw - W) // 2}+{(sh - H) // 2}")
    body = tk.Frame(root, bg=UI_PAPER); body.pack(fill="both", expand=True)
    sel = {"d": state["hotkey_name"], "t": state["hotkey_translate"], "g": state["translate_target"]}

    def clear():
        for w in body.winfo_children():
            w.destroy()

    def head(t, sub):
        tk.Label(body, text=t, bg=UI_PAPER, fg=UI_INK, font=("Segoe UI", 17, "bold"),
                 wraplength=W - 48, justify="left", anchor="w").pack(fill="x", padx=24, pady=(28, 6))
        tk.Label(body, text=sub, bg=UI_PAPER, fg=UI_SUB, font=("Segoe UI", 10),
                 wraplength=W - 48, justify="left", anchor="w").pack(fill="x", padx=24)

    def nav(back, primary_text, primary_cmd):
        bar = tk.Frame(body, bg=UI_PAPER); bar.pack(side="bottom", fill="x", padx=22, pady=20)
        _accent_btn(tk, bar, primary_text, primary_cmd).pack(side="right")
        if back:
            _ghost_btn(tk, bar, "← Terug", back).pack(side="left")

    def s0():
        clear(); head("Welkom bij Lazytype",
                      "Houd een toets ingedrukt, spreek, en je woorden verschijnen — in elke app. "
                      "Even 2 dingen instellen, dan kun je los.")
        nav(None, "Beginnen →", s1)

    def s1():
        clear(); head("Kies je dicteer-toets",
                      "Houd deze toets ingedrukt terwijl je spreekt; laat los om te stoppen. "
                      "Klik 'Wijzig…' en druk dan de gewenste toets(combo).")
        g = _keypicker(tk, body, "Dicteren", sel["d"])
        nav(s0, "Volgende →", lambda: (sel.update(d=g()), s2()))

    def s2():
        clear(); head("Vertaal-toets",
                      "Een aparte toets die je dictaat meteen vertaalt. Bijvoorbeeld: spreek Nederlands, "
                      "krijg Engels. Of spreek Engels, krijg Duits.")
        g1 = _keypicker(tk, body, "Vertaal-toets", sel["t"])
        g2 = _dropdown(tk, body, "Vertaal naar", LANG_CHOICES, sel["g"])
        nav(s1, "Volgende →", lambda: (sel.update(t=g1(), g=g2()), s3()))

    def s3():
        clear(); head("Aan de slag",
                      "Je gratis proef van 14 dagen loopt al. Vul nu of later je gratis Groq-key in, "
                      "of een Pro-sleutel als je een abonnement hebt.")
        box = tk.Frame(body, bg=UI_PAPER); box.pack(fill="x", padx=24, pady=10)
        _ghost_btn(tk, box, "Groq-key invoeren…", lambda: set_key_action(None, None)).pack(anchor="w", pady=3)
        _ghost_btn(tk, box, "Pro-sleutel invoeren…", lambda: set_license_action(None, None)).pack(anchor="w", pady=3)

        def finish():
            global _keypicking
            _keypicking = False
            pressed.clear()
            state["hotkey_name"] = sel["d"]; state["hotkey_translate"] = sel["t"]; state["translate_target"] = sel["g"]
            for k, envk in (("hotkey_name", "DICTATE_HOTKEY"), ("hotkey_translate", "DICTATE_TRANSLATE_HOTKEY"),
                            ("translate_target", "DICTATE_TRANSLATE_TARGET")):
                dictate.save_env_value(envk, state[k])
            dictate.save_env_value("DICTATE_ONBOARDED", "1")
            rebuild_hotkeys()
            root.destroy()

        nav(s2, "Klaar — start Lazytype", finish)

    def skip():
        global _keypicking
        _keypicking = False
        pressed.clear()
        dictate.save_env_value("DICTATE_ONBOARDED", "1")
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", skip)
    s0()
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


# ── Klein drijvend overlay-pilletje: live waveform + status + snel aanpassen ──
class Overlay:
    PILL = "#16171d"; SUB = "#a6a9b4"; CHIP = "#272833"; WAVE = "#8f7dff"

    def __init__(self):
        self.root = None; self.thread = None; self.cv = None
        self._t = 0; self._sm = 0.0; self._chips = []; self._drag = None; self._moved = False; self._imgs = {}

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
        from PIL import ImageTk
        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        transp = "#000001"
        try:
            root.configure(bg=transp); root.attributes("-transparentcolor", transp)
        except Exception:
            transp = self.PILL; root.configure(bg=transp)
        try: root.attributes("-alpha", 0.97)
        except Exception: pass
        self.W, self.H = 360, 54
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{self.W}x{self.H}+{(sw - self.W) // 2}+{sh - self.H - 74}")
        self.cv = tk.Canvas(root, width=self.W, height=self.H, highlightthickness=0, bg=transp)
        self.cv.pack()
        for k in ("idle", "recording", "working", "disabled"):
            self._imgs[k] = ImageTk.PhotoImage(make_icon(k, size=34, shape="squircle"))
        self.cv.bind("<ButtonPress-1>", self._press)
        self.cv.bind("<B1-Motion>", self._motion)
        self.cv.bind("<ButtonRelease-1>", self._release)
        self.root = root
        self._tick()
        root.mainloop()

    def _tick(self):
        if not self.root:
            return
        try: self._draw()
        except Exception: pass
        self._t += 1
        try: self.root.after(45, self._tick)
        except Exception: pass

    def _round(self, x0, y0, x1, y1, r, fill):
        self.cv.create_polygon(
            [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
             x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0],
            smooth=True, fill=fill, outline=fill)

    def _chip(self, cx, cy, text, cb):
        w = max(34, len(text) * 7 + 16)
        x0, x1, y0, y1 = cx - w // 2, cx + w // 2, cy - 10, cy + 10
        self._round(x0, y0, x1, y1, 10, self.CHIP)
        self.cv.create_text(cx, cy, text=text, fill=self.SUB, font=("Segoe UI", 8), anchor="c")
        self._chips.append((x0, x1, y0, y1, cb))

    def _waveform(self, x0, x1, cy):
        self._sm += (recorder.last_level - self._sm) * 0.4
        lv = self._sm
        n = 12; gap = (x1 - x0) / n
        for i in range(n):
            x = x0 + gap * (i + 0.5)
            f = 0.30 + 0.70 * abs(math.sin(self._t * 0.45 + i * 0.6))
            h = 3 + lv * 60 * f
            self.cv.create_line(x, cy - h / 2, x, cy + h / 2, fill=self.WAVE, width=3, capstyle="round")

    def _draw(self):
        cv = self.cv; cv.delete("all")
        W, H = self.W, self.H
        ph = "recording" if state["phase"] == "arming" else current_phase()
        if ph not in self._imgs:
            ph = "idle"
        self._round(5, 5, W - 5, H - 5, 18, self.PILL)
        cv.create_image(30, H // 2, image=self._imgs[ph])
        mx0, mx1 = 52, W - 132
        if ph == "recording":
            self._waveform(mx0, mx1, H // 2)
        else:
            label = ("Transcriberen" + "." * (1 + (self._t // 5) % 3)) if ph == "working" \
                else "Gepauzeerd" if ph == "disabled" else "Klaar — houd je toets in"
            cv.create_text((mx0 + mx1) // 2, H // 2, text=label, fill=self.SUB, font=("Segoe UI", 10), anchor="c")
        self._chips = []
        rx = W - 64
        self._chip(rx, H // 2 - 12, "taal: " + state["language"], self._cycle_lang)
        ai = {"off": "AI: uit", "clean": "AI: schoon"}.get(state["postprocess"], "AI → " + state["postprocess"])
        self._chip(rx, H // 2 + 12, ai, self._cycle_ai)

    def _cycle_lang(self):
        order = ["nl", "en", "de", "fr", "es", "auto"]
        i = order.index(state["language"]) if state["language"] in order else -1
        state["language"] = order[(i + 1) % len(order)]

    def _cycle_ai(self):
        order = ["off", "clean", "en", "nl", "de"]
        i = order.index(state["postprocess"]) if state["postprocess"] in order else -1
        state["postprocess"] = order[(i + 1) % len(order)]

    def _press(self, e):
        self._drag = (e.x_root, e.y_root, self.root.winfo_x(), self.root.winfo_y()); self._moved = False

    def _motion(self, e):
        if not self._drag:
            return
        dx, dy = e.x_root - self._drag[0], e.y_root - self._drag[1]
        if abs(dx) + abs(dy) > 4:
            self._moved = True
        self.root.geometry(f"+{self._drag[2] + dx}+{self._drag[3] + dy}")

    def _release(self, e):
        moved, self._drag = self._moved, None
        if moved:
            return
        for x0, x1, y0, y1, cb in self._chips:
            if x0 <= e.x <= x1 and y0 <= e.y <= y1:
                cb(); return
        if e.x <= 50:                       # klik op het logo = pauze aan/uit
            state["enabled"] = not state["enabled"]


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

    # Onboarding bij de allereerste start (op de main thread, vóór de tray-loop).
    if not os.environ.get("DICTATE_ONBOARDED"):
        try:
            run_onboarding()
        except Exception as e:
            print(f"  (onboarding overgeslagen: {e})")

    # Eerste keer zonder key? Vraag hem meteen (op de main thread, vóór de tray-loop).
    if state["engine"] == "groq" and not os.environ.get("GROQ_API_KEY"):
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
