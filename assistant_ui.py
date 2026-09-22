"""Small native macOS status window for the Jev voice assistant."""
import queue
import sys
import threading

import objc
from AppKit import (
    NSApp,
    NSApplication,
    NSApplicationActivationPolicyRegular,
    NSBackingStoreBuffered,
    NSMenu,
    NSMenuItem,
    NSButton,
    NSColor,
    NSEvent,
    NSEventMaskFlagsChanged,
    NSEventModifierFlagOption,
    NSFont,
    NSMakePoint,
    NSMakeRect,
    NSScreen,
    NSSegmentedControl,
    NSWindow,
    NSSecureTextField,
    NSTextField,
    NSVisualEffectBlendingModeBehindWindow,
    NSVisualEffectMaterialHUDWindow,
    NSVisualEffectStateActive,
    NSVisualEffectView,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskFullSizeContentView,
    NSWindowStyleMaskTitled,
)
from Foundation import NSObject, NSTimer, NSUserDefaults
from secrets_store import KEY_NAMES, get_secret, missing_secrets, save_secret


BASE_HEIGHT, ROW = 154, 24
STICK_TOP, STICK_BOTTOM = 8, 32  # NSViewMinYMargin, NSViewMaxYMargin
NORMAL, FLOATING = 0, 3  # NSNormalWindowLevel, NSFloatingWindowLevel
HINTS = {"ptt": "Hold right Option to talk", "wake": "Say \u201cHey Jev\u201d, then your command"}
MODES = ("ptt", "wake")

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
}


def label(text, frame, size, color=None):
    view = NSTextField.labelWithString_(text)
    view.setFrame_(frame)
    view.setFont_(NSFont.systemFontOfSize_weight_(size, 0.5))
    view.setTextColor_(color or NSColor.labelColor())
    view.setLineBreakMode_(4)
    return view


