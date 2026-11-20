# TI84GPT

ChatGPT on a TI-84 Plus, using an **ESP32-C3 SuperMini** wired to the
calculator's link port.

You type a question on the calculator, the ESP32 sends it to the OpenAI API
over Wi-Fi, and the answer comes back as screens of text you page through.
Nothing else has to be running: no PC, no server.

- Ask a question, read the answer screen by screen, reply to keep the conversation going
- A separate math-solver mode (answer first, then short steps in TI-84 notation)
- Wi-Fi, API key and model are entered on a setup page from your phone
- The calculator program (`prgmGPT`) is stored in the ESP32 and sent to the calculator over the link wires, so you never need a cable or TI Connect
- No Arduino libraries to install: the sketch only needs the ESP32 boards package

> **Status: not yet run on real hardware.** I wrote this without a calculator
> on the bench. The link protocol, the calculator program and the text
> handling are tested against a simulated calculator (`test/`), and the
> firmware is compiled for the ESP32-C3 by the GitHub workflow, but the first
> power-up is yours. The [first power-up checklist](#first-power-up-checklist)
> is written for that, and the STATUS screen and serial log are there to make
> any problem easy to pin down.

## Wiring

Settings live in [`TI84GPT/config.h`](TI84GPT/config.h). The defaults match this build:

| ESP32-C3 SuperMini pad | Goes to | How the firmware uses it |
| --- | --- | --- |
| `5` | link jack, bottom contact | link data wire |
| `7` | link jack, right-side contact | link data wire |
| `6` | C21 | **read only** — never driven, no pull-up |
| `G` | calculator ground | required: the link cannot work without a shared ground |
| `5V` (or `3.3`) | calculator power | required to run without USB |

- **You do not need to know which link wire is tip and which is ring.** The
  first time the calculator talks, the firmware works it out from the data and
  remembers it. The STATUS screen shows what it found.
- The link pins are driven open-drain: the ESP32 only ever pulls a wire to
  ground or lets go. It never drives a link wire high.
- Pin `6` is only listened to. Its level is shown on the STATUS screen and the
  setup page as `AUX GPIO6`, which is the quickest way to find out what that
  point on the board does (compare calculator on / off).
- To use different pins, change `PIN_LINK_A`, `PIN_LINK_B` and `PIN_AUX`.
  On an ESP32-C3 avoid 2, 8 and 9 (boot strapping pins).

## Flashing

1. Arduino IDE → **Boards Manager** → install **esp32 by Espressif Systems**, version 3.3 or newer.
2. Open `TI84GPT/TI84GPT.ino` (keep all the files in that folder together).
3. **Tools → Board → esp32 → Nologo ESP32C3 Super Mini**.
   (Or *ESP32C3 Dev Module* with **USB CDC On Boot: Enabled**.)
4. **Tools → Partition Scheme → Huge APP (3MB No OTA/1MB SPIFFS)**.
   Wi-Fi, HTTPS and the setup page together are about 1.3 MB, which is just
   over the default 1.2 MB slot. Skip this and the IDE stops with "Sketch too big".
5. Plug in the SuperMini, pick its port, **Upload**.
   If the upload cannot connect: hold `BOOT`, tap `RESET`, release `BOOT`, upload again.

No changes to the code are needed before uploading.

## Setup (once)

1. Power the ESP32. With no Wi-Fi saved it starts **setup mode**: the blue LED blinks slowly.
2. On your phone, join the Wi-Fi network **`TI84-GPT`**, password **`84848484`**.
3. The setup page should pop up by itself; if not, browse to **`http://192.168.4.1`**.
4. Enter your Wi-Fi name and password and your OpenAI API key, then **Save**.
   A phone hotspot works fine as the Wi-Fi network. The ESP32 is 2.4 GHz only.
5. Tap **Test ChatGPT**. If it complains about the model, tap **Load model list**
   and pick one your key can use, **Save**, test again.
6. Switch the calculator on, leave it on the home screen, tap
   **Send program to calculator**. `GPT` appears under `PRGM`.
7. Tap **Finish setup** (or just leave: setup mode turns itself off after 10 minutes).

The API key is created at <https://platform.openai.com/api-keys> and needs a
little credit on the account; each question costs a fraction of a cent with a
small model. The key and Wi-Fi password are stored in the ESP32's flash and
are never sent back to the browser.

**Without the phone:** you can also install the program from the calculator's
home screen. Type `5→C` (`5` `STO▸` `ALPHA` `PRGM`) and press ENTER, then
`Send(C)` and ENTER. `Send(` is in the catalog: `2nd` `0`, press `LN` to jump
to the S entries, scroll down to `Send(`. A few seconds later `GPT` is in the
`PRGM` menu.

## Using it

`PRGM` → `GPT` → ENTER.

| Menu item | What it does |
| --- | --- |
| ASK CHATGPT | New conversation. Type the question, press ENTER. |
| FOLLOW-UP | Continue the same conversation. |
| SOLVE MATH | New conversation with the math-solver instructions. |
| STATUS | Wi-Fi, IP address, model, link wiring and counters. |
| WIFI SETUP | Turns setup mode back on (to change network, key or model). |
| UPDATE | Re-sends `prgmGPT` from the ESP32 (after a firmware update). |
| QUIT | Back to the home screen. |

While reading an answer:

| Key | |
| --- | --- |
| ENTER, ▶ or ▼ | next screen |
| ◀ or ▲ | previous screen |
| `+` | reply (follow-up) |
| CLEAR | back to the menu (also cancels while it says THINKING) |

The bottom line shows where you are, for example `2/5   ENTER=MORE`.

Typing: press `2nd` `ALPHA` for alpha-lock, `ALPHA` `0` is a space. Math keys
work too — `X²`, `√(`, `π`, `sin(` and so on arrive at ChatGPT as `X^2`,
`sqrt(`, `pi`, `sin(`.

## First power-up checklist

Do this with the SuperMini on USB and the Arduino **Serial Monitor** open at 115200 baud.

1. **Boot.** You should see `TI84GPT 1.0 - link on GPIO5 and GPIO7`.
   Type `s` + ENTER in the serial monitor at any time for the status report,
   and `d` + ENTER to log every packet the calculator sends (`h` lists the commands).
2. **Wires idle.** With the calculator on, the report should say `TIP=... high`
   and `RING=... high`. With the calculator off they will most likely read `LOW`.
   If one is stuck `LOW` with the calculator on, that wire is on the wrong pad or shorted.
3. **Calculator talks.** On the calculator: `5→C`, then `Send(C)`.
   The log should print `calculator: command 5` and, about 1.5 s later,
   `program transfer: sent`. The `LINK:` line counts good and bad packets.
4. **Program runs.** Run `prgmGPT`, choose STATUS. A screen of text means both
   directions of the link work.
5. **Internet.** Finish the setup page, then ASK CHATGPT.

What the outcome tells you:

| You see | Likely cause |
| --- | --- |
| Nothing in the log at step 3, `Send(C)` ends in `ERR:LINK` or just returns | Link wires not reaching GPIO 5/7, or no common ground |
| `LINK:` bad count climbing, no good packets | One link wire open, or the two wires shorted together |
| `program transfer: calculator did not answer` | Calculator was not on its home screen, or was off |
| `calculator refused the program` | Memory full, or `GPT` is archived on the calculator |
| Calculator shows `No Wi-Fi...` | Wrong password, out of range, or a 5 GHz-only network |
| `ERROR 401: Incorrect API key...` | Key typed wrong or revoked |
| `ERROR 404: The model ... does not exist` | Pick another model on the setup page |

## Changing things

- **Instructions given to ChatGPT, model, API address:** the setup page, under *More options*.
  The API address can point at any service that speaks the OpenAI chat-completions API.
- **Pins, setup network name/password, timeouts:** `TI84GPT/config.h`.
- **The calculator program:** edit `launcher/GPT.txt`, run
  `python3 tools/build_launcher.py`, upload the firmware, choose UPDATE on the calculator.
  `launcher/GPT.8xp` is the same program as a file, if you would rather send it with TI Connect.
- **Run the tests** (Linux/macOS/WSL, needs `g++`): `make -C test`

## Things worth knowing

- **Batteries.** The ESP32 draws far more than the calculator does. The
  firmware switches Wi-Fi off after 5 idle minutes (`WIFI_IDLE_OFF_MIN`) and
  reconnects on the next question, but it does not deep-sleep yet. If the
  board is powered straight from the batteries it will drain them over a day
  or two even with the calculator off.
- **Link voltage.** The ESP32's pins are 3.3 V parts. This design relies on
  the calculator's weak pull-ups, as the direct-wired tutorials do.
- **Setup mode is the only time the ESP32 accepts settings.** While it is on,
  anyone who knows the `TI84-GPT` password and is in range can change them
  (they cannot read the key back). Change `SETUP_AP_PASS` in `config.h` if that matters to you.
- **Tests and exams.** Don't use it where outside help isn't allowed.

## How it works

The calculator program uses TI-BASIC's `Send(` and `Get(`, which were made for
TI's CBL data loggers. The ESP32 answers the way a CBL would:

```
Send(Str1)   the text you typed
Send(C)      run command C (1 ask, 2 reply, 3 solve, 4 status, 5 install, 6 setup)
Get(S)       0 while working, 1 when the result is ready
Get(N)       number of screens in the result
Send(P)      choose screen P
Get(Str0)    that screen, 8 rows x 16 columns, ready for Output(1,1,Str0)
```

The link loop never waits on the network (a second task does the HTTPS
request), so the calculator always gets an immediate answer to `Get(S)`.

| File | |
| --- | --- |
| `TI84GPT/ti_link.*` | link protocol: bits, bytes, packets, tip/ring auto-detect |
| `TI84GPT/calc_app.*` | the `Send(`/`Get(` conversation, sending programs |
| `TI84GPT/text_layout.*` | Markdown/LaTeX clean-up, word wrap, screens |
| `TI84GPT/chat.*`, `json_mini.*` | conversation memory and the API's JSON |
| `TI84GPT/main_app.cpp`, `portal.cpp`, `net_client.cpp`, `settings.cpp` | ESP32 side: tasks, setup page, HTTPS, saved settings |
| `launcher/GPT.txt` | the calculator program, readable |
| `test/` | simulated wires + simulated calculator + a small TI-BASIC interpreter that runs the real `prgmGPT` |

## Credits

- [ChromaLock's TI-32](https://github.com/chromalock/TI-32) showed that `Send(`/`Get(` plus an ESP32 inside the case makes a usable link.
- [ArTICL](https://github.com/KermMartian/ArTICL) by Christopher Mitchell was the reference for how a TI-84 Plus behaves when it talks to a CBL.
- Token values come from the [TI-Toolkit token sheets](https://github.com/TI-Toolkit/tokens).

The code in this folder was written from scratch for the ESP32-C3 SuperMini.
