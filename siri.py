"""Mac voice assistant: hold right Option or say "Hey Jev", then speak. Jev decides, Fish speaks."""
import os, re, sys, time, queue, random, argparse, subprocess, tempfile, threading, hashlib, collections
import numpy as np, requests, sounddevice as sd, soundfile as sf
from dotenv import load_dotenv
from pynput import keyboard
from secrets_store import get_secret

load_dotenv()
TS_KEY = get_secret("TYPESAFE_API_KEY")
FISH_KEY = get_secret("FISH_AUDIO_API_KEY")
OR_KEY = get_secret("OPENROUTER_API_KEY")
VOICE_ID = "9a9cf47702da476aa4629e2506d4a857"
PTT_KEY = keyboard.Key.alt_r
SAMPLE_RATE = 16000
GATE = 0.65
WHISPER_MODEL = "small.en"
COMMAND_PROMPT = "Open Spotify. Play. Pause. Next track. Turn Spotify down. Turn the Mac volume down. Mute. Dark mode on. Lock the screen."
WAKE_PROMPT = "Hey Jev, open Spotify. Hey Jev, pause the music. Hey Jev, turn the volume down."
# Whisper often hears "Jev" as Jeff or Jeb, so accept the close ones
WAKE = re.compile(r"^\W*(?:hey|hi|hay|okay|ok|a)\W+(?:jev|jevs|jeff|jeffs|jef|jeb|jab|chev|jeve|jav)\b\W*", re.I)
WAKE_WINDOW = 6.0


def reload_keys():
    global TS_KEY, FISH_KEY, OR_KEY
    TS_KEY = get_secret("TYPESAFE_API_KEY")
    FISH_KEY = get_secret("FISH_AUDIO_API_KEY")
    OR_KEY = get_secret("OPENROUTER_API_KEY")

# --------------------------------------------------------------------------- Jev
QUESTIONS = {
    "category": {"type": "choice", "instructions": "What kind of request is this?",
                 "criteria": {"mac_command": "asks the computer to do something",
                              "information_request": "asks a general knowledge or factual question",
                              "chit_chat": "just talking, greeting, or thanking",
                              "unclear": "garbled, empty, or makes no sense"}},
    "compound": {"type": "noul", "instructions": "Does the request contain more than one distinct action?"},
    "target": {"type": "choice", "instructions": "What is the primary thing being controlled?",
               "criteria": {"app": "an application", "volume": "sound level", "display": "screen appearance or dark mode",
                            "media": "music playback", "system": "locking or sleeping the computer"}},
    "app": {"type": "choice", "instructions": "Which app, if any, is named?",
            "criteria": {"spotify": None, "slack": None, "chrome": None, "vscode": None, "finder": None,
                         "safari": None, "messages": None, "notes": None, "none": None}},
    "app_action": {"type": "choice", "instructions": "What should happen to the app?",
                   "criteria": {"open": "open, launch, or start the app itself", "quit": "quit, close, or kill the app",
                                "none": "the request is about playback, volume, or something inside the app, not opening or quitting it"}},
    "volume_action": {"type": "choice", "instructions": "What should happen to the volume, if anything?",
                      "criteria": {"up": None, "down": None, "mute": None, "unmute": None,
                                   "set": "set to a specific level", "none": None}},
    "volume_scope": {"type": "choice", "instructions": "Which volume should change?",
                     "criteria": {"spotify": "Spotify's own in-app volume when Spotify is explicitly named",
                                  "system": "the Mac's overall output volume, including unqualified volume requests"}},
    "volume_level": {"type": "score", "instructions": "If a volume level is asked for, how loud?",
                     "criteria": ["silent", "quiet", "medium", "loud", "max"]},
    "display_action": {"type": "choice", "instructions": "What should happen to dark mode?",
                       "criteria": {"dark_on": None, "dark_off": None, "toggle": None, "none": None}},
    "media_action": {"type": "choice", "instructions": "What should happen to music playback?",
                     "criteria": {"play": None, "pause": None, "next": None, "previous": None, "none": None}},
    "system_action": {"type": "choice", "instructions": "What should happen to the computer?",
                      "criteria": {"lock": None, "sleep": None, "none": None}},
}


