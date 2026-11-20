// The calculator-facing side of the firmware.
//
// The calculator program talks to us with the TI-BASIC Send( and Get(
// commands, exactly as it would talk to a CBL data logger:
//
//   Send(Str1)   text for the next command (the question)
//   Send(C)      run command C (see CommandCode)
//   Get(S)       0 while we are still working, 1 when the result is ready
//   Get(N)       number of screens in the result
//   Send(P)      choose screen P (1..N)
//   Get(Str0)    the chosen screen: 8 rows x 16 columns, ready for Output(1,1,
//
// Every command produces a "document" of one or more screens, including
// errors, so the calculator program needs only one display loop.
#pragma once
#include <stddef.h>
#include <stdint.h>

#include <string>
#include <vector>

#include "text_layout.h"
#include "ti_link.h"

namespace app {

enum CommandCode {
  COMMAND_ASK = 1,      // new conversation, question in the last string sent
  COMMAND_REPLY = 2,    // follow-up in the same conversation
  COMMAND_SOLVE = 3,    // new conversation using the math-solver prompt
  COMMAND_STATUS = 4,   // Wi-Fi / version / wiring report
  COMMAND_INSTALL = 5,  // (re)send the calculator program
  COMMAND_SETUP = 6,    // start the phone setup page
};

enum FooterKind {
  FOOTER_CHAT,  // last screen offers "+=REPLY"
  FOOTER_INFO,  // last screen offers "CLEAR=MENU"
};

const int kScreenCols = 16;
const int kTextRows = 7;  // the 8th row is the footer

// Implemented by the sketch (and by the desktop tests).
class Handler {
 public:
  // The calculator sent a command. Called from the link loop, so it must
  // return quickly: start the work and later call CalcApp::finish() with the
  // same ticket (from any task).
  virtual void onCommand(int command, const std::string &text, uint32_t ticket) = 0;
  // Called once the firmware knows which data wire is tip and which is ring.
  virtual void onWiringLearned(bool swapped) { (void)swapped; }
  virtual ~Handler() {}
};

enum SendResult {
  SEND_OK = 0,
  SEND_NO_ANSWER = 1,  // calculator not listening (off, or not on the home screen)
  SEND_REFUSED = 2,    // calculator said no (memory full, variable locked...)
  SEND_FAILED = 3,     // transfer broke off part-way
};

const char *sendResultText(int result);

struct Stats {
  uint32_t packets = 0;      // good packets from the calculator
  uint32_t errors = 0;       // receive attempts that failed
  uint32_t commands = 0;     // Send(C) commands seen
  int lastError = 0;         // last ti::LinkResult that was not OK
  uint32_t lastPacketMs = 0;  // hal::millis() of the last good packet
};

class CalcApp {
 public:
  void begin(ti::Link *link, Handler *handler, bool lowercase);
  void setLowercase(bool lowercase);

  // Service the link. Call as often as possible; returns true if a packet was
  // handled (so the caller knows not to sleep yet).
  bool poll();

  // Publish the result of a command. Ignored if a newer command has started
  // since `ticket` was issued. Safe to call from another task.
  void finish(uint32_t ticket, const std::string &text, FooterKind footer);

  bool busy();

  // Push a program to the calculator with a "silent" transfer. The calculator
  // must be switched on and sitting at its home screen. `tokens` is the
  // tokenized program body (without the 2-byte length).
  int sendProgram(const char *name, const uint8_t *tokens, size_t len);

  Stats stats();

 private:
  void handlePacket(const ti::Packet &pkt);
  void onVariableReceived(uint16_t dataLen);
  void buildReply(uint8_t type, const uint8_t *name, uint8_t *header);
  void normalizeHeader();
  int sendProgramOnce(const char *name, const uint8_t *tokens, size_t len);
  std::string footerFor(int page, int pages, FooterKind kind) const;

  ti::Link *link_ = nullptr;
  Handler *handler_ = nullptr;

  uint8_t rx_[2304];       // data of the packet being received
  uint8_t varHeader_[16];  // variable header from the last RTS / REQ
  uint8_t varHeaderLen_ = 0;
  std::vector<uint8_t> tx_;  // data waiting for the calculator's CTS
  bool wiringReported_ = false;
  bool reportedSwapped_ = false;
  bool backingOff_ = false;
  uint32_t backoffStartMs_ = 0;

  // --- shared with other tasks, protected by hal::lock() ---
  bool lowercase_ = true;
  bool busy_ = false;
  uint32_t ticket_ = 0;
  std::string argText_;
  std::vector<layout::Line> lines_;
  FooterKind footerKind_ = FOOTER_INFO;
  int page_ = 1;
  Stats stats_;
};

}  // namespace app
