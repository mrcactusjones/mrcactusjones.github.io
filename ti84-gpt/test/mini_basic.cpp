#include "mini_basic.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

#include "../TI84GPT/ti_tokens.h"
#include "../TI84GPT/ti_vars.h"

namespace {
const uint8_t T_STORE = 0x04, T_LPAREN = 0x10, T_RPAREN = 0x11, T_MAX = 0x19, T_MIN = 0x1A;
const uint8_t T_QUOTE = 0x2A, T_COMMA = 0x2B, T_DOT = 0x3A, T_OR = 0x3C, T_COLON = 0x3E, T_NEWLINE = 0x3F;
const uint8_t T_AND = 0x40, T_EQ = 0x6A, T_LT = 0x6B, T_GT = 0x6C, T_LE = 0x6D, T_GE = 0x6E, T_NE = 0x6F;
const uint8_t T_ADD = 0x70, T_SUB = 0x71, T_MUL = 0x82, T_DIV = 0x83, T_STR = 0xAA, T_GETKEY = 0xAD;
const uint8_t T_NEG = 0xB0, T_IF = 0xCE, T_THEN = 0xCF, T_REPEAT = 0xD2, T_END = 0xD4, T_LBL = 0xD6;
const uint8_t T_GOTO = 0xD7, T_STOP = 0xD9, T_INPUT = 0xDC, T_DISP = 0xDE, T_OUTPUT = 0xE0;
const uint8_t T_CLRHOME = 0xE1, T_MENU = 0xE6, T_SEND = 0xE7, T_GET = 0xE8;

bool isLetter(uint8_t t) { return t >= 'A' && t <= 'Z'; }
bool isDigit(uint8_t t) { return t >= '0' && t <= '9'; }
}  // namespace

MiniBasic::MiniBasic(const std::vector<uint8_t> &program, sim::Calc &calc) : calc_(calc) {
  memset(vars_, 0, sizeof(vars_));
  Line current;
  bool inString = false;
  for (size_t i = 0; i < program.size(); i++) {
    const uint8_t t = program[i];
    if (t == T_NEWLINE) {
      lines_.push_back(current);
      current.clear();
      inString = false;
      continue;
    }
    if (t == T_QUOTE) inString = !inString;
    if (t == T_COLON && !inString) error = "':' statement separators are not supported by the test interpreter";
    current.push_back(t);
    if (ti::isTwoByteLead(t) && i + 1 < program.size()) current.push_back(program[++i]);
  }
  lines_.push_back(current);
  for (int r = 0; r < 8; r++) {
    for (int c = 0; c < 16; c++) cells_[r][c] = " ";
  }
}

std::string MiniBasic::text(const Line &tokens) {
  return ti::detokenize(tokens.data(), tokens.size());
}

std::string MiniBasic::row(int r) const {
  std::string out;
  for (int c = 0; c < 16; c++) out += cells_[r - 1][c];
  return out;
}

std::string MiniBasic::screenText() const {
  std::string out;
  for (int r = 1; r <= 8; r++) {
    out += row(r);
    out += '\n';
  }
  return out;
}

void MiniBasic::fail(const std::string &why) {
  if (!error.empty()) return;
  char where[48];
  snprintf(where, sizeof(where), " (program line %u: ", (unsigned)(pc_ + 1));
  error = why + where + (pc_ < lines_.size() ? text(lines_[pc_]) : std::string("end")) + ")";
  stopped_ = true;
}

// Home-screen Output(: one character cell per token, wrapping to the next
// row, never scrolling; text that would run past row 8 is cut off.
void MiniBasic::output(int row, int col, const Line &tokens) {
  if (row < 1 || row > 8 || col < 1 || col > 16) {
    fail("Output( position off the screen (ERR:DOMAIN)");
    return;
  }
  int r = row - 1, c = col - 1;
  for (size_t i = 0; i < tokens.size(); i++) {
    Line one(1, tokens[i]);
    if (ti::isTwoByteLead(tokens[i]) && i + 1 < tokens.size()) one.push_back(tokens[++i]);
    if (r > 7) return;
    cells_[r][c] = text(one);
    if (++c > 15) {
      c = 0;
      r++;
    }
  }
}

bool MiniBasic::parseString(Cursor &c, Line &tokens) {
  tokens.clear();
  if (!c.more() || c.peek() != T_QUOTE) return false;
  c.pos++;
  while (c.more() && c.peek() != T_QUOTE) tokens.push_back((*c.line)[c.pos++]);
  if (c.more()) c.pos++;  // closing quote (optional at the end of a line)
  return true;
}

