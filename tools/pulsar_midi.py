#!/usr/bin/env python3
"""
pulsar_midi.py - MIDI for the Pulsar II on Linux: an ALSA sequencer client ("Pulsar2 MIDI") that feeds MIDI
from any ALSA/PipeWire application (Ardour, a keyboard, aseqdump ...) into the DSP modules' MIDI pads, plus
the DSP-side helpers that SCOPE's synths need (MIDI source FIFO, SetVoices, PROCParam tables).

Spec and evidence: docs/midi_synths.md.  Python 3 standard library only (libasound via ctypes).
This file never touches the card by itself: the delivery class takes a `writer` (set_value / get_value of a
pulsar_loader.Board, called under pulsard's board lock), and the DSP helpers only return pulsar_modules ops.

How SCOPE does it (Windows, docs/midi_synths.md §1):
  * A MIDI message travels as ONE 32-bit word `status<<16 | data1<<8 | data2` (scScope.sys MiniMidi
    FUN_18000bed0 @0x18000bed0).  On a MIDI pad (type 0xE) a NEW message is a CHANGE of the output word: the
    sender toggles bit 31 on every message (SNC2MIDI 'sendR0', sendMidiMsg mailbox mode), receivers compare
    with the last word they saw (M_V_M16E MIDI_SCHL, pc2midi, M_C_MOD ...), once per async pass.
  * PC -> DSP: host atom "MIDI Output Device" (Sim2k midiOut_module) + DSP module SNC2MIDI.dsp ("Sync to
    MIDI").  The host writes words into a 32-entry FIFO in DSP memory and then the write position
    (scScope.sys FUN_1800184d0 @0x1800184d0: SetValue(fifo + wr, msg); SetValue(FPwr, wr+1)); SNC2MIDI pops
    one word every second async pass and sends it on its MIDI output with the bit-31 toggle.

Usage:
  pulsar_midi.py encode 90 3c 64 b0 07 7f        bytes -> SCOPE MIDI words
  pulsar_midi.py listen [--seconds N] [--connect CLIENT:PORT]
                                                 ALSA client "Pulsar2 MIDI", print the words it would send
  pulsar_midi.py selftest                        packer tests + ALSA loopback (own sender port -> own client)
  pulsar_midi.py ops [--dsp 1] [--voices 4]      linker ops (dry run) for SNC2MIDI + MVC Easy 16 test synth
  pulsar_midi.py voices FILE.dev [--voices 4]    voice-expanded plan of a SCOPE synth (scope_device.voice_plan)
"""

import argparse
import ctypes
import ctypes.util
import os
import select
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

DEFAULT_DSP_DIR = "/var/lib/snd-pulsar/dsp"

# ============================================================================ SCOPE MIDI words

FIFO_LEN = 32                   # SNC2MIDI: read index masked with 0x1F; midiOut_module descriptor +0xe6 = 0x20
TOGGLE = 0x80000000             # bit 31 toggled per message by the sender of a MIDI pad


def _msg_len(status):
    """Data bytes after a status byte (scScope.sys tables DAT_180023360 / DAT_180023368)."""
    if status < 0xF0:
        return 1 if (status & 0xF0) in (0xC0, 0xD0) else 2
    return {0xF1: 1, 0xF2: 2, 0xF3: 1}.get(status, 0)


def word(status, d1=0, d2=0):
    return ((status & 0xFF) << 16) | ((d1 & 0xFF) << 8) | (d2 & 0xFF)     # F7 may sit in a data byte (SysEx end)


class MidiPacker:
    """Raw MIDI bytes -> SCOPE MIDI words, as scScope.sys modWriteLongBufferData (FUN_18000bf40):
    channel messages and F1/F2/F3 as one word each (running status expanded), realtime bytes F8..FF as
    `byte<<16` (also in the middle of other messages), SysEx as words `F0 d1 d2`, the last one carrying the
    F7 in the first free byte (`F0 d F7` or `F0 F7 00`)."""

    def __init__(self):
        self.running = 0
        self.cur = []               # bytes of the message being assembled (status first)
        self.need = 0
        self.sysex = False

    def feed(self, data):
        out = []
        for b in bytes(data):
            if b >= 0xF8:                                   # realtime: never interrupts the parser state
                out.append(word(b))
                continue
            if self.sysex:
                if b < 0x80:
                    self.cur.append(b)
                    if len(self.cur) == 3:
                        out.append(word(*self.cur))
                        self.cur = [0xF0]
                    continue
                self.cur.append(0xF7)                         # F7 (or any status) ends the SysEx
                out.append(word(*(self.cur + [0, 0])[:3]))
                self.sysex, self.cur = False, []
                if b == 0xF7:
                    continue
            if b >= 0x80:
                if b == 0xF0:
                    self.sysex, self.cur, self.running = True, [0xF0], 0
                    continue
                if b == 0xF7:
                    continue                                  # stray end of SysEx
                self.cur, self.need = [b], _msg_len(b)
                self.running = b if b < 0xF0 else 0
                if self.need == 0:
                    out.append(word(b))
                    self.cur = []
                continue
            if not self.cur:                                  # data byte: running status
                if not self.running:
                    continue                                  # bad data, dropped (kernel prints "bad data")
                self.cur, self.need = [self.running], _msg_len(self.running)
            self.cur.append(b)
            if len(self.cur) == self.need + 1:
                out.append(word(*(self.cur + [0, 0])[:3]))
                self.cur = []
        return out


