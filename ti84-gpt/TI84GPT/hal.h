// Tiny hardware layer so the protocol code can run both on the ESP32 and in
// the desktop test harness (test/ folder).
#pragma once
#include <stddef.h>
#include <stdint.h>

namespace hal {

// Link lines are open-drain: we either pull a line to ground or let go of it.
void lineInit(int pin);     // released, weak pull-up on
void lineLow(int pin);      // pull to ground
void lineRelease(int pin);  // let go
bool lineHigh(int pin);     // read the actual level on the wire

uint32_t micros();
uint32_t millis();
void sleepMs(uint32_t ms);
// Called inside wait loops that have already been waiting a while: gives the
// other tasks (and the watchdog) a turn. May take up to a millisecond.
void relax();

// Lock protecting state shared between the link loop and the network task.
void lock();
void unlock();

void logf(const char *fmt, ...) __attribute__((format(printf, 1, 2)));

struct Guard {
  Guard() { lock(); }
  ~Guard() { unlock(); }
  Guard(const Guard &) = delete;
  Guard &operator=(const Guard &) = delete;
};

}  // namespace hal