def split_questions():
    """The same branch questions twice, one set scoped to the first action asked for, one to the second."""
    out = {}
    for slot, word in (("first", "FIRST"), ("second", "SECOND")):
        for k, q in QUESTIONS.items():
            if k in ("category", "compound"):
                continue
            out[f"{slot}_{k}"] = {**q, "instructions": f"Considering ONLY the {word} action the user asks for: {q['instructions']}"}
    return out


SPLIT_QUESTIONS = split_questions()


def jev(text, questions=None):
    t = time.time()
    r = requests.post("https://api.typesafe.ai/v1/systemone", json={"model": "jev-latest", "state": text, "questions": questions or QUESTIONS},
                      headers={"Authorization": f"Bearer {TS_KEY}"}, timeout=30)
    r.raise_for_status()
    j = r.json()
    ans = {}
    for k, a in j["answers"].items():
        if a["type"] == "noul":  # probability, confidence is distance from 0.5
            ans[k] = (a["noul"] >= 0.5, max(a["noul"], 1 - a["noul"]))
        elif a["type"] == "score":  # index into the rubric, legend maps it back to the label
            ans[k] = (a["legend"][str(int(round(a["score"])))], a.get("confidence", 0))
        else:
            ans[k] = (a["choice"], a.get("confidence", 0))
    cost = j.get("usage", {}).get("input_tokens", 0) * 0.042 / 1e6 
    return ans, int((time.time() - t) * 1000), cost


# --------------------------------------------------------------------------- Mac actions
APPS = {"spotify": "Spotify", "slack": "Slack", "chrome": "Google Chrome", "vscode": "Visual Studio Code",
        "finder": "Finder", "safari": "Safari", "messages": "Messages", "notes": "Notes"}
LEVELS = {"silent": 0, "quiet": 25, "medium": 50, "loud": 75, "max": 100}


def osa(script):
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "AppleScript failed")
    return result.stdout.strip()


def sh(*cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"command failed: {' '.join(cmd)}")


def volume():
    return int(osa("output volume of (get volume settings)"))


def spotify_volume():
    return int(osa('tell application "Spotify" to get sound volume'))


def open_app(name, wait=5.0):
    """Launch and wait until the app reports running, so a follow up action doesn't land too early."""
    sh("open", "-a", name)
    t = time.time()
    while time.time() - t < wait and osa(f'application "{name}" is running') != "true":
        time.sleep(0.2)


def spotify_play(tries=12):
    """Spotify ignores play while it's still loading, so keep asking until it says it's playing."""
    for _ in range(tries):
        osa('tell application "Spotify" to play')
        time.sleep(0.5)
        if osa('tell application "Spotify" to player state') == "playing":
            return
    raise RuntimeError("Spotify never started playing")


ACTIONS = {
    "app_open": lambda a: open_app(APPS[a]),
    "app_quit": lambda a: osa(f'tell application "{APPS[a]}" to quit'),
    "volume_up": lambda _: osa(f"set volume output volume {min(100, volume() + 20)}"),
    "volume_down": lambda _: osa(f"set volume output volume {max(0, volume() - 20)}"),
    "volume_mute": lambda _: osa("set volume output muted true"),
    "volume_unmute": lambda _: osa("set volume output muted false"),
    "volume_set": lambda lvl: osa(f"set volume output volume {LEVELS.get(lvl, 50)}"),
    "spotify_volume_up": lambda _: osa(f'tell application "Spotify" to set sound volume to {min(100, spotify_volume() + 20)}'),
    "spotify_volume_down": lambda _: osa(f'tell application "Spotify" to set sound volume to {max(0, spotify_volume() - 20)}'),
    "spotify_volume_mute": lambda _: osa('tell application "Spotify" to set sound volume to 0'),
    "spotify_volume_unmute": lambda _: osa('tell application "Spotify" to set sound volume to 50'),
    "spotify_volume_set": lambda lvl: osa(f'tell application "Spotify" to set sound volume to {LEVELS.get(lvl, 50)}'),
    "display_dark_on": lambda _: osa('tell application "System Events" to tell appearance preferences to set dark mode to true'),
    "display_dark_off": lambda _: osa('tell application "System Events" to tell appearance preferences to set dark mode to false'),
    "display_toggle": lambda _: osa('tell application "System Events" to tell appearance preferences to set dark mode to not dark mode'),
    "media_play": lambda _: spotify_play(),
    "media_pause": lambda _: osa('tell application "Spotify" to pause'),
    "media_next": lambda _: osa('tell application "Spotify" to next track'),
    "media_previous": lambda _: osa('tell application "Spotify" to previous track'),
    "system_lock": lambda _: osa('tell application "System Events" to keystroke "q" using {control down, command down}'),
    "system_sleep": lambda _: sh("pmset", "sleepnow"),
}

