// Saved settings, stored in the ESP32's NVS flash area.
#ifdef ARDUINO

#include <Preferences.h>

#include "config.h"
#include "glue.h"
#include "hal.h"

static const char *kNamespace = "ti84gpt";

Settings gSettings;

void settingsLoad() {
  Preferences prefs;
  prefs.begin(kNamespace, true);
  Settings s;
  s.ssid = prefs.getString("ssid", "");
  s.pass = prefs.getString("pass", "");
  s.apiKey = prefs.getString("key", "");
  s.apiBase = prefs.getString("base", DEFAULT_API_BASE);
  s.model = prefs.getString("model", DEFAULT_MODEL);
  s.promptChat = prefs.getString("pchat", DEFAULT_PROMPT_CHAT);
  s.promptMath = prefs.getString("pmath", DEFAULT_PROMPT_MATH);
  s.lowercase = prefs.getBool("lower", true);
  s.swapped = prefs.getBool("swapped", false);
  s.wiringKnown = prefs.getBool("wiring", false);
  prefs.end();
  if (s.apiBase.isEmpty()) s.apiBase = DEFAULT_API_BASE;
  if (s.model.isEmpty()) s.model = DEFAULT_MODEL;

  hal::Guard guard;
  gSettings = s;
}

void settingsSave() {
  Settings s;
  {
    hal::Guard guard;
    s = gSettings;
  }
  Preferences prefs;
  prefs.begin(kNamespace, false);
  prefs.putString("ssid", s.ssid);
  prefs.putString("pass", s.pass);
  prefs.putString("key", s.apiKey);
  prefs.putString("base", s.apiBase);
  prefs.putString("model", s.model);
  prefs.putString("pchat", s.promptChat);
  prefs.putString("pmath", s.promptMath);
  prefs.putBool("lower", s.lowercase);
  prefs.putBool("swapped", s.swapped);
  prefs.putBool("wiring", s.wiringKnown);
  prefs.end();
}

void settingsErase() {
  Preferences prefs;
  prefs.begin(kNamespace, false);
  prefs.clear();
  prefs.end();
  settingsLoad();
}

#endif  // ARDUINO
