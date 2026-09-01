"""Server-sent events, framed once for both transports.

The agent loop is the only route on this surface that answers with a stream
rather than a result, and the framing it needs is fiddly enough that having two
copies of it — one in the sync transport and one in the async — would mean two
places to get the CRLF handling wrong. So the byte-level work lives here as a
small state machine that neither transport has to understand: feed it whatever
arrived, take whatever events that completed.

The parsing is deliberately narrow. This reads ``event:`` and ``data:`` and
nothing else — no ``id:``, no ``retry:``, no reconnection — because the platform
sends neither and a client that reconnected mid-run would restart an agent loop
that is still going, on the caller's own model key.
"""

from __future__ import annotations

import codecs
import json
from dataclasses import dataclass
from typing import Any

__all__ = ["SSEDecoder", "SSEEvent"]


@dataclass(frozen=True)
class SSEEvent:
    """One frame off the wire."""

    #: The ``event:`` name, or ``"message"`` where the frame did not give one,
    #: which is what the SSE spec says an unnamed event is called.
    event: str
    #: The ``data:`` payload, parsed as JSON when it is JSON and left as the raw
    #: string when it is not. Not an error either way: what a frame carries is
    #: the platform's business, and a client that raised on an unparseable one
    #: would fail a run over a frame it did not need.
    data: Any


def parse_event(chunk: str) -> SSEEvent | None:
    """One frame's lines, as an event — or ``None`` if it carried no data.

    A frame with no ``data:`` line is not an event. Comment-only frames (the
    ``:`` keep-alives a proxy uses to hold an idle connection open) are exactly
    that shape, and yielding them would put something in a caller's loop that
    says nothing about their run.
    """
    event = "message"
    data: list[str] = []
    for line in chunk.split("\n"):
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            # One leading space after the colon is separator, not payload.
            data.append(line[5:].removeprefix(" "))
    if not data:
        return None
    joined = "\n".join(data)
    try:
        return SSEEvent(event, json.loads(joined))
    except ValueError:
        return SSEEvent(event, joined)


class SSEDecoder:
    """Bytes in, events out, across chunk boundaries that fall anywhere.

    Two things make this more than a ``split``:

    A **multibyte character can straddle a chunk**, so the bytes are run through
    an incremental UTF-8 decoder rather than decoded per chunk. Decoding each
    chunk on its own would put a replacement character in the middle of a step's
    text every time a network read landed mid-character.

    A **CRLF can straddle one too**. The spec allows CRLF and a lone CR as line
    terminators, and a proxy that reframes the stream is entitled to use either,
    so terminators are normalised to LF before frames are found — splitting on
    ``"\\n\\n"`` alone would never find a boundary in a CR-framed stream and
    would collapse a whole run into one unparseable event. The awkward case is a
    ``\\r`` at the end of a chunk: it contributes exactly one line break whether
    or not a ``\\n`` follows it in the next chunk, so it is normalised and framed
    immediately, and the ``\\n`` that may follow is swallowed. Waiting to see
    would deliver every event one chunk late; not swallowing would fabricate a
    frame boundary, splitting one event in two and losing its type.
    """

    def __init__(self) -> None:
        # "replace" rather than strict, and only for the very end: an
        # incremental decoder holds an incomplete trailing sequence back on
        # every ordinary chunk either way, so this changes nothing until
        # `flush` — where strict RAISES on a stream cut mid-character. That
        # would turn a run whose result had already arrived into an exception
        # thrown by the tidying-up afterwards.
        #
        # "utf-8-sig", not "utf-8", so a leading BOM is consumed rather than
        # left on the front of the first line. The platform does not send one,
        # but an intermediary that re-encodes the stream may, and a BOM glued to
        # `event:` matches neither field name — so the whole first frame, which
        # is the one carrying the first step, is silently dropped.
        self._decoder = codecs.getincrementaldecoder("utf-8-sig")("replace")
        self._buffer = ""
        self._swallow_lf = False

    def feed(self, chunk: bytes) -> list[SSEEvent]:
        """Whatever events this chunk completed, in order. Often none."""
        text = self._decoder.decode(chunk)
        # A partial multibyte character decodes to "" and must not clear the
        # pending swallow — the "\n" it guards against is still to come.
        if not text:
            return []
        if self._swallow_lf:
            text = text.removeprefix("\n")
            self._swallow_lf = False
        if text.endswith("\r"):
            self._swallow_lf = True
        self._buffer += text.replace("\r\n", "\n").replace("\r", "\n")
        events = []
        while True:
            sep = self._buffer.find("\n\n")
            if sep == -1:
                break
            frame, self._buffer = self._buffer[:sep], self._buffer[sep + 2 :]
            event = parse_event(frame)
            if event is not None:
                events.append(event)
        return events

    def flush(self) -> SSEEvent | None:
        """The last frame, if the stream ended without a blank line after it.

        The decoder is flushed rather than dropped. A stream cut mid-character
        leaves the front half of one inside it, and every incremental decode
        above holds those bytes back waiting for the rest — so without this the
        final event silently loses them and looks complete. Flushed, they decode
        to U+FFFD, which is a visible mark that something was cut off.

        Takes the tail rather than reading it. Both transports call this once,
        but a decoder that answered a second call with the same event would hand
        a caller one step of their run twice — a decoder is a thing you feed
        until it is empty, and this is what empties it.
        """
        self._buffer += self._decoder.decode(b"", final=True)
        tail, self._buffer = self._buffer, ""
        # The CR-swallow goes with the buffer. This method's own claim is that a
        # decoder is a thing you feed until it is empty and that this is what
        # empties it, and a flag left armed here is state the object still holds
        # — a decoder fed again would eat a leading LF belonging to the next
        # stream. Nothing in this SDK reuses one, which is why it was never a
        # live loss; leaving it set made the sentence above false
        # (adversarial review, OPL-4232).
        self._swallow_lf = False
        return parse_event(tail)