# --------------------------------------------------------------------------- Scripted replies with Fish tags
REPLIES = {
    "app_open": ["[cheerful] {app}'s up.", "{app}, opening now.", "[chuckling] There you go, {app}."],
    "app_quit": ["{app}'s gone.", "[sighing] Closing {app}. Good riddance.", "Done, {app} is closed."],
    "volume_up": ["Louder it is.", "[cheerful] Turning it up.", "Up we go."],
    "volume_down": ["Bringing it down.", "[sighing] A little quieter.", "Turning it down."],
    "volume_mute": ["[sighing] Muting. Finally some quiet.", "Muted.", "Shh. Muted."],
    "volume_unmute": ["Sound's back.", "[cheerful] Unmuted.", "And we're back."],
    "volume_set": ["Set to {level}.", "Volume's {level} now."],
    "spotify_volume_up": ["Turning Spotify up.", "[cheerful] Spotify's louder."],
    "spotify_volume_down": ["Turning Spotify down.", "Spotify's a little quieter."],
    "spotify_volume_mute": ["Spotify's muted.", "[sighing] Muting Spotify."],
    "spotify_volume_unmute": ["Spotify's sound is back.", "[cheerful] Spotify's unmuted."],
    "spotify_volume_set": ["Spotify's set to {level}.", "Set Spotify to {level}."],
    "display_dark_on": ["[chuckling] Lights off.", "Dark mode on.", "Going dark."],
    "display_dark_off": ["[cheerful] Let there be light.", "Dark mode off.", "Back to light."],
    "display_toggle": ["Flipped it.", "There, switched."],
    "media_play": ["[cheerful] Playing.", "Music's on.", "Here we go."],
    "media_pause": ["Paused.", "[sighing] Pausing. Take your time.", "Holding it there."],
    "media_next": ["Skipping.", "[chuckling] Not a fan? Next one.", "Next track."],
    "media_previous": ["Going back one.", "Previous track.", "[chuckling] Again? Sure."],
    "system_lock": ["Locking up. See you soon.", "Locked.", "Screen's locked."],
    "system_sleep": ["Good night.", "Sleeping now.", "[sighing] Finally, a nap."],
    "info": ["[chuckling] That's a question, not a command. I'll get a brain for that soon.",
             "[sighing] I can't answer that one yet."],
    "chit_chat": ["[chuckling] Hi. Give me something to do.", "[cheerful] Hey. I'm listening."],
    "compound_done": ["[chuckling] Done, both of them.", "[cheerful] All done.", "Both sorted."],
    "wake": ["Yes?", "[cheerful] Mm-hm?", "I'm listening."],
    "clarify": ["[clear throat] Sorry, say that again?", "Hm, one more time?"],
    "give_up": ["[sighing] I'm not sure what you mean. Try saying it differently?"],
    "unsupported": ["[chuckling] I know what you want, I just can't do that one yet."],
}


SPEAK_FIRST = {"volume_mute", "system_lock", "system_sleep"}


def say_line(key, **fmt):
    return random.choice(REPLIES[key]).format(**fmt)


# --------------------------------------------------------------------------- LLM fallback (questions only)
LLM_MODEL = "anthropic/claude-haiku-4.5"


def ask_llm(text):
    t = time.time()
    r = requests.post("https://openrouter.ai/api/v1/chat/completions",
                      headers={"Authorization": f"Bearer {OR_KEY}"},
                      json={"model": LLM_MODEL, "max_tokens": 80, "usage": {"include": True},
                            "messages": [{"role": "system", "content": "You are a voice assistant. Answer in one short spoken sentence, no markdown. "
                                          "You may start with exactly one tag from: [chuckling] [laughing] [sighing] [cheerful], or none."},
                                         {"role": "user", "content": text}]}, timeout=30)
    r.raise_for_status()
    j = r.json()
    return j["choices"][0]["message"]["content"].strip(), int((time.time() - t) * 1000), j.get("usage", {}).get("cost")


