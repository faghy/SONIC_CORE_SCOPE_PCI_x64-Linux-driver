#!/usr/bin/env python3
"""
pulsar_delay.py - SCOPE's "PC delay atoms" on Linux (docs/pc_delay.md).

SCOPE implements every long delay line in host RAM: Sim2k atoms "PC Master 4k/32k Delay", "PC 256k Delay" and
"PC Early Reflection" own slot descriptors, and the card's slot engine moves the samples (scScope.sys slot
types 0xE..0x17). On Linux the kernel side is pulsar_delay.c (PULSAR_IOCTL_DELAY_*); this module holds

  * ATOMS: the atom table (name -> kernel kind, ring, taps, pad layout), used by scope_device.py,
  * pad_role(): what an atom input pad is (signal in, delay of tap k, ER gain k, ER tap count),
  * the ioctl wrappers delay_alloc / delay_free / delay_param / supported,
  * HostWords: allocator for the BAR SRAM dwords that receive DSP async outputs (delay times sent by
    DLEXTM0/DLEXTM1/DLINTL5 "DT"), and export_to_host() / unexport_host() for the DSP side,
  * PcDelay: one instantiated atom (used by pulsard for SCOPE devices).

Python 3 standard library only. Nothing here touches the hardware except through the hwdep ioctls.
"""
import struct

# ---------------------------------------------------------------------------------------------------------
# atom table.  Sim2k class names / file names from Sim2k.dll strings (.\atoms\pc_delay*.cpp) [C]
# kind = PULSAR_DELAY_* of pulsar_uapi.h
# ---------------------------------------------------------------------------------------------------------
KIND_4K, KIND_32K, KIND_256K, KIND_ER = 0, 1, 2, 3
KIND_NAMES = {KIND_4K: "4k", KIND_32K: "32k", KIND_256K: "256k", KIND_ER: "er"}

LAT = 0x82                 # engine round trip DSP -> host ring -> DSP (scScope.sys FUN_180010440) [C]
MIN_DELAY = 0xC2           # Sim2k clamp (FUN_10c5efe0 / FUN_10c5f5a0 / FUN_10c5eb10) [C]
MAX_DELAY = {KIND_4K: 0x1000, KIND_32K: 0x8000, KIND_256K: 0x3FC00, KIND_ER: 0x10881}
ER_TAPS = 16


def _spec(kind, taps, sim2k, slot_types):
    return {"kind": kind, "type": KIND_NAMES[kind], "taps": taps, "sim2k": sim2k, "slot_types": slot_types,
            "min_delay": MIN_DELAY, "max_delay": MAX_DELAY[kind]}


ATOMS = {
    "PC Master 4k Delay": _spec(KIND_4K, 8, "pc_delay4k", (0xE, 0x11)),
    "PC Master 4k Delay Float": _spec(KIND_4K, 8, "pc_delay4k_flt", (0xE, 0x11)),
    "PC Master 4k Delay Float 1 Tap": _spec(KIND_4K, 1, "pc_delay4k_flt_1tap", (0xE, 0x11)),
    "PC Master 32k Delay": _spec(KIND_32K, 8, "pc_delay32k", (0xF, 0x12)),
    "PC Master 32k Delay Float": _spec(KIND_32K, 8, "pc_delay32k_flt", (0xF, 0x12)),
    "PC Master 32k Delay Float 1 Tap": _spec(KIND_32K, 1, "pc_delay32k_flt_1tap", (0xF, 0x12)),
    "PC 256k Delay": _spec(KIND_256K, 1, "pc_delay256k", (0x10, 0x13)),
    "PC Early Reflection": _spec(KIND_ER, 1, "pc_erDelay", (0x16, 0x17)),
}


def atom_spec(name):
    """Spec dict for a host delay atom name (as stored in the .dev ROCAtom), else None."""
    if not name:
        return None
    s = ATOMS.get(name.strip())
    if s is None:
        for k, v in ATOMS.items():          # Sim2k also accepts the file base name (pc_delay32k.pc ...)
            if name.strip().lower() in (v["sim2k"] + ".pc", v["sim2k"]):
                s = ATOMS[k]
    return dict(s) if s else None


def pad_role(spec, i):
    """Role of atom input pad i: ("in",), ("delay", k), ("gain", k) or ("ntaps",).
    4k/32k: In, Del1..Del8; 256k: in, Del; ER: In, Del1..16, Gai1..16, Taps [C, Sim2k pad name functions]."""
    if i == 0:
        return ("in",)
    if spec["kind"] == KIND_ER:
        if i <= ER_TAPS:
            return ("delay", i - 1)
        if i <= 2 * ER_TAPS:
            return ("gain", i - 1 - ER_TAPS)
        return ("ntaps",)
    return ("delay", i - 1)


# ---------------------------------------------------------------------------------------------------------
# kernel interface (pulsar_uapi.h)
# ---------------------------------------------------------------------------------------------------------
P_DELAY, P_SOURCE, P_GAIN, P_NTAPS = 0, 1, 2, 3
_ALLOC_FMT = "<III16H16iI3I"                  # struct pulsar_delay_alloc
_PARAM_FMT = "<IIIi"                          # struct pulsar_delay_param


