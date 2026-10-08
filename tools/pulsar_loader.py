#!/usr/bin/env python3
"""
pulsar_loader.py - userspace DSP bring-up for the Creamware / Sonic Core Pulsar II.

Replicates what SCOPE's Sim2k.dll does on Windows (see ../re_notes/boot_sequence.md):
the whole 4 MB BAR0 is mapped into this process and the card is driven directly.

  pulsar_loader.py info                  read-only: board id + a few registers
  pulsar_loader.py boot --dry-run        simulate everything against a fake BAR, log all writes
  sudo pulsar_loader.py boot             reset the card, load puls2os0..5.21k, start the DSPs,
                                         then verify each DSP answers a GetValue round-trip

Access paths to BAR0 (first that works): the snd-pulsar hwdep device (/dev/snd/hwC<n>D0),
or --resource /sys/bus/pci/devices/0000:06:01.0/resource0 (works without the kernel module).

Function addresses in comments refer to Sim2k.dll (FUN_10cxxxxx) or scScope.sys (0x18xxxxxxx).
"""

import argparse
import ctypes
import glob
import mmap
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sc_decode  # noqa: E402

BAR_LEN = 4 * 1024 * 1024
# DSP files are Sonic Core's and are not shipped: extract the SCOPE PCI 5.1 installer with innoextract
# into a "scope_full" folder next to the repository, or point PULSAR_DSP_DIR at its App/Dsp folder.
_HERE = os.path.dirname(os.path.realpath(__file__))
DEFAULT_DSP_DIR = os.environ.get("PULSAR_DSP_DIR") or os.path.join(
    _HERE, "..", "..", "scope_full", "app", "App", "Dsp")

NUM_DSP = 6                  # Pulsar2
PULSAR2_BOARD_ID = 6         # (BAR[0] >> 8) & 0x1f
SYNCPC_MEMBASE = 0xC300      # (0x6200 - 128 syncPcChannels) * 2
SRAM = 0x20000               # dword index of BAR+0x80000
ACK_BASE = 0x800             # sysmsg ack mailbox: SRAM dword 0x800 + 2*dsp (BAR+0x82000+8n)
FIFO = 0x81000 // 4          # dword index of host->card command FIFO
FIFO_LEN = 0x400
FIFO_HEADROOM = 0x100
# Pulsar2 DSPs are class 'pluto' (FUN_10c3c100, vtable 0x10c8a634), not 'sharc'.
# DM_START + DM_SIZE - 1 = 0xDFFF is the boot release flag polled by wait_boot @PM 0x8096.
DM_START, DM_SIZE = 0xC400, 0x1C00
WRAP_PAD = (0x0FE0C008, 0x1212, 0x2424, 0x3636, 0x4848, 0x5A5A, 0x6C6C, 0x7E7E)

REG_NAMES = {0: "ctrl/status", 1: "irq cfg", 2: "fifo rd / clk", 3: "reg3",
             4: "reg4", 5: "audio cfg", 6: "reg6", 7: "reg7", 8: "reg8", 9: "reg9"}


class LoaderError(Exception):
    pass


# --------------------------------------------------------------------------- BAR access

