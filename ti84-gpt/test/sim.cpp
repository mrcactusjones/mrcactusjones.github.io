#include "sim.h"

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <chrono>
#include <mutex>
#include <thread>

#include "../TI84GPT/hal.h"

// ===========================================================================
// The wires
// ===========================================================================

namespace {

const int kPins = 32;
std::mutex gWireMutex;
bool gDeviceLow[kPins];
bool gCalcLow[kPins];
uint32_t gReleasedAt[kPins];
uint32_t gRiseCount[kPins];
uint32_t gLastPulseUs[kPins];
uint32_t gRiseUs = 0;
std::mutex gStateMutex;
const auto gStart = std::chrono::steady_clock::now();

struct Event {
  uint32_t t;
  char who;
  int pin;
  bool low;
};
Event gTrace[64];
unsigned gTraceCount = 0;

void setDriver(bool *drivers, int pin, bool low) {
  std::lock_guard<std::mutex> guard(gWireMutex);
  gTrace[gTraceCount++ % 64] = Event{sim::nowUs(), drivers == gCalcLow ? 'C' : 'F', pin, low};
  const bool wasDriven = gDeviceLow[pin] || gCalcLow[pin];
  drivers[pin] = low;
  const bool isDriven = gDeviceLow[pin] || gCalcLow[pin];
  const uint32_t now = sim::nowUs();
  if (wasDriven && !isDriven) {
    gReleasedAt[pin] = now;
    gRiseCount[pin]++;
  } else if (!wasDriven && isDriven) {
    const uint32_t highSince = gReleasedAt[pin] + gRiseUs;
    gLastPulseUs[pin] = ((int32_t)(now - highSince) > 0) ? now - highSince : 0;
  }
}

}  // namespace

namespace sim {

bool verbose = false;
int timingViolations = 0;

void dumpTrace() {
  std::lock_guard<std::mutex> guard(gWireMutex);
  const unsigned n = gTraceCount < 64 ? gTraceCount : 64;
  for (unsigned i = 0; i < n; i++) {
    const Event &e = gTrace[(gTraceCount - n + i) % 64];
    printf("      %10u us  %s pin %d %s\n", (unsigned)e.t, e.who == 'C' ? "calc    " : "firmware", e.pin,
           e.low ? "LOW" : "release");
  }
}

uint32_t riseCount(int pin) {
  std::lock_guard<std::mutex> guard(gWireMutex);
  return gRiseCount[pin];
}

bool pulledAgain(int pin, uint32_t risesBefore, uint32_t &highUs) {
  std::lock_guard<std::mutex> guard(gWireMutex);
  if (!(gDeviceLow[pin] || gCalcLow[pin]) || gRiseCount[pin] == risesBefore) return false;
  highUs = gLastPulseUs[pin];
  return true;
}

uint32_t nowUs() {
  return (uint32_t)std::chrono::duration_cast<std::chrono::microseconds>(
             std::chrono::steady_clock::now() - gStart)
      .count();
}

void resetWires() {
  std::lock_guard<std::mutex> guard(gWireMutex);
  memset(gDeviceLow, 0, sizeof(gDeviceLow));
  memset(gCalcLow, 0, sizeof(gCalcLow));
  memset(gReleasedAt, 0, sizeof(gReleasedAt));
  memset(gRiseCount, 0, sizeof(gRiseCount));
  memset(gLastPulseUs, 0, sizeof(gLastPulseUs));
}

void setRiseTimeUs(uint32_t us) { gRiseUs = us; }

void calcLow(int pin) { setDriver(gCalcLow, pin, true); }
void calcRelease(int pin) { setDriver(gCalcLow, pin, false); }

bool level(int pin) {
  std::lock_guard<std::mutex> guard(gWireMutex);
  if (gDeviceLow[pin] || gCalcLow[pin]) return false;
  return nowUs() - gReleasedAt[pin] >= gRiseUs;
}

}  // namespace sim