# --------------------------------------------------------------------------- Decision
def sub_action(ans, target):
    """(conf, action_key, arg, reply_key, fmt) for a target, or None if Jev didn't pick anything confident."""
    if target == "app":
        (app, ac), (action, aac) = ans["app"], ans["app_action"]
        if app == "none" or action == "none" or min(ac, aac) < GATE:
            return None
        return (min(ac, aac), f"app_{action}", app, f"app_{action}", {"app": APPS[app]})
    key = {"volume": "volume_action", "display": "display_action", "media": "media_action", "system": "system_action"}[target]
    action, conf = ans[key]
    if action == "none" or conf < GATE:
        return None
    lvl = ans["volume_level"][0] if target == "volume" else None
    prefix = target
    if target == "volume":
        scope, scope_conf = ans["volume_scope"]
        named_spotify = ans["app"][0] == "spotify"
        if scope == "spotify" and (scope_conf >= 0.5 or named_spotify):
            prefix = "spotify_volume"
    return (conf, f"{prefix}_{action}", lvl, f"{prefix}_{action}", {"level": lvl})


def decide(ans):
    """Read the Jev fan-out. Returns ("actions", [...]), ("reply", key), ("llm", None) or ("clarify", None)."""
    cat, cconf = ans["category"]
    if cat == "chit_chat" and cconf >= GATE:
        return ("reply", "chit_chat")
    if cat == "information_request" and cconf >= GATE:
        return ("llm", None)
    if cat == "unclear" and cconf >= GATE:
        return ("clarify", None)
    if ans["compound"][0] and ans["compound"][1] >= GATE:
        return ("split", None)
    a = pick_action(ans)
    if a:
        return ("actions", [a])
    return ("llm", None) if cat == "information_request" else ("clarify", None)


def pick_action(ans):
    """Trust Jev's target if it's reasonably sure, else take the single most confident action anywhere."""
    target, tconf = ans["target"]
    a = sub_action(ans, target) if tconf >= 0.5 else None
    if a is None:
        cands = [x for x in (sub_action(ans, t) for t in ("app", "volume", "display", "media", "system")) if x]
        a = max(cands, key=lambda x: x[0]) if cands else None
    return a


def split_actions(text, ans):
    """Second Jev call with first/second slots, so two actions in one sentence each get their own answers."""
    sans, ms, cost = jev(text, SPLIT_QUESTIONS)
    print(f"  -- split call: jev {ms}ms  ${cost:.6f}")
    acts = []
    for slot in ("first", "second"):
        half = {k[len(slot) + 1:]: v for k, v in sans.items() if k.startswith(slot + "_")}
        a = pick_action(half)
        print(f"  {slot:15} {a[1] if a else 'nothing confident'}" + (f" {a[2]}" if a and a[2] else ""))
        if a and (a[1], a[2]) not in [(x[1], x[2]) for x in acts]:
            acts.append(a)
    if len(acts) < 2:  # split didn't separate them, fall back to whatever the first fan-out was sure about
        acts = [a for a in (sub_action(ans, t) for t in ("app", "volume", "display", "media", "system")) if a]
    return acts


# --------------------------------------------------------------------------- Fish TTS
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "tts")


