#include "text_layout.h"

#include <ctype.h>
#include <string.h>

namespace layout {

// ---------------------------------------------------------------------------
// Markdown / LaTeX clean-up
// ---------------------------------------------------------------------------

// Reads a balanced {...} group starting at s[i]; on success `i` ends up just
// past the closing brace.
static bool readGroup(const std::string &s, size_t &i, std::string &content) {
  if (i >= s.size() || s[i] != '{') return false;
  int depth = 0;
  for (size_t j = i; j < s.size(); j++) {
    if (s[j] == '{') {
      depth++;
    } else if (s[j] == '}') {
      depth--;
      if (depth == 0) {
        content = s.substr(i + 1, j - i - 1);
        i = j + 1;
        return true;
      }
    }
  }
  return false;
}

// True when `t` can sit next to "/" or "^" without brackets: a plain number
// or a single symbol. ("2a" is not simple: x/2a would read as (x/2)*a.)
static bool isSimple(const std::string &t) {
  if (t.empty()) return false;
  bool number = true;
  size_t symbols = 0;
  for (size_t i = 0; i < t.size(); i++) {
    const unsigned char c = (unsigned char)t[i];
    if (!(isdigit(c) || c == '.')) number = false;
    if (!(isalnum(c) || c >= 0x80)) return number && c == '.';
    if ((c & 0xC0) != 0x80) symbols++;  // count characters, not UTF-8 bytes
  }
  return number || symbols == 1;
}

static std::string paren(const std::string &t) { return isSimple(t) ? t : "(" + t + ")"; }

struct Replacement {
  const char *name;
  const char *text;
};

// LaTeX commands -> plain text. Greek letters and relations use the real
// characters; toGlyphs() turns those into the matching calculator symbols.
static const Replacement kLatex[] = {
    {"cdot", "*"},      {"times", "*"},      {"ast", "*"},       {"div", "/"},
    {"pm", "+/-"},      {"mp", "-/+"},       {"le", "≤"},   {"leq", "≤"},
    {"ge", "≥"},   {"geq", "≥"},   {"ne", "≠"},   {"neq", "≠"},
    {"approx", "~"},    {"sim", "~"},        {"infty", "inf"},   {"to", "->"},
    {"rightarrow", "->"}, {"Rightarrow", "=>"}, {"implies", "=>"}, {"iff", "<=>"},
    {"leftarrow", "<-"}, {"left", ""},       {"right", ""},      {"quad", " "},
    {"qquad", " "},     {"displaystyle", ""}, {"cdots", "..."},  {"ldots", "..."},
    {"dots", "..."},    {"degree", "°"}, {"circ", "°"}, {"int", "∫"},
    {"sum", "Σ"},  {"partial", "d"},    {"angle", "∠"}, {"pi", "π"},
    {"theta", "θ"}, {"alpha", "α"}, {"beta", "β"}, {"gamma", "γ"},
    {"Delta", "Δ"}, {"delta", "δ"}, {"epsilon", "ε"}, {"varepsilon", "ε"},
    {"lambda", "λ"}, {"mu", "μ"},  {"rho", "ρ"},  {"Sigma", "Σ"},
    {"sigma", "σ"}, {"tau", "τ"},  {"phi", "φ"},  {"Phi", "Φ"},
    {"Omega", "Ω"}, {"chi", "χ"},  {"sqrt", "sqrt"},   {"frac", "frac"},
};

static const char *const kUnwrap[] = {"text",   "mathrm", "mathbf",       "mathit", "textbf",
                                      "textit", "boxed",  "operatorname", "mathbb", "mathcal"};

static std::string cleanMath(const std::string &s, int depth) {
  if (depth > 6) return s;
  std::string out;
  out.reserve(s.size());
  size_t i = 0;
  while (i < s.size()) {
    const char c = s[i];
    if (c == '\\') {
      if (i + 1 >= s.size()) break;
      const char n = s[i + 1];
      if (n == '(' || n == ')' || n == '[' || n == ']') {
        i += 2;
        continue;
      }
      if (n == '\\') {
        out += '\n';
        i += 2;
        continue;
      }
      if (n == ',' || n == ';' || n == ':' || n == ' ') {
        out += ' ';
        i += 2;
        continue;
      }
      if (n == '!') {
        i += 2;
        continue;
      }
      if (n == '%' || n == '$' || n == '{' || n == '}' || n == '_' || n == '&' || n == '#') {
        out += n;
        i += 2;
        continue;
      }
      if (isalpha((unsigned char)n)) {
        size_t j = i + 1;
        while (j < s.size() && isalpha((unsigned char)s[j])) j++;
        const std::string name = s.substr(i + 1, j - i - 1);
        i = j;

        if (name == "frac" || name == "dfrac" || name == "tfrac") {
          std::string a, b;
          size_t k = i;
          if (readGroup(s, k, a) && readGroup(s, k, b)) {
            out += paren(cleanMath(a, depth + 1)) + "/" + paren(cleanMath(b, depth + 1));
            i = k;
            continue;
          }
        } else if (name == "sqrt") {
          std::string root, a;
          size_t k = i;
          if (k < s.size() && s[k] == '[') {
            const size_t close = s.find(']', k);
            if (close != std::string::npos) {
              root = s.substr(k + 1, close - k - 1);
              k = close + 1;
            }
          }
          if (readGroup(s, k, a)) {
            if (root.empty()) {
              out += "sqrt(" + cleanMath(a, depth + 1) + ")";
            } else {
              out += "(" + cleanMath(a, depth + 1) + ")^(1/" + cleanMath(root, depth + 1) + ")";
            }
            i = k;
            continue;
          }
        } else if (name == "begin" || name == "end") {
          std::string ignored;
          readGroup(s, i, ignored);
          continue;
        }

        bool handled = false;
        for (size_t u = 0; u < sizeof(kUnwrap) / sizeof(kUnwrap[0]); u++) {
          if (name == kUnwrap[u]) {
            std::string a;
            if (readGroup(s, i, a)) out += cleanMath(a, depth + 1);
            handled = true;
            break;
          }
        }
        if (handled) continue;

        const char *rep = nullptr;
        for (size_t r = 0; r < sizeof(kLatex) / sizeof(kLatex[0]); r++) {
          if (name == kLatex[r].name) {
            rep = kLatex[r].text;
            break;
          }
        }
        out += rep ? rep : name.c_str();  // \sin -> sin, \ln -> ln, ...
        continue;
      }
      i++;  // a backslash on its own: drop it
      continue;
    }

    if ((c == '^' || c == '_') && i + 1 < s.size() && s[i + 1] == '{') {
      size_t k = i + 1;
      std::string a;
      if (readGroup(s, k, a)) {
        const std::string inner = cleanMath(a, depth + 1);
        out += c;
        out += (c == '^') ? paren(inner) : inner;
        i = k;
        continue;
      }
    }

    out += c;
    i++;
  }
  return out;
}

static std::string cleanMarkdownLine(const std::string &line) {
  size_t i = 0;
  while (i < line.size() && (line[i] == ' ' || line[i] == '\t')) i++;

  // Headings and block quotes.
  size_t j = i;
  while (j < line.size() && line[j] == '#') j++;
  if (j > i && (j == line.size() || line[j] == ' ')) i = j;
  while (i < line.size() && (line[i] == '>' || line[i] == ' ')) {
    if (line[i] == '>' && !(i + 1 < line.size() && line[i + 1] == '=')) {
      i++;
    } else if (line[i] == ' ') {
      i++;
    } else {
      break;
    }
  }

  // Horizontal rules: a line made only of -, *, _ and spaces (3 or more).
  {
    size_t marks = 0;
    bool only = true;
    for (size_t k = i; k < line.size(); k++) {
      if (line[k] == '-' || line[k] == '*' || line[k] == '_') {
        marks++;
      } else if (line[k] != ' ') {
        only = false;
        break;
      }
    }
    if (only && marks >= 3) return std::string();
  }

  std::string out;
  // "* item" / "+ item" bullets become "- item".
  if (i + 1 < line.size() && (line[i] == '*' || line[i] == '+') && line[i + 1] == ' ') {
    out += "- ";
    i += 2;
  }

  while (i < line.size()) {
    const char c = line[i];
    const char n = (i + 1 < line.size()) ? line[i + 1] : '\0';
    if ((c == '*' && n == '*') || (c == '_' && n == '_') || (c == '~' && n == '~')) {
      i += 2;
      continue;
    }
    if (c == '`') {
      i++;
      continue;
    }
    if (c == '$' && !(n >= '0' && n <= '9')) {  // math delimiter, not a price
      i++;
      continue;
    }
    out += c;
    i++;
  }
  return out;
}

std::string cleanMarkup(const std::string &utf8) {
  const std::string math = cleanMath(utf8, 0);
  std::string out;
  out.reserve(math.size());
  size_t start = 0;
  while (start <= math.size()) {
    size_t end = math.find('\n', start);
    if (end == std::string::npos) end = math.size();
    std::string line = math.substr(start, end - start);
    if (!line.empty() && line[line.size() - 1] == '\r') line.erase(line.size() - 1);
    out += cleanMarkdownLine(line);
    if (end < math.size()) out += '\n';
    start = end + 1;
  }
  return out;
}

// ---------------------------------------------------------------------------
// Text -> calculator glyphs
// ---------------------------------------------------------------------------

static Glyph asciiGlyph(char c, bool lowercase) {
  if ((c >= '0' && c <= '9') || (c >= 'A' && c <= 'Z')) return (Glyph)c;
  if (c >= 'a' && c <= 'z') {
    if (!lowercase) return (Glyph)(c - 'a' + 'A');
    // Lowercase letters are two-byte tokens with a gap after 'k'.
    return (c <= 'k') ? (Glyph)(0xBBB0 + (c - 'a')) : (Glyph)(0xBBBC + (c - 'l'));
  }
  switch (c) {
    case ' ': return 0x29;
    case '!': return 0x2D;
    case '"': return 0x2A;
    case '#': return 0xBBD2;
    case '$': return 0xBBD3;
    case '%': return 0xBBDA;
    case '&': return 0xBBD4;
    case '\'': return 0xAE;
    case '(': return 0x10;
    case ')': return 0x11;
    case '*': return 0x82;
    case '+': return 0x70;
    case ',': return 0x2B;
    case '-': return 0x71;
    case '.': return 0x3A;
    case '/': return 0x83;
    case ':': return 0x3E;
    case ';': return 0xBBD6;
    case '<': return 0x6B;
    case '=': return 0x6A;
    case '>': return 0x6C;
    case '?': return 0xAF;
    case '@': return 0xBBD1;
    case '[': return 0x06;
    case '\\': return 0xBBD7;
    case ']': return 0x07;
    case '^': return 0xF0;
    case '_': return 0xBBD9;
    case '`': return 0xBBD5;
    case '{': return 0x08;
    case '|': return 0xBBD8;
    case '}': return 0x09;
    case '~': return 0xBBCF;
    default: return 0;
  }
}

struct UnicodeMap {
  uint32_t cp;
  Glyph glyph;        // calculator symbol, or 0 to use `ascii`
  const char *ascii;  // replacement spelling
};

static const UnicodeMap kUnicode[] = {
    {0x00A0, 0x29, nullptr},    // no-break space
    {0x00A1, 0xBB9E, nullptr},  // inverted !
    {0x00B0, 0x0B, nullptr},    // degree
    {0x00B1, 0, "+/-"},
    {0x00B2, 0x0D, nullptr},    // squared
    {0x00B3, 0x0F, nullptr},    // cubed
    {0x00B5, 0xBBA6, nullptr},  // micro
    {0x00B7, 0, "*"},
    {0x00B9, 0, "^1"},
    {0x00BC, 0, "1/4"},
    {0x00BD, 0, "1/2"},
    {0x00BE, 0, "3/4"},
    {0x00BF, 0xBB9D, nullptr},  // inverted ?
    {0x00C0, 0xBB6F, nullptr},  {0x00C1, 0xBB6E, nullptr},  {0x00C2, 0xBB70, nullptr},
    {0x00C3, 0, "A"},           {0x00C4, 0xBB71, nullptr},  {0x00C5, 0, "A"},
    {0x00C6, 0, "AE"},          {0x00C7, 0xBB96, nullptr},  {0x00C8, 0xBB77, nullptr},
    {0x00C9, 0xBB76, nullptr},  {0x00CA, 0xBB78, nullptr},  {0x00CB, 0xBB79, nullptr},
    {0x00CC, 0xBB7F, nullptr},  {0x00CD, 0xBBCD, nullptr},  {0x00CE, 0xBB80, nullptr},
    {0x00CF, 0xBB81, nullptr},  {0x00D1, 0xBB98, nullptr},  {0x00D2, 0xBB87, nullptr},
    {0x00D3, 0xBB86, nullptr},  {0x00D4, 0xBB88, nullptr},  {0x00D5, 0, "O"},
    {0x00D6, 0xBB89, nullptr},  {0x00D7, 0, "*"},           {0x00D8, 0, "O"},
    {0x00D9, 0xBB8F, nullptr},  {0x00DA, 0xBB8E, nullptr},  {0x00DB, 0xBB90, nullptr},
    {0x00DC, 0xBB91, nullptr},  {0x00DF, 0xBBDD, nullptr},  {0x00E0, 0xBB73, nullptr},
    {0x00E1, 0xBB72, nullptr},  {0x00E2, 0xBB74, nullptr},  {0x00E3, 0, "a"},
    {0x00E4, 0xBB75, nullptr},  {0x00E5, 0, "a"},           {0x00E6, 0, "ae"},
    {0x00E7, 0xBB97, nullptr},  {0x00E8, 0xBB7B, nullptr},  {0x00E9, 0xBB7A, nullptr},
    {0x00EA, 0xBB7C, nullptr},  {0x00EB, 0xBB7D, nullptr},  {0x00EC, 0xBB83, nullptr},
    {0x00ED, 0xBB82, nullptr},  {0x00EE, 0xBB84, nullptr},  {0x00EF, 0xBB85, nullptr},
    {0x00F1, 0xBB99, nullptr},  {0x00F2, 0xBB8B, nullptr},  {0x00F3, 0xBB8A, nullptr},
    {0x00F4, 0xBB8C, nullptr},  {0x00F5, 0, "o"},           {0x00F6, 0xBB8D, nullptr},
    {0x00F7, 0, "/"},           {0x00F8, 0, "o"},           {0x00F9, 0xBB93, nullptr},
    {0x00FA, 0xBB92, nullptr},  {0x00FB, 0xBB94, nullptr},  {0x00FC, 0xBB95, nullptr},
    {0x00FD, 0, "y"},           {0x00FF, 0, "y"},
    {0x0394, 0xBBA2, nullptr},  // Delta
    {0x03A3, 0xBBA9, nullptr},  // Sigma
    {0x03A6, 0xBBAB, nullptr},  // Phi
    {0x03A9, 0xBBAC, nullptr},  // Omega
    {0x03B1, 0xBB9F, nullptr},  // alpha
    {0x03B2, 0xBBA0, nullptr},  // beta
    {0x03B3, 0xBBA1, nullptr},  // gamma
    {0x03B4, 0xBBA3, nullptr},  // delta
    {0x03B5, 0xBBA4, nullptr},  // epsilon
    {0x03B8, 0x5B, nullptr},    // theta
    {0x03BB, 0xBBA5, nullptr},  // lambda
    {0x03BC, 0xBBA6, nullptr},  // mu
    {0x03C0, 0xAC, nullptr},    // pi
    {0x03C1, 0xBBA8, nullptr},  // rho
    {0x03C3, 0xBBCB, nullptr},  // sigma
    {0x03C4, 0xBBCC, nullptr},  // tau
    {0x03C6, 0xBBAB, nullptr},  // phi
    {0x03C7, 0xBBAE, nullptr},  // chi
    {0x2010, 0, "-"},           {0x2011, 0, "-"},           {0x2012, 0, "-"},
    {0x2013, 0, "-"},           {0x2014, 0, "-"},           {0x2015, 0, "-"},
    {0x2018, 0, "'"},           {0x2019, 0, "'"},           {0x201C, 0, "\""},
    {0x201D, 0, "\""},          {0x2022, 0, "-"},           {0x2026, 0, "..."},
    {0x2032, 0, "'"},           {0x2033, 0, "\""},
    {0x2070, 0, "^0"},          {0x2074, 0, "^4"},          {0x2075, 0, "^5"},
    {0x2076, 0, "^6"},          {0x2077, 0, "^7"},          {0x2078, 0, "^8"},
    {0x2079, 0, "^9"},          {0x207A, 0, "^+"},          {0x207B, 0, "^-"},
    {0x207F, 0, "^n"},
    {0x2080, 0xBBE0, nullptr},  {0x2081, 0xBBE1, nullptr},  {0x2082, 0xBBE2, nullptr},
    {0x2083, 0xBBE3, nullptr},  {0x2084, 0xBBE4, nullptr},  {0x2085, 0xBBE5, nullptr},
    {0x2086, 0xBBE6, nullptr},  {0x2087, 0xBBE7, nullptr},  {0x2088, 0xBBE8, nullptr},
    {0x2089, 0xBBE9, nullptr},
    {0x2190, 0, "<-"},          {0x2192, 0, "->"},          {0x21D2, 0, "=>"},
    {0x21D4, 0, "<=>"},
    {0x2202, 0, "d"},           // partial derivative
    {0x2211, 0xBBA9, nullptr},  // summation
    {0x2212, 0, "-"},           // minus sign
    {0x2215, 0, "/"},
    {0x2217, 0, "*"},
    {0x2219, 0, "*"},
    {0x221A, 0xBBF4, nullptr},  // square root
    {0x221E, 0, "inf"},
    {0x2220, 0xBBDC, nullptr},  // angle
    {0x222B, 0xBBF1, nullptr},  // integral
    {0x2248, 0, "~"},
    {0x2260, 0x6F, nullptr},    // not equal
    {0x2264, 0x6D, nullptr},    // less or equal
    {0x2265, 0x6E, nullptr},    // greater or equal
    {0x22C5, 0, "*"},
    {0x2705, 0, ""},
};

static uint32_t nextCodepoint(const std::string &s, size_t &i) {
  const unsigned char c = (unsigned char)s[i++];
  if (c < 0x80) return c;
  int extra;
  uint32_t cp;
  if (c >= 0xF0) {
    extra = 3;
    cp = c & 0x07;
  } else if (c >= 0xE0) {
    extra = 2;
    cp = c & 0x0F;
  } else if (c >= 0xC0) {
    extra = 1;
    cp = c & 0x1F;
  } else {
    return 0xFFFD;  // stray continuation byte
  }
  while (extra-- > 0) {
    if (i >= s.size() || ((unsigned char)s[i] & 0xC0) != 0x80) return 0xFFFD;
    cp = (cp << 6) | ((unsigned char)s[i++] & 0x3F);
  }
  return cp;
}

static void push(std::vector<Glyph> &out, Glyph g, size_t maxGlyphs) {
  if (out.size() >= maxGlyphs) return;
  // Runs of spaces are pointless on a 16-column screen.
  if (g == GLYPH_SPACE && !out.empty() && out.back() == GLYPH_SPACE) return;
  out.push_back(g);
}

void toGlyphs(const std::string &utf8, bool lowercase, std::vector<Glyph> &out, size_t maxGlyphs) {
  size_t i = 0;
  while (i < utf8.size() && out.size() < maxGlyphs) {
    const uint32_t cp = nextCodepoint(utf8, i);
    if (cp == '\n') {
      push(out, GLYPH_NEWLINE, maxGlyphs);
      continue;
    }
    if (cp == '\t') {
      push(out, GLYPH_SPACE, maxGlyphs);
      continue;
    }
    if (cp < 0x80) {
      const Glyph g = (cp >= 0x20) ? asciiGlyph((char)cp, lowercase) : 0;
      if (g) push(out, g, maxGlyphs);
      continue;
    }
    for (size_t k = 0; k < sizeof(kUnicode) / sizeof(kUnicode[0]); k++) {
      if (kUnicode[k].cp != cp) continue;
      if (kUnicode[k].glyph) {
        push(out, kUnicode[k].glyph, maxGlyphs);
      } else {
        for (const char *p = kUnicode[k].ascii; *p; p++) {
          const Glyph g = asciiGlyph(*p, lowercase);
          if (g) push(out, g, maxGlyphs);
        }
      }
      break;
    }
    // Anything else (emoji, other scripts) is dropped.
  }
}

// ---------------------------------------------------------------------------
// Word wrap and page rendering
// ---------------------------------------------------------------------------

void wrap(const std::vector<Glyph> &glyphs, int cols, std::vector<Line> &lines) {
  lines.clear();
  if (cols < 1) return;
  const size_t width = (size_t)cols;
  Line line, word;
  bool spacePending = false;

  for (size_t idx = 0; idx <= glyphs.size(); idx++) {
    const bool atEnd = (idx == glyphs.size());
    const Glyph g = atEnd ? GLYPH_NEWLINE : glyphs[idx];

    if (g != GLYPH_NEWLINE && g != GLYPH_SPACE) {
      word.push_back(g);
      continue;
    }

    // A word just ended: place it.
    if (!word.empty()) {
      const size_t need = word.size() + ((spacePending && !line.empty()) ? 1 : 0);
      if (!line.empty() && line.size() + need > width) {
        lines.push_back(line);
        line.clear();
      }
      if (!line.empty() && spacePending) line.push_back(GLYPH_SPACE);
      size_t i = 0;
      if (line.empty()) {
        // Words longer than a whole line are simply cut.
        while (word.size() - i > width) {
          lines.push_back(Line(word.begin() + (long)i, word.begin() + (long)(i + width)));
          i += width;
        }
      }
      line.insert(line.end(), word.begin() + (long)i, word.end());
      word.clear();
    }
    spacePending = (g == GLYPH_SPACE);

    if (g == GLYPH_NEWLINE && !line.empty()) {
      lines.push_back(line);
      line.clear();
    }
  }
}

void layoutText(const std::string &utf8, bool lowercase, int cols, size_t maxGlyphs,
                std::vector<Line> &lines) {
  std::vector<Glyph> glyphs;
  toGlyphs(cleanMarkup(utf8), lowercase, glyphs, maxGlyphs);
  wrap(glyphs, cols, lines);
}

static void emit(std::vector<uint8_t> &tokens, Glyph g) {
  if (g > 0xFF) tokens.push_back((uint8_t)(g >> 8));
  tokens.push_back((uint8_t)(g & 0xFF));
}

void renderPage(const std::vector<Line> &lines, int page, int cols, int rows,
                const std::string &footer, std::vector<uint8_t> &tokens) {
  tokens.clear();
  if (page < 1) page = 1;
  const size_t first = (size_t)(page - 1) * (size_t)rows;
  for (int r = 0; r < rows; r++) {
    const size_t idx = first + (size_t)r;
    int n = 0;
    if (idx < lines.size()) {
      for (size_t k = 0; k < lines[idx].size() && n < cols; k++, n++) emit(tokens, lines[idx][k]);
    }
    for (; n < cols; n++) emit(tokens, GLYPH_SPACE);
  }

  int n = 0;
  for (size_t k = 0; k < footer.size() && n < cols; k++) {
    const Glyph g = asciiGlyph(footer[k], false);
    if (g) {
      emit(tokens, g);
      n++;
    }
  }
  for (; n < cols; n++) emit(tokens, GLYPH_SPACE);
}

}  // namespace layout
