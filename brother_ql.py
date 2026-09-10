r"""
brother_ql.py  --  pure-Python (stdlib + Pillow only) raster driver for the
Brother QL-810W label printer over WiFi/Ethernet (raw TCP port 9100).

No third-party packages besides Pillow: no brother_ql, no packbits, no numpy,
no attrs.  Python 3.8+.  Works on Windows (no OS specific tricks).

Public API
----------
    encode_label(img, label="62", printer="QL-810W", cut=True, compress=True,
                 threshold=70, rotate="auto", zero_lines=False) -> bytes
    prepare_image(img, label="62", printer="QL-810W", threshold=70,
                  rotate="auto") -> PIL.Image (mode "1", 720 px wide, 0 = black)
    decode_job(job) -> dict            (parse a job back; used by the self-test)
    send_to_printer(ip, job_bytes, port=9100, timeout=15) -> dict
    print_image(ip, img, **encode_kwargs) -> dict     (encode + send)
    printer_status(ip, port=9100, timeout=5) -> dict
    find_printers(prefix="192.168.0.", ports=(9100,), timeout=0.4) -> list[str]
    packbits_encode(data) / packbits_decode(data)

Byte layout emitted by encode_label() for QL-810W, label "62" (62 mm continuous
tape DK-22205), mono, N raster lines  -- this mirrors what the well known
`brother_ql` library's convert() produces for model QL-810W / label "62":

    1B 69 61 01                  ESC i a 01   switch dynamic command mode -> raster
                                              (sent FIRST so a printer whose static
                                              command mode is "P-touch Template" is
                                              forced into raster before it reads data)
    200 x 00                     invalidate  (flush any half-received command)
    1B 40                        ESC @        initialize
    1B 69 61 01                  ESC i a 01   switch to raster mode again (post-init)
    1B 69 53                     ESC i S      status information request
    1B 69 7A CE 0A 3E 00         ESC i z      print information (media & quality)
          <N as uint32 LE> 00 00
        CE = valid flags:  0x80 PI_RECOVER  (always set by brother_ql -
                                             "printer recovers to default
                                             settings on media mismatch")
                           0x40 PI_QUALITY  (priority to print quality; brother_ql
                                             hq=True default)
                           0x08 PI_LENGTH   (media length field is valid)
                           0x04 PI_WIDTH    (media width field is valid)
                           0x02 PI_KIND     (media type field is valid)
                           (0x01 = PI_STARTING_PAGE is NOT set; brother_ql never
                            sets it either)
        0A = media type      0x0A continuous length tape (0x0B = die-cut)
        3E = media width mm  62
        00 = media length mm 0 for continuous
        N  = number of raster lines that follow (uint32, little endian)
        00 = starting page   0x00 for the first (only) page, 0x01 otherwise
        00 = reserved
    1B 69 4D 40                  ESC i M 40   various mode: bit 6 = auto cut ON
                                              (only when cut=True)
    1B 69 41 01                  ESC i A 01   cut every 1 label (only when cut=True)
    1B 69 4B 08 | 00             ESC i K      expanded mode: bit 3 = cut at end
                                              (set when cut=True), bit 0 = 0 mono,
                                              bit 6 = 0 (300 dpi, no 600 dpi)
    1B 69 64 23 00               ESC i d      feed margin = 35 dots (uint16 LE)
    4D 02                        M            compression: 02 = TIFF/PackBits
                                              (omitted entirely when not compressing;
                                              brother_ql never sends "M 00")
    N raster lines, one per image row (top row first):
        67 00 <n> <n bytes>      'g' 00 n     raster line; n = 90 uncompressed
                                              (720 pins / 8) or the PackBits
                                              length (1 byte, always <= 91)
        5A                       'Z'          zero raster line (only when
                                              compress=True and zero_lines=True,
                                              for a completely blank row)
    1A                           print with feeding  (last page)

Design decisions
----------------
* Geometry (label "62" on a 720-pin head):  printable width 696 dots,
  right margin 12 dots, so the image is pasted at x = 720 - 696 - 12 = 12 and
  the 24 unused pins are split 12 left / 12 right (exactly brother_ql's
  `new_im.paste(im, (device_pixel_width - im.size[0] - right_margin_dots, 0))`).
* Threshold: brother_ql semantics.  `threshold=70` (percent) means a pixel is
  printed when its darkness (255 - grey) >= int((100-70)/100*255) = 76,
  i.e. grey <= 179.  Alpha is composited on white first.
* Rotation (rotate="auto"): a continuous label is never rotated by brother_ql;
  we add one unambiguous case: if the width is not 696 but the HEIGHT is
  exactly 696 (label drawn sideways, short side = tape width) rotate 90 deg
  CCW (PIL convention, like brother_ql's `im.rotate(90, expand=True)`).
  Any other size is scaled (aspect preserved, LANCZOS) so width == 696.
  rotate=0/90/180/270 forces an explicit rotation instead.
* Bit order: each raster line is 90 bytes, MSB first.  Like brother_ql we
  mirror the row (FLIP_LEFT_RIGHT) before packing, so byte 0 bit 7 is the
  RIGHT-most pin (x = 719) and byte 89 bit 0 is the LEFT-most pin (x = 0).
  brother_ql does `image.transpose(Image.FLIP_LEFT_RIGHT)` then
  `image.convert("1").tobytes("raw")` (Pillow packs mode "1" MSB first) on an
  image it had already inverted with ImageOps.invert, so 1 bits = printed
  (black) dots -- which is Brother's documented convention ("1: print").
* 'Z' zero-line command: although documented for the QL-800 series, the
  QL-810W REJECTS it in raster mode (the printer latches a red "other-error"
  and prints nothing).  The field-proven brother_ql library never emits 'Z' --
  it always sends a 'g' line, and a blank row PackBits-compresses to just
  `g 00 02 A7 00` (5 bytes).  So zero_lines DEFAULTS TO FALSE here to match it.
  Leave it False for the QL-810W; True is kept only for other models/testing.
"""

