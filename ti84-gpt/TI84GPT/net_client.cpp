// One HTTP or HTTPS request, start to finish.
#ifdef ARDUINO

#include <HTTPClient.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>

#include "config.h"
#include "glue.h"
#include "hal.h"

// Use the core's built-in certificate bundle when it has one (core 3.3+);
// otherwise fall back to an unverified connection. Picked at compile time, so
// the sketch builds on either.
template <typename Client>
static auto verifyWithBundle(Client &client, int) -> decltype(client.useBuiltinCACertBundle(), bool()) {
  client.useBuiltinCACertBundle();
  return true;
}

template <typename Client>
static bool verifyWithBundle(Client &client, long) {
  client.setInsecure();
  return false;
}

int httpRequest(bool post, const String &url, const String &bearer, const std::string &body,
                std::string &response, std::string &error) {
  response.clear();
  error.clear();

  WiFiClientSecure secure;
  WiFiClient plain;
  HTTPClient http;
  bool begun;
  if (url.startsWith("https://")) {
#if TLS_VERIFY
    if (!verifyWithBundle(secure, 0)) {
      hal::logf("warning: this esp32 core cannot check TLS certificates - update the esp32 boards package");
    }
#else
    secure.setInsecure();
#endif
    begun = http.begin(secure, url);
  } else {
    begun = http.begin(plain, url);
  }
  if (!begun) {
    error = "bad API address";
    return -1;
  }

  const uint32_t timeoutMs = (uint32_t)HTTP_TIMEOUT_S * 1000u;
  http.setConnectTimeout(15000);
  http.setTimeout((uint16_t)(timeoutMs > 65000u ? 65000u : timeoutMs));
  http.setReuse(false);
  if (bearer.length()) http.addHeader("Authorization", "Bearer " + bearer);

  int code;
  if (post) {
    http.addHeader("Content-Type", "application/json");
    code = http.POST((uint8_t *)body.data(), body.size());
  } else {
    code = http.GET();
  }

  if (code > 0) {
    const String payload = http.getString();
    response.assign(payload.c_str(), payload.length());
  } else {
    error = HTTPClient::errorToString(code).c_str();
  }
  http.end();
  return code;
}

#endif  // ARDUINO
