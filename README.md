# Hey Jev

A voice assistant for your Mac. Say "Hey Jev" or hold right Option, say a thing, it does it and answers back.

- **Jev** (TypeSafe) makes every decision in one call, $0.00004 per request
- **Fish Audio S2.1 Pro** speaks every reply, with emotion tags like `[chuckling]` and `[sighing]`
- **Whisper** (local, faster-whisper) turns your voice into text
- An LLM only wakes up when Jev says you asked a question, not a command

**Mac only.** Works on macOS Sequoia and Tahoe. It controls the Mac through AppleScript and the Keychain, so it won't run on Windows or Linux.

## What it can do

Open or quit apps, Mac volume up / down / mute / set, Spotify volume, play / pause / next / previous, dark mode, lock or sleep the Mac. Two things in one sentence work too: "pause Spotify and open Slack".

Timers and reminders: "set a timer for 5 minutes", "remind me in 20 minutes to call Mum", "how long is left?", "cancel the timer". Each one counts down live in the window, and she tells you when it's done.

Anything that isn't a command ("who wrote Hamlet") goes to Claude Haiku via OpenRouter and gets spoken back.

## What you need

- A Mac
- Python 3 (tested on 3.14, see below if you don't have it)
- The Spotify desktop app, for the music commands
- Three API keys:
  - **TypeSafe (Jev):** [https://typesafe.ai](https://typesafe.ai)
  - **Fish Audio:** [https://fish.audio/?fpr=henryk](https://fish.audio/?fpr=henryk). Sign in, then create a key on the API keys page in your account. You don't need a paid plan or API credit: the `s2.1-pro-free` model this app uses is free on the API until the end of November 2026.
  - **OpenRouter (optional):** [https://openrouter.ai](https://openrouter.ai), only used to answer questions

### Don't have Python?

Check in Terminal:

```bash
python3 --version
```

If that prints a version, you're set. If not, pick one:

- **Easiest:** download the macOS installer from [python.org/downloads](https://www.python.org/downloads/) and run it.
- **With Homebrew:** `brew install python`

## Setup

```bash
git clone https://github.com/henryklunaris/hey-jev.git
cd hey-jev
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python setup.py py2app -A
open "dist/Hey Jev - Fish Audio.app"
```

The py2app line builds the app bundle in alias mode, so it runs the code straight from this folder. Build it once, and again only if you move the folder.

First launch:

1. The Keys panel opens. Paste your three keys, they're saved in your Mac Keychain. Change them any time with the **Keys…** button.
2. Whisper downloads its `small.en` model (about 250MB), one time.
3. macOS will ask for **Microphone** access. Say yes.
4. Add "Hey Jev - Fish Audio" (or your terminal, if you run from the terminal) under **System Settings > Privacy & Security > Accessibility**, or key presses are ignored.
5. The first time it quits an app or toggles dark mode you'll get an **Automation** prompt. Say yes.

The window goes green when it's ready. The switch in the bottom right picks how you talk to it:

- **Hold Option:** hold right Option, talk, let go.
- **Hey Jev:** always listening. Say "Hey Jev, open Spotify" in one go, or say "Hey Jev", wait for her reply, then give the command.

### Or let Claude Code set it up

Paste this into Claude Code with the repo link:

> Clone https://github.com/henryklunaris/hey-jev and set it up on my Mac. Check Python 3 is installed and help me install it if not. Create a venv from requirements.txt, build the app with `python setup.py py2app -A`, then tell me which API keys I need, where to get them, and which macOS permissions to grant. Then open the app from the dist folder.

Use Claude Code (the terminal, or the Code tab in the desktop app). The chat side of Claude Desktop runs commands in a Linux sandbox, not on your Mac, so the Mac only packages fail there.

## Using the window

- **Minimise** with the yellow button or Cmd+M.
- **Close** hides the window but keeps it listening. Click the Dock icon to bring it back.
- **Keep on Top** in the Window menu (Cmd+T) keeps it above other apps. Off by default.
- **Quit** with Cmd+Q.

## Running from the terminal

Useful for seeing the Jev trace (every question, answer and confidence per turn):

```bash
.venv/bin/python siri.py               # hold right Option mode, trace prints to the terminal
.venv/bin/python siri.py --wake        # Hey Jev mode, always listening
.venv/bin/python siri.py --text "open spotify and turn it down"   # one turn, no mic
.venv/bin/python siri.py --ui          # same as the app, but shows as "Python" in the Dock
```

Keys can also go in a `.env` file in this folder (`TYPESAFE_API_KEY`, `FISH_AUDIO_API_KEY`, `OPENROUTER_API_KEY`). A key in `.env` takes priority over the one saved in the Keychain.

## How it works

1. Audio is recorded while you hold right Option. In Hey Jev mode the mic stays open, and each phrase is transcribed locally and only acted on if it starts with "Hey Jev".
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

## Troubleshooting

- **Holding Option does nothing.** The app needs Accessibility access. Add it under System Settings > Privacy & Security > Accessibility, then quit and reopen it.
- **"401 Unauthorized" in the window.** One of your keys is wrong or expired. Re-paste it with the Keys… button. If you also have a `.env`, check the key there, because it wins over the Keychain.
- **The app won't open again.** It's probably still running with the window closed. Click its Dock icon, or quit it properly with Cmd+Q and open it again.
- **It stopped controlling apps after a macOS update.** Updates can reset permissions. Check Microphone, Accessibility and Automation under Privacy & Security again.

## Files

- `siri.py` all the logic: questions, actions, replies, Whisper, Fish, LLM fallback
- `assistant_ui.py` the status window, mode switch and Keys panel
- `secrets_store.py` Keychain read / write
- `app.py` and `setup.py` the app bundle entry point and the py2app config, output lands in `dist/`
- `assets/` the app icon

## Change the voice

`VOICE_ID` at the top of `siri.py`. Find voices at [https://fish.audio](https://fish.audio), open one and copy its ID from the page link. Her replies re-render in the new voice automatically on the next launch.
