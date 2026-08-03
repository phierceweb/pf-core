"""Bounded ``Content-Encoding`` decoding for :mod:`pf_core.fetch`.

A size cap on the wire bytes bounds nothing once the body is compressed — the
decoded output is what consumes memory. Decoding runs against the same budget
as the read, and every malformed-body failure leaves as :class:`ClientError` so
callers have one exception to catch.
"""

from __future__ import annotations

import zlib
from email.message import Message

from pf_core.exceptions import ClientError

_GZIP_WBITS = 16 + zlib.MAX_WBITS
_RAW_DEFLATE_WBITS = -zlib.MAX_WBITS


def _inflate(data: bytes, wbits: int, max_bytes: int | None, url: str) -> bytes:
    """Inflate buffered *data*, emitting at most *max_bytes* (``None`` = unlimited).

    Raises:
        ClientError: Decoded output exceeded *max_bytes*.
        zlib.error: Stream is malformed or ended early.
    """
    out = bytearray()
    stream = data
    while stream:
        obj = zlib.decompressobj(wbits=wbits)
        while stream:
            # 0 is zlib's "unlimited" sentinel; otherwise ask for one byte past
            # the budget so an overrun is detectable without decoding the rest.
            budget = 0 if max_bytes is None else max_bytes - len(out) + 1
            out += obj.decompress(stream, budget)
            if max_bytes is not None and len(out) > max_bytes:
                raise ClientError(
                    "decoded body exceeded max_bytes",
                    context={"url": url, "max_bytes": max_bytes},
                )
            stream = obj.unconsumed_tail
        if not obj.eof:
            raise zlib.error("compressed stream ended before the end-of-stream marker")
        if wbits != _GZIP_WBITS:  # only gzip concatenates members
            break
        # Matches gzip.decompress: NUL padding between members is not garbage.
        stream = obj.unused_data.lstrip(b"\x00")
    return bytes(out)


def decode_body(data: bytes, headers: Message, max_bytes: int | None, url: str) -> bytes:
    """Decode *data* per its ``Content-Encoding``; identity bodies pass through.

    Raises:
        ClientError: Body exceeded *max_bytes* once decoded, or could not be decoded.
    """
    encoding = (headers.get("Content-Encoding") or "").strip().lower()
    if encoding not in ("gzip", "deflate"):
        return data
    try:
        if encoding == "gzip":
            return _inflate(data, _GZIP_WBITS, max_bytes, url)
        try:
            return _inflate(data, zlib.MAX_WBITS, max_bytes, url)
        except zlib.error:
            # Raw deflate — servers that skip the zlib wrapper. Catching only
            # zlib.error matters: an over-cap ClientError must not retry here.
            return _inflate(data, _RAW_DEFLATE_WBITS, max_bytes, url)
    except (OSError, zlib.error, EOFError) as exc:
        raise ClientError(
            "undecodable Content-Encoding",
            context={"url": url, "encoding": encoding, "bytes": len(data)},
            cause=exc,
        ) from exc
