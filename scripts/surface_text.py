"""Reading TypeScript without a TypeScript loader.

The platform's surface tables are TypeScript source, and this repository has no
business growing a Node toolchain to read two of them. So they are read as text
and matched over — which works because both tables are literal data, and needs
this much care because both files are heavily commented and several of those
comments *quote the shapes being matched*. Over a raw file, the patterns in
``check_surface`` invent routes and parameters out of prose.

This bounded reader handles literal inventories and the lexical boundaries
needed to locate them. A declaration it cannot read is refused explicitly,
so an unsupported expression cannot silently become a missing route or an
empty parameter inventory.

It was a direct port of the TypeScript SDK's ``scripts/surface-text.mjs`` and no
longer is: that reader, and the MCP SDK's copy of it, still match parameter
names with a single-quote regex and drop what they cannot read. So a refusal
here says nothing about them — the same upstream shape can pass there and be
missing from their mirrors, which is the failure this file stopped having.
"""

from __future__ import annotations

import re
from typing import Literal

_REGEX_CAN_FOLLOW = "([{=,:;!?&|~+*%^<>"
_REGEX_CAN_FOLLOW_WORDS = ("return", "case", "throw", "yield")
_TRAILING_WORD = re.compile(r"([A-Za-z_$][\w$]*)\Z")
_KEY = re.compile(r"([A-Za-z_$][\w$]*)\s*:")
_IDENT_CHAR = re.compile(r"[\w$]")
#: How far back a slash's role is decided from. Longer than any operand this looks
#: for, and bounded so that one pass over a file stays linear in its length.
_LOOKBACK = 256
_MEMBER_OPERAND = re.compile(r"\.[A-Za-z_$][\w$]*[ \t]*\Z")
#: A value a slash can only divide: a number, or a name that is not one of the
#: keywords a regex is allowed to follow. Anything else — a closing parenthesis
#: or bracket above all — stays undecidable here and is refused rather than
#: guessed at, because a control condition's ``)`` is exactly what precedes the
#: regex literals this reader must not walk into.
_DIVISION_OPERAND = re.compile(r"(?:[0-9][\w.]*|[A-Za-z_$][\w$]*)[ \t]*\Z")
#: A postfix increment or decrement — a VALUE, and one whose last character is an
#: operator a regex is otherwise allowed to follow. The operand before the `++` is
#: required: bare `++ /re/.lastIndex` is a PREFIX update of a regex property, which
#: is legal and is not a division (adversarial review, OPL-4805).
_POSTFIX_OPERAND = re.compile(r"[\w$\])]\s*(?:\+\+|--)\s*\Z")
#: The same three operands as `_MEMBER_OPERAND` and `_DIVISION_OPERAND`, allowing a
#: LINE BREAK between the value and the slash. Their own tails stop at a space or a
#: tab on purpose, because `checked_slash_end` reads raw source, where looking back
#: over a newline can borrow a token out of a line comment. `certain_operator`
#: reads a prefix whose comments are already blanked, so it can afford the longer
#: reach — and needs it, since `obj.return` on the line above a slash is the same
#: division as `obj.return` beside it (adversarial review, OPL-4805). ASI does not
#: change that: `a` then a newline then `/b/` is a division in JavaScript too.
_MEMBER_OPERAND_LINES = re.compile(r"\.[A-Za-z_$][\w$]*\s*\Z")
_DIVISION_OPERAND_LINES = re.compile(r"(?:[0-9][\w.]*|[A-Za-z_$][\w$]*)\s*\Z")
#: Words that are not values, so a slash after one is not a division. Spelled
#: as every reserved and contextual word there is rather than as the handful
#: that can lead a regex, because the cost of the two mistakes is not the same:
#: a word wrongly listed here loses a division and refuses, and a word left out
#: reads a regex as arithmetic and walks into its body — where a ``}`` closes a
#: scope nobody opened and a backtick ends a template. Enumerating the openers
#: is how ``export default /…/`` and ``extends /…/`` got in, both of which
#: carried a whole fake route table inside a regex. The five reserved words
#: that ARE values — ``this``, ``super``, ``true``, ``false`` and ``null`` —
#: are deliberately absent.
_NOT_A_VALUE = frozenset(
    {
        "abstract",
        "as",
        "asserts",
        "async",
        "await",
        "break",
        "case",
        "catch",
        "class",
        "const",
        "continue",
        "debugger",
        "declare",
        "default",
        "delete",
        "do",
        "else",
        "enum",
        "export",
        "extends",
        "finally",
        "for",
        "from",
        "function",
        "if",
        "implements",
        "import",
        "in",
        "infer",
        "instanceof",
        "interface",
        "is",
        "keyof",
        "let",
        "namespace",
        "new",
        "of",
        "out",
        "override",
        "package",
        "private",
        "protected",
        "public",
        "readonly",
        "return",
        "satisfies",
        "static",
        "switch",
        "throw",
        "try",
        "type",
        "typeof",
        "unique",
        "var",
        "void",
        "while",
        "with",
        "yield",
    }
)


