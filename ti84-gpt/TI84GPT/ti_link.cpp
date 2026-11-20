#include "ti_link.h"

#include "hal.h"

namespace ti {

// How long each step of a bit handshake may take before we give up.
static const uint32_t kPhaseTimeoutUs = 400000;

// After letting go of a line, give it this long to float back up before we
// look at the wires again (otherwise our own release could be mistaken for
// the start of the next bit).
static const uint32_t kSettleUs = 200;

// Before we start a packet, both lines must have been idle for this long.
// The ESP32 is thousands of times faster than the calculator's Z80: without
// this pause we could let go of a line (finishing a received bit) and pull it
// low again (starting our reply) before the calculator has even noticed the
// release, and it would wait forever for a release it missed.
static const uint32_t kTurnaroundUs = 1500;

// The mirror image of that problem. After the last bit of our packet we wait
// for the calculator to let go of its acknowledge line. If Wi-Fi interrupts us
// at that moment, the calculator may let go AND already pull the same line
// low again for the first bit of its reply before we look. So if the line is
// still low after this long, assume exactly that happened and move on to
// receiving; the calculator is patiently holding that first bit for us.
static const uint32_t kFinalReleaseUs = 20000;

const char *linkResultName(int rc) {
  switch (rc) {
    case LINK_OK: return "ok";
    case LINK_TIMEOUT: return "timeout";
    case LINK_CHECKSUM: return "bad checksum";
    case LINK_OVERFLOW: return "packet too big";
    case LINK_STUCK: return "lines stuck low";
    default: return "error";
  }
}

bool commandHasData(uint8_t command) {
  switch (command) {
    case CMD_VAR:
    case CMD_DATA:
    case CMD_SKIP:
    case CMD_DEL:
    case CMD_REQ:
    case CMD_RTS:
      return true;
    default:
      return false;
  }
}

// Machine ids a calculator can open a packet with. Used to notice that the
// two wires are the other way round: every bit then arrives inverted, so the
// first byte is the complement of one of these.
static bool plausibleMachineId(uint8_t id) {
  switch (id) {
    case 0x73:  // TI-83+/84+
    case 0x82:  // TI-82 / CBL mode
    case 0x83:  // TI-83
    case 0x95:  // CBL mode, lists
    case 0x23:  // computer
    case 0x03:
    case 0x02:
      return true;
    default:
      return false;
  }
}

void Link::begin(int pinA, int pinB, bool swapped) {
  pinA_ = pinA;
  pinB_ = pinB;
  hal::lineInit(pinA_);
  hal::lineInit(pinB_);
  setSwapped(swapped);
}

void Link::setSwapped(bool swapped) {
  swapped_ = swapped;
  tip_ = swapped ? pinB_ : pinA_;
  ring_ = swapped ? pinA_ : pinB_;
}

void Link::release() {
  hal::lineRelease(pinA_);
  hal::lineRelease(pinB_);
}

bool Link::idle() const { return hal::lineHigh(pinA_) && hal::lineHigh(pinB_); }

bool Link::incoming() const { return hal::lineHigh(pinA_) != hal::lineHigh(pinB_); }

bool Link::stuckLow() const { return !hal::lineHigh(pinA_) && !hal::lineHigh(pinB_); }

static void settle(int line) {
  uint32_t t0 = hal::micros();
  while (!hal::lineHigh(line) && hal::micros() - t0 < kSettleUs) {
  }
}

int Link::recvByte(uint8_t &out, uint32_t firstTimeoutUs) {
  uint8_t value = 0;
  for (int bit = 0; bit < 8; bit++) {
    const uint32_t limit = (bit == 0) ? firstTimeoutUs : kPhaseTimeoutUs;
    uint32_t t0 = hal::micros();
    bool tipHigh, ringHigh;
    for (;;) {
      tipHigh = hal::lineHigh(tip_);
      ringHigh = hal::lineHigh(ring_);
      if (tipHigh != ringHigh) {
        // Look twice so a glitch is not taken for a bit.
        if (hal::lineHigh(tip_) == tipHigh && hal::lineHigh(ring_) == ringHigh) break;
        continue;
      }
      if (hal::micros() - t0 > limit) {
        release();
        return tipHigh ? LINK_TIMEOUT : LINK_STUCK;
      }
    }

    const bool one = !ringHigh;  // ring low = 1, tip low = 0
    value = (uint8_t)((value >> 1) | (one ? 0x80 : 0x00));
    const int ackLine = one ? tip_ : ring_;
    const int srcLine = one ? ring_ : tip_;

    hal::lineLow(ackLine);
    t0 = hal::micros();
    while (!hal::lineHigh(srcLine)) {
      if (hal::micros() - t0 > kPhaseTimeoutUs) {
        release();
        return LINK_TIMEOUT;
      }
    }
    hal::lineRelease(ackLine);
    settle(ackLine);
  }
  out = value;
  return LINK_OK;
}

// Wait until both lines have stayed high for `quietUs` in a row.
bool Link::waitIdle(uint32_t quietUs) {
  const uint32_t start = hal::micros();
  uint32_t quietSince = start;
  for (;;) {
    const uint32_t now = hal::micros();
    if (!hal::lineHigh(tip_) || !hal::lineHigh(ring_)) {
      quietSince = now;
      if (now - start > kPhaseTimeoutUs) return false;
    } else if (now - quietSince >= quietUs) {
      return true;
    }
  }
}

int Link::sendByte(uint8_t value, bool lastOfPacket) {
  for (int bit = 0; bit < 8; bit++) {
    uint32_t t0 = hal::micros();
    while (!hal::lineHigh(tip_) || !hal::lineHigh(ring_)) {
      if (hal::micros() - t0 > kPhaseTimeoutUs) {
        release();
        return LINK_TIMEOUT;
      }
    }

    const bool one = (value & 1) != 0;
    const int line = one ? ring_ : tip_;
    const int ackLine = one ? tip_ : ring_;

    hal::lineLow(line);
    t0 = hal::micros();
    while (hal::lineHigh(ackLine)) {
      if (hal::micros() - t0 > kPhaseTimeoutUs) {
        release();
        return LINK_TIMEOUT;
      }
    }
    hal::lineRelease(line);
    const bool finalBit = lastOfPacket && bit == 7;
    t0 = hal::micros();
    while (!hal::lineHigh(ackLine)) {
      const uint32_t waited = hal::micros() - t0;
      if (finalBit && waited > kFinalReleaseUs) break;  // see kFinalReleaseUs
      if (waited > kPhaseTimeoutUs) {
        release();
        return LINK_TIMEOUT;
      }
    }
    value >>= 1;
  }
  return LINK_OK;
}

int Link::receive(Packet &pkt, uint8_t *data, size_t maxData, uint32_t firstTimeoutUs) {
  uint8_t header[4];
  int rc = recvByte(header[0], firstTimeoutUs);
  if (rc != LINK_OK) return rc;

  if (!plausibleMachineId(header[0]) && plausibleMachineId((uint8_t)~header[0])) {
    // Tip and ring are the other way round. The handshake itself still works
    // in that case (we acknowledge on "the other" wire either way), only the
    // bit values come out inverted - so fix this byte and carry on.
    setSwapped(!swapped_);
    header[0] = (uint8_t)~header[0];
    hal::logf("link: tip/ring were swapped, now tip=GPIO%d ring=GPIO%d", tip_, ring_);
  }
  if (plausibleMachineId(header[0])) known_ = true;

  for (int i = 1; i < 4; i++) {
    rc = recvByte(header[i], kPhaseTimeoutUs);
    if (rc != LINK_OK) return rc;
  }

  pkt.machine = header[0];
  pkt.command = header[1];
  pkt.length = (uint16_t)(header[2] | (header[3] << 8));
  pkt.dataLen = 0;

  if (!commandHasData(pkt.command) || pkt.length == 0) return LINK_OK;
  if (pkt.length > maxData) return LINK_OVERFLOW;

  uint16_t sum = 0;
  for (uint16_t i = 0; i < pkt.length; i++) {
    rc = recvByte(data[i], kPhaseTimeoutUs);
    if (rc != LINK_OK) return rc;
    sum = (uint16_t)(sum + data[i]);
  }
  uint8_t lo, hi;
  rc = recvByte(lo, kPhaseTimeoutUs);
  if (rc != LINK_OK) return rc;
  rc = recvByte(hi, kPhaseTimeoutUs);
  if (rc != LINK_OK) return rc;
  if (sum != (uint16_t)(lo | (hi << 8))) return LINK_CHECKSUM;

  pkt.dataLen = pkt.length;
  return LINK_OK;
}

int Link::send(uint8_t machine, uint8_t command, const uint8_t *data, uint16_t len) {
  if (!waitIdle(kTurnaroundUs)) {
    release();
    return LINK_TIMEOUT;
  }
  const bool hasData = (len != 0 && data != nullptr && commandHasData(command));
  const uint8_t header[4] = {machine, command, (uint8_t)(len & 0xFF), (uint8_t)(len >> 8)};
  for (int i = 0; i < 4; i++) {
    int rc = sendByte(header[i], !hasData && i == 3);
    if (rc != LINK_OK) return rc;
  }
  if (!hasData) return LINK_OK;

  uint16_t sum = 0;
  for (uint16_t i = 0; i < len; i++) {
    int rc = sendByte(data[i], false);
    if (rc != LINK_OK) return rc;
    sum = (uint16_t)(sum + data[i]);
  }
  int rc = sendByte((uint8_t)(sum & 0xFF), false);
  if (rc != LINK_OK) return rc;
  return sendByte((uint8_t)(sum >> 8), true);
}

}  // namespace ti
