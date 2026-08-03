# Antigravity Watch — live agent status on a Bangle.js 2

Your watch shows what the Antigravity agent is doing, live:

| state | screen | buzz |
|---|---|---|
| IDLE | dark | — |
| WORKING | blue | tiny tick |
| NEEDS YOU | orange (approval pending) | double buzz |
| NEEDS ATTENTION | yellow (the agent asked you a question — shown on screen) | triple buzz |
| DONE | green (+ optional summary) | long buzz |

With `--summarize`, a Gemini flash-lite call turns the agent's final
response into a one-line plain-text summary that appears on the green
screen a second after the buzz. Off by default — without it (and without
any API key) DONE is just the buzz + green screen. Key lookup:
`--gemini-key` → `$GEMINI_API_KEY` → `tools/gemini.key` (gitignored).

While the app is open the screen stays on and unlocked (demo mode).
The physical button exits to the launcher; the app is installed there as
**Antigravity** (icon: the Antigravity glyph as a 256-byte 1-bit transparent
image in `watchapp/agentwatch.img` — drawn in the launcher's theme color),
and re-opening it re-syncs within ~10s.

## Quickstart

One command, first time and every time (watch on, in range, not connected
to anything else):

```bash
python3 agentwatch.py
```

On a fresh clone this does everything automatically: compiles the BLE bridge (needs
Xcode command line tools), starts the connection daemon, **installs the
watch app over the air if it isn't already there** (or is outdated), then
starts pushing agent states. The only manual step is clicking Allow on
macOS's one-time Bluetooth permission prompt for "BangleBridge".

No Python dependencies, no npm, no Web IDE, no Chrome, no App Loader.

Manual controls: `set idle|working|waiting|done`, `test`, `install-app`,
`--dry-run`. Tuning: `--settle 3` (quiet seconds before DONE), `--name
Bangle.js` (BLE name prefix).

## How it fits together

```
Antigravity IDE
  └─ writes  ~/.gemini/antigravity*/brain/<conv>/.../transcript.jsonl
       ▲ tailed every 0.5s
agentwatch.py  (state machine: idle/working/waiting/done)
  └─ writes one-line JS commands into  tools/bridge.cmd  (named pipe)
BangleBridge.app  (daemon: persistent BLE connection, auto-reconnect)
  └─ forwards bytes to the watch's Nordic UART console
Bangle.js 2
  └─ watchapp/agentwatch.app.js defines _AW(state, msg) → draw + buzz
```

Three pieces, all in this repo:

1. **`agentwatch.py`** — Python, stdlib only. Tails the transcripts, derives
   the state, pushes transitions immediately (plus a silent re-sync every
   10s so the watch converges after you exit/re-open the app).
2. **`tools/BangleBridge.app`** — ~200-line Swift CoreBluetooth CLI in a
   minimal app bundle. Holds the BLE connection open so updates are instant.
   It exists because macOS kills Bluetooth use by processes whose host app
   lacks the entitlement (TCC): a plain script run from a terminal dies with
   exit 134. Bundled as its own app and launched via `open`, it gets its own
   one-time permission prompt and then works from anywhere. `setup` compiles
   it if missing (needs Xcode command line tools).
3. **`watchapp/agentwatch.app.js`** — the watch app. Installed **over the
   air by our own bridge** (`install-app` streams it into the watch's
   storage) — no Web IDE, no App Loader, no Chrome needed.

## FAQ

**How do Bangle.js app installs normally work?** There's no app-store
binary format: the official [App Loader](https://banglejs.com/apps) is a
Web Bluetooth page that streams plain files (`<id>.app.js`, `<id>.info`,
an icon) into the watch's storage over the BLE UART console, then the
launcher lists every `*.info` it finds. Our installer speaks exactly that
protocol from Python — the result on the watch is indistinguishable from
an App Loader install.

**Is it a real installed app on the watch?** Yes — it lives in the watch's
storage with a launcher entry, like any app from the official App Loader.
It just gets there via our bridge instead of the browser. Installs are
verified: each line is acknowledged by the watch when the return channel
works, and when it doesn't, the watch broadcasts a verification beacon in
its Bluetooth advertising name that the daemon reads from the scan.

**Would someone else need our custom bridge?** To *install/run the demo on
their own Mac*: yes — they clone this repo and run `setup`; the bridge is
part of the package and builds itself. Nothing else is required (no
GitHub pages, no Chrome). The watch side is plain Espruino JavaScript that
any Bangle.js 2 accepts. If someone only wants the watch app without our
Mac pipeline, they could paste `watchapp/agentwatch.app.js` into the
Espruino Web IDE instead — but without the bridge + watcher nothing will
drive it.

**Why did the Web Bluetooth pages (App Loader / demo links) fail?** That's
Chrome missing macOS Bluetooth permission — System Settings → Privacy &
Security → Bluetooth → enable your browser. Unrelated to this pipeline,
which never touches the browser.

**Battery?** Always-on screen + persistent BLE costs real battery; that's
the demo trade-off. Exiting the app restores normal lock/timeout behavior.

## Caveats

- The watch holds one BLE connection: while the daemon is connected, the
  Web IDE / App Loader can't see the watch. `pkill -f banglebridge` frees it.
- Questions and approvals are detected from Antigravity's live
  per-conversation sqlite DBs (`~/.gemini/antigravity*/conversations/*.db`,
  step status 9 = awaiting user; type 138 = question, whose text is shown
  on the watch). The transcripts alone can't see them — pending steps are
  only appended there after they resolve.
- Antigravity's transcript format is internal and may change.

## Provenance

Scoped-down slice of the original weekend plan (watch → approve/reject/start
agents). This is the one-way half: agent state → wrist. The two-way approve
path (sideloaded extension firing internal `antigravity.*` commands) is the
natural next step.
