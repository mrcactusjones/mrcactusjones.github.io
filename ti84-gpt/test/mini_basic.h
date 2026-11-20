// A tiny TI-BASIC interpreter: just the commands prgmGPT uses. It runs the
// real tokenized program against the simulated link, so the tests exercise
// the calculator program itself and not a paraphrase of it.
#pragma once
#include <stdint.h>

#include <deque>
#include <functional>
#include <string>
#include <vector>

#include "sim.h"

class MiniBasic {
 public:
  MiniBasic(const std::vector<uint8_t> &program, sim::Calc &calc);

  // --- the scripted "user" ---
  std::deque<int> menuChoices;      // 1-based menu picks, in order
  std::deque<std::string> inputs;   // what gets typed at each Input
  std::function<int()> getKey;      // key code, or 0 for "no key pressed"

  // --- what the program did ---
  std::string row(int r) const;     // text of home-screen row 1..8
  std::string screenText() const;   // all 8 rows joined with '\n'
  std::vector<std::string> disp;    // everything shown with Disp
  std::vector<std::string> trace;   // menu titles and labels visited
  int gotosInsideLoops = 0;         // TI-BASIC leaks memory on these
  std::string error;

  // Runs until the program ends. Returns false on an error or when the
  // time limit runs out.
  bool run(uint32_t timeLimitMs);

  double var(char name) const { return vars_[name - 'A']; }

 private:
  typedef std::vector<uint8_t> Line;
  struct Cursor {
    const Line *line;
    size_t pos;
    bool more() const { return pos < line->size(); }
    uint8_t peek() const { return (*line)[pos]; }
  };

  bool step();
  bool jump(const Line &label);
  double parseOr(Cursor &c);
  double parseAnd(Cursor &c);
  double parseCompare(Cursor &c);
  double parseSum(Cursor &c);
  double parseProduct(Cursor &c);
  double parsePrimary(Cursor &c);
  bool parseString(Cursor &c, Line &tokens);
  void output(int row, int col, const Line &tokens);
  void fail(const std::string &why);
  static std::string text(const Line &tokens);

  std::vector<Line> lines_;
  sim::Calc &calc_;
  size_t pc_ = 0;
  bool stopped_ = false;
  double vars_[26];
  Line strings_[10];
  std::vector<size_t> loops_;  // line numbers of open Repeat statements
  std::string cells_[8][16];
};
