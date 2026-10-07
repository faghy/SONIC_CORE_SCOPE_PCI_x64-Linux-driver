#!/usr/bin/env python3
"""
scope_dev.py - decode Creamware / Sonic Core SCOPE "S3" object files
(.dev devices, .io hardware I/O devices, .mdl modules, .pro projects,
.pre/.efp presets ...) and dump the serialized object tree.

Reimplemented from SCOPE 5.1 (Windows):

  container   wxvc.dll  (static zlib 1.1.3 with a Creamware patch)
                gz_open        FUN_10311100 @ 0x10311100  ('c' mode = method 7)
                check_header   FUN_10310930 @ 0x10310930  (magic "S3" or 1f 8b)
                inflate        inflate      @ 0x10313150  (zlib hdr XOR 0x21/0x63)
                inflate_blocks FUN_103119a0 @ 0x103119a0  (3 junk bits per block)
              cwWindows.dll  wxGzipFileInputStream (gzdopen/gzread wrapper)
  archive     cwWindows.dll  cwArchive (MFC CArchive clone: LE ints, MFC strings)
  objects     base.dll       ScopeFileHeader::Serialize   @ 0x1002f450
                             RIOObject::Serialize         @ 0x100822f0
                             RIOObject::ReadExtraBytes    @ 0x10012b20
                             RODObject::Load              @ 0x10087250
                             RODModule::Serialize         @ 0x100c27c0
                             RODPad::Serialize            @ 0x100b6bf0
                             RODRouting::SerializeRouting @ 0x100ae620
                             ROCAtom::Serialize           @ 0x100bdbe0
                             RIOVar::Serialize            @ 0x10082fb0
                             ROCPad::Serialize            @ 0x10083270
                             ROCParam::Serialize          @ 0x1007dc90
                             ROCParameter::Serialize      @ 0x100858d0
                             RODParameter::Serialize      @ 0x10085650
                             ROCParamList::Serialize      @ 0x10083320
                             RODModule::SerializeRefInformation @ 0x100bee90
                             RODModule::LoadReferenceID   @ 0x10016900

GUI objects (GO*, Pep* display objects, bitmaps) are not decoded: their
serializers live partly in PepBase/*.dll.  They are skipped by trying the
offsets in front of the next ROD*/ROC* class-name record and keeping the one
from which the enclosing object's remaining fields parse consistently
(Parser.skip_gui; memoized, validated against known object ids).  Everything
audio-relevant (modules, DSP atoms, pads, routings, parameters, references to
other .dev files, DSP placement) is decoded field by field.  Spec and
validation: docs/io_format.md.

Usage:
  scope_dev.py FILE                 dump object tree
  scope_dev.py --summary FILE ...   one-line-per-object summary (atoms, pads, routes)
  scope_dev.py --plain OUT FILE     write the decompressed archive
  scope_dev.py --check FILE|DIR ... parse everything, report failures
  options: --gui (show skipped GUI blobs), --json,
           --dsp DIR (with --summary: name of the DSP file behind each atom)

Python 3 standard library only.
"""
import argparse
import json
import os
import re
import struct
import sys
import zlib

# ---------------------------------------------------------------------------
# 1. container: "S3" = gzip with patched header and patched deflate
# ---------------------------------------------------------------------------

_LB = (3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 17, 19, 23, 27, 31, 35, 43, 51, 59,
       67, 83, 99, 115, 131, 163, 195, 227, 258)
_LE = (0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4,
       5, 5, 5, 5, 0)
_DB = (1, 2, 3, 4, 5, 7, 9, 13, 17, 25, 33, 49, 65, 97, 129, 193, 257, 385, 513,
       769, 1025, 1537, 2049, 3073, 4097, 6145, 8193, 12289, 16385, 24577)
_DE = (0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10,
       11, 11, 12, 12, 13, 13)
_ORD = (16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15)


class FormatError(Exception):
    pass


def _huff_table(lens):
    """Canonical Huffman -> flat lookup table indexed by the next mx bits
    (LSB first). entry = sym << 4 | codelen, -1 = invalid."""
    mx = max(lens) if lens else 0
    if mx == 0:
        return [-1], 0
    cnt = [0] * (mx + 1)
    for ln in lens:
        if ln:
            cnt[ln] += 1
    nxt = [0] * (mx + 2)
    code = 0
    for i in range(1, mx + 1):
        code = (code + cnt[i - 1]) << 1
        nxt[i] = code
    size = 1 << mx
    tab = [-1] * size
    for sym, ln in enumerate(lens):
        if not ln:
            continue
        c = nxt[ln]
        nxt[ln] += 1
        rev = 0
        for i in range(ln):
            rev = (rev << 1) | ((c >> i) & 1)
        e = (sym << 4) | ln
        for j in range(rev, size, 1 << ln):
            tab[j] = e
    return tab, mx


_FIXED = None


