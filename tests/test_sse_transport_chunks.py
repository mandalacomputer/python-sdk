"""Public agent calls must not depend on compressed HTTP chunk boundaries."""

from __future__ import annotations

import gzip
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest

import mandala_computer as mc
from mandala_computer._sse import MAX_SSE_BUFFER

DONE = b'event: done\ndata: {"stop":"end_turn","text":"finished"}\n\n'


class CompressedBody(httpx.SyncByteStream, httpx.AsyncByteStream):
    def __init__(self, content: bytes, chunk_size: int) -> None:
        self.content = gzip.compress(content)
        self.chunk_size = chunk_size

    def __iter__(self) -> Iterator[bytes]:
        for offset in range(0, len(self.content), self.chunk_size):
            yield self.content[offset : offset + self.chunk_size]

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self:
            yield chunk


def transport(body: bytes, chunk_size: int) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"id": "vm-test", "status": "running"})
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream", "content-encoding": "gzip"},
            stream=CompressedBody(body, chunk_size),
        )

    return httpx.MockTransport(handle)


def valid_body(frame_payload_size: int, count: int) -> bytes:
    return (b'event: text\ndata: "' + b"x" * frame_payload_size + b'"\n\n') * count + DONE


@pytest.mark.parametrize("chunk_size", [8, 1 << 20])
def test_public_agent_accepts_gzip_chunks_with_many_frames(
    monkeypatch: pytest.MonkeyPatch, chunk_size: int
) -> None:
    monkeypatch.setattr("mandala_computer._sse.MAX_SSE_BUFFER", 128)
    with (
        httpx.Client(transport=transport(valid_body(64, 20), chunk_size)) as http,
        mc.Client("fake", http_client=http) as client,
    ):
        result = client.computers.get("vm-test").agent("do it", model_key="fake")
    assert result.finished and result.text == "finished"


@pytest.mark.parametrize("chunk_size", [8, 1 << 20])
async def test_public_async_agent_accepts_gzip_chunks_with_many_frames(
    monkeypatch: pytest.MonkeyPatch, chunk_size: int
) -> None:
    monkeypatch.setattr("mandala_computer._sse.MAX_SSE_BUFFER", 128)
    async with (
        httpx.AsyncClient(transport=transport(valid_body(64, 20), chunk_size)) as http,
        mc.AsyncClient("fake", http_client=http) as client,
    ):
        computer = await client.computers.get("vm-test")
        result = await computer.agent("do it", model_key="fake")
    assert result.finished and result.text == "finished"


def test_public_agent_accepts_a_decompressed_chunk_above_the_real_limit() -> None:
    body = valid_body(128 * 1024, 129)
    assert len(body) > MAX_SSE_BUFFER
    with (
        httpx.Client(transport=transport(body, 1 << 20)) as http,
        mc.Client("fake", http_client=http) as client,
    ):
        result = client.computers.get("vm-test").agent("do it", model_key="fake")
    assert result.finished and result.text == "finished"


@pytest.mark.parametrize("prefix", [b"", b'event: text\ndata: "ok"\n\n'])
@pytest.mark.parametrize("suffix", [b"", b"\n\n"])
@pytest.mark.parametrize("chunk_size", [8, 1 << 20])
def test_public_agent_refuses_oversized_complete_or_incomplete_frames(
    monkeypatch: pytest.MonkeyPatch, prefix: bytes, suffix: bytes, chunk_size: int
) -> None:
    monkeypatch.setattr("mandala_computer._sse.MAX_SSE_BUFFER", 128)
    body = prefix + b'event: text\ndata: "' + b"x" * 129 + b'"' + suffix
    with (
        httpx.Client(transport=transport(body, chunk_size)) as http,
        mc.Client("fake", http_client=http) as client,
    ):
        computer = client.computers.get("vm-test")
        with pytest.raises(mc.MandalaError, match="frame limit"):
            computer.agent("do it", model_key="fake")


@pytest.mark.parametrize("prefix", [b"", b'event: text\ndata: "ok"\n\n'])
@pytest.mark.parametrize("suffix", [b"", b"\n\n"])
@pytest.mark.parametrize("chunk_size", [8, 1 << 20])
async def test_public_async_agent_refuses_oversized_complete_or_incomplete_frames(
    monkeypatch: pytest.MonkeyPatch, prefix: bytes, suffix: bytes, chunk_size: int
) -> None:
    monkeypatch.setattr("mandala_computer._sse.MAX_SSE_BUFFER", 128)
    body = prefix + b'event: text\ndata: "' + b"x" * 129 + b'"' + suffix
    async with (
        httpx.AsyncClient(transport=transport(body, chunk_size)) as http,
        mc.AsyncClient("fake", http_client=http) as client,
    ):
        computer = await client.computers.get("vm-test")
        with pytest.raises(mc.MandalaError, match="frame limit"):
            await computer.agent("do it", model_key="fake")
