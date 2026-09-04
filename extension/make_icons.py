import struct
import zlib

def make_png(w, h, r, g, b):
    """Create a solid-color PNG file."""
    raw = b""
    for _ in range(h):
        raw += b"\x00" + bytes([r, g, b, 255] * w)

    # IHDR
    ihdr_data = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    ihdr_crc = struct.pack(">I", zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF)
    ihdr = struct.pack(">I", 13) + b"IHDR" + ihdr_data + ihdr_crc

    # IDAT
    compressed = zlib.compress(raw)
    idat_crc = struct.pack(">I", zlib.crc32(b"IDAT" + compressed) & 0xFFFFFFFF)
    idat = struct.pack(">I", len(compressed)) + b"IDAT" + compressed + idat_crc

    # IEND
    iend_crc = struct.pack(">I", zlib.crc32(b"IEND") & 0xFFFFFFFF)
    iend = struct.pack(">I", 0) + b"IEND" + iend_crc

    return b"\x89PNG\r\n\x1a\n" + ihdr + idat + iend

# Purple (#6c5ce7)
with open(r"e:\ContextBridge\extension\icon48.png", "wb") as f:
    f.write(make_png(48, 48, 108, 92, 231))

with open(r"e:\ContextBridge\extension\icon128.png", "wb") as f:
    f.write(make_png(128, 128, 108, 92, 231))

print("Icons created successfully")
