"""Shared lock + read/write for settings.env."""
import os
import threading

settings_lock = threading.Lock()
SETTINGS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "settings.env")


def read_settings() -> str:
    with settings_lock:
        try:
            with open(SETTINGS_PATH) as f:
                return f.read()
        except Exception:
            return ""


def write_settings(content: str):
    with settings_lock:
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w") as f:
            f.write(content)
        os.replace(tmp, SETTINGS_PATH)
