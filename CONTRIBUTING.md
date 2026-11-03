# Contributing to Roku Listen

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

**Prerequisites:** Windows 10/11, Python 3.11+, [FFmpeg](https://ffmpeg.org/) on your `PATH`, Google Chrome, and a Roku with [Playlet](https://github.com/iBicha/playlet) installed.

```powershell
git clone https://github.com/6wen66/roku-listen.git
cd roku-listen
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python roku_listen_gui.py
```

Load the extension from the `extension/` folder (`chrome://extensions` → **Developer mode** → **Load unpacked**).

To build the standalone `.exe`:

```powershell
.\build.ps1 -Clean      # -> dist\RokuListen.exe and dist\playlet-cast-extension.zip
```

## Project layout

| Path | What it does |
|------|--------------|
| `roku_listen_gui.py` | The desktop app: UI, private-listening audio, Roku remote, auto-cast sync engine |
| `roku_listen.py` | Protocol helpers + a minimal command-line client (plays through `ffplay`) |
| `yt_embed.py` | Launches Chrome in app mode and embeds its window in the YouTube tab |
| `yt_bridge.py` | Local HTTP bridge (`127.0.0.1:8765`) between the app and the extension |
| `extension/` | Chrome extension: Playlet cast button and the auto-cast content script (`feed.js`) |
| `assets/` | Bundled fonts and icons |

## Coding guidelines

- Match the style of the surrounding code: standard library first, small focused functions, and comments that explain *why* rather than *what*.
- Keep the UI on the Tk thread. Do network and Roku I/O on worker threads, then hand results back with `after()`.
- Never block the bridge's request handlers. Push work onto a queue, the way `CastSync` does.
- Don't commit build output, logs, or personal data such as your Roku's IP or account info. `.gitignore` covers the usual suspects.

## Testing your change

There is no automated test suite yet (contributions welcome!). Before opening a PR, please check by hand:

- [ ] **Listen tab:** Connect plays TV audio for 30 s or more, volume works, and Disconnect restores the TV speakers.
- [ ] **Transport:** play/pause, ±10 s and rewind/fast-forward act on the Roku.
- [ ] **YouTube tab:** Chrome embeds, resizes with the window, and closes with the app.
- [ ] **Auto-cast:** picking a video plays it on the TV; pause, scrub, and going back to Home all mirror correctly.
- [ ] No errors in `%APPDATA%\RokuListen.log`.

## Submitting a pull request

1. Fork the repo and create a branch from `main`: `git checkout -b fix/short-description`.
2. Make your change, and update `README.md` if behavior or setup changed.
3. Commit with a clear message, for example `Fix seek echo after auto-cast nudge`.
4. Push and open a PR. Fill in the template: what changed, why, and how you tested it.
5. A maintainer will review it. Small follow-up commits are fine; history is squashed on merge.
