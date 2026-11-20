// Desktop tests for the TI84GPT firmware logic.
//
//   make -C test        (from the ti84-gpt folder)
//
// Nothing here needs a calculator or an ESP32: the two link wires, the
// calculator's side of the protocol and a small TI-BASIC interpreter are all
// simulated (sim.cpp, mini_basic.cpp).
#include <math.h>
#include <stdio.h>
#include <string.h>

#include <atomic>
#include <chrono>
#include <fstream>
#include <mutex>
#include <sstream>
#include <thread>

#include "../TI84GPT/calc_app.h"
#include "../TI84GPT/chat.h"
#include "../TI84GPT/json_mini.h"
#include "../TI84GPT/launcher_program.h"
#include "../TI84GPT/text_layout.h"
#include "../TI84GPT/ti_link.h"
#include "../TI84GPT/ti_tokens.h"
#include "../TI84GPT/ti_vars.h"
#include "mini_basic.h"
#include "sim.h"

static int gChecks = 0, gFailures = 0;

#define CHECK(cond)                                                        \
  do {                                                                     \
    gChecks++;                                                             \
    if (!(cond)) {                                                         \
      gFailures++;                                                         \
      printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond);             \
    }                                                                      \
  } while (0)

#define CHECK_EQ(a, b)                                                     \
  do {                                                                     \
    gChecks++;                                                             \
    const std::string va = (a), vb = (b);                                  \
    if (va != vb) {                                                        \
      gFailures++;                                                         \
      printf("  FAIL %s:%d  %s\n    got:      [%s]\n    expected: [%s]\n", __FILE__, __LINE__, #a, \
             va.c_str(), vb.c_str());                                      \
    }                                                                      \
  } while (0)

static std::string trimRight(std::string s) {
  while (!s.empty() && s[s.size() - 1] == ' ') s.erase(s.size() - 1);
  return s;
}

// Text of a rendered screen row (16 cells) from raw string tokens.
static std::vector<std::string> rowsOf(const std::vector<uint8_t> &tokens) {
  std::vector<std::string> rows;
  std::string current;
  int cells = 0;
  for (size_t i = 0; i < tokens.size(); i++) {
    size_t n = 1;
    if (ti::isTwoByteLead(tokens[i]) && i + 1 < tokens.size()) n = 2;
    current += ti::detokenize(&tokens[i], n);
    i += n - 1;
    if (++cells == 16) {
      rows.push_back(current);
      current.clear();
      cells = 0;
    }
  }
  if (cells) rows.push_back(current);
  return rows;
}

// ---------------------------------------------------------------------------
// Unit tests
// ---------------------------------------------------------------------------

static void testReals() {
  printf("real numbers\n");
  const long values[] = {0, 1, 2, 5, 9, 10, 24, 45, 95, 105, 128, 1000, 4000, 65535, -3, 123456789};
  for (size_t i = 0; i < sizeof(values) / sizeof(values[0]); i++) {
    uint8_t fw[9], ref[9];
    ti::intToReal(values[i], fw);
    sim::encodeReal((double)values[i], ref);
    CHECK(memcmp(fw, ref, 9) == 0);
    CHECK(fabs(ti::realToDouble(ref) - (double)values[i]) < 1e-6);
    CHECK(fabs(sim::decodeReal(fw) - (double)values[i]) < 1e-6);
  }
  uint8_t r[9];
  sim::encodeReal(3.75, r);
  CHECK(fabs(ti::realToDouble(r) - 3.75) < 1e-9);
  sim::encodeReal(0.002, r);
  CHECK(fabs(ti::realToDouble(r) - 0.002) < 1e-9);
  // 1 is stored as 00 80 10 00 00 00 00 00 00
  const uint8_t one[9] = {0x00, 0x80, 0x10, 0, 0, 0, 0, 0, 0};
  ti::intToReal(1, r);
  CHECK(memcmp(r, one, 9) == 0);
  const uint8_t oneOhFive[9] = {0x00, 0x82, 0x10, 0x50, 0, 0, 0, 0, 0};
  ti::intToReal(105, r);
  CHECK(memcmp(r, oneOhFive, 9) == 0);
}

