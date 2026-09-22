"""Small native macOS status window for the Jev voice assistant."""
import queue
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
    NSPanel,
    NSScreen,
    NSSecureTextField,
    NSTextField,
    NSVisualEffectBlendingModeBehindWindow,
    NSVisualEffectMaterialHUDWindow,
    NSVisualEffectStateActive,
    NSVisualEffectView,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskFullSizeContentView,
    NSWindowStyleMaskTitled,
)
from Foundation import NSObject
from secrets_store import KEY_NAMES, get_secret, missing_secrets, save_secret


STATUS_COLORS = {
    "Starting": NSColor.systemOrangeColor(),
    "Ready": NSColor.systemGreenColor(),
    "Listening": NSColor.systemRedColor(),
    "Transcribing": NSColor.systemBlueColor(),
    "Thinking": NSColor.systemPurpleColor(),
    "Doing it": NSColor.systemOrangeColor(),
    "Speaking": NSColor.systemTealColor(),
    "Something went wrong": NSColor.systemRedColor(),
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
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskFullSizeContentView
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 420, 154), style, NSBackingStoreBuffered, False
        )
        self.panel.setTitle_("Hey Jev - Fish Audio")
        self.panel.setTitlebarAppearsTransparent_(True)
        self.panel.setMovableByWindowBackground_(True)
        self.panel.setFloatingPanel_(True)
        self.panel.setHidesOnDeactivate_(False)

        background = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, 420, 154))
        background.setMaterial_(NSVisualEffectMaterialHUDWindow)
        background.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
        background.setState_(NSVisualEffectStateActive)
        self.panel.setContentView_(background)

        self.dot = label("●", NSMakeRect(25, 76, 24, 30), 18, NSColor.systemOrangeColor())
        self.status = label("Starting", NSMakeRect(55, 78, 330, 30), 22)
        self.detail = label("Loading Whisper…", NSMakeRect(27, 39, 365, 30), 14, NSColor.secondaryLabelColor())
        self.hint = label("Hold right Option to talk", NSMakeRect(27, 14, 365, 22), 12, NSColor.tertiaryLabelColor())
        for view in (self.dot, self.status, self.detail, self.hint):
            background.addSubview_(view)

        settings = NSButton.buttonWithTitle_target_action_("Keys…", self, "showSettings:")
        settings.setFrame_(NSMakeRect(343, 118, 65, 24))  # top right, in line with the title bar
        settings.setBezelStyle_(1)  # rounded, so the title shows (9 is the "?" help button)
        settings.setControlSize_(1)  # small
        settings.setFont_(NSFont.systemFontOfSize_(11))
        background.addSubview_(settings)

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
            self.showSettings_(None)
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
            run_voice_assistant(self.notify, self.controls)
        except Exception as exc:
            self.notify("Something went wrong", str(exc))

    def showSettings_(self, _sender):
        try:
            self._show_settings()
        except Exception as exc:  # a Python error inside an AppKit callback would otherwise kill the app
            self.notify("Something went wrong", str(exc))

    @objc.python_method
    def _show_settings(self):
        if getattr(self, "settings_panel", None):
            self.settings_panel.makeKeyAndOrderFront_(None)
            return

        self.settings_panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 460, 305), NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered, False
        )
        self.settings_panel.setTitle_("Hey Jev Keys")
        content = self.settings_panel.contentView()
        content.addSubview_(label("API keys", NSMakeRect(24, 245, 400, 32), 24))
        content.addSubview_(label("Saved in your Mac Keychain. Existing keys stay hidden.",
                                  NSMakeRect(25, 218, 410, 22), 13, NSColor.secondaryLabelColor()))

        field_names = (
            ("TypeSafe", "TYPESAFE_API_KEY"),
            ("Fish Audio", "FISH_AUDIO_API_KEY"),
            ("OpenRouter", "OPENROUTER_API_KEY"),
        )
        self.key_fields = {}
        for index, (title, key_name) in enumerate(field_names):
            y = 169 - index * 54
            content.addSubview_(label(title, NSMakeRect(25, y + 3, 92, 24), 13))
            field = NSSecureTextField.alloc().initWithFrame_(NSMakeRect(119, y, 315, 28))
            field.setBezelStyle_(1)  # rounded, like a search field
            field.setFocusRingType_(1)  # no square focus ring around the rounded field
            field.setPlaceholderString_("Already configured" if get_secret(key_name) else "Paste key")
            content.addSubview_(field)
            self.key_fields[key_name] = field

        self.settings_message = label("", NSMakeRect(25, 22, 290, 22), 12, NSColor.systemRedColor())
        content.addSubview_(self.settings_message)
        save = NSButton.buttonWithTitle_target_action_("Save", self, "saveSettings:")
        save.setFrame_(NSMakeRect(344, 15, 90, 32))
        save.setKeyEquivalent_("\r")
        content.addSubview_(save)
        self.settings_panel.center()
        self.settings_panel.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

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
            self.settings_panel.orderOut_(None)
            self.settings_panel = None
            self._start_worker()
        except Exception as exc:
            self.settings_message.setStringValue_(str(exc))

    @objc.python_method
    def notify(self, state, detail=""):
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "updateStatus:", {"state": state, "detail": detail}, False
        )

    def updateStatus_(self, payload):
        state = str(payload["state"])
        detail = str(payload.get("detail", ""))
        self.status.setStringValue_(state)
        self.detail.setStringValue_(detail)
        self.dot.setTextColor_(STATUS_COLORS.get(state, NSColor.labelColor()))

    def applicationShouldTerminateAfterLastWindowClosed_(self, _application):
        return False  # both windows are NSPanels, which AppKit doesn't count, so closing Keys would quit the app

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