def quoted_end(text: str, start: int) -> int:
    """One past the end of the single-, double-, or backtick-quoted literal at ``start``."""
    quote = text[start]
    i = start + 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if quote == "`" and text[i : i + 2] == "${":
            # Interpolations may contain strings or nested templates of their
            # own. Their quotes cannot terminate the surrounding template.
            contents = balanced(text, i + 1, "{", "}", strict_slashes=True)
            i += len(contents) + 3
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


def position(text: str, offset: int) -> str:
    """Where an offset is, in the terms the file's own editor uses.

    Every refusal in this reader names a place, and an offset is not one: the
    run that enforces this comparison is the platform's CI, so the person who
    reads the message is looking at *their* file for the construct it cannot
    read. A byte count sends them counting; a line and column is where the
    cursor goes.
    """
    line = text.count("\n", 0, offset) + 1
    return f"line {line} column {offset - text.rfind(chr(10), 0, offset)}"


def checked_slash_end(text: str, start: int) -> int:
    """Skip a known regex or division operator, refusing undecidable slash roles.

    Use before interpreting delimiters in code, including template
    interpolations: regex braces and backticks must never alter lexical scope.
    """
    # A same-line member access is an operand, even with a keyword property.
    # Never borrow a member-looking suffix from a preceding line comment.
    before = text[:start]
    if _MEMBER_OPERAND.search(before):
        return start + 1
    # Ordinary arithmetic over a number or a plain name, which is what `60 * 60
    # / 2` in a document being read is: a slash cannot open a regex after a
    # value, so nothing here is a guess. Refusing it made every legal division
    # upstream a failed comparison in the platform's CI, over source this
    # reader does not otherwise care about. A reserved word is not a value —
    # see `_NOT_A_VALUE` — and a closing parenthesis or bracket is still
    # undecidable, so both fall through to the refusal below.
    operand = _DIVISION_OPERAND.search(before)
    if operand and operand.group(0).strip() not in _NOT_A_VALUE:
        return start + 1
    if not regex_can_start(text, start):
        raise ValueError(f"ambiguous slash at {position(text, start)}")
    end = regex_end(text, start)
    if end == start + 1:
        raise ValueError(f"unreadable regex literal at {position(text, start)}")
    return end


def certain_operator(before: str) -> bool:
    """Whether a slash straight after ``before`` can ONLY divide — it follows a value.

    The three shapes a value ends in: a member access, a postfix ``++`` or ``--``
    with an operand in front of it, and a number or a name that is not a reserved
    word. ``checked_slash_end`` asks the first and the third for the same reason: a
    regex cannot begin after a value, so these positions are not guesses at all.

    ``before`` must be the text up to the slash with its COMMENTS already blanked.
    The reach here crosses line breaks, and over raw source that would let a name
    inside a line comment answer for the code below it.

    ``regex_can_start`` does not subsume this and asking it first is the point.
    That predicate looks at the character before the slash, so ``x++ / 2`` reads as
    a slash after ``+`` and ``obj.return / 2`` as a slash after the keyword
    ``return`` — both of which it calls regex positions, and both of which are
    divisions (adversarial review, OPL-4805).
    """
    if _MEMBER_OPERAND_LINES.search(before) or _POSTFIX_OPERAND.search(before):
        return True
    operand = _DIVISION_OPERAND_LINES.search(before)
    return bool(operand and operand.group(0).strip() not in _NOT_A_VALUE)


