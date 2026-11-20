// Turns a chat answer (UTF-8, possibly with Markdown/LaTeX leftovers) into
// screens of calculator characters.
#pragma once
#include <stddef.h>
#include <stdint.h>

#include <string>
#include <vector>

namespace layout {

// One character cell on the calculator screen, stored as a TI token:
// 0x00XX for one-byte tokens, 0xXXYY for two-byte tokens.
typedef uint16_t Glyph;
const Glyph GLYPH_SPACE = 0x0029;
const Glyph GLYPH_NEWLINE = 0xFFFF;  // marker, never sent to the calculator

typedef std::vector<Glyph> Line;

// Strip Markdown and convert the most common LaTeX into plain text.
std::string cleanMarkup(const std::string &utf8);

// UTF-8 text -> glyphs. Characters the calculator cannot show are replaced by
// a close ASCII spelling or dropped. Stops after maxGlyphs.
void toGlyphs(const std::string &utf8, bool lowercase, std::vector<Glyph> &out, size_t maxGlyphs);

// Word-wrap into lines of at most `cols` glyphs.
void wrap(const std::vector<Glyph> &glyphs, int cols, std::vector<Line> &lines);

// Convenience: cleanMarkup + toGlyphs + wrap.
void layoutText(const std::string &utf8, bool lowercase, int cols, size_t maxGlyphs,
                std::vector<Line> &lines);

inline int pageCount(size_t lineCount, int rowsPerPage) {
  if (lineCount == 0) return 1;
  return (int)((lineCount + (size_t)rowsPerPage - 1) / (size_t)rowsPerPage);
}

// Build the token data for one full screen: `rows` text lines padded to
// `cols`, followed by one footer line. `page` is 1-based.
void renderPage(const std::vector<Line> &lines, int page, int cols, int rows,
                const std::string &footer, std::vector<uint8_t> &tokens);

}  // namespace layout