static void testTokens(const std::string &root) {
  printf("tokens and the calculator program\n");
  const uint8_t typed[] = {0x53, 0x4F, 0x4C, 0x56, 0x45, 0x29, 0x58, 0x0D, 0x70, 0x33, 0x58, 0x6A, 0x31, 0x30};
  CHECK_EQ(ti::detokenize(typed, sizeof(typed)), "SOLVE X^2+3X=10");
  const uint8_t math[] = {0xBC, 0x32, 0x11, 0x82, 0xAC, 0x83, 0xC2, 0xB0, 0x58, 0x11};
  CHECK_EQ(ti::detokenize(math, sizeof(math)), "sqrt(2)*pi/sin(-X)");
  const uint8_t lower[] = {0xBB, 0xB0, 0xBB, 0xBA, 0xBB, 0xBC, 0xBB, 0xCA, 0xAA, 0x09};
  CHECK_EQ(ti::detokenize(lower, sizeof(lower)), "aklzStr0");

  // The program built into the firmware must be exactly launcher/GPT.txt.
  // (The header was produced by the Python tokenizer; reading it back with
  // the firmware's own, separately generated token table checks both.)
  std::ifstream in(root + "/launcher/GPT.txt");
  CHECK(in.good());
  std::string line, source;
  while (std::getline(in, line)) {
    if (line.compare(0, 2, "//") == 0 || line.empty()) continue;
    if (!source.empty()) source += '\n';
    source += line;
  }
  CHECK_EQ(ti::detokenize(kLauncherProgram, kLauncherProgramSize), source);
}

static void testLayout() {
  printf("text layout\n");
  std::vector<layout::Line> lines;
  std::vector<uint8_t> tokens;

  layout::layoutText("The quick brown fox jumps over the lazy dog.", false, 16, 4000, lines);
  layout::renderPage(lines, 1, 16, 7, "1/1   CLEAR=MENU", tokens);
  std::vector<std::string> rows = rowsOf(tokens);
  CHECK(rows.size() == 8);
  CHECK_EQ(rows[0], "THE QUICK BROWN ");
  CHECK_EQ(rows[1], "FOX JUMPS OVER  ");
  CHECK_EQ(rows[2], "THE LAZY DOG.   ");
  CHECK_EQ(rows[3], "                ");
  CHECK_EQ(rows[7], "1/1   CLEAR=MENU");
  CHECK(tokens.size() == 128);  // all one-byte tokens in upper case

  layout::layoutText("abc xyz", true, 16, 4000, lines);
  layout::renderPage(lines, 1, 16, 7, "", tokens);
  CHECK(tokens.size() == 128 + 6);  // six lowercase letters cost a byte extra each
  CHECK_EQ(trimRight(rowsOf(tokens)[0]), "abc xyz");

  // A word that exactly fills a line, and one that is longer than a line.
  layout::layoutText("ABCDEFGHIJKLMNOP QRS ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", false, 16, 4000, lines);
  CHECK(lines.size() == 5);
  layout::renderPage(lines, 1, 16, 7, "", tokens);
  rows = rowsOf(tokens);
  CHECK_EQ(rows[0], "ABCDEFGHIJKLMNOP");
  CHECK_EQ(rows[1], "QRS             ");
  CHECK_EQ(rows[2], "ABCDEFGHIJKLMNOP");
  CHECK_EQ(rows[3], "QRSTUVWXYZ012345");
  CHECK_EQ(rows[4], "6789            ");

  // Newlines start a new row; blank lines are not worth a row on this screen.
  layout::layoutText("ANS: 4\n\nStep 1: add\nStep 2: done\n", false, 16, 4000, lines);
  CHECK(lines.size() == 3);

  // Page maths.
  CHECK(layout::pageCount(0, 7) == 1);
  CHECK(layout::pageCount(7, 7) == 1);
  CHECK(layout::pageCount(8, 7) == 2);
  CHECK(layout::pageCount(15, 7) == 3);

  // The glyph limit really limits.
  layout::layoutText(std::string(10000, 'A'), false, 16, 4000, lines);
  CHECK(lines.size() == 250);

  // Markdown and LaTeX leftovers.
  CHECK_EQ(layout::cleanMarkup("## Answer\n**x = 4** and `y`"), "Answer\nx = 4 and y");
  CHECK_EQ(layout::cleanMarkup("* one\n- two\n---\ndone"), "- one\n- two\n\ndone");
  CHECK_EQ(layout::cleanMarkup("\\(x = \\frac{-b \\pm \\sqrt{b^2-4ac}}{2a}\\)"),
           "x = (-b +/- sqrt(b^2-4ac))/(2a)");
  CHECK_EQ(layout::cleanMarkup("\\frac{1}{2} + x^{10} + a_{n}"), "1/2 + x^10 + a_n");
  CHECK_EQ(layout::cleanMarkup("$x^{n+1}$ costs $5"), "x^(n+1) costs $5");
  CHECK_EQ(layout::cleanMarkup("2 \\cdot 3 \\le 7, \\sin(x)"), "2 * 3 \xE2\x89\xA4 7, sin(x)");
  CHECK_EQ(layout::cleanMarkup("a >= b\n> quoted"), "a >= b\nquoted");

  // Symbols the calculator has get the real glyph; others a plain spelling.
  std::vector<layout::Glyph> g;
  layout::toGlyphs("\xCF\x80r\xC2\xB2 \xE2\x89\xA4 9 \xE2\x80\x94 \xE2\x80\x9Cok\xE2\x80\x9D \xF0\x9F\x98\x80", false, g, 100);
  const layout::Glyph expected[] = {0xAC, 'R', 0x0D, 0x29, 0x6D, 0x29, '9', 0x29, 0x71, 0x29, 0x2A, 'O', 'K', 0x2A, 0x29};
  CHECK(g.size() == sizeof(expected) / sizeof(expected[0]));
  for (size_t i = 0; i < g.size() && i < sizeof(expected) / sizeof(expected[0]); i++) CHECK(g[i] == expected[i]);
  g.clear();
  layout::toGlyphs("caf\xC3\xA9 na\xC3\xAFve", true, g, 100);
  CHECK(g.size() == 10 && g[3] == 0xBB7A && g[7] == 0xBB85);
}