def _reads_as_regex(
    before: str, text: str, at: int, undecided: Literal["operator", "regex"]
) -> bool:
    """Whether to read the slash at ``at`` as opening a regex literal.

    Certain divisions first, then ``regex_can_start`` where it is confident, then
    the caller's policy for the positions nothing here can decide — see
    ``strip_comments``. ``before`` is the comment-blanked text up to the slash.
    """
    if certain_operator(before):
        return False
    if regex_can_start(text, at):
        return True
    return undecided == "regex" and regex_end(text, at) > at + 1


def blank_inside(literal: str) -> str:
    """One quoted literal with its CONTENTS spaced out, delimiters and lines kept."""
    if len(literal) < 2:
        return literal
    return literal[0] + re.sub(r"[^\r\n]", " ", literal[1:-1]) + literal[-1]


def strip_comments(
    text: str,
    *,
    language: Literal["typescript", "go"] = "typescript",
    literals: bool = False,
    undecided_slash: Literal["operator", "regex"] = "operator",
) -> str:
    """Blank out comments without touching comment markers inside literals.

    Replaced with spaces rather than deleted, so every offset in the result still
    names the same character in the original — which is what lets a match here be
    used against the source it came from. Go raw backticks have neither escapes
    nor template interpolation, and Go has no regex literal syntax.

    ``literals=True`` blanks the CONTENTS of every string, template, raw and
    regex literal as well, leaving the delimiters where they were. For a reader that finds
    a declaration by matching over the whole file, a literal is prose: a template
    or a raw string is free to carry a line that reads exactly like the
    declaration being looked for — an example in a generated document, a snippet
    of a script — and a match inside one is a sentence, not a declaration. It is
    off by default because the readers that locate literal DATA need the data.

    ``undecided_slash`` says what to do with a TypeScript slash whose role this
    reader cannot decide — the ones `regex_can_start` answers ``False`` for that
    are not obviously divisions either, a slash after a ``)`` above all, since a
    control condition's closing parenthesis is exactly what precedes
    ``if (ok) /re/.test(v)`` and also exactly what precedes ``(a + b) / 2``.
    ``"operator"`` reads it as division and leaves the characters alone, which is
    what this pass has always done; ``"regex"`` reads it as a regex literal and
    skips its body. Neither answer is right in general, and that is the point of
    the parameter: a caller that cannot afford to be wrong can read the file BOTH
    ways and refuse when the two disagree, which is a cheaper guarantee than a
    JavaScript parser and a stronger one than either reading alone. A regex
    literal cannot span a line break, so under ``"regex"`` a real division is
    skipped over at most as far as the end of its own line.
    """
    out: list[str] = []
    # The tail of what has been written, bounded: every lookback here is a few
    # tokens, and joining the whole prefix at each slash would make one pass over
    # a file quadratic in the number of slashes in it.
    tail = ""

    def keep(chunk: str) -> None:
        nonlocal tail
        out.append(chunk)
        tail = (tail + chunk)[-_LOOKBACK:]

    i = 0
    # A hashbang is a comment to every tool that reads one of these files and was
    # not one to this reader, so its text counted as code — including a brace,
    # which is how a closing one nobody wrote hid a top-level declaration
    # (adversarial review, OPL-4805).
    if text.startswith("#!"):
        end = text.find("\n")
        i = len(text) if end == -1 else end
        keep(" " * i)
    while i < len(text):
        ch = text[i]
        if ch in "'\"`":
            if language == "go" and ch == "`":
                closing = text.find("`", i + 1)
                if closing == -1:
                    raise ValueError("unterminated raw string")
                end = closing + 1
            else:
                end = quoted_end(text, i)
            keep(blank_inside(text[i:end]) if literals else text[i:end])
            i = end
        elif ch == "/" and text[i : i + 2] == "//":
            end = text.find("\n", i + 2)
            stop = len(text) if end == -1 else end
            keep(" " * (stop - i))
            i = stop
        elif ch == "/" and text[i : i + 2] == "/*":
            end = text.find("*/", i + 2)
            stop = len(text) if end == -1 else end + 2
            # Newlines kept so line numbers survive; everything else spaced out.
            keep(re.sub(r"[^\r\n]", " ", text[i:stop]))
            i = stop
        # Not `checked_slash_end`, which REFUSES an undecidable slash: this pass
        # runs over ordinary modules rather than over literal tables, and a slash
        # after a `)` — which it cannot decide — is `Date.now() / 1000` in one of
        # them today, so the strict reader would answer legal arithmetic with a
        # refusal and take the whole comparison down with it.
        #
        # `regex_can_start` is not a substitute for that strictness and is not
        # claimed as one. It says "a regex COULD begin here", and a division can
        # stand in some of the same places: after `++`, or after a member access
        # whose property is a reserved word, it answers yes to what is really an
        # operator (adversarial review, OPL-4805). It is a lexical guess, and both
        # of its mistakes can move a literal's boundary. A caller that must not be
        # wrong reads the file under both `undecided_slash` policies and refuses
        # when they disagree; `constant` does exactly that.
        elif (
            language == "typescript"
            and ch == "/"
            and _reads_as_regex(tail, text, i, undecided_slash)
        ):
            end = regex_end(text, i)
            # A regex body is a literal like any other, and one that is left
            # standing is one whose braces and quotes are counted as code. That
            # cost a reader of top-level declarations both answers: a `}` in a
            # regex closed a scope nobody opened, so a module declaration read as
            # nested and a nested one read as top level (adversarial review,
            # OPL-4805).
            keep(blank_inside(text[i:end]) if literals else text[i:end])
            i = end
        else:
            keep(ch)
            i += 1
    return "".join(out)


