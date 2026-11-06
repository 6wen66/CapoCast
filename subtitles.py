"""Subtitles for a Web cast: find the page's separate subtitle tracks, fetch the one you picked to this PC, and turn it
into a styled ASS file that the encoder burns into the picture (see upscaler.command).

The RokuCast channel on the TV can't show a separate subtitle file, so the PC draws the words onto the video itself and
the TV just plays the result. Subtitles that are already part of the picture need none of this.

Pure helpers (finding, labelling, picking, restyling) are separate from the one place that runs FFmpeg (to_ass), so they
can be tested without a network.
"""
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request

SUB_EXT = (".vtt", ".webvtt", ".srt", ".ass", ".ssa")
SUB_MIMES = {"text/vtt", "application/x-subrip", "text/x-ssa", "text/x-ass", "application/x-ass", "text/srt"}
NOT_SUBS = ("thumb", "sprite", "storyboard", "preview", "poster", "tiles", "chapter")   # .vtt files that index pictures
KINDS_OK = ("", "subtitles", "captions")                                              # not chapters, metadata, audio description
MAX_TRACKS = 12
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

LANGUAGES = {
    "en": "English", "eng": "English", "english": "English", "es": "Spanish", "spa": "Spanish", "spanish": "Spanish",
    "espanol": "Spanish", "fr": "French", "fra": "French", "fre": "French", "french": "French", "de": "German",
    "ger": "German", "deu": "German", "german": "German", "pt": "Portuguese", "por": "Portuguese",
    "portuguese": "Portuguese", "ptbr": "Portuguese", "it": "Italian", "ita": "Italian", "italian": "Italian",
    "ru": "Russian", "rus": "Russian", "russian": "Russian", "ja": "Japanese", "jpn": "Japanese", "jap": "Japanese",
    "japanese": "Japanese", "ko": "Korean", "kor": "Korean", "korean": "Korean", "zh": "Chinese", "chi": "Chinese",
    "zho": "Chinese", "chinese": "Chinese", "ar": "Arabic", "ara": "Arabic", "arabic": "Arabic", "hi": "Hindi",
    "hin": "Hindi", "hindi": "Hindi", "id": "Indonesian", "ind": "Indonesian", "indonesian": "Indonesian",
    "vi": "Vietnamese", "vie": "Vietnamese", "vietnamese": "Vietnamese", "th": "Thai", "tha": "Thai", "thai": "Thai",
    "tr": "Turkish", "tur": "Turkish", "turkish": "Turkish", "pl": "Polish", "pol": "Polish", "polish": "Polish",
    "nl": "Dutch", "nld": "Dutch", "dut": "Dutch", "dutch": "Dutch",
}


def is_subtitle_url(url, mime=""):
    """A separate subtitle file (WebVTT, SRT or ASS), going by its name and content type. Files that index thumbnails
    are also .vtt, so those are told apart by name."""
    path = urllib.parse.urlparse(url).path.lower()
    if any(w in path for w in NOT_SUBS):
        return False
    return path.endswith(SUB_EXT) or str(mime).split(";")[0].strip().lower() in SUB_MIMES


def is_native_ass(url):
    return urllib.parse.urlparse(url).path.lower().endswith((".ass", ".ssa"))


def language_of(text, strict=False):
    """'English' for 'en', 'eng', 'English'... or '' if no language is named. strict is for text that wasn't written to
    be a label (an address): there, two-letter codes only count when they're the whole file name ('en.vtt'), since
    'id' and 'it' turn up in all kinds of paths."""
    text = str(text or "").lower()
    if strict:
        stem = os.path.splitext(urllib.parse.urlparse(text).path.rsplit("/", 1)[-1])[0]
        if stem in LANGUAGES:
            return LANGUAGES[stem]
        tokens = [t for t in re.split(r"[^a-z]+", urllib.parse.urlparse(text).path) if len(t) >= 3]
    else:
        tokens = [t for t in re.split(r"[^a-z]+", text) if t]
    for t in tokens:
        if t in LANGUAGES:
            return LANGUAGES[t]
    return ""


def hls_renditions(text, base_url):
    """The subtitle tracks listed in an HLS master playlist: [{'url', 'label', 'lang'}]."""
    out = []
    for line in str(text or "").splitlines():
        if not line.startswith("#EXT-X-MEDIA:"):
            continue
        attrs = {k.upper(): v.strip('"') for k, v in re.findall(r'([A-Za-z0-9-]+)=("[^"]*"|[^,]*)', line[13:])}
        if attrs.get("TYPE") != "SUBTITLES" or not attrs.get("URI"):
            continue
        name = attrs.get("NAME", "")
        out.append({"url": urllib.parse.urljoin(base_url, attrs["URI"]), "label": name,
                    "lang": language_of(attrs.get("LANGUAGE", "")) or language_of(name)})
    return out