static void testJson() {
  printf("JSON and chat messages\n");
  const std::string ok =
      "{\n  \"id\": \"chatcmpl-1\",\n  \"object\": \"chat.completion\",\n  \"choices\": [\n    {\n"
      "      \"index\": 0,\n      \"message\": {\n        \"role\": \"assistant\",\n"
      "        \"content\": \"2 + 2 = 4\\nLine \\\"two\\\" \\u03c0 \\ud83d\\ude00 caf\\u00e9\",\n"
      "        \"refusal\": null\n      },\n      \"finish_reason\": \"stop\"\n    }\n  ],\n"
      "  \"usage\": {\"prompt_tokens\": 9, \"completion_tokens\": 12, \"nested\": {\"a\": [1, 2, {\"b\": \"}]\"}]}}\n}";
  std::string content, error;
  CHECK(chat::parseResponse(ok, content, error));
  CHECK_EQ(content, "2 + 2 = 4\nLine \"two\" \xCF\x80 \xF0\x9F\x98\x80 caf\xC3\xA9");

  // Keys in a different order, with decoys before the real one.
  const std::string shuffled =
      "{\"usage\":{\"choices\":\"decoy\"},\"choices\":[{\"finish_reason\":\"stop\",\"message\":"
      "{\"content\":\"hi\",\"role\":\"assistant\"}}]}";
  CHECK(chat::parseResponse(shuffled, content, error));
  CHECK_EQ(content, "hi");

  const std::string bad = "{\"error\":{\"message\":\"Incorrect API key provided: sk-...\",\"type\":\"invalid_request_error\",\"code\":\"invalid_api_key\"}}";
  CHECK(!chat::parseResponse(bad, content, error));
  CHECK_EQ(error, "Incorrect API key provided: sk-...");

  CHECK(!chat::parseResponse("<html>502 Bad Gateway</html>", content, error));
  CHECK(!chat::parseResponse("{\"choices\":[]}", content, error));
  CHECK(!chat::parseResponse("{\"choices\":[{\"message\":{\"content\":\"unterminated", content, error));
  CHECK(!chat::parseResponse("", content, error));

  const std::string parts = "{\"choices\":[{\"message\":{\"content\":[{\"type\":\"text\",\"text\":\"a\"},{\"type\":\"text\",\"text\":\"b\"}]}}]}";
  CHECK(chat::parseResponse(parts, content, error));
  CHECK_EQ(content, "ab");

  chat::Conversation c;
  c.addUser("What is \"2+2\"?\nThanks");
  CHECK_EQ(c.requestJson("gpt-x", "Be brief."),
           "{\"model\":\"gpt-x\",\"messages\":[{\"role\":\"system\",\"content\":\"Be brief.\"},"
           "{\"role\":\"user\",\"content\":\"What is \\\"2+2\\\"?\\nThanks\"}]}");
  c.addAssistant("4");
  c.addUser("and 3+3?");
  CHECK_EQ(c.requestJson("m", ""),
           "{\"model\":\"m\",\"messages\":[{\"role\":\"user\",\"content\":\"What is \\\"2+2\\\"?\\nThanks\"},"
           "{\"role\":\"assistant\",\"content\":\"4\"},{\"role\":\"user\",\"content\":\"and 3+3?\"}]}");
  c.trim(10);
  CHECK_EQ(c.requestJson("m", ""), "{\"model\":\"m\",\"messages\":[{\"role\":\"user\",\"content\":\"and 3+3?\"}]}");

  // What we build must be readable by our own reader (round trip).
  chat::Conversation tricky;
  tricky.addUser("tab\there \\ back\x01slash \xCF\x80");
  const std::string body = tricky.requestJson("m", "");
  const char *b = body.c_str(), *e = b + body.size();
  std::string back;
  CHECK(json::readString(json::member(json::element(json::member(b, e, "messages"), e, 0), e, "content"), e, back));
  CHECK_EQ(back, "tab\there \\ back\x01slash \xCF\x80");

  std::vector<std::string> ids;
  chat::parseModelList("{\"object\":\"list\",\"data\":[{\"id\":\"gpt-a\",\"object\":\"model\"},{\"id\":\"gpt-b\"}]}", ids);
  CHECK(ids.size() == 2 && ids[0] == "gpt-a" && ids[1] == "gpt-b");
}