def inflate_s3(d, pos, scrambled):
    """Raw deflate decoder. scrambled=True: Creamware variant, every block
    header (BFINAL/BTYPE) is preceded by 3 junk bits (inflate_blocks state
    TYPE, wxvc.dll 0x10311a3b).  Returns (data, offset after last block)."""
    global _FIXED
    if _FIXED is None:
        _FIXED = (_huff_table([8] * 144 + [9] * 112 + [7] * 24 + [8] * 8),
                  _huff_table([5] * 30))
    out = bytearray()
    n = len(d)
    bb = bc = 0
    p = pos
    while True:
        while bc < 32:
            bb |= (d[p] if p < n else 0) << bc
            p += 1
            bc += 8
        if scrambled:
            bb >>= 3
            bc -= 3
        final = bb & 1
        btype = (bb >> 1) & 3
        bb >>= 3
        bc -= 3
        if btype == 0:                       # stored
            k = bc & 7
            bb >>= k
            bc -= k
            p -= bc >> 3
            bb = bc = 0
            ln = d[p] | d[p + 1] << 8
            p += 4
            out += d[p:p + ln]
            p += ln
        else:
            if btype == 1:
                (lt, lm), (dt, dm) = _FIXED
            elif btype == 2:
                hl = (bb & 31) + 257
                hd = ((bb >> 5) & 31) + 1
                hc = ((bb >> 10) & 15) + 4
                bb >>= 14
                bc -= 14
                cl = [0] * 19
                for i in range(hc):
                    if bc < 3:
                        bb |= (d[p] if p < n else 0) << bc
                        p += 1
                        bc += 8
                    cl[_ORD[i]] = bb & 7
                    bb >>= 3
                    bc -= 3
                ct, cm = _huff_table(cl)
                cmask = (1 << cm) - 1
                lens = []
                while len(lens) < hl + hd:
                    while bc < 24:
                        bb |= (d[p] if p < n else 0) << bc
                        p += 1
                        bc += 8
                    e = ct[bb & cmask]
                    if e < 0:
                        raise FormatError("bad code-length code")
                    ln = e & 15
                    s = e >> 4
                    bb >>= ln
                    bc -= ln
                    if s < 16:
                        lens.append(s)
                    elif s == 16:
                        r = 3 + (bb & 3); bb >>= 2; bc -= 2
                        lens += [lens[-1]] * r
                    elif s == 17:
                        r = 3 + (bb & 7); bb >>= 3; bc -= 3
                        lens += [0] * r
                    else:
                        r = 11 + (bb & 127); bb >>= 7; bc -= 7
                        lens += [0] * r
                lt, lm = _huff_table(lens[:hl])
                dt, dm = _huff_table(lens[hl:])
            else:
                raise FormatError("invalid deflate block type")
            lmask = (1 << lm) - 1
            dmask = (1 << dm) - 1
            app = out.append
            while True:
                if bc < 48:
                    while bc < 56:
                        bb |= (d[p] if p < n else 0) << bc
                        p += 1
                        bc += 8
                e = lt[bb & lmask]
                if e < 0:
                    raise FormatError("bad literal/length code")
                ln = e & 15
                s = e >> 4
                bb >>= ln
                bc -= ln
                if s < 256:
                    app(s)
                    continue
                if s == 256:
                    break
                s -= 257
                eb = _LE[s]
                length = _LB[s] + (bb & ((1 << eb) - 1))
                bb >>= eb
                bc -= eb
                e = dt[bb & dmask]
                if e < 0:
                    raise FormatError("bad distance code")
                ln = e & 15
                s = e >> 4
                bb >>= ln
                bc -= ln
                eb = _DE[s]
                dist = _DB[s] + (bb & ((1 << eb) - 1))
                bb >>= eb
                bc -= eb
                st = len(out) - dist
                if st < 0:
                    raise FormatError("distance too far back")
                if dist >= length:
                    out += out[st:st + length]
                else:
                    for i in range(length):
                        app(out[st + i])
        if final:
            break
    p -= bc >> 3
    return bytes(out), p


def unpack_container(d):
    """Return (plain archive bytes, info dict).  Accepts 'S3' files (method 7,
    zlib-wrapped, optionally scrambled), standard gzip and plain archives."""
    info = {}
    if d[:2] == b"S3":
        method, flags = d[2], d[3]
        info["container"] = "S3"
        if method != 7 or flags & 0xE0:
            raise FormatError("S3: unexpected method %d flags 0x%x" % (method, flags))
        p = 10                                  # 6 bytes mtime/xfl/os skipped
        if flags & 4:
            p += 2 + (d[p] | d[p + 1] << 8)
        if flags & 8:
            p = d.index(b"\0", p) + 1
        if flags & 0x10:
            p = d.index(b"\0", p) + 1
        if flags & 2:
            p += 2
        cmf, flg = d[p], d[p + 1]
        scrambled = (cmf & 15) != 8             # inflate(): first byte low nibble
        if scrambled:
            cmf ^= 0x21
            flg ^= 0x63
        if (cmf & 15) != 8 or ((cmf << 8) | flg) % 31:
            raise FormatError("S3: bad zlib header")
        info["scrambled"] = scrambled
        plain, q = inflate_s3(d, p + 2, scrambled)
        adler = int.from_bytes(d[q:q + 4], "big")
        if zlib.adler32(plain) != adler:
            raise FormatError("S3: adler32 mismatch")
        q += 4
        crc, isize = struct.unpack_from("<II", d, q)
        if crc != zlib.crc32(plain) or isize != len(plain) & 0xFFFFFFFF:
            raise FormatError("S3: gzip trailer mismatch")
        return plain, info
    if d[:2] == b"\x1f\x8b":
        info["container"] = "gzip"
        return zlib.decompress(d, 31), info
    info["container"] = "plain"
    return d, info


# ---------------------------------------------------------------------------
# 2. archive reader (cwArchive)
# ---------------------------------------------------------------------------

MARK_VAR = 0xFFFFD8F1          # -9999: written before every RIOVar of a var list


class Reader:
    def __init__(self, data, pos=0):
        self.d = data
        self.p = pos

    def need(self, n):
        if self.p + n > len(self.d):
            raise FormatError("read past end @0x%x" % self.p)

    def u8(self):
        self.need(1)
        v = self.d[self.p]
        self.p += 1
        return v

    def u16(self):
        self.need(2)
        v = struct.unpack_from("<H", self.d, self.p)[0]
        self.p += 2
        return v

    def u32(self):
        self.need(4)
        v = struct.unpack_from("<I", self.d, self.p)[0]
        self.p += 4
        return v

    def i32(self):
        self.need(4)
        v = struct.unpack_from("<i", self.d, self.p)[0]
        self.p += 4
        return v

    def f32(self):
        self.need(4)
        v = struct.unpack_from("<f", self.d, self.p)[0]
        self.p += 4
        return v

    def f64(self):
        self.need(8)
        v = struct.unpack_from("<d", self.d, self.p)[0]
        self.p += 8
        return v

    def raw(self, n):
        if n < 0:
            raise FormatError("negative length")
        self.need(n)
        v = self.d[self.p:self.p + n]
        self.p += n
        return v

    def strlen(self):
        """ReadStringLength (cwWindows 0x1023f6e0): u8, 0xff -> u16,
        0xffff -> u32, 0xfffe -> unicode marker."""
        n = self.u8()
        if n != 0xFF:
            return n
        n = self.u16()
        if n == 0xFFFE:
            return -1
        if n == 0xFFFF:
            return self.u32()
        return n

    def string(self, maxlen=1 << 20):
        n = self.strlen()
        wide = n == -1
        if wide:
            n = self.strlen()
        if n > maxlen:
            raise FormatError("string too long (%d) @0x%x" % (n, self.p))
        b = self.raw(n * (2 if wide else 1))
        return b.decode("utf-16-le" if wide else "latin-1")


# ---------------------------------------------------------------------------
# 3. object model
# ---------------------------------------------------------------------------

