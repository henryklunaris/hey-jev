"""Store API keys in the macOS login Keychain, with .env as a dev fallback."""
import os
import subprocess

from dotenv import load_dotenv

load_dotenv()  # so a .env works from the app bundle too, not just the terminal


SERVICE = "com.jevsiri.keys"
KEY_NAMES = ("TYPESAFE_API_KEY", "FISH_AUDIO_API_KEY", "OPENROUTER_API_KEY")


def keychain_value(name):
    result = subprocess.run(
        ["security", "find-generic-password", "-a", name, "-s", SERVICE, "-w"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def get_secret(name):
    return os.getenv(name) or keychain_value(name)  # .env wins, so editing it always takes effect


def save_secret(name, value):
    if name not in KEY_NAMES:
        raise ValueError(f"unknown secret: {name}")
    value = value.strip()
    if not value:
        return
    result = subprocess.run(
        ["security", "add-generic-password", "-U", "-a", name, "-s", SERVICE, "-w", value],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Could not save to Keychain")


def missing_secrets():
    return [name for name in KEY_NAMES if not get_secret(name)]