// ---------------------------------------------------------------------------
// Whole-system tests: firmware logic <-> simulated wires <-> simulated calculator
// ---------------------------------------------------------------------------

static const char *kLongAnswer =
    "2+2 = 4. Here is a much longer explanation so that the answer needs several screens on the "
    "calculator: one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen twenty. The end.";

struct Rig : public app::Handler {
  ti::Link link;
  app::CalcApp app;
  std::mutex mutex;
  std::vector<std::pair<int, std::string> > commands;
  std::vector<std::thread> workers;
  std::atomic<bool> stop{false};
  std::atomic<bool> installWanted{false};
  std::atomic<int> installResult{-1};
  std::atomic<int> wiringReports{0};
  std::atomic<bool> wiringSwapped{false};
  int replyDelayMs = 60;
  bool longAnswerGiven = false;
  std::thread device;

  void start() {
    link.begin(5, 7, false);  // the firmware starts out believing GPIO5 is tip
    app.begin(&link, this, true);
    device = std::thread([this]() {
      // Same shape as the sketch's loop(): keep serving the link, and push the
      // program a little later, once the calculator is back at its home screen.
      uint32_t installAt = 0;
      bool installPending = false;
      while (!stop) {
        if (app.poll()) continue;
        if (installWanted.exchange(false)) {
          installPending = true;
          installAt = sim::nowUs() + 80000;
        }
        if (installPending && (int32_t)(sim::nowUs() - installAt) >= 0) {
          installPending = false;
          installResult = app.sendProgram(LAUNCHER_NAME, kLauncherProgram, kLauncherProgramSize);
        }
        std::this_thread::yield();
      }
    });
  }

