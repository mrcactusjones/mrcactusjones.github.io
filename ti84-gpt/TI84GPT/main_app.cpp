// TI84GPT main program for the ESP32.
//
// Two tasks:
//   * the Arduino loop() services the calculator link (and the setup page).
//     It must never wait on the network, so the calculator always gets a
//     prompt answer to Get(S) while ChatGPT is thinking.
//   * the "net" task owns Wi-Fi and does the slow work: HTTPS requests,
//     connecting, starting and stopping setup mode.
#ifdef ARDUINO

#include <Arduino.h>
#include <WiFi.h>

#include <deque>

#include "chat.h"
#include "config.h"
#include "glue.h"
#include "hal.h"
#include "launcher_program.h"

Shared gShared;
ti::Link gLink;
app::CalcApp gCalc;

namespace {

struct Job {
  int kind;
  std::string text;
  uint32_t ticket;
};

std::deque<Job> gJobs;  // protected by hal::lock()
TaskHandle_t gNetTask = nullptr;
volatile bool gNetBusy = false;
volatile uint32_t gLastNetUseMs = 0;

chat::Conversation gConversation;  // only touched by the net task
bool gConversationIsMath = false;

// Program push requested: tries left and when to try next (hal::lock()).
int gInstallTries = 0;
uint32_t gInstallAtMs = 0;

// Wire order learned by the link loop, waiting to be saved.
volatile bool gWiringDirty = false;

std::string str(const String &s) { return std::string(s.c_str(), s.length()); }

// ---------------------------------------------------------------------------
// Calculator commands
// ---------------------------------------------------------------------------

class Glue : public app::Handler {
 public:
  void onCommand(int command, const std::string &text, uint32_t ticket) override {
    hal::logf("calculator: command %d \"%s\"", command, text.c_str());
    switch (command) {
      case app::COMMAND_STATUS:
        // No network needed: answer straight away, even mid-request.
        gCalc.finish(ticket, statusText(), app::FOOTER_INFO);
        break;
      case app::COMMAND_INSTALL:
        gCalc.finish(ticket, "Sending prgmGPT to the calculator...", app::FOOTER_INFO);
        requestInstall();
        break;
      case app::COMMAND_ASK:
      case app::COMMAND_REPLY:
      case app::COMMAND_SOLVE:
      case app::COMMAND_SETUP:
        postJob(command, text, ticket);
        break;
      default:
        gCalc.finish(ticket, "Unknown command. Choose UPDATE in the menu to refresh prgmGPT.",
                     app::FOOTER_INFO);
        break;
    }
  }

  void onWiringLearned(bool swapped) override {
    (void)swapped;
    gWiringDirty = true;  // saved from loop() once the link is quiet
  }
};

Glue gGlue;

// ---------------------------------------------------------------------------
// Network task
// ---------------------------------------------------------------------------

bool portalWanted() {
  hal::Guard guard;
  return gShared.portalWanted;
}

Settings settingsCopy() {
  hal::Guard guard;
  return gSettings;
}

bool wifiConnect(const Settings &s, uint32_t timeoutMs) {
  if (s.ssid.isEmpty()) return false;
  if (WiFi.status() == WL_CONNECTED && WiFi.SSID() == s.ssid) return true;

  const bool portal = portalWanted();
  hal::logf("wifi: connecting to \"%s\"", s.ssid.c_str());
  WiFi.mode(portal ? WIFI_AP_STA : WIFI_STA);
  // While the setup access point is up, endless retries would keep dragging
  // the radio off the phone's channel; one attempt at a time is kinder.
  WiFi.setAutoReconnect(!portal);
  WiFi.begin(s.ssid.c_str(), s.pass.length() ? s.pass.c_str() : nullptr);
  const uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < timeoutMs) delay(100);

  if (WiFi.status() != WL_CONNECTED) {
    hal::logf("wifi: could not connect");
    return false;
  }
  WiFi.setSleep(true);  // modem sleep between packets: much kinder to AAA cells
  configTime(0, 0, "pool.ntp.org", "time.google.com");
  hal::logf("wifi: connected, IP %s, %d dBm", WiFi.localIP().toString().c_str(), (int)WiFi.RSSI());
  return true;
}

void portalStart() {
  if (portalWanted()) return;
  hal::logf("setup mode: join Wi-Fi \"%s\" and open http://192.168.4.1", SETUP_AP_SSID);
  WiFi.mode(WIFI_AP_STA);
  WiFi.setAutoReconnect(false);
  const char *password = (strlen(SETUP_AP_PASS) >= 8) ? SETUP_AP_PASS : nullptr;
  WiFi.softAP(SETUP_AP_SSID, password);
  delay(300);
  WiFi.scanNetworks(true);  // in the background; results appear on the page
  hal::Guard guard;
  gShared.portalWanted = true;
}

