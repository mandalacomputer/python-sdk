"""Execute the README with the released OpenAI client and an HTTPX2 mock transport."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import openai._base_client
import pytest

README = Path(__file__).resolve().parents[1] / "README.md"
START = "<!-- byok-openai-example:start -->"
END = "<!-- byok-openai-example:end -->"
ENV = {
    "MANDALA_API_KEY": "mandala-synthetic-bearer-A",
    "MANDALA_COMPUTER_ID": "computer-synthetic-running",
    "ANTHROPIC_API_KEY": "anthropic-synthetic-model-key-B",
    "ANTHROPIC_MODEL": "claude-synthetic-computer-use-model",
}


def extract_example(markdown: str) -> str:
    """Require exactly one marked, nonempty Python block; never silently skip it."""
    if markdown.count(START) != 1 or markdown.count(END) != 1:
        raise ValueError("Expected exactly one pair of OpenAI example markers")
    start = markdown.index(START) + len(START)
    end = markdown.index(END)
    match = re.fullmatch(r"\s*```python\n(.*?)\n```\s*", markdown[start:end], re.DOTALL)
    if match is None or not match[1].strip() or "```" in match[1]:
        raise ValueError("Expected one nonempty Python example inside the markers")
    return match[1]


@pytest.fixture
def example() -> str:
    return extract_example(README.read_text(encoding="utf-8"))


@dataclass
class Capture:
    outcome: str = "success"
    requests: list[httpx2.Request] = field(default_factory=list)
    responses: list[httpx2.Response] = field(default_factory=list)
    clients: list[httpx2.Client] = field(default_factory=list)
    retry_delays: list[float] = field(default_factory=list)

    def respond(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.outcome == "interrupted":
            raise httpx2.ReadError("Synthetic interrupted connection", request=request)
        if self.outcome == "provider_refusal":
            response = httpx2.Response(
                429,
                json={"error": {"message": "Synthetic model rate limit", "code": 429}},
            )
        elif self.outcome == "computer_busy":
            response = httpx2.Response(
                409,
                json={"error": {"message": "Synthetic computer already in use", "code": 409}},
            )
        elif self.outcome == "platform_refusal":
            response = httpx2.Response(429, json={"error": "Synthetic platform rate limit"})
        else:
            assert self.outcome == "success"
            response = httpx2.Response(
                200,
                json={
                    "id": "chatcmpl-synthetic",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "synthetic-agent-endpoint",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Synthetic page title"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
                    "agent": {
                        "computer_id": ENV["MANDALA_COMPUTER_ID"],
                        "steps": [],
                        "stop": "end_turn",
                    },
                },
            )
        self.responses.append(response)
        return response

    def execute(self, code: str) -> dict[str, Any]:
        namespace: dict[str, Any] = {"__name__": "__readme_example__"}
        # Execute the checked-in documentation itself, not a copied request.
        exec(compile(code, str(README), "exec"), namespace)  # noqa: S102
        assert namespace["OpenAI"] is openai.OpenAI
        assert isinstance(namespace["client"], openai.OpenAI)
        assert namespace["client"].is_closed()
        return namespace


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch) -> Iterator[Capture]:
    captured = Capture()
    initialize_client = httpx2.Client.__init__

    def initialize_mock_client(client: httpx2.Client, **kwargs: Any) -> None:
        # Inject the supported transport argument, retaining the real client,
        # request serialization and retry behavior. Proxy mounts cannot escape it.
        kwargs.update(
            transport=httpx2.MockTransport(captured.respond),
            trust_env=False,
            proxy=None,
            mounts={},
        )
        initialize_client(client, **kwargs)
        captured.clients.append(client)

    def refuse_network(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("The README test must never use a network transport")

    with monkeypatch.context() as patch:
        for name in tuple(os.environ):
            if name.startswith(("OPENAI_", "MANDALA_", "ANTHROPIC_")):
                patch.delenv(name)
        for name, value in ENV.items():
            patch.setenv(name, value)
        patch.setattr(httpx2.Client, "__init__", initialize_mock_client)
        patch.setattr(httpx2.HTTPTransport, "handle_request", refuse_network)
        # Skip only the retry wait, without changing the released client's
        # retry decision or count, and without patching process-wide time.sleep.
        patch.setattr(
            openai._base_client,
            "time",
            SimpleNamespace(time=time.time, sleep=captured.retry_delays.append),
        )
        try:
            yield captured
        finally:
            clients_closed = all(client.is_closed for client in captured.clients)
            responses_closed = all(response.is_closed for response in captured.responses)
            for response in captured.responses:
                response.close()
            for client in captured.clients:
                client.close()
            assert clients_closed, "The README must close its OpenAI client, including on errors"
            assert responses_closed, "The client must close every mocked response"


def assert_wire_contract(requests: list[httpx2.Request]) -> None:
    assert len(requests) == 1, "The README must send exactly one desktop task request"
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://app.mandala.computer/api/v1/chat/completions"
    assert request.url.query == b""
    assert request.headers["Authorization"] == f"Bearer {ENV['MANDALA_API_KEY']}"
    assert request.headers["X-Model-Key"] == ENV["ANTHROPIC_API_KEY"]
    body = json.loads(request.content)
    assert body == {
        "model": ENV["ANTHROPIC_MODEL"],
        "messages": [
            {"role": "user", "content": "Read the page title in the browser and report it."}
        ],
        "computer_id": ENV["MANDALA_COMPUTER_ID"],
        "max_steps": 5,
        "stream": False,
    }
    assert body["stream"] is False
    for credential in (ENV["MANDALA_API_KEY"], ENV["ANTHROPIC_API_KEY"]):
        assert credential.encode() not in request.content
        assert credential not in str(request.url)


def test_readme_request_uses_real_openai_client(
    example: str, capture: Capture, capsys: pytest.CaptureFixture[str]
) -> None:
    namespace = capture.execute(example)
    assert_wire_contract(capture.requests)
    assert len(capture.clients) == 1
    assert capture.retry_delays == []
    assert isinstance(namespace["completion"], openai.types.chat.ChatCompletion)
    assert capsys.readouterr().out == "Synthetic page title\n"


FAILURES = [
    ("provider_refusal", openai.RateLimitError),
    ("computer_busy", openai.ConflictError),
    ("platform_refusal", openai.RateLimitError),
    ("interrupted", openai.APIConnectionError),
]


@pytest.mark.parametrize(("outcome", "error"), FAILURES)
def test_readme_does_not_replay_a_failed_task(
    example: str, capture: Capture, outcome: str, error: type[Exception]
) -> None:
    capture.outcome = outcome
    with pytest.raises(error):
        capture.execute(example)
    assert_wire_contract(capture.requests)
    assert len(capture.clients) == 1
    assert capture.retry_delays == []


@pytest.mark.parametrize(("outcome", "error"), FAILURES)
def test_default_retries_fail_the_one_request_check(
    example: str, capture: Capture, outcome: str, error: type[Exception]
) -> None:
    setting = "    max_retries=0,\n"
    assert example.count(setting) == 1
    capture.outcome = outcome
    with pytest.raises(error):
        capture.execute(example.replace(setting, ""))
    assert len(capture.requests) == 3
    assert len(capture.retry_delays) == 2
    assert all(0 < delay <= 2 for delay in capture.retry_delays)
    with pytest.raises(AssertionError, match="exactly one"):
        assert_wire_contract(capture.requests)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("https://app.mandala.computer/api/v1", "https://app.mandala.computer/api/wrong"),
        ('"computer_id":', '"computer":'),
        ('"MANDALA_API_KEY"', '"ANTHROPIC_API_KEY"'),
        ("        stream=False,\n", ""),
    ],
)
def test_request_checks_reject_wrong_example(
    example: str, capture: Capture, old: str, new: str
) -> None:
    assert example.count(old) == 1
    capture.execute(example.replace(old, new))
    with pytest.raises(AssertionError):
        assert_wire_contract(capture.requests)


@pytest.mark.parametrize(
    "markdown",
    [
        "",
        f"{START}\n```python\nprint('missing end')\n```",
        f"```python\nprint('missing start')\n```\n{END}",
        f"{END}\n```python\nprint('reversed markers')\n```\n{START}",
        f"{START}\n{START}\n```python\nprint('duplicate')\n```\n{END}",
        f"{START}\n```python\nprint('duplicate')\n```\n{END}\n{END}",
        f"{START}\nNo code block.\n{END}",
        f"{START}\n```python\n\n```\n{END}",
        f"{START}\n```python\n   \n```\n{END}",
        f"{START}\n```python\nprint('unclosed fence')\n{END}",
        f"{START}\n```sh\npython example.py\n```\n{END}",
        f"{START}\n```python\nprint('one')\n```\n```python\nprint('two')\n```\n{END}",
    ],
)
def test_malformed_or_empty_example_fails_extraction(markdown: str) -> None:
    with pytest.raises(ValueError):
        extract_example(markdown)
