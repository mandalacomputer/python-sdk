# gorillacloud-python

Python SDK for [GorillaCloud](https://gorillacloud.ai) — cloud desktops for AI agents.

> **Status: pre-release scaffolding.** There is no client code here yet. The
> package is deliberately empty until the platform exposes a curated, versioned
> `/api/v1` surface for it to bind to (see [Design notes](#design-notes)).

## Intended usage

```python
from gorillacloud import Computer

with Computer() as c:          # provisions, waits for the desktop, cleans up
    c.launch("firefox")
    shot = c.screenshot()
    c.click(240, 180)
    c.type("hello")
```

## Install

```sh
pip install gorillacloud       # not yet published
```

Authentication uses an API key from the dashboard (Settings → API keys), read
from `GORILLACLOUD_API_KEY` by default.

## Design notes

**This SDK binds only to the curated `/api/v1` surface, never to the hypervisor
daemon's own routes.** That boundary is deliberate and load-bearing:

- The daemon's VM representation carries fields that are nobody's business
  outside the platform — owner ids, VNC tokens and ports, MAC addresses, host
  image paths. The public surface exposes a deliberately narrower object.
- The daemon's auth model treats an absent owner header as *administrative*
  scope. Nothing client-facing should be one bug away from that.
- Multi-host is on the platform roadmap. The daemon's routes will change shape
  when the control plane splits from the hypervisor agents; the point of the v1
  surface is that this SDK does not have to care.

Practically: if something the SDK needs isn't in `/api/v1`, the fix is to add it
there, not to reach past it.

## Development

```sh
pip install -e ".[dev]"
pytest
ruff check .
mypy
```

## License

MIT — see [LICENSE](LICENSE).
