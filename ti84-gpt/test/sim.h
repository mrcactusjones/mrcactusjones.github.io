// Desktop simulation of the two link wires and of the calculator on the other
// end. Lets the firmware's protocol code run and be tested without hardware.
#pragma once
#include <stdint.h>

#include <string>
#include <vector>

namespace sim {

// ---- the wires ------------------------------------------------------------
void resetWires();
// Time a wire takes to float back up after everyone lets go of it.
void setRiseTimeUs(uint32_t us);
void calcLow(int pin);
void calcRelease(int pin);
bool level(int pin);  // true = high
uint32_t nowUs();
extern bool verbose;

// A real calculator samples the wires every few microseconds and can be away
// for a fraction of a millisecond in an interrupt. If the firmware releases a
// line and pulls it low again in less than this, the calculator may never see
// the release. The simulated calculator counts every time that happens.
const uint32_t kCalcBlindUs = 500;
extern int timingViolations;
// Number of times `pin` has gone from low back to high so far.
uint32_t riseCount(int pin);
// True if `pin` has been released and pulled low again since riseCount() was
// `risesBefore`; highUs is how long it stayed high in between.
bool pulledAgain(int pin, uint32_t risesBefore, uint32_t &highUs);
void dumpTrace();  // the last few wire events, for debugging

// ---- the calculator -------------------------------------------------------
// Speaks the calculator's side of the link protocol, written independently of
// the firmware code (from the TI link protocol documentation).
class Calc {
 public:
  Calc(int tipPin, int ringPin) : tip_(tipPin), ring_(ringPin) {}

  // Random pauses (0..maxUs) between protocol steps, like a real, slow Z80.
  void setJitterUs(uint32_t maxUs) { jitterUs_ = maxUs; }

  // TI-BASIC Send( and Get( as a TI-84 Plus performs them.
  bool sendReal(char name, double value);
  bool sendString(int strIndex, const std::vector<uint8_t> &tokens);
  bool getReal(char name, double &value);
  bool getString(int strIndex, std::vector<uint8_t> &tokens);

  // Home screen: accept one "silent" variable transfer from a computer.
  bool receiveSilent(std::string &name, uint8_t &type, std::vector<uint8_t> &data, uint32_t timeoutMs);

  // Fault injection: send only the first `bytes` bytes of a Send(real) and
  // then walk away, as if the batteries died mid-transfer.
  void sendTruncated(int bytes);

  // Raw packet access (public so tests can inject faults).
  bool sendPacket(uint8_t machine, uint8_t command, const uint8_t *data, uint16_t len,
                  bool corruptChecksum = false);
  bool recvPacket(uint8_t &machine, uint8_t &command, std::vector<uint8_t> &data, uint32_t timeoutMs);

  std::string lastError;

 private:
  bool sendByte(uint8_t value);
  bool recvByte(uint8_t &value, uint32_t timeoutUs);
  bool expect(uint8_t command, std::vector<uint8_t> &data, const char *what);
  bool sendVar(const uint8_t *header, const std::vector<uint8_t> &data);
  bool getVar(const uint8_t *header, std::vector<uint8_t> &data);
  void jitter();

  int tip_, ring_;
  uint32_t jitterUs_ = 0;
  uint32_t rng_ = 12345;
};

// Helpers shared by the tests.
void encodeReal(double value, uint8_t *real);  // independent of the firmware's encoder
double decodeReal(const uint8_t *real);
std::vector<uint8_t> typed(const std::string &text);  // what the keypad would produce

}  // namespace sim
