# Contributing to CapoCast

Thanks for taking the time to help! Bug reports, fixes and ideas are all welcome.

## Ground rules

- Be kind and constructive. Assume good intent.
- Keep pull requests focused: one fix or feature per PR.
- By contributing you agree your work is licensed under the project's [GPL-3.0 license](LICENSE).

## Reporting bugs

Open an [issue](../../issues/new/choose) using the **Bug report** template and include:

1. What you did, what you expected, and what happened.
2. Your setup: Windows version, Roku model and OS version (*Settings → System → About*), Playlet version, Chrome version.
3. The relevant part of the app log: `%APPDATA%\RokuListen.log`. Please remove anything you consider private.

Please check existing issues first, and search closed ones too.

## Suggesting features

Open an issue with the **Feature request** template. Describe the problem you want solved before the solution you have in mind.

## Development setup

**Prerequisites:** Windows 10/11, Python 3.11+, [FFmpeg](https://ffmpeg.org/) on your `PATH`, Google Chrome, and a Roku with [Playlet](https://github.com/iBicha/playlet) installed. For the Web tab you also need [Brave](https://brave.com/download/) (opened once, so it has downloaded its ad filters) and the RokuCast channel sideloaded on the Roku.

```powershell
git clone https://github.com/6wen66/CapoCast.git
cd CapoCast
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python roku_listen_gui.py
```

Load the extension from the `extension/` folder (`chrome://extensions` → **Developer mode** → **Load unpacked**).

To build the standalone `.exe`:

```powershell
.\build.ps1 -Clean      # -> dist\CapoCast.exe and dist\playlet-cast-extension.zip
```

## Project layout

| Path | What it does |
|------|--------------|
| `roku_listen_gui.py` | The desktop app: UI, private-listening audio, Roku remote, auto-cast sync engine |
| `roku_listen.py` | Protocol helpers + a minimal command-line client (plays through `ffplay`) |
| `yt_embed.py` | Launches Chrome in app mode and embeds its window in the YouTube tab |
| `yt_bridge.py` | Local HTTP bridge (`127.0.0.1:8765`) between the app and the extension |
| `web_tab.py` | The Web tab: toolbar, headless Brave lifecycle, and the Cast flow |
| `web_view.py` | Draws the headless browser's page on a canvas and forwards mouse / keyboard (key translation is pure and unit tested) |
| `web_sync.py` | Keeps the TV following the page's video after a Web cast (pause, play, scrub). Pure logic over injected TV / relay / guard objects, so it's unit tested with fakes |
| `web_guard.py` | Drives Brave over DevTools: pop-up / redirect / click-trap guards, stream capture, screencast, input, and the page's play / pause / seek events |
| `stream_finder.py` | Chooses the real video among decoys. Pure logic, no I/O |
| `media_relay.py` | LAN server that relays a stream to the Roku with the site's headers and rewrites HLS / DASH manifests |
| `brave_profile.py` | The Web tab's private Brave profile and its launch flags; copies Brave's ad filters into it |
| `drm.py` | DRM refusal (by site and by HLS key). Keep it: the project doesn't cast DRM content |
| `tests/` | Unit tests, the hostile test site (`decoy_site.py`) and three end-to-end checks (see below) |
| `extension/` | Chrome extension: Playlet cast button and the auto-cast content script (`feed.js`) |
| `assets/` | Bundled fonts and icons |

## Coding guidelines

- Match the style of the surrounding code: standard library first, small focused functions, and comments that explain *why* rather than *what*.
- Keep the UI on the Tk thread. Do network and Roku I/O on worker threads, then hand results back with `after()`.
- Never block the bridge's request handlers. Push work onto a queue, the way `CastSync` does.
- Don't commit build output, logs, or personal data such as your Roku's IP or account info. `.gitignore` covers the usual suspects.

## Testing your change

Run the unit tests (no Roku needed):

```powershell
python -m unittest discover -s tests -v
```

If you touched the Web tab, also run the end-to-end checks. They need Brave and FFmpeg, run Brave headless (nothing appears on screen and nothing takes focus, so they're safe to run while you game) and print PASS/FAIL per check:

```powershell
python tests\guard_smoke.py       # ~45 s: protection, frames, typing/scroll/alerts, decoy filter, against a hostile site
python tests\web_tab_smoke.py     # ~50 s: the real Web tab and Cast button against a fake Roku on localhost:8060
python tests\formats_smoke.py     # ~2 min: HLS, HLS with a Referer check, disguised HLS, DASH, DASH with absolute URLs
```

`web_tab_smoke.py` needs port 8060 free (close CapoCast first) and `formats_smoke.py` loads hls.js and dash.js from a CDN. To try the hostile site by hand, run `python tests\decoy_site.py` and open the printed URL in the Web tab.

The UI and the Roku-facing code have no automated tests, so before opening a PR please also check by hand:

- [ ] **Listen tab:** Connect plays TV audio for 30 s or more, volume works, and Disconnect restores the TV speakers.
- [ ] **Transport:** play/pause, ±10 s and rewind/fast-forward act on the Roku.
- [ ] **YouTube tab:** Chrome embeds, resizes with the window, and closes with the app.
- [ ] **Auto-cast:** picking a video plays it on the TV; pause, scrub, and going back to Home all mirror correctly.
- [ ] **Web tab:** Brave embeds and resizes with the window; on a real video page Cast plays on the TV (check the Windows Firewall prompt on a first run); pause and ±10 s work from the Listen tab; going Web → YouTube → Web keeps both working.
- [ ] No errors in `%APPDATA%\RokuListen.log`.

## Submitting a pull request

1. Fork the repo and create a branch from `main`: `git checkout -b fix/short-description`.
2. Make your change, and update `README.md` if behavior or setup changed.
3. Commit with a clear message, for example `Fix seek echo after auto-cast nudge`.
4. Push and open a PR. Fill in the template: what changed, why, and how you tested it.
5. A maintainer will review it. Small follow-up commits are fine; history is squashed on merge.