double MiniBasic::parsePrimary(Cursor &c) {
  if (!c.more()) {
    fail("expression ends too early");
    return 0;
  }
  const uint8_t t = c.peek();
  if (isDigit(t) || t == T_DOT) {
    std::string number;
    while (c.more() && (isDigit(c.peek()) || c.peek() == T_DOT)) number += (c.peek() == T_DOT ? '.' : (char)(*c.line)[c.pos]), c.pos++;
    return atof(number.c_str());
  }
  if (isLetter(t)) {
    c.pos++;
    return vars_[t - 'A'];
  }
  if (t == T_GETKEY) {
    c.pos++;
    return getKey ? (double)getKey() : 0.0;
  }
  if (t == T_NEG) {
    c.pos++;
    return -parsePrimary(c);
  }
  if (t == T_LPAREN) {
    c.pos++;
    const double v = parseOr(c);
    if (c.more() && c.peek() == T_RPAREN) c.pos++;
    return v;
  }
  if (t == T_MAX || t == T_MIN) {
    c.pos++;
    const double a = parseOr(c);
    if (!c.more() || c.peek() != T_COMMA) {
      fail("max(/min( needs two arguments");
      return 0;
    }
    c.pos++;
    const double b = parseOr(c);
    if (c.more() && c.peek() == T_RPAREN) c.pos++;
    return (t == T_MAX) ? (a > b ? a : b) : (a < b ? a : b);
  }
  fail("unexpected token in expression");
  return 0;
}

double MiniBasic::parseProduct(Cursor &c) {
  double v = parsePrimary(c);
  while (c.more() && (c.peek() == T_MUL || c.peek() == T_DIV)) {
    const uint8_t op = c.peek();
    c.pos++;
    const double r = parsePrimary(c);
    v = (op == T_MUL) ? v * r : v / r;
  }
  return v;
}

double MiniBasic::parseSum(Cursor &c) {
  double v = parseProduct(c);
  while (c.more() && (c.peek() == T_ADD || c.peek() == T_SUB)) {
    const uint8_t op = c.peek();
    c.pos++;
    const double r = parseProduct(c);
    v = (op == T_ADD) ? v + r : v - r;
  }
  return v;
}

double MiniBasic::parseCompare(Cursor &c) {
  double v = parseSum(c);
  while (c.more()) {
    const uint8_t op = c.peek();
    if (op != T_EQ && op != T_LT && op != T_GT && op != T_LE && op != T_GE && op != T_NE) break;
    c.pos++;
    const double r = parseSum(c);
    switch (op) {
      case T_EQ: v = (v == r); break;
      case T_LT: v = (v < r); break;
      case T_GT: v = (v > r); break;
      case T_LE: v = (v <= r); break;
      case T_GE: v = (v >= r); break;
      default: v = (v != r); break;
    }
  }
  return v;
}

double MiniBasic::parseAnd(Cursor &c) {
  double v = parseCompare(c);
  while (c.more() && c.peek() == T_AND) {
    c.pos++;
    const double r = parseCompare(c);
    v = (v != 0 && r != 0);
  }
  return v;
}

double MiniBasic::parseOr(Cursor &c) {
  double v = parseAnd(c);
  while (c.more() && c.peek() == T_OR) {
    c.pos++;
    const double r = parseAnd(c);
    v = (v != 0 || r != 0);
  }
  return v;
}

bool MiniBasic::jump(const Line &label) {
  if (label.empty() || label.size() > 2) {
    fail("bad label");
    return false;
  }
  if (!loops_.empty()) gotosInsideLoops++;
  for (size_t i = 0; i < lines_.size(); i++) {
    const Line &l = lines_[i];
    if (l.size() == label.size() + 1 && l[0] == T_LBL && memcmp(&l[1], label.data(), label.size()) == 0) {
      pc_ = i;
      trace.push_back("Lbl " + text(label));
      return true;
    }
  }
  fail("ERR:LABEL - no Lbl " + text(label));
  return false;
}