// The firmware's hardware layer, backed by the simulated wires.
namespace hal {

void lineInit(int pin) { setDriver(gDeviceLow, pin, false); }
void lineLow(int pin) { setDriver(gDeviceLow, pin, true); }
void lineRelease(int pin) { setDriver(gDeviceLow, pin, false); }
bool lineHigh(int pin) { return sim::level(pin); }
uint32_t micros() { return sim::nowUs(); }
uint32_t millis() { return sim::nowUs() / 1000; }
void sleepMs(uint32_t ms) { std::this_thread::sleep_for(std::chrono::milliseconds(ms)); }
void lock() { gStateMutex.lock(); }
void unlock() { gStateMutex.unlock(); }

void logf(const char *fmt, ...) {
  if (!sim::verbose) return;
  va_list args;
  va_start(args, fmt);
  printf("    [fw] ");
  vprintf(fmt, args);
  printf("\n");
  va_end(args);
}

}  // namespace hal

// ===========================================================================
// The calculator
// ===========================================================================

namespace sim {

static const uint32_t kStepTimeoutUs = 2000000;

void Calc::jitter() {
  if (!jitterUs_) return;
  rng_ = rng_ * 1664525u + 1013904223u;
  const uint32_t wait = (rng_ >> 8) % (jitterUs_ + 1);
  const uint32_t t0 = nowUs();
  while (nowUs() - t0 < wait) {
  }
}

// Sender: pull TIP for a 0 or RING for a 1, wait for the other line to be
// pulled by the receiver, let go, wait for the receiver to let go.
bool Calc::sendByte(uint8_t value) {
  for (int bit = 0; bit < 8; bit++) {
    jitter();
    uint32_t t0 = nowUs();
    while (!level(tip_) || !level(ring_)) {
      if (nowUs() - t0 > kStepTimeoutUs) {
        lastError = "lines never went idle";
        return false;
      }
    }
    const bool one = (value >> bit) & 1;
    const int mine = one ? ring_ : tip_;
    const int theirs = one ? tip_ : ring_;
    calcLow(mine);
    t0 = nowUs();
    while (level(theirs)) {
      if (nowUs() - t0 > kStepTimeoutUs) {
        calcRelease(mine);
        lastError = "no acknowledge for a bit";
        return false;
      }
    }
    const uint32_t risesBefore = riseCount(theirs);  // taken while the line is low
    jitter();
    calcRelease(mine);
    t0 = nowUs();
    while (!level(theirs)) {
      uint32_t highUs = 0;
      if (pulledAgain(theirs, risesBefore, highUs)) {
        // The firmware let go and pulled the line low again while this thread
        // was not looking. Whether a real calculator would have caught that
        // depends only on how long the line stayed high.
        if (highUs < kCalcBlindUs) {
          if (timingViolations++ == 0) dumpTrace();
          printf("  TIMING: firmware released its acknowledge for only %u us before pulling the same "
                 "line low again\n", (unsigned)highUs);
        }
        break;
      }
      if (nowUs() - t0 > kStepTimeoutUs) {
        lastError = "acknowledge never released";
        return false;
      }
    }
  }
  return true;
}

bool Calc::recvByte(uint8_t &value, uint32_t timeoutUs) {
  value = 0;
  for (int bit = 0; bit < 8; bit++) {
    uint32_t t0 = nowUs();
    bool tipHigh, ringHigh;
    for (;;) {
      tipHigh = level(tip_);
      ringHigh = level(ring_);
      if (tipHigh != ringHigh) break;
      if (nowUs() - t0 > (bit == 0 ? timeoutUs : kStepTimeoutUs)) {
        lastError = "nothing received";
        return false;
      }
    }
    const bool one = !ringHigh;
    if (one) value |= (uint8_t)(1 << bit);
    const int ack = one ? tip_ : ring_;
    const int src = one ? ring_ : tip_;
    jitter();
    calcLow(ack);
    t0 = nowUs();
    while (!level(src)) {
      if (nowUs() - t0 > kStepTimeoutUs) {
        calcRelease(ack);
        lastError = "sender never released its line";
        return false;
      }
    }
    jitter();
    calcRelease(ack);
    // A real calculator's own line needs a moment to rise before it looks again.
    t0 = nowUs();
    while (!level(ack) && nowUs() - t0 < 200) {
    }
  }
  return true;
}

static bool hasData(uint8_t command) {
  return command == 0x06 || command == 0x15 || command == 0x36 || command == 0x88 || command == 0xA2 ||
         command == 0xC9;
}

bool Calc::sendPacket(uint8_t machine, uint8_t command, const uint8_t *data, uint16_t len,
                      bool corruptChecksum) {
  const uint8_t header[4] = {machine, command, (uint8_t)(len & 0xFF), (uint8_t)(len >> 8)};
  for (int i = 0; i < 4; i++) {
    if (!sendByte(header[i])) return false;
  }
  if (!hasData(command) || len == 0) return true;
  uint16_t sum = 0;
  for (uint16_t i = 0; i < len; i++) {
    if (!sendByte(data[i])) return false;
    sum = (uint16_t)(sum + data[i]);
  }
  if (corruptChecksum) sum ^= 0x0101;
  return sendByte((uint8_t)(sum & 0xFF)) && sendByte((uint8_t)(sum >> 8));
}

bool Calc::recvPacket(uint8_t &machine, uint8_t &command, std::vector<uint8_t> &data, uint32_t timeoutMs) {
  uint8_t header[4];
  data.clear();
  for (int i = 0; i < 4; i++) {
    if (!recvByte(header[i], i == 0 ? timeoutMs * 1000u : kStepTimeoutUs)) return false;
  }
  machine = header[0];
  command = header[1];
  const uint16_t len = (uint16_t)(header[2] | (header[3] << 8));
  if (!hasData(command) || len == 0) return true;
  uint16_t sum = 0;
  for (uint16_t i = 0; i < len; i++) {
    uint8_t b;
    if (!recvByte(b, kStepTimeoutUs)) return false;
    data.push_back(b);
    sum = (uint16_t)(sum + b);
  }
  uint8_t lo, hi;
  if (!recvByte(lo, kStepTimeoutUs) || !recvByte(hi, kStepTimeoutUs)) return false;
  if (sum != (uint16_t)(lo | (hi << 8))) {
    lastError = "bad checksum from the firmware";
    return false;
  }
  return true;
}

bool Calc::expect(uint8_t command, std::vector<uint8_t> &data, const char *what) {
  uint8_t machine, got;
  if (!recvPacket(machine, got, data, 2000)) {
    lastError = std::string("waiting for ") + what + ": " + lastError;
    return false;
  }
  if (got != command) {
    char buf[96];
    snprintf(buf, sizeof(buf), "expected %s, got command 0x%02X from 0x%02X", what, got, machine);
    lastError = buf;
    return false;
  }
  return true;
}

// Send( : RTS -> ACK, CTS -> ACK -> DATA -> ACK -> EOT -> ACK
bool Calc::sendVar(const uint8_t *header, const std::vector<uint8_t> &data) {
  std::vector<uint8_t> scratch;
  if (!sendPacket(0x82, 0xC9, header, 11)) return false;
  if (!expect(0x56, scratch, "ACK of RTS")) return false;
  if (!expect(0x09, scratch, "CTS")) return false;
  if (!sendPacket(0x82, 0x56, nullptr, 0)) return false;
  if (!sendPacket(0x82, 0x15, data.data(), (uint16_t)data.size())) return false;
  if (!expect(0x56, scratch, "ACK of DATA")) return false;
  if (!sendPacket(0x82, 0x92, nullptr, 0)) return false;
  return expect(0x56, scratch, "ACK of EOT");
}

// Get( : REQ -> ACK, VAR -> ACK, CTS -> ACK, DATA -> ACK
bool Calc::getVar(const uint8_t *header, std::vector<uint8_t> &data) {
  std::vector<uint8_t> reply;
  if (!sendPacket(0x82, 0xA2, header, 11)) return false;
  if (!expect(0x56, reply, "ACK of REQ")) return false;
  if (!expect(0x06, reply, "VAR")) return false;
  if (reply.size() < 11) {
    lastError = "VAR header too short";
    return false;
  }
  const size_t announced = (size_t)(reply[0] | (reply[1] << 8));
  if (!sendPacket(0x82, 0x56, nullptr, 0)) return false;
  if (!sendPacket(0x82, 0x09, nullptr, 0)) return false;
  std::vector<uint8_t> scratch;
  if (!expect(0x56, scratch, "ACK of CTS")) return false;
  if (!expect(0x15, data, "DATA")) return false;
  if (data.size() != announced) {
    lastError = "DATA size differs from the size announced in VAR";
    return false;
  }
  return sendPacket(0x82, 0x56, nullptr, 0);
}

// A TI-84 Plus in CBL mode sends strings with type 0 and only the name
// (0xAA, index) gives them away.
static void realHeader(uint8_t *header, char name) {
  memset(header, 0, 11);
  header[0] = 9;
  header[2] = 0x00;
  header[3] = (uint8_t)name;
}

static void stringHeader(uint8_t *header, int strIndex, size_t size) {
  memset(header, 0, 11);
  header[0] = (uint8_t)(size & 0xFF);
  header[1] = (uint8_t)(size >> 8);
  header[2] = 0x00;
  header[3] = 0xAA;
  header[4] = (uint8_t)(strIndex == 0 ? 9 : strIndex - 1);
}

bool Calc::sendReal(char name, double value) {
  uint8_t header[11];
  realHeader(header, name);
  std::vector<uint8_t> data(9);
  encodeReal(value, data.data());
  return sendVar(header, data);
}

bool Calc::sendString(int strIndex, const std::vector<uint8_t> &tokens) {
  std::vector<uint8_t> data(tokens.size() + 2);
  data[0] = (uint8_t)(tokens.size() & 0xFF);
  data[1] = (uint8_t)(tokens.size() >> 8);
  if (!tokens.empty()) memcpy(data.data() + 2, tokens.data(), tokens.size());
  uint8_t header[11];
  stringHeader(header, strIndex, data.size());
  return sendVar(header, data);
}

bool Calc::getReal(char name, double &value) {
  uint8_t header[11];
  realHeader(header, name);
  std::vector<uint8_t> data;
  if (!getVar(header, data)) return false;
  if (data.size() != 9) {
    lastError = "real variable is not 9 bytes";
    return false;
  }
  value = decodeReal(data.data());
  return true;
}

bool Calc::getString(int strIndex, std::vector<uint8_t> &tokens) {
  uint8_t header[11];
  stringHeader(header, strIndex, 0);
  std::vector<uint8_t> data;
  if (!getVar(header, data)) return false;
  if (data.size() < 2) {
    lastError = "string variable too short";
    return false;
  }
  const size_t len = (size_t)(data[0] | (data[1] << 8));
  if (len != data.size() - 2) {
    lastError = "string length word does not match its data";
    return false;
  }
  tokens.assign(data.begin() + 2, data.end());
  return true;
}

void Calc::sendTruncated(int bytes) {
  uint8_t header[11];
  realHeader(header, 'C');
  const uint8_t packet[4] = {0x82, 0xC9, 11, 0};
  for (int i = 0; i < bytes; i++) {
    if (!sendByte(i < 4 ? packet[i] : header[i - 4])) return;
  }
}

// Silent receive: RTS(0x23) -> ACK, CTS -> ACK -> DATA -> ACK -> EOT
bool Calc::receiveSilent(std::string &name, uint8_t &type, std::vector<uint8_t> &data, uint32_t timeoutMs) {
  uint8_t machine, command;
  std::vector<uint8_t> header, scratch;
  if (!recvPacket(machine, command, header, timeoutMs)) return false;
  if (machine != 0x23 || command != 0xC9 || header.size() != 13) {
    char buf[96];
    snprintf(buf, sizeof(buf), "expected RTS from a computer, got 0x%02X/0x%02X len %u", machine, command,
             (unsigned)header.size());
    lastError = buf;
    return false;
  }
  const size_t size = (size_t)(header[0] | (header[1] << 8));
  type = header[2];
  name.clear();
  for (int i = 0; i < 8 && header[3 + i]; i++) name += (char)header[3 + i];
  if (!sendPacket(0x73, 0x56, nullptr, 0)) return false;
  if (!sendPacket(0x73, 0x09, nullptr, 0)) return false;
  if (!recvPacket(machine, command, scratch, 2000) || command != 0x56) {
    lastError = "no ACK after CTS";
    return false;
  }
  if (!recvPacket(machine, command, data, 2000) || command != 0x15) {
    lastError = "no DATA";
    return false;
  }
  if (data.size() != size) {
    lastError = "DATA size differs from the RTS header";
    return false;
  }
  if (!sendPacket(0x73, 0x56, nullptr, 0)) return false;
  if (!recvPacket(machine, command, scratch, 2000) || command != 0x92) {
    lastError = "no EOT";
    return false;
  }
  return true;
}

// ---------------------------------------------------------------------------

void encodeReal(double value, uint8_t *real) {
  memset(real, 0, 9);
  real[1] = 0x80;
  if (value == 0) return;
  if (value < 0) {
    real[0] = 0x80;
    value = -value;
  }
  char text[40];
  snprintf(text, sizeof(text), "%.13e", value);  // d.ddddddddddddde+XX
  int exponent = atoi(strchr(text, 'e') + 1);
  real[1] = (uint8_t)(0x80 + exponent);
  int n = 0;
  for (const char *p = text; *p && *p != 'e' && n < 14; p++) {
    if (*p < '0' || *p > '9') continue;
    const uint8_t digit = (uint8_t)(*p - '0');
    real[2 + n / 2] |= (n % 2) ? digit : (uint8_t)(digit << 4);
    n++;
  }
}

double decodeReal(const uint8_t *real) {
  char text[40];
  int n = 0;
  for (int i = 0; i < 14; i++) {
    const uint8_t pair = real[2 + i / 2];
    text[n++] = (char)('0' + ((i % 2) ? (pair & 0x0F) : (pair >> 4)));
    if (i == 0) text[n++] = '.';
  }
  snprintf(text + n, sizeof(text) - (size_t)n, "e%d", (int)real[1] - 0x80);
  const double value = atof(text);
  return (real[0] & 0x80) ? -value : value;
}

std::vector<uint8_t> typed(const std::string &text) {
  std::vector<uint8_t> tokens;
  for (size_t i = 0; i < text.size(); i++) {
    const char c = text[i];
    if ((c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9')) {
      tokens.push_back((uint8_t)c);
      continue;
    }
    switch (c) {
      case ' ': tokens.push_back(0x29); break;
      case '+': tokens.push_back(0x70); break;
      case '-': tokens.push_back(0x71); break;
      case '*': tokens.push_back(0x82); break;
      case '/': tokens.push_back(0x83); break;
      case '=': tokens.push_back(0x6A); break;
      case '?': tokens.push_back(0xAF); break;
      case '(': tokens.push_back(0x10); break;
      case ')': tokens.push_back(0x11); break;
      case '.': tokens.push_back(0x3A); break;
      case ',': tokens.push_back(0x2B); break;
      case '^': tokens.push_back(0xF0); break;
      default:
        fprintf(stderr, "sim::typed: no key for '%c'\n", c);
        abort();
    }
  }
  return tokens;
}

}  // namespace sim
