"""Verifying a delivery from an account webhook (platform OPL-3923, OPL-4300).

The platform signs every delivery with `Standard Webhooks
<https://www.standardwebhooks.com>`_ v1, verbatim, so that a receiver can check
it against a published specification and somebody else's library rather than
against this file. This is the reference verifier the Python SDK ships, and it
does the one thing that scheme's three headers ask for:

* ``webhook-id`` — the delivery id, ``whd-`` and sixteen hex characters,
  unchanged across retries. Remember each one you accept for at least the
  replay window, and a retry of a delivery you already handled is recognised
  rather than processed twice.
* ``webhook-timestamp`` — Unix seconds, the time of THIS attempt. A delivery
  more than :data:`REPLAY_WINDOW_S` from your clock is refused before its MAC
  is even computed; retries carry a fresh one, so the eighth attempt sixteen
  hours on verifies as cleanly as the first.
* ``webhook-signature`` — ``v1,`` and base64 of HMAC-SHA256 over
  ``<id>.<timestamp>.<raw body>``, keyed by the secret's bytes after
  ``whsec_``, base64-decoded. Several signatures may share the header,
  separated by a space; ANY verifying ``v1`` entry passes, and an entry of a
  version this client does not know is ignored rather than refused — which is
  what lets a rotated secret keep working for its 24-hour grace, and what lets
  the scheme grow a version without breaking a verifier that predates it.

THE BODY IS THE BYTES ON THE WIRE. The whole design turns on that: the
signature covers the exact request body — not a re-serialised object, not a
trimmed or re-encoded string — and a framework that parses JSON before the
verifier sees it has destroyed the thing that was signed. :func:`verify`
therefore takes ``bytes`` and refuses a ``str``, so that the obvious mistake,
``json.dumps(request.json)``, is a loud ``ValueError`` in development rather
than a verifier that quietly refuses every delivery in production.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time
from collections.abc import Mapping

from . import _api

__all__ = ["REPLAY_WINDOW_S", "SECRET_PREFIX", "verify"]

#: How far, in seconds either side of the receiver's clock, a delivery's
#: ``webhook-timestamp`` may lie and still be accepted. The platform's own
#: number (``REPLAY_WINDOW_S`` in ``web/lib/webhooksign.ts``), which is also
#: what the specification recommends and what Stripe's verifier defaults to,
#: so every library a receiver might reach for already enforces it. Together
#: with a receiver that remembers each accepted ``webhook-id`` for this long,
#: it closes every replay there is: a captured request older than the window
#: is refused on the timestamp before the id is consulted, so the memory a
#: receiver needs is finite by construction.
REPLAY_WINDOW_S = _api.WEBHOOK_REPLAY_WINDOW_S

#: What every signing secret begins with. The bytes after it, base64-decoded,
#: are the HMAC key.
SECRET_PREFIX = "whsec_"

_ID = "webhook-id"
_TIMESTAMP = "webhook-timestamp"
_SIGNATURE = "webhook-signature"

#: The most digits a ``webhook-timestamp`` may carry and still be converted.
#:
#: Unix seconds are ten digits now and stay eleven until the year 5138, so
#: twenty is room the platform will never need — and the bound is not about
#: plausibility, it is about arithmetic. ``int()`` builds an integer of any
#: size from this header, and the header is written by whoever can reach the
#: receiver's endpoint: from 309 digits the value can exceed what a float
#: holds and from 310 it always does, so ``abs(clock - sent)`` below raises
#: ``OverflowError``, and past
#: 4300 digits ``int(value, 10)`` itself raises ``ValueError`` on CPython's
#: integer-string conversion limit. Either one escapes :func:`verify`, whose
#: whole contract is that a malformed header is ``False`` and never an
#: exception, and turns every receiver into a 500 that the platform then
#: retries (adversarial review, OPL-4478). Length is the one property that
#: can be tested before the conversion that is the hazard, so it is tested
#: first.
_MAX_TIMESTAMP_DIGITS = 20


def _key(secret: str) -> bytes:
    """The HMAC key a secret carries, or a ``ValueError`` saying why not.

    A secret of the wrong shape is a CONFIGURATION error — the value pasted
    from ``POST /webhooks`` was truncated, or the wrong variable was read —
    and not a forged delivery, so it is raised rather than folded into a
    ``False`` that would send somebody looking at the sender.
    """
    if not isinstance(secret, str):
        raise ValueError(  # noqa: TRY004 — one exception type for one class of mistake
            f"secret must be a string, not {type(secret).__name__}"
        )
    if not secret.startswith(SECRET_PREFIX):
        raise ValueError(f"secret must begin with {SECRET_PREFIX!r}")
    try:
        key = base64.b64decode(secret[len(SECRET_PREFIX) :], validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"secret is not {SECRET_PREFIX}<base64>: {e}") from None
    if not key:
        raise ValueError("secret carries no key")
    return key


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """One header, found whatever case it was sent in.

    HTTP header names are case-insensitive and the specification spells these
    in lower case, but a framework is free to hand them over as
    ``Webhook-Id`` or ``WEBHOOK_ID``-adjacent, and a verifier that depended on
    the spelling would refuse every delivery on one framework and none on
    another. A ``str`` value is taken as it is; anything else — a list of
    values, a bytes value — is refused, because a signature that has to be
    guessed at is not a signature.
    """
    direct = headers.get(name)
    if direct is None:
        for key, value in headers.items():
            if isinstance(key, str) and key.lower() == name:
                direct = value
                break
    return direct if isinstance(direct, str) else None


def _timestamp(value: str) -> int | None:
    """Unix seconds, decimal, as the platform sends them — or ``None``.

    Strictly ASCII digits, unpadded: ``int()`` would also take ``" 1788264000"``,
    ``"+1"``, ``"1_0"`` and the digits of every other script — as would
    ``str.isdigit`` — none of which the platform writes, and a verifier is the
    wrong place to be generous about what it accepts.

    At most :data:`_MAX_TIMESTAMP_DIGITS` of them, and that bound is checked
    before the conversion rather than after: a longer string is not merely
    implausible, it is the one input for which ``int()`` and the window
    arithmetic that follows raise instead of answering.
    """
    if not (value.isascii() and value.isdigit()) or len(value) > _MAX_TIMESTAMP_DIGITS:
        return None
    return int(value, 10)


def verify(
    secret: str,
    headers: Mapping[str, str],
    raw_body: bytes,
    *,
    now: float | None = None,
    tolerance: float = REPLAY_WINDOW_S,
) -> bool:
    """Whether a delivery was signed by the subscription holding ``secret``.

    ``True`` when the request is a genuine, timely delivery; ``False`` for
    anything else — a missing or malformed header, a timestamp outside
    ``tolerance`` seconds of ``now``, or a signature that does not match. It
    does not say which, and that is deliberate: a verifier that explains its
    refusals is an oracle, and there is nothing a caller should do differently
    for the different kinds of "no".

    ``secret`` is the ``whsec_…`` value ``POST /webhooks`` or a rotate answered
    once. ``headers`` are the request's, in any case. ``raw_body`` is the
    request body exactly as it arrived, as bytes — see the module docstring
    for why nothing else will do.

    Configuration mistakes ARE raised, as :class:`ValueError`: a secret of the
    wrong shape, or a body handed over as text. Neither is something a forged
    request can cause, and folding them into ``False`` would send the caller
    looking at the sender for a bug in the receiver.

    ``now`` is the receiver's clock in Unix seconds, for tests; it defaults to
    :func:`time.time`. ``tolerance`` is the replay window, defaulting to the
    platform's :data:`REPLAY_WINDOW_S`.

    A ``webhook-id`` that is not ASCII is one of the malformed headers this
    answers ``False`` to. The platform writes ``whd-`` and sixteen hex
    characters, so nothing it sends is affected; the rule is stated because it
    is stricter than the hazard it closes, which is only the id that cannot be
    encoded at all.

    What this does not do, and a receiver still must: remember every
    ``webhook-id`` it accepts for at least the window, and refuse a repeat.
    Retries carry the same id, so that is what makes a delivery processed
    once.
    """
    key = _key(secret)
    if isinstance(raw_body, str):
        raise ValueError(  # noqa: TRY004 — see `_key`
            "raw_body must be the request body as bytes — the exact bytes on the wire, "
            "not a re-serialised object"
        )
    body = bytes(raw_body)

    msg_id = _header(headers, _ID)
    stamp = _header(headers, _TIMESTAMP)
    signatures = _header(headers, _SIGNATURE)
    if msg_id is None or stamp is None or signatures is None:
        return False
    # The id is signed as its own bytes, so it has to survive `.encode()`, and
    # a header is not guaranteed to. A server that decodes request headers with
    # `surrogateescape` — aiohttp does — turns a raw 0xFF byte into the lone
    # surrogate '\udcff', which UTF-8 cannot encode: `msg_id.encode()` below
    # would raise `UnicodeEncodeError` straight out of a function whose whole
    # contract is that a malformed header is `False`. The platform writes
    # `whd-` and sixteen hex characters, so nothing it sends is lost by
    # refusing the non-ASCII id here, and a forged one fails the MAC anyway
    # (adversarial review, OPL-4478).
    if not msg_id.isascii():
        return False
    sent = _timestamp(stamp)
    if sent is None:
        return False
    clock = time.time() if now is None else now
    if abs(clock - sent) > tolerance:
        return False

    # The signed content is `<id>.<timestamp>.<body>`, with the timestamp AS
    # SENT: the header's own text, not the integer re-rendered, so what is
    # checked is exactly what the sender signed and nothing normalised.
    signed = msg_id.encode() + b"." + stamp.encode() + b"." + body
    want = hmac.new(key, signed, hashlib.sha256).digest()

    for entry in signatures.split():
        version, _, encoded = entry.partition(",")
        if version != "v1" or not encoded:
            continue
        try:
            got = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            continue
        if len(got) == len(want) and hmac.compare_digest(got, want):
            return True
    return False
