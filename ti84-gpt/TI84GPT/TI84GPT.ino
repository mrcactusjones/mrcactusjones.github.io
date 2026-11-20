// TI84GPT - ChatGPT on a TI-84 Plus through an ESP32-C3 SuperMini wired to
// the calculator's link port.
//
// Board:  "Nologo ESP32C3 Super Mini"  (or "ESP32C3 Dev Module" with
//         Tools > USB CDC On Boot > Enabled)
// Needs:  the "esp32 by Espressif Systems" boards package, version 3.3 or
//         newer. No extra libraries.
//
// Wiring and other settings are in config.h. Everything else (Wi-Fi, API key,
// model) is entered on the setup page - see README.md.
//
// The program itself lives in main_app.cpp; the other files are:
//   ti_link.*      the calculator link protocol (bits, bytes, packets)
//   calc_app.*     the Send( / Get( conversation with the calculator program
//   ti_vars.*      calculator number and string formats
//   ti_tokens.*    calculator character codes
//   text_layout.*  turning an answer into 16x8 screens
//   chat.*         conversation memory and the ChatGPT JSON
//   json_mini.*    a small JSON reader
//   net_client.*   HTTPS
//   portal.*       the phone setup page
//   settings.*     saved settings
//   launcher_program.h  the calculator program prgmGPT (built from launcher/GPT.txt)

void appSetup();
void appLoop();

void setup() { appSetup(); }

void loop() { appLoop(); }