  void shutdown() {
    stop = true;
    device.join();
    for (size_t i = 0; i < workers.size(); i++) workers[i].join();
  }

  std::pair<int, std::string> command(size_t i) {
    std::lock_guard<std::mutex> guard(mutex);
    return i < commands.size() ? commands[i] : std::make_pair(-1, std::string());
  }
  size_t commandCount() {
    std::lock_guard<std::mutex> guard(mutex);
    return commands.size();
  }

  void onCommand(int command, const std::string &text, uint32_t ticket) override {
    bool firstAsk = false;
    {
      std::lock_guard<std::mutex> guard(mutex);
      commands.push_back(std::make_pair(command, text));
      if (command == app::COMMAND_ASK && !longAnswerGiven) firstAsk = longAnswerGiven = true;
    }
    if (command == app::COMMAND_INSTALL) {
      app.finish(ticket, "Sending the program...", app::FOOTER_INFO);
      installWanted = true;
      return;
    }
    if (command == app::COMMAND_STATUS) {
      app.finish(ticket, "WIFI: HomeNet\nIP: 192.168.1.50", app::FOOTER_INFO);  // answered at once
      return;
    }
    std::string reply;
    if (firstAsk) {
      reply = kLongAnswer;
    } else {
      reply = "Reply to: " + text;
    }
    const int delay = replyDelayMs;
    workers.push_back(std::thread([this, ticket, reply, delay]() {
      std::this_thread::sleep_for(std::chrono::milliseconds(delay));
      app.finish(ticket, reply, app::FOOTER_CHAT);
    }));
  }

  void onWiringLearned(bool swapped) override {
    wiringSwapped = swapped;
    wiringReports++;
  }
};

static std::string squeeze(const std::string &s) {  // single spaces, no ends
  std::string out;
  bool space = false;
  for (size_t i = 0; i < s.size(); i++) {
    if (s[i] == ' ' || s[i] == '\n') {
      space = !out.empty();
    } else {
      if (space) out += ' ';
      space = false;
      out += s[i];
    }
  }
  return out;
}

