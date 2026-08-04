# Antigravity Watch

Your Bangle.js 2 shows what your [Antigravity](https://antigravity.google) agent
is doing, live:

| state | screen | buzz |
|---|---|---|
| IDLE | dark | none |
| WORKING | blue | tick |
| NEEDS YOU | orange, shows the command awaiting approval | double |
| NEEDS ATTENTION | yellow, shows the agent's question or plan summary | triple |
| DONE | green, optional one-line summary | long |

## Quickstart

Requirements: macOS, Xcode command line tools, a Bangle.js 2 in range and not
connected to anything else. No Python dependencies.

```bash
git clone https://github.com/MFS-code/antigravity-watch
cd antigravity-watch
python3 agentwatch.py
```

First run compiles the BLE bridge, starts the connection daemon, installs the
watch app over the air, and starts streaming states. Click Allow on the one
macOS Bluetooth permission prompt. Later runs skip whatever is already done.

For a one-line summary of what the agent did on the DONE screen, put a Gemini
API key in `tools/gemini.key` (gitignored) or `$GEMINI_API_KEY` and run:

```bash
python3 agentwatch.py --summarize
```

## Commands and flags

```
python3 agentwatch.py               watch and push states (the default)
python3 agentwatch.py test          buzz the watch to check the link
python3 agentwatch.py set <state>   push idle|working|waiting|attention|done
python3 agentwatch.py install-app   force a watch app (re)install
```

Flags: `--summarize`, `--gemini-key KEY`, `--settle N` (quiet seconds before
DONE, default 1), `--idle-after N` (default 120), `--name PREFIX` (BLE name,
default Bangle.js), `--dry-run` (print instead of sending).

## How it works

```
Antigravity IDE writes state under ~/.gemini/antigravity*/
  transcripts (brain/*/…/transcript.jsonl)  -> working / done
  conversation DBs (conversations/*.db)     -> pending questions & approvals
  artifact metadata (brain/*/*.metadata.json) -> plan reviews
        |
agentwatch.py   polls the above, derives one state, writes a JS one-liner
        |       into a named pipe on every transition
BangleBridge.app  Swift CoreBluetooth daemon; holds a persistent connection
        |         to the watch and forwards bytes to its UART console
Bangle.js 2     watchapp/agentwatch.app.js draws the screen and buzzes
```

Details worth knowing:

- The watch app is a normal Bangle app (storage files plus a launcher entry),
  same as anything from the official [App Loader](https://banglejs.com/apps).
  It gets installed by our bridge over BLE instead of a browser, and installs
  are verified: line-by-line acks from the watch when its return channel
  works, otherwise a CRC beacon broadcast in the watch's advertising name.
- The bridge exists because macOS kills Bluetooth use by unentitled
  processes. As its own signed app bundle it gets one permission prompt and
  then works from any terminal.
- Pending questions and approvals never appear in the transcripts (they are
  only appended after you resolve them); the conversation DBs carry them in
  real time. Step status 9 means awaiting user, type 138 is a question.
- While the watch app is open the screen stays on and unlocked. The button
  exits to the launcher; reopening resyncs within 10 s.

## Caveats

- One BLE connection at a time: while the daemon is connected, the Web IDE
  and App Loader cannot see the watch. `pkill -f banglebridge` frees it.
- Antigravity's on-disk formats are internal and may change without notice.
- Always-on screen plus persistent BLE costs battery; exiting the watch app
  restores normal lock behavior.
