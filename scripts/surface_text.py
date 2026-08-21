"""Reading TypeScript without a TypeScript loader.

The platform's surface tables are TypeScript source, and this repository has no
business growing a Node toolchain to read two of them. So they are read as text
and matched over — which works because both tables are literal data, and needs
this much care because both files are heavily commented and several of those
comments *quote the shapes being matched*. Over a raw file, the patterns in
``check_surface`` invent routes and parameters out of prose.

A direct port of the TypeScript SDK's ``scripts/surface-text.mjs``, function for
function. The two scripts read the same files and should not disagree about what
is in them: a parameter one of them cannot see is a gap the other reports alone,
which is a worse failure than neither seeing it.
"""

from __future__ import annotations

import re

_REGEX_CAN_FOLLOW = "([{=,:;!?&|~+*%^<>"
_REGEX_CAN_FOLLOW_WORDS = ("return", "case", "throw", "yield")
_TRAILING_WORD = re.compile(r"([A-Za-z_$][\w$]*)\Z")
_KEY = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*:")


def quoted_end(text: str, start: int) -> int:
    """One past the end of the single-, double-, or backtick-quoted literal at ``start``."""
    quote = text[start]
    i = start + 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == quote:
            return i + 1
        i += 1
    return len(text)


def regex_can_start(text: str, at: int) -> bool:
    """Whether a slash here can begin a regex literal rather than divide two values."""
    i = at - 1
    while i >= 0 and text[i].isspace():
        i -= 1
    if i < 0:
        return True
    if text[i] in _REGEX_CAN_FOLLOW:
        return True
    m = _TRAILING_WORD.search(text[: i + 1])
    return m is not None and m.group(1) in _REGEX_CAN_FOLLOW_WORDS


def regex_end(text: str, start: int) -> int:
    """One past the end of the regex literal at ``start``, flags included."""
    in_class = False
    i = start + 1
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "[":
            in_class = True
        elif ch == "]":
            in_class = False
        elif ch == "/" and not in_class:
            i += 1
            while i < len(text) and text[i].isalpha():
                i += 1
            return i
        elif ch in "\r\n":
            # An unterminated literal was never a literal. Treating it as one
            # would swallow the rest of the file.
            return start + 1
        i += 1
    return start + 1


def strip_comments(text: str) -> str:
    """Blank out comments without touching comment markers inside literals.

    Replaced with spaces rather than deleted, so every offset in the result still
    names the same character in the original — which is what lets a match here be
    used against the source it came from.
    """
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in "'\"`":
            end = quoted_end(text, i)
            out.append(text[i:end])
            i = end
        elif ch == "/" and text[i : i + 2] == "//":
            end = text.find("\n", i + 2)
            stop = len(text) if end == -1 else end
            out.append(" " * (stop - i))
            i = stop
        elif ch == "/" and text[i : i + 2] == "/*":
            end = text.find("*/", i + 2)
            stop = len(text) if end == -1 else end + 2
            # Newlines kept so line numbers survive; everything else spaced out.
            out.append(re.sub(r"[^\r\n]", " ", text[i:stop]))
            i = stop
        elif ch == "/" and regex_can_start(text, i):
            end = regex_end(text, i)
            out.append(text[i:end])
            i = end
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def balanced(text: str, start: int, open_ch: str, close_ch: str) -> str:
    """The text between the bracket at ``start`` and the one that balances it.

    Depth-counted rather than matched lazily to the next closer, because every
    table here nests: a route's ``query`` holds objects and a body's schema holds
    more of them, so the first ``]`` is nowhere near the end of the list.
    """
    depth = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch in "'\"`":
            i = quoted_end(text, i)
            continue
        if ch == "/" and text[i : i + 2] == "//":
            end = text.find("\n", i + 2)
            i = len(text) if end == -1 else end
            continue
        if ch == "/" and text[i : i + 2] == "/*":
            end = text.find("*/", i + 2)
            i = len(text) if end == -1 else end + 2
            continue
        if ch == "/" and regex_can_start(text, i):
            i = regex_end(text, i)
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start + 1 : i]
        i += 1
    raise ValueError(f"unbalanced {open_ch} from offset {start}")


def top_level_keys(body: str) -> list[str]:
    """The keys of an object literal, at its own depth only.

    A nested schema has keys of its own — ``type``, ``description``, ``items`` —
    and every one of them would otherwise read as a field of the body.
    """
    keys: list[str] = []
    depth = 0
    i = 0
    while i < len(body):
        ch = body[i]
        if ch in "'\"`":
            i = quoted_end(body, i)
            continue
        if ch == "/" and regex_can_start(body, i):
            i = regex_end(body, i)
            continue
        if ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
        elif depth == 0:
            m = _KEY.match(body, i)
            if m:
                keys.append(m.group(1))
                i = m.end()
                continue
        i += 1
    return keys