def _ioc(dirbits, nr, size):
    return (dirbits << 30) | (size << 16) | (ord("P") << 8) | nr


IOCTL_ALLOC = _ioc(3, 0x10, struct.calcsize(_ALLOC_FMT))
IOCTL_FREE = _ioc(1, 0x11, 4)
IOCTL_PARAM = _ioc(1, 0x12, struct.calcsize(_PARAM_FMT))


class DelayError(Exception):
    pass


def _fd(bar):
    fd = getattr(bar, "fd", None)
    return fd if isinstance(fd, int) else None


def supported(bar):
    """True if the loaded snd-pulsar has the delay ioctls (FREE of handle 0 -> ENOENT, old module -> ENOTTY)."""
    import errno
    import fcntl
    fd = _fd(bar)
    if fd is None:
        return True                           # simulated board
    try:
        fcntl.ioctl(fd, IOCTL_FREE, struct.pack("<I", 0))
    except OSError as e:
        return e.errno == errno.ENOENT
    return True


_SIM = {"next": 0, "slots": set()}


def delay_alloc(bar, kind, write_slot, ntaps, delays=(), tap_slots=()):
    """PULSAR_IOCTL_DELAY_ALLOC -> (handle, [tap slots])."""
    import fcntl
    d = [int(x) for x in delays] + [0] * (16 - len(delays))
    t = [int(x) for x in tap_slots] + [0] * (16 - len(tap_slots))
    buf = bytearray(struct.pack(_ALLOC_FMT, kind, write_slot, ntaps, *t, *d, 0, 0, 0, 0))
    fd = _fd(bar)
    if fd is None:                            # dry run: emulate the kernel's slot pick
        _SIM["next"] += 1
        slots = []
        for k in range(ntaps):
            s = t[k] or next(x for x in range(0x182, 0x200) if x not in _SIM["slots"] and x not in slots)
            slots.append(s)
        _SIM["slots"].update(slots)
        return _SIM["next"], slots
    try:
        fcntl.ioctl(fd, IOCTL_ALLOC, buf, True)
    except OSError as e:
        raise DelayError("PULSAR_IOCTL_DELAY_ALLOC failed: %s" % e)
    v = struct.unpack(_ALLOC_FMT, bytes(buf))
    return v[35], list(v[3:3 + ntaps])


def delay_free(bar, handle, slots=()):
    import fcntl
    fd = _fd(bar)
    if fd is None:
        _SIM["slots"].difference_update(slots)
        return
    try:
        fcntl.ioctl(fd, IOCTL_FREE, struct.pack("<I", handle))
    except OSError as e:
        raise DelayError("PULSAR_IOCTL_DELAY_FREE failed: %s" % e)


def delay_param(bar, handle, param, index, value):
    import fcntl
    fd = _fd(bar)
    v = int(value)
    v = v - (1 << 32) if v >= 0x80000000 else v
    if fd is None:
        return
    try:
        fcntl.ioctl(fd, IOCTL_PARAM, struct.pack(_PARAM_FMT, handle, param, index, v))
    except OSError as e:
        raise DelayError("PULSAR_IOCTL_DELAY_PARAM failed: %s" % e)


# ---------------------------------------------------------------------------------------------------------
# DSP async output -> host dword (Sim2k FUN_10c1a9e0 pool, os_sendmsgPX2 host header) [C/L]
# ---------------------------------------------------------------------------------------------------------
HOST_HDR = 0x63E00000         # host destination header (dspAckDest = 0x63E00800 | 2n, verified on hardware)


class HostWords:
    """BAR SRAM dwords for DSP->PC async values: BAR + 0x80000 + 4*w. Sim2k's pool is 0x800..0x1FFF with the
    sysmsg acks at 0x800+2n and the uC mailbox at 0x80B; we stay in 0x1000..0x1FFF [L]."""

    def __init__(self, lo=0x1000, hi=0x2000):
        self.free = list(range(lo, hi))

    def alloc(self):
        if not self.free:
            raise DelayError("no DSP->PC dword left")
        return self.free.pop(0)

    def release(self, w):
        if w not in self.free:
            self.free.insert(0, w)


def export_to_host(rack, src, k, word):
    """Make async output k of DSP module `src` also send its value to host dword `word` (export header list,
    like Rack._export_async for a DSP receiver). Returns ops."""
    sd = rack.dsp[src.dsp]
    lst = src.exports.setdefault(k, [])
    hdr = HOST_HDR | (word & 0x1FFFF)
    if hdr not in lst:
        lst.append(hdr)
    return _write_exports(sd, src, k)


def unexport_host(rack, src, k, word):
    sd = rack.dsp[src.dsp]
    lst = src.exports.get(k, [])
    hdr = HOST_HDR | (word & 0x1FFFF)
    if hdr in lst:
        lst.remove(hdr)
    return _write_exports(sd, src, k)


