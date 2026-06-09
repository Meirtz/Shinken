"""Offline coverage for DockerLocalProvider's hand-rolled PNG readiness decoder.

This decode path (_png_has_non_black_pixel / _unfilter / _paeth) decides whether a
freshly-created container is "ready" (non-black screen). It is pure and on the create
main path, but was previously exercised only through a live Docker container — so a bug
in the unfilter/Paeth math would make ready containers time out or all-black screens pass
as ready. These tests pin it with synthetic PNGs and known filter vectors.
"""

from __future__ import annotations

import struct
import zlib

from shinken.providers.docker import _paeth, _png_has_non_black_pixel, _unfilter


def _png(width: int, height: int, rows: list[list[int]], color_type: int = 2) -> bytes:
    """Build a minimal valid 8-bit PNG. `rows` is one filtered-type-0 scanline of channel
    bytes per row (caller provides raw pixel bytes; filter byte 0 is prepended here)."""
    channels = {0: 1, 2: 3, 6: 4}[color_type]
    raw = b"".join(bytes([0]) + bytes(r) for r in rows)
    assert all(len(r) == width * channels for r in rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def test_paeth_predictor_known_vectors():
    assert _paeth(0, 0, 0) == 0
    assert _paeth(10, 20, 10) == 20  # p=20, closest to `up`
    assert _paeth(20, 10, 10) == 20  # closest to `left`
    assert _paeth(2, 2, 10) == 2  # upper_left dominates → pick left/up tie → left


def test_unfilter_sub_and_up_and_average():
    # filter 1 (Sub): each byte += left
    row = bytearray([10, 0, 0, 5, 0, 0])
    _unfilter(row, bytearray(6), bpp=3, filter_type=1)
    assert list(row) == [10, 0, 0, 15, 0, 0]
    # filter 2 (Up): each byte += the byte above
    row = bytearray([1, 2, 3])
    _unfilter(row, bytearray([4, 5, 6]), bpp=3, filter_type=2)
    assert list(row) == [5, 7, 9]


def test_all_black_png_is_not_ready():
    png = _png(2, 1, [[0, 0, 0, 0, 0, 0]])
    assert _png_has_non_black_pixel(png) is False


def test_one_non_black_pixel_is_ready():
    png = _png(2, 1, [[0, 0, 0, 1, 0, 0]])
    assert _png_has_non_black_pixel(png) is True


def test_non_png_bytes_degrade_to_none():
    assert _png_has_non_black_pixel(b"not a png") is None


def test_grayscale_and_rgba_color_types():
    assert _png_has_non_black_pixel(_png(2, 1, [[0, 0]], color_type=0)) is False
    assert _png_has_non_black_pixel(_png(1, 1, [[0, 0, 0, 255]], color_type=6)) is False
    assert _png_has_non_black_pixel(_png(1, 1, [[9, 0, 0, 255]], color_type=6)) is True
