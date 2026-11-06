"""The quick app launcher: which apps the Roku has, and which of them you keep one click away. No Tk here.

The Roku lists what is installed at /query/apps, draws each app's picture at /query/icon/<id> and starts one with
POST /launch/<id>. Pinned apps are remembered by id, so renaming or moving an app on the Roku doesn't lose it."""
import xml.etree.ElementTree as ET

PLAYLET_ID = "693751"           # the Roku channel id of Playlet, which plays the YouTube tab's videos
DEFAULT_PINS = [PLAYLET_ID]     # Playlet is what this app is built around, so it is there from the start
MAX_PINS = 5                    # five apps plus the "All apps" tile fill three rows of two


def parse_apps(xml_text):
    """[{'id', 'name'}] for the apps installed on the Roku, sorted by name. TV inputs (HDMI 1, Live TV...) are left
    out: they can't be launched like an app. Returns [] for anything that isn't the Roku's app list."""
    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, TypeError, ValueError):
        return []
    apps = [{"id": a.get("id", ""), "name": (a.text or "").strip()} for a in root.iter("app")
            if a.get("type") == "appl" and a.get("id") and (a.text or "").strip()]
    return sorted(apps, key=lambda a: a["name"].lower())


def clean_pins(saved):
    """The pinned ids from the settings file: Playlet if nothing was ever saved, else what was saved (an empty list
    means you unpinned everything and means it)."""
    if not isinstance(saved, list):
        return list(DEFAULT_PINS)
    out = []
    for item in saved:
        item = str(item)
        if item and item not in out:
            out.append(item)
    return out[:MAX_PINS]


def toggle_pin(pins, app_id):
    """(new pins, what happened): 'pinned', 'unpinned' or 'full' (no room, nothing changed)."""
    if app_id in pins:
        return [p for p in pins if p != app_id], "unpinned"
    if len(pins) >= MAX_PINS:
        return list(pins), "full"
    return [*pins, app_id], "pinned"


def filter_apps(apps, text):
    """The apps whose name contains `text`, ignoring case. Everything for an empty filter."""
    text = (text or "").strip().lower()
    return [a for a in apps if text in a["name"].lower()] if text else list(apps)


def name_for(apps, app_id, fallback=""):
    """The app's name, or `fallback` (or the id) while the list hasn't loaded."""
    for a in apps:
        if a["id"] == app_id:
            return a["name"]
    return fallback or ("Playlet" if app_id == PLAYLET_ID else app_id)
