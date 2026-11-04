"""The Web tab's private Brave profile.

Brave blocks ads with filter lists it downloads into its profile. A brand-new profile never fetches them (it stays
empty, so nothing is blocked), but your normal Brave profile has them. So before each launch the lists are copied
from there into the Web profile. A separate profile is needed anyway: Chrome-family browsers only allow remote
debugging (which the pop-up/redirect guard uses) on a non-default profile folder.
"""
import os
import re
import shutil

BRAVE_PATHS = [
    r"%LocalAppData%\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"%ProgramFiles(x86)%\BraveSoftware\Brave-Browser\Application\brave.exe",
]
SOURCE = os.path.expandvars(r"%LocalAppData%\BraveSoftware\Brave-Browser\User Data")
# new-tab-page photo/sponsor bundles: big and nothing to do with blocking
SKIP = {"aoojcmojmmcbpfgoecoadbdpnagfchel", "gccbbckogglekeggclmmekihdgdpdgoe"}
MAX_COMPONENT_BYTES = 12_000_000


def find_brave():
    for p in BRAVE_PATHS:
        p = os.path.expandvars(p)
        if os.path.exists(p):
            return p
    return None


def profile_dir():
    return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "RokuListen", "web-profile")


def port_file(profile):
    return os.path.join(profile, "DevToolsActivePort")


def launch_args(profile, url="about:blank"):
    """Brave with no window at all (--headless=new). The app draws the page itself from DevTools screen frames, so
    nothing ever opens on your desktop or takes focus from a game, and there's no window to embed."""
    return [f"--user-data-dir={profile}", "--remote-debugging-port=0", "--no-first-run", "--no-default-browser-check",
            "--headless=new", "--window-size=1280,800", "--hide-scrollbars",
            "--disable-blink-features=AutomationControlled",   # no navigator.webdriver flag: sites refuse "bots"
            "--disable-renderer-backgrounding", "--disable-background-timer-throttling", url]


def _version_key(name):
    return tuple(int(x) for x in re.findall(r"\d+", name))


def _size(path):
    return sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(path) for f in fs)


def has_filter_lists(profile):
    """True if the profile holds at least one downloaded filter list."""
    try:
        for comp in os.listdir(profile):
            base = os.path.join(profile, comp)
            if re.fullmatch(r"[a-p]{32}", comp) and os.path.isdir(base):
                for ver in os.listdir(base):
                    if os.path.exists(os.path.join(base, ver, "list.txt")):
                        return True
    except OSError:
        pass
    return False


def seed_filters(profile, log=lambda m: None, source=SOURCE):
    """Copy Brave's downloaded filter components from the normal profile. Call before launching (not while the
    Web profile's browser is running). Returns True if the profile ends up with filter lists."""
    os.makedirs(profile, exist_ok=True)
    copied = 0
    try:
        comps = [c for c in os.listdir(source) if re.fullmatch(r"[a-p]{32}", c) and c not in SKIP]
    except OSError:
        comps = []
    for comp in comps:
        src_comp = os.path.join(source, comp)
        try:
            versions = [v for v in os.listdir(src_comp) if os.path.isdir(os.path.join(src_comp, v))]
            if not versions:
                continue
            newest = max(versions, key=_version_key)
            src_ver, dst_comp = os.path.join(src_comp, newest), os.path.join(profile, comp)
            dst_ver = os.path.join(dst_comp, newest)
            if os.path.isdir(dst_ver) or _size(src_ver) > MAX_COMPONENT_BYTES:
                continue
            for old in (os.listdir(dst_comp) if os.path.isdir(dst_comp) else []):
                shutil.rmtree(os.path.join(dst_comp, old), ignore_errors=True)
            shutil.copytree(src_ver, dst_ver)
            copied += 1
        except OSError as e:
            log(f"filter seed: {comp} skipped ({e!r})")
    ok = has_filter_lists(profile)
    log(f"filter seed: {copied} component(s) updated, lists present: {ok}")
    return ok