void portalStop() {
  if (!portalWanted()) return;
  {
    hal::Guard guard;
    gShared.portalWanted = false;
  }
  // Give loop() a moment to close the web server before the radio changes.
  for (int i = 0; i < 100; i++) {
    bool running;
    {
      hal::Guard guard;
      running = gShared.portalRunning;
    }
    if (!running) break;
    delay(20);
  }
  WiFi.softAPdisconnect(true);
  if (WiFi.status() == WL_CONNECTED) {
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);
  } else {
    WiFi.mode(WIFI_OFF);
  }
  hal::logf("setup mode: off");
}

// Sends the conversation to the API. Returns true with the answer, or false
// with a message meant for the calculator screen.
bool askModel(const Settings &s, const std::string &prompt, std::string &answer) {
  const std::string body = gConversation.requestJson(str(s.model), prompt);
  std::string response, error;
  const uint32_t start = millis();
  const int code = httpRequest(true, s.apiBase + "/chat/completions", s.apiKey, body, response, error);
  hal::logf("api: HTTP %d after %lu ms, %u bytes", code, (unsigned long)(millis() - start),
            (unsigned)response.size());

  if (code <= 0) {
    answer = "ERROR: could not reach the ChatGPT server (" + error + "). Check the Wi-Fi.";
    return false;
  }
  std::string content, apiError;
  if (chat::parseResponse(response, content, apiError)) {
    answer = content;
    return true;
  }
  char status[24];
  snprintf(status, sizeof(status), "ERROR %d: ", code);
  answer = status + apiError;
  return false;
}

void runChat(const Job &job) {
  const Settings s = settingsCopy();
  const bool fresh = (job.kind != app::COMMAND_REPLY) || gConversation.empty();

  if (job.text.empty()) {
    gCalc.finish(job.ticket, "Nothing was typed. Choose ASK CHATGPT and type a question.", app::FOOTER_INFO);
    return;
  }
  if (s.ssid.isEmpty()) {
    gCalc.finish(job.ticket, "Wi-Fi is not set up yet. Choose WIFI SETUP in the menu.", app::FOOTER_INFO);
    return;
  }
  if (s.apiKey.isEmpty() && s.apiBase.indexOf("openai.com") >= 0) {
    gCalc.finish(job.ticket, "No API key yet. Choose WIFI SETUP in the menu and enter your OpenAI key.",
                 app::FOOTER_INFO);
    return;
  }
  if (!wifiConnect(s, 20000)) {
    gCalc.finish(job.ticket, "No Wi-Fi: could not join " + str(s.ssid) +
                                 ". Is it in range? WIFI SETUP changes the network.",
                 app::FOOTER_INFO);
    return;
  }

  if (fresh) {
    gConversation.clear();
    gConversationIsMath = (job.kind == app::COMMAND_SOLVE);
  }
  gConversation.addUser(job.text);
  gConversation.trim(HISTORY_MAX_CHARS);

  std::string answer;
  if (askModel(s, str(gConversationIsMath ? s.promptMath : s.promptChat), answer)) {
    gConversation.addAssistant(answer);
    gCalc.finish(job.ticket, answer, app::FOOTER_CHAT);
  } else {
    gConversation.dropLast();  // so asking again does not send it twice
    gCalc.finish(job.ticket, answer, app::FOOTER_INFO);
  }
}

void runTest() {
  const Settings s = settingsCopy();
  if (!wifiConnect(s, 20000)) {
    setMessage("Test failed: could not join Wi-Fi \"" + str(s.ssid) + "\".");
    return;
  }
  chat::Conversation saved = gConversation;
  gConversation.clear();
  gConversation.addUser("Reply with exactly: TI-84 link OK");
  std::string answer;
  const bool ok = askModel(s, "", answer);
  gConversation = saved;
  setMessage(ok ? "Test passed. ChatGPT said: " + answer : "Test failed. " + answer);
}

void runModels() {
  const Settings s = settingsCopy();
  if (!wifiConnect(s, 20000)) {
    setMessage("Could not join Wi-Fi \"" + str(s.ssid) + "\".");
    return;
  }
  std::string response, error;
  const int code = httpRequest(false, s.apiBase + "/models", s.apiKey, std::string(), response, error);
  std::vector<std::string> ids;
  if (code == 200) chat::parseModelList(response, ids);
  if (ids.empty()) {
    std::string content, apiError;
    chat::parseResponse(response, content, apiError);
    char status[40];
    snprintf(status, sizeof(status), "No model list (HTTP %d). ", code);
    setMessage(status + (code <= 0 ? error : apiError));
    return;
  }
  char found[64];
  snprintf(found, sizeof(found), "%u models loaded - tap the Model box to pick one.", (unsigned)ids.size());
  {
    hal::Guard guard;
    gShared.models.swap(ids);
  }
  setMessage(found);
}