def fetch_renditions(url, headers, timeout=5.0, opener=None):
    """Subtitle tracks named inside the stream's own master playlist, [] if it isn't one or can't be read."""
    if "m3u8" not in url.lower():
        return []
    try:
        req = urllib.request.Request(url, headers=dict(headers or {}))
        with (opener or urllib.request.urlopen)(req, timeout=timeout) as r:
            text = r.read(400_000).decode("utf-8", errors="replace")
    except Exception:
        return []
    return hls_renditions(text, url) if "#EXT-X-MEDIA" in text else []


def build_tracks(files, dom_tracks=(), renditions=(), fallback_headers=None):
    """One list of what the page offers: [{'url', 'headers', 'label', 'lang'}], best guess first.

    files: subtitle files the browser was seen fetching, [{'url', 'headers'}]. dom_tracks: the page's own <track>
    elements, [{'src', 'label', 'lang', 'kind', 'def'}], which supply names and the ones the page marks default.
    renditions: from the stream's master playlist."""
    named = {}
    for t in dom_tracks or ():
        if str(t.get("kind", "subtitles")).lower() in KINDS_OK and str(t.get("src", "")).startswith(("http://", "https://")):
            named[t["src"]] = t
    tracks, seen = [], set()

    def add(url, headers, label, lang, default=False):
        if url in seen or len(tracks) >= MAX_TRACKS:
            return
        seen.add(url)
        lang = lang or language_of(label) or language_of(url, strict=True)
        tracks.append({"url": url, "headers": dict(headers or fallback_headers or {}), "label": label or lang or "Subtitles",
                       "lang": lang, "default": default})

    for f in files or ():
        t = named.get(f["url"], {})
        add(f["url"], f.get("headers"), (t.get("label") or "").strip(), language_of(t.get("lang", "")), bool(t.get("def")))
    for url, t in named.items():
        add(url, None, (t.get("label") or "").strip(), language_of(t.get("lang", "")), bool(t.get("def")))
    for r in renditions or ():
        add(r["url"], fallback_headers, r.get("label", ""), r.get("lang", ""))
    tracks.sort(key=lambda t: not t["default"])         # stable: the page's own default first
    counts = {}
    for t in tracks:                                    # two files with one name get told apart
        counts[t["label"]] = counts.get(t["label"], 0) + 1
        if counts[t["label"]] > 1:
            t["label"] = f"{t['label']} {counts[t['label']]}"
    return tracks


def pick_track(tracks, want):
    """The track `want` names ('english', or a label like 'English 2'), or None. want is what the user chose last time,
    stored lower-case; it matches a label first and a language second."""
    want = str(want or "").strip().lower()
    if not want:
        return None
    for t in tracks:
        if t["label"].lower() == want:
            return t
    for t in tracks:
        if t["lang"].lower() == want:
            return t
    return None


# ASS wants sizes in the script's own units (ffmpeg writes PlayResY 288), so 19 is about 6.6% of the picture's height:
# big enough for a TV across the room, small enough to leave the picture alone.
STYLE = "Style: Default,Roboto,19,&Hffffff,&Hffffff,&H000000,&H80000000,1,0,0,0,100,100,0,0,1,2,1,2,12,12,16,1"


def restyle(ass_text):
    """Make FFmpeg's plain default look (small thin Arial) readable on a TV: bold Roboto, a dark outline and shadow."""
    return re.sub(r"^Style: Default,.*$", STYLE, ass_text, count=1, flags=re.M)


def has_dialogue(ass_text):
    return bool(re.search(r"^Dialogue:", ass_text, flags=re.M))


def to_ass(track, out_path, log=lambda m: None, timeout=90, run=subprocess.run, exe=None):
    """Fetch `track` the way the browser did and write it to out_path as ASS. True on success. FFmpeg does the fetching
    so that WebVTT, SRT, ASS and HLS subtitle playlists all come out the same way."""
    exe = exe or shutil.which("ffmpeg")
    if not exe:
        log("subtitles: ffmpeg not found")
        return False
    from upscaler import _input_options
    url = track["url"]
    hls = ".m3u8" in url.lower()
    cmd = [exe, "-nostdin", "-y", "-hide_banner", "-loglevel", "error", *_input_options(track.get("headers") or {}, "hls" if hls else ""),
           "-i", url, "-map", "0:s:0", "-c:s", "ass", out_path]
    try:
        done = run(cmd, capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
        if getattr(done, "returncode", 1) != 0:
            log(f"subtitles: ffmpeg refused {url[:90]}: {done.stderr.decode(errors='replace').strip()[-160:]}")
            return False
        with open(out_path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception as e:
        log(f"subtitles: couldn't fetch {url[:90]}: {e!r}")
        return False
    if not has_dialogue(text):
        log(f"subtitles: nothing readable in {url[:90]}")
        return False
    if not is_native_ass(url):
        text = restyle(text)                    # a fansub's own .ass keeps its styling
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)
    return True
