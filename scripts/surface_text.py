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
_KEY = re.compile(r"([A-Za-z_$][\w$]*)\s*:")
_IDENT_CHAR = re.compile(r"[\w$]")


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
    raise ValueError("unterminated quoted literal")


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


def top_level_value_at(body: str, name: str) -> int:
    """Where the value of one top-level key starts, or ``-1`` when it is not there.

    A sibling of :func:`top_level_keys` for the values it cannot read: the shape
    of a value is what decides how it is read — ``object(...)`` has named fields,
    a raw ``{ type: 'string' }`` schema has none to name, a bare identifier is a
    shape this cannot read at all — and telling those apart by searching the
    whole entry for a spelling answers about whichever one turns up first at any
    depth. A ``body: { … }`` quoted in a response example then vouches for the
    entry's own ``body: SHARED_BODY``, the unreadable shape passes as a readable
    one, and the route is compared against no fields at all.

    Depth-counted rather than found by ``str.find`` for that reason, and by the
    key rather than by ``key: `` for another: the one space after the colon is a
    spelling, not a shape, so a list the formatter wrapped over two lines —
    ``query:\n  [{ name: 'limit' }]`` — is still found here and was silently
    skipped before.

    Quoted spellings of the key as well: ``'body':`` names the same field as
    ``body:`` does, and a reader that insists on one of them reports the other
    as absent.
    """
    lit = re.escape(name)
    key = re.compile(rf"(?:'{lit}'|\"{lit}\"|{lit})\s*:\s*")
    depth = 0
    i = 0
    while i < len(body):
        # Before the literal skip below, so a quoted key is still seen; and not
        # mid-identifier, so `subbody:` is not read as `body:`.
        if depth == 0 and (i == 0 or not _IDENT_CHAR.match(body[i - 1])):
            m = key.match(body, i)
            if m:
                return m.end()
        ch = body[i]
        if ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
            if depth < 0:
                # As in its siblings: a depth that goes negative never comes
                # back, so the key would be reported missing rather than
                # unreadable — and missing is an answer this caller acts on.
                raise ValueError(f"unbalanced {ch} at offset {i}")
        elif ch in "'\"`":
            i = quoted_end(body, i)
            continue
        elif ch == "/" and regex_can_start(body, i):
            i = regex_end(body, i)
            continue
        i += 1
    return -1


def split_items(body: str) -> list[str]:
    """Split comma-separated entries, refusing incomplete or mismatched syntax.

    Literals and nested collections belong to an entry; their commas do not
    separate entries. A trailing comma is allowed, but a hole is unreadable.
    """
    items: list[str] = []
    stack: list[str] = []
    start = i = 0
    while i < len(body):
        ch = body[i]
        if ch in "'\"`":
            end = quoted_end(body, i)
            i = end
            continue
        if ch == "/" and regex_can_start(body, i):
            i = regex_end(body, i)
            continue
        if ch in "{[(":
            stack.append(ch)
        elif ch in "}])":
            if not stack or stack.pop() != {"}": "{", "]": "[", ")": "("}[ch]:
                raise ValueError(f"unbalanced {ch} at offset {i}")
        elif ch == "," and not stack:
            item = body[start:i].strip()
            if not item:
                raise ValueError("empty entry between separators")
            items.append(item)
            start = i + 1
        i += 1
    if stack:
        raise ValueError("unbalanced entry")
    tail = body[start:].strip()
    if tail:
        items.append(tail)
    return items


def literal_string(value: str) -> str:
    """Read a quoted name, without evaluating expressions or escape languages.

    Quotes, slashes and backslashes may be escaped. Other escapes in names
    require an explicit reader update instead of being guessed at.
    """
    value = value.strip()
    if not value or value[0] not in "'\"" or quoted_end(value, 0) != len(value):
        raise ValueError("expected a single- or double-quoted literal")
    quote = value[0]
    if len(value) < 2 or value[-1] != quote:
        raise ValueError("unterminated literal")
    out: list[str] = []
    i = 1
    while i < len(value) - 1:
        ch = value[i]
        if ch == "\\":
            i += 1
            if i >= len(value) - 1 or value[i] not in "'\"/\\":
                raise ValueError("unsupported escape in a name")
            ch = value[i]
        if ord(ch) < 32:
            raise ValueError("control character in a name")
        out.append(ch)
        i += 1
    return "".join(out)


def object_entries(body: str) -> dict[str, str]:
    """Read every object's own key/value pair, or refuse an unknown entry."""
    entries: dict[str, str] = {}
    for item in split_items(body):
        if item[0] in "'\"":
            end = quoted_end(item, 0)
            key = literal_string(item[:end])
            rest = item[end:].lstrip()
            if not rest.startswith(":"):
                raise ValueError("expected a colon after a quoted key")
            value = rest[1:].strip()
        else:
            match = _KEY.match(item)
            if match is None:
                raise ValueError("unsupported object entry")
            key, value = match.group(1), item[match.end() :].strip()
        if not value or key in entries:
            raise ValueError("missing value or duplicate object key")
        entries[key] = value
    return entries


def literal_contents(value: str, opening: str, closing: str) -> str:
    """Read one complete collection literal, with no trailing expression."""
    value = value.strip()
    if not value.startswith(opening):
        raise ValueError(f"expected a {opening}{closing} literal")
    body = balanced(value, 0, opening, closing)
    if value[len(body) + 2 :].strip():
        raise ValueError("unsupported expression after a literal")
    # Validate all delimiter kinds, not only the one the outer literal uses.
    split_items(body)
    return body


def top_level_keys(body: str) -> list[str]:
    """Every key at the object's own depth, including quoted spellings."""
    return list(object_entries(body))
