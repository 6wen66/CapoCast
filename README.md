<div align="center">

<img src="assets/youtube.png" width="72" alt="">

# Roku Listen

**Your Roku's audio on your PC, and a YouTube tab that plays everything you pick on your TV.**

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
![Platform: Windows](https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-0078D6)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB)

<img src="docs/screenshots/youtube-tab.png" width="820" alt="Roku Listen YouTube tab">

</div>

---

## Why

- **Private listening without your phone.** Roku's "private listening" normally streams TV audio to the Roku mobile app. Roku Listen speaks the same protocol, so the sound plays straight from your PC: no phone, no Bluetooth relay, and much lower latency.
- **YouTube on the TV, driven from your PC.** The YouTube tab is your real Chrome, with your login and all your extensions, embedded in the app. Pick a video and it plays on the TV. Pause, scrub, or switch videos, and the TV follows.

> [!IMPORTANT]
> **[Playlet](https://github.com/iBicha/playlet) must be installed on your Roku** for the YouTube tab, auto-cast and ±10 s seeking. Playlet is a free, open-source YouTube client for Roku. Install it from the [Roku Channel Store](https://channelstore.roku.com/details/4a41d0921265a5e31429a7679442153f:b5bcb5b630c28b01e93bf59856317b43/playlet), or search for **Playlet** on your Roku, and keep it open on the TV while you use the YouTube tab.

## Why Playlet? A free, ad-free YouTube for your TV

Roku Listen plays everything through **Playlet**, a free and open-source YouTube client for Roku. You get what people usually pay a subscription for, plus a few things no official app offers:

- 🔥 **No ads, ever.** No pre-rolls and no unskippable mid-rolls. In the developer's words: *"Not now, not ever."*
- ⚔️ **[SponsorBlock](https://sponsor.ajay.app/) built in.** Automatically skips sponsor reads, intros, "like and subscribe" reminders and other filler, using the community's crowd-sourced segments.
- 🕵️ **No tracking, privacy-first.** Optionally route everything through an [Invidious](https://invidious.io/) backend so you never talk to Google directly.
- 🙋 **Your accounts still work.** Sign in with your YouTube account (or Invidious) to keep your subscriptions and recommendations.
- ⚡ **Fast.** Without the bloat it's quicker than the official Roku app ([see the comparison](https://youtu.be/qmSvA-QQW20)).
- 🔮 **Picture-in-picture** on the TV.
- 📱 **Cast from anywhere:** your phone's YouTube app, a browser, a TV link code, and now your PC through Roku Listen.
- 💸 **Free and open source.** No subscription, no paywall, and the code is public ([AGPL-3.0](https://github.com/iBicha/playlet/blob/main/LICENSE)).

| | Official YouTube app | YouTube Premium | **Playlet + Roku Listen** |
|---|:---:|:---:|:---:|
| Price | Free | Monthly subscription | **Free** |
| Ads | Yes | None | **None** |
| SponsorBlock (skip sponsors and filler) | ✗ | ✗ | **✓** |
| Privacy (no tracking / Invidious option) | ✗ | ✗ | **✓** |
| Use your YouTube account | ✓ | ✓ | **✓** |
| Open source | ✗ | ✗ | **✓** |
| Browse on your PC with your Chrome extensions, play on the TV | ✗ | ✗ | **✓** |
| Hear the TV through your PC (private listening) | Phone app only | Phone app only | **✓** |

> [!NOTE]
> Playlet is an independent project by [iBicha](https://github.com/iBicha/playlet), and Roku Listen isn't affiliated with it. Feature claims above come from [Playlet's own README](https://github.com/iBicha/playlet#features). Like any Roku channel, Roku collects some usage data; Playlet's own telemetry can be turned off in its settings. If you want to support the creators you watch, channel memberships, Patreon and merch go straight to them.

## Features

| | |
|---|---|
| 🎧 **TV audio on your PC** | Roku private listening over Wi-Fi, with a live volume slider (0–200 %) and adjustable buffer delay |
| ⏯️ **Remote control** | Roku-style arrows and OK (hold to repeat), Back, play/pause, rewind, fast-forward, and exact ±10 s seeks, with live "now playing" status |
| ⌨️ **Type on TV** | Type into the TV's search and sign-in boxes from your PC keyboard (Ctrl+V pastes); arrows, Enter and Esc work the menus. Like the phone app, it pops up by itself when a keyboard opens on the TV |
| ▶️ **Embedded YouTube** | Your own Chrome profile (login, extensions, dark mode) inside the app window |
| 📺 **Auto-cast sync** | Play, pause, scrub, autoplay-next and Shorts mirror to the TV; going back to Home sends Playlet back to its home screen |
| 🎯 **Latency compensation** | Learns how long Playlet takes to rebuffer and aims seeks ahead, then keeps the PC player locked to the TV's timeline |
| 🧩 **Playlet Cast extension** | One-click "cast this tab" from normal Chrome too (Alt+Shift+C, or right-click a link → *Cast link to Playlet*) |

<img src="docs/screenshots/listen-tab.png" width="640" alt="Roku Listen Listen tab">

## Prerequisites

- **Windows 10 or 11**
- **A Roku TV or player that supports private listening.** If the official Roku mobile app can do private listening on your device, Roku Listen can too.
- **[Playlet](https://github.com/iBicha/playlet) installed on the Roku**, for YouTube and auto-cast. See above.
- **Google Chrome**, signed in to YouTube if you want your own feed.
- **[FFmpeg](https://ffmpeg.org/download.html) on your `PATH`.** It decodes the audio stream. The easiest install is `winget install Gyan.FFmpeg`.
- The PC and the Roku on the **same network**.
- *Only when running from source:* Python 3.11 or newer.

## Installation

### Option A: download (recommended)

1. Grab **`RokuListen.exe`** and **`playlet-cast-extension.zip`** from the [latest release](../../releases/latest).
2. Unzip the extension somewhere permanent, for example `Documents\playlet-cast`.
3. In Chrome, open `chrome://extensions`, turn on **Developer mode**, click **Load unpacked**, and select that folder.
4. Open the extension's **Options** and enter your Roku's IP address. You can find it on the Roku under *Settings → Network → About*.
5. Run `RokuListen.exe`. If Windows Firewall asks, allow it on **private networks**; the audio arrives over UDP.

### Option B: from source

```powershell
git clone https://github.com/6wen66/roku-listen.git
cd roku-listen
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python roku_listen_gui.py
```

Then load the `extension/` folder in Chrome as in steps 3–4 above. To build your own `.exe`, run `.\build.ps1 -Clean`.

## Usage

### Listen tab

1. Enter your Roku's IP (it's saved for next time) and click **Connect**. The TV's speakers mute and the audio plays on your PC.
2. Adjust **Volume** live. Everything above 100 % is a boost.
3. **Delay ms** sets the Roku-side buffer. Lower it (about 200) for tighter lip-sync, or raise it if you hear crackles.
4. The transport buttons control whatever is playing on the Roku. The first one is the remote's **Back** button. The **Remote** column beside them has the remote's arrows and **OK** as square buttons. Holding an arrow repeats it.
5. **Type on TV:** click the box, then type. Letters go into whatever search or sign-in box is open on the TV, and **Ctrl+V** pastes, which is handy for long passwords. While the box is selected, the arrow keys move around the TV's menus, **Enter** is OK, **Esc** is Back and **Backspace** deletes. Click anywhere else in the app to stop sending keys.
6. **Keyboard pop-up:** when a search or sign-in keyboard opens on the TV, the app switches to the Listen tab, selects the Type box (outlined in green) and comes to the front. If another app such as a game is in the foreground, Windows flashes the taskbar button instead. The box shows the TV's own text, with passwords as dots. When the TV's keyboard closes, the app goes back to the tab you were on. You can also start typing anywhere on the Listen tab without clicking the box first.

### YouTube tab

1. Open Playlet on the TV.
2. Switch to the **YouTube** tab. Chrome opens inside the app.
3. With **Auto-cast** on (top-right switch):

| You do this on the PC | The TV does this |
|---|---|
| Click any video (or a hover preview, autoplay, a Short) | Plays it through Playlet. The PC copy is muted. |
| Pause / play | Pauses / plays |
| Scrub the timeline | Seeks, aimed ahead to cover Playlet's rebuffer |
| Go back to Home, search or a channel | Closes the player and returns to Playlet's home screen |

The status next to the switch shows the link state: **Ready**, **On TV · \<title\>**, **Connecting…**, or a hint if something's wrong. Turning Auto-cast off unmutes the PC and leaves the TV alone.

### Command-line client

For a minimal, audio-only setup without the GUI:

```powershell
python roku_listen.py --host 192.168.1.50 --delay-ms 300
```

## How it works

```
 ┌───────────── Roku Listen (Python / Tk) ──────────────┐        ┌────────── Roku ──────────┐
 │  Listen tab ── ECP WebSocket :8060 (auth + setup) ───┼───────▶│  private listening       │
 │      ▲         RTP/Opus audio ◀── UDP :6970 ─────────┼────────│  (audio stream)          │
 │      └ ffmpeg → PCM → volume → speakers              │        │                          │
 │                                                      │        │                          │
 │  YouTube tab ── embedded Chrome (your profile)       │        │                          │
 │      └ Playlet Cast extension (feed.js)              │        │                          │
 │            │ play / pause / seek / leave             │        │                          │
 │            ▼                                         │        │                          │
 │  Bridge 127.0.0.1:8765 → CastSync ── HTTP :8888 ─────┼───────▶│  Playlet (YouTube)       │
 │                         └─ ECP :8060 (state, keys) ──┼───────▶│                          │
 └──────────────────────────────────────────────────────┘        └──────────────────────────┘
```

- **Audio:** the app authenticates to the Roku's ECP WebSocket the way the official mobile app does, then asks for the audio stream to be sent to the PC as RTP/Opus. It keeps the stream alive with RTCP receiver reports. FFmpeg decodes it to PCM, and the app applies volume and plays it.
- **YouTube:** Chrome runs in app mode with your own profile and is re-parented into the app window. The extension watches the main YouTube player (never hover previews or ads) and reports events to the app over a local bridge that only the extension can use. The app mirrors those events to Playlet. Playlet seeks by reloading at a timestamp, which costs a 2–3 s rebuffer, so seeks are aimed ahead by a learned amount, and the muted PC player is then nudged onto the TV's timeline.

## Project structure

```
roku-listen/
├── roku_listen_gui.py     # desktop app: UI, audio, remote control, auto-cast sync
├── roku_listen.py         # protocol helpers + command-line client
├── yt_embed.py            # launches and embeds Chrome in the YouTube tab
├── yt_bridge.py           # local bridge between the app and the extension
├── extension/             # "Playlet Cast" Chrome extension (Manifest V3)
│   ├── manifest.json
│   ├── background.js      # cast button, bridge relay, injection into open tabs
│   ├── feed.js            # auto-cast: mirrors the YouTube player to the TV
│   └── popup.* / options.*
├── assets/                # Roboto font (Apache 2.0) and icons
├── docs/screenshots/
├── build.ps1              # builds dist\RokuListen.exe + extension zip
└── requirements.txt
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| Status says **Reload Playlet Cast in chrome://extensions** | Check that the extension is loaded and **Developer mode** is on, then click its reload icon. |
| **Port 8765 is busy** | Another copy of Roku Listen is running. Close it. Opening the app again normally just brings the existing window forward. |
| **Can't reach Playlet on the Roku** | Open Playlet on the TV and check the Roku IP. |
| Remote buttons or **Type on TV** do nothing | On the Roku: *Settings → System → Advanced system settings → Control by mobile apps → Network access → Default/Permissive*. |
| Connected but no sound | Allow Roku Listen through Windows Firewall on private networks, and check that FFmpeg is on your `PATH`. |
| Crackling audio | Raise **Delay ms** (try 500–700). |

The app's log lives at `%APPDATA%\RokuListen.log` and its settings at `%APPDATA%\RokuListen.json`.

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Acknowledgements

- [**RPListening**](https://github.com/runz0rd/RPListening), originally by William Seemann, for the reverse-engineered Roku private-listening protocol.
- [**Playlet**](https://github.com/iBicha/playlet) by iBicha, for the open-source YouTube client for Roku and its local web API.
- [**Roboto**](https://fonts.google.com/specimen/Roboto), licensed under Apache 2.0 (see `assets/Roboto-LICENSE.txt`).

## License

[GPL-3.0](LICENSE) © Roku Listen contributors.

*Roku Listen is an independent project. It is not affiliated with, endorsed by, or sponsored by Roku, Inc., Google LLC or YouTube. Roku, YouTube and their logos are trademarks of their respective owners.*