void runJob(const Job &job) {
  switch (job.kind) {
    case app::COMMAND_ASK:
    case app::COMMAND_REPLY:
    case app::COMMAND_SOLVE:
      runChat(job);
      break;

    case app::COMMAND_SETUP:
      portalStart();
      gCalc.finish(job.ticket,
                   "SETUP IS ON.\nPhone Wi-Fi:\n " SETUP_AP_SSID "\nPassword:\n " SETUP_AP_PASS
                   "\nThen browse to\n 192.168.4.1",
                   app::FOOTER_INFO);
      break;

    case JOB_WIFI_CONNECT: {
      const Settings s = settingsCopy();
      const bool connected = wifiConnect(s, 20000);
      if (portalWanted()) {
        setMessage(connected ? "Wi-Fi connected. Now try Test ChatGPT."
                             : "Could not join \"" + str(s.ssid) + "\" - check the name and password.");
      } else if (!connected && job.text == "boot") {
        portalStart();
      }
      break;
    }

    case JOB_WIFI_OFF:
      if (!portalWanted()) {
        hal::logf("wifi: idle, switching the radio off");
        WiFi.disconnect(true);
        WiFi.mode(WIFI_OFF);
      }
      break;

    case JOB_PORTAL_START: portalStart(); break;
    case JOB_PORTAL_STOP: portalStop(); break;
    case JOB_TEST: runTest(); break;
    case JOB_MODELS: runModels(); break;
    case JOB_SCAN: WiFi.scanNetworks(true); break;
    default: break;
  }
}

void netTask(void *) {
  for (;;) {
    ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    for (;;) {
      Job job;
      {
        hal::Guard guard;
        if (gJobs.empty()) break;
        job = gJobs.front();
        gJobs.pop_front();
      }
      gNetBusy = true;
      runJob(job);
      gLastNetUseMs = millis();
      gNetBusy = false;
    }
  }
}

// ---------------------------------------------------------------------------
// loop() helpers
// ---------------------------------------------------------------------------

void serviceInstall() {
  {
    hal::Guard guard;
    if (gInstallTries <= 0 || (int32_t)(millis() - gInstallAtMs) < 0) return;
  }
  const int result = gCalc.sendProgram(LAUNCHER_NAME, kLauncherProgram, kLauncherProgramSize);
  int triesLeft;
  {
    hal::Guard guard;
    gInstallTries = (result == app::SEND_OK) ? 0 : gInstallTries - 1;
    gInstallAtMs = millis() + 2000;
    triesLeft = gInstallTries;
  }
  hal::logf("program transfer: %s", app::sendResultText(result));
  if (result == app::SEND_OK) {
    setMessage("prgmGPT is on the calculator: press PRGM and run GPT.");
  } else if (triesLeft == 0) {
    setMessage(std::string("Program not sent: ") + app::sendResultText(result));
  }
}

void serviceWiring() {
  if (!gWiringDirty) return;
  gWiringDirty = false;
  {
    hal::Guard guard;
    if (gSettings.wiringKnown && gSettings.swapped == gLink.swapped()) return;
    gSettings.swapped = gLink.swapped();
    gSettings.wiringKnown = true;
  }
  settingsSave();
  hal::logf("link: tip is GPIO%d, ring is GPIO%d (saved)", gLink.tipPin(), gLink.ringPin());
}

void serviceIdle() {
#if WIFI_IDLE_OFF_MIN > 0
  static uint32_t lastCheckMs = 0;
  if (millis() - lastCheckMs < 5000) return;
  lastCheckMs = millis();
  if (gNetBusy || portalWanted() || WiFi.getMode() == WIFI_OFF) return;
  if (millis() - gLastNetUseMs > (uint32_t)WIFI_IDLE_OFF_MIN * 60000u) {
    gLastNetUseMs = millis();
    postJob(JOB_WIFI_OFF);
  }
#endif
}

void serviceLed() {
#if PIN_LED >= 0
  // On while working, slow blink in setup mode, otherwise off.
  bool on = gNetBusy || gCalc.busy();
  if (!on && portalWanted()) on = (millis() / 500) % 2;
  digitalWrite(PIN_LED, (on != (LED_ACTIVE_LOW != 0)) ? HIGH : LOW);
#endif
}