import queue
import socket
import struct
import threading
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from PIL import Image

__all__ = [
    "encode_label", "prepare_image", "decode_job", "send_to_printer",
    "print_image", "printer_status", "find_printers", "packbits_encode",
    "packbits_decode", "LABELS", "PRINTERS",
]

# --------------------------------------------------------------------------- #
# Specs                                                                        #
# --------------------------------------------------------------------------- #

# Continuous (endless) tapes, values copied from brother_ql.labels.
# printable = printable width in dots, right_margin = dots to the right of the
# printable area, feed_margin = ESC i d value.  Only "62" is exercised by the
# self-test; the others are here for completeness.
LABELS = {
    "12": {"tape_mm": 12, "printable": 106, "right_margin": 29, "feed_margin": 35},
    "29": {"tape_mm": 29, "printable": 306, "right_margin": 6,  "feed_margin": 35},
    "38": {"tape_mm": 38, "printable": 413, "right_margin": 12, "feed_margin": 35},
    "50": {"tape_mm": 50, "printable": 554, "right_margin": 12, "feed_margin": 35},
    "54": {"tape_mm": 54, "printable": 590, "right_margin": 0,  "feed_margin": 35},
    "62": {"tape_mm": 62, "printable": 696, "right_margin": 12, "feed_margin": 35},
}

# 720-pin, 300 dpi QL models that support the full command set we emit.
PRINTERS = {
    "QL-800":    {"pins": 720},
    "QL-810W":   {"pins": 720},
    "QL-820NWB": {"pins": 720},
    "QL-700":    {"pins": 720},
    "QL-710W":   {"pins": 720},
    "QL-720NW":  {"pins": 720},
}

MEDIA_TYPE_CONTINUOUS = 0x0A
MEDIA_TYPE_DIE_CUT = 0x0B

# print-information valid flags (ESC i z)
PI_KIND = 0x02
PI_WIDTH = 0x04
PI_LENGTH = 0x08
PI_QUALITY = 0x40
PI_RECOVER = 0x80

ESC = b"\x1b"
CMD_INVALIDATE = b"\x00" * 200
CMD_INITIALIZE = ESC + b"@"
CMD_STATUS = ESC + b"iS"
CMD_RASTER_MODE = ESC + b"ia\x01"
RASTER_LINE = b"g\x00"
ZERO_LINE = b"Z"
CMD_PRINT_LAST = b"\x1a"

_INVERT = bytes(255 - i for i in range(256))
_LANCZOS = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.BICUBIC)
_FLIP_LR = getattr(getattr(Image, "Transpose", Image), "FLIP_LEFT_RIGHT")


def _label_spec(label: str) -> Dict[str, int]:
    try:
        return LABELS[str(label)]
    except KeyError:
        raise ValueError("unknown label %r (known: %s)" % (label, ", ".join(sorted(LABELS))))


def _printer_spec(printer: str) -> Dict[str, int]:
    try:
        return PRINTERS[printer]
    except KeyError:
        raise ValueError("unknown printer %r (known: %s)" % (printer, ", ".join(sorted(PRINTERS))))


# --------------------------------------------------------------------------- #
# PackBits (TIFF compression, Brother "M 02")                                  #
# --------------------------------------------------------------------------- #

def packbits_encode(data: bytes) -> bytes:
    """Standard PackBits: header 0..127 => next h+1 literal bytes;
    header 129..255 => repeat next byte 257-h times (2..128); 128 unused."""
    out = bytearray()
    n = len(data)
    i = 0
    while i < n:
        b = data[i]
        j = i + 1
        while j < n and j - i < 128 and data[j] == b:
            j += 1
        run = j - i
        if run >= 2:
            out.append(257 - run)
            out.append(b)
            i = j
            continue
        # literal run: stop where a repeat of >= 2 begins
        j = i + 1
        while j < n and j - i < 128:
            if j + 1 < n and data[j] == data[j + 1]:
                break
            j += 1
        out.append(j - i - 1)
        out += data[i:j]
        i = j
    return bytes(out)


def packbits_decode(data: bytes) -> bytes:
    out = bytearray()
    n = len(data)
    i = 0
    while i < n:
        h = data[i]
        i += 1
        if h == 128:
            continue
        if h < 128:
            cnt = h + 1
            chunk = data[i:i + cnt]
            if len(chunk) != cnt:
                raise ValueError("truncated PackBits literal")
            out += chunk
            i += cnt
        else:
            if i >= n:
                raise ValueError("truncated PackBits repeat")
            out += bytes((data[i],)) * (257 - h)
            i += 1
    return bytes(out)


# --------------------------------------------------------------------------- #
# Image preparation                                                            #
# --------------------------------------------------------------------------- #

def _flatten_to_grey(img: Image.Image) -> Image.Image:
    """Any PIL mode -> 'L', with transparency composited on white."""
    im = img
    if im.mode == "P" and "transparency" in im.info:
        im = im.convert("RGBA")
    if im.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        im = bg
    if im.mode != "L":
        im = im.convert("L")
    return im