def unpack(words):
    """SCOPE MIDI words -> raw bytes (the capture direction, scScope.sys FUN_18000c4a0 @0x18000c4a0)."""
    out, sysex = bytearray(), False
    for w in words:
        w &= 0x7FFFFFFF
        s, d1, d2 = (w >> 16) & 0xFF, (w >> 8) & 0xFF, w & 0xFF
        if s >= 0xF8:
            out.append(s)
            continue
        if s == 0xF0:
            if not sysex:
                out.append(0xF0)
                sysex = True
            for d in (d1, d2):
                if d < 0xF0:
                    out.append(d)
                else:
                    out.append(0xF7)
                    sysex = False
                    break
            continue
        if sysex:
            out.append(0xF7)
            sysex = False
        if s < 0x80:
            continue
        out.append(s)
        n = _msg_len(s)
        if n > 0:
            out.append(d1)
        if n > 1:
            out.append(d2)
    return bytes(out)


# ============================================================================ delivery into the DSP FIFO

class ScopeMidiFifo:
    """Host side of SNC2MIDI's FIFO mode (Windows: scScope.sys FUN_1800184d0 fed by sendMidiMsg).

    writer: object with set_value(dsp, addr, value) and optionally get_value(dsp, addr) - a pulsar_loader.Board;
            calls are made with `lock` held when one is given (pulsard: its board lock).
    info:   dict from midi_source_ops(): dsp, fifo (DM address of the 32 words), wpos (value slot of input 3),
            rpos (DM address of async output 1 = read position).
    The DSP pops one word every second async pass (fs / (2 * asRatio) words/s, 1600/s at 48 kHz); `drain`
    is a conservative estimate used when the read position cannot be read back."""

    def __init__(self, writer, info, lock=None, drain=1000.0):
        self.w, self.info, self.lock = writer, info, lock
        self.wr = 0                     # next FIFO index the host writes (SNC2MIDI starts with rd = 0)
        self.fill = 0.0                 # estimated words not yet popped
        self.t = time.monotonic()
        self.drain = drain
        self.sent = self.dropped = 0

    def _estimate(self):
        now = time.monotonic()
        self.fill = max(0.0, self.fill - (now - self.t) * self.drain)
        self.t = now

    def _read_rpos(self):
        g = getattr(self.w, "get_value", None)
        if g is None or self.info.get("rpos") is None:
            return None
        try:
            return g(self.info["dsp"], self.info["rpos"]) & (FIFO_LEN - 1)
        except Exception:
            return None

    def send(self, words, timeout=0.05):
        """Queue SCOPE MIDI words.  Waits (at most `timeout` per word) while the FIFO is full; words that still
        do not fit are dropped and counted (a stuck DSP must not block the sequencer thread forever)."""
        d, fifo, wpos = self.info["dsp"], self.info["fifo"], self.info["wpos"]
        for wd in words:
            self._estimate()
            deadline = time.monotonic() + timeout
            while self.fill >= FIFO_LEN - 2:
                rd = self._locked(self._read_rpos)
                if rd is not None:
                    self.fill = (self.wr - rd) & (FIFO_LEN - 1)
                    self.t = time.monotonic()
                    if self.fill < FIFO_LEN - 2:
                        break
                if time.monotonic() > deadline:
                    break
                time.sleep(0.0005)
                self._estimate()
            if self.fill >= FIFO_LEN - 2:
                self.dropped += 1
                continue
            nxt = (self.wr + 1) & (FIFO_LEN - 1)

            def put(i=self.wr, n=nxt, v=wd & 0x7FFFFFFF):
                self.w.set_value(d, fifo + i, v)         # the word first ...
                self.w.set_value(d, wpos, n)             # ... then publish the new write position
            self._locked(put)
            self.wr = nxt
            self.fill += 1
            self.sent += 1

    def _locked(self, fn):
        if self.lock is None:
            return fn()
        with self.lock:
            return fn()

    def reset(self):
        """After (re)loading SNC2MIDI: its read index is 0 again."""
        self.wr, self.fill = 0, 0.0


