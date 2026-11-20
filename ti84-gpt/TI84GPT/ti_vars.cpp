#include "ti_vars.h"

#include <string.h>

#include "ti_tokens.h"

namespace ti {

// TI real: byte 0 = flags (bit 7 = negative), byte 1 = exponent + 0x80,
// bytes 2..8 = 14 BCD digits d.ddddddddddddd
double realToDouble(const uint8_t *real) {
  double mantissa = 0;
  for (int i = 0; i < 14; i++) {
    const uint8_t pair = real[2 + (i >> 1)];
    const int digit = (i & 1) ? (pair & 0x0F) : (pair >> 4);
    mantissa = mantissa * 10.0 + digit;
  }
  // mantissa now holds all 14 digits as an integer: scale back to d.ddd...
  int exponent = (int)real[1] - 0x80 - 13;
  while (exponent > 0) {
    mantissa *= 10.0;
    exponent--;
  }
  while (exponent < 0) {
    mantissa /= 10.0;
    exponent++;
  }
  return (real[0] & 0x80) ? -mantissa : mantissa;
}

void intToReal(long value, uint8_t *real) {
  memset(real, 0, kRealSize);
  real[1] = 0x80;
  if (value == 0) return;
  if (value < 0) {
    real[0] = 0x80;
    value = -value;
  }
  char digits[24];
  int count = 0;
  while (value > 0 && count < 20) {
    digits[count++] = (char)(value % 10);
    value /= 10;
  }
  // digits[] is least-significant first; the most significant goes first.
  real[1] = (uint8_t)(0x80 + count - 1);
  for (int i = 0; i < 14 && i < count; i++) {
    const uint8_t digit = (uint8_t)digits[count - 1 - i];
    if (i & 1) {
      real[2 + (i >> 1)] |= digit;
    } else {
      real[2 + (i >> 1)] |= (uint8_t)(digit << 4);
    }
  }
}

std::string detokenize(const uint8_t *tokens, size_t len) {
  std::string out;
  out.reserve(len + 16);
  for (size_t i = 0; i < len; i++) {
    const uint8_t b = tokens[i];
    const char *text = nullptr;
    if (isTwoByteLead(b) && i + 1 < len) {
      text = tokenText2((uint16_t)((b << 8) | tokens[i + 1]));
      i++;
    } else {
      text = tokenText1(b);
    }
    out += text ? text : "?";
  }
  return out;
}

}  // namespace ti