bool MiniBasic::step() {
  if (pc_ >= lines_.size()) {
    stopped_ = true;
    return false;
  }
  const Line &line = lines_[pc_];
  if (line.empty()) {
    pc_++;
    return true;
  }
  Cursor c{&line, 1};
  const uint8_t head = line[0];

  switch (head) {
    case T_CLRHOME:
      for (int r = 0; r < 8; r++) {
        for (int col = 0; col < 16; col++) cells_[r][col] = " ";
      }
      break;

    case T_LBL:
      break;

    case T_GOTO:
      return jump(Line(line.begin() + 1, line.end()));

    case T_STOP:
      stopped_ = true;
      return false;

    case T_MENU: {
      Line title;
      if (!parseString(c, title)) return fail("Menu( needs a title"), false;
      if (text(title).size() > 16) return fail("Menu( title longer than 16 characters"), false;
      std::vector<Line> labels;
      while (c.more() && c.peek() == T_COMMA) {
        c.pos++;
        Line item, label;
        if (!parseString(c, item)) return fail("Menu( item must be a string"), false;
        if (text(item).size() > 14) return fail("Menu( item longer than 14 characters"), false;
        if (!c.more() || c.peek() != T_COMMA) return fail("Menu( item without a label"), false;
        c.pos++;
        while (c.more() && c.peek() != T_COMMA && c.peek() != T_RPAREN) label.push_back(line[c.pos++]);
        labels.push_back(label);
      }
      if (labels.empty() || labels.size() > 7) return fail("Menu( takes 1 to 7 items"), false;
      trace.push_back("Menu " + text(title));
      if (menuChoices.empty()) return fail("test script has no more menu choices"), false;
      const int pick = menuChoices.front();
      menuChoices.pop_front();
      if (pick < 1 || pick > (int)labels.size()) return fail("menu choice out of range"), false;
      return jump(labels[(size_t)pick - 1]);
    }

    case T_DISP:
      for (;;) {
        Line s;
        if (c.more() && c.peek() == T_QUOTE) {
          parseString(c, s);
          disp.push_back(text(s));
        } else {
          char buf[32];
          snprintf(buf, sizeof(buf), "%g", parseOr(c));
          disp.push_back(buf);
        }
        if (!c.more() || c.peek() != T_COMMA) break;
        c.pos++;
      }
      break;

    case T_INPUT: {
      if (line.size() != 3 || line[1] != T_STR) return fail("test interpreter only does Input StrN"), false;
      if (inputs.empty()) return fail("test script has nothing more to type"), false;
      strings_[line[2]] = sim::typed(inputs.front());
      inputs.pop_front();
      break;
    }

    case T_SEND:
    case T_GET: {
      bool ok;
      if (c.more() && c.peek() == T_STR && c.pos + 1 < line.size()) {
        const uint8_t slot = line[c.pos + 1];
        const int index = (slot == 9) ? 0 : slot + 1;
        ok = (head == T_SEND) ? calc_.sendString(index, strings_[slot]) : calc_.getString(index, strings_[slot]);
      } else if (c.more() && isLetter(c.peek())) {
        const char name = (char)c.peek();
        ok = (head == T_SEND) ? calc_.sendReal(name, vars_[name - 'A']) : calc_.getReal(name, vars_[name - 'A']);
      } else {
        return fail("Send(/Get( needs a variable"), false;
      }
      if (!ok) return fail("ERR:LINK - " + calc_.lastError), false;
      break;
    }

    case T_OUTPUT: {
      const double r = parseOr(c);
      if (!c.more() || c.peek() != T_COMMA) return fail("Output( needs row,col,text"), false;
      c.pos++;
      const double col = parseOr(c);
      if (!c.more() || c.peek() != T_COMMA) return fail("Output( needs row,col,text"), false;
      c.pos++;
      Line s;
      if (c.more() && c.peek() == T_QUOTE) {
        parseString(c, s);
      } else if (c.more() && c.peek() == T_STR && c.pos + 1 < line.size()) {
        s = strings_[line[c.pos + 1]];
      } else {
        char buf[32];
        snprintf(buf, sizeof(buf), "%g", parseOr(c));
        s = sim::typed(buf);
      }
      output((int)r, (int)col, s);
      break;
    }

    case T_REPEAT:
      loops_.push_back(pc_);
      break;

    case T_END: {
      if (loops_.empty()) return fail("End without Repeat"), false;
      const Line &opener = lines_[loops_.back()];
      Cursor cond{&opener, 1};
      if (parseOr(cond) != 0) {
        loops_.pop_back();  // Repeat stops once its condition is true
      } else {
        pc_ = loops_.back() + 1;
        return true;
      }
      break;
    }

    case T_IF: {
      const double cond = parseOr(c);
      if (pc_ + 1 < lines_.size() && !lines_[pc_ + 1].empty() && lines_[pc_ + 1][0] == T_THEN) {
        return fail("If ... Then is not supported by the test interpreter"), false;
      }
      if (cond == 0) pc_++;  // skip the single statement that follows
      break;
    }

    default: {
      // expression -> variable
      Cursor e{&line, 0};
      const double value = parseOr(e);
      if (!e.more() || e.peek() != T_STORE || e.pos + 2 != line.size() || !isLetter(line[e.pos + 1])) {
        return fail("statement not understood by the test interpreter"), false;
      }
      vars_[line[e.pos + 1] - 'A'] = value;
      break;
    }
  }
  if (!error.empty()) return false;
  pc_++;
  return true;
}

bool MiniBasic::run(uint32_t timeLimitMs) {
  if (!error.empty()) return false;
  const uint32_t t0 = sim::nowUs();
  while (!stopped_) {
    if (!step() && !error.empty()) return false;
    if (sim::nowUs() - t0 > timeLimitMs * 1000u) {
      fail("time limit reached");
      return false;
    }
  }
  return error.empty();
}
