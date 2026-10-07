#!/usr/bin/env python3
"""
pulsar_modules.py - link, load, activate and wire relocatable DSP modules (.dsp/.ol) on a running
Pulsar2 (pluto / ADSP-21065L) DSP, replicating SCOPE's Sim2k.dll. Spec: docs/module_loading.md.

Design: this module never touches hardware. All linker/allocator state lives in `PlutoDsp` objects
(one per DSP, built from the puls2os<n>.21k image the DSP runs). Every operation returns a list of
*ops* (plain tuples). `execute(board, ops)` replays them with the `pulsar_loader.Board` helpers
(upload_code / upload_data / set_value / sysmsg) - pass a Board built on a SimBar for a dry run, or
just print the ops with `format_ops()`.

  Op tuples
    ("code",   dsp, addr, data_bytes, label)    UploadCode: 6 bytes per 48-bit instruction (state 2 path)
    ("data",   dsp, addr, [u32...],   label)    UploadData (40-bit DM words cut to their top 32 bits)
    ("set",    dsp, addr, value,      label)    SetValue
    ("sysmsg", dsp, type, a, b,       label)    sysmsg {a, b, type} at sysMsg+1, wait for the ack
    ("patch",  dsp, [pm_addr...], value, label) UploadData(list -> codeBuf) + sysmsg 6 (low 16 bits := value)

Offline use:
  pulsar_modules.py link P2_AINIT.dsp P2_ANO.dsp CSineR4.dsp --dsp 0 [--disasm] [--ops]
  pulsar_modules.py selftest            link the three test modules on all six puls2os images and check
                                        every branch / data reference of the relocated code

Function addresses (FUN_10cxxxxx) refer to Sim2k.dll (SCOPE 5.1); [C] = read from the code, [L] = likely.
"""

import argparse
import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sc_decode  # noqa: E402

_HERE = os.path.dirname(os.path.realpath(__file__))
DEFAULT_DSP_DIR = os.environ.get("PULSAR_DSP_DIR") or os.path.join(
    _HERE, "..", "..", "scope_full", "app", "App", "Dsp")

PM_START, PM_SIZE = 0x8000, 0x1800      # pluto vt+0xd8/+0xdc  -> PM heap 0x8000..0x97FF
DM_START, DM_SIZE = 0xC400, 0x1C00      # pluto vt+0xe0/+0xe4  -> DM heap 0xC400..0xDFFF
COMM_BASE = 0xC080                      # pluto vt+0xf8: sync-output (TCB) block of DSP d = COMM_BASE + 0x20*d
TCB_SPAN = 0x20                         # words per DSP block before a neighbour block must be moved
CODEBUF_LEN = 0x60                      # OS codeBuf: 96 DM words (max patch addresses per sysmsg 6)
CLEARMEM_MIN = 0x20                     # UploadData uses sysmsg 0x11 for all-zero blocks longer than this

PM_SEGS = ("seg_pmco", "seg_sync", "seg_asyn", "seg_init", "seg_exit")
DM_SEGS = ("seg_mod", "seg_dmda", "seg_inda", "seg_exda", "seg_mod2", "seg_mod3", "seg_dmd2", "seg_dmd3",
           "sram_mod", "sram_com")
HOST_ONLY = ("seg_desc", "seg_name", "seg_info", "seg_id", "seg_attr", "seg_junc", "seg_cfg", "seg_md")

SYNC_PAD = 0xC000                       # pad type & 0xC000 != 0 -> sync (audio-rate, double-buffered) pad


class LinkError(Exception):
    pass


# ============================================================================ heap (FUN_10c25c30/25a80/25e70)

class _Block:
    __slots__ = ("start", "len", "use", "next", "prev")

    def __init__(self, start, ln):
        self.start, self.len, self.use = start, ln, 0
        self.next = self.prev = self


class Heap:
    """Sim2k block heap: circular address-ordered list, first fit from `head`, low end of the block.
    free() makes the (merged) freed block the new head, so the next search starts there."""

    def __init__(self, start, size):
        self.head = _Block(start, size)

    def alloc(self, n):
        if n < 1:
            return -1
        b = self.head
        while not (b.use == 0 and n <= b.len):
            b = b.next
            if b is self.head:
                return -1
        if n < b.len:
            u = _Block(b.start, n)
            u.next, u.prev = b, b.prev
            b.prev.next = u
            b.prev = u
            b.len -= n
            b.start += n
            b = u
        b.use += 1
        return b.start

    def alloc_fixed(self, addr, n):
        if n < 1:
            return -1
        b = self.head
        while True:
            if b.start == addr and b.len == n:
                b.use += 1                                   # exact block: shared, refcounted
                return addr
            if b.use == 0 and b.start <= addr < b.start + b.len:
                if b.start + b.len < addr + n:
                    return -1
                if b.start < addr:
                    u = _Block(addr, b.start + b.len - addr)
                    u.prev, u.next = b, b.next
                    b.next.prev = u
                    b.next = u
                    b.len = addr - b.start
                    b = u
                if n < b.len:
                    r = _Block(addr + n, b.len - n)
                    r.next, r.prev = b.next, b
                    b.next.prev = r
                    b.next = r
                    b.len = n
                b.use += 1
                return addr
            b = b.next
            if b is self.head:
                return -1

    def free(self, addr):
        if addr is None or addr < 0:
            return
        b = self.head
        while b.start != addr:
            b = b.next
            if b is self.head:
                raise LinkError("free of unknown block 0x%x" % addr)
        b.use -= 1
        if b.use < 1:
            p = b.prev
            if p is not b and p.use == 0 and p.start + p.len == b.start:
                p.len += b.len
                p.next = b.next
                b.next.prev = p
                b = p
            nx = b.next
            if nx is not b and nx.use == 0 and b.start + b.len == nx.start:
                b.len += nx.len
                b.next = nx.next
                nx.next.prev = b
        self.head = b

    def blocks(self):
        out, b = [], self.head
        while True:
            out.append((b.start, b.len, b.use))
            b = b.next
            if b is self.head:
                return sorted(out)

    def free_ranges(self):
        return [(a, n) for a, n, u in self.blocks() if u == 0]