def prepare_image(img: Image.Image, label: str = "62", printer: str = "QL-810W",
                  threshold: int = 70, rotate: Union[str, int] = "auto") -> Image.Image:
    """Return the exact 1-bit bitmap that will be printed: mode "1",
    width = printer pins (720), 0 = black (printed), 255 = white.  The label
    content sits at x = pins - printable - right_margin, margins are white."""
    spec = _label_spec(label)
    pins = _printer_spec(printer)["pins"]
    pw = spec["printable"]

    im = _flatten_to_grey(img)

    if rotate == "auto":
        angle = 90 if (im.size[0] != pw and im.size[1] == pw) else 0
    else:
        angle = int(rotate) % 360
    if angle:
        im = im.rotate(angle, expand=True)

    if im.size[0] != pw:
        new_h = max(1, int(round(pw / float(im.size[0]) * im.size[1])))
        im = im.resize((pw, new_h), _LANCZOS)

    t = min(255, max(0, int((100 - int(threshold)) / 100.0 * 255)))
    limit = 255 - t  # grey <= limit  =>  printed
    lut = [0 if v <= limit else 255 for v in range(256)]
    im = im.point(lut, mode="1")

    canvas = Image.new("1", (pins, im.size[1]), 255)
    canvas.paste(im, (pins - pw - spec["right_margin"], 0))
    return canvas


def _raster_rows(prepared: Image.Image) -> List[bytes]:
    """Split a prepared (mode '1', 720 wide) image into per-row byte strings
    in printer bit order: mirrored, MSB first, 1 = print."""
    pins = prepared.size[0]
    if pins % 8:
        raise ValueError("raster width must be a multiple of 8")
    row_len = pins // 8
    raw = prepared.transpose(_FLIP_LR).tobytes()  # MSB first, 1 = white
    raw = raw.translate(_INVERT)                   # 1 = black = print
    return [raw[i:i + row_len] for i in range(0, len(raw), row_len)]


def _rows_to_image(rows: Sequence[bytes], pins: int) -> Image.Image:
    """Inverse of _raster_rows."""
    row_len = pins // 8
    for r in rows:
        if len(r) != row_len:
            raise ValueError("row has %d bytes, expected %d" % (len(r), row_len))
    raw = b"".join(rows).translate(_INVERT)
    im = Image.frombytes("1", (pins, len(rows)), raw)
    return im.transpose(_FLIP_LR)


# --------------------------------------------------------------------------- #
# Job encoder / decoder                                                        #
# --------------------------------------------------------------------------- #

def _print_information(spec: Dict[str, int], raster_lines: int, page: int = 0,
                       hq: bool = True) -> bytes:
    flags = PI_RECOVER | PI_KIND | PI_WIDTH | PI_LENGTH
    if hq:
        flags |= PI_QUALITY
    return (ESC + b"iz" + bytes((flags, MEDIA_TYPE_CONTINUOUS, spec["tape_mm"], 0))
            + struct.pack("<L", raster_lines) + bytes((0 if page == 0 else 1, 0)))


def encode_label(img: Image.Image, label: str = "62", printer: str = "QL-810W",
                 cut: bool = True, compress: bool = True, threshold: int = 70,
                 rotate: Union[str, int] = "auto", zero_lines: bool = False) -> bytes:
    """Encode a PIL image into a complete Brother raster print job (bytes).
    See the module docstring for the byte layout."""
    spec = _label_spec(label)
    pins = _printer_spec(printer)["pins"]
    prepared = prepare_image(img, label=label, printer=printer,
                             threshold=threshold, rotate=rotate)
    rows = _raster_rows(prepared)
    if not rows:
        raise ValueError("image has no rows")
    row_len = pins // 8

    out = bytearray()
    # Command order mirrors the field-proven `brother_ql` library exactly.
    # The raster-mode switch (ESC i a 01) MUST come FIRST -- before the
    # invalidate -- so a printer whose static command mode is "P-touch
    # Template" is forced into raster mode before it interprets any data.
    # Sending it late (after the invalidate) makes such a printer reject the
    # job and latch a red "other-error" that only a power cycle clears.
    out += CMD_RASTER_MODE          # ESC i a 01  -- switch to raster FIRST
    out += CMD_INVALIDATE           # 200 x 00    -- flush any half-received cmd
    out += CMD_INITIALIZE           # ESC @       -- initialize
    out += CMD_RASTER_MODE          # ESC i a 01  -- again, after init (brother_ql)
    out += CMD_STATUS               # ESC i S     -- status information request
    out += _print_information(spec, len(rows))
    if cut:
        out += ESC + b"iM" + b"\x40"        # auto cut on
        out += ESC + b"iA" + b"\x01"        # cut every label
    out += ESC + b"iK" + bytes((0x08 if cut else 0x00,))   # expanded mode
    out += ESC + b"id" + struct.pack("<H", spec["feed_margin"])
    if compress:
        out += b"M\x02"             # compression mode: TIFF/PackBits
        # NB: when NOT compressing, brother_ql omits the 'M' command entirely
        # (it does not send "M 00").  We match that -- an extra "M 00" is one
        # of the bytes the proven library never emits.

    blank = b"\x00" * row_len
    for row in rows:
        if compress:
            if zero_lines and row == blank:
                out += ZERO_LINE
                continue
            data = packbits_encode(row)
        else:
            data = row
        if len(data) > 255:
            raise ValueError("raster line too long")
        out += RASTER_LINE + bytes((len(data),)) + data
    out += CMD_PRINT_LAST
    return bytes(out)