class AppDelegate(NSObject):
    def applicationDidFinishLaunching_(self, _notification):
        self.controls = queue.Queue()
        self.option_down = False
        self.worker_started = False
        self.mode = NSUserDefaults.standardUserDefaults().stringForKey_("mode") or "ptt"
        if self.mode not in MODES:
            self.mode = "ptt"
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable
                 | NSWindowStyleMaskFullSizeContentView)
        self.panel = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 420, 154), style, NSBackingStoreBuffered, False
        )
        self.panel.setTitle_("Hey Jev - Fish Audio")
        self.panel.setTitlebarAppearsTransparent_(True)
        self.panel.setMovableByWindowBackground_(True)
        self.panel.setReleasedWhenClosed_(False)  # closing just hides it, the Dock icon brings it back
        self.on_top = NSUserDefaults.standardUserDefaults().boolForKey_("keep_on_top")
        self.panel.setLevel_(FLOATING if self.on_top else NORMAL)
        self._add_window_menu()

        background = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, 420, 154))
        background.setMaterial_(NSVisualEffectMaterialHUDWindow)
        background.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
        background.setState_(NSVisualEffectStateActive)
        background.setAutoresizingMask_(18)  # grow with the window
        self.panel.setContentView_(background)
        self.background = background
        self.timer_rows = []

        self.dot = label("●", NSMakeRect(25, 76, 24, 30), 18, NSColor.systemOrangeColor())
        self.status = label("Starting", NSMakeRect(55, 78, 330, 30), 22)
        self.detail = label("Loading Whisper…", NSMakeRect(27, 39, 365, 30), 14, NSColor.secondaryLabelColor())
        self.hint = label(HINTS[self.mode], NSMakeRect(27, 14, 220, 22), 12, NSColor.tertiaryLabelColor())
        for view in (self.dot, self.status, self.detail, self.hint):
            background.addSubview_(view)
        for view in (self.dot, self.status, self.detail):
            view.setAutoresizingMask_(STICK_TOP)
        self.hint.setAutoresizingMask_(STICK_BOTTOM)

        settings = NSButton.buttonWithTitle_target_action_("Keys…", self, "showSettings:")
        settings.setFrame_(NSMakeRect(343, 118, 65, 24))  # top right, in line with the title bar
        settings.setBezelStyle_(1)  # rounded, so the title shows (9 is the "?" help button)
        settings.setControlSize_(1)  # small
        settings.setFont_(NSFont.systemFontOfSize_(11))
        settings.setAutoresizingMask_(STICK_TOP)
        background.addSubview_(settings)

        self.mode_switch = NSSegmentedControl.segmentedControlWithLabels_trackingMode_target_action_(
            ["Hold Option", "Hey Jev"], 0, self, "modeChanged:"
        )
        self.mode_switch.setControlSize_(1)
        self.mode_switch.setFont_(NSFont.systemFontOfSize_(11))
        self.mode_switch.setFrame_(NSMakeRect(252, 12, 156, 24))
        self.mode_switch.setSelectedSegment_(MODES.index(self.mode))
        self.mode_switch.setAutoresizingMask_(STICK_BOTTOM)
        background.addSubview_(self.mode_switch)
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(0.5, self, "tick:", None, True)

        screen = NSScreen.mainScreen().visibleFrame()
        self.panel.setFrameOrigin_(NSMakePoint(screen.origin.x + (screen.size.width - 420) / 2,
                                               screen.origin.y + screen.size.height - 190))
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
            self.performSelector_withObject_afterDelay_("showSettings:", None, 0.3)
        else:
            self._start_worker()

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
            run_voice_assistant(self.notify, self.controls, self.mode)
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

    def showSettings_(self, _sender):
        try:
            self._show_settings()
        except Exception as exc:  # a Python error inside an AppKit callback would otherwise kill the app
            self.notify("Something went wrong", str(exc))

    @objc.python_method
    def _show_settings(self):
        if getattr(self, "settings_sheet", None):
            return
        sheet = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 400, 250), NSWindowStyleMaskTitled, NSBackingStoreBuffered, False
        )
        content = sheet.contentView()
        content.addSubview_(label("API keys", NSMakeRect(22, 204, 360, 30), 20))
        content.addSubview_(label("Saved in your Mac Keychain. Existing keys stay hidden.",
                                  NSMakeRect(23, 182, 360, 20), 12, NSColor.secondaryLabelColor()))

        field_names = (
            ("TypeSafe", "TYPESAFE_API_KEY"),
            ("Fish Audio", "FISH_AUDIO_API_KEY"),
            ("OpenRouter", "OPENROUTER_API_KEY"),
        )
        self.key_fields = {}
        for index, (title, key_name) in enumerate(field_names):
            y = 136 - index * 40
            content.addSubview_(label(title, NSMakeRect(23, y + 3, 84, 22), 13))
            field = NSSecureTextField.alloc().initWithFrame_(NSMakeRect(108, y, 270, 26))
            field.setBezelStyle_(1)  # rounded
            field.setFocusRingType_(1)
            field.setPlaceholderString_("Already configured" if get_secret(key_name) else "Paste key")
            content.addSubview_(field)
            self.key_fields[key_name] = field

        self.settings_message = label("", NSMakeRect(23, 20, 180, 20), 11, NSColor.systemRedColor())
        content.addSubview_(self.settings_message)
        save = NSButton.buttonWithTitle_target_action_("Save", self, "saveSettings:")
        save.setFrame_(NSMakeRect(298, 12, 82, 32))
        save.setKeyEquivalent_("\r")
        content.addSubview_(save)
        if not missing_secrets():
            cancel = NSButton.buttonWithTitle_target_action_("Cancel", self, "closeSettings:")
            cancel.setFrame_(NSMakeRect(210, 12, 86, 32))
            cancel.setKeyEquivalent_("\x1b")
            content.addSubview_(cancel)

        self.settings_sheet = sheet
        NSApp.activateIgnoringOtherApps_(True)
        self.panel.makeKeyAndOrderFront_(None)
        self.panel.beginSheet_completionHandler_(sheet, None)
        sheet.makeFirstResponder_(self.key_fields["TYPESAFE_API_KEY"])

    def closeSettings_(self, _sender):
        if getattr(self, "settings_sheet", None):
            self.panel.endSheet_(self.settings_sheet)
            self.settings_sheet.orderOut_(None)
            self.settings_sheet = None

    def saveSettings_(self, _sender):
        try:
            for key_name in KEY_NAMES:
                value = self.key_fields[key_name].stringValue()
                if value:
                    save_secret(key_name, value)
            still_missing = missing_secrets()
            if still_missing:
                names = ", ".join(name.replace("_API_KEY", "").replace("_", " ").title() for name in still_missing)
                self.settings_message.setStringValue_(f"Still needed: {names}")
                return
            from siri import reload_keys
            reload_keys()
            self.closeSettings_(None)
            self._start_worker()
        except Exception as exc:
            self.settings_message.setStringValue_(str(exc))

    @objc.python_method
    def notify(self, state, detail=""):
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "updateStatus:", {"state": state, "detail": detail}, False
        )

    def tick_(self, _timer):
        siri = sys.modules.get("siri")
        timers = siri.timer_snapshot()[:3] if siri else []
        if len(timers) != len(self.timer_rows):
            self._layout_timer_rows(len(timers))
        for (name_view, time_view), (name, left) in zip(self.timer_rows, timers):
            name_view.setStringValue_(name)
            m, sec = divmod(int(left + 0.999), 60)
            h, m = divmod(m, 60)
            time_view.setStringValue_(f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}")

    @objc.python_method
    def _layout_timer_rows(self, count):
        for name_view, time_view in self.timer_rows:
            name_view.removeFromSuperview()
            time_view.removeFromSuperview()
        self.timer_rows = []
        frame = self.panel.frame()
        height = BASE_HEIGHT + ROW * count + (8 if count else 0)
        top = frame.origin.y + frame.size.height
        self.panel.setFrame_display_animate_(NSMakeRect(frame.origin.x, top - height, frame.size.width, height), True, True)
        for i in range(count):
            y = 44 + ROW * (count - 1 - i)  # soonest on top, just above the bottom row
            name_view = label("", NSMakeRect(27, y, 280, 20), 13, NSColor.secondaryLabelColor())
            time_view = label("", NSMakeRect(310, y, 98, 20), 15, NSColor.systemTealColor())
            time_view.setFont_(NSFont.monospacedDigitSystemFontOfSize_weight_(15, 0.4))
            time_view.setAlignment_(2)  # right
            for v in (name_view, time_view):
                v.setAutoresizingMask_(STICK_BOTTOM)
                self.background.addSubview_(v)
            self.timer_rows.append((name_view, time_view))

    def updateStatus_(self, payload):
        state = str(payload["state"])
        detail = str(payload.get("detail", ""))
        self.status.setStringValue_(state)
        self.detail.setStringValue_(detail)
        self.dot.setTextColor_(STATUS_COLORS.get(state, NSColor.labelColor()))

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