def balanced(
    text: str, start: int, open_ch: str, close_ch: str, *, strict_slashes: bool = False
) -> str:
    """The text between the bracket at ``start`` and the one that balances it.

    Depth-counted rather than matched lazily to the next closer, because every
    table here nests: a route's ``query`` holds objects and a body's schema holds
    more of them, so the first ``]`` is nowhere near the end of the list.

    Template interpolations require strict slash handling before any delimiter
    is interpreted. Other callers may read languages with ordinary division;
    retaining their existing behavior avoids imposing JavaScript rules there.
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
        if ch == "/" and (strict_slashes or regex_can_start(text, i)):
            i = checked_slash_end(text, i) if strict_slashes else regex_end(text, i)
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start + 1 : i]
        i += 1
    raise ValueError(f"unbalanced {open_ch} from {position(text, start)}")


def module_matches(source: str, pattern: str) -> list[re.Match[str]]:
    """Find declarations only in module code, outside literals and nested scopes.

    The source must already be comment-blanked. This is a lexical boundary
    check, not expression evaluation; unsupported declarations remain absent
    and their callers refuse an inventory they cannot establish.
    """
    expression = re.compile(pattern)
    matches: list[re.Match[str]] = []
    stack: list[str] = []
    i = 0
    while i < len(source):
        ch = source[i]
        if ch in "'\"`":
            i = quoted_end(source, i)
            continue
        if ch == "/":
            i = checked_slash_end(source, i)
            continue
        if not stack and (i == 0 or not _IDENT_CHAR.match(source[i - 1])):
            match = expression.match(source, i)
            if match:
                matches.append(match)
                # Patterns end at the initializer's opening delimiter. Process
                # it below so its contents are nested, while
                # avoiding a second match at an exported declaration's const.
                i = match.end() - 1
                ch = source[i]
        if ch in "{[(":
            stack.append(ch)
        elif ch in "}])" and (not stack or stack.pop() != {"}": "{", "]": "[", ")": "("}[ch]):
            raise ValueError(f"unbalanced {ch} at {position(source, i)}")
        i += 1
    if stack:
        raise ValueError("unbalanced module scope")
    return matches


def initializer_contents(source: str, start: int, opening: str, closing: str) -> str:
    """Read a literal initializer only when its declaration ends there.

    A semicolon or end of input supplies an unambiguous boundary. Other
    continuations, including expressions after a newline, require a reader
    update; a balanced literal alone says nothing about the final value.
    """
    body = balanced(source, start, opening, closing)
    remainder = source[start + len(body) + 2 :].lstrip()
    if remainder and not remainder.startswith(";"):
        raise ValueError("unsupported initializer continuation; expected semicolon or end of input")
    return body


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
                raise ValueError(f"unbalanced {ch} at {position(body, i)}")
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
