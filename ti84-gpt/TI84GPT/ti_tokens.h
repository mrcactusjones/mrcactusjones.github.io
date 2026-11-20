// TI-83+/84+ token tables: what each calculator token looks like as text.
#pragma once
#include <stdint.h>

namespace ti {

// Text for a one-byte token, or nullptr if there is no such token.
const char *tokenText1(uint8_t token);
// Text for a two-byte token (lead byte in the high half), or nullptr.
const char *tokenText2(uint16_t token);

// Lead bytes that are followed by a second byte.
inline bool isTwoByteLead(uint8_t b) {
  return b == 0x5C || b == 0x5D || b == 0x5E || b == 0x60 || b == 0x61 || b == 0x62 ||
         b == 0x63 || b == 0x7E || b == 0xAA || b == 0xBB || b == 0xEF;
}

// Tokens the firmware itself needs by name.
const uint8_t TOK_SPACE = 0x29;
const uint8_t TOK_NEWLINE = 0x3F;
const uint8_t TOK_STR_LEAD = 0xAA;  // Str1..Str0 are 0xAA 0x00..0x09
const uint8_t TOK_PIC_LEAD = 0x60;

}  // namespace ti