def decode_job(job: bytes, pins: int = 720) -> Dict[str, Any]:
    """Parse a job produced by encode_label() back into its parts.
    Returns dict with keys: info (print-information fields), commands (list of
    (name, value) tuples), compressed (bool), rows (list of 90-byte rows),
    image (PIL mode '1', 0 = black), ok_end (bool)."""
    row_len = pins // 8
    i = 0
    n = len(job)
    cmds: List[Tuple[str, Any]] = []
    info: Dict[str, Any] = {}
    compressed = False
    rows: List[bytes] = []
    g_lines = 0
    zero_lines = 0
    ok_end = False
    blank = b"\x00" * row_len

    while i < n:
        b = job[i]
        if b == 0x00:  # invalidate: a run of NUL bytes (may appear after the
                       # leading switch-mode command, not only at offset 0)
            j = i
            while j < n and job[j] == 0:
                j += 1
            cmds.append(("invalidate", j - i))
            i = j
            continue
        if b == 0x1B:
            if job[i + 1:i + 2] == b"@":
                cmds.append(("initialize", None))
                i += 2
                continue
            if job[i + 1:i + 2] != b"i":
                raise ValueError("unknown ESC command at %d" % i)
            c = job[i + 2:i + 3]
            if c == b"S":
                cmds.append(("status_request", None)); i += 3
            elif c == b"a":
                cmds.append(("switch_mode", job[i + 3])); i += 4
            elif c == b"z":
                blk = job[i + 3:i + 13]
                if len(blk) != 10:
                    raise ValueError("truncated print-information")
                info = {
                    "valid_flags": blk[0], "media_type": blk[1],
                    "media_width": blk[2], "media_length": blk[3],
                    "raster_lines": struct.unpack("<L", blk[4:8])[0],
                    "page": blk[8], "reserved": blk[9],
                }
                cmds.append(("print_information", info)); i += 13
            elif c == b"M":
                cmds.append(("various_mode", job[i + 3])); i += 4
            elif c == b"A":
                cmds.append(("cut_every", job[i + 3])); i += 4
            elif c == b"K":
                cmds.append(("expanded_mode", job[i + 3])); i += 4
            elif c == b"d":
                cmds.append(("margin", struct.unpack("<H", job[i + 3:i + 5])[0])); i += 5
            else:
                raise ValueError("unknown ESC i %r at %d" % (c, i))
        elif b == 0x4D:  # 'M'
            compressed = job[i + 1] == 0x02
            cmds.append(("compression", job[i + 1])); i += 2
        elif b == 0x67:  # 'g'
            if job[i + 1] != 0x00:
                raise ValueError("bad raster line header at %d" % i)
            ln = job[i + 2]
            data = job[i + 3:i + 3 + ln]
            if len(data) != ln:
                raise ValueError("truncated raster line at %d" % i)
            rows.append(packbits_decode(data) if compressed else bytes(data))
            g_lines += 1
            i += 3 + ln
        elif b == 0x5A:  # 'Z'
            rows.append(blank)
            zero_lines += 1
            i += 1
        elif b in (0x1A, 0x0C):
            cmds.append(("print", b))
            ok_end = (b == 0x1A and i == n - 1)
            i += 1
        else:
            raise ValueError("unexpected byte 0x%02X at offset %d" % (b, i))

    return {
        "info": info, "commands": cmds, "compressed": compressed, "rows": rows,
        "g_lines": g_lines, "zero_lines": zero_lines,
        "image": _rows_to_image(rows, pins) if rows else None, "ok_end": ok_end,
    }


# --------------------------------------------------------------------------- #
# Network                                                                      #
# --------------------------------------------------------------------------- #

def send_to_printer(ip: str, job_bytes: bytes, port: int = 9100,
                    timeout: float = 15) -> Dict[str, Any]:
    """Send a raw job to the printer's port 9100.
    Returns {"ok": True, "bytes": n} or {"ok": False, "error": "..."}."""
    if not job_bytes:
        return {"ok": False, "error": "empty job"}
    sock = None
    try:
        sock = socket.create_connection((ip, int(port)), timeout=timeout)
        sock.sendall(job_bytes)
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        return {"ok": True, "bytes": len(job_bytes)}
    except (OSError, socket.timeout) as exc:
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def print_image(ip: str, img: Image.Image, port: int = 9100, timeout: float = 15,
                **encode_kwargs: Any) -> Dict[str, Any]:
    """Convenience: encode_label(img, **encode_kwargs) then send_to_printer()."""
    try:
        job = encode_label(img, **encode_kwargs)
    except Exception as exc:  # noqa: BLE001 - surface as result, not raise
        return {"ok": False, "error": "encode: %s: %s" % (type(exc).__name__, exc)}
    res = send_to_printer(ip, job, port=port, timeout=timeout)
    k = job.find(ESC + b"iz")
    if k >= 0:
        res["raster_lines"] = struct.unpack("<L", job[k + 7:k + 11])[0]
    return res


_MEDIA_TYPES = {
    0x00: "No media", 0x01: "Laminated tape", 0x03: "Non-laminated tape",
    0x0A: "Continuous length tape", 0x0B: "Die-cut labels",
    0x11: "Heat-shrink tube", 0x4A: "Continuous length tape",
    0x4B: "Die-cut labels", 0xFF: "Incompatible tape",
}
_ERRORS_1 = ["No media when printing", "End of media (die-cut only)",
             "Tape cutter jam", "(bit 3 unused)", "Main unit in use",
             "Printer turned off", "High-voltage adapter", "Fan motor error"]
