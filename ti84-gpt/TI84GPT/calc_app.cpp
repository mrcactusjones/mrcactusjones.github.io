#include "calc_app.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

#include "hal.h"
#include "ti_tokens.h"
#include "ti_vars.h"

namespace app {

static const uint32_t kProtocolVersion = 1;
static const size_t kMaxGlyphs = 4000;  // about 35 screens

const char *sendResultText(int result) {
  switch (result) {
    case SEND_OK: return "sent";
    case SEND_NO_ANSWER: return "calculator did not answer (is it on, at the home screen?)";
    case SEND_REFUSED: return "calculator refused the program (memory full or program archived)";
    default: return "transfer broke off";
  }
}

void CalcApp::begin(ti::Link *link, Handler *handler, bool lowercase) {
  link_ = link;
  handler_ = handler;
  lowercase_ = lowercase;
  layout::layoutText("TI-84 GPT ready.", lowercase_, kScreenCols, kMaxGlyphs, lines_);
}

void CalcApp::setLowercase(bool lowercase) {
  hal::Guard guard;
  lowercase_ = lowercase;
}

bool CalcApp::busy() {
  hal::Guard guard;
  return busy_;
}

Stats CalcApp::stats() {
  hal::Guard guard;
  return stats_;
}

bool CalcApp::poll() {
  if (backingOff_) {
    // After a failed receive, wait for the wires to go quiet (or 200 ms)
    // before trying again, so a wire that is stuck low - calculator switched
    // off, plug half out - does not keep us busy for ever.
    if (!link_->idle() && hal::millis() - backoffStartMs_ < 200) return false;
    backingOff_ = false;
  }
  if (!link_->incoming()) return false;

  ti::Packet pkt;
  const int rc = link_->receive(pkt, rx_, sizeof(rx_), 50000);
  if (rc != ti::LINK_OK) {
    backingOff_ = true;
    backoffStartMs_ = hal::millis();
    if (debug_) hal::logf("link: receive failed (%s)", ti::linkResultName(rc));
    hal::Guard guard;
    stats_.errors++;
    stats_.lastError = rc;
    return true;
  }
  {
    hal::Guard guard;
    stats_.packets++;
    stats_.lastPacketMs = hal::millis();
  }
  if (debug_) {
    // Printing takes a moment; the calculator simply waits a little longer
    // for its answer.
    if (pkt.dataLen >= 4 && (pkt.command == ti::CMD_RTS || pkt.command == ti::CMD_REQ)) {
      hal::logf("link: 0x%02X %s: variable type 0x%02X name %02X %02X, %u bytes", pkt.machine,
                pkt.command == ti::CMD_RTS ? "sends" : "asks for", rx_[2], rx_[3],
                pkt.dataLen > 4 ? rx_[4] : 0, (unsigned)(rx_[0] | (rx_[1] << 8)));
    } else {
      hal::logf("link: 0x%02X command 0x%02X, %u data bytes", pkt.machine, pkt.command, (unsigned)pkt.dataLen);
    }
  }

  if (link_->orientationKnown() && (!wiringReported_ || reportedSwapped_ != link_->swapped())) {
    wiringReported_ = true;
    reportedSwapped_ = link_->swapped();
    handler_->onWiringLearned(reportedSwapped_);
  }

  handlePacket(pkt);
  return true;
}

// A TI-84 Plus in "CBL mode" describes some variables oddly: strings, pictures
// and Y= variables arrive with type 0 and are only recognisable by the first
// byte of their name.
void CalcApp::normalizeHeader() {
  if (varHeaderLen_ < 4) return;
  uint8_t &type = varHeader_[2];
  const uint8_t name0 = varHeader_[3];
  if (type == ti::VAR_STRING && name0 == ti::VAR_LIST) {
    type = ti::VAR_LIST;
  } else if (type == ti::VAR_REAL && name0 == ti::TOK_STR_LEAD) {
    type = ti::VAR_STRING;
  } else if (type == ti::VAR_REAL && name0 == ti::TOK_PIC_LEAD) {
    type = ti::VAR_PICTURE;
  } else if (type == ti::VAR_REAL && name0 == 0x5E) {
    type = ti::VAR_YVAR;
  }
}

void CalcApp::handlePacket(const ti::Packet &pkt) {
  uint8_t me;
  switch (pkt.machine) {
    case ti::ID_CALC_82: me = ti::ID_CBL_82; break;
    case ti::ID_CALC_85: me = ti::ID_CBL_85; break;
    default: return;  // not meant for a CBL: stay quiet
  }

  switch (pkt.command) {
    case ti::CMD_RTS:  // Send( step 1: "I want to send this variable"
      varHeaderLen_ = (uint8_t)((pkt.dataLen < sizeof(varHeader_)) ? pkt.dataLen : sizeof(varHeader_));
      memset(varHeader_, 0, sizeof(varHeader_));
      memcpy(varHeader_, rx_, varHeaderLen_);
      normalizeHeader();
      if (link_->send(me, ti::CMD_ACK, nullptr, 0) != ti::LINK_OK) return;
      link_->send(me, ti::CMD_CTS, nullptr, 0);
      break;

    case ti::CMD_DATA:  // Send( step 2: the variable's contents
      if (link_->send(me, ti::CMD_ACK, nullptr, 0) != ti::LINK_OK) return;
      if (pkt.machine == ti::ID_CALC_82) onVariableReceived(pkt.dataLen);
      break;

    case ti::CMD_EOT:
      link_->send(me, ti::CMD_ACK, nullptr, 0);
      break;

    case ti::CMD_REQ: {  // Get( step 1: "give me this variable"
      varHeaderLen_ = (uint8_t)((pkt.dataLen < sizeof(varHeader_)) ? pkt.dataLen : sizeof(varHeader_));
      memset(varHeader_, 0, sizeof(varHeader_));
      memcpy(varHeader_, rx_, varHeaderLen_);
      normalizeHeader();
      if (link_->send(me, ti::CMD_ACK, nullptr, 0) != ti::LINK_OK) return;
      uint8_t reply[13];
      buildReply(varHeader_[2], &varHeader_[3], reply);
      link_->send(me, ti::CMD_VAR, reply, sizeof(reply));
      break;
    }

    case ti::CMD_CTS:  // Get( step 2: "go ahead"
      if (link_->send(me, ti::CMD_ACK, nullptr, 0) != ti::LINK_OK) return;
      link_->send(me, ti::CMD_DATA, tx_.data(), (uint16_t)tx_.size());
      break;

    default:  // ACKs and anything else need no answer
      break;
  }
}

void CalcApp::onVariableReceived(uint16_t dataLen) {
  const uint8_t type = varHeader_[2];
  const uint8_t name0 = varHeader_[3];

  if (type == ti::VAR_STRING && dataLen >= 2) {
    size_t len = (size_t)(rx_[0] | (rx_[1] << 8));
    if (len > (size_t)dataLen - 2) len = (size_t)dataLen - 2;
    const std::string text = ti::detokenize(rx_ + 2, len);
    hal::Guard guard;
    argText_ = text;
    return;
  }

  if (type != ti::VAR_REAL || dataLen < ti::kRealSize) {
    if (debug_) hal::logf("link: ignoring variable of type 0x%02X (%u bytes)", type, (unsigned)dataLen);
    return;
  }
  const long value = lround(ti::realToDouble(rx_));

  if (name0 == 'P') {
    hal::Guard guard;
    page_ = (int)value;
    return;
  }

  if (name0 == 'C') {
    uint32_t ticket;
    std::string text;
    std::vector<layout::Line> working;
    layout::layoutText("Working...", false, kScreenCols, kMaxGlyphs, working);
    {
      hal::Guard guard;
      ticket = ++ticket_;
      busy_ = true;
      page_ = 1;
      footerKind_ = FOOTER_INFO;
      lines_.swap(working);
      text = argText_;
      stats_.commands++;
    }
    handler_->onCommand((int)value, text, ticket);
  }
}

std::string CalcApp::footerFor(int page, int pages, FooterKind kind) const {
  char left[12];
  snprintf(left, sizeof(left), "%d/%d", page, pages);
  const char *right = (page < pages) ? "ENTER=MORE" : (kind == FOOTER_CHAT ? "+=REPLY" : "CLEAR=MENU");
  std::string footer(left);
  const size_t used = footer.size() + strlen(right);
  footer.append(used < (size_t)kScreenCols ? (size_t)kScreenCols - used : 1, ' ');
  footer += right;
  return footer;
}

// Prepare the answer to Get(...): fills tx_ with the variable's data and
// `header` with the 13-byte variable header that announces it.
void CalcApp::buildReply(uint8_t type, const uint8_t *name, uint8_t *header) {
  memset(header, 0, 13);

  if (type == ti::VAR_STRING) {
    std::vector<uint8_t> tokens;
    {
      hal::Guard guard;
      const int pages = layout::pageCount(lines_.size(), kTextRows);
      if (page_ < 1) page_ = 1;
      if (page_ > pages) page_ = pages;
      layout::renderPage(lines_, page_, kScreenCols, kTextRows, footerFor(page_, pages, footerKind_),
                         tokens);
    }
    tx_.resize(tokens.size() + 2);
    tx_[0] = (uint8_t)(tokens.size() & 0xFF);
    tx_[1] = (uint8_t)(tokens.size() >> 8);
    memcpy(tx_.data() + 2, tokens.data(), tokens.size());
    header[2] = ti::VAR_STRING;
    header[3] = ti::TOK_STR_LEAD;
    header[4] = name[1];  // answer as the same StrN that was asked for
  } else {
    long value = 0;
    {
      hal::Guard guard;
      switch (name[0]) {
        case 'S': value = busy_ ? 0 : 1; break;
        case 'N': value = layout::pageCount(lines_.size(), kTextRows); break;
        case 'P': value = page_; break;
        case 'V': value = (long)kProtocolVersion; break;
        default: value = 0; break;
      }
    }
    tx_.resize(ti::kRealSize);
    ti::intToReal(value, tx_.data());
    header[2] = ti::VAR_REAL;
    header[3] = name[0];
  }
  header[0] = (uint8_t)(tx_.size() & 0xFF);
  header[1] = (uint8_t)(tx_.size() >> 8);
}

void CalcApp::finish(uint32_t ticket, const std::string &text, FooterKind footer) {
  bool lowercase;
  {
    hal::Guard guard;
    if (ticket != ticket_) return;  // the user has moved on to something else
    lowercase = lowercase_;
  }
  std::vector<layout::Line> lines;
  layout::layoutText(text, lowercase, kScreenCols, kMaxGlyphs, lines);
  if (lines.empty()) layout::layoutText("(empty answer)", lowercase, kScreenCols, kMaxGlyphs, lines);

  hal::Guard guard;
  if (ticket != ticket_) return;
  lines_.swap(lines);
  footerKind_ = footer;
  page_ = 1;
  busy_ = false;
}

// ---------------------------------------------------------------------------
// Sending a program to the calculator ("silent" transfer, as a computer would)
// ---------------------------------------------------------------------------

int CalcApp::sendProgramOnce(const char *name, const uint8_t *tokens, size_t len) {
  const uint8_t me = ti::ID_COMPUTER_83P;

  std::vector<uint8_t> var(len + 2);
  var[0] = (uint8_t)(len & 0xFF);
  var[1] = (uint8_t)(len >> 8);
  memcpy(var.data() + 2, tokens, len);

  uint8_t header[13];
  memset(header, 0, sizeof(header));
  header[0] = (uint8_t)(var.size() & 0xFF);
  header[1] = (uint8_t)(var.size() >> 8);
  header[2] = ti::VAR_PROGRAM;
  for (int i = 0; i < 8 && name[i]; i++) header[3 + i] = (uint8_t)name[i];

  ti::Packet pkt;
  int rc = link_->send(me, ti::CMD_RTS, header, sizeof(header));
  if (rc != ti::LINK_OK) {
    hal::logf("send program: calculator did not take the request (%s)", ti::linkResultName(rc));
    return SEND_NO_ANSWER;
  }
  rc = link_->receive(pkt, rx_, sizeof(rx_), 2000000);
  if (rc != ti::LINK_OK || pkt.command != ti::CMD_ACK) {
    hal::logf("send program: no ACK for the request (%s, command 0x%02X)", ti::linkResultName(rc), pkt.command);
    return SEND_NO_ANSWER;
  }
  rc = link_->receive(pkt, rx_, sizeof(rx_), 4000000);
  if (rc != ti::LINK_OK) {
    hal::logf("send program: no go-ahead (%s)", ti::linkResultName(rc));
    return SEND_FAILED;
  }
  if (pkt.command == ti::CMD_SKIP) {
    hal::logf("send program: calculator refused, reason %u", pkt.dataLen ? rx_[0] : 0u);
    link_->send(me, ti::CMD_ACK, nullptr, 0);
    return SEND_REFUSED;
  }
  if (pkt.command != ti::CMD_CTS) {
    hal::logf("send program: expected go-ahead, got command 0x%02X", pkt.command);
    return SEND_FAILED;
  }
  rc = link_->send(me, ti::CMD_ACK, nullptr, 0);
  if (rc == ti::LINK_OK) rc = link_->send(me, ti::CMD_DATA, var.data(), (uint16_t)var.size());
  if (rc != ti::LINK_OK) {
    hal::logf("send program: sending the data failed (%s)", ti::linkResultName(rc));
    return SEND_FAILED;
  }
  rc = link_->receive(pkt, rx_, sizeof(rx_), 5000000);
  if (rc != ti::LINK_OK || pkt.command != ti::CMD_ACK) {
    hal::logf("send program: data not acknowledged (%s, command 0x%02X)", ti::linkResultName(rc), pkt.command);
    return SEND_FAILED;
  }
  link_->send(me, ti::CMD_EOT, nullptr, 0);
  return SEND_OK;
}

int CalcApp::sendProgram(const char *name, const uint8_t *tokens, size_t len) {
  if (!link_->idle()) return SEND_NO_ANSWER;

  int result = sendProgramOnce(name, tokens, len);
  if (result == SEND_NO_ANSWER && !link_->orientationKnown()) {
    // We have never heard from the calculator, so we may have tip and ring the
    // wrong way round. Give the calculator time to forget the garbled attempt,
    // then try the other way.
    hal::sleepMs(2500);
    link_->setSwapped(!link_->swapped());
    result = sendProgramOnce(name, tokens, len);
    if (result == SEND_OK) {
      link_->setOrientationKnown(true);
    } else {
      link_->setSwapped(!link_->swapped());
    }
  }
  if (link_->orientationKnown() && (!wiringReported_ || reportedSwapped_ != link_->swapped())) {
    wiringReported_ = true;
    reportedSwapped_ = link_->swapped();
    handler_->onWiringLearned(reportedSwapped_);
  }
  return result;
}

}  // namespace app
