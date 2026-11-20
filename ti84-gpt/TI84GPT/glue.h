// Things shared between the ESP32-only parts of the sketch: the main .ino
// (link loop + network task), the saved settings and the setup web page.
#pragma once
#include <Arduino.h>

#include <string>
#include <vector>

#include "calc_app.h"
#include "ti_link.h"

#define FIRMWARE_VERSION "1.0"

void halBegin();  // hal_esp32.cpp

// ---------------------------------------------------------------------------
// Saved settings (kept in the ESP32's flash, entered on the setup page)
// ---------------------------------------------------------------------------
struct Settings {
  String ssid;
  String pass;
  String apiKey;
  String apiBase;
  String model;
  String promptChat;
  String promptMath;
  bool lowercase = true;
  bool swapped = false;      // link wire order: false = PIN_LINK_A is tip
  bool wiringKnown = false;  // true once the calculator has confirmed it
};

void settingsLoad();
void settingsSave();
void settingsErase();

// ---------------------------------------------------------------------------
// State shared between tasks. Take hal::lock() (hal::Guard) to touch any of
// gSettings or gShared.
// ---------------------------------------------------------------------------
struct Shared {
  bool portalWanted = false;  // network task: access point is up, serve the page
  bool portalRunning = false;  // link loop: web server is listening
  std::string message;         // latest result line shown on the setup page
  std::vector<std::string> models;
};

extern Settings gSettings;
extern Shared gShared;
extern ti::Link gLink;
extern app::CalcApp gCalc;

// ---------------------------------------------------------------------------
// Work for the network task. Kinds 1..6 are app::CommandCode values straight
// from the calculator; the rest come from the setup page and the main loop.
// ---------------------------------------------------------------------------
enum JobKind {
  JOB_WIFI_CONNECT = 100,  // text "boot": open the setup page if it fails
  JOB_WIFI_OFF,
  JOB_PORTAL_START,
  JOB_PORTAL_STOP,
  JOB_TEST,    // one small question to check Wi-Fi, key and model
  JOB_MODELS,  // fetch the list of models the key can use
  JOB_SCAN,    // look for Wi-Fi networks
};

void postJob(int kind, const std::string &text = std::string(), uint32_t ticket = 0);

// Ask the link loop to push prgmGPT to the calculator in about a second.
void requestInstall();

void setMessage(const std::string &message);

// Multi-line status report, used by the calculator's STATUS screen, the setup
// page and the serial console.
std::string statusText();

// net_client.cpp: one HTTP(S) request. Returns the HTTP status code, or a
// value <= 0 with `error` filled in when the server could not be reached.
int httpRequest(bool post, const String &url, const String &bearer, const std::string &body,
                std::string &response, std::string &error);

// portal.cpp: the setup web page.
void portalSetup();
void portalLoop();
