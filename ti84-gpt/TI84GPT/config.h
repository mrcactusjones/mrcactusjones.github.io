// TI84GPT - settings you may want to change before uploading.
// Everything about Wi-Fi and the API key is entered later on the phone
// setup page, so nothing secret lives in this file.
#pragma once

// ---------------------------------------------------------------------------
// Wiring (ESP32-C3 SuperMini)
// ---------------------------------------------------------------------------
// The two wires soldered to the 2.5 mm link jack. It does not matter which of
// them is "tip" and which is "ring": the firmware works that out by itself the
// first time the calculator talks to it and remembers the answer.
#define PIN_LINK_A 5
#define PIN_LINK_B 7

// Third wire (soldered at C21 on the calculator board). The firmware only
// ever READS this pin - it is never driven and has no pull-up - so it is safe
// whatever that point turns out to be. Its level is shown on the setup page
// and in the STATUS screen. Set to -1 if you have no such wire.
#define PIN_AUX 6

// Onboard LED of the SuperMini (blue, on when the pin is LOW). -1 = no LED.
#define PIN_LED 8
#define LED_ACTIVE_LOW 1

// ---------------------------------------------------------------------------
// Setup access point (only on air while the setup page is active)
// ---------------------------------------------------------------------------
#define SETUP_AP_SSID "TI84-GPT"
#define SETUP_AP_PASS "84848484"     // 8+ characters, or "" for an open network
#define SETUP_TIMEOUT_MIN 10         // setup page switches itself off after this

// ---------------------------------------------------------------------------
// ChatGPT defaults (all of these can be changed on the setup page)
// ---------------------------------------------------------------------------
#define DEFAULT_API_BASE "https://api.openai.com/v1"
#define DEFAULT_MODEL "gpt-5.4-mini"

#define DEFAULT_PROMPT_CHAT                                                   \
  "You are answering on a TI-84 Plus graphing calculator. The screen is 16 "   \
  "characters wide and 7 lines tall, so be brief: give the answer first and "  \
  "keep the whole reply under 350 characters unless the user asks for more "   \
  "detail. Plain text only: no Markdown, no LaTeX, no emoji, no tables. "      \
  "Write math the way it is typed on a TI-84: ^ for powers, * and / for "      \
  "multiply and divide, sqrt( ), pi, and parentheses instead of fractions "    \
  "stacked on lines."

#define DEFAULT_PROMPT_MATH                                                   \
  "You are a math solver running on a TI-84 Plus graphing calculator with a "  \
  "16x7 character screen. Solve the problem the user typed. Put the final "    \
  "answer on the first line as 'ANS: ...', then show the key steps, one "      \
  "short step per line. Plain text only: no Markdown, no LaTeX, no emoji. "    \
  "Use TI-84 notation: ^ for powers, * and /, sqrt( ), pi. Give exact "        \
  "values and a decimal approximation when it helps. Stay under 500 "          \
  "characters."

// Seconds to wait for ChatGPT before giving up.
#define HTTP_TIMEOUT_S 60

// Verify the server's TLS certificate against the built-in CA bundle
// (needs ESP32 Arduino core 3.3 or newer; older cores fall back to
// unverified TLS and say so on the serial log).
#define TLS_VERIFY 1

// Turn Wi-Fi off after this many minutes without a question (saves the
// calculator's batteries). It reconnects by itself on the next question.
// 0 = keep Wi-Fi connected all the time.
#define WIFI_IDLE_OFF_MIN 5

// How much of the conversation to remember for follow-up questions.
#define HISTORY_MAX_CHARS 6000

// Longest answer kept, in calculator characters (about 35 screens).
#define ANSWER_MAX_GLYPHS 4000