# AttributeType (base.dll *Attribute::type()) -> (name, value kind).  Kinds
# read from the exported vtables (slot 0x34 = SerializeRead of the Long/
# Short/Float/Double/Bool/String base class) of base.dll [C].
ATTR = {
    0: ("Long", "i32"), 1: ("Short", "u16"), 2: ("Float", "f32"),
    3: ("Double", "f64"), 4: ("String", "str"), 5: ("Bool", "u8"),
    6: ("DataType", "u16"), 7: ("DataSize", "u16"), 8: ("DataElements", "u16"),
    9: ("RangeMin", "f64"), 10: ("RangeMax", "f64"), 0xC: ("Format", "str"),
    0xD: ("Sync", "u8"), 0xE: ("PropName", "str"), 0xF: ("PropValue", "str"),
    0x10: ("PropCategory", "str"), 0x11: ("PropRoutable", "str"),
    0x12: ("PropLicence", "str"), 0x13: ("PropEditable", "str"),
    0x14: ("PropFormat", "str"), 0x15: ("PropOrder", "str"),
    0x16: ("TabOrder", "str"), 0x17: ("PropMinSize", "str"),
    0x18: ("PropRightAlign", "str"), 0x19: ("PropNewLine", "str"),
    0x1A: ("NotifyPad", "u8"), 0x1B: ("RestorePad", "u16"), 0x1C: ("Unit", "u16"),
    0x1D: ("DynamicPad", "u8"), 0x1E: ("NumVoices", "u16"),
    0x1F: ("VoiceOffset", "u16"), 0x20: ("BoardId", "u16"), 0x21: ("DSPId", "u16"),
    0x22: ("OnSameDSP", "i32"), 0x23: ("ModularClass", "i32"),
    0x24: ("IgnoreType", "u8"), 0x25: ("RestorePadInSerialize", "u8"),
    0x26: ("HiddenRouting", "u8"), 0x27: ("RestoreObject", "u8"),
    0x28: ("SlotID", "i32"), 0x29: ("ModuleID", "str"),
}

# ExtraBytes chunk keys seen in RODModule::ReadExtraChunk (0x10098f00) etc.
EXTRA_KEYS = {0x102: "numVoices", 0x107: "boardID", 0x105: "moduleKey",
              0x108: "pepAtomFlag", 0x10E: "?", 0x10F: "dspPlacement",
              0x114: "paramListRect", 0x118: "paramConverter"}


def decode_placement(hexdata):
    """Chunk 0x10f = RODModule::SaveDSPPlacement (0x10056320) records:
    u8 0, ref(module), u32 n, n x i32 DSP assignment (-1 = free)  |
    u8 1, ref(module) (module flag 0x10)  |  u8 0xff = end."""
    b = bytes.fromhex(hexdata)
    out = []
    p = 0
    try:
        while p < len(b):
            tag = b[p]
            p += 1
            if tag == 0xFF:
                break
            ids = []
            while True:
                v = struct.unpack_from("<I", b, p)[0]
                p += 4
                ids.append(v & 0x7FFFFFFF)
                if not v & 0x80000000:
                    break
            if tag == 0:
                n = struct.unpack_from("<i", b, p)[0]
                p += 4
                dsps = list(struct.unpack_from("<%di" % n, b, p))
                p += 4 * n
                out.append({"module": ".".join("%x" % i for i in ids), "dsp": dsps})
            else:
                out.append({"module": ".".join("%x" % i for i in ids), "flag": tag})
    except struct.error:
        out.append("truncated")
    return out

class Obj:
    __slots__ = ("cls", "off", "end", "f", "kids")

    def __init__(self, cls, off):
        self.cls = cls
        self.off = off
        self.end = None
        self.f = {}
        self.kids = []

    def to_json(self):
        def conv(v):
            if isinstance(v, Obj):
                return v.to_json()
            if isinstance(v, (list, tuple)):
                return [conv(x) for x in v]
            if isinstance(v, dict):
                return {k: conv(x) for k, x in v.items()}
            return v
        d = {"class": self.cls, "off": self.off, "end": self.end}
        for k, v in self.f.items():
            d[k] = conv(v)
        if self.kids:
            d["children"] = conv(self.kids)
        return d


# end of a GOContainer tree: extras chunk 0x106 (tag 0xc, key 0x106, len 4, value, 0)
_GO_END_RE = re.compile(rb"\x0c\x00\x00\x00\x06\x01\x00\x00\x04\x00\x00\x00....\x00\x00\x00\x00", re.S)

# a ROD*/ROC* class-name record (MFC string); used as resync anchor
_ANCHOR_RE = re.compile(rb"[\x03-\x1f](?=(ROD|ROC)[A-Za-z0-9_]{1,28})")