// Handy on the bench: single-letter commands over the USB serial port.
void serviceSerial() {
  while (Serial.available() > 0) {
    const int c = Serial.read();
    switch (c) {
      case 's': Serial.println(statusText().c_str()); break;
      case 'i': requestInstall(); Serial.println("sending prgmGPT in a moment..."); break;
      case 'p': postJob(JOB_PORTAL_START); break;
      case 'q': postJob(JOB_PORTAL_STOP); break;
      case 'd':
        gCalc.setDebug(!gCalc.debug());
        Serial.println(gCalc.debug() ? "link debug ON: every packet is logged" : "link debug off");
        break;
      case 'h':
      case '?':
        Serial.println("s = status, d = log link packets, i = send prgmGPT to the calculator, "
                       "p = setup page on, q = setup page off");
        break;
      default: break;
    }
  }
}

}  // namespace

// ---------------------------------------------------------------------------
// Used by the other files
// ---------------------------------------------------------------------------

void postJob(int kind, const std::string &text, uint32_t ticket) {
  {
    hal::Guard guard;
    gJobs.push_back(Job{kind, text, ticket});
  }
  if (gNetTask) xTaskNotifyGive(gNetTask);
}

void requestInstall() {
  hal::Guard guard;
  gInstallTries = 4;
  gInstallAtMs = millis() + 1500;  // lets the calculator get back to its home screen
}

void setMessage(const std::string &message) {
  hal::logf("%s", message.c_str());
  hal::Guard guard;
  gShared.message = message;
}

std::string statusText() {
  const Settings s = settingsCopy();
  const app::Stats stats = gCalc.stats();
  char line[96];
  std::string out = "TI84GPT " FIRMWARE_VERSION "\n";

  if (s.ssid.isEmpty()) {
    out += "WIFI: not set up\n";
  } else if (WiFi.status() == WL_CONNECTED) {
    snprintf(line, sizeof(line), "WIFI: %s %ddBm\nIP: %s\n", s.ssid.c_str(), (int)WiFi.RSSI(),
             WiFi.localIP().toString().c_str());
    out += line;
  } else {
    out += "WIFI: " + str(s.ssid) + (WiFi.getMode() == WIFI_OFF ? " (asleep)\n" : " (not connected)\n");
  }
  if (portalWanted()) out += "SETUP PAGE: ON\n";
  out += "KEY: " + std::string(s.apiKey.isEmpty() ? "MISSING" : "set") + "\n";
  out += "MODEL: " + str(s.model) + "\n";

  snprintf(line, sizeof(line), "TIP=GPIO%d %s\nRING=GPIO%d %s\n", gLink.tipPin(),
           hal::lineHigh(gLink.tipPin()) ? "high" : "LOW", gLink.ringPin(),
           hal::lineHigh(gLink.ringPin()) ? "high" : "LOW");
  out += line;
  if (!gLink.orientationKnown()) out += "(tip/ring not confirmed yet)\n";
#if PIN_AUX >= 0
  snprintf(line, sizeof(line), "AUX GPIO%d: %s\n", PIN_AUX, digitalRead(PIN_AUX) ? "high" : "LOW");
  out += line;
#endif
  snprintf(line, sizeof(line), "LINK: %lu ok, %lu bad\nHEAP: %lu", (unsigned long)stats.packets,
           (unsigned long)stats.errors, (unsigned long)ESP.getFreeHeap());
  out += line;
  return out;
}

void appSetup() {
  Serial.begin(115200);
#if ARDUINO_USB_CDC_ON_BOOT
  Serial.setTxTimeoutMs(0);  // never stall the link because nobody is reading USB
#endif
  halBegin();

#if PIN_LED >= 0
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, LED_ACTIVE_LOW ? HIGH : LOW);
#endif
#if PIN_AUX >= 0
  pinMode(PIN_AUX, INPUT);  // listen only: no pull-up, never driven
#endif

  settingsLoad();
  const Settings s = settingsCopy();
  gLink.begin(PIN_LINK_A, PIN_LINK_B, s.swapped);
  gLink.setOrientationKnown(s.wiringKnown);
  gCalc.begin(&gLink, &gGlue, s.lowercase);
  portalSetup();

  hal::logf("TI84GPT " FIRMWARE_VERSION " - link on GPIO%d and GPIO%d", PIN_LINK_A, PIN_LINK_B);

  xTaskCreate(netTask, "net", 16384, nullptr, 1, &gNetTask);
  // The link loop outranks the network task so a transfer in progress is
  // never starved by TLS number-crunching.
  vTaskPrioritySet(nullptr, 2);

  gLastNetUseMs = millis();
  if (s.ssid.isEmpty()) {
    postJob(JOB_PORTAL_START);
  } else {
    postJob(JOB_WIFI_CONNECT, "boot");
  }
}

void appLoop() {
  if (gCalc.poll()) return;  // more may follow immediately: stay on the link

  serviceWiring();
  serviceInstall();
  portalLoop();
  serviceSerial();
  serviceIdle();
  serviceLed();
  delay(1);  // let the network task and the idle task run
}

#endif  // ARDUINO
