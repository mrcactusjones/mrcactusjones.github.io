// The setup page you open on a phone: Wi-Fi name and password, API key,
// model, plus a few tools. It is only on air while "setup mode" is active
// (first boot, failed Wi-Fi, or WIFI SETUP chosen on the calculator).
#ifdef ARDUINO

#include <DNSServer.h>
#include <WebServer.h>
#include <WiFi.h>

#include "config.h"
#include "glue.h"
#include "hal.h"
#include "json_mini.h"

static WebServer server(80);
static DNSServer dns;
static bool running = false;
static uint32_t touchedMs = 0;

static const char kPage[] PROGMEM = R"HTML(<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TI-84 GPT setup</title>
<style>
body{font-family:system-ui,sans-serif;max-width:34rem;margin:0 auto;padding:1rem;background:#f3f3ef;color:#1c1c1c}
h1{font-size:1.35rem;margin:.2rem 0 .8rem}
h2{font-size:1rem;margin:1.4rem 0 .3rem}
label{display:block;font-size:.85rem;margin:.7rem 0 .2rem}
input,textarea,button{font:inherit;width:100%;box-sizing:border-box;padding:.55rem;border:1px solid #8a8a8a;border-radius:.4rem;background:#fff;color:#1c1c1c}
textarea{height:7rem}
button{background:#1f4e8c;color:#fff;border:0;margin-top:.6rem}
button.alt{background:#4d4d4d}
button.warn{background:#9c2f2f}
.box{background:#fff;border:1px solid #c9c9c9;border-radius:.5rem;padding:.6rem .8rem;font:0.85rem ui-monospace,monospace;white-space:pre-wrap;overflow-wrap:anywhere}
.row{display:flex;gap:.5rem}.row>*{flex:1}
#msg{min-height:1.3rem;margin-top:.7rem;font-weight:600}
summary{margin-top:.8rem;cursor:pointer}
.check{display:flex;align-items:center;gap:.5rem}.check input{width:auto}
</style></head><body>
<h1>TI-84 GPT setup</h1>
<div class="box" id="status">Loading...</div>
<div id="msg"></div>

<h2>Wi-Fi</h2>
<label for="ssid">Network name</label>
<input id="ssid" list="nets" autocomplete="off" autocapitalize="none">
<datalist id="nets"></datalist>
<label for="pass">Password</label>
<input id="pass" type="password" placeholder="(leave empty to keep the saved one)">

<h2>ChatGPT</h2>
<label for="key">OpenAI API key</label>
<input id="key" type="password" autocomplete="off" placeholder="(leave empty to keep the saved one)">
<label for="model">Model</label>
<input id="model" list="models" autocomplete="off" autocapitalize="none">
<datalist id="models"></datalist>
<details><summary>More options</summary>
<label for="base">API address</label>
<input id="base" autocapitalize="none">
<label for="pchat">Instructions for ASK and FOLLOW-UP</label>
<textarea id="pchat"></textarea>
<label for="pmath">Instructions for SOLVE MATH</label>
<textarea id="pmath"></textarea>
<label class="check"><input type="checkbox" id="lower"> Show lowercase letters on the calculator</label>
</details>
<button onclick="save()">Save</button>

<h2>Tools</h2>
<div class="row"><button class="alt" onclick="act('test')">Test ChatGPT</button>
<button class="alt" onclick="act('models')">Load model list</button></div>
<div class="row"><button class="alt" onclick="act('install')">Send program to calculator</button>
<button class="alt" onclick="act('scan')">Scan for Wi-Fi</button></div>
<div class="row"><button class="alt" onclick="act('swap')">Swap tip / ring</button>
<button class="alt" onclick="act('done')">Finish setup</button></div>
<button class="warn" onclick="if(confirm('Erase the Wi-Fi password, API key and every other setting?'))act('erase')">Erase all settings</button>

<script>
const $=id=>document.getElementById(id);
let filled=false;
function say(t){$('msg').textContent=t}
function options(id,items){const l=$(id);l.textContent='';for(const v of items){const o=document.createElement('option');o.value=v;l.appendChild(o)}}
async function poll(){
  try{
    const s=await (await fetch('/status.json')).json();
    $('status').textContent=s.report;
    if(s.message)say(s.message);
    options('nets',s.networks);options('models',s.models);
    if(!filled){filled=true;$('ssid').value=s.ssid;$('model').value=s.model;$('base').value=s.base;
      $('pchat').value=s.pchat;$('pmath').value=s.pmath;$('lower').checked=s.lower}
  }catch(e){}
}
async function post(url,data){
  try{return await (await fetch(url,{method:'POST',body:new URLSearchParams(data)})).text()}
  catch(e){return 'No answer from the ESP32 - is your phone still on its Wi-Fi?'}
}
async function save(){
  say('Saving...');
  say(await post('/save',{ssid:$('ssid').value,pass:$('pass').value,key:$('key').value,model:$('model').value,
    base:$('base').value,pchat:$('pchat').value,pmath:$('pmath').value,lower:$('lower').checked?'1':'0'}));
  $('pass').value='';$('key').value='';
}
async function act(what){say('Working...');say(await post('/action',{do:what}));if(what=='erase'){filled=false}}
setInterval(poll,1500);poll();
</script>
</body></html>)HTML";

static void touch() { touchedMs = millis(); }

static void handleRoot() {
  touch();
  server.send_P(200, "text/html; charset=utf-8", kPage);
}

static void appendList(std::string &out, const char *key, const std::vector<std::string> &items) {
  out += "\"";
  out += key;
  out += "\":[";
  for (size_t i = 0; i < items.size(); i++) {
    if (i) out += ",";
    out += "\"" + json::escape(items[i]) + "\"";
  }
  out += "]";
}

static void handleStatus() {
  touch();
  Settings s;
  std::string message;
  std::vector<std::string> models;
  {
    hal::Guard guard;
    s = gSettings;
    message = gShared.message;
    models = gShared.models;
  }

  std::vector<std::string> networks;
  const int found = WiFi.scanComplete();
  for (int i = 0; i < found && networks.size() < 25; i++) {
    const String name = WiFi.SSID((uint8_t)i);
    if (!name.length()) continue;
    bool seen = false;
    for (size_t k = 0; k < networks.size(); k++) seen = seen || (networks[k] == name.c_str());
    if (!seen) networks.push_back(name.c_str());
  }

  // Passwords and the API key are never sent back to the browser.
  std::string out = "{\"report\":\"" + json::escape(statusText()) + "\",";
  out += "\"message\":\"" + json::escape(message) + "\",";
  out += "\"ssid\":\"" + json::escape(s.ssid.c_str()) + "\",";
  out += "\"model\":\"" + json::escape(s.model.c_str()) + "\",";
  out += "\"base\":\"" + json::escape(s.apiBase.c_str()) + "\",";
  out += "\"pchat\":\"" + json::escape(s.promptChat.c_str()) + "\",";
  out += "\"pmath\":\"" + json::escape(s.promptMath.c_str()) + "\",";
  out += std::string("\"lower\":") + (s.lowercase ? "true" : "false") + ",";
  appendList(out, "networks", networks);
  out += ",";
  appendList(out, "models", models);
  out += "}";
  server.sendHeader("Cache-Control", "no-store");
  server.send(200, "application/json", out.c_str());
}

static String limited(const String &value, unsigned int maxLen) {
  String v = value;
  v.trim();
  if (v.length() > maxLen) v.remove(maxLen);
  return v;
}

static void handleSave() {
  touch();
  const String ssid = limited(server.arg("ssid"), 32);
  const String pass = server.arg("pass");  // spaces can be part of a password
  const String key = limited(server.arg("key"), 300);
  String base = limited(server.arg("base"), 200);
  while (base.endsWith("/")) base.remove(base.length() - 1);
  const String model = limited(server.arg("model"), 80);
  const String pchat = limited(server.arg("pchat"), 1500);
  const String pmath = limited(server.arg("pmath"), 1500);
  const bool lowercase = server.arg("lower") != "0";

  if (base.length() && !base.startsWith("https://") && !base.startsWith("http://")) {
    server.send(200, "text/plain", "The API address must start with https://");
    return;
  }

  {
    hal::Guard guard;
    // An empty password box means "keep it" - unless the network changed, in
    // which case it means the new network has no password.
    if (pass.length() || ssid != gSettings.ssid) gSettings.pass = pass.length() <= 64 ? pass : String();
    gSettings.ssid = ssid;
    if (key.length()) gSettings.apiKey = key;
    gSettings.apiBase = base.length() ? base : String(DEFAULT_API_BASE);
    gSettings.model = model.length() ? model : String(DEFAULT_MODEL);
    gSettings.promptChat = pchat;
    gSettings.promptMath = pmath;
    gSettings.lowercase = lowercase;
  }
  settingsSave();
  gCalc.setLowercase(lowercase);
  setMessage("Saved. Connecting to Wi-Fi...");
  postJob(JOB_WIFI_CONNECT);
  server.send(200, "text/plain", "Saved. Connecting to Wi-Fi...");
}

static void handleAction() {
  touch();
  const String what = server.arg("do");
  const char *reply = "Unknown action.";
  if (what == "test") {
    postJob(JOB_TEST);
    reply = "Asking ChatGPT a test question...";
  } else if (what == "models") {
    postJob(JOB_MODELS);
    reply = "Fetching the model list...";
  } else if (what == "scan") {
    postJob(JOB_SCAN);
    reply = "Scanning... the list under Network name fills in after a few seconds.";
  } else if (what == "install") {
    requestInstall();
    reply = "Sending prgmGPT - the calculator must be on, at its home screen.";
  } else if (what == "swap") {
    gLink.setSwapped(!gLink.swapped());
    {
      hal::Guard guard;
      gSettings.swapped = gLink.swapped();
    }
    settingsSave();
    reply = "Tip and ring swapped.";
  } else if (what == "done") {
    postJob(JOB_PORTAL_STOP);
    reply = "Setup mode is switching off. You can close this page.";
  } else if (what == "erase") {
    settingsErase();
    gCalc.setLowercase(true);
    reply = "All settings erased.";
  }
  setMessage(reply);
  server.send(200, "text/plain", reply);
}

// Phones probe random addresses to detect a "sign in to Wi-Fi" page; sending
// them all to our page makes the setup page pop up by itself.
static void handleNotFound() {
  server.sendHeader("Location", String("http://") + WiFi.softAPIP().toString() + "/", true);
  server.send(302, "text/plain", "");
}

void portalSetup() {
  server.on("/", HTTP_GET, handleRoot);
  server.on("/status.json", HTTP_GET, handleStatus);
  server.on("/save", HTTP_POST, handleSave);
  server.on("/action", HTTP_POST, handleAction);
  server.onNotFound(handleNotFound);
}

void portalLoop() {
  bool wanted;
  {
    hal::Guard guard;
    wanted = gShared.portalWanted;
  }

  if (wanted && !running) {
    dns.start(53, "*", WiFi.softAPIP());
    server.begin();
    running = true;
    touch();
    hal::Guard guard;
    gShared.portalRunning = true;
  } else if (!wanted && running) {
    server.stop();
    dns.stop();
    running = false;
    hal::Guard guard;
    gShared.portalRunning = false;
  }

  if (!running) return;
  server.handleClient();
  if (millis() - touchedMs > (uint32_t)SETUP_TIMEOUT_MIN * 60000u) {
    touch();
    hal::logf("setup page unused for %d minutes: switching it off", SETUP_TIMEOUT_MIN);
    postJob(JOB_PORTAL_STOP);
  }
}

#endif  // ARDUINO