static void testFullSession(const char *title, bool wiresSwapped, uint32_t jitterUs, uint32_t riseUs) {
  printf("session: %s\n", title);
  sim::resetWires();
  sim::setRiseTimeUs(riseUs);
  Rig rig;
  rig.start();
  sim::Calc calc(wiresSwapped ? 7 : 5, wiresSwapped ? 5 : 7);
  calc.setJitterUs(jitterUs);

  // --- 1. First-time install from the home screen:  5->C : Send(C) ----------
  CHECK(calc.sendReal('C', 5));
  std::string name;
  uint8_t type = 0;
  std::vector<uint8_t> var;
  const bool received = calc.receiveSilent(name, type, var, 5000);
  CHECK(received);
  if (!received) printf("    %s\n", calc.lastError.c_str());
  CHECK_EQ(name, "GPT");
  CHECK(type == 0x05);
  CHECK(var.size() == kLauncherProgramSize + 2);
  CHECK(var.size() >= 2 && (size_t)(var[0] | (var[1] << 8)) == kLauncherProgramSize);
  CHECK(var.size() == kLauncherProgramSize + 2 && memcmp(&var[2], kLauncherProgram, kLauncherProgramSize) == 0);
  for (int i = 0; i < 200 && rig.installResult < 0; i++) std::this_thread::sleep_for(std::chrono::milliseconds(5));
  CHECK(rig.installResult == app::SEND_OK);
  CHECK(rig.wiringReports == 1);
  CHECK(rig.wiringSwapped == wiresSwapped);
  if (var.size() < 3) {
    rig.shutdown();
    return;
  }

  // --- 2. Run the program that just arrived ---------------------------------
  MiniBasic basic(std::vector<uint8_t>(var.begin() + 2, var.end()), calc);
  basic.menuChoices = {1, 4, 7};  // ASK CHATGPT ... later STATUS ... later QUIT
  basic.inputs = {"WHAT IS 2+2", "AND 3+3?"};
  // ENTER, ENTER, ENTER (already on the last screen), left, + (reply),
  // CLEAR (menu), CLEAR (leave the status screen)
  std::deque<int> keys = {105, 105, 105, 24, 95, 45, 45};
  std::vector<std::string> screens;
  bool sawThinking = false;
  basic.getKey = [&]() -> int {
    if (basic.row(4).find("THINKING") != std::string::npos) {
      sawThinking = true;
      return 0;  // just waiting
    }
    if (keys.empty()) return 0;
    screens.push_back(basic.screenText());
    const int key = keys.front();
    keys.pop_front();
    return key;
  };
  const bool ran = basic.run(30000);
  CHECK(ran);
  if (!ran) printf("    %s\n", basic.error.c_str());
  CHECK(basic.gotosInsideLoops == 0);
  CHECK(sawThinking);
  CHECK(keys.empty());
  CHECK(basic.menuChoices.empty());
  CHECK(basic.inputs.empty());

  // What the firmware was asked to do, in order.
  CHECK(rig.commandCount() == 4);
  CHECK(rig.command(0).first == app::COMMAND_INSTALL);
  CHECK(rig.command(1).first == app::COMMAND_ASK);
  CHECK_EQ(rig.command(1).second, "WHAT IS 2+2");
  CHECK(rig.command(2).first == app::COMMAND_REPLY);
  CHECK_EQ(rig.command(2).second, "AND 3+3?");
  CHECK(rig.command(3).first == app::COMMAND_STATUS);

  // What the user saw. screens[i] is the screen at the moment key i was pressed.
  CHECK(screens.size() == 7);
  if (screens.size() == 7) {
    std::vector<layout::Line> lines;
    layout::layoutText(kLongAnswer, true, 16, 4000, lines);
    const int pages = layout::pageCount(lines.size(), 7);
    CHECK(pages == 3);
    char footer[20];
    snprintf(footer, sizeof(footer), "1/%d   ENTER=MORE", pages);
    CHECK_EQ(screens[0].substr(7 * 17, 16), footer);
    CHECK_EQ(screens[0].substr(0, 16), "2+2 = 4. Here is");
    snprintf(footer, sizeof(footer), "2/%d   ENTER=MORE", pages);
    CHECK_EQ(screens[1].substr(7 * 17, 16), footer);
    snprintf(footer, sizeof(footer), "%d/%d      +=REPLY", pages, pages);
    CHECK_EQ(screens[2].substr(7 * 17, 16), footer);
    CHECK_EQ(screens[3], screens[2]);  // ENTER on the last screen changes nothing
    CHECK_EQ(screens[4], screens[1]);  // left arrow went back one screen

    // The three screens together are the whole answer, in order, no word lost.
    std::string all;
    for (int p = 0; p < 3; p++) {
      for (int r = 0; r < 7; r++) all += screens[(size_t)p].substr((size_t)r * 17, 16) + " ";
    }
    CHECK_EQ(squeeze(all), squeeze(kLongAnswer));

    CHECK_EQ(trimRight(screens[5].substr(0, 16)), "Reply to: AND");
    CHECK_EQ(trimRight(screens[5].substr(17, 16)), "3+3?");
    CHECK_EQ(screens[5].substr(7 * 17, 16), "1/1      +=REPLY");
    CHECK_EQ(trimRight(screens[6].substr(0, 16)), "WIFI: HomeNet");
    CHECK_EQ(trimRight(screens[6].substr(17, 16)), "IP: 192.168.1.50");
    CHECK_EQ(screens[6].substr(7 * 17, 16), "1/1   CLEAR=MENU");
  }

  const app::Stats stats = rig.app.stats();
  CHECK(stats.errors == 0);
  CHECK(stats.packets > 50);
  rig.shutdown();
}

