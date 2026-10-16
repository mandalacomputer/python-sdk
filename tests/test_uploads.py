"""Resumable transfer contracts, including partial writes and ambiguous replies."""

import inspect
import io

import httpx
import pytest
import respx
from tests.test_workspace_move import BASE, computer

from mandala_computer import UploadInterruptedError

PATH = "/tmp/é & data"
UPLOAD = "a" * 32
ROUTE = f"{BASE}/computers/vm-1/files/uploads"


def receipt(offset, size):
    return {
        "path": PATH,
        "upload_id": UPLOAD,
        "offset": offset,
        "size": size,
        "complete": offset == size,
    }


async def result(call):
    return await call if inspect.isawaitable(call) else call


@pytest.mark.parametrize("asynchronous", [False, True])
@respx.mock
async def test_resume_partial_chunk_and_cleanup(asynchronous):
    size = 10 * 1024 * 1024
    offset = 123
    respx.get(ROUTE).mock(httpx.Response(200, json=receipt(offset, size)))

    def put(request):
        nonlocal offset
        assert int(request.url.params["offset"]) == offset
        assert len(request.content) <= 4 * 1024 * 1024
        assert request.url.params["overwrite"] == "false"
        offset += len(request.content)
        return httpx.Response(200, json=receipt(offset, size))

    chunks = respx.put(ROUTE).mock(side_effect=put)
    cleanup = respx.delete(ROUTE).mock(httpx.Response(200, json={"ok": True}))
    async with computer(asynchronous) as c:
        done = await result(
            c.upload_file(
                PATH, io.BytesIO(b"x" * size), size=size, upload_id=UPLOAD, overwrite=False
            )
        )
        assert done.complete and done.offset == size
        await result(c.cancel_file_upload(PATH, UPLOAD, no_wake=True))
    assert chunks.call_count == 3
    assert cleanup.calls.last.request.url.params["no_wake"] == "1"


@pytest.mark.parametrize("asynchronous", [False, True])
@respx.mock
async def test_lost_completion_is_not_republished(asynchronous):
    respx.get(ROUTE).mock(httpx.Response(200, json=receipt(3, 3)))
    async with computer(asynchronous) as c:
        assert (await result(c.upload_file(PATH, io.BytesIO(), size=3, upload_id=UPLOAD))).complete


@pytest.mark.parametrize("asynchronous", [False, True])
@respx.mock
async def test_short_source_retains_id_and_no_write(asynchronous):
    respx.get(ROUTE).mock(httpx.Response(200, json=receipt(1, 3)))
    async with computer(asynchronous) as c:
        with pytest.raises(UploadInterruptedError) as error:
            await result(c.upload_file(PATH, io.BytesIO(b"x"), size=3, upload_id=UPLOAD))
        assert error.value.upload_id == UPLOAD
        with pytest.raises(ValueError):
            await result(c.write_file_chunk(PATH, b"abc", upload_id=UPLOAD, offset=2, size=3))


@pytest.mark.parametrize("asynchronous", [False, True])
@respx.mock
async def test_ambiguous_response_never_claims_completion(asynchronous):
    respx.put(ROUTE).mock(httpx.Response(200, json={"offset": 3}))
    async with computer(asynchronous) as c:
        with pytest.raises(ValueError, match="invalid upload progress"):
            await result(c.write_file_chunk(PATH, b"abc", upload_id=UPLOAD, offset=0, size=3))