_ERRORS_2 = ["Replace media error", "Expansion buffer full",
             "Transmission / communication error", "Communication buffer full",
             "Cover open", "Cancel key", "Media cannot be fed", "System error"]
_STATUS_TYPES = {0x00: "Reply to status request", 0x01: "Printing completed",
                 0x02: "Error occurred", 0x05: "Notification", 0x06: "Phase change"}
_PHASES = {0x00: "Waiting to receive", 0x01: "Printing"}
_MODEL_CODES = {0x4F: "QL-500/550", 0x31: "QL-560", 0x32: "QL-570", 0x33: "QL-580N",
                0x51: "QL-650TD", 0x35: "QL-700", 0x36: "QL-710W", 0x37: "QL-720NW",
                0x38: "QL-800", 0x39: "QL-810W", 0x41: "QL-820NWB",
                0x50: "QL-1050", 0x34: "QL-1060N"}


def parse_status(reply: bytes) -> Dict[str, Any]:
    """Best-effort parse of a 32-byte Brother status block."""
    if len(reply) < 32:
        return {"ok": False, "error": "short status reply (%d bytes)" % len(reply),
                "raw": reply.hex()}
    r = reply[:32]
    err1 = [_ERRORS_1[b] for b in range(8) if r[8] & (1 << b)]
    err2 = [_ERRORS_2[b] for b in range(8) if r[9] & (1 << b)]
    return {
        "ok": True,
        "valid_header": r[0] == 0x80 and r[1] == 0x20 and r[2] == 0x42,
        "model_code": r[4], "model": _MODEL_CODES.get(r[4], "unknown (0x%02X)" % r[4]),
        "errors": err1 + err2, "error_bytes": (r[8], r[9]),
        "media_width_mm": r[10], "media_type_code": r[11],
        "media_type": _MEDIA_TYPES.get(r[11], "unknown (0x%02X)" % r[11]),
        "media_length_mm": r[17],
        "status_type": _STATUS_TYPES.get(r[18], "unknown (0x%02X)" % r[18]),
        "phase": _PHASES.get(r[19], "unknown (0x%02X)" % r[19]),
        "phase_number": struct.unpack(">H", r[20:22])[0],
        "notification": r[22],
        "raw": r.hex(),
    }


