"""The event framing, fed the way a network feeds it.

Every test here splits its input somewhere awkward on purpose. A decoder that
only ever sees whole frames is not being tested — chunk boundaries fall wherever
the network puts them, and every bug this file exists to catch is one that only
appears when a boundary lands mid-frame, mid-terminator or mid-character.
"""

from __future__ import annotations

from mandala_computer._sse import SSEDecoder


def drain(*chunks: bytes) -> list[tuple[str, object]]:
    """Feed the chunks in order and collect everything, tail included."""
    decoder = SSEDecoder()
    out = [(e.event, e.data) for chunk in chunks for e in decoder.feed(chunk)]
    tail = decoder.flush()
    if tail is not None:
        out.append((tail.event, tail.data))
    return out


def test_one_whole_frame() -> None:
    assert drain(b'event: step\ndata: {"n": 1}\n\n') == [("step", {"n": 1})]


def test_a_frame_split_across_chunks_arrives_once_and_whole() -> None:
    assert drain(b"event: st", b'ep\ndata: {"n', b'": 1}\n\n') == [("step", {"n": 1})]


def test_several_frames_in_one_chunk_come_out_in_order() -> None:
    events = drain(b"event: a\ndata: 1\n\nevent: b\ndata: 2\n\nevent: c\ndata: 3\n\n")
    assert [name for name, _ in events] == ["a", "b", "c"]


def test_an_unnamed_frame_is_a_message() -> None:
    """What the spec calls a frame with no `event:` line."""
    assert drain(b"data: hello\n\n") == [("message", "hello")]


def test_data_that_is_not_json_stays_a_string() -> None:
    """Not an error. A frame this SDK does not need must not fail the run."""
    assert drain(b"event: note\ndata: not json at all\n\n") == [("note", "not json at all")]


def test_multiple_data_lines_are_joined_with_newlines() -> None:
    assert drain(b"data: one\ndata: two\n\n") == [("message", "one\ntwo")]


def test_only_one_leading_space_is_separator() -> None:
    """`data:  x` carries a space. Stripping both would corrupt indented text."""
    assert drain(b"data:  indented\n\n") == [("message", " indented")]


def test_a_keepalive_comment_yields_nothing() -> None:
    """A proxy holding an idle connection open must not show up in a caller's loop."""
    assert drain(b": keep-alive\n\n", b"data: real\n\n") == [("message", "real")]


def test_crlf_framing_is_understood() -> None:
    """A proxy is entitled to reframe the stream, and some do."""
    assert drain(b'event: step\r\ndata: {"n": 1}\r\n\r\n') == [("step", {"n": 1})]


def test_lone_cr_framing_is_understood() -> None:
    assert drain(b'event: step\rdata: {"n": 1}\r\r') == [("step", {"n": 1})]


def test_a_crlf_split_across_chunks_is_one_terminator_not_two() -> None:
    """The case the swallow exists for.

    The `\\r` ending the first chunk and the `\\n` opening the second are one
    line break. Counted twice they fabricate a frame boundary in the middle of
    an event, which splits it in two and loses its `event:` line with the half
    that gets discarded — so the run's result arrives as an unnamed fragment.
    """
    assert drain(b"event: step\r", b'\ndata: {"n": 1}\r\n\r\n') == [("step", {"n": 1})]


def test_a_frame_boundary_split_across_chunks_still_ends_the_frame() -> None:
    """The other half of it: a real boundary must not be delayed or lost."""
    assert drain(b"data: one\r", b"\n\r\ndata: two\n\n") == [
        ("message", "one"),
        ("message", "two"),
    ]


def test_a_trailing_cr_is_framed_now_rather_than_a_chunk_late() -> None:
    """A CR-framed stream that pauses must not hold its last event back.

    Waiting to see whether an LF follows would deliver every event one chunk
    late, which on a stream that goes quiet between steps is a step reported
    only when the next one happens.
    """
    decoder = SSEDecoder()
    assert [e.data for e in decoder.feed(b"data: one\r\r")] == ["one"]


def test_a_multibyte_character_split_across_chunks_survives() -> None:
    """Decoded incrementally, not per chunk.

    Per chunk, the halves of this é each decode to U+FFFD and the model's text
    comes back corrupted at whatever offset the network happened to break at.
    """
    text = "café ☕".encode()
    assert drain(b"data: " + text[:5], text[5:] + b"\n\n") == [("message", "café ☕")]


def test_a_chunk_that_decodes_to_nothing_does_not_clear_a_pending_swallow() -> None:
    """Both awkward boundaries at once, which is where they interact.

    An empty read — or the front half of a multibyte character, which decodes to
    the same nothing — must not be taken as "no `\\n` came". The `\\n` may be on
    its way in the chunk after that, and a flag cleared early counts it as a
    second terminator: the two `data:` lines of one frame become two frames, and
    whichever of them carried the `event:` line takes the name for itself.
    """
    assert drain(b"data: one\r", b"", b"\ndata: two\n\n") == [("message", "one\ntwo")]


def test_a_frame_with_no_blank_line_after_it_is_still_delivered() -> None:
    """A stream that ends cleanly on its last frame, without the trailing break."""
    assert drain(b'event: done\ndata: {"stop": "end_turn"}\n') == [("done", {"stop": "end_turn"})]


def test_a_stream_cut_mid_character_leaves_a_visible_mark() -> None:
    """Flushed rather than dropped, so truncation shows instead of passing.

    The held-back bytes decode to U+FFFD. Dropping them would make a cut-off
    final event look complete, which is the one way this could lie.
    """
    (event,) = drain(b"data: caf" + "é".encode()[:1])
    assert event[1] == "caf�"


def test_a_stream_of_nothing_yields_nothing() -> None:
    assert drain(b"") == []
    assert drain() == []
