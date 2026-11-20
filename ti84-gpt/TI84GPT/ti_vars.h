// Calculator variable formats: real numbers and token strings.
#pragma once
#include <stddef.h>
#include <stdint.h>

#include <string>

namespace ti {

enum VarType : uint8_t {
  VAR_REAL = 0x00,
  VAR_LIST = 0x01,
  VAR_MATRIX = 0x02,
  VAR_YVAR = 0x03,
  VAR_STRING = 0x04,
  VAR_PROGRAM = 0x05,
  VAR_PROTECTED_PROGRAM = 0x06,
  VAR_PICTURE = 0x07,
};

const size_t kRealSize = 9;  // flags, exponent, 7 bytes of BCD digits

// 9-byte TI real -> double.
double realToDouble(const uint8_t *real);
// Whole number -> 9-byte TI real.
void intToReal(long value, uint8_t *real);

// Calculator tokens -> readable UTF-8 text ("sin(", "^2", "pi", ...).
std::string detokenize(const uint8_t *tokens, size_t len);

}  // namespace ti