def printer_status(ip: str, port: int = 9100, timeout: float = 5) -> Dict[str, Any]:
    """Send ESC i S and parse the 32-byte reply (tolerant of no reply)."""
    sock = None
    try:
        sock = socket.create_connection((ip, int(port)), timeout=timeout)
        sock.sendall(CMD_STATUS)
        buf = b""
        while len(buf) < 32:
            chunk = sock.recv(32 - len(buf))
            if not chunk:
                break
            buf += chunk
        if not buf:
            return {"ok": False, "error": "connected but no status reply"}
        return parse_status(buf)
    except (OSError, socket.timeout) as exc:
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def find_printers(prefix: str = "192.168.0.", ports: Sequence[int] = (9100,),
                  timeout: float = 0.4, max_threads: int = 40,
                  hosts: Optional[Sequence[int]] = None) -> List[str]:
    """Threaded scan of prefix+1..254 for hosts with any of `ports` open.
    Returns a sorted list of IP strings."""
    work: "queue.Queue[str]" = queue.Queue()
    for h in (hosts if hosts is not None else range(1, 255)):
        work.put("%s%d" % (prefix, h))
    found: List[str] = []
    lock = threading.Lock()

    def worker() -> None:
        while True:
            try:
                ip = work.get_nowait()
            except queue.Empty:
                return
            for port in ports:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(timeout)
                try:
                    if s.connect_ex((ip, int(port))) == 0:
                        with lock:
                            found.append(ip)
                        break
                except OSError:
                    pass
                finally:
                    s.close()

    threads = [threading.Thread(target=worker, daemon=True)
               for _ in range(max(1, min(int(max_threads), 40)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return sorted(found, key=lambda s: [int(p) for p in s.split(".")])


# --------------------------------------------------------------------------- #
# Self-test                                                                    #
# --------------------------------------------------------------------------- #

def _make_test_label(width: int = 696, height: int = 320) -> Image.Image:
    from PIL import ImageDraw, ImageFont
    im = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(im)
    d.rectangle([4, 4, width - 5, height - 5], outline="black", width=6)
    text = "BRUNO'S TEST"
    font = None
    for name in ("arialbd.ttf", "arial.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans.ttf",
                 "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                 "/System/Library/Fonts/Helvetica.ttc", "/Library/Fonts/Arial.ttf",
                 "C:/Windows/Fonts/arialbd.ttf"):
        try:
            font = ImageFont.truetype(name, 84)
            break
        except OSError:
            continue
    if font is None:
        try:
            font = ImageFont.load_default(size=96)
        except TypeError:
            font = ImageFont.load_default()
    try:
        box = d.textbbox((0, 0), text, font=font)
        tw, th = box[2] - box[0], box[3] - box[1]
        ox, oy = box[0], box[1]
    except AttributeError:  # very old Pillow
        tw, th = d.textsize(text, font=font)
        ox = oy = 0
    d.text(((width - tw) // 2 - ox, (height - th) // 2 - oy), text, fill="black", font=font)
    d.text((24, height - 60), "62mm continuous  QL-810W  300dpi", fill="black")
    return im


# ─────────────────────────────────────────────────────────────────────────────
# AirPrint / IPP (URF) path.  9 Sep 2026: the raw-raster port-9100 path is
# REJECTED by this QL-810W's firmware (it latches a red "other-error" even for a
# job byte-identical to the proven brother_ql library, in Raster command mode,
# with correct 62mm continuous media).  The printer's NATIVE network path —
# AirPrint/IPP with an image/urf document — works cleanly (verified: the printer
# reports job-completed-successfully and a label feeds).  So QL-810W labels go
# out via encode_urf()+ipp_print() instead of encode_label()+send_to_printer().
#
# URF (Apple Raster) format, verified against apple/cups raster-stream.c and
# mbevand/urf2image:  "UNIRAST\0" + page_count(uint32 BE), then per page a
# 32-byte header [bpp, colorspace_idx, duplex, quality, mediatype, mediapos,
# 0*6, width(BE), height(BE), dpi(BE), 0*8], then per output line a 1-byte
# line-repeat (count-1) followed by URF-PackBits until `width` px are produced
# (0x00..0x7F n => repeat next pixel n+1 times; 0x00=black..0xFF=white).
# This printer advertises only "W8" (8-bit grey) => bpp=8, colorspace_idx=0
# (CUPS_CSPACE_SW).  We emit repeat-runs only (always valid; mono rows are runs).
# ─────────────────────────────────────────────────────────────────────────────
def _urf_packbits_line(row: bytes) -> bytes:
    out = bytearray()
    i = 0; n = len(row)
    while i < n:
        px = row[i]; j = i + 1
        while j < n and row[j] == px and (j - i) < 128:
            j += 1
        out.append((j - i) - 1)     # 0x00..0x7F: repeat next pixel (run) times
        out.append(px)
        i = j
    return bytes(out)


def encode_urf(img: Image.Image, dpi: int = 300) -> bytes:
    """Encode a PIL image as a single-page URF (image/urf) document for the
    QL-810W's AirPrint path.  8-bit greyscale, 0=black..255=white."""
    g = img.convert("L")
    W, H = g.size
    px = g.load()
    hdr = bytearray(32)
    hdr[0] = 8                       # bpp
    hdr[1] = 0                       # colorspace idx -> CUPS_CSPACE_SW ("W8")
    hdr[3] = 4                       # quality: normal
    struct.pack_into(">I", hdr, 12, W)
    struct.pack_into(">I", hdr, 16, H)
    struct.pack_into(">I", hdr, 20, dpi)
    body = bytearray(b"UNIRAST\x00" + struct.pack(">I", 1))
    body += hdr
    for y in range(H):
        body += b"\x00"              # line-repeat 0 -> this line once
        body += _urf_packbits_line(bytes(px[x, y] for x in range(W)))
    return bytes(body)


def decode_urf(doc: bytes):
    """Minimal decoder for round-trip validation; returns (W, H, rows)."""
    assert doc[:8] == b"UNIRAST\x00", "bad URF magic"
    hdr = doc[12:44]
    W = struct.unpack(">I", hdr[12:16])[0]; H = struct.unpack(">I", hdr[16:20])[0]
    i = 44; rows = []
    while len(rows) < H and i < len(doc):
        rep = doc[i] + 1; i += 1
        line = bytearray()
        while len(line) < W:
            code = doc[i]; i += 1
            if code == 0x80:
                line += b"\xff" * (W - len(line)); break
            if code <= 0x7f:
                line += bytes([doc[i]]) * (code + 1); i += 1
            else:
                m = (256 - code) + 1
                line += doc[i:i + m]; i += m
        line = bytes(line[:W])
        rows.extend([line] * rep)
    return W, H, rows


def _ipp_attr(tag: int, name: str, value: str) -> bytes:
    nb = name.encode(); vb = value.encode()
    return bytes([tag]) + struct.pack(">H", len(nb)) + nb + struct.pack(">H", len(vb)) + vb


def ipp_print(ip: str, doc: bytes, fmt: str = "image/urf", user: str = "dashboard",
              jobname: str = "label", path: str = "/ipp/print", port: int = 631,
              timeout: float = 30) -> Dict[str, Any]:
    """Send an IPP Print-Job carrying `doc` to the printer.  Returns
    {'ok': bool, 'status': int}. status 0x0000..0x00ff == successful."""
    import urllib.request
    uri = "ipp://%s%s" % (ip, path)
    body = bytearray()
    body += struct.pack(">H", 0x0200)              # IPP 2.0
    body += struct.pack(">H", 0x0002)              # Print-Job
    body += struct.pack(">I", 1)                   # request-id
    body += b"\x01"                                # operation-attributes-tag
    body += _ipp_attr(0x47, "attributes-charset", "utf-8")
    body += _ipp_attr(0x48, "attributes-natural-language", "en")
    body += _ipp_attr(0x45, "printer-uri", uri)
    body += _ipp_attr(0x42, "requesting-user-name", user)
    body += _ipp_attr(0x42, "job-name", jobname)
    body += _ipp_attr(0x49, "document-format", fmt)
    body += b"\x03"                                # end-of-attributes-tag
    body += doc
    req = urllib.request.Request("http://%s:%d%s" % (ip, port, path), data=bytes(body),
                                 headers={"Content-Type": "application/ipp"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = r.read()
    status = struct.unpack(">H", resp[2:4])[0] if len(resp) >= 4 else -1
    return {"ok": (0 <= status <= 0x00ff), "status": status}


IPP_BUSY = 0x0507   # server-error-busy: the QL-810W rejects a new job while it is
                    # still printing one (multiple-document-jobs-supported=false),
                    # so batch copies MUST be serialised, not fired back-to-back.


def ipp_get_state(ip: str, path: str = "/ipp/print", port: int = 631,
                  timeout: float = 8) -> str:
    """Return the printer-state keyword: 'idle' | 'processing' | 'stopped' | ''."""
    import urllib.request
    uri = "ipp://%s%s" % (ip, path)
    body = bytearray()
    body += struct.pack(">H", 0x0200)              # IPP 2.0
    body += struct.pack(">H", 0x000b)              # Get-Printer-Attributes
    body += struct.pack(">I", 1)
    body += b"\x01"                                # operation-attributes-tag
    body += _ipp_attr(0x47, "attributes-charset", "utf-8")
    body += _ipp_attr(0x48, "attributes-natural-language", "en")
    body += _ipp_attr(0x45, "printer-uri", uri)
    body += _ipp_attr(0x44, "requested-attributes", "printer-state")  # 0x44 keyword
    body += b"\x03"                                # end-of-attributes-tag
    req = urllib.request.Request("http://%s:%d%s" % (ip, port, path), data=bytes(body),
                                 headers={"Content-Type": "application/ipp"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = r.read()
    except Exception:
        return ""
    # walk the IPP attribute stream for the printer-state enum (value-tag 0x23)
    i = 8; n = len(resp)
    while i < n:
        tag = resp[i]
        if tag in (0x01, 0x02, 0x04, 0x05):        # attribute-group tags
            i += 1; continue
        if tag == 0x03:                            # end-of-attributes
            break
        i += 1
        if i + 2 > n: break
        nl = struct.unpack(">H", resp[i:i + 2])[0]; i += 2
        name = resp[i:i + nl]; i += nl
        if i + 2 > n: break
        vl = struct.unpack(">H", resp[i:i + 2])[0]; i += 2
        val = resp[i:i + vl]; i += vl
        if name == b"printer-state" and tag == 0x23 and vl == 4:
            return {3: "idle", 4: "processing", 5: "stopped"}.get(struct.unpack(">i", val)[0], "")
    return ""


def ipp_wait_idle(ip: str, timeout: float = 25, poll: float = 1.0) -> bool:
    """Block until the printer is idle (or state is unreadable). True if free."""
    import time
    end = time.time() + timeout
    while time.time() < end:
        st = ipp_get_state(ip)
        if st in ("idle", ""):     # idle, or can't tell -> don't block forever
            return True
        time.sleep(poll)
    return False


def ipp_print_wait(ip: str, doc: bytes, fmt: str = "image/urf", timeout: float = 30,
                   busy_retries: int = 12) -> Dict[str, Any]:
    """Print one job, but wait for the printer to be free first and retry while it
    reports BUSY — so batches of copies come out one after another, none dropped."""
    import time
    ipp_wait_idle(ip, timeout=25)
    r = ipp_print(ip, doc, fmt=fmt, timeout=timeout)
    tries = 0
    while (not r.get("ok")) and int(r.get("status", -1)) == IPP_BUSY and tries < busy_retries:
        time.sleep(1.5)
        ipp_wait_idle(ip, timeout=25)
        r = ipp_print(ip, doc, fmt=fmt, timeout=timeout)
        tries += 1
    return r


def print_label_airprint(ip: str, img: Image.Image, dpi: int = 300,
                         timeout: float = 30) -> Dict[str, Any]:
    """Encode `img` as URF and print it via IPP/AirPrint on the QL-810W."""
    return ipp_print_wait(ip, encode_urf(img, dpi=dpi), timeout=timeout)


def _self_test() -> None:
    import os
    import random

    here = os.path.dirname(os.path.abspath(__file__))

    # PackBits round trips, including nasty edge cases
    rnd = random.Random(1234)
    samples = [b"", b"\x00", b"\x00" * 90, b"\xff" * 300, bytes(range(256)),
               b"abccccccccccdef", b"\x01\x02" * 100]
    for _ in range(200):
        n = rnd.randint(0, 200)
        samples.append(bytes(rnd.choice([0, 0, 0, 255, 255, rnd.randint(0, 255)])
                             for _ in range(n)))
    for s in samples:
        assert packbits_decode(packbits_encode(s)) == s, "PackBits round trip failed"
    print("PackBits: %d samples round-trip OK" % len(samples))

    label_img = _make_test_label()
    prepared = prepare_image(label_img)
    assert prepared.mode == "1" and prepared.size == (720, 320), prepared.size
    # margins must be white (unprinted): 12 px each side for 62 mm
    px = prepared.load()
    for y in range(prepared.size[1]):
        for x in list(range(0, 12)) + list(range(708, 720)):
            assert px[x, y] == 255, "margin pixel printed at (%d,%d)" % (x, y)
    # first printable column is the border -> black
    assert px[12 + 4, 100] == 0, "expected border pixel to be black"

    summary = {}
    # zero_lines defaults to False (QL-810W rejects the 'Z' command); we still
    # exercise the legacy zero_lines=True path here to keep decode_job covered.
    for compress in (True, False):
        zl = compress  # True case also tests the 'Z' path; False stays all-'g'
        job = encode_label(label_img, compress=compress, zero_lines=zl)
        # Preamble byte layout (mirrors the proven brother_ql library):
        #   [0:4]     ESC i a 01   switch to raster mode  (FIRST)
        #   [4:204]   200 x 00     invalidate
        #   [204:206] ESC @        initialize
        #   [206:210] ESC i a 01   switch to raster mode  (again, post-init)
        #   [210:213] ESC i S      status information request
        #   [213:226] ESC i z ...  print-information (13 bytes)
        #   [226:230] ESC i M 40   various mode (auto cut)
        #   [230:234] ESC i A 01   cut every 1
        #   [234:238] ESC i K 08   expanded mode
        #   [238:243] ESC i d 23 00 margin = 35
        #   [243:245] M 02         compression mode  -- ONLY when compress=True
        assert job[0:4] == b"\x1b\x69\x61\x01", "job must start with the raster switch"
        assert job[4:204] == b"\x00" * 200, "200 zero invalidate must follow the switch"
        assert job[204:206] == b"\x1b\x40", "ESC @ must follow the invalidate"
        assert job[206:210] == b"\x1b\x69\x61\x01", "second raster switch after init"
        assert job[210:213] == b"\x1b\x69\x53"
        pi = b"\x1b\x69\x7a" + bytes((0xCE, 0x0A, 62, 0)) + struct.pack("<L", 320) + b"\x00\x00"
        assert job[213:226] == pi, "print-information block mismatch: %s" % job[213:226].hex()
        assert job[226:230] == b"\x1b\x69\x4d\x40"
        assert job[230:234] == b"\x1b\x69\x41\x01"
        assert job[234:238] == b"\x1b\x69\x4b\x08"
        assert job[238:243] == b"\x1b\x69\x64\x23\x00"
        if compress:
            assert job[243:245] == b"M\x02", "compression mode must be M 02 when on"
        else:
            # no 'M' command at all -- raster starts immediately after margin
            assert job[243:244] == b"g", "no 'M' command expected when compress is off"
        assert job[-1:] == b"\x1a", "job must end with 0x1A"

        dec = decode_job(job)
        assert dec["ok_end"], "0x1A must be the final byte"
        assert dec["compressed"] is compress
        assert dec["info"]["raster_lines"] == 320 == len(dec["rows"]), dec["info"]
        assert dec["info"]["valid_flags"] == 0xCE and dec["info"]["media_type"] == 0x0A
        assert dec["info"]["media_width"] == 62 and dec["info"]["media_length"] == 0
        names = [c[0] for c in dec["commands"]]
        expect = ["switch_mode", "invalidate", "initialize", "switch_mode",
                  "status_request", "print_information", "various_mode", "cut_every",
                  "expanded_mode", "margin"]
        if compress:
            expect.append("compression")
        expect.append("print")
        assert names == expect, names
        assert dict(dec["commands"])["margin"] == 35

        # every raster line well formed (decode_job raises otherwise); check sizes.
        # NB: counts come from the parser, not substring searches -- 0x5A / 0x67
        # are legal bytes inside PackBits data.
        g_lines, z_lines = dec["g_lines"], dec["zero_lines"]
        if not compress:
            assert g_lines == 320, g_lines
            body = job[243:-1]
            assert len(body) == 320 * 93, "each uncompressed line must be g 00 5A + 90 bytes"
            for k in range(320):
                assert body[k * 93:k * 93 + 3] == b"g\x00\x5a"
        else:
            assert g_lines + z_lines == 320, (g_lines, z_lines)
            assert z_lines >= 1, "expected some blank rows -> 'Z' lines"
        for r in dec["rows"]:
            assert len(r) == 90

        # exact bitmap round trip
        back = dec["image"]
        assert back.size == prepared.size and back.mode == "1"
        assert back.tobytes() == prepared.tobytes(), "decoded bitmap differs from prepared bitmap"

        # bit order sanity: x=719 (right edge) is byte 0 bit 7, x=0 is byte 89 bit 0
        probe = Image.new("1", (720, 1), 255)
        probe.putpixel((719, 0), 0)
        r0 = _raster_rows(probe)[0]
        assert r0[0] == 0x80 and r0[1:] == b"\x00" * 89
        probe = Image.new("1", (720, 1), 255)
        probe.putpixel((0, 0), 0)
        r0 = _raster_rows(probe)[0]
        assert r0[89] == 0x01 and r0[:89] == b"\x00" * 89

        fname = os.path.join(here, "brother_test.bin" if compress else "brother_test_uncompressed.bin")
        with open(fname, "wb") as fh:
            fh.write(job)
        summary[compress] = (len(job), g_lines, z_lines, fname)

    # rotation / scaling rules
    tall = Image.new("L", (320, 696), 255)
    assert prepare_image(tall).size == (720, 320), "height==696 must auto-rotate"
    wide = Image.new("L", (1392, 400), 255)
    assert prepare_image(wide).size == (720, 200), "wider images scale to 696 keeping aspect"
    narrow = Image.new("RGBA", (348, 100), (0, 0, 0, 0))
    assert prepare_image(narrow).size == (720, 200)
    assert prepare_image(narrow).tobytes() == prepare_image(Image.new("L", (348, 100), 255)).tobytes(), \
        "transparent pixels must become white"

    print("Test label: 696x320 -> prepared 720x320, 12/12 px margins OK")
    for compress in (True, False):
        size, g, z, fname = summary[compress]
        print("compress=%-5s job=%6d bytes  g-lines=%3d  Z-lines=%3d  -> %s"
              % (compress, size, g, z, fname))
    with open(summary[True][3], "rb") as fh:
        head = fh.read()[204:245]
    print("Preamble after the 200 zero bytes (hex): %s"
          % " ".join("%02x" % b for b in head))
    dec_img = decode_job(open(summary[True][3], "rb").read())["image"]
    dec_img.convert("L").save(os.path.join(here, "brother_test_decoded.png"))
    print("Decoded bitmap written to brother_test_decoded.png")
    print("ALL SELF-TESTS PASSED")


if __name__ == "__main__":
    _self_test()