class Bar:
    """Real BAR0 mapping. Every access is one aligned 32-bit load/store."""

    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_SYNC)
        self.mm = mmap.mmap(self.fd, BAR_LEN, mmap.MAP_SHARED,
                            mmap.PROT_READ | mmap.PROT_WRITE)
        self.w = (ctypes.c_uint32 * (BAR_LEN // 4)).from_buffer(self.mm)
        self.log = None

    def rd(self, idx):
        return self.w[idx]

    def wr(self, idx, val):
        if self.log:
            self.log.write("W %06x %08x\n" % (idx * 4, val & 0xFFFFFFFF))
        self.w[idx] = val & 0xFFFFFFFF


class SimBar:
    """Fake BAR for --dry-run: the FIFO is always drained and every DSP acks at once."""

    def __init__(self, logfile):
        self.path = "<simulated>"
        self.w = [0] * (BAR_LEN // 4)
        self.w[0] = PULSAR2_BOARD_ID << 8
        self.log = logfile

    def rd(self, idx):
        if idx == 2:                       # card FIFO read index == host write index
            return self.w[3]
        if SRAM + ACK_BASE <= idx < SRAM + ACK_BASE + 2 * NUM_DSP and not (idx & 1):
            return 1                       # sysmsg acknowledged
        return self.w[idx]

    def wr(self, idx, val):
        self.log.write("W %06x %08x\n" % (idx * 4, val & 0xFFFFFFFF))
        self.w[idx] = val & 0xFFFFFFFF


def find_hwdep():
    for dev in sorted(glob.glob("/sys/class/sound/hwC*D0")):
        # hwC<n>D0/device is the ALSA card; walk up to the PCI device
        d = os.path.realpath(os.path.join(dev, "device"))
        while d != "/" and not os.path.exists(os.path.join(d, "vendor")):
            d = os.path.dirname(d)
        try:
            with open(os.path.join(d, "vendor")) as f:
                if int(f.read(), 16) == 0x14B5:
                    return "/dev/snd/" + os.path.basename(dev)
        except OSError:
            continue
    return None


def open_bar(args):
    if args.dry_run:
        return SimBar(open(args.log, "w"))
    path = args.resource or find_hwdep()
    if not path:
        raise LoaderError("no snd-pulsar hwdep device found; load the module or pass --resource")
    bar = Bar(path)
    if args.log:
        bar.log = open(args.log, "w")
    return bar


def sim_sleep_ms(ms):
    """Sim2k Sleep() FUN_10c2c3f0 sleeps ((ms+9)/10)*10+1 ms."""
    time.sleep((((ms + 9) // 10) * 10 + 1) / 1000.0)


# --------------------------------------------------------------------------- clock (re_notes/clock_rate.md)

# ---- constants (add near the top)
UC_DSP = 5                                 # Pulsar2 vt+0x224 FUN_10c314c0
UC_MBOX = 0x800 + 1 + 2 * UC_DSP           # host SRAM dword 0x80B (BAR+0x8202C)
UC_DEST = 0x63E00000 | UC_MBOX             # vt+0xa8(0xff, 0x80B, 0, len=1)
PPLATE_ID = 2                              # backplateID -> PPlate (FUN_10c09d80)
PPLATE_NBITS_W, PPLATE_NBITS_R = 11, 16    # PPlate vt+0 / vt+4
COMM_BASE = 0xC080                         # pluto vt+0xf8; TCB block = COMM_BASE + 0x20*dsp
RATE_BITS = {48000: 0x2, 44100: 0x4, 32000: 0x6}
FSCALE = {48000: 0x40000000, 44100: 0x3ACCCCCC, 32000: 0x2AAAAAAA,
          88200: 0x75999999, 96000: 0x80000000}


class ClockMixin:
    """Methods to add to Board. Requires self.syms[dsp] (symbol dicts from load_kernel)."""

    # ---- clock primitives (FUN_10c22380 / FUN_10c22140 / FUN_10c2cb90 / FUN_10c22230)
    def start_clk(self, fast=False):
        if not fast:
            self.bit_set(2, 2); self.sleep(2)
        self.bit_set(2, 4)
        self.bit_set(0, 0x10)
        if not fast:
            self.sleep(2)
        self.bit_clr(2, 4)
        if not fast:
            self.sleep(2)

    def stop_clk(self):
        self.bit_set(2, 4); self.sleep(2)
        self.bit_clr(2, 2)
        self.bit_set(2, 2)
        self.bit_clr(0, 0x10); self.sleep(2)
        self.bit_clr(2, 4)

    def stop_clk_sync(self):
        """vt+0x30: stopClk, then re-align the DSP ring pointer to DSP 1."""
        self.stop_clk()
        if self.dry_run:
            return True
        self.sleep(2)
        t = time.monotonic() + 1.0
        while not (self.reg_rd(0) & 0x20) and time.monotonic() < t:
            self.sleep(2)
        for _ in range(NUM_DSP + 1):
            if (self.reg_rd(0) & 0xF) == 1:
                return True
            self.bit_clr(2, 2); self.bit_set(2, 2); self.sleep(2)
        print("warning: cannot switch to DSP 1, word clock or communication stalled "
              "(reg0=0x%08x)" % self.reg_rd(0))
        return False

    def reset_clk(self):
        self.bit_set(0, 2); self.bit_clr(0, 2)
        self.bit_set(2, 2)
        for d in range(NUM_DSP):                       # FUN_10c222f0 (pluto)
            self.set_value(d, self.syms[d]["wclk"], 1)
            self.set_value(d, self.syms[d]["alastClk"], 0)

    def resync_clock(self):
        """FUN_10c44870 (synchronizeBoards=1), single board."""
        self.stop_clk_sync()
        self.reset_clk()
        self.sleep(2)
        self.start_clk(fast=True)
        self.sleep(2)

    # ---- backplate serial config (FUN_10c30100 / FUN_10c301d0)
    def write_audio_cfg(self, cfg, nbits=PPLATE_NBITS_W):
        if self.verbose:
            print("HW: writeAudioCfg (0x%x)" % cfg)
        self.mask_set(5, 0, 0xD00)
        for _ in range(nbits):
            d = (cfg & 1) << 10
            self.mask_set(5, d, 0xD00)
            self.mask_set(5, d | 0x800, 0xD00)
            self.mask_set(5, d, 0xD00)
            cfg >>= 1
        self.mask_set(5, 0x100, 0xD00)
        self.mask_set(5, 0, 0xD00)

    def read_audio_cfg(self, nbits=PPLATE_NBITS_R):
        for v in (0x800, 0, 0x100, 0x900, 0x100, 0):
            self.mask_set(5, v, 0xD00)
        st = 0
        for i in range(nbits):
            bit = (self.reg_rd(5) >> 6) & 1
            st = (st << 1) | bit if i < 9 else st | (bit << i)
            self.mask_set(5, 0x800, 0xD00)
            self.mask_set(5, 0, 0xD00)
        return st

    # ---- PPlate vt+8 FUN_10c094c0
    def plate_set(self, cfg):
        cfg = (cfg | 0x100) if cfg & 0x10000 else (cfg & ~0x100)
        w = (cfg | 0x400) & 0xFFFF
        if cfg & 1:
            self.write_audio_cfg(w)
        else:
            rate = {0x4000: 88200, 6: 32000, 2: 48000}.get(cfg & 0x4006, 44100)
            running = bool(self.shadow[0] & 0x10)
            if rate == getattr(self, "plate_rate", -1) or not running:
                self.write_audio_cfg(w)
            else:
                self.stop_clk_sync(); self.sleep(2)
                self.write_audio_cfg(w)
                self.sleep(2); self.start_clk()
            self.plate_rate = rate
        self.plate_cfg = w & ~0x500
        return self.plate_cfg

    def plate_reset_havarie(self):                     # PPlate vt+0x14 FUN_10c08c10
        self.plate_set(self.plate_cfg | 0x40)
        self.plate_set(self.plate_cfg & ~0x40)

    # ---- uC query through DSP5 (FUN_10c32b00 / FUN_10c32c50)
    def uc_query(self, cmd):
        s = self.syms[UC_DSP]
        if "ucCmdOut" not in s or "ucMagicDest" not in s:
            return None
        self.bar.wr(SRAM + UC_MBOX, 0xFFFFFFFF)
        self.set_value(UC_DSP, s["ucMagicDest"], UC_DEST)
        self.set_value(UC_DSP, s["ucCmdOut"], cmd)
        self.sleep(11)
        return self.bar.rd(SRAM + UC_MBOX)

    def tcb_init(self):                                # pluto vt+0x10 / tail of vt+0x6c
        for d in range(NUM_DSP):
            if not self.sysmsg(d, 0xB, 3, COMM_BASE + 0x20 * d):
                raise LoaderError("timeout waiting for acknowledge from dsp %d (sysmsg 0xB)" % d)

    def chains_init(self):                             # FUN_10c446b0 -> pluto vt+8 FUN_10c3be00
        for d in range(NUM_DSP):
            self.set_value(d, self.syms[d]["_firstsync"], self.syms[d]["ret_sync"])
            self.set_value(d, self.syms[d]["_firstasync"], 0)

    # ---- Run tail + start (FUN_10c32f80 tail, FUN_10c2cf50, FUN_10c446b0, first FUN_10c4eae0)
    def finish_run(self, irq=False, rate=44100):
        sn = self.uc_query(0x229)                      # vt+0xf8
        inf = self.uc_query(0x21F)                     # vt+0x220
        self.uc_serial = sn                            # board serial (licence check, pulsar_license)
        self.uc_info = None if inf is None else (inf >> 16) & 0xFFFF
        print("  uC: serial=%s info=%s" % (
            "n/a" if sn is None else "0x%08x" % sn,
            "n/a" if inf is None else "0x%04x" % ((inf >> 16) & 0xFFFF)))
        bp = self.get_value(0, self.syms[0]["backplateID"]) & 0x3F   # vt+0x164 -> vt+0x280
        print("  backplateID = 0x%02x%s" % (bp, "" if bp == PPLATE_ID else "  (NOT PPlate, cfg may be wrong)"))
        self.plate_rate, self.plate_cfg = -1, 4        # PPlate vt+0x10 FUN_10c08de0
        self.plate_set(0x84)                           # -> stop/realign, write 0x484, startClk
        self.plate_cfg &= ~0x80
        if irq:                                        # master vt+0x1ec FUN_10c2cf50 (intBlkSize 0x400)
            self.mask_set(1, 0x11, 0x3F)
        self.chains_init()                             # FUN_10c446b0
        self.tcb_init()                                # FUN_10c4eae0 first call, FUN_10c3c230
        self.chains_init()
        self.set_rate(rate, write_plate=False)         # FUN_10c4eae0 rate block with DAT_10ca036c = 44100

    # ---- sample rate / clock source
    def set_rate(self, rate, clock="internal", write_plate=True):
        if write_plate:                                # pulsar_cfg -> vt+0x290 -> PPlate.set
            if clock == "internal":
                if rate not in RATE_BITS:
                    raise LoaderError("PPlate supports only %s Hz internal" % sorted(RATE_BITS))
                self.plate_set(RATE_BITS[rate])
            elif clock == "external":
                self.plate_set(0x1)                    # [?] ext source select beyond bit0 unknown
            else:
                raise LoaderError("clock must be 'internal' or 'external'")
        # FUN_10c35970 -> FUN_10c30520
        self.set_reg(9, 0)
        st = self.read_audio_cfg()
        if self.verbose:
            print("HW: readAudioCfg () = 0x%x" % st)
        if st & 0x100:
            self.sleep(5); self.stop_clk_sync()
            self.plate_reset_havarie()
            self.sleep(5); self.start_clk()
        # pluto vt+0x6c FUN_10c3c290 per DSP
        fs = FSCALE.get(rate, int(round(rate * 2 ** 32 / 192000.0)) & 0xFFFFFFFF)
        ratio = max(10, min(30, rate // 3125))
        for d in range(NUM_DSP):
            self.set_value(d, self.syms[d]["FScale"], fs)
            self.set_value(d, self.syms[d]["asRatio"], ratio)
            if not self.sysmsg(d, 0xB, 3, COMM_BASE + 0x20 * d):
                raise LoaderError("timeout waiting for acknowledge from dsp %d (sysmsg 0xB)" % d)
        self.resync_clock()                            # FUN_10c44870
        w = self.get_value(0, self.syms[0]["wclk"])
        st = self.read_audio_cfg()
        print("  rate %d (%s): wclk=0x%x status=0x%04x%s%s" % (
            rate, clock, w, st, "" if st & 4 else " (no lock bit 0x4)",
            " HAVARIE(0x100)" if st & 0x100 else ""))
        return st


# --------------------------------------------------------------------------- board

class Board(ClockMixin):
    def __init__(self, bar, verbose=False, dry_run=False):
        self.bar = bar
        self.verbose = verbose
        self.dry_run = dry_run
        self.shadow = [0] * 10          # board+0xf4: u16 shadow of control regs 0..9
        self.fifo_wr = 0                # kernel ring state (IOCTL 0x1d20ac)
        self.state = [0] * NUM_DSP      # board+0x11c: 0 = reset, 1 = booting, 2 = OS running
        self.msgbuf = []                # board+0x19c, max 0x3e dwords
        self.force_wrap = False         # board+0xac0
        self.sleep = (lambda ms: None) if dry_run else sim_sleep_ms
        self.sysmsg_addr = [None] * NUM_DSP   # OS symbol sysMsg per DSP
        self.syms = {}
        self.kernel_fifo = False            # True once the kernel owns the command FIFO (after set_controls)

    # ---- control registers (FUN_10c20440 / 10c204b0 / 10c204e0 / 10c20470)
    def reg_rd(self, i):
        return self.bar.rd(i)

    def set_reg(self, i, v):
        self.shadow[i] = v & 0xFFFF
        self.bar.wr(i, self.shadow[i])

    def bit_set(self, i, m):
        self.set_reg(i, self.shadow[i] | m)

    def bit_clr(self, i, m):
        self.set_reg(i, self.shadow[i] & ~m)

    def mask_set(self, i, v, m):
        self.set_reg(i, (self.shadow[i] & ~m) | (v & m))

    # ---- command FIFO, as scScope.sys SendMsgBuf @0x18000f570
    def _kernel_send(self, words):
        """PULSAR_IOCTL_SEND_MSG: once the kernel also writes the FIFO (mixer controls), every frame must go
        through it so that both writers are serialized and share one write index."""
        import fcntl
        buf = struct.pack("<I%dI" % len(words), len(words), *words) + bytes(4 * (62 - len(words)))
        req = (1 << 30) | (len(buf) << 16) | (ord("P") << 8) | 0x04     # _IOW('P', 4, struct pulsar_msg)
        try:
            fcntl.ioctl(self.bar.fd, req, buf)
        except OSError as e:
            raise LoaderError("PULSAR_IOCTL_SEND_MSG failed: %s" % e)

    def fifo_init(self, wr):
        self.fifo_wr = wr & (FIFO_LEN - 1)

    def fifo_send(self, words):
        if self.kernel_fifo:
            self._kernel_send(words)
            return
        n = len(words)
        deadline = time.monotonic() + 1.0
        while True:
            rd = self.bar.rd(2) & (FIFO_LEN - 1)
            free = FIFO_LEN - ((self.fifo_wr - rd) & (FIFO_LEN - 1))
            if free >= n + FIFO_HEADROOM:
                break
            if time.monotonic() > deadline:
                raise LoaderError("command FIFO stalled (rd=0x%x wr=0x%x)" % (rd, self.fifo_wr))
        for w in words:
            self.bar.wr(FIFO + self.fifo_wr, w)
            self.fifo_wr = (self.fifo_wr + 1) & (FIFO_LEN - 1)
        self.bar.wr(3, self.fifo_wr)

    # ---- sendMsg, Sim2k FUN_10c33140
    def send_msg(self, header, payload=(), batch=False):
        dsp = (header >> 21) & 0xF
        raw = bool(header & 0x80000000)
        wrap = (not batch and not raw and self.state[dsp] > 0) or self.force_wrap
        t = dsp << 21
        if wrap:
            header |= 0x20000000
            if not batch:
                self.msgbuf += [t | 0x120000, t | 0x120000]
        self.msgbuf.append(header & 0xFFFFFFFF)
        self.msgbuf += [p & 0xFFFFFFFF for p in payload]
        if wrap:
            if not batch:
                self.msgbuf.append(t | 0x20100000)
            self.msgbuf += WRAP_PAD
        if len(self.msgbuf) > 0x3E:
            raise LoaderError("message buffer overflow (%d dwords)" % len(self.msgbuf))
        if not batch:
            if wrap:
                self.msgbuf.append(0)
            self.fifo_send(self.msgbuf)
            self.msgbuf = []

    # ---- IOP register write, FUN_10c2d180
    def iop(self, dsp, reg, val, batch=False):
        hdr = ((dsp | 0x10) << 21) | (0x120000 if batch else 0x100000) | reg
        self.send_msg(hdr, [val], batch)

    # ---- SetValue, FUN_10c2d200 / FUN_10c33110
    def set_value(self, dsp, addr, val):
        if self.verbose:
            print("DSP%x: SetValue (0x%x, 0x%x)" % (dsp, addr, val))
        self.send_msg(((dsp | 0x10) << 21) | addr, [val])

    # ---- UploadCode, FUN_10c2d310 (states 0, 1 and 2)
    def upload_code(self, dsp, data, addr, n):
        instr = [data[6 * i:6 * i + 6] for i in range(n)]
        st = self.state[dsp]
        if st == 0:
            if dsp == 0:
                for b in instr:
                    self.send_msg(0x8A000000 | b[5], [b[4], b[3], b[2], b[1], b[0]])
                for _ in range(max(0, 0x100 - n)):
                    self.send_msg(0x8A000000, [0, 0, 0, 0, 0])
            self.state[dsp] = 1
            self.sleep(10)
            self.bit_clr(0, 0x20)
            return
        if st == 2:
            # OS running (asm 10c2d60c..10c2d7de): chunks of 10 instructions, each one batched message
            # [batch start, IOP 48-bit DMA setup, code frame, IOP back to 32-bit/stride 2, closing IOP].
            for off in range(0, n, 10):
                cnt = min(10, n - off)
                words = []
                for k in range(off, off + cnt, 2):
                    a = instr[k]
                    if k + 1 < off + cnt:
                        b = instr[k + 1]
                        words += [int.from_bytes(a[0:4], "big"), int.from_bytes(b[4:6] + a[4:6], "big"),
                                  int.from_bytes(b[0:4], "big")]
                    else:
                        words += [int.from_bytes(a[0:4], "big"), int.from_bytes(a[4:6], "big")]
                self.send_msg((dsp << 21) | 0x120000, [], batch=True)
                self.iop(dsp, 0x1C, 0x20E0, batch=True)
                self.iop(dsp, 0x41, 1, batch=True)
                self.iop(dsp, 0x1C, 0xE1, batch=True)
                self.force_wrap = True
                self.send_msg(((len(words) << 4 | dsp) << 21) | (addr + off), words, batch=True)
                self.force_wrap = False
                self.iop(dsp, 0x1C, 0x40, batch=True)
                self.iop(dsp, 0x41, 2, batch=True)
                self.iop(dsp, 0x12001C, 0x41)
            return
        if st != 1:
            raise LoaderError("UploadCode in state %d not supported" % st)
        self.iop(dsp, 0x1C, 0x20E0)
        self.iop(dsp, 0x42, n)
        self.iop(dsp, 0x41, 1)
        self.iop(dsp, 0x1C, 0xE1)
        i = 0
        while i < n:
            a = instr[i]
            if i == n - 1:
                words = [int.from_bytes(a[0:4], "big"), int.from_bytes(a[4:6], "big")]
                hdr = ((dsp | 0x20) << 21) | addr
                i += 1
                addr += 1
            else:
                b = instr[i + 1]
                words = [int.from_bytes(a[0:4], "big"),
                         int.from_bytes(b[4:6] + a[4:6], "big"),
                         int.from_bytes(b[0:4], "big")]
                hdr = ((dsp | 0x30) << 21) | addr
                i += 2
                addr += 2
            self.send_msg(hdr, words)

    # ---- UploadData, FUN_10c338b0
    def upload_data(self, dsp, words, addr):
        """words: list of 32-bit values (40-bit DM words already truncated to their top 32 bits)."""
        n = len(words)
        st = self.state[dsp]
        if st == 0:
            raise LoaderError("UploadData to DSP%d before boot" % dsp)
        if st == 1:
            self.iop(dsp, 0x1C, 0x2040)
            self.iop(dsp, 0x42, n)
            self.iop(dsp, 0x1C, 0x41)
        if n < 3:
            for k, w in enumerate(words):
                self.send_msg(((dsp | 0x10) << 21) | (addr + k), [w])
        elif st == 1 or n <= 3:                    # DAT_10ca0f24 == 3
            off = 0
            while off < n:
                cnt = min(14, n - off)
                self.send_msg((dsp << 21) | 0x120000, [], batch=True)
                self.iop(dsp, 0x41, 1, batch=True)
                self.force_wrap = True
                self.send_msg(((cnt << 4 | dsp) << 21) | (addr + off), words[off:off + cnt], batch=True)
                self.force_wrap = False
                self.iop(dsp, 0x120041, 2)
                off += cnt
        else:
            u = 0
            while u < n:
                k = min(14, (n - u + 1) // 2)
                last = u - 1 + 2 * k
                for j in range(2):
                    cnt = k - (1 if n < last + j else 0)
                    chunk = words[u + j:u + j + 2 * cnt:2]
                    self.send_msg(((cnt << 4 | dsp) << 21) | (addr + u + j), chunk)
                u += 2 * k

    # ---- sysmsg / GetValue, FUN_10c33410 / FUN_10c22890 / FUN_10c22650
    def sysmsg(self, dsp, mtype, a, b):
        ack = SRAM + ACK_BASE + 2 * dsp
        self.bar.wr(ack, 0)
        # OS reads a, b at sysMsg+1/+2 and the type last at sysMsg+3 (dsp_boot_analysis.md)
        self.upload_data(dsp, [a, b, mtype], self.sysmsg_addr[dsp] + 1)
        for _ in range(2):
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                if self.bar.rd(ack) != 0:
                    return True
        return False

    def get_value(self, dsp, addr):
        if not self.sysmsg(dsp, 8, addr, 0):
            raise LoaderError("timeout waiting for acknowledge from dsp %d" % dsp)
        return self.bar.rd(SRAM + ACK_BASE + 2 * dsp + 1)

    # ---------------------------------------------------------------- boot steps

    def open_init(self):
        """FUN_10c21a50."""
        bid = (self.reg_rd(0) >> 8) & 0x1F
        if bid != PULSAR2_BOARD_ID:
            raise LoaderError("board id %d, expected %d (Pulsar2)" % (bid, PULSAR2_BOARD_ID))
        for k in range(0x400):
            self.bar.wr(SRAM + k, 0)
        for i, v in ((0, 0x2E), (1, 0), (2, 0), (3, 0), (4, 0x14), (5, 4), (6, 0x14), (8, 0)):
            self.set_reg(i, v)
        self.fifo_init(0)

    def reset(self):
        """FUN_10c32db0 + FUN_10c20600 (SRAM test skipped)."""
        self.bar.wr(SRAM + 0, SYNCPC_MEMBASE | 0x84000000)
        self.bar.wr(SRAM + 1, SYNCPC_MEMBASE | 0x84000001)
        self.bit_clr(5, 0x1000)
        for k in range(2):
            self.set_reg(0, 0x2F)
            self.bit_clr(2, 2)
            self.sleep(10)
            self.bit_clr(0, 4)
            self.sleep(20)
            self.bit_clr(0, 0x13)
            self.fifo_init(self.reg_rd(2))
            if k == 0:
                self.send_msg(((0 | 0x10) << 21) | 0, [0])
        self.state = [0] * NUM_DSP
        self.bit_set(0, 4)
        self.bit_clr(0, 4)

    def load_kernel(self, dsp, path):
        """loadKernel FUN_10c3a7c0 -> image patch + upload FUN_10c20c60."""
        obj, _ = sc_decode.load(path)
        if not obj.flags & 2:
            raise LoaderError("%s is not an executable image" % path)
        syms = {s.name: s.value for s in obj.symbols if s.scnum > 0}
        secs = []
        for s in obj.sections:
            if s.size <= 0:
                continue
            data = bytearray(s.data)
            ws = s.wordsize
            nwords = s.size // ws

            def at(name):
                v = syms.get(name)
                if v is not None and s.vaddr <= v < s.vaddr + nwords:
                    return (v - s.vaddr) * ws
                return None

            if s.space == "PM":
                o = at("loadPX1")
                if o is not None:                       # FUN_10c2d260
                    data[o + 3] = 0x0A if dsp == NUM_DSP - 1 else 0x08
                o = at("call_serCommSetDMA")
                if o is not None:                       # FUN_10c2d2e0 (no satellites)
                    data[o] = data[o + 1] = 0
            else:
                o = at("cmdMask")
                if o is not None:                       # FUN_10c2d2a0 (board+0xac4 = 1)
                    data[o:o + 4] = bytes((0xC0, 0, 0, 0))
                o = at("dspID")
                if o is not None:
                    data[o + 3] = dsp
            secs.append((s, bytes(data), nwords))

        if "sysMsg" not in syms:
            raise LoaderError("%s has no sysMsg symbol" % path)
        self.sysmsg_addr[dsp] = syms["sysMsg"]
        if self.verbose:
            print("DSP%d: %s" % (dsp, os.path.basename(path)))
        for s, data, nwords in secs:
            if self.verbose:
                print("  %-8s %s 0x%05x %4d words" % (s.name, s.space, s.vaddr, nwords))
            if s.space == "PM":
                self.upload_code(dsp, data, s.vaddr, nwords)
            else:
                words = [int.from_bytes(data[5 * i:5 * i + 4], "big") for i in range(nwords)]
                self.upload_data(dsp, words, s.vaddr)
        return syms

    def run(self, bus_master=False):
        """FUN_10c32f80 up to the dspID round-trip."""
        for dsp in range(NUM_DSP):                     # FUN_10c20990 -> FUN_10c3aa50 (no seg_stak)
            self.upload_data(dsp, [1], DM_START + DM_SIZE - 1)
            self.state[dsp] = 2
        self.sleep(10)                                 # vt+0x21c FUN_10c30a20
        self.iop(0, 0x1C, 0x2040)
        self.iop(0, 0x41, 2)
        self.iop(0, 0x42, 0xFFFFFFFF)
        self.iop(0, 0x1C, 0x41)
        if bus_master:                                 # cfg enablePCIMaster
            for k in range(2, 0x400):                  # vt+0x1d0: bar[0x20002..] = 0
                self.bar.wr(SRAM + k, 0)
            self.bit_set(0, 0x80)
        self.mask_set(1, 0, 0x2B)
        self.bit_set(0, 0x40)
        self.bit_clr(0, 0x08)
        self.sleep(5)
        self.bit_set(2, 2)                             # startClk(0) FUN_10c22380
        self.sleep(2)
        self.bit_set(2, 4)
        self.bit_set(0, 0x10)
        self.sleep(2)
        self.bit_clr(2, 4)
        self.sleep(2)


# --------------------------------------------------------------------------- commands

def cmd_info(args):
    bar = open_bar(args)
    raw = bar.rd(0)
    print("BAR access : %s" % bar.path)
    print("board id   : 0x%08x  (id %d, hwRev %d)" % (raw, (raw >> 8) & 0x1F, (raw >> 13) & 7))
    for i in (0, 2, 4):
        print("reg%d @0x%02x : 0x%08x  %s" % (i, i * 4, bar.rd(i), REG_NAMES[i]))
    print("sample cnt : 0x%08x" % bar.rd(4))
    print("SRAM[0..1] : 0x%08x 0x%08x" % (bar.rd(SRAM), bar.rd(SRAM + 1)))
    print("ack mbox   : " + " ".join("%08x/%08x" % (bar.rd(SRAM + ACK_BASE + 2 * d),
                                                    bar.rd(SRAM + ACK_BASE + 2 * d + 1))
                                         for d in range(NUM_DSP)))


def cmd_diag(args):
    """Read-only health check, modelled on Sim2k's ack-timeout diagnosis (FUN_10c22650)."""
    bar = open_bar(args)
    r0 = bar.rd(0)
    if r0 & 0x30:
        where = "stalled at Cebulon (%s)" % ("async" if r0 & 0x20 else "sync")
    else:
        where = "stalled at DSP %d" % (r0 & 0x3F)
    seen = set()
    for _ in range(1000):
        seen.add(bar.rd(0) & 0x3F)
    if len(seen) > 1:
        where = "not stalled"
    print("reg0       : 0x%08x  -> communication %s (low bits seen: %s)"
          % (r0, where, " ".join("%02x" % v for v in sorted(seen))))
    print("fifo rd    : 0x%03x" % (bar.rd(2) & 0x3FF))
    c0 = bar.rd(4)
    t0 = time.monotonic()
    snap0 = [bar.rd(SRAM + k) for k in range(0x1000)]
    time.sleep(1.0)
    c1 = bar.rd(4)
    dt = time.monotonic() - t0
    snap1 = [bar.rd(SRAM + k) for k in range(0x1000)]
    print("sample cnt : 0x%08x -> 0x%08x  (%.0f Hz)" % (c0, c1, ((c1 - c0) & 0xFFFFFFFF) / dt))
    nz = [k for k in range(0x1000) if snap1[k]]
    print("SRAM 0x80000..0x83fff: %d non-zero dwords" % len(nz))
    for k in nz[:48]:
        print("  BAR+0x%05x = 0x%08x" % (0x80000 + 4 * k, snap1[k]))
    ch = [k for k in range(0x1000) if snap0[k] != snap1[k]]
    print("changed within 1 s: %d dwords" % len(ch))
    for k in ch[:32]:
        print("  BAR+0x%05x : 0x%08x -> 0x%08x" % (0x80000 + 4 * k, snap0[k], snap1[k]))


def cmd_dump(args):
    """Save regs 0..9 and the shared SRAM (BAR+0x80000..0x83fff) as text, one dword per line."""
    bar = open_bar(args)
    out = args.out or "pulsar_dump.txt"
    with open(out, "w") as f:
        for i in range(10):
            f.write("REG %d %08x\n" % (i, bar.rd(i)))
        for k in range(0x1000):
            f.write("%05x %08x\n" % (0x80000 + 4 * k, bar.rd(SRAM + k)))
    print("dumped regs + SRAM to %s" % out)


def cmd_peek(args):
    """GetValue on DSPs that are already running (after a successful boot)."""
    bar = open_bar(args)
    b = Board(bar, verbose=args.verbose, dry_run=args.dry_run)
    b.fifo_init(bar.rd(3))                 # continue from the card's current write index
    b.state = [2] * NUM_DSP
    allsyms = {}
    for dsp in range(NUM_DSP):
        obj, _ = sc_decode.load(os.path.join(args.dsp_dir, "puls2os%d.21k" % dsp))
        allsyms[dsp] = {s.name: s.value for s in obj.symbols if s.scnum > 0}
        b.sysmsg_addr[dsp] = allsyms[dsp]["sysMsg"]
    for rep in range(args.repeat):
        if rep:
            time.sleep(1.0)
            print("--- %d s later" % rep)
        _peek_once(b, args, allsyms)


def _peek_once(b, args, allsyms):
    for dsp in args.dsp:
        syms = allsyms[dsp]
        for name in args.sym:
            if ":" in name:
                lo, hi = (int(x, 0) for x in name.split(":"))
                vals = [b.get_value(dsp, a) for a in range(lo, hi)]
                print("DSP%d: 0x%05x: %s" % (dsp, lo, " ".join("%08x" % v for v in vals)))
                continue
            addr = int(name, 0) if name[0].isdigit() else syms.get(name)
            if addr is None:
                print("DSP%d: %-14s (no such symbol)" % (dsp, name))
                continue
            print("DSP%d: %-14s @0x%05x = 0x%08x" % (dsp, name, addr, b.get_value(dsp, addr)))


def cmd_clock(args):
    """Measure the DSP word-clock counter (OS symbol wclk) against BAR+0x10 over ~2 s."""
    bar = open_bar(args)
    b = Board(bar, dry_run=args.dry_run)
    b.fifo_init(bar.rd(3))
    b.state = [2] * NUM_DSP
    obj, _ = sc_decode.load(os.path.join(args.dsp_dir, "puls2os0.21k"))
    syms = {s.name: s.value for s in obj.symbols if s.scnum > 0}
    b.sysmsg_addr[0] = syms["sysMsg"]
    samples = []
    for _ in range(3):
        t = time.monotonic()
        w = b.get_value(0, syms["wclk"])
        samples.append((t, w, bar.rd(4)))
        time.sleep(1.0)
    (t0, w0, c0), (t1, w1, c1) = samples[0], samples[-1]
    dt = t1 - t0
    print("wclk      : 0x%x -> 0x%x  = %.1f /s" % (w0, w1, ((w1 - w0) & 0xFFFFFFFF) / dt))
    print("BAR+0x10  : 0x%x -> 0x%x  = %.1f /s" % (c0, c1, ((c1 - c0) & 0xFFFFFFFF) / dt))


def play_tone(b, dsp_dir, freq, rate, volume_db=-30.0):
    """Audible test (docs/io_format.md 5.3): P2_AINIT on DSP0; CSineR4 -> LINVOL -> P2_ANO L/R on DSP1."""
    import pulsar_modules as pm
    rack = pm.Rack(dsp_dir)
    _, ops = rack.load(os.path.join(dsp_dir, "P2_AINIT.dsp"), 0)       # pulls in PINIT.ol (codec init)
    ano, o = rack.load(os.path.join(dsp_dir, "P2_ANO.dsp"), 1)         # pulls in P2_IO.ol (SPORT0)
    ops += o
    sine, o = rack.load(os.path.join(dsp_dir, "CSineR4.dsp"), 1)
    ops += o
    vol, o = rack.load(os.path.join(dsp_dir, "LINVOL.dsp"), 1)         # Out = In * Vol (1.31 fraction)
    ops += o
    gain = min(0x7FFFFFFF, int(round(10 ** (volume_db / 20.0) * 0x7FFFFFFF)))
    ops += rack.connect(sine, 0, vol, 0)                                # Cout -> LINVOL.In
    ops += rack.set_in_pad(vol, 1, gain)                                # LINVOL.Vol
    ops += rack.connect(vol, 0, ano, 0)                                 # LINVOL.Out -> LIn
    ops += rack.connect(vol, 0, ano, 1)                                 # LINVOL.Out -> RIn
    ops += rack.set_in_pad(sine, 0, int(round(freq / rate * 2 ** 32)) & 0xFFFFFFFF)
    if b.verbose:
        print(pm.format_ops(ops))
    pm.execute(b, ops)
    b.plate_set(RATE_BITS.get(rate, 0x4))                              # cfg without 0x80: un-mute analog outs
    print("  playing %g Hz at %.1f dB on analog out 1/2 (P2_AINIT@DSP0, P2_ANO+CSineR4+LINVOL@DSP1)"
          % (freq, volume_db))


PLAY_SLOTS = (0x180, 0x181)          # PC playback slots -> DSP DM 0xC300 / 0xC302 (docs/pcm_streaming.md 3.1)


def set_route(bar, rate, block, play_slots=(), cap_slots=()):
    """PULSAR_IOCTL_SET_ROUTE: tell snd-pulsar which slots carry which ALSA channel."""
    import fcntl
    ps = list(play_slots) + [0] * (8 - len(play_slots))
    cs = list(cap_slots) + [0] * (8 - len(cap_slots))
    buf = struct.pack("<II8H8HIII", rate, block, *ps, *cs, len(play_slots), len(cap_slots), 0)
    req = (1 << 30) | (len(buf) << 16) | (ord("P") << 8) | 0x02   # _IOW('P', 2, struct pulsar_pcm_route)
    if isinstance(bar, Bar):
        fcntl.ioctl(bar.fd, req, buf)


def set_controls(bar, controls):
    """PULSAR_IOCTL_SET_CONTROLS: controls = [(name, dsp, [addrL, addrR], init_db), ...]."""
    import fcntl
    buf = struct.pack("<I", len(controls))
    for name, dsp, addrs, init_db in controls:
        a = list(addrs) + [0] * (2 - len(addrs))
        buf += struct.pack("<44sIIIIi", name.encode()[:43], dsp, len(addrs), a[0], a[1], int(round(init_db * 100)))
    buf += bytes(4 + 4 * 64 - len(buf))                               # struct pulsar_controls: 4 x 64-byte entries
    req = (1 << 30) | (len(buf) << 16) | (ord("P") << 8) | 0x03     # _IOW('P', 3, struct pulsar_controls)
    if isinstance(bar, Bar):
        fcntl.ioctl(bar.fd, req, buf)


def _gain(db):
    return min(0x7FFFFFFF, int(round(10 ** (db / 20.0) * 0x7FFFFFFF)))


def setup_pcm(b, dsp_dir, rate, volume_db=-30.0, block=1024, monitor_db=None, rack=None):
    """Analog graph on DSP1, then SET_ROUTE (docs/pcm_streaming.md 5.6):
         PC slot 0x180/0x181 -> LINVOL (volume_db) ---------------------> P2_ANO L/R
         P2_ANI L/R -> capture slots (to the host)
       with monitor_db (direct monitoring, no PC latency), per channel:
         PC  -> LINVOL (volume_db + 6) ---\
                                          ADD2N ((a+b)/2) -> P2_ANO
         ANI -> LINVOL (monitor_db + 6) --/
    """
    import pulsar_modules as pm
    rack = rack or pm.Rack(dsp_dir)
    mod = lambda name, dsp=1: rack.load(os.path.join(dsp_dir, name), dsp)
    ainit, ops = mod("P2_AINIT.dsp", 0)
    ano, o = mod("P2_ANO.dsp")
    ops += o
    # capture: P2_ANI LOut/ROut -> own comm slots in DSP1's sync block, copied to the host by the card
    ani, o = mod("P2_ANI.dsp")
    ops += o
    cap_slots = []
    for j in range(2):
        addr, o = rack.dsp[1].alloc_sync_output(ani, j)
        ops += o
        cap_slots.append((addr - 0xC000) // 2)
    mix_comp = 6.0 if monitor_db is not None else 0.0                  # ADD2N halves both inputs
    pc_vols, mon_vols, adds = [], [], []
    for ch, slot in enumerate(PLAY_SLOTS):
        pc, o = mod("LINVOL.dsp")
        ops += o
        ops += rack.dsp[1].link_input(pc, 0, 0xC000 + 2 * slot)        # signal <- PC slot
        ops += rack.set_in_pad(pc, 1, _gain(volume_db + mix_comp))
        pc_vols.append(pc)
        if monitor_db is None:
            ops += rack.connect(pc, 0, ano, ch)
            continue
        mon, o = mod("LINVOL.dsp")
        ops += o
        ops += rack.connect(ani, ch, mon, 0)                            # signal <- analog in
        ops += rack.set_in_pad(mon, 1, _gain(monitor_db + mix_comp))
        mon_vols.append(mon)
        add, o = mod("ADD2N.dsp")
        ops += o
        adds.append(add)
        ops += rack.connect(pc, 0, add, 0) + rack.connect(mon, 0, add, 1) + rack.connect(add, 0, ano, ch)
    if b.verbose:
        print(pm.format_ops(ops))
    pm.execute(b, ops)
    b.plate_set(RATE_BITS.get(rate, 0x4))                              # un-mute analog outs
    set_route(b.bar, rate, block, PLAY_SLOTS, cap_slots)
    # mixer controls = LINVOL gains (alsamixer: "DSP Out", "Input Monitor"); not named "PCM" on purpose,
    # so PipeWire keeps its software volume and does not lift the safety gain
    ctls = [("DSP Out Playback Volume", 1, [m.value_slots[1] for m in pc_vols], volume_db + mix_comp)]
    if mon_vols:
        ctls.append(("Input Monitor Playback Volume", 1, [m.value_slots[1] for m in mon_vols], monitor_db + mix_comp))
    set_controls(b.bar, ctls)
    if isinstance(b.bar, Bar):
        b.kernel_fifo = True                # the kernel now writes the FIFO too (mixer): go through it
    print("  playback: slots %s -> %.1f dB -> analog out 1/2"
          % (", ".join("0x%x" % s for s in PLAY_SLOTS), volume_db))
    print("  capture: analog in 1/2 (P2_ANI@DSP1) -> slots %s" % ", ".join("0x%x" % s for s in cap_slots))
    if monitor_db is not None:
        print("  direct monitor: analog in 1/2 -> analog out 1/2 at %.1f dB" % monitor_db)
    return {"rack": rack, "ainit": ainit, "ano": ano, "ani": ani, "pc_vols": pc_vols, "mon_vols": mon_vols,
            "adds": adds, "cap_slots": cap_slots}


def cmd_plate(args):
    """Rewrite the backplate audio-cfg word (PPlate, 11 bits) on a running card, then read the status.
    0x404 = 44.1 kHz internal, analog outputs live; 0x484 = same with bit 0x80 = analog outputs muted."""
    bar = open_bar(args)
    b = Board(bar, verbose=True, dry_run=args.dry_run)
    b.shadow[5] = 0x4                       # reg5 after open/reset (bit 0x1000 cleared, clkSrc default)
    b.write_audio_cfg(args.cfg)
    st = b.read_audio_cfg()
    print("readAudioCfg = 0x%04x%s%s" % (st, " lock(0x4)" if st & 4 else "", " HAVARIE(0x100)" if st & 0x100 else ""))


def hwdep_info(bar):
    """PULSAR_IOCTL_GET_INFO on the hwdep device (pulsar_uapi.h): raw_id, rev, bar_len, irq_count, last_status."""
    import fcntl
    if not isinstance(bar, Bar) or not bar.path.startswith("/dev/snd/"):
        return None
    buf = bytearray(32)
    req = (2 << 30) | (32 << 16) | (ord("P") << 8) | 0x01      # _IOR('P', 1, struct pulsar_info)
    fcntl.ioctl(bar.fd, req, buf)
    return struct.unpack("<8I", buf)


def cmd_counter(args):
    """Sample BAR+0x10 every 5 ms for 1 s (unwrapping), and measure the IRQ rate via the hwdep ioctl."""
    bar = open_bar(args)
    info0 = hwdep_info(bar)
    t0 = time.monotonic()
    samples = [(t0, bar.rd(4))]
    while time.monotonic() - t0 < 1.0:
        time.sleep(0.005)
        samples.append((time.monotonic(), bar.rd(4)))
    info1 = hwdep_info(bar)
    dt = samples[-1][0] - samples[0][0]
    total, wraps, maxv = 0, [], 0
    for (ta, a), (tb, b_) in zip(samples, samples[1:]):
        maxv = max(maxv, a, b_)
        if b_ >= a:
            total += b_ - a
        else:
            wraps.append(a)
    # unwrap with the observed modulus: next power of two above the max value seen
    mod = 1 << maxv.bit_length()
    total = sum(((b_ - a) % mod) for (_, a), (_, b_) in zip(samples, samples[1:]))
    steps = [((b_ - a) % mod) for (_, a), (_, b_) in zip(samples, samples[1:])]
    print("BAR+0x10   : %d samples in %.3f s, max value 0x%x -> modulus 0x%x, %d wraps"
          % (len(samples), dt, maxv, mod, len(wraps)))
    print("rate       : %.1f counts/s (step per 5 ms: min %d, max %d)" % (total / dt, min(steps), max(steps)))
    if info0 and info1:
        print("IRQ        : %d in %.3f s = %.2f /s, last status 0x%08x"
              % (info1[3] - info0[3], dt, (info1[3] - info0[3]) / dt, info1[4]))


def boot_card(args, bar=None):
    """Reset, load the six OS images, run, check every DSP, then clock/rate (and the --pcm/--tone graph).
    Returns (board, ok, graph) where graph is setup_pcm()'s handle dict or None."""
    bar = bar or open_bar(args)
    b = Board(bar, verbose=args.verbose, dry_run=args.dry_run)
    print("[1/5] open/init registers")
    b.open_init()
    print("[2/5] reset DSPs")
    b.reset()
    syms = {}
    print("[3/5] load DSP OS images")
    for dsp in range(NUM_DSP):
        path = os.path.join(args.dsp_dir, "puls2os%d.21k" % dsp)
        syms[dsp] = b.load_kernel(dsp, path)
    print("[4/5] run")
    b.run(bus_master=args.bus_master)
    print("[5/5] GetValue(dspID) round-trip")
    ok, graph = True, None
    for dsp in range(NUM_DSP):
        try:
            v = b.get_value(dsp, syms[dsp]["dspID"])
            print("  DSP%d: dspID -> 0x%08x %s" % (dsp, v, "OK" if (v & 0xFF) == dsp else "(unexpected)"))
        except LoaderError as e:
            ok = False
            print("  DSP%d: %s" % (dsp, e))
    b.syms = syms
    if ok and not args.no_finish:
        print("[6/6] finish run + sample rate %d (%s)" % (args.rate, args.clock))
        b.finish_run(irq=args.irq, rate=44100)
        if args.rate != 44100 or args.clock != "internal":
            b.set_rate(args.rate, args.clock)
        if args.pcm:
            if not (args.irq and args.bus_master):
                raise LoaderError("--pcm needs --irq and --bus-master")
            print("[pcm] playback graph + ALSA route")
            graph = setup_pcm(b, args.dsp_dir, args.rate, args.volume, monitor_db=args.monitor)
        elif args.tone:
            print("[tone] sine %g Hz, %.1f dB -> P-Plate analog out 1/2" % (args.tone, args.volume))
            play_tone(b, args.dsp_dir, args.tone, args.rate, args.volume)
    return b, ok, graph


def cmd_boot(args):
    b, ok, _ = boot_card(args)
    bar = b.bar
    print("FIFO wr=0x%x rd=0x%x, reg0=0x%08x" % (b.fifo_wr, bar.rd(2) & 0x3FF, bar.rd(0)))
    if args.dry_run:
        print("dry run: all BAR writes logged to %s" % args.log)
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("info", "diag", "dump", "peek", "clock", "plate", "counter", "boot"))
    ap.add_argument("--resource", help="mmap this file instead of the hwdep device "
                                       "(e.g. /sys/bus/pci/devices/0000:06:01.0/resource0)")
    ap.add_argument("--dsp-dir", default=DEFAULT_DSP_DIR)
    ap.add_argument("--out", help="output file for dump")
    ap.add_argument("--dsp", type=int, nargs="+", default=[0], help="DSPs for peek")
    ap.add_argument("--sym", nargs="+", default=["dspID"], help="symbols or addresses for peek (a:b = range)")
    ap.add_argument("--cfg", type=lambda x: int(x, 0), default=0x404, help="plate: cfg word (default 0x404)")
    ap.add_argument("--repeat", type=int, default=1, help="peek: read N times, 1 s apart")
    ap.add_argument("--dry-run", action="store_true", help="simulate against a fake BAR")
    ap.add_argument("--log", help="log every BAR write to this file (default for --dry-run: bar_writes.log)")
    ap.add_argument("--rate", type=int, default=44100, choices=(32000, 44100, 48000))
    ap.add_argument("--clock", default="internal", choices=("internal", "external"))
    ap.add_argument("--volume", type=float, default=-30.0, help="tone level in dBFS (default -30)")
    ap.add_argument("--monitor", type=float, help="with --pcm: direct monitoring of analog in 1/2 at this level (dB, max -6)")
    ap.add_argument("--pcm", action="store_true", help="after boot, build the PC playback graph and set the ALSA route")
    ap.add_argument("--tone", type=float, help="after boot, play a full-scale sine of this frequency on analog out 1/2")
    ap.add_argument("--irq", action="store_true", help="reg1 = 0x11 (block IRQ); needs the snd-pulsar ISR")
    ap.add_argument("--no-finish", action="store_true", help="stop after the dspID round-trip")
    ap.add_argument("--bus-master", action="store_true",
                    help="enable PCI bus-master (reg0 bit 0x80) like Windows; off by default "
                         "until host DMA buffers are set up")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    if args.dry_run and not args.log:
        args.log = "bar_writes.log"
    try:
        return {"info": cmd_info, "diag": cmd_diag, "dump": cmd_dump, "peek": cmd_peek, "clock": cmd_clock, "plate": cmd_plate, "counter": cmd_counter, "boot": cmd_boot}[args.command](args) or 0
    except LoaderError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