static void testCancelAndStaleAnswer() {
  printf("session: CLEAR while waiting, then ask again\n");
  sim::resetWires();
  sim::setRiseTimeUs(0);
  Rig rig;
  rig.replyDelayMs = 300;
  rig.longAnswerGiven = true;
  rig.start();
  sim::Calc calc(5, 7);

  MiniBasic basic(std::vector<uint8_t>(kLauncherProgram, kLauncherProgram + kLauncherProgramSize), calc);
  basic.menuChoices = {3, 1, 7};  // SOLVE MATH (cancelled) ... ASK ... QUIT
  basic.inputs = {"FIRST", "SECOND"};
  int thinkingPolls = 0;
  bool cancelled = false;
  std::string finalScreen;
  basic.getKey = [&]() -> int {
    if (basic.row(4).find("THINKING") != std::string::npos) {
      if (!cancelled && ++thinkingPolls == 2) {
        cancelled = true;
        return 45;  // CLEAR
      }
      return 0;
    }
    if (finalScreen.empty()) {
      finalScreen = basic.screenText();
      return 45;
    }
    return 0;
  };
  const bool ran = basic.run(30000);
  CHECK(ran);
  if (!ran) printf("    %s\n", basic.error.c_str());
  CHECK(cancelled);
  CHECK(rig.commandCount() == 2);
  CHECK(rig.command(0).first == app::COMMAND_SOLVE);
  CHECK_EQ(rig.command(0).second, "FIRST");
  CHECK(rig.command(1).first == app::COMMAND_ASK);
  // The answer to the cancelled question must never show up for the new one.
  CHECK_EQ(trimRight(finalScreen.substr(0, 16)), "Reply to: SECOND");
  rig.shutdown();
}

static void testLinkFaults() {
  printf("link faults\n");
  sim::resetWires();
  sim::setRiseTimeUs(0);
  Rig rig;
  rig.start();
  sim::Calc calc(5, 7);
  double value = -1;

  // Normal operation first.
  CHECK(calc.getReal('S', value) && value == 1);
  CHECK(calc.getReal('V', value) && value == 1);
  CHECK(calc.getReal('Z', value) && value == 0);

  // A transfer that dies after 3 bytes, then after 9 bytes.
  calc.sendTruncated(3);
  std::this_thread::sleep_for(std::chrono::milliseconds(600));
  CHECK(calc.getReal('N', value) && value == 1);
  calc.sendTruncated(9);
  std::this_thread::sleep_for(std::chrono::milliseconds(600));
  CHECK(calc.getReal('N', value) && value == 1);

  // A packet with a bad checksum gets no answer and does no harm.
  uint8_t header[11] = {9, 0, 0, 'C', 0, 0, 0, 0, 0, 0, 0};
  CHECK(calc.sendPacket(0x82, 0xC9, header, 11, true));
  uint8_t machine, command;
  std::vector<uint8_t> data;
  CHECK(!calc.recvPacket(machine, command, data, 300));
  CHECK(calc.getReal('S', value) && value == 1);
  CHECK(rig.commandCount() == 0);

  // Packets not meant for a CBL (calculator-to-calculator link) are ignored.
  CHECK(calc.sendPacket(0x73, 0x68, nullptr, 0));
  CHECK(!calc.recvPacket(machine, command, data, 300));

  // An empty string and a long one.
  CHECK(calc.sendString(1, std::vector<uint8_t>()));
  CHECK(calc.sendString(1, sim::typed(std::string(900, 'Q'))));
  CHECK(calc.sendReal('C', 2));
  for (int i = 0; i < 400 && rig.app.busy(); i++) std::this_thread::sleep_for(std::chrono::milliseconds(5));
  CHECK(rig.command(0).second == std::string(900, 'Q'));

  // Asking for screens that do not exist is clamped, not an error.
  std::vector<uint8_t> tokens;
  CHECK(calc.sendReal('P', 99));
  CHECK(calc.getString(0, tokens));
  CHECK(calc.getReal('N', value));
  const double pages = value;
  CHECK(calc.getReal('P', value) && value == pages);
  CHECK(calc.sendReal('P', -4));
  CHECK(calc.getString(9, tokens));
  CHECK(calc.getReal('P', value) && value == 1);

  // While the firmware is busy, Get(S) answers 0 straight away.
  rig.replyDelayMs = 400;
  CHECK(calc.sendString(1, sim::typed("SLOW")));
  CHECK(calc.sendReal('C', 1));
  CHECK(calc.getReal('S', value) && value == 0);
  for (int i = 0; i < 400 && rig.app.busy(); i++) std::this_thread::sleep_for(std::chrono::milliseconds(5));
  CHECK(calc.getReal('S', value) && value == 1);

  const app::Stats stats = rig.app.stats();
  CHECK(stats.errors == 3);  // two truncated transfers and one bad checksum
  rig.shutdown();
}