def fetch_tts(text):
    """Return a wav path for this line, generating it once and caching on disk. Returns (path, ms, cached)."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, hashlib.sha1(f"{VOICE_ID}|{text}".encode()).hexdigest() + ".wav")
    if os.path.exists(path):
        return path, 0, True
    t = time.time()
    r = requests.post("https://api.fish.audio/v1/tts", headers={"Authorization": f"Bearer {FISH_KEY}", "model": "s2.1-pro-free"},
                      json={"text": text, "reference_id": VOICE_ID, "format": "wav"}, timeout=60)
    r.raise_for_status()
    open(path, "wb").write(r.content)
    return path, int((time.time() - t) * 1000), False


def speak(text):
    path, ms, cached = fetch_tts(text)
    subprocess.run(["afplay", path])
    return ms


def all_scripted_lines():
    """Every fixed reply with placeholders expanded, so the whole set can be pre-rendered."""
    for key, lines in REPLIES.items():
        for line in lines:
            if "{app}" in line:
                yield from (line.format(app=a) for a in APPS.values())
            elif "{level}" in line:
                yield from (line.format(level=l) for l in LEVELS)
            else:
                yield line


def warm_cache():
    """Pre-render all scripted lines in the background so replies play instantly ($0 on the free string)."""
    made = 0
    for line in all_scripted_lines():
        try:
            _, _, cached = fetch_tts(line)
            made += 0 if cached else 1
        except Exception as e:
            print(f"  cache miss for {line!r}: {e}")
    if made:
        print(f"  cached {made} new reply lines")


# --------------------------------------------------------------------------- One turn
misses = 0


def emit(notify, state, detail=""):
    if notify:
        notify(state, detail)


def handle(text, stt_ms=None, notify=None):
    global misses
    print(f"\n> heard: {text!r}" + (f"  (stt {stt_ms}ms)" if stt_ms is not None else ""))
    if not text.strip():
        emit(notify, "Ready", "Didn't catch anything")
        return
    emit(notify, "Thinking", text)
    ans, jev_ms, cost = jev(text)
    for k, (v, c) in ans.items():
        flag = "" if c >= GATE else "  <- below gate"
        print(f"  {k:15} {str(v):22} {c:.2f}{flag}")
    print(f"  jev {jev_ms}ms  ${cost:.6f}")
    kind, payload = decide(ans)
    if kind == "split":
        payload = split_actions(text, ans)
        kind = "actions" if payload else "clarify"
    if kind == "clarify":
        misses += 1
        line = say_line("give_up") if misses >= 2 else say_line("clarify")
        if misses >= 2:
            misses = 0
    else:
        misses = 0
        if kind == "reply":
            line = say_line(payload)
        elif kind == "llm":
            line, llm_ms, llm_cost = ask_llm(text)
            print(f"  llm {LLM_MODEL} {llm_ms}ms  ${llm_cost}")
        else:
            line = say_line(payload[0][3], **payload[0][4]) if len(payload) == 1 else say_line("compound_done")
            # anything that kills the sound or the screen gets the reply first, or she'd mute herself
            speak_first = any(a[1] in SPEAK_FIRST or (a[1].endswith("volume_set") and a[2] == "silent") for a in payload)
            if speak_first:
                say(line, notify)
            done = 0
            for _, action, arg, _, _ in payload:
                try:
                    emit(notify, "Doing it", text)
                    ACTIONS[action](arg)
                    print(f"  action: {action} {arg or ''}")
                    done += 1
                except Exception as e:
                    print(f"  action failed: {action} {e}")
            if speak_first:
                emit(notify, "Ready", line)
                return
            if not done:
                line = say_line("unsupported")
    say(line, notify)
    emit(notify, "Ready", line)


def say(line, notify):
    print(f"  say: {line}")
    emit(notify, "Speaking", line)
    tts_ms = speak(line)
    print(f"  fish {'cached' if tts_ms == 0 else str(tts_ms) + 'ms'}")


# --------------------------------------------------------------------------- Mic + push to talk
class Recorder:
    BLOCK = 1600  # 100ms at 16kHz

    def __init__(self):
        self.frames, self.on = [], False
        self.wake, self.paused = False, False
        self.segments = queue.Queue()
        self.noise = 0.005
        self._reset_segment()
        self.stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                     blocksize=self.BLOCK, callback=self._cb)
        self.stream.start()

    def _reset_segment(self):
        self.speech, self.silent = [], 0
        self.preroll = collections.deque(maxlen=3)

    def _cb(self, indata, *_):
        if self.on:
            self.frames.append(indata.copy())
        if not self.wake or self.paused:
            if self.speech:
                self._reset_segment()
            return
        block = indata[:, 0].copy()
        rms = float(np.sqrt(np.mean(block ** 2)))
        loud = rms > max(self.noise * 3, 0.01)
        if not self.speech:
            if loud:
                self.speech, self.silent = list(self.preroll) + [block], 0
            else:
                self.noise = 0.95 * self.noise + 0.05 * rms  # track the room's background level
                self.preroll.append(block)
            return
        self.speech.append(block)
        self.silent = 0 if loud else self.silent + 1
        if self.silent >= 8 or len(self.speech) >= 150:  # 0.8s pause ends a phrase, 15s max
            if len(self.speech) - self.silent >= 4:
                self.segments.put(np.concatenate(self.speech))
            self._reset_segment()

    def start(self):
        self.frames, self.on = [], True

    def stop(self):
        self.on = False
        return np.concatenate(self.frames)[:, 0] if self.frames else np.zeros(0, dtype="float32")


def ready_text(wake):
    return "Say \u201cHey Jev\u201d and your command" if wake else "Ready when you are"


def run_voice_assistant(notify=None, controls=None, mode="ptt"):
    from faster_whisper import WhisperModel
    print("loading whisper...")
    emit(notify, "Starting", "Loading Whisper\u2026")
    model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    rec = Recorder()
    busy = threading.Lock()
    armed_until = [0.0]

    def transcribe(audio, prompt):
        t = time.time()
        segs, _ = model.transcribe(audio, language="en", beam_size=1, vad_filter=True, initial_prompt=prompt)
        return " ".join(s.text.strip() for s in segs).strip(), int((time.time() - t) * 1000)

    def run_turn(text, stt_ms):
        with busy:
            rec.paused = True  # don't hear her own reply
            try:
                handle(text, stt_ms, notify)
            except Exception as exc:
                print(f"\n  turn failed: {exc}")
                emit(notify, "Something went wrong", str(exc))
                time.sleep(2)
                emit(notify, "Ready", ready_text(rec.wake))
            finally:
                time.sleep(0.3)
                rec.paused = False

    def ptt_turn(audio):
        emit(notify, "Transcribing", "Working out what you said\u2026")
        try:
            text, ms = transcribe(audio, COMMAND_PROMPT)
        except Exception as exc:
            emit(notify, "Something went wrong", str(exc))
            return
        run_turn(text, ms)

    def wake_loop():
        while True:
            try:
                audio = rec.segments.get(timeout=1)
            except queue.Empty:
                if armed_until[0] and time.time() > armed_until[0]:
                    armed_until[0] = 0
                    emit(notify, "Ready", ready_text(rec.wake))
                continue
            if not rec.wake or busy.locked():
                continue
            try:
                text, ms = transcribe(audio, WAKE_PROMPT)
            except Exception as exc:
                print(f"\n  transcribe failed: {exc}")
                continue
            m = WAKE.match(text)
            if m:
                rest = text[m.end():].strip(" .,!?")
                if rest:
                    armed_until[0] = 0
                    run_turn(rest, ms)
                else:
                    with busy:
                        rec.paused = True
                        say(say_line("wake"), notify)
                        time.sleep(0.2)
                        rec.paused = False
                    armed_until[0] = time.time() + WAKE_WINDOW
                    emit(notify, "Listening", "Go ahead\u2026")
            elif armed_until[0] and time.time() < armed_until[0]:
                armed_until[0] = 0
                run_turn(text, ms)
            elif text:
                print(f"\n  (not for me: {text!r})")

    def set_mode(new):
        rec.wake = new == "wake"
        armed_until[0] = 0
        print(f"\n[mode: {'always listening' if rec.wake else 'hold right Option'}]")
        if not busy.locked():
            emit(notify, "Ready", ready_text(rec.wake))

    def start_recording():
        if not rec.wake and not rec.on and not busy.locked():
            rec.start()
            print("\n[listening]", end="", flush=True)
            emit(notify, "Listening", "Release right Option when you\u2019re done")

    def stop_recording():
        if rec.on:
            audio = rec.stop()
            if len(audio) > SAMPLE_RATE * 0.3:
                threading.Thread(target=ptt_turn, args=(audio,), daemon=True).start()

    threading.Thread(target=warm_cache, daemon=True).start()
    threading.Thread(target=wake_loop, daemon=True).start()
    set_mode(mode)
    print("ready. ctrl+c to quit.")
    if controls is not None:
        while True:
            command = controls.get()
            if isinstance(command, tuple) and command[0] == "mode":
                set_mode(command[1])
            elif command == "press":
                start_recording()
            elif command == "release":
                stop_recording()

    if rec.wake:
        threading.Event().wait()

    def on_press(key):
        if key == PTT_KEY:
            start_recording()

    def on_release(key):
        if key == PTT_KEY:
            stop_recording()

    with keyboard.Listener(on_press=on_press, on_release=on_release) as l:
        l.join()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", help="skip the mic, run one turn on this transcript")
    ap.add_argument("--ui", action="store_true", help="show the native floating status window")
    ap.add_argument("--wake", action="store_true", help="always listening, say \"Hey Jev\" instead of holding Option")
    args = ap.parse_args()
    if args.ui:
        from assistant_ui import run_app
        run_app()
        return
    if not TS_KEY or not FISH_KEY:
        sys.exit("need TYPESAFE_API_KEY and FISH_AUDIO_API_KEY in Keychain or .env")
    if args.text:
        handle(args.text)
        return
    run_voice_assistant(mode="wake" if args.wake else "ptt")


if __name__ == "__main__":
    main()
