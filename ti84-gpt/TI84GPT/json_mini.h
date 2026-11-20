// A very small JSON reader: just enough to pull a few values out of an API
// response without needing an extra library.
#pragma once
#include <string>

namespace json {

// All functions take [p, end) and return nullptr when the thing is not there.

const char *skipSpace(const char *p, const char *end);

// Returns the position just after the value starting at p.
const char *skipValue(const char *p, const char *end);

// p must point at '{'. Returns the start of the value stored under `key`.
const char *member(const char *p, const char *end, const char *key);

// p must point at '['. Returns the start of element `index`.
const char *element(const char *p, const char *end, int index);

// p must point at '"'. Decodes the string (escapes, \uXXXX) into UTF-8.
bool readString(const char *p, const char *end, std::string &out);

// Escape text for use inside a JSON string (without the surrounding quotes).
std::string escape(const std::string &text);

}  // namespace json
