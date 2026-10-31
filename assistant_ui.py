"""Native macOS window for the Jev voice assistant: status on top, sidebar tabs for the dictionary, apps, history and keys."""
import json
import os
import re
import queue
import sys
import threading

import objc
from AppKit import (
    NSApp,
    NSApplication,
    NSApplicationActivationPolicyRegular,
    NSBackingStoreBuffered,
    NSBox,
    NSButton,
    NSColor,
    NSEvent,
    NSEventMaskFlagsChanged,
    NSEventModifierFlagOption,
    NSFont,
    NSMakeRect,
    NSMenu,
    NSMenuItem,
    NSPasteboard,
    NSPasteboardTypeString,
    NSPopUpButton,
    NSScrollView,
    NSSearchField,
    NSSecureTextField,
    NSSegmentedControl,
    NSTableColumn,
    NSTableView,
    NSTextField,
    NSView,
    NSVisualEffectBlendingModeBehindWindow,
    NSVisualEffectStateFollowsWindowActiveState,
    NSVisualEffectView,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskFullSizeContentView,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSObject, NSTimer, NSUserDefaults
from bubble import Bubble
from dictation import HISTORY, read_vocab, save_vocab
from secrets_store import KEY_NAMES, OPTIONAL, get_secret, missing_secrets, save_secret


W, H, SIDEBAR, HEADER = 900, 600, 210, 72
PAGE_W, PAGE_H = W - SIDEBAR, H - HEADER
NORMAL, FLOATING = 0, 3  # NSNormalWindowLevel, NSFloatingWindowLevel
SIDEBAR_MATERIAL = 7  # NSVisualEffectMaterialSidebar
HINTS = {"ptt": "Hold right Option to talk", "wake": "Say “Hey Jev”, then your command"}
MODES = ("ptt", "wake")
TABS = (("home", "Home"), ("dict", "Dictionary"), ("apps", "Apps"), ("hist", "Dictation history"), ("priv", "Privacy"), ("settings", "Settings"), ("keys", "Keys"))
LOG_FILE = os.path.expanduser("~/Library/Logs/Hey Jev.log")
TYPING_WPM, SPEAKING_WPM = 40, 150  # average typing vs talking speed, for "time saved"
ACTION_NAMES = {"app_open": "Open app", "app_quit": "Quit app", "app_hide": "Hide app", "app_minimise": "Minimise",
                "app_focus": "Switch to app", "media_play": "Play", "media_pause": "Pause", "media_next": "Next track",
                "media_previous": "Previous track", "timer_set": "Timers", "timer_remind": "Reminders"}
APPS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apps.json")
HOW_TO = ("“Hey Jev, open Spotify” runs a command. In Hold Option mode, hold right Option and just say it.\n"
          "“Hey Jev, transcribe” starts dictating, with a bubble at the bottom of the screen. "
          "Say “stop transcribing” and it pastes where your cursor is.")
PRIVACY = (
    ("Stays on your Mac", "Everything the mic hears. Whisper listens for \u201cHey Jev\u201d on your Mac and throws away anything that isn't for her. Your dictionary, dictation history and logs stay in this folder and ~/Library/Logs."),
    ("TypeSafe (Jev)  \u00b7  text", "Only the words after \u201cHey Jev\u201d, like \u201copen Spotify\u201d, to work out what to do."),
    ("OpenRouter or OpenAI  \u00b7  audio", "Dictation audio, only between \u201ctranscribe\u201d and \u201cstop transcribing\u201d. It goes to OpenAI directly if you added that key, otherwise through OpenRouter. Questions that aren't commands go to Claude Haiku on OpenRouter as text."),
    ("Fish Audio  \u00b7  text", "The text of her replies, to turn into her voice. Scripted replies are saved after the first time, so most are never sent again."),
)
KEY_ROWS = (
    ("TypeSafe", "TYPESAFE_API_KEY", "Jev, decides what each command means"),
    ("Fish Audio", "FISH_AUDIO_API_KEY", "Her voice"),
    ("OpenRouter", "OPENROUTER_API_KEY", "Questions, and dictation when there's no OpenAI key"),
    ("OpenAI", "OPENAI_API_KEY", "Optional, sends dictation straight to OpenAI"),
)

STATUS_COLORS = {
    "Starting": NSColor.systemOrangeColor(),
    "Ready": NSColor.systemGreenColor(),
    "Listening": NSColor.systemRedColor(),
    "Transcribing": NSColor.systemBlueColor(),
    "Thinking": NSColor.systemPurpleColor(),
    "Doing it": NSColor.systemOrangeColor(),
    "Speaking": NSColor.systemTealColor(),
    "Something went wrong": NSColor.systemRedColor(),
    "Time's up": NSColor.systemYellowColor(),
    "Dictating": NSColor.systemRedColor(),
    "Finishing": NSColor.systemBlueColor(),
}


def label(text, frame, size, color=None, weight=0.5):
    view = NSTextField.labelWithString_(text)
    view.setFrame_(frame)
    view.setFont_(NSFont.systemFontOfSize_weight_(size, weight))
    view.setTextColor_(color or NSColor.labelColor())
    view.setLineBreakMode_(4)  # truncate the end
    return view


def text_field(frame, placeholder, secure=False):
    field = (NSSecureTextField if secure else NSTextField).alloc().initWithFrame_(frame)
    field.setBezelStyle_(1)  # rounded
    field.setPlaceholderString_(placeholder)
    return field


def button(title, target, action, frame):
    b = NSButton.buttonWithTitle_target_action_(title, target, action)
    b.setFrame_(frame)
    b.setBezelStyle_(1)
    return b


def app_key(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")  # "Claude Code" -> claude_code, what Jev matches on


class ClickAwayView(NSView):
    def mouseDown_(self, event):
        self.window().makeFirstResponder_(None)  # clicking empty space takes focus out of the search and text boxes
        objc.super(ClickAwayView, self).mouseDown_(event)


class AppDelegate(NSObject):
    def applicationDidFinishLaunching_(self, _notification):
        self.controls = queue.Queue()
        self.option_down = False
        self.worker_started = False
        defaults = NSUserDefaults.standardUserDefaults()
        self.mode = defaults.stringForKey_("mode") or "ptt"
        if self.mode not in MODES:
            self.mode = "ptt"
        self.mic = defaults.stringForKey_("mic") or ""
        self.auto_enter = bool(defaults.boolForKey_("auto_enter"))
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable
                 | NSWindowStyleMaskFullSizeContentView)
        self.panel = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H), style, NSBackingStoreBuffered, False
        )
        self.panel.setTitle_("Hey Jev - Fish Audio")
        self.panel.setTitleVisibility_(1)  # hidden, the sidebar says it
        self.panel.setTitlebarAppearsTransparent_(True)
        self.panel.setMovableByWindowBackground_(True)
        self.panel.setReleasedWhenClosed_(False)  # closing just hides it, the Dock icon brings it back
        self.on_top = defaults.boolForKey_("keep_on_top")
        self.panel.setLevel_(FLOATING if self.on_top else NORMAL)
        self._add_window_menu()
        root = ClickAwayView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))
        self.panel.setContentView_(root)
        backdrop = NSBox.alloc().initWithFrame_(NSMakeRect(SIDEBAR, 0, W - SIDEBAR, H))
        backdrop.setBoxType_(4)  # custom, filled with the normal window colour
        backdrop.setBorderWidth_(0)
        backdrop.setFillColor_(NSColor.windowBackgroundColor())
        root.addSubview_(backdrop)
        self.bubble = Bubble()

        self._build_sidebar(root)
        self._build_header(root)
        self.pages = {"home": self._build_home(), "dict": self._build_dictionary(), "apps": self._build_apps(), "hist": self._build_history(),
                      "priv": self._build_privacy(), "settings": self._build_settings(), "keys": self._build_keys()}
        for page in self.pages.values():
            root.addSubview_(page)
        self._select_tab("keys" if missing_secrets() else "home")
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(0.5, self, "tick:", None, True)

        self.panel.center()
        self.panel.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)
        self.global_monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            NSEventMaskFlagsChanged, self._global_flags_changed
        )
        self.local_monitor = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            NSEventMaskFlagsChanged, self._local_flags_changed
        )
        if missing_secrets():
            self.updateStatus_({"state": "Starting", "detail": "Add your API keys to begin"})
        else:
            self._start_worker()

    # ------------------------------------------------------------------ layout
    @objc.python_method
    def _build_sidebar(self, root):
        side = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, SIDEBAR, H))
        side.setMaterial_(SIDEBAR_MATERIAL)
        side.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
        side.setState_(NSVisualEffectStateFollowsWindowActiveState)
        root.addSubview_(side)
        side.addSubview_(label("Hey Jev", NSMakeRect(20, H - 82, 170, 22), 15, weight=0.6))
        self.tab_rows = {}
        for i, (key, title) in enumerate(TABS):
            y = H - 124 - i * 34
            box = NSBox.alloc().initWithFrame_(NSMakeRect(10, y, SIDEBAR - 20, 30))
            box.setBoxType_(4)  # custom, so it can be filled
            box.setBorderWidth_(0)
            box.setCornerRadius_(7)
            box.setTitlePosition_(0)  # no title
            side.addSubview_(box)
            tab = NSButton.buttonWithTitle_target_action_(title, self, "tabClicked:")
            tab.setFrame_(NSMakeRect(20, y + 3, SIDEBAR - 40, 24))
            tab.setBordered_(False)
            tab.setAlignment_(0)  # left
            tab.setTag_(i)
            side.addSubview_(tab)
            self.tab_rows[key] = (box, tab)
        self.timer_rows = []
        for i in range(3):  # up to 3 timers, soonest on top
            y = 52 + (2 - i) * 22
            name_view = label("", NSMakeRect(20, y, 110, 20), 12, NSColor.secondaryLabelColor())
            time_view = label("", NSMakeRect(126, y, 64, 20), 13, NSColor.systemTealColor())
            time_view.setFont_(NSFont.monospacedDigitSystemFontOfSize_weight_(13, 0.4))
            time_view.setAlignment_(2)  # right
            for v in (name_view, time_view):
                v.setHidden_(True)
                side.addSubview_(v)
            self.timer_rows.append((name_view, time_view))
        self.hint = NSTextField.wrappingLabelWithString_(HINTS[self.mode])
        self.hint.setFrame_(NSMakeRect(20, 10, SIDEBAR - 36, 32))
        self.hint.setFont_(NSFont.systemFontOfSize_(11))
        self.hint.setTextColor_(NSColor.tertiaryLabelColor())
        side.addSubview_(self.hint)

    @objc.python_method
    def _build_header(self, root):
        x = SIDEBAR + 24
        self.dot = label("●", NSMakeRect(x, H - 42, 20, 24), 15, NSColor.systemOrangeColor())
        self.status = label("Starting", NSMakeRect(x + 22, H - 42, 300, 24), 17, weight=0.6)
        self.detail = label("Loading Whisper…", NSMakeRect(x, H - 64, 440, 20), 13, NSColor.secondaryLabelColor(), 0.0)
        for v in (self.dot, self.status, self.detail):
            root.addSubview_(v)
        self.mode_switch = NSSegmentedControl.segmentedControlWithLabels_trackingMode_target_action_(
            ["Hold Option", "Hey Jev"], 0, self, "modeChanged:"
        )
        self.mode_switch.setFrame_(NSMakeRect(W - 196, H - 46, 172, 26))
        self.mode_switch.setSelectedSegment_(MODES.index(self.mode))
        root.addSubview_(self.mode_switch)
        line = NSBox.alloc().initWithFrame_(NSMakeRect(SIDEBAR, H - HEADER, W - SIDEBAR, 1))
        line.setBoxType_(2)  # separator
        root.addSubview_(line)

    @objc.python_method
    def _page(self, title, subtitle):
        page = NSView.alloc().initWithFrame_(NSMakeRect(SIDEBAR, 0, PAGE_W, PAGE_H))
        page.addSubview_(label(title, NSMakeRect(24, PAGE_H - 50, 400, 30), 22, weight=0.6))
        sub = label(subtitle, NSMakeRect(24, PAGE_H - 74, PAGE_W - 48, 20), 13, NSColor.secondaryLabelColor(), 0.0)
        page.addSubview_(sub)
        return page

    @objc.python_method
    def _table(self, page, frame, columns, editable=True):
        scroll = NSScrollView.alloc().initWithFrame_(frame)
        table = NSTableView.alloc().initWithFrame_(scroll.bounds())
        for ident, title, width in columns:
            col = NSTableColumn.alloc().initWithIdentifier_(ident)
            col.setWidth_(width)
            col.headerCell().setStringValue_(title)
            col.setEditable_(editable)
            table.addTableColumn_(col)
        table.setRowHeight_(24)
        table.setUsesAlternatingRowBackgroundColors_(True)
        table.setDataSource_(self)
        table.setDelegate_(self)
        scroll.setDocumentView_(table)
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(2)  # bezel
        page.addSubview_(scroll)
        return table

    @objc.python_method
    def _build_home(self):
        page = self._page("Your Jev stats", "Everything since you started using Jev. Refreshes each time you open this tab.")
        self.stat_cards = []
        card_w, card_h, gap = (PAGE_W - 48 - 32) / 3, 104, 16
        for i in range(6):
            x = 24 + (i % 3) * (card_w + gap)
            y = PAGE_H - 106 - card_h - (i // 3) * (card_h + gap)
            card = NSBox.alloc().initWithFrame_(NSMakeRect(x, y, card_w, card_h))
            card.setBoxType_(4)
            card.setBorderWidth_(0)
            card.setCornerRadius_(12)
            card.setFillColor_(NSColor.quaternaryLabelColor().colorWithAlphaComponent_(0.12))
            page.addSubview_(card)
            value = label("", NSMakeRect(x + 16, y + 52, card_w - 32, 40), 30, weight=0.6)
            title = label("", NSMakeRect(x + 16, y + 30, card_w - 32, 20), 13, NSColor.labelColor(), 0.2)
            sub = label("", NSMakeRect(x + 16, y + 12, card_w - 32, 18), 11, NSColor.secondaryLabelColor(), 0.0)
            for v in (value, title, sub):
                page.addSubview_(v)
            self.stat_cards.append((value, title, sub))
        top_y = PAGE_H - 106 - 2 * card_h - gap - 40
        page.addSubview_(label("Most used", NSMakeRect(24, top_y, 200, 20), 14, weight=0.6))
        self.top_actions = NSTextField.wrappingLabelWithString_("")
        self.top_actions.setFrame_(NSMakeRect(24, top_y - 40, PAGE_W - 48, 38))
        self.top_actions.setFont_(NSFont.systemFontOfSize_(13))
        self.top_actions.setTextColor_(NSColor.secondaryLabelColor())
        page.addSubview_(self.top_actions)
        page.addSubview_(label("How to use", NSMakeRect(24, top_y - 70, 200, 20), 14, weight=0.6))
        how = NSTextField.wrappingLabelWithString_(HOW_TO)
        how.setFrame_(NSMakeRect(24, 10, PAGE_W - 48, top_y - 82))
        how.setFont_(NSFont.systemFontOfSize_(13))
        how.setTextColor_(NSColor.secondaryLabelColor())
        page.addSubview_(how)
        return page

    @objc.python_method
    def _load_stats(self):
        words = dictations = words_today = dictations_today = 0
        today = __import__("time").strftime("%Y-%m-%d")
        if os.path.exists(HISTORY):
            with open(HISTORY, encoding="utf-8") as f:
                for line in f:
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    n = len(item.get("text", "").split())
                    words += n
                    dictations += 1
                    if item.get("time", "").startswith(today):
                        words_today += n
                        dictations_today += 1
        heard, actions, cost, opened = 0, {}, 0.0, {}
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith("> heard:"):
                        heard += 1
                    elif line.startswith("  action: "):
                        parts = line.split()
                        actions[parts[1]] = actions.get(parts[1], 0) + 1
                        if parts[1] == "app_open" and len(parts) > 2:
                            opened[parts[2]] = opened.get(parts[2], 0) + 1
                    elif line.startswith("  jev ") and "$" in line:
                        try:
                            cost += float(line.rsplit("$", 1)[1])
                        except ValueError:
                            pass
        saved = words / TYPING_WPM - words / SPEAKING_WPM
        saved_text = f"{int(saved // 60)} h {int(saved % 60)} min" if saved >= 60 else f"{saved:.0f} min" if saved >= 1 else f"{saved * 60:.0f} sec"
        names = {row[0]: row[1] for row in getattr(self, "apps", [])}
        fav = max(opened, key=opened.get) if opened else None
        cards = (
            (saved_text, "Time saved", f"vs typing at {TYPING_WPM} words a minute"),
            (f"{words:,}", "Words dictated", f"{words_today:,} today"),
            (f"{dictations:,}", "Dictations", f"{dictations_today:,} today"),
            (f"{sum(actions.values()):,}", "Commands done", f"from {heard:,} things she heard"),
            (names.get(fav, fav or "None yet"), "Most opened app", f"opened {opened[fav]} times" if fav else "Say \u201cHey Jev, open Spotify\u201d"),
            (f"${cost:.4f}", "Spent on Jev", "TypeSafe, all time"),
        )
        for (value, title, sub), (v, t, s) in zip(self.stat_cards, cards):
            value.setStringValue_(v)
            title.setStringValue_(t)
            sub.setStringValue_(s)
        top = sorted(actions.items(), key=lambda kv: -kv[1])[:6]
        self.top_actions.setStringValue_("   \u00b7   ".join(f"{ACTION_NAMES.get(a, a.replace('_', ' ').capitalize())}  {n}" for a, n in top)
                                         or "Nothing yet. Say \u201cHey Jev\u201d and a command to get started.")

    @objc.python_method
    def _build_dictionary(self):
        page = self._page("Dictionary", "Words the transcriber gets wrong. Anything under “heard as” is swapped for the word.")
        self.search = NSSearchField.alloc().initWithFrame_(NSMakeRect(PAGE_W - 224, PAGE_H - 48, 200, 26))
        self.search.setPlaceholderString_("Search")
        self.search.setTarget_(self)
        self.search.setAction_("searchChanged:")
        page.addSubview_(self.search)

        top = PAGE_H - 118
        self.new_word = text_field(NSMakeRect(24, top, 170, 26), "Correct word")
        self.new_alts = text_field(NSMakeRect(202, top, 340, 26), "Heard as, comma separated")
        self.new_alts.setTarget_(self)
        self.new_alts.setAction_("addWord:")  # Return in the second box adds it
        for v in (self.new_word, self.new_alts):
            page.addSubview_(v)
        page.addSubview_(button("Add word", self, "addWord:", NSMakeRect(550, top - 2, 116, 30)))

        self.table = self._table(page, NSMakeRect(24, 56, PAGE_W - 48, top - 70),
                                 (("word", "Word", 170), ("heard", "Heard as (double-click to edit)", 440)))

        page.addSubview_(button("Remove", self, "removeWord:", NSMakeRect(20, 14, 100, 30)))
        self.dict_message = label("", NSMakeRect(130, 20, 400, 20), 12, NSColor.secondaryLabelColor(), 0.0)
        page.addSubview_(self.dict_message)
        self.vocab = [[w, list(alts)] for w, alts in read_vocab().items()]
        self._filter_vocab()
        return page

    @objc.python_method
    def _build_apps(self):
        page = self._page("Apps", "What Jev can open, quit, hide, minimise or switch to. Restart Jev after changes (Cmd+Q, reopen).")
        top = PAGE_H - 118
        self.new_say = text_field(NSMakeRect(24, top, 140, 26), "Name you say")
        self.new_app = text_field(NSMakeRect(172, top, 170, 26), "App name")
        self.new_heard = text_field(NSMakeRect(350, top, 192, 26), "Heard as (optional)")
        self.new_heard.setTarget_(self)
        self.new_heard.setAction_("addApp:")
        for v in (self.new_say, self.new_app, self.new_heard):
            page.addSubview_(v)
        page.addSubview_(button("Add app", self, "addApp:", NSMakeRect(550, top - 2, 116, 30)))
        self.apps_table = self._table(page, NSMakeRect(24, 56, PAGE_W - 48, top - 70),
                                      (("say", "Name you say", 130), ("app", "App", 170), ("heard", "Heard as (double-click to edit)", 310)))
        page.addSubview_(button("Remove", self, "removeApp:", NSMakeRect(20, 14, 100, 30)))
        self.apps_message = label("", NSMakeRect(130, 20, 500, 20), 12, NSColor.secondaryLabelColor(), 0.0)
        page.addSubview_(self.apps_message)
        with open(APPS_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        # each row is [name you say (the key), app, heard as, spoken name in replies or None]
        self.apps = [[k, v, [], None] if isinstance(v, str) else [k, v["app"], list(v.get("heard_as", [])), v.get("say")]
                     for k, v in raw.items()]
        return page

    @objc.python_method
    def _build_history(self):
        page = self._page("Dictation history", "Everything you\u2019ve dictated, newest first. Kept on this Mac only.")
        self.hist_table = self._table(page, NSMakeRect(24, 56, PAGE_W - 48, PAGE_H - 146),
                                      (("time", "When", 130), ("text", "Text", 480)), editable=False)
        page.addSubview_(button("Copy", self, "copyHistory:", NSMakeRect(20, 14, 100, 30)))
        self.hist_message = label("", NSMakeRect(130, 20, 500, 20), 12, NSColor.secondaryLabelColor(), 0.0)
        page.addSubview_(self.hist_message)
        self.history = []
        return page

    @objc.python_method
    def _build_privacy(self):
        page = self._page("Privacy", "In Hey Jev mode the mic is always on, but it\u2019s heard on your Mac first. Only this ever leaves it.")
        y = PAGE_H - 110
        for title, body in PRIVACY:
            page.addSubview_(label(title, NSMakeRect(24, y, PAGE_W - 48, 20), 14, weight=0.6))
            text = NSTextField.wrappingLabelWithString_(body)
            text.setFrame_(NSMakeRect(24, y - 58, PAGE_W - 48, 56))
            text.setFont_(NSFont.systemFontOfSize_(13))
            text.setTextColor_(NSColor.secondaryLabelColor())
            page.addSubview_(text)
            y -= 96
        return page

    @objc.python_method
    def _build_settings(self):
        page = self._page("Settings", "Pick the microphone Jev listens with. It switches straight away.")
        y = PAGE_H - 140
        page.addSubview_(label("Microphone", NSMakeRect(24, y + 16, 160, 20), 14, weight=0.6))
        page.addSubview_(label("Plugged in a new one? Restart Jev to see it here.", NSMakeRect(24, y - 2, 330, 18), 11,
                               NSColor.secondaryLabelColor(), 0.0))
        self.mic_menu = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(360, y + 4, 306, 26), False)
        self.mic_menu.setTarget_(self)
        self.mic_menu.setAction_("micChanged:")
        page.addSubview_(self.mic_menu)
        self.mic_message = label("", NSMakeRect(24, y - 40, PAGE_W - 48, 20), 12, NSColor.secondaryLabelColor(), 0.0)
        page.addSubview_(self.mic_message)
        y -= 90
        page.addSubview_(label("Press Enter after pasting", NSMakeRect(24, y + 16, 300, 20), 14, weight=0.6))
        page.addSubview_(label("Sends your dictation straight away, so you don't have to touch the keyboard.",
                               NSMakeRect(24, y - 2, PAGE_W - 120, 18), 11, NSColor.secondaryLabelColor(), 0.0))
        self.enter_box = NSButton.checkboxWithTitle_target_action_("", self, "autoEnterChanged:")
        self.enter_box.setFrame_(NSMakeRect(PAGE_W - 44, y + 14, 24, 24))
        self.enter_box.setState_(1 if self.auto_enter else 0)
        page.addSubview_(self.enter_box)
        return page

    def autoEnterChanged_(self, sender):
        self.auto_enter = sender.state() == 1
        NSUserDefaults.standardUserDefaults().setBool_forKey_(self.auto_enter, "auto_enter")
        self.controls.put(("auto_enter", self.auto_enter))

    @objc.python_method
    def _load_mics(self):
        try:
            import sounddevice as sd
            names = list(dict.fromkeys(d["name"] for d in sd.query_devices() if d["max_input_channels"] > 0))
            default = sd.query_devices(kind="input")["name"]
        except Exception:
            names, default = [], "none found"
        self.mic_menu.removeAllItems()
        self.mic_menu.addItemWithTitle_(f"System default ({default})")
        self.mic_menu.addItemsWithTitles_(names)
        if self.mic in names:
            self.mic_menu.selectItemWithTitle_(self.mic)
            self.mic_message.setStringValue_(f"Now using: {self.mic}")
        else:
            self.mic_menu.selectItemAtIndex_(0)
            gone = f"{self.mic} isn’t plugged in, so " if self.mic else ""
            self.mic_message.setStringValue_(f"{gone}Now using: {default}")

    def micChanged_(self, sender):
        self.mic = "" if sender.indexOfSelectedItem() == 0 else str(sender.titleOfSelectedItem())
        NSUserDefaults.standardUserDefaults().setObject_forKey_(self.mic, "mic")
        self.controls.put(("mic", self.mic))
        self._load_mics()

    @objc.python_method
    def _build_keys(self):
        page = self._page("Keys", "Saved in your Mac Keychain. A key in .env wins over these. Existing keys stay hidden.")
        self.key_fields = {}
        for i, (title, key_name, use) in enumerate(KEY_ROWS):
            y = PAGE_H - 140 - i * 64
            page.addSubview_(label(title, NSMakeRect(24, y + 16, 160, 20), 14, weight=0.6))
            page.addSubview_(label(use, NSMakeRect(24, y - 2, 330, 18), 11, NSColor.secondaryLabelColor(), 0.0))
            placeholder = "Already configured" if get_secret(key_name) else ("Optional" if key_name in OPTIONAL else "Paste key")
            field = text_field(NSMakeRect(360, y + 4, 306, 26), placeholder, secure=True)
            page.addSubview_(field)
            self.key_fields[key_name] = field
        y = PAGE_H - 140 - len(KEY_ROWS) * 64
        page.addSubview_(button("Save keys", self, "saveSettings:", NSMakeRect(PAGE_W - 136, y + 8, 116, 30)))
        self.settings_message = label("", NSMakeRect(24, y + 14, 480, 20), 12, NSColor.systemRedColor(), 0.0)
        page.addSubview_(self.settings_message)
        return page

    # ------------------------------------------------------------------ tabs
    def tabClicked_(self, sender):
        self._select_tab(TABS[sender.tag()][0])

    @objc.python_method
    def _select_tab(self, key):
        if key == "hist":
            self._load_history()
        if key == "home":
            self._load_stats()
        if key == "settings":
            self._load_mics()
        for name, page in self.pages.items():
            page.setHidden_(name != key)
        for name, (box, tab) in self.tab_rows.items():
            on = name == key
            box.setFillColor_(NSColor.quaternaryLabelColor() if on else NSColor.clearColor())
            tab.setFont_(NSFont.systemFontOfSize_weight_(13, 0.5 if on else 0.0))

    def showSettings_(self, _sender):
        self.showMain_(None)
        self._select_tab("keys")

    # ------------------------------------------------------------------ dictionary
    @objc.python_method
    def _filter_vocab(self):
        q = self.search.stringValue().strip().lower()
        self.visible = [i for i, (w, alts) in enumerate(self.vocab)
                        if not q or q in w.lower() or any(q in a.lower() for a in alts)]
        self.table.reloadData()

    @objc.python_method
    def _save_vocab(self, message):
        try:
            save_vocab({w: alts for w, alts in self.vocab})
            self.dict_message.setStringValue_(message)
        except Exception as exc:
            self.dict_message.setStringValue_(f"Couldn't save: {exc}")
        self._filter_vocab()

    def numberOfRowsInTableView_(self, table):
        if table == getattr(self, "apps_table", None):
            return len(self.apps)
        if table == getattr(self, "hist_table", None):
            return len(self.history)
        return len(getattr(self, "visible", []))

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        ident = column.identifier()
        if table == getattr(self, "apps_table", None):
            key, app, heard, _ = self.apps[row]
            return {"say": key.replace("_", " "), "app": app}.get(ident, ", ".join(heard))
        if table == getattr(self, "hist_table", None):
            return self.history[row][ident]
        word, alts = self.vocab[self.visible[row]]
        return word if ident == "word" else ", ".join(alts)

    def tableView_setObjectValue_forTableColumn_row_(self, table, value, column, row):
        value = str(value or "").strip()
        if table == getattr(self, "apps_table", None):
            entry, ident = self.apps[row], column.identifier()
            if ident == "heard":
                entry[2] = [a.strip() for a in value.split(",") if a.strip()]
            elif ident == "say" and app_key(value):
                entry[0] = app_key(value)
            elif ident == "app" and value:
                entry[1] = value
            self._save_apps("Saved, restart Jev to use it")
            return
        entry = self.vocab[self.visible[row]]
        if column.identifier() == "word":
            if value:
                entry[0] = value
        else:
            entry[1] = [a.strip() for a in value.split(",") if a.strip()]
        self._save_vocab("Saved, used straight away")

    def searchChanged_(self, _sender):
        self._filter_vocab()

    def addWord_(self, _sender):
        word = self.new_word.stringValue().strip()
        alts = [a.strip() for a in self.new_alts.stringValue().split(",") if a.strip()]
        if not word or not alts:
            self.dict_message.setStringValue_("Add the word and at least one way it gets misheard")
            return
        for entry in self.vocab:
            if entry[0].lower() == word.lower():  # already there, just add the new spellings
                entry[1] += [a for a in alts if a.lower() not in (x.lower() for x in entry[1])]
                break
        else:
            self.vocab.insert(0, [word, alts])
        self.new_word.setStringValue_("")
        self.new_alts.setStringValue_("")
        self.search.setStringValue_("")
        self._save_vocab(f"Added {word}, used straight away")
        self.panel.makeFirstResponder_(self.new_word)

    def removeWord_(self, _sender):
        row = self.table.selectedRow()
        if row < 0:
            self.dict_message.setStringValue_("Pick a word in the list first")
            return
        word = self.vocab.pop(self.visible[row])[0]
        self._save_vocab(f"Removed {word}")

    # ------------------------------------------------------------------ apps
    @objc.python_method
    def _save_apps(self, message):
        lines = []
        for key, app, heard, say in self.apps:
            if heard or say:
                value = {"app": app, **({"say": say} if say else {}), **({"heard_as": heard} if heard else {})}
            else:
                value = app
            lines.append(f"  {json.dumps(key)}: {json.dumps(value, ensure_ascii=False)}")
        try:
            with open(APPS_FILE, "w", encoding="utf-8") as f:
                f.write("{\n" + ",\n".join(lines) + "\n}\n")  # one app per line, like the file you'd write by hand
            self.apps_message.setStringValue_(message)
        except Exception as exc:
            self.apps_message.setStringValue_(f"Couldn't save: {exc}")
        self.apps_table.reloadData()

    def addApp_(self, _sender):
        say, app = self.new_say.stringValue().strip(), self.new_app.stringValue().strip()
        heard = [a.strip() for a in self.new_heard.stringValue().split(",") if a.strip()]
        if not say or not app:
            self.apps_message.setStringValue_("Add the name you say and the app\u2019s name")
            return
        key = app_key(say)
        if any(row[0] == key for row in self.apps):
            self.apps_message.setStringValue_(f"{say} is already in the list")
            return
        self.apps.append([key, app, heard, None])
        for v in (self.new_say, self.new_app, self.new_heard):
            v.setStringValue_("")
        self._save_apps(f"Added {say}, restart Jev to use it")
        self.panel.makeFirstResponder_(self.new_say)

    def removeApp_(self, _sender):
        row = self.apps_table.selectedRow()
        if row < 0:
            self.apps_message.setStringValue_("Pick an app in the list first")
            return
        name = self.apps.pop(row)[0].replace("_", " ")
        self._save_apps(f"Removed {name}, restart Jev to finish")

    # ------------------------------------------------------------------ history
    @objc.python_method
    def _load_history(self):
        self.history = []
        if os.path.exists(HISTORY):
            with open(HISTORY, encoding="utf-8") as f:
                for line in f:
                    try:
                        item = json.loads(line)
                        self.history.append({"time": item["time"][:16], "text": item["text"]})
                    except (ValueError, KeyError):
                        continue  # skip a half-written line
        self.history.reverse()
        self.hist_table.reloadData()
        self.hist_message.setStringValue_("" if self.history else "Nothing yet. Say \u201cHey Jev, transcribe\u201d to start.")

    def copyHistory_(self, _sender):
        row = self.hist_table.selectedRow()
        if row < 0:
            self.hist_message.setStringValue_("Pick one in the list first")
            return
        board = NSPasteboard.generalPasteboard()
        board.clearContents()
        board.setString_forType_(self.history[row]["text"], NSPasteboardTypeString)
        self.hist_message.setStringValue_("Copied")

    # ------------------------------------------------------------------ keys
    def saveSettings_(self, _sender):
        try:
            for key_name in KEY_NAMES:
                value = self.key_fields[key_name].stringValue()
                if value:
                    save_secret(key_name, value)
                    self.key_fields[key_name].setStringValue_("")
                    self.key_fields[key_name].setPlaceholderString_("Already configured")
            still_missing = missing_secrets()
            if still_missing:
                names = ", ".join(name.replace("_API_KEY", "").replace("_", " ").title() for name in still_missing)
                self.settings_message.setTextColor_(NSColor.systemRedColor())
                self.settings_message.setStringValue_(f"Still needed: {names}")
                return
            from siri import reload_keys
            reload_keys()
            self.settings_message.setTextColor_(NSColor.secondaryLabelColor())
            self.settings_message.setStringValue_("Saved")
            self._start_worker()
        except Exception as exc:
            self.settings_message.setTextColor_(NSColor.systemRedColor())
            self.settings_message.setStringValue_(str(exc))

    # ------------------------------------------------------------------ assistant
    @objc.python_method
    def _global_flags_changed(self, event):
        self._handle_flags(event)

    @objc.python_method
    def _local_flags_changed(self, event):
        self._handle_flags(event)
        return event

    @objc.python_method
    def _handle_flags(self, event):
        if event.keyCode() != 61:
            return
        is_down = bool(event.modifierFlags() & NSEventModifierFlagOption)
        if is_down != self.option_down:
            self.option_down = is_down
            self.controls.put("press" if is_down else "release")

    @objc.python_method
    def _start_worker(self):
        if self.worker_started:
            return
        self.worker_started = True
        threading.Thread(target=self._run_assistant, daemon=True).start()

    @objc.python_method
    def _run_assistant(self):
        from siri import run_voice_assistant
        try:
            run_voice_assistant(self.notify, self.controls, self.mode, self.mic, self.auto_enter)
        except Exception as exc:
            self.notify("Something went wrong", str(exc))

    @objc.python_method
    def _add_window_menu(self):
        item = NSMenuItem.alloc().init()
        NSApp.mainMenu().addItem_(item)
        menu = NSMenu.alloc().initWithTitle_("Window")
        menu.addItemWithTitle_action_keyEquivalent_("Minimize", "performMiniaturize:", "m")
        self.on_top_item = menu.addItemWithTitle_action_keyEquivalent_("Keep on Top", "toggleOnTop:", "t")
        self.on_top_item.setTarget_(self)
        self.on_top_item.setState_(1 if self.on_top else 0)
        menu.addItemWithTitle_action_keyEquivalent_("Show Hey Jev", "showMain:", "1").setTarget_(self)
        item.setSubmenu_(menu)
        NSApp.setWindowsMenu_(menu)

    def toggleOnTop_(self, _sender):
        self.on_top = not self.on_top
        NSUserDefaults.standardUserDefaults().setBool_forKey_(self.on_top, "keep_on_top")
        self.panel.setLevel_(FLOATING if self.on_top else NORMAL)
        self.on_top_item.setState_(1 if self.on_top else 0)

    def showMain_(self, _sender):
        self.panel.deminiaturize_(None)
        self.panel.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

    def applicationShouldHandleReopen_hasVisibleWindows_(self, _app, _visible):
        self.showMain_(None)
        return True

    def modeChanged_(self, sender):
        self.mode = MODES[sender.selectedSegment()]
        NSUserDefaults.standardUserDefaults().setObject_forKey_(self.mode, "mode")
        self.hint.setStringValue_(HINTS[self.mode])
        self.controls.put(("mode", self.mode))

    @objc.python_method
    def notify(self, state, detail=""):
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "updateStatus:", {"state": state, "detail": detail}, False
        )

    def tick_(self, _timer):
        siri = sys.modules.get("siri")
        timers = siri.timer_snapshot()[:3] if hasattr(siri, "timer_snapshot") else []  # siri may still be loading
        for i, (name_view, time_view) in enumerate(self.timer_rows):
            name_view.setHidden_(i >= len(timers))
            time_view.setHidden_(i >= len(timers))
            if i < len(timers):
                name, left = timers[i]
                m, sec = divmod(int(left + 0.999), 60)
                h, m = divmod(m, 60)
                name_view.setStringValue_(name)
                time_view.setStringValue_(f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}")

    def updateStatus_(self, payload):
        state = str(payload["state"])
        detail = str(payload.get("detail", ""))
        self.status.setStringValue_(state)
        self.detail.setStringValue_(detail)
        self.dot.setTextColor_(STATUS_COLORS.get(state, NSColor.labelColor()))
        if state in ("Dictating", "Finishing"):
            self.bubble.show(working=state == "Finishing")
        else:
            self.bubble.hide()

    def applicationShouldTerminateAfterLastWindowClosed_(self, _application):
        return False  # keep listening with the window closed, the Dock icon reopens it

    def applicationWillTerminate_(self, _notification):
        if getattr(self, "global_monitor", None):
            NSEvent.removeMonitor_(self.global_monitor)
        if getattr(self, "local_monitor", None):
            NSEvent.removeMonitor_(self.local_monitor)


def build_menu():
    """App and Edit menus, so Cmd+Q works and Cmd+V pastes into the key fields."""
    bar = NSMenu.alloc().init()
    app_item = NSMenuItem.alloc().init()
    bar.addItem_(app_item)
    app_menu = NSMenu.alloc().init()
    app_menu.addItemWithTitle_action_keyEquivalent_("Quit Hey Jev", "terminate:", "q")
    app_item.setSubmenu_(app_menu)
    edit_item = NSMenuItem.alloc().init()
    bar.addItem_(edit_item)
    edit = NSMenu.alloc().initWithTitle_("Edit")
    for title, action, key in (("Undo", "undo:", "z"), ("Cut", "cut:", "x"), ("Copy", "copy:", "c"),
                               ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")):
        edit.addItemWithTitle_action_keyEquivalent_(title, action, key)
    edit_item.setSubmenu_(edit)
    NSApp.setMainMenu_(bar)


def run_app():
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)  # shows in the Dock with a menu bar
    build_menu()
    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    app.run()
