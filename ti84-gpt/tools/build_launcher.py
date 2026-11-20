#!/usr/bin/env python3
"""Tokenize launcher/GPT.txt (TI-BASIC source) for the TI-84 Plus.

Writes:
  TI84GPT/launcher_program.h   the program as a C array, built into the firmware
  launcher/GPT.8xp             the same program as a file for TI Connect

Run from the ti84-gpt folder (or anywhere):   python3 tools/build_launcher.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SOURCE = os.path.join(ROOT, "launcher", "GPT.txt")
HEADER = os.path.join(ROOT, "TI84GPT", "launcher_program.h")
FILE_8XP = os.path.join(ROOT, "launcher", "GPT.8xp")
PROGRAM_NAME = "GPT"

# --- TI-83+/84+ tokens -------------------------------------------------------
# Commands and operators recognised outside of "strings".
TOKENS = {
    "->": 0x04, "[": 0x06, "]": 0x07, "{": 0x08, "}": 0x09, "(": 0x10, ")": 0x11,
    "round(": 0x12, "max(": 0x19, "min(": 0x1A, " ": 0x29, '"': 0x2A, ",": 0x2B,
    "!": 0x2D, ".": 0x3A, " or ": 0x3C, " xor ": 0x3D, ":": 0x3E, " and ": 0x40,
    "theta": 0x5B, "prgm": 0x5F, "=": 0x6A, "<": 0x6B, ">": 0x6C, "<=": 0x6D,
    ">=": 0x6E, "!=": 0x6F, "+": 0x70, "-": 0x71, "Ans": 0x72, "*": 0x82,
    "/": 0x83, "ClrDraw": 0x85, "rand": 0xAB, "pi": 0xAC, "getKey": 0xAD,
    "'": 0xAE, "?": 0xAF, "~": 0xB0, "int(": 0xB1, "abs(": 0xB2, "not(": 0xB8,
    "iPart(": 0xB9, "fPart(": 0xBA, "If ": 0xCE, "Then": 0xCF, "Else": 0xD0,
    "While ": 0xD1, "Repeat ": 0xD2, "For(": 0xD3, "End": 0xD4, "Return": 0xD5,
    "Lbl ": 0xD6, "Goto ": 0xD7, "Pause ": 0xD8, "Stop": 0xD9, "IS>(": 0xDA,
    "DS<(": 0xDB, "Input ": 0xDC, "Prompt ": 0xDD, "Disp ": 0xDE,
    "DispGraph": 0xDF, "Output(": 0xE0, "ClrHome": 0xE1, "Menu(": 0xE6,
    "Send(": 0xE7, "Get(": 0xE8, "^": 0xF0,
    "randInt(": 0xBB0A, "sub(": 0xBB0C, "inString(": 0xBB0F, "expr(": 0xBB2A,
    "length(": 0xBB2B, "GetCalc(": 0xBB53, "DelVar ": 0xBB54,
}
for _i in range(10):
    TOKENS[str(_i)] = 0x30 + _i
    TOKENS["Str%d" % _i] = 0xAA00 + (9 if _i == 0 else _i - 1)
    TOKENS["Pic%d" % _i] = 0x6000 + (9 if _i == 0 else _i - 1)
for _i in range(26):
    TOKENS[chr(ord("A") + _i)] = 0x41 + _i
for _i in range(6):
    TOKENS["L%d" % (_i + 1)] = 0x5D00 + _i

# Characters allowed inside "strings" (where command names must stay text).
STRING_TOKENS = {
    " ": 0x29, "!": 0x2D, ",": 0x2B, ".": 0x3A, ":": 0x3E, "=": 0x6A, "<": 0x6B,
    ">": 0x6C, "+": 0x70, "-": 0x71, "*": 0x82, "/": 0x83, "'": 0xAE, "?": 0xAF,
    "(": 0x10, ")": 0x11, "[": 0x06, "]": 0x07, "{": 0x08, "}": 0x09, "^": 0xF0,
    "prgm": 0x5F, "#": 0xBBD2, "$": 0xBBD3, "%": 0xBBDA, "&": 0xBBD4,
    "@": 0xBBD1, ";": 0xBBD6, "_": 0xBBD9, "|": 0xBBD8, "~": 0xBBCF,
}
for _i in range(10):
    STRING_TOKENS[str(_i)] = 0x30 + _i
for _i in range(26):
    STRING_TOKENS[chr(ord("A") + _i)] = 0x41 + _i
    _c = chr(ord("a") + _i)  # lowercase letters: two-byte tokens, gap after k
    STRING_TOKENS[_c] = (0xBBB0 + _i) if _c <= "k" else (0xBBBC + _i - 11)

NEWLINE = 0x3F
QUOTE = 0x2A


def emit(out, code):
    if code > 0xFF:
        out.append(code >> 8)
    out.append(code & 0xFF)


def longest_match(table, line, pos):
    best = None
    for text in table:
        if line.startswith(text, pos) and (best is None or len(text) > len(best)):
            best = text
        # "Stop " style tokens may sit at the very end of a line without the space
        elif text.endswith(" ") and len(text) > 1 and line[pos:] == text[:-1]:
            if best is None or len(text) - 1 > len(best):
                best = text
    return best


def tokenize_line(line, number):
    out = []
    pos = 0
    in_string = False
    while pos < len(line):
        if line[pos] == '"':
            in_string = not in_string
            out.append(QUOTE)
            pos += 1
            continue
        table = STRING_TOKENS if in_string else TOKENS
        match = longest_match(table, line, pos)
        if match is None:
            sys.exit("%s:%d: cannot tokenize %r" % (SOURCE, number, line[pos:pos + 12]))
        emit(out, table[match])
        pos += min(len(match), len(line) - pos)
    return out


def tokenize(source):
    program = []
    first = True
    for number, raw in enumerate(source.splitlines(), 1):
        line = raw.rstrip("\r\n")
        if line.startswith("//") or not line.strip():
            continue
        if not first:
            program.append(NEWLINE)
        first = False
        program.extend(tokenize_line(line, number))
    return bytes(program)


def make_8xp(name, tokens):
    """Wrap a tokenized program in the .8xp file format."""
    var = len(tokens).to_bytes(2, "little") + tokens
    entry = (
        (13).to_bytes(2, "little")            # length of the header that follows
        + len(var).to_bytes(2, "little")
        + bytes([0x05])                       # program
        + name.encode("ascii").ljust(8, b"\0")
        + bytes([0x00, 0x00])                 # version, not archived
        + len(var).to_bytes(2, "little")
        + var
    )
    comment = b"TI84GPT launcher".ljust(42, b"\0")
    checksum = sum(entry) & 0xFFFF
    return (b"**TI83F*\x1a\x0a\x00" + comment + len(entry).to_bytes(2, "little")
            + entry + checksum.to_bytes(2, "little"))


def main():
    with open(SOURCE, "r", encoding="utf-8") as f:
        tokens = tokenize(f.read())

    lines = [
        "// Generated by tools/build_launcher.py from launcher/GPT.txt.",
        "// Do not edit by hand: change GPT.txt and run the script again.",
        "#pragma once",
        "#include <stddef.h>",
        "#include <stdint.h>",
        "",
        '#define LAUNCHER_NAME "%s"' % PROGRAM_NAME,
        "",
        "static const uint8_t kLauncherProgram[] = {",
    ]
    for i in range(0, len(tokens), 12):
        lines.append("    " + " ".join("0x%02X," % b for b in tokens[i:i + 12]))
    lines += ["};", "", "static const size_t kLauncherProgramSize = sizeof(kLauncherProgram);", ""]
    with open(HEADER, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))

    with open(FILE_8XP, "wb") as f:
        f.write(make_8xp(PROGRAM_NAME, tokens))

    print("prgm%s: %d bytes -> %s, %s" % (PROGRAM_NAME, len(tokens),
                                          os.path.relpath(HEADER, ROOT),
                                          os.path.relpath(FILE_8XP, ROOT)))


if __name__ == "__main__":
    main()