class PrintWriter:
    """Stand-in writer for dry runs: prints the SetValue frames."""

    def __init__(self, out=sys.stdout):
        self.out = out

    def set_value(self, dsp, addr, value):
        self.out.write("DSP%d SetValue 0x%04X = 0x%08X\n" % (dsp, addr, value & 0xFFFFFFFF))


# ============================================================================ DSP-side helpers (ops only)

SNC2MIDI = "SNC2MIDI.dsp"       # "Sync to MIDI": in0 Sync In, in1 MIDI Fifo, in2 MIDI In, in3 Fifo write pos
MVC_EASY16 = "M_V_M16E.dsp"     # "Midi Voice Control Easy 16"


def midi_source_ops(rack, dsp, dsp_dir=DEFAULT_DSP_DIR, name="PC MIDI In"):
    """Load SNC2MIDI on `dsp` in FIFO mode.  Returns (module, ops, info) - info is what ScopeMidiFifo needs.
    in1 ('MIDI Fifo', PROCParam) points at a value word holding the FIFO address (the module reads
    DM(DM(input1))); in3 (write position) gets its own value word; in0/in2 stay at _null (in2 == _null is
    what selects the FIFO path)."""
    import pulsar_modules as pm
    mod, ops = rack.load(os.path.join(dsp_dir, SNC2MIDI), dsp, name=name)
    d = rack.dsp[dsp]
    fifo = d.dm.alloc(FIFO_LEN)
    if fifo < 0:
        raise pm.LinkError("DSP%d: no DM for the MIDI FIFO" % dsp)
    mod.tables = {1: (fifo, FIFO_LEN)}
    ops.append(("data", dsp, fifo, [0] * FIFO_LEN, "%s: MIDI FIFO (32 words)" % mod.name))
    ops += rack.set_in_pad(mod, 1, fifo)
    ops += rack.set_in_pad(mod, 3, 0)
    info = {"dsp": dsp, "fifo": fifo, "wpos": mod.value_slots[3],
            "rpos": mod.seg_mod + mod.cls.off_async_out(1), "midi_out": 0}
    return mod, ops, info


def table_ops(rack, mod, i, values, elements=None, label=None):
    """PROCParam input (tuning/velocity/key tables, FIFOs): allocate `elements` DM words, upload `values`
    (zero-padded) and point input i at a value word holding the table address (base.dll writes these arrays
    from the atom pad's stored items)."""
    import pulsar_modules as pm
    n = elements or len(values)
    d = rack.dsp[mod.dsp]
    addr = d.dm.alloc(n)
    if addr < 0:
        raise pm.LinkError("DSP%d: no DM for a %d-word table" % (mod.dsp, n))
    if not hasattr(mod, "tables"):
        mod.tables = {}
    mod.tables[i] = (addr, n)
    vals = [int(v) & 0xFFFFFFFF for v in list(values)[:n]] + [0] * max(0, n - len(values))
    ops = [("data", mod.dsp, addr, vals, label or "%s in%d table (%d words)" % (mod.name, i, n))]
    return ops + rack.set_in_pad(mod, i, addr)


def free_tables(rack, mod):
    """To be called by pulsard after unload(mod): table memory is not part of the module's segments."""
    for addr, _n in getattr(mod, "tables", {}).values():
        rack.dsp[mod.dsp].dm.free(addr)
    mod.tables = {}


