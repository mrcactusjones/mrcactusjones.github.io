// TI graph-link protocol over the calculator's 2.5 mm I/O port.
//
// Physical layer: two open-collector lines, "tip" (red) and "ring" (white),
// both idle high. The sender pulls TIP low for a 0 bit or RING low for a 1
// bit; the receiver answers by pulling the other line low; both let go. Bits
// go out least-significant first. Every bit is handshaked, so neither side
// needs precise timing.
//
// Packet layer: [machine id][command][length lo][length hi] and, for the
// commands that carry data, [data ...][checksum lo][checksum hi].
#pragma once
#include <stddef.h>
#include <stdint.h>

namespace ti {

enum MachineId : uint8_t {
  ID_COMPUTER_83P = 0x23,  // a computer talking to a TI-83+/84+
  ID_CALC_83P = 0x73,      // a TI-83+/84+ talking to a computer / calculator
  ID_CALC_82 = 0x82,       // what a TI-83+/84+ uses for Send( / Get(
  ID_CALC_85 = 0x95,       // ... and for Send( / Get( with lists
  ID_CBL_82 = 0x12,        // CBL answering 0x82
  ID_CBL_85 = 0x15,        // CBL answering 0x95
};

enum Command : uint8_t {
  CMD_VAR = 0x06,
  CMD_CTS = 0x09,
  CMD_DATA = 0x15,
  CMD_VER = 0x2D,
  CMD_SKIP = 0x36,  // also EXIT
  CMD_ACK = 0x56,
  CMD_ERR = 0x5A,
  CMD_RDY = 0x68,
  CMD_SCR = 0x6D,
  CMD_KEY = 0x87,
  CMD_DEL = 0x88,
  CMD_EOT = 0x92,
  CMD_REQ = 0xA2,
  CMD_RTS = 0xC9,
};

enum LinkResult {
  LINK_OK = 0,
  LINK_TIMEOUT = -1,   // the other side stopped answering
  LINK_CHECKSUM = -2,  // data arrived damaged
  LINK_OVERFLOW = -3,  // packet larger than our buffer
  LINK_STUCK = -4,     // both lines held low (calculator off?)
};

const char *linkResultName(int rc);

struct Packet {
  uint8_t machine = 0;
  uint8_t command = 0;
  uint16_t length = 0;   // raw length field from the header
  uint16_t dataLen = 0;  // data bytes actually received
};

// True for commands that are followed by data and a checksum.
bool commandHasData(uint8_t command);

class Link {
 public:
  // pinA/pinB are the two data wires in either order. `swapped` selects which
  // one is treated as tip: false = pinA is tip, true = pinB is tip.
  void begin(int pinA, int pinB, bool swapped);

  bool swapped() const { return swapped_; }
  void setSwapped(bool swapped);
  // Becomes true once a packet from the calculator has confirmed which wire
  // is which.
  bool orientationKnown() const { return known_; }
  void setOrientationKnown(bool known) { known_ = known; }

  int tipPin() const { return tip_; }
  int ringPin() const { return ring_; }

  bool idle() const;      // both lines high: calculator on, nothing happening
  bool incoming() const;  // exactly one line low: the calculator is sending
  bool stuckLow() const;  // both low: calculator off or unplugged

  // Receive one packet. `firstTimeoutUs` is how long to wait for it to start.
  int receive(Packet &pkt, uint8_t *data, size_t maxData, uint32_t firstTimeoutUs);

  // Send one packet. With len == 0 only the 4 header bytes go out.
  int send(uint8_t machine, uint8_t command, const uint8_t *data, uint16_t len);

  void release();

 private:
  int recvByte(uint8_t &out, uint32_t firstTimeoutUs);
  int sendByte(uint8_t value, bool lastOfPacket);
  bool waitIdle(uint32_t quietUs);

  int pinA_ = -1, pinB_ = -1;
  int tip_ = -1, ring_ = -1;
  bool swapped_ = false;
  bool known_ = false;
};

}  // namespace ti