static void testInstallWithoutCalculator() {
  printf("install: nobody listening\n");
  sim::resetWires();
  sim::setRiseTimeUs(0);
  Rig rig;
  rig.start();
  // Wires idle high but no calculator answers: both wire orders get tried.
  const uint32_t t0 = sim::nowUs();
  rig.installWanted = true;
  for (int i = 0; i < 2000 && rig.installResult < 0; i++) std::this_thread::sleep_for(std::chrono::milliseconds(5));
  CHECK(rig.installResult == app::SEND_NO_ANSWER);
  CHECK(sim::nowUs() - t0 > 2500000);
  CHECK(rig.link.swapped() == false);  // back to where it started
  CHECK(rig.wiringReports == 0);

  // Calculator switched off: both wires low. Refuse immediately.
  sim::calcLow(5);
  sim::calcLow(7);
  rig.installResult = -1;
  rig.installWanted = true;
  for (int i = 0; i < 200 && rig.installResult < 0; i++) std::this_thread::sleep_for(std::chrono::milliseconds(5));
  CHECK(rig.installResult == app::SEND_NO_ANSWER);
  // The two wires do not drop at the very same instant, so at most one
  // receive attempt may start and fail; after that the firmware stays quiet.
  std::this_thread::sleep_for(std::chrono::milliseconds(1500));
  CHECK(rig.app.stats().errors <= 1);
  sim::resetWires();
  rig.shutdown();
}

static void testInstallUnknownWiring() {
  printf("install: pushed from the setup page before the calculator ever spoke\n");
  for (int swapped = 0; swapped <= 1; swapped++) {
    sim::resetWires();
    Rig rig;
    rig.start();
    sim::Calc calc(swapped ? 7 : 5, swapped ? 5 : 7);
    rig.installWanted = true;
    std::string name;
    uint8_t type = 0;
    std::vector<uint8_t> var;
    bool got = false;
    // A real calculator shrugs off a garbled attempt; keep listening.
    for (int attempt = 0; attempt < 6 && !got; attempt++) got = calc.receiveSilent(name, type, var, 3000);
    CHECK(got);
    CHECK_EQ(name, "GPT");
    for (int i = 0; i < 2000 && rig.installResult < 0; i++) std::this_thread::sleep_for(std::chrono::milliseconds(5));
    CHECK(rig.installResult == app::SEND_OK);
    CHECK(rig.link.swapped() == (swapped != 0));
    CHECK(rig.wiringReports == 1);
    rig.shutdown();
  }
}

int main(int argc, char **argv) {
  const std::string root = (argc > 1) ? argv[1] : "..";
  setvbuf(stdout, nullptr, _IOLBF, 0);
  sim::verbose = (argc > 2);
  if (argc > 3) {  // soak mode: hammer the transfer paths
    for (int i = 0; i < atoi(argv[3]); i++) {
      testFullSession("soak: straight", false, 0, 0);
      testFullSession("soak: swapped", true, 0, 0);
      testFullSession("soak: swapped, slow", true, 60, 15);
      testFullSession("soak: straight, slow", false, 25, 40);
    }
    CHECK(sim::timingViolations == 0);
    printf("\n%d checks, %d failed\n", gChecks, gFailures);
    return gFailures ? 1 : 0;
  }

  testReals();
  testTokens(root);
  testLayout();
  testJson();
  testFullSession("wires as the firmware first guesses", false, 0, 0);
  testFullSession("tip and ring the other way round", true, 0, 0);
  testFullSession("slow calculator, slow-rising wires", true, 60, 15);
  testCancelAndStaleAnswer();
  testLinkFaults();
  testInstallWithoutCalculator();
  testInstallUnknownWiring();

  CHECK(sim::timingViolations == 0);
  printf("\n%d checks, %d failed\n", gChecks, gFailures);
  return gFailures ? 1 : 0;
}