# ============================================================================ COFF helpers

def _seg(obj, name):
    for s in obj.sections:
        if s.name[:8] == name[:8]:
            return s
    return None


def _nwords(obj, name):
    s = _seg(obj, name)
    return 0 if s is None else s.size // s.wordsize


def _dm32(sec, i):
    return int.from_bytes(sec.data[5 * i:5 * i + 4], "big")


def _find_sym(obj, name, ext_only):
    """FUN_10c0b940 (exact, case-sensitive; ext_only -> storage class C_EXT)."""
    for s in obj.symbols:
        if s.name == name and (not ext_only or s.sclass == 2):
            return s
    return None


def _strings(sec):
    """seg_name: one char per DM word (low byte of the 32-bit value), NUL-separated."""
    out, cur = [], []
    for i in range(sec.size // 5):
        c = _dm32(sec, i) & 0xFF
        if c == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(chr(c))
    if cur:
        out.append("".join(cur))
    return out


_INPUT_RE = re.compile(r"input(\d+)$")


# ============================================================================ libraries (.ol) and module classes

class Library:
    """A .ol file: shared code/data with init/exit, no seg_mod (FUN_10c17080 / FUN_10c16e90)."""

    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        self.obj, _ = sc_decode.load(path)
        self.size = {k: _nwords(self.obj, k) for k in
                     ("seg_pmco", "seg_dmda", "seg_init", "seg_inda", "seg_exit", "seg_exda")}

    def exports(self, name):
        s = _find_sym(self.obj, name, True)
        return s is not None and s.scnum != 0


class Pad:
    def __init__(self, index, kind, num, typ, lo, hi, short="", long=""):
        self.index, self.kind, self.num = index, kind, num          # kind: in / async_out / sync_out
        self.type, self.min, self.max = typ, lo, hi
        self.short, self.long = short, long

    @property
    def sync(self):
        return bool(self.type & SYNC_PAD)

    def __repr__(self):
        return "<%s%d %s %r type=0x%08x>" % (self.kind, self.num, "sync" if self.sync else "async",
                                              self.long or self.short, self.type)


class ModuleClass:
    """Parsed module file (Sim2k 'desc', FUN_10c017b0)."""

    def __init__(self, path, library_paths=None):
        self.path = path
        self.name = os.path.basename(path)
        self.obj, _ = sc_decode.load(path)
        d = _seg(self.obj, "seg_desc")
        if _seg(self.obj, "seg_mod") is None or d is None:
            raise LinkError("%s: not a module (no seg_mod/seg_desc); .ol files are libraries" % self.name)
        self.numIn, self.numAsyncOut, self.numSyncOut = _dm32(d, 0), _dm32(d, 1), _dm32(d, 2)
        self.syncCycles, self.asyncCycles, self.flags = _dm32(d, 3), _dm32(d, 4), _dm32(d, 5)
        self.size = {k: _nwords(self.obj, k) for k in PM_SEGS + DM_SEGS}
        for k in ("seg_mod2", "seg_mod3", "seg_dmd2", "seg_dmd3", "sram_mod", "sram_com"):
            if self.size[k]:
                raise LinkError("%s: %s needs a heap pluto does not have" % (self.name, k))
        names = _strings(_seg(self.obj, "seg_name")) if _seg(self.obj, "seg_name") else []
        self.short = names[0] if names else self.name
        self.long = names[1] if len(names) > 1 else ""
        self.pads = []
        p = 0
        for kind, cnt in (("in", self.numIn), ("async_out", self.numAsyncOut), ("sync_out", self.numSyncOut)):
            for k in range(cnt):
                w = 6 + 3 * p
                typ = _dm32(d, w) if w < d.size // 5 else 0
                lo = _dm32(d, w + 1) if w + 1 < d.size // 5 else 0
                hi = _dm32(d, w + 2) if w + 2 < d.size // 5 else 0
                self.pads.append(Pad(p, kind, k, typ, lo, hi,
                                     names[2 * p + 2] if 2 * p + 2 < len(names) else "",
                                     names[2 * p + 3] if 2 * p + 3 < len(names) else ""))
                p += 1
        self.library_paths = library_paths
        self.libs = None
        self.missing = []

    # seg_mod word offsets (Sim2k address arithmetic, FUN_10c17ad0 / FUN_10c17c60 / FUN_10c1b8f0) [C]
    def off_async_out(self, k):
        return 5 + 2 * k

    def off_sync_out(self, j):
        return 5 + 2 * self.numAsyncOut + j

    def off_input(self, i):
        return 6 + 2 * self.numAsyncOut + self.numSyncOut + i

    def resolve_libs(self, os_obj, libraries):
        """FUN_10c17510 / FUN_10c173c0: pick the .ol libraries that export this class's imports."""
        if self.libs is not None:
            return
        self.libs, self.missing = [], []

        def need(name):
            s = _find_sym(os_obj, name, True)
            if (s is not None and s.scnum != 0) or _INPUT_RE.match(name):
                return
            if any(lib.exports(name) for lib in self.libs):
                return
            for lib in libraries:
                if lib.exports(name):
                    self.libs.append(lib)
                    return
            self.missing.append(name)

        for s in self.obj.symbols:
            if s.scnum == 0:
                need(s.name)
        i = 0
        while i < len(self.libs):                     # libraries' own imports
            for s in self.libs[i].obj.symbols:
                if s.scnum == 0:
                    need(s.name)
            i += 1


class Module:
    """One loaded instance (Sim2k dsp_module, vtable 0x10c83c84)."""

    def __init__(self, cls, name=None):
        self.cls = cls
        self.name = name or cls.short
        self.dsp = None
        self.base = {}                       # segment -> DSP address (module +0x84..+0xb8)
        self.init_block = self.inda_block = -1
        self.sync_slots = [-1] * cls.numSyncOut   # module +0x1c: allocated sync-output slot per output
        self.inputs = {}                     # input index -> source address (relocation of 'inputN')
        self.sites = {}                      # (segment, symbol) -> [PM/DM word address of each reloc]
        self.syncout_sites = {}              # sync output j -> [PM addr of instrs referencing it]
        self.value_slots = {}                # input index -> host value slot (set_in_pad)
        self.exports = {}                    # async out k -> [export header] (cross-DSP async)
        self.export_list_addr = {}           # async out k -> DM address of that header list
        self.loaded = False

    @property
    def seg_mod(self):
        return self.base["seg_mod"]

    @property
    def has_sync(self):
        return self.cls.size["seg_sync"] > 0

    @property
    def has_async(self):
        return self.cls.size["seg_asyn"] > 0

    @property
    def entry(self):
        """Sync entry = seg_sync base + symbol 'sync' (0 in every module seen)."""
        s = _find_sym(self.cls.obj, "sync", False)
        return self.base["seg_sync"] + (s.value if s is not None and s.scnum > 0 else 0)

    def input_word(self, i):
        return self.seg_mod + self.cls.off_input(i)

    def __repr__(self):
        return "<Module %s dsp%s mod=0x%x>" % (self.name, self.dsp, self.base.get("seg_mod", -1))


class _LibEntry:
    def __init__(self, lib):
        self.lib = lib
        self.flags = 1                       # 2 = linked+uploaded, 4 = init done
        self.addr = {k: -1 for k in lib.size}
        self.use = 0


# ============================================================================ per-DSP linker / chain state

class PlutoDsp:
    def __init__(self, os_path, dspno, dsp_dir=None):
        self.dspno = dspno
        self.os_path = os_path
        self.os, _ = sc_decode.load(os_path)
        self.syms = {s.name: s.value for s in self.os.symbols if s.scnum > 0}
        self.dsp_dir = dsp_dir or os.path.dirname(os_path)
        self.pm = Heap(PM_START, PM_SIZE)
        self.dm = Heap(DM_START, DM_SIZE)
        for s in self.os.sections:            # thunk_FUN_10c20c60: reserve every OS section at its address
            h = self.pm if s.flags & 1 else self.dm
            if h.alloc_fixed(s.vaddr, s.size // s.wordsize) != s.vaddr:
                raise LinkError("kernel %s does not fit" % os_path)
        self.libtab = []
        self.modules = []                     # host list order = sync/async chain order
        # sync-output (TCB) block as left by boot (tcb_init: sysmsg 0xB, count 3, base 0xC080+0x20*d)
        self.tcb_base = COMM_BASE + TCB_SPAN * dspno
        self.tcb_count = 3
        self._libraries = None
        self.placed_syms = []                 # (space, addr, name) of relocated module/library symbols

    def _note_syms(self, obj, bases, prefix):
        for sy in obj.symbols:
            if sy.scnum <= 0 or sy.scnum > len(obj.sections):
                continue
            sec = obj.sections[sy.scnum - 1]
            b = bases.get(sec.name[:8], -1)
            if b is not None and b >= 0 and sec.name[:8] not in HOST_ONLY + ("seg_init", "seg_inda") and not sy.name.startswith("."):
                self.placed_syms.append((sec.space, b - sec.vaddr + sy.value, "%s.%s" % (prefix, sy.name)))

    # ---- helpers
    def sym(self, name):
        v = self.syms.get(name)
        if v is None:
            raise LinkError("DSP%d OS has no symbol %s" % (self.dspno, name))
        return v

    def libraries(self):
        if self._libraries is None:
            paths = sorted(glob.glob(os.path.join(self.dsp_dir, "*.ol")), key=lambda p: p.lower())
            self._libraries = [Library(p) for p in paths]
        return self._libraries

    def _libentry(self, lib):
        return next((e for e in self.libtab if e.lib.path == lib.path), None)

    # ---- external symbol lookup, pluto vt+0x13c FUN_10c3b1c0 [C]
    def lookup(self, name, module):
        if module is not None:
            m = _INPUT_RE.match(name)
            if m:
                n = int(m.group(1))
                if n >= module.cls.numIn:
                    raise LinkError("%s: %s is an invalid input" % (module.name, name))
                a = module.inputs.get(n)
                return a if a is not None else self.sym("_null")
        s = _find_sym(self.os, name, True)
        if s is not None and s.scnum > 0:
            return s.value
        for e in self.libtab:                 # only seg_pmco / seg_dmda symbols of libraries are exported
            s = _find_sym(e.lib.obj, name, True)
            if s is not None and s.scnum > 0:
                sec = e.lib.obj.sections[s.scnum - 1]
                if sec.name == "seg_pmco" and e.addr["seg_pmco"] >= 0:
                    return s.value - sec.vaddr + e.addr["seg_pmco"]
                if sec.name == "seg_dmda" and e.addr["seg_dmda"] >= 0:
                    return s.value - sec.vaddr + e.addr["seg_dmda"]
        return -1

    # ---- relocation, FUN_10c0b1d0 -> FUN_10c0b090 -> FUN_10c0aba0 -> FUN_10c0a9c0 [C]
    def relocate(self, obj, sec, bases, module, unresolved):
        data = bytearray(sec.data)
        segbase = bases[sec.name[:8]]
        for (vaddr, symndx, rtype) in sec.relocs:
            sym = obj.symbyidx[symndx]
            off = vaddr - sec.vaddr
            if rtype in (2, 6):
                p, nb = 6 * off + 3, 3        # low 24 bits of the 48-bit instruction
            elif rtype == 3:
                p, nb = 6 * off + 2, 4        # low 32 bits of the 48-bit instruction
            elif rtype == 4:
                p, nb = 5 * off, 4            # top 32 bits of the 40-bit DM word
            else:
                raise LinkError("%s: unknown relocation type %d" % (obj, rtype))
            if sym.scnum == 0:
                val = self.lookup(sym.name, module)
                if val < 0:
                    unresolved.add(sym.name)
                    continue
            elif sym.scnum < 0:
                unresolved.add(sym.name + "(abs)")
                continue
            else:
                ssec = obj.sections[sym.scnum - 1]
                nbase = bases.get(ssec.name[:8], -1)
                if nbase is None or nbase < 0:
                    unresolved.add("%s(%s not placed)" % (sym.name, ssec.name))
                    continue
                val = nbase - ssec.vaddr + sym.value
            if val < 1:
                unresolved.add(sym.name + "(=0)")
                continue
            if rtype == 6:
                val -= segbase + off          # PC-relative to the instruction's new address
            field = int.from_bytes(data[p:p + nb], "big")
            if sym.scnum != 0:
                field -= sym.value            # defined symbols: field = sym.value + addend
            val += field
            if module is not None:            # module vt+0x80 FUN_10c17c60: own sync outputs -> slot
                val, j = self._map_syncout(module, val)
                if j is not None and rtype == 3:
                    module.syncout_sites.setdefault(j, []).append(segbase + off)
                # On pluto vt+0x170 == 0 -> ireg is always I7, so the I-register field of 0xAE/0xAF
                # instructions is never rewritten, and vt+0x150 is the identity (no satellites).
            if module is not None:
                module.sites.setdefault((sec.name[:8], sym.name), []).append(segbase + off)
            val &= (1 << (8 * nb)) - 1
            data[p:p + nb] = val.to_bytes(nb, "big")
        return bytes(data)

    def _map_syncout(self, mod, addr):
        lo = mod.seg_mod + mod.cls.off_sync_out(0)
        if lo <= addr < lo + mod.cls.numSyncOut:
            j = addr - lo
            slot = mod.sync_slots[j]
            return (slot if slot >= 0 else addr), j
        return addr, None

    # ---- allocation, pluto vt+0xa0 FUN_10c3dff0 [C]
    def _allocate(self, mod):
        c = mod.cls
        c.resolve_libs(self.os, self.libraries())
        if c.missing:
            raise LinkError("%s: undefined external symbols %s" % (c.name, ", ".join(sorted(set(c.missing)))))
        fresh = [lib for lib in c.libs if self._libentry(lib) is None or not self._libentry(lib).flags & 4]
        init_sz = max([c.size["seg_init"]] + [lib.size["seg_init"] for lib in fresh])
        inda_sz = max([c.size["seg_inda"]] + [lib.size["seg_inda"] for lib in fresh])
        for lib in reversed(c.libs):          # FUN_10c3dc80(mod, 1)
            e = self._libentry(lib)
            if e is None:
                e = _LibEntry(lib)
                self.libtab.append(e)
            for k, h in (("seg_pmco", self.pm), ("seg_dmda", self.dm), ("seg_exit", self.pm),
                         ("seg_exda", self.dm)):
                if e.addr[k] < 0 and lib.size[k] > 0:
                    e.addr[k] = h.alloc(lib.size[k])
                    if e.addr[k] < 0:
                        raise LinkError("DSP%d: no memory for %s %s" % (self.dspno, lib.name, k))
            e.use += 1
        twin = next((m for m in self.modules if m.cls.path == c.path), None)   # FUN_10c158f0
        b = mod.base
        if twin is None:
            b["seg_pmco"] = self.pm.alloc(c.size["seg_pmco"])
            b["seg_exit"] = self.pm.alloc(c.size["seg_exit"])
            b["seg_asyn"] = self.pm.alloc(c.size["seg_asyn"])
            b["seg_dmda"] = self.dm.alloc(c.size["seg_dmda"])
        else:                                 # second instance shares code/data (refcount)
            for k, h in (("seg_pmco", self.pm), ("seg_exit", self.pm), ("seg_asyn", self.pm),
                         ("seg_dmda", self.dm)):
                b[k] = h.alloc_fixed(twin.base[k], c.size[k])
        b["seg_mod"] = self.dm.alloc(c.size["seg_mod"])
        b["seg_sync"] = self.pm.alloc(c.size["seg_sync"])
        mod.init_block = self.pm.alloc(init_sz)
        mod.inda_block = self.dm.alloc(inda_sz)
        b["seg_init"] = mod.init_block if c.size["seg_init"] > 0 else -1
        b["seg_inda"] = mod.inda_block if c.size["seg_inda"] > 0 else -1
        for k, v in b.items():
            if c.size.get(k, 0) > 0 and v < 0:
                raise LinkError("DSP%d: cannot allocate %s %s (%d words)" % (self.dspno, c.name, k, c.size[k]))
        for lib in reversed(c.libs):          # FUN_10c3dc80(mod, 2): new libs run init from the same block
            e = self._libentry(lib)
            if not e.flags & 4:
                if e.addr["seg_init"] < 0 and lib.size["seg_init"] > 0:
                    e.addr["seg_init"] = self.pm.alloc_fixed(mod.init_block, init_sz)
                if e.addr["seg_inda"] < 0 and lib.size["seg_inda"] > 0:
                    e.addr["seg_inda"] = self.dm.alloc_fixed(mod.inda_block, inda_sz)

    # ---- load = allocate + link + upload + activate (pluto vt+0x9c FUN_10c3b890, FUN_10c1b170)
    def load(self, cls_or_path, name=None, after=None, allow_unresolved=False):
        """Returns (module, ops). `after`: insert after this module in the chains (default: at the end)."""
        cls = cls_or_path if isinstance(cls_or_path, ModuleClass) else ModuleClass(cls_or_path)
        mod = Module(cls, name)
        mod.dsp = self.dspno
        ops = []
        unresolved = set()
        self._allocate(mod)
        # 1. libraries, FUN_10c3ab80 -> FUN_10c1cc30: reverse order, once per DSP
        for lib in reversed(cls.libs):
            e = self._libentry(lib)
            if e.flags & 2:
                continue
            for k in ("seg_pmco", "seg_dmda", "seg_exda", "seg_exit", "seg_inda", "seg_init"):
                s = _seg(lib.obj, k)
                if s is not None and e.addr[k] >= 0:
                    ops.append(self._upload_op(s, e.addr[k], self.relocate(lib.obj, s, e.addr, None, unresolved),
                                               "%s:%s" % (lib.name, k)))
            self._note_syms(lib.obj, e.addr, lib.name.split(".")[0])
            if e.addr["seg_init"] >= 0:
                entry = e.addr["seg_init"]       # FUN_10c1cc30: CALL the seg_init base, I0 = 0
                ops.append(("sysmsg", self.dspno, 7, entry, 0, "%s: run seg_init (sys_callfunction)" % lib.name))
            e.flags |= 2
            for k, h in (("seg_init", self.pm), ("seg_inda", self.dm)):
                if e.addr[k] >= 0:
                    h.free(e.addr[k])
                e.addr[k] = -1
            e.flags |= 4
        # 2. module sections, FUN_10c1bea0 order
        shared = any(m.cls.path == cls.path for m in self.modules)
        order = ["seg_mod"]
        if not shared:
            order += ["seg_pmco", "seg_exit", "seg_asyn", "seg_dmda"]
        order += ["seg_init", "seg_inda", "seg_sync"]
        for k in order:
            s = _seg(cls.obj, k)
            a = mod.base.get(k, -1)
            if s is None or a < 0:
                continue
            ops.append(self._upload_op(s, a, self.relocate(cls.obj, s, mod.base, mod, unresolved),
                                       "%s:%s" % (mod.name, k)))
        self._note_syms(cls.obj, mod.base, mod.name)
        if unresolved and not allow_unresolved:
            raise LinkError("%s on DSP%d: unresolved %s" % (cls.name, self.dspno, ", ".join(sorted(unresolved))))
        mod.unresolved = unresolved
        mod.loaded = True
        # 3. activation (FUN_10c1b170) - computed before the module is in the host list
        idx = len(self.modules) if after is None else self.modules.index(after) + 1
        ops = self._fix(ops) + self._activate(mod, idx)
        self.modules.insert(idx, mod)
        # 4. init/inda overlays are free again once fnInit has run
        self.pm.free(mod.init_block)
        self.dm.free(mod.inda_block)
        mod.init_block = mod.inda_block = -1
        mod.base["seg_init"] = mod.base["seg_inda"] = -1
        return mod, ops

    @staticmethod
    def _upload_op(sec, addr, data, label):
        if sec.space == "PM":
            return ("code", None, addr, data, label)
        n = len(data) // 5
        return ("data", None, addr, [int.from_bytes(data[5 * i:5 * i + 4], "big") for i in range(n)], label)

    def _fix(self, ops):
        return [(o[0], self.dspno) + o[2:] if o[1] is None else o for o in ops]

    def _activate(self, mod, idx):
        ops = []
        if mod.has_async:
            prev = next((m for m in reversed(self.modules[:idx]) if m.has_async), None)
            link = prev.seg_mod if prev else self.sym("_firstasync")
            ops.append(("sysmsg", self.dspno, 2, mod.seg_mod, link,
                        "%s: run fnInit + link into async chain (sys_addmodule)" % mod.name))
        elif mod.cls.size["seg_init"] > 0:
            ops.append(("sysmsg", self.dspno, 2, mod.seg_mod, self.sym("_firstmod"),
                        "%s: run fnInit (sys_addmodule on dummy list _firstmod)" % mod.name))
        if mod.has_sync:                      # FUN_10c1b070
            prev = next((m for m in reversed(self.modules[:idx]) if m.has_sync), None)
            nxt = next((m for m in self.modules[idx:] if m.has_sync), None)
            target = nxt.entry if nxt else self.sym("ret_sync")
            ops.append(self._patch_op(mod.sites.get(("seg_sync", "ret_sync"), []), target,
                                      "%s: chain exit -> %s" % (mod.name, nxt.name if nxt else "ret_sync")))
            if prev is None:
                ops.append(("set", self.dspno, self.sym("_firstsync"), mod.entry,
                            "_firstsync -> %s" % mod.name))
            else:
                ops.append(self._patch_op(prev.sites.get(("seg_sync", "ret_sync"), []), mod.entry,
                                          "%s: chain exit -> %s" % (prev.name, mod.name)))
        return self._fix([o for o in ops if o is not None])

    def _patch_op(self, addrs, value, label):
        if not addrs:
            return None
        if len(addrs) > CODEBUF_LEN:
            raise LinkError("too many patch sites (%d)" % len(addrs))
        return ("patch", self.dspno, list(addrs), value, label)

    # ---- unload (FUN_10c15aa0 -> FUN_10c1b310, FUN_10c1cbc0, FUN_10c3b640)
    def unload(self, mod):
        ops = []
        idx = self.modules.index(mod)
        if mod.has_sync:                      # FUN_10c1b240
            prev = next((m for m in reversed(self.modules[:idx]) if m.has_sync), None)
            nxt = next((m for m in self.modules[idx + 1:] if m.has_sync), None)
            tgt = nxt.entry if nxt else self.sym("ret_sync")
            if prev is None:
                ops.append(("set", self.dspno, self.sym("_firstsync"), tgt, "_firstsync -> %s" %
                            (nxt.name if nxt else "ret_sync")))
            else:
                ops.append(self._patch_op(prev.sites.get(("seg_sync", "ret_sync"), []), tgt,
                                          "%s: chain exit -> %s" % (prev.name, nxt.name if nxt else "ret_sync")))
        if mod.has_async:
            prev = next((m for m in reversed(self.modules[:idx]) if m.has_async), None)
            nxt = next((m for m in self.modules[idx + 1:] if m.has_async), None)
            ops.append(("set", self.dspno, prev.seg_mod if prev else self.sym("_firstasync"),
                        nxt.seg_mod if nxt else 0, "%s: unlink from async chain" % mod.name))
        if mod.base.get("seg_exit", -1) >= 0:
            ops.append(("sysmsg", self.dspno, 7, mod.base["seg_exit"], mod.seg_mod,
                        "%s: run seg_exit (sys_callfunction, I0 = seg_mod)" % mod.name))
        self.modules.remove(mod)
        for k in ("seg_dmda", "seg_mod"):
            self.dm.free(mod.base.get(k, -1))
        for k in ("seg_asyn", "seg_pmco", "seg_exit", "seg_sync"):
            self.pm.free(mod.base.get(k, -1))
        for v in mod.value_slots.values():
            self.dm.free(v)
        for lib in mod.cls.libs:
            e = self._libentry(lib)
            e.use -= 1
            if e.use == 0:
                if e.addr["seg_exit"] >= 0:
                    ops.append(("sysmsg", self.dspno, 7, e.addr["seg_exit"], 0, "%s: run seg_exit" % lib.name))
                for k, h in (("seg_pmco", self.pm), ("seg_dmda", self.dm), ("seg_exit", self.pm),
                             ("seg_exda", self.dm)):
                    h.free(e.addr[k])
                self.libtab.remove(e)
        mod.loaded = False
        return [o for o in ops if o is not None]

    # ---- sync-output slots (allocSyncOutput FUN_10c2e070, append path pluto FUN_10c38900) [C]
    def alloc_sync_output(self, mod, j):
        """Give sync output j its own 2-word slot in this DSP's EPB1 sync block (sent to all DSPs every
        word clock). Returns (slot, ops). Must be done before link time, or the running code is re-patched."""
        if mod.sync_slots[j] >= 0:
            return mod.sync_slots[j], []
        if self.tcb_base + 2 * (self.tcb_count + 1) + 1 >= self.tcb_base_limit():
            raise LinkError("DSP%d: sync block full (moving neighbour blocks is not implemented)" % self.dspno)
        self.tcb_count += 1
        slot = self.tcb_base - 4 + 2 * self.tcb_count
        mod.sync_slots[j] = slot
        ops = [("sysmsg", self.dspno, 0xB, self.tcb_count, self.tcb_base,
                "sync block: %d words at 0x%x (sys_syncmsghead)" % (self.tcb_count, self.tcb_base))]
        if mod.loaded and mod.syncout_sites.get(j):
            # Sim2k uses the OS copySyncOut element for a glitch-free move; a plain patch is enough offline.
            ops.append(self._patch_op(mod.syncout_sites[j], slot, "%s: sync out %d -> slot 0x%x" %
                                      (mod.name, j, slot)))
        return slot, ops

    def tcb_base_limit(self):
        return COMM_BASE + TCB_SPAN * (self.dspno + 1)

    # ---- inputs (FUN_10c1b8f0) [C]
    def link_input(self, mod, i, addr, label=None):
        """Point input i of `mod` (on this DSP) at DM address `addr` (a 2-word pair for sync inputs).
        addr None -> disconnect (back to _null)."""
        if addr is None:
            addr = self.sym("_null")
        mod.inputs[i] = addr
        if not mod.loaded:
            return []
        ops = [("set", self.dspno, mod.input_word(i), addr, label or "%s: input%d word = 0x%x" % (mod.name, i, addr))]
        ops.append(self._patch_op(mod.sites.get(("seg_sync", "input%d" % i), []), addr,
                                  "%s: seg_sync reads of input%d -> 0x%x" % (mod.name, i, addr)))
        return [o for o in ops if o is not None]


# ============================================================================ board-level helpers

class Rack:
    """All six DSPs of one Pulsar2 board."""

    def __init__(self, dsp_dir=DEFAULT_DSP_DIR, ndsp=6):
        self.dsp = [PlutoDsp(os.path.join(dsp_dir, "puls2os%d.21k" % d), d, dsp_dir) for d in range(ndsp)]

    def load(self, path, dsp, **kw):
        return self.dsp[dsp].load(path, **kw)

    def unload(self, mod):
        return self.dsp[mod.dsp].unload(mod)

    def connect(self, src, out_pad, dst, in_pad, slot=None):
        """Wire output pad `out_pad` (index among src's outputs: async outs first, then sync outs) to
        input `in_pad` of dst. Same DSP: direct address. Other DSP, sync: slot in src's sync block.
        Other DSP, async: export header list (os_sendmsgPX2). Returns ops."""
        c = src.cls
        sd, dd = self.dsp[src.dsp], self.dsp[dst.dsp]
        ops = []
        if out_pad < c.numAsyncOut:
            k = out_pad
            if src.dsp == dst.dsp:
                addr = src.seg_mod + c.off_async_out(k)
            else:
                addr, o = self._export_async(src, k, dst)
                ops += o
        else:
            j = out_pad - c.numAsyncOut
            if src.dsp == dst.dsp and not slot:
                addr = sd._map_syncout(src, src.seg_mod + c.off_sync_out(j))[0]
            else:
                addr, o = sd.alloc_sync_output(src, j)
                ops += o
        return ops + dd.link_input(dst, in_pad, addr)

    def disconnect(self, dst, in_pad):
        return self.dsp[dst.dsp].link_input(dst, in_pad, None)

    def _export_async(self, src, k, dst):
        """FUN_10c15d60 -> pluto FUN_10c3b440 [C]: receiver gets a DM word, src's exports list gets a header."""
        sd, dd = self.dsp[src.dsp], self.dsp[dst.dsp]
        slot = dd.dm.alloc(1)
        hdr = 0x20000000 | ((dst.dsp & 0xF) << 21) | ((dst.dsp & 0x10) << 14) | (slot & 0x1FFFF)
        lst = src.exports.setdefault(k, [])
        old = src.export_list_addr.get(k, -1)
        lst.append(hdr)
        if old >= 0:
            sd.dm.free(old)
        la = sd.dm.alloc(len(lst))
        src.export_list_addr[k] = la
        ops = [("data", sd.dspno, la, list(lst), "%s: async out %d export list" % (src.name, k)),
               ("set", sd.dspno, src.seg_mod + src.cls.off_async_out(k) + 1, (len(lst) << 20) | la,
                "%s: async out %d -> %d destinations" % (src.name, k, len(lst)))]
        return slot, ops

    # ---- parameters
    def set_in_pad(self, mod, i, value):
        """SetInPad (FUN_10c4cb70 / FUN_10c45bf0): feed an unconnected input with a host value.
        Sync pads get a 2-word pair (both halves written), async pads one word."""
        d = self.dsp[mod.dsp]
        pad = mod.cls.pads[i]
        ops = []
        slot = mod.value_slots.get(i)
        if slot is None:
            slot = d.dm.alloc(2 if pad.sync else 1)
            if slot < 0:
                raise LinkError("DSP%d: no DM for an input value" % d.dspno)
            mod.value_slots[i] = slot
            ops.append(("set", d.dspno, slot, value, "%s.%s value" % (mod.name, pad.short or i)))
            if pad.sync:
                ops.append(("set", d.dspno, slot + 1, value, "%s.%s value (2nd half)" % (mod.name, pad.short)))
            return ops + d.link_input(mod, i, slot)
        ops.append(("set", d.dspno, slot, value, "%s.%s value" % (mod.name, pad.short or i)))
        if pad.sync:
            ops.append(("set", d.dspno, slot + 1, value, "%s.%s value (2nd half)" % (mod.name, pad.short)))
        return ops

    def set_out_pad(self, mod, k, value):
        """SetOutPad, pluto FUN_10c37db0: write an async output's value word."""
        return [("set", mod.dsp, mod.seg_mod + mod.cls.off_async_out(k), value, "%s async out %d" % (mod.name, k))]

    def module_set_value(self, mod, symname, value, index=0):
        """ModuleSetValue (FUN_10c18fc0): EXT symbol in a DM section of the module, + index."""
        s = _find_sym(mod.cls.obj, symname, True)
        if s is None or s.scnum <= 0:
            raise LinkError("%s: no EXT symbol %s" % (mod.name, symname))
        sec = mod.cls.obj.sections[s.scnum - 1]
        if sec.space != "DM" or mod.base.get(sec.name[:8], -1) < 0:
            raise LinkError("%s: %s is not in a loaded DM section" % (mod.name, symname))
        return [("set", mod.dsp, mod.base[sec.name[:8]] + s.value - sec.vaddr + index, value,
                 "%s.%s[%d]" % (mod.name, symname, index))]


# ============================================================================ executing ops on a Board

def execute(board, ops, sysmsg_addr=None):
    """Replay ops with pulsar_loader.Board (DSPs must be in state 2 = OS running).
    codeBuf/sys_clearMem come from board.syms[dsp] (pulsar_loader boot) or the OS images."""
    for op in ops:
        kind, dsp = op[0], op[1]
        syms = board.syms.get(dsp, {}) if getattr(board, "syms", None) else {}
        if kind == "code":
            _, _, addr, data, _ = op
            board.upload_code(dsp, data, addr, len(data) // 6)
        elif kind == "data":
            _, _, addr, words, _ = op
            if len(words) > CLEARMEM_MIN and not any(words) and syms.get("sys_clearMem", 0) > 0:
                _sysmsg(board, dsp, 0x11, addr, len(words))     # UploadData's clearMem shortcut
            else:
                board.upload_data(dsp, words, addr)
        elif kind == "set":
            board.set_value(dsp, op[2], op[3])
        elif kind == "sysmsg":
            _sysmsg(board, dsp, op[2], op[3], op[4])
        elif kind == "patch":
            _, _, addrs, value, _ = op
            board.upload_data(dsp, list(addrs), syms.get("codeBuf", 0xC41D))
            _sysmsg(board, dsp, 6, len(addrs), value)
        else:
            raise LinkError("unknown op %r" % (kind,))


def _sysmsg(board, dsp, t, a, b):
    if not board.sysmsg(dsp, t, a, b):
        raise LinkError("timeout waiting for acknowledge from dsp %d (sysmsg 0x%x)" % (dsp, t))


def format_ops(ops):
    out = []
    for op in ops:
        k, d = op[0], op[1]
        if k == "code":
            out.append("DSP%d UploadCode 0x%04X %4d instr  %s" % (d, op[2], len(op[3]) // 6, op[4]))
        elif k == "data":
            out.append("DSP%d UploadData 0x%04X %4d words  %s" % (d, op[2], len(op[3]), op[4]))
        elif k == "set":
            out.append("DSP%d SetValue   0x%04X = 0x%08X  %s" % (d, op[2], op[3] & 0xFFFFFFFF, op[4]))
        elif k == "sysmsg":
            out.append("DSP%d sysmsg %-3s a=0x%X b=0x%X  %s" % (d, "0x%x" % op[2], op[3], op[4] & 0xFFFFFFFF, op[5]))
        elif k == "patch":
            out.append("DSP%d patch %s := 0x%X  %s" % (d, ",".join("0x%04X" % a for a in op[2]), op[3], op[4]))
    return "\n".join(out)


# ============================================================================ offline validation

def check_code(dsp, ops, extra_pm=()):
    """Decode every uploaded instruction; report static branch targets that land neither in an OS PM
    section nor in code uploaded by `ops` (init overlays count: they are valid while they run)."""
    import sharc_dis
    regions = [(s.vaddr, s.vaddr + s.size // 6) for s in dsp.os.sections if s.flags & 1]
    regions += [(o[2], o[2] + len(o[3]) // 6) for o in ops if o[0] == "code"] + list(extra_pm)
    bad, n = [], 0
    for op in ops:
        if op[0] != "code":
            continue
        addr, data = op[2], op[3]
        for i in range(len(data) // 6):
            w = int.from_bytes(data[6 * i:6 * i + 6], "big")
            ins = sharc_dis.decode(addr + i, w)
            for t in ins.targets:
                n += 1
                if not any(a <= t < b for a, b in regions):
                    bad.append((op[4], addr + i, t, ins.text))
    return n, bad


def disasm_ops(dsp, ops, out=sys.stdout, raw=False):
    import sharc_dis
    syms = sharc_dis.SymTab(dsp.os)
    for space, addr, name in dsp.placed_syms:
        tbl = syms.pm if space == "PM" else syms.dm
        lst = tbl.setdefault(addr, [])
        if name not in lst:
            lst.append(name)
    for op in ops:
        if op[0] != "code":
            continue
        out.write("; %s @0x%04X\n" % (op[4], op[2]))
        for i in range(len(op[3]) // 6):
            w = int.from_bytes(op[3][6 * i:6 * i + 6], "big")
            line = sharc_dis.fmt_line(op[2] + i, w, sharc_dis.decode(op[2] + i, w), syms)
            if not raw:                       # publishing rule: no opcode bytes
                line = "\n".join(l[:7] + l[21:] if re.match(r"[0-9A-F]{5}  [0-9A-F]{12}", l) else l
                                 for l in line.split("\n"))
            out.write(line + "\n")


def cmd_link(a):
    d = PlutoDsp(os.path.join(a.dsp_dir, "puls2os%d.21k" % a.dsp), a.dsp, a.dsp_dir)
    allops = []
    for f in a.files:
        p = f if os.path.exists(f) else os.path.join(a.dsp_dir, f)
        mod, ops = d.load(p, allow_unresolved=True)
        allops += ops
        print("DSP%d %-14s mod=0x%04X sync=%s libs=%s unresolved=%s" % (
            a.dsp, mod.cls.name, mod.seg_mod, "0x%04X" % mod.base["seg_sync"] if mod.has_sync else "-",
            [lib.name for lib in mod.cls.libs], sorted(mod.unresolved) or "none"))
        if a.ops:
            print(format_ops(ops))
    n, bad = check_code(d, allops)
    print("branch targets checked: %d, outside allocated PM: %d" % (n, len(bad)))
    for b in bad:
        print("  %s @0x%04X -> 0x%04X  %s" % b)
    print("PM free:", ", ".join("0x%04X+%d" % r for r in d.pm.free_ranges()))
    print("DM free:", ", ".join("0x%04X+%d" % r for r in d.dm.free_ranges()))
    if a.disasm:
        disasm_ops(d, allops, raw=a.raw)
    return 0


def cmd_selftest(a):
    rc = 0
    for n in range(6):
        rack_dsp = PlutoDsp(os.path.join(a.dsp_dir, "puls2os%d.21k" % n), n, a.dsp_dir)
        allops, mods = [], []
        for f in ("P2_AINIT.dsp", "P2_ANO.dsp", "CSineR4.dsp"):
            mod, ops = rack_dsp.load(os.path.join(a.dsp_dir, f), allow_unresolved=True)
            mods.append(mod)
            allops += ops
            if mod.unresolved:
                rc = 1
        ano, sine = mods[1], mods[2]
        allops += rack_dsp.link_input(ano, 0, sine.seg_mod + sine.cls.off_sync_out(0))
        allops += rack_dsp.link_input(ano, 1, sine.seg_mod + sine.cls.off_sync_out(1))
        cnt, bad = check_code(rack_dsp, allops)
        print("puls2os%d: %s | branches %d, bad %d | chain %s" % (
            n, " ".join("%s@0x%04X%s" % (m.cls.short, m.seg_mod, "!" + ",".join(sorted(m.unresolved))
                                          if m.unresolved else "") for m in mods),
            cnt, len(bad), " -> ".join(m.name for m in rack_dsp.modules if m.has_sync) + " -> ret_sync"))
        rc |= 1 if bad else 0
    return rc


def sim_board(rack, log):
    """A pulsar_loader.Board on the simulated BAR (every FIFO write logged, every sysmsg acked)."""
    import pulsar_loader
    board = pulsar_loader.Board(pulsar_loader.SimBar(log), dry_run=True)
    board.state = [2] * len(rack.dsp)
    for d in rack.dsp:
        board.syms[d.dspno] = d.syms
        board.sysmsg_addr[d.dspno] = d.syms["sysMsg"]
    return board


def cmd_dryrun(a):
    """Analog-out test chain: CSineR4 -> P2_ANO on one DSP, executed against a simulated BAR."""
    rack = Rack(a.dsp_dir)
    d = a.dsp
    ops = []
    for f in ("P2_AINIT.dsp", "P2_ANO.dsp", "CSineR4.dsp"):
        m, o = rack.load(os.path.join(a.dsp_dir, f), d)
        ops += o
    ano, sine = rack.dsp[d].modules[-2], rack.dsp[d].modules[-1]
    ops += rack.connect(sine, 0, ano, 0) + rack.connect(sine, 1, ano, 1)
    ops += rack.set_in_pad(sine, 0, 0x00A3D70A)          # phase increment (raw 32-bit, scaling [?])
    print(format_ops(ops))
    with open(a.log, "w") as log:
        execute(sim_board(rack, log), ops)
    print("dry run: BAR writes logged to %s" % a.log)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("link", "selftest", "dryrun"))
    ap.add_argument("--log", default="module_writes.log", help="dryrun: BAR write log")
    ap.add_argument("files", nargs="*")
    ap.add_argument("--dsp", type=int, default=0)
    ap.add_argument("--dsp-dir", default=DEFAULT_DSP_DIR)
    ap.add_argument("--ops", action="store_true", help="print the host operations")
    ap.add_argument("--disasm", action="store_true", help="disassemble the relocated code")
    ap.add_argument("--raw", action="store_true", help="with --disasm: include opcode words (do not publish)")
    a = ap.parse_args()
    try:
        return {"link": cmd_link, "selftest": cmd_selftest, "dryrun": cmd_dryrun}[a.command](a)
    except LinkError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