def _write_exports(sd, src, k):
    lst = src.exports.get(k, [])
    old = src.export_list_addr.get(k, -1)
    if old >= 0:
        sd.dm.free(old)
        src.export_list_addr.pop(k, None)
    word_addr = src.seg_mod + src.cls.off_async_out(k) + 1
    if not lst:
        return [("set", sd.dspno, word_addr, 0, "%s: async out %d -> no destinations" % (src.name, k))]
    la = sd.dm.alloc(len(lst))
    src.export_list_addr[k] = la
    return [("data", sd.dspno, la, list(lst), "%s: async out %d export list" % (src.name, k)),
            ("set", sd.dspno, word_addr, (len(lst) << 20) | la,
             "%s: async out %d -> %d destinations" % (src.name, k, len(lst)))]


# ---------------------------------------------------------------------------------------------------------
# one instance
# ---------------------------------------------------------------------------------------------------------

class PcDelay:
    """An instantiated host delay atom. `spec` from atom_spec(); slots/handle from the kernel."""

    def __init__(self, spec, name):
        self.spec, self.name = spec, name
        self.handle = None
        self.write_slot = None
        self.tap_slots = []
        self.delays = [MIN_DELAY] * ER_TAPS
        self.gains = [0] * ER_TAPS
        self.ntaps_er = ER_TAPS
        self.sources = {}                    # pad index -> (src module, async out k, host word)

    # pads, numbered like the atom (inputs first; outputs = taps)
    def inputs(self):
        if self.spec["kind"] == KIND_ER:
            names = ["In"] + ["Del%d" % i for i in range(1, 17)] + ["Gai%d" % i for i in range(1, 17)] + ["Taps"]
        elif self.spec["kind"] == KIND_256K:
            names = ["in", "Del"]
        else:
            names = ["In"] + ["Del%d" % i for i in range(1, self.spec["taps"] + 1)]
        return [{"index": i, "name": n, "sync": i == 0} for i, n in enumerate(names)]

    def outputs(self):
        if self.spec["kind"] in (KIND_ER, KIND_256K):
            return [{"index": 0, "name": "out", "sync": True}]
        return [{"index": k, "name": "Tap%d" % (k + 1), "sync": True} for k in range(self.spec["taps"])]

    def tap_addr(self, k):
        """DM address a DSP input reads tap k from (the PC window, broadcast to every DSP)."""
        if k >= len(self.tap_slots):
            raise DelayError("%s: tap %d not allocated" % (self.name, k))
        return 0xC000 + 2 * self.tap_slots[k]

    def allocate(self, bar, write_addr, ntaps):
        """write_addr = DM comm slot (0xC0xx) the source's sync output was moved to."""
        self.write_slot = (write_addr - 0xC000) // 2
        n = 1 if self.spec["kind"] in (KIND_ER, KIND_256K) else max(1, ntaps)
        self.handle, self.tap_slots = delay_alloc(bar, self.spec["kind"], self.write_slot, n, self.delays)
        if self.spec["kind"] == KIND_ER:
            for k in range(ER_TAPS):
                delay_param(bar, self.handle, P_GAIN, k, self.gains[k])
            delay_param(bar, self.handle, P_NTAPS, 0, self.ntaps_er)
        return self.tap_slots

    def set_pad(self, bar, i, value):
        """Host value on input pad i (raw word, i.e. after the unit-2 fs/48000 scaling)."""
        role = pad_role(self.spec, i)
        v = int(value) & 0xFFFFFFFF
        v = v - (1 << 32) if v & 0x80000000 else v
        if role[0] == "in":
            raise DelayError("%s: the signal input cannot take a value" % self.name)
        if role[0] == "delay":
            self.delays[role[1]] = v
            p = (P_DELAY, role[1])
        elif role[0] == "gain":
            self.gains[role[1]] = v
            p = (P_GAIN, role[1])
        else:
            self.ntaps_er = max(0, min(ER_TAPS, v))
            p = (P_NTAPS, 0)
        if self.handle is not None:
            if role[0] == "delay" and i in self.sources:
                return
            delay_param(bar, self.handle, p[0], p[1], v)

    def set_source(self, bar, i, word):
        """Delay pad i fed by a DSP async output that sends to host dword `word` (None = host value again)."""
        role = pad_role(self.spec, i)
        if role[0] != "delay" or self.spec["kind"] == KIND_ER:
            raise DelayError("%s: pad %d cannot be driven by a DSP output" % (self.name, i))
        if self.handle is not None:
            delay_param(bar, self.handle, P_SOURCE, role[1], word or 0)
            if not word:
                delay_param(bar, self.handle, P_DELAY, role[1], self.delays[role[1]])

    def free(self, bar):
        if self.handle is not None:
            delay_free(bar, self.handle, self.tap_slots)
        self.handle = None


if __name__ == "__main__":
    import sys
    for n, s in ATOMS.items():
        print("%-34s kind=%-4s taps=%d slot types 0x%x/0x%x delay %d..%d" % (
            n, s["type"], s["taps"], s["slot_types"][0], s["slot_types"][1], s["min_delay"], s["max_delay"]))
    print("ioctl alloc=0x%08x free=0x%08x param=0x%08x" % (IOCTL_ALLOC, IOCTL_FREE, IOCTL_PARAM))
    sys.exit(0)