def set_voices_ops(rack, mod, n):
    """Sim2k SetVoices -> FUN_10c17de0 [C]: write n to the module's `numVoices` word (seg_mod) and re-point the
    seg_sync relocation of `<prefix>VoiceDef` (e.g. jmpVoiceDef, copySyncOutVoiceDef) at `<prefix><n>`, the
    entry of the unrolled per-voice code that handles n voices.  n is clamped to the module's array size."""
    import pulsar_modules as pm
    import scope_device as sdv
    lay = sdv.voice_layout(mod.cls)
    if lay is not None:
        n = max(1, min(n, lay["n"]))
    obj = mod.cls.obj
    ops = []
    s = pm._find_sym(obj, "numVoices", False)
    if s is not None and s.scnum > 0:
        sec = obj.sections[s.scnum - 1]
        base = mod.base.get(sec.name[:8], -1)
        if base >= 0:
            ops.append(("set", mod.dsp, base + s.value - sec.vaddr, n, "%s.numVoices = %d" % (mod.name, n)))
    d = rack.dsp[mod.dsp]
    for (seg, name), addrs in sorted(mod.sites.items()):
        if seg != "seg_sync" or not name.endswith("VoiceDef"):
            continue
        tgt = pm._find_sym(obj, name[:-3] + str(n), False)
        if tgt is None or tgt.scnum <= 0:
            raise pm.LinkError("%s: no symbol %s%d for %d voices" % (mod.name, name[:-3], n, n))
        tsec = obj.sections[tgt.scnum - 1]
        addr = mod.base[tsec.name[:8]] + tgt.value - tsec.vaddr
        op = d._patch_op(addrs, addr, "%s: %s -> %s%d (0x%x)" % (mod.name, name, name[:-3], n, addr))
        if op is not None:
            ops.append(op)
    mod.voices = n
    return ops


