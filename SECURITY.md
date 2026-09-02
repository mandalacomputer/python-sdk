# Security

This repository holds the Mandala Computer Python SDK (`mandala-computer` on PyPI). It is a client: it
talks to the public API at `https://app.mandala.computer` with an API key the
caller supplies, and it holds no infrastructure of its own.

## Reporting a vulnerability

Please do not open a public issue for anything security-sensitive. Use GitHub's
private reporting instead: **Security → Report a vulnerability** on this
repository. That reaches the maintainers directly and stays private until a fix
is out.

Reports about the platform itself — the API, the console at
app.mandala.computer, or a computer's isolation from other tenants — are welcome
through the same channel; we will route them.

Include what you can of: the version affected, steps to reproduce, and what an
attacker could do with it. You will hear back within five working days.

## Supported versions

Pre-1.0, only the latest published release receives fixes. Upgrade before
reporting if you are on an older one.

## What is and is not in scope here

In scope: anything in this repository that could leak an API key, sign or
verify a webhook incorrectly, send a request somewhere other than the
configured base URL, or execute something it was not asked to.

Out of scope: rate limits, the behaviour of the guest operating system inside a
computer you created, and findings that require a compromised machine running
the client.
