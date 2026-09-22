# Hey Jev

A push to talk voice assistant for your Mac. Hold right Option, say a thing, it does it and answers back.

- **Jev** (TypeSafe) makes every decision in one call, $0.00004 per request
- **Fish Audio S2.1 Pro** speaks every reply, with emotion tags like `[chuckling]` and `[sighing]`
- **Whisper** (local, faster-whisper) turns your voice into text
- An LLM only wakes up when Jev says you asked a question, not a command

Works on macOS Sequoia and Tahoe.

## What it can do

Open or quit apps, Mac volume up / down / mute / set, Spotify volume, play / pause / next / previous, dark mode, lock or sleep the Mac. Two things in one sentence work too: "pause Spotify and open Slack".

Anything that isn't a command ("who wrote Hamlet") goes to Claude Haiku via OpenRouter and gets spoken back.

## Setup

You need three API keys:

- TypeSafe (Jev): [https://typesafe.ai](https://typesafe.ai)
- Fish Audio: [https://fish.audio/?fpr=henryk](https://fish.audio/?fpr=henryk) (the `s2.1-pro-free` model is free on the API until the end of November 2026)
- OpenRouter (optional): [https://openrouter.ai](https://openrouter.ai) (only used for questions)

Then:

```bash
git clone <this repo> jev-siri
cd jev-siri
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python setup.py py2app -A
open "dist/Hey Jev - Fish Audio.app"
```

The py2app line builds the app bundle in alias mode, so it runs the code straight from this folder. Build it once, and again only if you move the folder.

First launch:

1. The Keys panel opens. Paste your three keys, they're saved in your Mac Keychain.
2. Whisper downloads its `small.en` model (about 250MB), one time.
3. macOS will ask for **Microphone** access. Say yes.
4. Add "Hey Jev - Fish Audio" (or your terminal, if you run from the terminal) under **System Settings > Privacy & Security > Accessibility**, or key presses are ignored.
5. The first time it quits an app or toggles dark mode you'll get an **Automation** prompt. Say yes.

The window goes green when it's ready. Hold right Option, talk, let go.

### Or let Claude Code set it up

Paste this into Claude Code from inside the cloned folder:

> Set this project up on my Mac: create a venv from requirements.txt, build the app with `python setup.py py2app -A`, then tell me which API keys I need, where to get them, and which macOS permissions to grant. Then open the app from the dist folder.

Use Claude Code (the terminal, or the Code tab in the desktop app). The chat side of Claude Desktop runs commands in a Linux sandbox, not on your Mac, so the Mac only packages fail there.

## Running from the terminal

Useful for seeing the Jev trace (every question, answer and confidence per turn):

```bash
.venv/bin/python siri.py               # mic mode, trace prints to the terminal
.venv/bin/python siri.py --text "open spotify and turn it down"   # one turn, no mic
.venv/bin/python siri.py --ui          # same as the app, but shows as "Python" in the Dock
```

Keys can also go in a `.env` file (`TYPESAFE_API_KEY`, `FISH_AUDIO_API_KEY`, `OPENROUTER_API_KEY`) if you'd rather not use the Keychain.

## How it works

1. Hold right Option, audio is recorded until you let go.
2. faster-whisper transcribes it locally for free, about 0.8s.
3. One Jev call asks every question at once (category, is it compound, target, which app, which action, volume level, and so on). The code ignores the answers that don't apply. This is the speculative fan-out pattern from the TypeSafe docs.
4. If Jev says the request is two things, a second Jev call asks the same questions twice, scoped to "the first action" and "the second action". No LLM needed to split.
5. The action runs as a one line `osascript` or shell command.
6. A scripted reply with emotion tags is picked at random and played. All scripted lines are pre-rendered into `cache/tts/` on first launch, so replies are instant. Only LLM answers are generated live.

Below 0.65 confidence it asks you to say it again, twice in a row and it gives up.

## What it costs

- **Fish Audio:** $0. The `s2.1-pro-free` model string on the API is free until the end of November 2026. You don't need to top up API credits. (Their MCP and web playground bill your plan credits instead, this app doesn't use those.) After November the paid `s2.1-pro` is $15 per million characters, and the cached replies mean a normal day of use is a few cents.
- **Jev:** $0.042 per million input tokens, output free. One command is about $0.00004, a two part command about $0.00011.
- **Whisper:** free, runs on your Mac.
- **OpenRouter (questions only):** Claude Haiku, about $0.0002 per answer.



## Files

- `siri.py` all the logic: questions, actions, replies, Whisper, Fish, LLM fallback
- `assistant_ui.py` the floating status window and the Keys panel
- `secrets_store.py` Keychain read / write
- `app.py` and `setup.py` the app bundle entry point and the py2app config, output lands in `dist/`



## Change the voice

`VOICE_ID` at the top of `siri.py`. Find voices at [https://fish.audio](https://fish.audio), any voice id works. Delete `cache/tts/` after changing it so the replies get re-rendered.