def test_synth_ops(rack, dsp, voices=4, dsp_dir=DEFAULT_DSP_DIR):
    """Dry-run builder of the first MIDI target (docs/midi_synths.md §5.1): SNC2MIDI -> MVC Easy 16 ->
    per voice MMOSC6 + ADSR-EG5 + LINVOL -> Mixer 16, all on one DSP, only unprotected modules.
    Returns (modules dict, ops, fifo info).  Output = mods["out"] sync out 0 (-20 dB LINVOL after the mixer;
    connect it to Mix L/R)."""
    import scope_device as sdv
    j = lambda f: os.path.join(dsp_dir, f)          # noqa: E731
    mods, ops = {}, []
    src, o, info = midi_source_ops(rack, dsp, dsp_dir)
    mods["midi"] = src
    ops += o
    mvc, o = rack.load(j(MVC_EASY16), dsp, name="MVC Easy 16")
    mods["mvc"] = mvc
    ops += o
    mix, o = rack.load(j("16MIX.dsp"), dsp, name="Mixer 16")
    mods["mix"] = mix
    ops += o
    lay_mvc, lay_mix = sdv.voice_layout(mvc.cls), sdv.voice_layout(mix.cls)
    voices = max(1, min(voices, lay_mvc["n"], lay_mix["n"]))
    ops += set_voices_ops(rack, mvc, voices) + set_voices_ops(rack, mix, voices)
    ops += table_ops(rack, mvc, 1, [0] * 128, 128, "MVC Tune table (equal temperament: all offsets 0)")
    ops += rack.set_in_pad(mvc, 3, 16)                       # channel 16 = omni (bit 4 set) [L]
    ops += rack.connect(src, 0, mvc, 0)                      # SNC2MIDI MIDI Out -> MVC Midi Input
    ops += rack.set_in_pad(mix, 0, 0x7FFFFFFF // max(1, voices))   # master gain 1/voices (no clipping)
    for v in range(voices):
        osc, o = rack.load(j("MMOSC6.dsp"), dsp, name="Osc v%d" % v)
        ops += o
        eg, o = rack.load(j("ADSR-EG5.dsp"), dsp, name="ADSR v%d" % v)
        ops += o
        vca, o = rack.load(j("LINVOL.dsp"), dsp, name="VCA v%d" % v)
        ops += o
        mods["osc%d" % v], mods["eg%d" % v], mods["vca%d" % v] = osc, eg, vca
        ops += rack.connect(mvc, sdv.voice_out_pad(lay_mvc, 8, v), osc, 0)   # Frequency v -> osc f
        ops += rack.connect(mvc, sdv.voice_out_pad(lay_mvc, 7, v), eg, 0)    # Gate v -> ADSR gate
        ops += rack.connect(eg, 0, mvc, sdv.voice_in_pad(lay_mvc, 8, v))     # ADSR EG sync -> MVC ESYN v
        ops += rack.connect(osc, 0, vca, 0)
        ops += rack.connect(eg, 1, vca, 1)                                   # ADSR level -> VCA vol
        ops += rack.connect(vca, 0, mix, sdv.voice_in_pad(lay_mix, 1, v))    # -> Mixer 16 input v
        # EZSynth's saved values: saw/rect wave 4; A 0, D max (= hold), S 0, R 100 samples, slope 5.
        # Time pads are unit 2 (samples at 48 kHz, scaled by fs/48000 by the host); R = 4800 = 100 ms here.
        for i, val in ((4, 0), (5, 0x7FFFFFFF), (6, 0), (7, 4800), (8, 5)):
            ops += rack.set_in_pad(eg, i, val)
        ops += rack.set_in_pad(osc, 1, 4)
    out, o = rack.load(j("LINVOL.dsp"), dsp, name="Synth Out")      # safety gain: saw is full scale
    mods["out"] = out
    ops += o
    ops += rack.connect(mix, 0, out, 0)
    ops += rack.set_in_pad(out, 1, 0x0CCCCCCC)                         # 0.1 = -20 dB
    return mods, ops, info


# ============================================================================ ALSA sequencer (ctypes)

SND_SEQ_OPEN_DUPLEX = 3
SND_SEQ_NONBLOCK = 1
CAP_READ, CAP_WRITE, CAP_SUBS_READ, CAP_SUBS_WRITE = 1 << 0, 1 << 1, 1 << 5, 1 << 6
TYPE_MIDI_GENERIC, TYPE_SYNTH, TYPE_HARDWARE, TYPE_SOFTWARE, TYPE_SYNTHESIZER, TYPE_APPLICATION = \
    1 << 1, 1 << 10, 1 << 16, 1 << 17, 1 << 18, 1 << 20
SND_SEQ_QUEUE_DIRECT = 253
SND_SEQ_ADDRESS_SUBSCRIBERS = 254
SND_SEQ_ADDRESS_UNKNOWN = 253
EV_SIZE = 28                    # sizeof(snd_seq_event_t) on every ABI (u8 x4, 8-byte time, 2 addrs, 12-byte data)


class _PollFd(ctypes.Structure):
    _fields_ = [("fd", ctypes.c_int), ("events", ctypes.c_short), ("revents", ctypes.c_short)]


class AlsaError(Exception):
    pass


def _lib():
    name = ctypes.util.find_library("asound") or "libasound.so.2"
    lib = ctypes.CDLL(name, use_errno=True)
    P, I, L, S = ctypes.c_void_p, ctypes.c_int, ctypes.c_long, ctypes.c_char_p
    sig = {
        "snd_seq_open": (I, [ctypes.POINTER(P), S, I, I]),
        "snd_seq_close": (I, [P]),
        "snd_seq_set_client_name": (I, [P, S]),
        "snd_seq_client_id": (I, [P]),
        "snd_seq_create_simple_port": (I, [P, S, ctypes.c_uint, ctypes.c_uint]),
        "snd_seq_connect_from": (I, [P, I, I, I]),
        "snd_seq_connect_to": (I, [P, I, I, I]),
        "snd_seq_event_input": (I, [P, ctypes.POINTER(P)]),
        "snd_seq_event_input_pending": (I, [P, I]),
        "snd_seq_event_output_direct": (I, [P, P]),
        "snd_seq_poll_descriptors_count": (I, [P, ctypes.c_short]),
        "snd_seq_poll_descriptors": (I, [P, ctypes.POINTER(_PollFd), ctypes.c_uint, ctypes.c_short]),
        "snd_seq_nonblock": (I, [P, I]),
        "snd_seq_parse_address": (I, [P, P, S]),
        "snd_midi_event_new": (I, [ctypes.c_size_t, ctypes.POINTER(P)]),
        "snd_midi_event_free": (None, [P]),
        "snd_midi_event_no_status": (None, [P, I]),
        "snd_midi_event_decode": (L, [P, ctypes.c_char_p, L, P]),
        "snd_midi_event_encode": (L, [P, ctypes.c_char_p, L, P]),
        "snd_midi_event_reset_encode": (None, [P]),
        "snd_strerror": (S, [I]),
    }
    for fn, (res, args) in sig.items():
        f = getattr(lib, fn)
        f.restype, f.argtypes = res, args
    return lib


class SeqClient:
    """ALSA sequencer client with one writable port ("Synth In"); every MIDI event that reaches it is
    converted back to raw bytes (snd_midi_event_decode, running status off) and handed to `on_bytes`."""

    def __init__(self, name="Pulsar2 MIDI", port="Synth In", on_bytes=None):
        self.lib = _lib()
        self.h = ctypes.c_void_p()
        self._chk(self.lib.snd_seq_open(ctypes.byref(self.h), b"default", SND_SEQ_OPEN_DUPLEX, SND_SEQ_NONBLOCK),
                  "snd_seq_open")
        self.lib.snd_seq_set_client_name(self.h, name.encode())
        self.client = self.lib.snd_seq_client_id(self.h)
        self.port = self._chk(self.lib.snd_seq_create_simple_port(
            self.h, port.encode(), CAP_WRITE | CAP_SUBS_WRITE,
            TYPE_MIDI_GENERIC | TYPE_SYNTH | TYPE_HARDWARE | TYPE_SYNTHESIZER), "create port")
        self.dec = ctypes.c_void_p()
        self._chk(self.lib.snd_midi_event_new(4096, ctypes.byref(self.dec)), "snd_midi_event_new")
        self.lib.snd_midi_event_no_status(self.dec, 1)
        self.buf = ctypes.create_string_buffer(4096)
        self.on_bytes = on_bytes
        n = self.lib.snd_seq_poll_descriptors_count(self.h, select.POLLIN)
        pfds = (_PollFd * n)()
        self.lib.snd_seq_poll_descriptors(self.h, pfds, n, select.POLLIN)
        self.fds = [p.fd for p in pfds]

    def _chk(self, r, what):
        if r < 0:
            raise AlsaError("%s: %s" % (what, self.lib.snd_strerror(r).decode()))
        return r

    def connect_from(self, addr):
        """Subscribe our port to a source given as 'client:port' or a client name ('Midi Through:0')."""
        a = (ctypes.c_ubyte * 2)()
        self._chk(self.lib.snd_seq_parse_address(self.h, ctypes.cast(a, ctypes.c_void_p), addr.encode()),
                  "parse address %r" % addr)
        self._chk(self.lib.snd_seq_connect_from(self.h, self.port, a[0], a[1]), "connect from %s" % addr)

    def poll(self, timeout=None):
        """Wait for events (seconds, None = forever) and dispatch them; returns the number of events."""
        if not self.lib.snd_seq_event_input_pending(self.h, 1):
            r, _, _ = select.select(self.fds, [], [], timeout)
            if not r:
                return 0
        n = 0
        ev = ctypes.c_void_p()
        while True:
            r = self.lib.snd_seq_event_input(self.h, ctypes.byref(ev))
            if r == -11 or r == -4:                 # -EAGAIN / -EINTR: nothing more
                break
            if r == -28:                            # -ENOSPC: input overrun, events lost
                sys.stderr.write("pulsar_midi: ALSA input overrun\n")
                continue
            if r < 0:
                break
            n += 1
            ln = self.lib.snd_midi_event_decode(self.dec, self.buf, len(self.buf), ev)
            if ln > 0 and self.on_bytes:
                self.on_bytes(self.buf.raw[:ln])
            if r == 0:
                break
        return n

    def close(self):
        if self.h:
            self.lib.snd_midi_event_free(self.dec)
            self.lib.snd_seq_close(self.h)
            self.h = None


class SeqSender:
    """Tiny ALSA output client (tests): raw MIDI bytes -> events sent directly to the subscribers."""

    def __init__(self, name="Pulsar2 MIDI test"):
        self.lib = _lib()
        self.h = ctypes.c_void_p()
        r = self.lib.snd_seq_open(ctypes.byref(self.h), b"default", SND_SEQ_OPEN_DUPLEX, 0)
        if r < 0:
            raise AlsaError("snd_seq_open: %s" % self.lib.snd_strerror(r).decode())
        self.lib.snd_seq_set_client_name(self.h, name.encode())
        self.client = self.lib.snd_seq_client_id(self.h)
        self.port = self.lib.snd_seq_create_simple_port(self.h, b"out", CAP_READ | CAP_SUBS_READ,
                                                        TYPE_MIDI_GENERIC | TYPE_APPLICATION)
        self.enc = ctypes.c_void_p()
        self.lib.snd_midi_event_new(4096, ctypes.byref(self.enc))

    def connect_to(self, client, port):
        r = self.lib.snd_seq_connect_to(self.h, self.port, client, port)
        if r < 0:
            raise AlsaError("connect_to: %s" % self.lib.snd_strerror(r).decode())

    def send(self, data):
        ev = ctypes.create_string_buffer(EV_SIZE)
        data = bytes(data)
        i = 0
        while i < len(data):
            self.lib.snd_midi_event_reset_encode(self.enc)
            ctypes.memset(ev, 0, EV_SIZE)
            k = self.lib.snd_midi_event_encode(self.enc, data[i:], len(data) - i, ev)
            if k <= 0:
                break
            i += k
            if ev.raw[0] == 0:                       # SND_SEQ_EVENT_SYSTEM: incomplete message, nothing to send
                continue
            ev[3] = SND_SEQ_QUEUE_DIRECT             # queue
            ev[13] = self.port                       # source.port
            ev[14] = SND_SEQ_ADDRESS_SUBSCRIBERS     # dest.client
            ev[15] = SND_SEQ_ADDRESS_UNKNOWN         # dest.port
            r = self.lib.snd_seq_event_output_direct(self.h, ev)
            if r < 0:
                raise AlsaError("output: %s" % self.lib.snd_strerror(r).decode())

    def close(self):
        if self.h:
            self.lib.snd_midi_event_free(self.enc)
            self.lib.snd_seq_close(self.h)
            self.h = None


class MidiBridge:
    """ALSA client -> packer -> ScopeMidiFifo.  For pulsard: create it after the MIDI source module is loaded,
    run `serve()` in a thread; stop() ends it.  `sink` is a ScopeMidiFifo (or anything with send(words))."""

    def __init__(self, sink, name="Pulsar2 MIDI", connect=()):
        self.sink = sink
        self.packer = MidiPacker()
        self.seq = SeqClient(name, on_bytes=self._bytes)
        for a in connect:
            self.seq.connect_from(a)
        self.running = False

    def _bytes(self, data):
        w = self.packer.feed(data)
        if w:
            self.sink.send(w)

    def serve(self):
        self.running = True
        while self.running:
            self.seq.poll(0.2)

    def stop(self):
        self.running = False


# ============================================================================ CLI

class _Collect:
    def __init__(self):
        self.words = []

    def send(self, words):
        self.words += words


def cmd_encode(a):
    data = bytes(int(x, 16) for x in a.bytes)
    for w in MidiPacker().feed(data):
        print("0x%08X" % w)
    return 0


def cmd_listen(a):
    p = MidiPacker()

    def show(data):
        ws = p.feed(data)
        print("%-24s -> %s" % (data.hex(" "), " ".join("0x%06X" % w for w in ws)))
        sys.stdout.flush()
    c = SeqClient(a.name, on_bytes=show)
    for x in a.connect or []:
        c.connect_from(x)
    print("ALSA sequencer client %d:%d \"%s\" - connect a source, e.g. aconnect <src> %d:%d" % (
        c.client, c.port, a.name, c.client, c.port))
    end = time.monotonic() + a.seconds if a.seconds else None
    try:
        while end is None or time.monotonic() < end:
            c.poll(0.2)
    except KeyboardInterrupt:
        pass
    c.close()
    return 0


def _selftest_packer():
    P = lambda b: MidiPacker().feed(bytes(b))        # noqa: E731
    cases = [
        ([0x90, 0x3C, 0x64], [0x903C64]),
        ([0x90, 0x3C, 0x64, 0x3E, 0x00], [0x903C64, 0x903E00]),            # running status
        ([0xC5, 0x07], [0xC50700]),
        ([0xD0, 0x40], [0xD04000]),
        ([0xE0, 0x00, 0x40], [0xE00040]),
        ([0xF8], [0xF80000]),
        ([0x90, 0x3C, 0xF8, 0x64], [0xF80000, 0x903C64]),                   # realtime inside a message
        ([0xF0, 0x7E, 0x7F, 0x09, 0x01, 0xF7], [0xF07E7F, 0xF00901, 0xF0F700]),
        ([0xF0, 0x43, 0x10, 0x4C, 0xF7], [0xF04310, 0xF04CF7]),
        ([0xF2, 0x10, 0x20], [0xF21020]),
        ([0xF6], [0xF60000]),
    ]
    ok = True
    for b, exp in cases:
        got = P(b)
        if got != exp:
            ok = False
            print("FAIL %s -> %s (expected %s)" % (bytes(b).hex(" "), [hex(x) for x in got], [hex(x) for x in exp]))
    for b in ([0x90, 0x3C, 0x64], [0xB0, 0x07, 0x7F], [0xF0, 0x7E, 0x7F, 0x09, 0x01, 0xF7], [0xF0, 0x43, 0xF7]):
        if unpack(P(b)) != bytes(b):
            ok = False
            print("FAIL round trip %s -> %s" % (bytes(b).hex(" "), unpack(P(b)).hex(" ")))
    # toggled words decode the same
    if unpack([0x80903C64]) != bytes([0x90, 0x3C, 0x64]):
        ok = False
        print("FAIL bit-31 toggle")
    print("packer: %s (%d cases)" % ("OK" if ok else "FAILED", len(cases)))
    return ok


def _selftest_fifo():
    class W:
        def __init__(self):
            self.mem = {}

        def set_value(self, d, a, v):
            self.mem[(d, a)] = v

        def get_value(self, d, a):
            return self.mem.get((d, a), 0)
    w = W()
    info = {"dsp": 1, "fifo": 0xD000, "wpos": 0xD100, "rpos": 0xD101}
    f = ScopeMidiFifo(w, info, drain=1e9)
    f.send([0x903C64, 0x803C00])
    ok = w.mem[(1, 0xD000)] == 0x903C64 and w.mem[(1, 0xD001)] == 0x803C00 and w.mem[(1, 0xD100)] == 2
    f2 = ScopeMidiFifo(w, info, drain=0.0)           # nothing drains and rpos stays 0: must drop, not hang
    f2.send([0x903C64] * 40, timeout=0.001)
    ok = ok and f2.sent == FIFO_LEN - 2 and f2.dropped == 40 - (FIFO_LEN - 2)
    print("fifo: %s" % ("OK" if ok else "FAILED (sent %d dropped %d)" % (f2.sent, f2.dropped)))
    return ok


def _selftest_alsa():
    try:
        got = []
        rx = SeqClient("Pulsar2 MIDI selftest", on_bytes=lambda b: got.append(bytes(b)))
        tx = SeqSender()
    except (OSError, AlsaError) as e:
        print("alsa: SKIPPED (%s)" % e)
        return True
    try:
        tx.connect_to(rx.client, rx.port)
        msgs = [bytes([0x90, 0x3C, 0x64]), bytes([0xB0, 0x07, 0x50]), bytes([0xE0, 0x00, 0x40]),
                bytes([0x80, 0x3C, 0x00]), bytes([0xF0, 0x7E, 0x7F, 0x09, 0x01, 0xF7]), bytes([0xF8])]
        for m in msgs:
            tx.send(m)
        end = time.monotonic() + 2.0
        while len(got) < len(msgs) and time.monotonic() < end:
            rx.poll(0.1)
        p = MidiPacker()
        words = [w for g in got for w in p.feed(g)]
        exp = [w for m in msgs for w in MidiPacker().feed(m)]
        ok = words == exp
        print("alsa loopback %d:%d -> %d:%d: %s (%d events, words %s)" % (
            tx.client, tx.port, rx.client, rx.port, "OK" if ok else "FAILED", len(got),
            " ".join("%06X" % w for w in words)))
        return ok
    finally:
        tx.close()
        rx.close()


def cmd_selftest(a):
    ok = _selftest_packer() & _selftest_fifo()
    if not a.no_alsa:
        ok &= _selftest_alsa()
    return 0 if ok else 1


def cmd_ops(a):
    import pulsar_modules as pm
    rack = pm.Rack(a.dsp_dir)
    mods, ops, info = test_synth_ops(rack, a.dsp, a.voices, a.dsp_dir)
    print(pm.format_ops(ops))
    print("# %d ops; MIDI FIFO on DSP%d at 0x%04X, write position slot 0x%04X, read position 0x%04X" % (
        len(ops), info["dsp"], info["fifo"], info["wpos"], info["rpos"]))
    print("# output: %s sync out 0 (seg_mod 0x%04X) -> connect to Mix L/R" % (mods["out"].name, mods["out"].seg_mod))
    print("# a note-on C4 vel 100 would be:")
    ScopeMidiFifo(PrintWriter(), info).send(MidiPacker().feed(bytes([0x90, 0x3C, 0x64])))
    return 0


def cmd_voices(a):
    import json
    import scope_device as sdv
    p = sdv.voice_plan(a.file, a.voices, a.dsp_dir)
    if a.json:
        json.dump(p, sys.stdout, indent=1, default=str)
        print()
    else:
        sdv.print_voice_plan(p)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("encode")
    s.add_argument("bytes", nargs="+")
    s.set_defaults(fn=cmd_encode)
    s = sub.add_parser("listen")
    s.add_argument("--seconds", type=float, default=0)
    s.add_argument("--name", default="Pulsar2 MIDI")
    s.add_argument("--connect", action="append", help="source 'client:port' to subscribe to")
    s.set_defaults(fn=cmd_listen)
    s = sub.add_parser("selftest")
    s.add_argument("--no-alsa", action="store_true")
    s.set_defaults(fn=cmd_selftest)
    s = sub.add_parser("ops")
    s.add_argument("--dsp", type=int, default=2)
    s.add_argument("--voices", type=int, default=4)
    s.add_argument("--dsp-dir", default=DEFAULT_DSP_DIR)
    s.set_defaults(fn=cmd_ops)
    s = sub.add_parser("voices")
    s.add_argument("file")
    s.add_argument("--voices", type=int, default=4)
    s.add_argument("--dsp-dir", default=DEFAULT_DSP_DIR)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_voices)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