class Parser:
    def __init__(self, data, keep_gui=False):
        self.r = Reader(data)
        self.d = data
        self.keep_gui = keep_gui
        self.stats = {"gui_skips": 0}
        self.strict = False
        self.deepest = (0, "")
        self.cls_log = []
        self.id_log = []            # object ids defined so far (rolled back on failed resync)
        self.id_set = {}            # id -> count
        self.resyncing = 0
        self.memo = {}

    # -- generic pieces ------------------------------------------------------
    def classname(self):
        p = self.r.p
        self.cls_log.append(p)
        s = self.r.string(64)
        if s and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", s):
            raise FormatError("bad class name %r @0x%x" % (s, p))
        return s

    def ref(self):
        """RODModule::LoadReferenceID: path of object ids, bit31 = 'more'."""
        ids = []
        for _ in range(255):
            v = self.r.u32()
            if v & 0x80000000:
                ids.append(v ^ 0x80000000)
                continue
            ids.append(v)
            break
        else:
            raise FormatError("reference too long")
        return ids if ids != [0] else None

    def known_ref(self, ids):
        """Validation used while resyncing: a routing/pad reference must
        start with an object id that was already defined."""
        if not ids:
            return False
        if len(ids) > 1:
            # path into a module loaded from another file (RefModule): its
            # ids are not defined here; only check plausibility
            # (the last element can carry flags in its top byte, e.g. 0x01000029)
            return 0 < ids[0] < 0x1000000 and all(i for i in ids)
        return ids[0] in self.id_set

    def extras(self):
        """RIOObject::ReadExtraBytes (0x10012b20)."""
        r = self.r
        tag = r.u32()
        if tag == 0:
            return None
        out = []
        if self.strict and tag in (2, 4, 0x16):
            raise FormatError("legacy extras tag %d rejected while resyncing" % tag)
        if tag in (1, 3, 5, 6, 7):
            # legacy: (key 0, len=tag); never seen in SCOPE 5 files, and
            # rejecting it makes GUI resync much more reliable
            raise FormatError("legacy extras tag %d @0x%x" % (tag, r.p - 4))
        elif tag == 2:
            out.append((0x100, r.raw(8).hex()))
        elif tag == 4:
            out.append((0x102, r.i32()))
        elif tag == 0x16:
            out.append((0x101, r.raw(0x16).hex()))
        else:
            # WriteExtraBytesLength (0x10012c30): tag = total byte size of
            # the (key, len, data) chunks that follow; list ends with key 0
            if tag > 0x1000000:
                raise FormatError("implausible extras tag 0x%x @0x%x" % (tag, r.p - 4))
            start = r.p
            while True:
                key = r.u32()
                if key == 0:
                    break
                ln = r.u32()
                if ln > 0x1000000 or key > 0x10000:
                    raise FormatError("bad extra chunk @0x%x" % (r.p - 8))
                b = r.raw(ln)
                if ln == 4:
                    v = struct.unpack("<i", b)[0]
                elif key == 0x10F:
                    v = decode_placement(b.hex())
                else:
                    v = b.hex() if ln <= 64 else "%d bytes" % ln
                out.append((key, v))
            if tag != 0x1E and r.p - 4 - start != tag:
                raise FormatError("extras size mismatch @0x%x" % start)
        return [{"key": "0x%x" % k + ("(%s)" % EXTRA_KEYS[k] if k in EXTRA_KEYS else ""),
                 "val": v} for k, v in out]

    def attr_value(self, t):
        """Attribute::Serialize (0x10008880) read path: u32 lead, value,
        u32 trailer.  The reader ignores lead/trailer (current writers put 0,
        some older files 1)."""
        r = self.r
        if r.u32() > 0xFFFF:
            raise FormatError("bad attribute lead @0x%x" % (r.p - 4))
        v = self._value(ATTR.get(t, (None, "i32"))[1])
        if r.u32() > 0xFFFF:
            raise FormatError("bad attribute trailer @0x%x" % (r.p - 4))
        return v

    def _value(self, k):
        r = self.r
        if k == "i32":
            return r.i32()
        if k == "u16":
            return r.u16()
        if k == "f32":
            return r.f32()
        if k == "f64":
            return r.f64()
        if k == "u8":
            return r.u8()
        if k == "str":
            return r.string(4096)
        raise FormatError(k)

    def rocparam(self):
        """ROCParam::Serialize (0x1007dc90)."""
        r = self.r
        size = r.i32()
        if size < 0 or size > 0x1000000:
            raise FormatError("bad ROCParam size %d @0x%x" % (size, r.p - 4))
        raw = r.raw(size)
        tmpl = r.string(64)                     # AttrListTemplate name (data type)
        attrs = {}
        while True:
            t = r.i32()
            if t < 0 or t >= 0x2A:
                if t != -1:
                    raise FormatError("bad attribute type %d @0x%x" % (t, r.p - 4))
                break
            attrs[ATTR.get(t, ("attr%d" % t,))[0]] = self.attr_value(t)
        p = {"type": tmpl}
        if size == 4 and tmpl in ("Long", "Int", "Bool", "Short"):
            p["value"] = struct.unpack("<i", raw)[0]
        elif size == 4 and tmpl in ("Float",):
            p["value"] = struct.unpack("<f", raw)[0]
        elif size == 8 and tmpl in ("Double",):
            p["value"] = struct.unpack("<d", raw)[0]
        elif size:
            try:
                s = raw.decode("ascii")
                p["value"] = s.rstrip("\0") if s.isprintable() or s.endswith("\0") else raw.hex()
            except UnicodeDecodeError:
                p["value"] = raw.hex() if size <= 64 else "%d bytes" % size
        if attrs:
            p["attrs"] = attrs
        if size == 0 and tmpl in ARRAY_TEMPLATES:
            n = r.i32()
            p["items"] = [self.rocparam() for _ in range(n)]
        return p

    # -- RIOObject -----------------------------------------------------------
    def rio(self, o):
        """RIOObject::Serialize read path (0x100822f0)."""
        r = self.r
        o.f["id"] = oid = r.u32()
        if oid:
            self.id_log.append(oid)
            self.id_set[oid] = self.id_set.get(oid, 0) + 1
        pep = r.string(64)
        if pep:
            o.f["pep"] = pep
        o.f["name"] = r.string(4096)
        n = r.i32()
        if n < 0 or n > 100000:
            raise FormatError("bad var count %d @0x%x" % (n, r.p - 4))
        vars_ = []
        for _ in range(n):
            flag = r.u32()
            vname = None
            if flag:
                vname = r.string(4096)
            mark = r.u32()
            if mark != MARK_VAR:
                raise FormatError("var marker missing @0x%x" % (r.p - 4))
            cls = self.classname()
            v = self.obj(cls)
            if vname is not None:
                v.f["varname"] = vname
            vars_.append(v)
        if vars_:
            o.f["vars"] = vars_
        x = self.extras()
        if x:
            o.f["extras"] = x

    # -- dispatcher -------------------------------------------------------------
    def obj(self, cls):
        o = Obj(cls, self.r.p)
        fn = CLASS_PARSERS.get(cls)
        if fn is None:
            fn = guess_parser(cls)
        fn(self, o)
        o.end = self.r.p
        return o

    # -- GUI skipping ------------------------------------------------------------
    def _rollback_ids(self, mark):
        for i in self.id_log[mark:]:
            c = self.id_set[i] - 1
            if c:
                self.id_set[i] = c
            else:
                del self.id_set[i]
        del self.id_log[mark:]

    def skip_gui(self, o, cont):
        """Skip a GUI object tree (class name already read); cont() parses
        what follows in the enclosing object.  Passes, in order: strict window
        search, lenient window search (legacy extras tags allowed), and for the
        last GUI object of a file (no anchor after it) a strict then lenient
        scan of the whole tail."""
        start = self.r.p
        err = None
        for strict, mode in ((True, "anchor"), (False, "anchor"), (True, "gap"),
                             (False, "gap"), (True, "tail"), (False, "tail"),
                             (False, "far")):
            try:
                return self._skip_gui(o, cont, start, strict, mode)
            except FormatError as e:
                err = e
        raise err

    def _skip_gui(self, o, cont, start, strict, mode):
        """Memoized: an enclosing object's candidates re-parse the same inner
        GUI objects many times.  Key = (offset, pass, number of ids defined so
        far) - the last element fingerprints the parse context."""
        key = (start, strict, mode, len(self.id_log))
        if key in self.memo:
            cand = self.memo[key]
            if cand is None:
                raise FormatError("cannot resync after GUI object %s @0x%x (memo)" % (o.cls, start))
            ok, q, res, marks = self._try(o, cont, start, cand, strict)
            if ok:
                return self._accept(o, start, cand, res)
        try:
            res = self._skip_gui_search(o, cont, start, strict, mode)
        except FormatError:
            self.memo[key] = None
            raise
        self.memo[key] = start + o.f["gui_bytes"]
        return res

    def _try(self, o, cont, start, cand, strict):
        """Run cont() from cand.  Returns (ok, q, res, marks)."""
        self.r.p = cand
        mark = len(self.cls_log)
        idmark = len(self.id_log)
        old = self.strict
        self.strict = strict
        self.resyncing += 1
        try:
            res = cont()
            return True, self.r.p, res, (mark, idmark)
        except (FormatError, struct.error, IndexError, UnicodeDecodeError,
                RecursionError) as e:
            if self.r.p >= self.deepest[0]:
                self.deepest = (self.r.p, "%s (resync of %s @0x%x)" % (e, o.cls, start))
            self._undo((mark, idmark))
            return False, None, None, None
        finally:
            self.strict = old
            self.resyncing -= 1

    def _undo(self, marks):
        del self.cls_log[marks[0]:]
        self._rollback_ids(marks[1])

    def _accept(self, o, start, cand, res):
        o.f["gui_bytes"] = cand - start
        self.stats["gui_skips"] += 1
        return res

    def _skip_gui_search(self, o, cont, start, strict, mode):
        """Window search: candidates are the offsets up to 96 bytes in front of
        the next ROD*/ROC* class-name record (the anchor; end of data if none).
        Accepted at once: cont() ends exactly at the anchor, or reads the anchor
        as a class name.  Otherwise the candidate leaving the smallest gap of
        small u32 counters before the anchor wins.  Tail mode (no anchor left):
        try the offsets after GOContainer end signatures, then every offset."""
        n = len(self.d)
        m = _ANCHOR_RE.search(self.d, start)
        if mode == "tail":
            if m:
                raise FormatError("tail pass not applicable")
            sig = b"\x0c\x00\x00\x00\x06\x01\x00\x00\x04\x00\x00\x00"
            hits = [mm.start() + 20 for mm in re.finditer(re.escape(sig), self.d[start:])]
            for cand in [start + h for h in reversed(hits[-64:])] + list(range(start, n)):
                ok, q, res, marks = self._try(o, cont, start, cand, strict)
                if ok:
                    return self._accept(o, start, cand, res)
            raise FormatError("cannot resync after last GUI object %s @0x%x" % (o.cls, start))
        tried = 0
        while True:
            anchor = m.start() if m else n
            best = None
            lo = max(start, anchor - 96)
            # a GOContainer tree normally ends with its extras chunk 0x106
            # (tag 0xc, key 0x106, len 4, value, 0): offsets right after such a
            # chunk are tried first and win with any acceptance rule
            def consumed(q, marks):
                return q > anchor and anchor in self.cls_log[marks[0]:]

            def small_gap(q):
                if q > anchor:
                    return None
                gap = self.d[q:anchor]
                if anchor != n and (len(gap) > 96 or len(gap) % 4 or any(
                        v > 0xFFFF for v in struct.unpack("<%dI" % (len(gap) // 4), gap))):
                    return None
                return len(gap)
            for mm in _GO_END_RE.finditer(self.d, lo, anchor):
                cand = mm.end()
                ok, q, res, marks = self._try(o, cont, start, cand, strict)
                if not ok:
                    continue
                if consumed(q, marks) or (mode != "anchor" and small_gap(q) is not None):
                    return self._accept(o, start, cand, res)
                self._undo(marks)
            for cand in range(lo, anchor + 1):
                ok, q, res, marks = self._try(o, cont, start, cand, strict)
                if not ok:
                    continue
                if consumed(q, marks):
                    return self._accept(o, start, cand, res)
                self._undo(marks)
                g = small_gap(q) if mode != "anchor" else None
                if g is None:
                    continue
                key = (g, -cand)
                if best is None or key < best[0]:
                    best = (key, cand)
            if best is not None:
                ok, q, res, marks = self._try(o, cont, start, best[1], strict)
                if ok:
                    return self._accept(o, start, best[1], res)
            tried += 1
            if not m or tried > 64 or mode != "far":
                raise FormatError("cannot resync after GUI object %s @0x%x; deepest error @0x%x: %s"
                                  % (o.cls, start, self.deepest[0], self.deepest[1]))
            m = _ANCHOR_RE.search(self.d, anchor + 1)


ARRAY_TEMPLATES = ("PROCParam",)   # DataType 10 (array of ROCParam)


# --- class parsers --------------------------------------------------------------

def p_riovar(ps, o):
    """RIOVar::Serialize (0x10082fb0)."""
    ps.rio(o)
    r = ps.r
    o.f["varflag"] = r.u32()
    if r.u32():
        o.f["param"] = ps.rocparam()
    x = ps.extras()
    if x:
        o.f["extras2"] = x


def p_rocpad(ps, o):
    """ROCPad::Serialize (0x10083270): RIOVar + u32 pad flags + extras."""
    p_riovar(ps, o)
    fl = ps.r.u32()
    o.f["padflags"] = fl
    if fl & 0xF in (1, 2):                  # 1 = module input, 2 = module output
        o.f["dir"] = "in" if fl & 0xF == 1 else "out"
    x = ps.extras()
    if x:
        o.f["extras3"] = x


def p_rocalgo(ps, o):
    """ROCAlgo::Serialize (0x10097980) = RIOObject only."""
    ps.rio(o)


def p_rocatom(ps, o):
    """ROCAtom::Serialize (0x100bdbe0): RIOObject + module name (= DSP
    ModuleNameLong) + string + long."""
    ps.rio(o)
    o.f["module"] = ps.r.string(4096)
    s2 = ps.r.string(4096)
    if s2:
        o.f["module2"] = s2
    o.f["atomflags"] = ps.r.i32()


def p_rocparameter(ps, o):
    """ROCParameter::Serialize (0x100858d0)."""
    ps.rio(o)
    r = ps.r
    if r.u8():
        o.f["target"] = ps.ref()
    o.f["b1"] = r.u8()
    o.f["b2"] = r.u8()
    o.f["min"] = r.f64()
    o.f["max"] = r.f64()
    o.f["value"] = r.f64()
    o.f["b3"] = r.u8()
    o.f["l1"] = r.i32()
    o.f["l2"] = r.i32()
    o.f["b4"] = r.u8()
    o.f["l3"] = r.i32()
    o.f["b5"] = r.u8()
    fl = r.u32()
    o.f["pflags"] = "0x%08x" % fl
    if fl & 0x80000000:
        o.f["label"] = r.string(4096)
    o.f["raw16"] = r.raw(16).hex()
    o.f["param"] = ps.rocparam()
    x = ps.extras()
    if x:
        o.f["extras2"] = x


def p_rodparameter(ps, o):
    """RODParameter::Serialize (0x10085650)."""
    ps.rio(o)
    r = ps.r
    if r.u8():
        o.f["target"] = ps.ref()
    n = r.u16()
    for _ in range(n):
        o.kids.append(ps.obj(ps.classname()))
    x = ps.extras()
    if x:
        o.f["extras2"] = x


def p_rocparamlist(ps, o):
    """ROCParamList::Serialize (0x10083320)."""
    ps.rio(o)
    r = ps.r
    n = r.i32()
    if n < 0 or n > 100000:
        raise FormatError("bad paramlist count")
    items = []
    for _ in range(n):
        it = {}
        if r.u32():
            it["ref"] = ps.ref()
        ln = r.i32()
        if ln < 0 or ln > 0x100000:
            raise FormatError("bad paramlist item size")
        b = r.raw(ln)
        it["data"] = b.hex() if ln <= 32 else "%d bytes" % ln
        items.append(it)
    o.f["items"] = items
    x = ps.extras()
    if x:
        o.f["extras2"] = x


def rodobject(ps, o, cont):
    """RODObject::Load (0x10087250) followed by cont() (subclass fields)."""
    r = ps.r
    ps.rio(o)
    o.f["rod16"] = r.raw(16).hex()
    n = r.i32()
    if n < 0 or n > 1000:
        raise FormatError("bad view count")
    views = [(r.i32(), r.i32()) for _ in range(n)]
    if views:
        o.f["views"] = views
    gcls = ps.classname()
    if not gcls:
        x = ps.extras()
        if x:
            o.f["extras_rod"] = x
        return cont()
    g = Obj(gcls, r.p)
    o.f["gui"] = g

    def after_gui():
        o.f.pop("extras_rod", None)
        x = ps.extras()
        if x:
            o.f["extras_rod"] = x
        return cont()
    res = ps.skip_gui(g, after_gui)
    g.end = g.off + g.f.get("gui_bytes", 0)
    return res


def p_rodmodule(ps, o):
    """RODModule::Serialize read path (0x100c27c0)."""
    r = ps.r

    def cont():
        o.kids = []
        for k in ("algo", "routings", "modflags", "extras_mod"):
            o.f.pop(k, None)
        algo = r.u32()
        if algo > 0x20:
            raise FormatError("bad algo flag")
        if algo:
            cls = ps.classname()
            if not cls:
                raise FormatError("empty algo class")
            o.f["algo"] = ps.obj(cls)
        n = r.i32()
        if n < 0 or n > 10000:
            raise FormatError("bad child count %d" % n)
        for _ in range(n):
            k = r.u32()
            if k == 0:
                idx = r.i32()
                cls = ps.classname()
                if not cls:
                    raise FormatError("empty child class")
                ch = ps.obj(cls)
                ch.f["index"] = idx
                o.kids.append(ch)
            else:
                o.kids.append(p_refinfo(ps, k))
        n = r.i32()
        if n < 0 or n > 10000:
            raise FormatError("bad pad count %d" % n)
        for _ in range(n):
            r.u32()
            cls = ps.classname()
            if cls != "RODPad" and not cls.startswith("ROD"):
                raise FormatError("expected RODPad, got %r" % cls)
            o.kids.append(ps.obj(cls))
        if algo > 1:
            o.f["modflags"] = r.u32()
        n = r.i32()
        if n < 0 or n > 10000:
            raise FormatError("bad param count %d" % n)
        for _ in range(n):
            r.u32()
            cls = ps.classname()
            if not cls:
                raise FormatError("empty param class")
            po = ps.obj(cls)
            if r.u32():
                po.f["main"] = True
            o.kids.append(po)
        n = r.i32()
        if n < 0 or n > 100000:
            raise FormatError("bad routing count %d" % n)
        routes = []
        for _ in range(n):
            cls = ps.classname()
            if not cls.startswith("ROD"):
                raise FormatError("expected routing class, got %r" % cls)
            routes.append(ps.obj(cls))
        if routes:
            o.f["routings"] = routes
        x = ps.extras()
        if x:
            o.f["extras_mod"] = x
        return True
    rodobject(ps, o, cont)


def p_refinfo(ps, k):
    """RODModule::SerializeRefInformation (0x100bee90): a child module that
    is a reference to another file (e.g. a .dev inside a project)."""
    r = ps.r
    o = Obj("<RefModule>", r.p)
    o.f["k"] = k
    ver = r.u32()
    size = r.u32()
    blob = r.raw(size)
    o.f["blob"] = size
    if ver == 1 and size:
        sub = Parser(blob)
        sub.r.p = 0
        try:
            o.f["path"] = sub.r.string(4096)
            o.f["l"] = sub.r.i32()
            cls = sub.classname()
            o.f["restore"] = sub.obj(cls) if cls else None
            o.f["pos"] = struct.unpack_from("<ii", blob, sub.r.p)
            sub.r.p += 8
            n = sub.r.i32()
            extra = []
            for _ in range(n):
                extra.append(sub.obj(sub.classname()))
            if extra:
                o.f["params"] = extra
        except (FormatError, struct.error) as e:
            o.f["blob_error"] = str(e)
    o.end = r.p
    return o


def p_rodpad(ps, o):
    """RODPad::Serialize (0x100b6bf0)."""
    r = ps.r

    def cont():
        for k in ("link", "linkmode", "extras_pad"):
            o.f.pop(k, None)
        link = r.i32()
        if link != -1:
            if link < 1 or link > 17:
                raise FormatError("bad pad link")
            o.f["link"] = ps.ref()           # -> ROCPad of the module's atom
            o.f["linkmode"] = link
            if ps.resyncing and not ps.known_ref(o.f["link"]):
                raise FormatError("pad link to unknown id")
        fl = r.u8()
        o.f["padbyte"] = fl
        x = ps.extras()
        if x:
            o.f["extras_pad"] = x
        return True
    rodobject(ps, o, cont)


def p_rodrouting(ps, o):
    """RODRouting::SerializeRouting (0x100ae620): RODObject, 0, 0,
    ref(source RODPad), ref(dest RODPad), extras."""
    r = ps.r

    def cont():
        o.f.pop("extras_route", None)
        a = r.u32()
        b = r.u32()
        if a > 16 or b > 16:
            raise FormatError("bad routing lead")
        o.f["from"] = ps.ref()
        o.f["to"] = ps.ref()
        if not o.f["from"] or not o.f["to"]:
            raise FormatError("routing without endpoints")
        if ps.resyncing and not (ps.known_ref(o.f["from"]) and ps.known_ref(o.f["to"])):
            raise FormatError("routing to unknown ids")
        x = ps.extras()
        if x:
            o.f["extras_route"] = x
        return True
    rodobject(ps, o, cont)


def p_unknown(ps, o):
    """Unknown non-GUI class: decode the RIOObject header, then give up."""
    ps.rio(o)
    raise FormatError("no parser for class %s @0x%x" % (o.cls, o.off))


CLASS_PARSERS = {
    "RODModule": p_rodmodule, "RODBase": p_rodmodule,
    "RODPad": p_rodpad,
    "RODRouting": p_rodrouting,
    "RODParameter": p_rodparameter,
    "ROCAtom": p_rocatom,
    "ROCAlgo": p_rocalgo,
    "ROCPad": p_rocpad, "ROCAtomPad": p_rocpad,
    "ROCParameter": p_rocparameter,
    "ROCParamList": p_rocparamlist,
    "RIOVar": p_riovar,
}


def guess_parser(cls):
    if cls.startswith("ROD") and ("Module" in cls or "Base" in cls):
        return p_rodmodule
    if cls.endswith("Pad") and cls.startswith("ROC"):
        return p_rocpad
    if cls.startswith("ROC") and "Param" in cls:
        return p_rocparameter
    return p_unknown


# ---------------------------------------------------------------------------
# 4. file level
# ---------------------------------------------------------------------------

HDR_MAGIC = b"Creamware Scope File"


def parse_header(d):
    """ScopeFileHeader::Serialize (0x1002f450): 0x28 bytes magic text, 14 x
    u32, 0x1c bytes, u32 checksum."""
    if len(d) < 0x80:
        raise FormatError("file too short")
    magic = d[:0x28].split(b"\0")[0]
    vals = struct.unpack_from("<14I", d, 0x28)
    chk = struct.unpack_from("<I", d, 0x7C)[0]
    # CalculateChecksum (0x100083c0) over the 0x7c bytes
    s = x = 0
    for b in d[:0x7C]:
        s = (s + b) & 0xFFFFFFFF
        x ^= b
        x = ((x << 1) | (x >> 31)) & 0xFFFFFFFF
    calc = (((x ^ s) << 1) | ((x ^ s) >> 31)) & 0xFFFFFFFF
    return {"magic": magic.decode("latin-1", "replace"),
            "version": "0x%x" % vals[0], "fields": ["0x%x" % v for v in vals[1:]],
            "checksum_ok": chk == calc or chk == 0}


def is_object_archive(plain):
    return plain[:20] in (b"Creamware Scope File", b"SONICCORE SCOPE File")


def load(path):
    with open(path, "rb") as f:
        raw = f.read()
    plain, info = unpack_container(raw)
    return plain, info


def parse_plain(plain, keep_gui=False):
    hdr = parse_header(plain)
    ps = Parser(plain, keep_gui)
    ps.r.p = 0x80
    cls = ps.classname()
    root = ps.obj(cls)
    # RODModule::SaveReferenceParams (0x10076260): per-variable restore
    # records (vtable 0x1a0, not decoded) terminated by u32 0xfffffffd
    tail = len(plain) - ps.r.p
    if tail >= 4 and plain[-4:] == b"\xfd\xff\xff\xff":
        ps.stats["refparams_bytes"] = tail - 4
        tail = 0
    return hdr, root, tail, ps


# ---------------------------------------------------------------------------
# 5. output
# ---------------------------------------------------------------------------

def fmt_ref(ids):
    return "->" + ".".join("%x" % i for i in ids) if ids else "->none"


def ref_name(ref, ids):
    """Name of a referenced object: single ids are local; a path a.b... goes
    through module a (often a RefModule loaded from another file)."""
    if not ref:
        return None
    if len(ref) == 1:
        return ids.get(ref[0])
    head = ids.get(ref[0])
    return "%s / id %x" % (head or "module %x" % ref[0], ref[-1])


def dump(o, ids, out, ind=0, gui=False):
    pad = "  " * ind
    if isinstance(o, dict):
        out.write("%s%s\n" % (pad, o))
        return
    f = o.f
    head = "%s%s" % (pad, o.cls)
    if "id" in f:
        head += " #%x" % f["id"]
    if f.get("name"):
        head += " %r" % f["name"]
    if f.get("pep"):
        head += " pep=%s" % f["pep"]
    extra = []
    for k in ("module", "module2", "atomflags", "dir", "padflags", "linkmode", "index",
              "varname", "varflag", "padbyte", "modflags", "path", "label", "min",
              "max", "value", "pflags", "main"):
        if k in f:
            extra.append("%s=%s" % (k, f[k]))
    for k in ("link", "target", "from", "to"):
        if k in f:
            v = f[k]
            nm = ref_name(v, ids)
            extra.append("%s=%s%s" % (k, fmt_ref(v), "(%s)" % nm if nm else ""))
    out.write("%s  [@0x%x] %s\n" % (head, o.off, " ".join(extra)))
    if "param" in f and f["param"]:
        out.write("%s    param: %s\n" % (pad, f["param"]))
    for k in ("extras", "extras2", "extras3", "extras_rod", "extras_mod", "extras_pad"):
        if f.get(k):
            out.write("%s    %s: %s\n" % (pad, k, f[k]))
    if "gui" in f and gui:
        g = f["gui"]
        out.write("%s    gui: %s (%d bytes skipped)\n" % (pad, g.cls, g.f.get("gui_bytes", 0)))
    if f.get("restore") is not None:
        out.write("%s    restore:\n" % pad)
        dump(f["restore"], ids, out, ind + 3, gui)
    for v in f.get("vars", []):
        dump(v, ids, out, ind + 2, gui)
    if f.get("algo") is not None:
        out.write("%s  algo:\n" % pad)
        dump(f["algo"], ids, out, ind + 2, gui)
    for k in o.kids:
        dump(k, ids, out, ind + 1, gui)
    for k in f.get("params", []):
        dump(k, ids, out, ind + 2, gui)
    for rt in f.get("routings", []):
        dump(rt, ids, out, ind + 1, gui)


def collect_ids(o, ids, parent_chain=()):
    if not isinstance(o, Obj):
        return
    name = o.f.get("name") or o.cls
    if "id" in o.f and o.f["id"]:
        ids[o.f["id"]] = "%s %s" % (o.cls, name)
    subs = list(o.f.get("vars", [])) + list(o.kids) + list(o.f.get("routings", []))
    if o.f.get("algo") is not None:
        subs.append(o.f["algo"])
    for s in subs:
        collect_ids(s, ids)


def dsp_name_index(dspdir):
    """Map seg_name module names (short and long) -> DSP file names, using
    sc_decode.py from the same directory (Sim2k matches atoms by these names,
    FUN_10c03390)."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import sc_decode
    idx = {}
    for f in sorted(os.listdir(dspdir)):
        if not f.lower().endswith((".dsp", ".ol")):
            continue
        try:
            obj, _ = sc_decode.load(os.path.join(dspdir, f))
        except Exception:
            continue
        sec = {x.name: x for x in obj.sections}
        if "seg_name" not in sec:
            continue
        strs, cur = [], ""
        for w in obj.words(sec["seg_name"]):
            c = (w >> 8) & 0xFF
            if c:
                cur += chr(c)
            else:
                strs.append(cur)
                cur = ""
        for nm in strs[:2]:
            idx.setdefault(nm, []).append(f)
    return idx


DSP_INDEX = {}


def summary(o, ids, out, path=""):
    if not isinstance(o, Obj):
        return
    f = o.f
    me = path + "/" + (f.get("name") or o.cls)
    if o.cls.startswith("ROD") and o.cls not in ("RODPad", "RODRouting", "RODParameter"):
        a = f.get("algo")
        dspf = ""
        if a is not None and a.cls == "ROCAtom" and DSP_INDEX:
            dspf = " -> %s" % ",".join(DSP_INDEX.get(a.f.get("module"), ["?"]))
        out.write("MODULE %s  (%s #%x)%s%s\n" % (me, o.cls, f.get("id", 0),
                  "  atom=%s module=%r" % (a.cls, a.f.get("module")) if a is not None else "", dspf))
        if a is not None:
            for v in a.f.get("vars", []):
                out.write("   ATOMPAD %-10s #%x dir=%s type=%s\n" % (
                    v.f.get("name"), v.f.get("id", 0), v.f.get("dir"),
                    (v.f.get("param") or {}).get("type")))
    if o.cls == "RODPad":
        lk = f.get("link")
        nm = ref_name(lk, ids)
        out.write("   PAD %s #%x link=%s%s\n" % (me, f.get("id", 0), fmt_ref(lk),
                  " (%s)" % nm if nm else ""))
    if o.cls == "<RefModule>":
        out.write("REF %s path=%r\n" % (me, f.get("path")))
    for k in o.kids:
        summary(k, ids, out, me)
    for rt in f.get("routings", []):
        a, b = rt.f.get("from"), rt.f.get("to")
        na, nb = ref_name(a, ids), ref_name(b, ids)
        out.write("ROUTE in %s: %s%s => %s%s\n" % (
            me, fmt_ref(a), " (%s)" % na if na else "", fmt_ref(b), " (%s)" % nb if nb else ""))


def iter_files(paths):
    for p in paths:
        if os.path.isdir(p):
            for root, _, fs in os.walk(p):
                for f in sorted(fs):
                    yield os.path.join(root, f)
        else:
            yield p


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--plain", metavar="OUT", help="write decompressed archive of FILE")
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--gui", action="store_true")
    ap.add_argument("--dsp", metavar="DIR", help="with --summary: map atoms to DSP files in DIR (App/Dsp)")
    a = ap.parse_args()
    sys.setrecursionlimit(20000)
    if a.dsp:
        DSP_INDEX.update(dsp_name_index(a.dsp))
    if a.plain:
        plain, info = load(a.files[0])
        with open(a.plain, "wb") as f:
            f.write(plain)
        print("%s: %s, %d bytes" % (a.files[0], info, len(plain)))
        return 0
    if a.check:
        ok = bad = 0
        for p in iter_files(a.files):
            try:
                plain, info = load(p)
                if not is_object_archive(plain):
                    # e.g. .pre presets ("Creamware Scope technology preset
                    # file"): container verified, payload not decoded
                    print("CONTAINER-ONLY %s: %s" % (p, plain[4:44].split(b"\0")[0].decode("latin-1")))
                    ok += 1
                    continue
                hdr, root, tail, ps = parse_plain(plain)
                if tail:
                    raise FormatError("%d trailing bytes" % tail)
                ok += 1
            except Exception as e:      # report and continue
                bad += 1
                print("FAIL %s: %s" % (p, e))
        print("ok %d, failed %d" % (ok, bad))
        return 0 if bad == 0 else 1
    for p in iter_files(a.files):
        plain, info = load(p)
        if not is_object_archive(plain):
            print("== %s  (%s, %d bytes plain): not an object archive (%r...), use --plain"
                  % (p, info.get("container"), len(plain), plain[:44]))
            continue
        hdr, root, tail, ps = parse_plain(plain, a.gui)
        if a.json:
            json.dump({"file": p, "container": info, "header": hdr, "root": root.to_json(),
                       "trailing": tail}, sys.stdout, indent=1, default=str)
            print()
            continue
        ids = {}
        collect_ids(root, ids)
        print("== %s  (%s, %d bytes plain, header %s v%s, %d GUI blobs skipped, %d trailing)"
              % (p, info.get("container"), len(plain), hdr["magic"].split("\r")[0], hdr["version"],
                 ps.stats["gui_skips"], tail))
        if a.summary:
            summary(root, ids, sys.stdout)
        else:
            dump(root, ids, sys.stdout, gui=a.gui)
    return 0


if __name__ == "__main__":
    sys.exit(main())
