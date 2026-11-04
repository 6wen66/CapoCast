"""DRM refusal for the Web tab.

CapoCast won't cast DRM-protected services and contains no code that tries to get around the protection.
There are two gates: well-known DRM sites are refused by name, and any HLS playlist that declares a
Widevine / PlayReady / FairPlay key is refused when its playlist is read (media_relay.rewrite_hls).
"""
from urllib.parse import urlparse

DRM_HOSTS = {
    "netflix.com", "disneyplus.com", "disney.com", "starplus.com", "hotstar.com", "hulu.com",
    "max.com", "hbomax.com", "primevideo.com", "amazon.com", "amazon.co.uk", "amazon.de",
    "peacocktv.com", "paramountplus.com", "tv.apple.com", "appletv.com", "crunchyroll.com",
    "funimation.com", "britbox.com", "discoveryplus.com", "showtime.com", "starz.com", "mubi.com",
    "criterionchannel.com", "shudder.com", "spotify.com", "audible.com",
}

MESSAGE = ("{host} protects its video with DRM.\n\nCapoCast can't cast it and won't try to get around "
           "the protection. Use that service's own Roku channel instead.")

# KEYFORMAT values in an HLS key line that mean "this needs a DRM license", not a plain AES key
DRM_KEYFORMATS = ("widevine", "playready", "fairplay", "streamingkeydelivery", "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed")


class DrmError(Exception):
    """The stream is DRM-protected."""


def _suffixes(host):
    host = (host or "").lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    return [".".join(parts[i:]) for i in range(len(parts))]


def is_drm_host(url_or_host):
    host = urlparse(url_or_host).hostname or "" if "://" in url_or_host else url_or_host
    return any(s in DRM_HOSTS for s in _suffixes(host))


def refusal_for(url):
    """A user-facing message if this page must be refused, else None."""
    if not url or not is_drm_host(url):
        return None
    host = urlparse(url).hostname or url
    return MESSAGE.format(host=host[4:] if host.startswith("www.") else host)


def key_line_is_drm(line):
    """True if an #EXT-X-KEY / #EXT-X-SESSION-KEY line asks for a DRM license."""
    low = line.lower()
    if "method=none" in low:
        return False
    return any(f in low for f in DRM_KEYFORMATS)
