"""Dictation from Tatoscription: "Hey Jev, transcribe" records until "stop transcribing", then pastes the text at your cursor."""
import io, os, re, json, time, base64, subprocess
from concurrent.futures import ThreadPoolExecutor
import numpy as np, requests, soundfile as sf

MODEL = "openai/gpt-4o-mini-transcribe"
PROMPT = ("The following is a transcript of a person talking, you can remove and duplicated words and any fillers words. "
          "If its a longer transcript put into paragraphs for better readability.")
CHUNK_SECS = 30  # audio goes off in chunks this long while you talk, so stopping is quick
MAX_SECS = 2 * 60 * 60  # stops by itself after this, in case the stop phrase gets missed
HERE = os.path.dirname(os.path.abspath(__file__))
HISTORY = os.path.expanduser("~/Library/Logs/Hey Jev dictation.jsonl")
FAILED_DIR = os.path.expanduser("~/Library/Logs/Hey Jev dictation failed")
# Whisper often writes "transcribe" as Trenchcribe, Trend Scribe, Changecribe or Trench Club, so accept those too
SCRIBE = r"(?:transcri\w*|(?:tran\w*|trench\w*|trend|change)\s?(?:s?crib\w*|club)|dictat\w*|record\w*)"
START = re.compile(rf"^\W*(?:(?:please|can you|could you)\s+)?(?:start\s+)?(?:{SCRIBE}|take notes)"
                   r"(?:\s+(?:this|this meeting|this call|notes|mode|now|please|for me))?\W*$", re.I)

USER_VOCAB = os.path.join(HERE, "vocabulary.json")  # your own words, gitignored
VOCAB = []


def read_vocab():
    path = USER_VOCAB if os.path.exists(USER_VOCAB) else os.path.join(HERE, "vocabulary.example.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_vocab():
    global VOCAB
    VOCAB = [(re.compile(r"\b" + re.escape(alt) + r"\b", re.I), word) for word, alts in read_vocab().items() for alt in alts]


def save_vocab(words):
    """words is {"Word": ["heard as", ...]}, saved to vocabulary.json and used straight away."""
    with open(USER_VOCAB, "w", encoding="utf-8") as f:
        json.dump(words, f, indent=2, ensure_ascii=False)
        f.write("\n")
    load_vocab()


load_vocab()


def fix_vocab(text):
    for rx, word in VOCAB:
        text = rx.sub(word, text)
    return text


def strip_prompt(text):
    # gpt-4o transcribers sometimes echo the prompt back, same filter as Tatoscription
    cleaned = text
    for part in (PROMPT, *PROMPT.split(", "), *PROMPT.split(". ")):
        cleaned = re.sub(re.escape(part.strip(" .")), "", cleaned, flags=re.I)
    cleaned = re.sub(r"[ \t]+", " ", cleaned).strip(" ,.")
    return cleaned if cleaned else text


def paste(text, enter=False):
    subprocess.run(["pbcopy"], input=text.encode(), check=True)  # stays on the clipboard too, in case the paste misses
    time.sleep(0.2)
    # System Events, not pynput: pynput reads the keyboard layout, which crashes the app off the main thread
    subprocess.run(["osascript", "-e", 'tell application "System Events" to keystroke "v" using command down'], check=True)
    if enter:
        time.sleep(0.3)  # let the paste land before sending it
        subprocess.run(["osascript", "-e", 'tell application "System Events" to key code 36'], check=True)


class Dictation:
    def __init__(self, names, get_key, sample_rate):
        self.stop_rx = re.compile(rf"(?:\b(?:hey|hi|hay|okay|ok)\W+)?(?:\b(?:{names})\W+)?\b(?:stop|end|finish)\W+(?:the\W+)?"
                                  rf"{SCRIBE}\W*$", re.I)
        self.get_key, self.rate = get_key, sample_rate
        self.pool = ThreadPoolExecutor(3)
        self.active = False

    def start(self):
        self.active, self.started = True, time.time()
        self.stamp = time.strftime("%Y-%m-%d %H-%M-%S")
        self.buffer, self.chunks = [], []

    def timed_out(self):
        return self.active and time.time() - self.started > MAX_SECS

    def add(self, audio, heard):
        """Keep this phrase, returns True if it was the stop command."""
        self.buffer.append(audio)
        stop = bool(self.stop_rx.search(heard)) or self.timed_out()
        if stop or sum(map(len, self.buffer)) >= CHUNK_SECS * self.rate:
            self._send()
        return stop

    def _send(self):
        if self.buffer:
            self.chunks.append(self.pool.submit(self._transcribe, np.concatenate(self.buffer), len(self.chunks) + 1))
            self.buffer = []

    def finish(self):
        """Wait for every chunk, returns (text, failed chunks, total chunks)."""
        self._send()
        self.active = False
        parts, failed = [], 0
        for i, chunk in enumerate(self.chunks):
            try:
                parts.append(chunk.result())
            except Exception as exc:
                failed += 1
                parts.append("[missing part]")  # marks the gap, so it's obvious something is missing
                print(f"  dictation chunk {i + 1} of {len(self.chunks)} failed: {exc}")
        text = " ".join(p for p in parts if p)
        text = self.stop_rx.sub("", text).strip(" ,")
        text = fix_vocab(strip_prompt(text)) if text else ""
        if text:
            with open(HISTORY, "a", encoding="utf-8") as f:
                entry = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "text": text, **({"failed_parts": failed} if failed else {})}
                f.write(json.dumps(entry) + "\n")
        return text, failed, len(self.chunks)

    def _transcribe(self, audio, part):
        wav = io.BytesIO()
        sf.write(wav, audio, self.rate, format="WAV")
        for attempt in range(3):
            try:
                t = time.time()
                openai_key, openrouter_key = self.get_key()
                if openai_key:  # straight to OpenAI, so the audio only goes to one company
                    r = requests.post("https://api.openai.com/v1/audio/transcriptions",
                                      headers={"Authorization": f"Bearer {openai_key}"},
                                      data={"model": MODEL.split("/")[1], "language": "en", "prompt": PROMPT},
                                      files={"file": ("dictation.wav", wav.getvalue(), "audio/wav")}, timeout=90)
                else:
                    r = requests.post("https://openrouter.ai/api/v1/audio/transcriptions",
                                      headers={"Authorization": f"Bearer {openrouter_key}"},
                                      json={"model": MODEL, "language": "en", "provider": {"options": {"openai": {"prompt": PROMPT}}},  # OpenRouter only passes the prompt on this way
                                            "input_audio": {"data": base64.b64encode(wav.getvalue()).decode(), "format": "wav"}},
                                      timeout=90)
                if not r.ok:
                    print(f"  {'openai' if openai_key else 'openrouter'} said: {r.status_code} {r.text[:200]}")
                r.raise_for_status()
                text = r.json().get("text", "").strip()
                print(f"  dictation chunk {len(audio) / self.rate:.0f}s -> {len(text.split())} words  {int((time.time() - t) * 1000)}ms")
                return text
            except Exception as exc:
                if attempt == 2 or (isinstance(exc, requests.HTTPError) and exc.response.status_code < 500
                                    and exc.response.status_code != 429):
                    os.makedirs(FAILED_DIR, exist_ok=True)  # keep the audio so nothing is lost
                    sf.write(os.path.join(FAILED_DIR, f"{self.stamp} part {part}.wav"), audio, self.rate)  # one file per part, none overwritten
                    raise
                time.sleep(2 ** attempt)
