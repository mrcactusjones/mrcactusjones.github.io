#include "json_mini.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

namespace json {

const char *skipSpace(const char *p, const char *end) {
  while (p && p < end && (*p == ' ' || *p == '\n' || *p == '\r' || *p == '\t')) p++;
  return p;
}

static const char *skipString(const char *p, const char *end) {
  if (!p || p >= end || *p != '"') return nullptr;
  p++;
  while (p < end) {
    if (*p == '\\') {
      p += 2;
    } else if (*p == '"') {
      return p + 1;
    } else {
      p++;
    }
  }
  return nullptr;
}

const char *skipValue(const char *p, const char *end) {
  p = skipSpace(p, end);
  if (!p || p >= end) return nullptr;
  if (*p == '"') return skipString(p, end);
  if (*p == '{' || *p == '[') {
    // Walk to the matching bracket, stepping over strings.
    int depth = 0;
    while (p < end) {
      if (*p == '"') {
        p = skipString(p, end);
        if (!p) return nullptr;
        continue;
      }
      if (*p == '{' || *p == '[') depth++;
      if (*p == '}' || *p == ']') {
        depth--;
        if (depth == 0) return p + 1;
      }
      p++;
    }
    return nullptr;
  }
  // number, true, false, null
  while (p < end && *p != ',' && *p != '}' && *p != ']' && *p != ' ' && *p != '\n' && *p != '\r' &&
         *p != '\t') {
    p++;
  }
  return p;
}

const char *member(const char *p, const char *end, const char *key) {
  p = skipSpace(p, end);
  if (!p || p >= end || *p != '{') return nullptr;
  p++;
  const size_t keyLen = strlen(key);
  for (;;) {
    p = skipSpace(p, end);
    if (!p || p >= end || *p != '"') return nullptr;  // '}' or malformed
    const char *nameStart = p + 1;
    const char *afterName = skipString(p, end);
    if (!afterName) return nullptr;
    const size_t nameLen = (size_t)(afterName - 1 - nameStart);
    p = skipSpace(afterName, end);
    if (!p || p >= end || *p != ':') return nullptr;
    p = skipSpace(p + 1, end);
    if (nameLen == keyLen && memcmp(nameStart, key, keyLen) == 0) return p;
    p = skipValue(p, end);
    p = skipSpace(p, end);
    if (!p || p >= end || *p != ',') return nullptr;
    p++;
  }
}

const char *element(const char *p, const char *end, int index) {
  p = skipSpace(p, end);
  if (!p || p >= end || *p != '[') return nullptr;
  p = skipSpace(p + 1, end);
  for (int i = 0;; i++) {
    if (!p || p >= end || *p == ']') return nullptr;
    if (i == index) return p;
    p = skipValue(p, end);
    p = skipSpace(p, end);
    if (!p || p >= end || *p != ',') return nullptr;
    p = skipSpace(p + 1, end);
  }
}

static int hexValue(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

static bool readHex4(const char *p, const char *end, uint32_t &value) {
  if (end - p < 4) return false;
  value = 0;
  for (int i = 0; i < 4; i++) {
    const int h = hexValue(p[i]);
    if (h < 0) return false;
    value = (value << 4) | (uint32_t)h;
  }
  return true;
}

static void appendUtf8(std::string &out, uint32_t cp) {
  if (cp < 0x80) {
    out += (char)cp;
  } else if (cp < 0x800) {
    out += (char)(0xC0 | (cp >> 6));
    out += (char)(0x80 | (cp & 0x3F));
  } else if (cp < 0x10000) {
    out += (char)(0xE0 | (cp >> 12));
    out += (char)(0x80 | ((cp >> 6) & 0x3F));
    out += (char)(0x80 | (cp & 0x3F));
  } else {
    out += (char)(0xF0 | (cp >> 18));
    out += (char)(0x80 | ((cp >> 12) & 0x3F));
    out += (char)(0x80 | ((cp >> 6) & 0x3F));
    out += (char)(0x80 | (cp & 0x3F));
  }
}

bool readString(const char *p, const char *end, std::string &out) {
  out.clear();
  if (!p || p >= end || *p != '"') return false;
  p++;
  while (p < end) {
    const char c = *p++;
    if (c == '"') return true;
    if (c != '\\') {
      out += c;
      continue;
    }
    if (p >= end) return false;
    const char e = *p++;
    switch (e) {
      case 'n': out += '\n'; break;
      case 't': out += '\t'; break;
      case 'r': out += '\r'; break;
      case 'b': out += '\b'; break;
      case 'f': out += '\f'; break;
      case 'u': {
        uint32_t cp;
        if (!readHex4(p, end, cp)) return false;
        p += 4;
        if (cp >= 0xD800 && cp <= 0xDBFF && end - p >= 6 && p[0] == '\\' && p[1] == 'u') {
          uint32_t low;
          if (readHex4(p + 2, end, low) && low >= 0xDC00 && low <= 0xDFFF) {
            cp = 0x10000 + ((cp - 0xD800) << 10) + (low - 0xDC00);
            p += 6;
          }
        }
        appendUtf8(out, cp);
        break;
      }
      default: out += e; break;  // \" \\ \/
    }
  }
  return false;
}

std::string escape(const std::string &text) {
  std::string out;
  out.reserve(text.size() + 16);
  for (size_t i = 0; i < text.size(); i++) {
    const unsigned char c = (unsigned char)text[i];
    switch (c) {
      case '"': out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\n': out += "\\n"; break;
      case '\r': out += "\\r"; break;
      case '\t': out += "\\t"; break;
      default:
        if (c < 0x20) {
          char buf[8];
          snprintf(buf, sizeof(buf), "\\u%04x", c);
          out += buf;
        } else {
          out += (char)c;
        }
    }
  }
  return out;
}

}  // namespace json
