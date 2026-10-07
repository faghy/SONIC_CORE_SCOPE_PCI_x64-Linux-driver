#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
# Decoding tables ported from MAME src/devices/cpu/sharc/sharc_dasm.cpp
#   license:BSD-3-Clause, copyright-holders:Ville Linde
#
# Redistribution and use in source and binary forms, with or without modification, are permitted provided
# that the following conditions are met:
# 1. Redistributions of source code must retain the above copyright notice, this list of conditions and
#    the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and
#    the following disclaimer in the documentation and/or other materials provided with the distribution.
# 3. Neither the name of the copyright holder nor the names of its contributors may be used to endorse or
#    promote products derived from this software without specific prior written permission.
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED
# WARRANTIES ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DAMAGES
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""
sharc_dis.py - disassembler for Analog Devices ADSP-2106x / ADSP-21065L (SHARC) 48-bit instructions.

Decoding tables are a Python port of MAME's src/devices/cpu/sharc/sharc_dasm.cpp
(Ville Linde, BSD-3-Clause), extended with:
  * symbol annotation from the COFF symbol table (sc_decode.py),
  * ADSP-21065L IOP register names for direct DM addresses < 0x100,
  * LA flag on JUMP/CALL, CJUMP/RFRAME, raw fallback for unknown compute ops,
  * absolute branch targets for PC-relative forms.

Usage:
  sharc_dis.py file.21k [--section NAME] [--from ADDR --to ADDR]
  sharc_dis.py --word 063E00008080           (decode single instruction words)

Output: `addr  hex  mnemonic  ; symbol / annotation`
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# --------------------------------------------------------------------------- register tables

UREG = ["???"] * 256
for _i in range(16):
    UREG[0x00 + _i] = "R%d" % _i
    UREG[0x10 + _i] = "I%d" % _i
    UREG[0x20 + _i] = "M%d" % _i
    UREG[0x30 + _i] = "L%d" % _i
    UREG[0x40 + _i] = "B%d" % _i
for _k, _v in {0x60: "FADDR", 0x61: "DADDR", 0x63: "PC", 0x64: "PCSTK", 0x65: "PCSTKP",
               0x66: "LADDR", 0x67: "CURLCNTR", 0x68: "LCNTR",
               0x70: "USTAT1", 0x71: "USTAT2", 0x79: "IRPTL", 0x7A: "MODE2", 0x7B: "MODE1",
               0x7C: "ASTAT", 0x7D: "IMASK", 0x7E: "STKY", 0x7F: "IMASKP",
               0xDB: "PX", 0xDC: "PX1", 0xDD: "PX2", 0xDE: "TPERIOD", 0xDF: "TCOUNT"}.items():
    UREG[_k] = _v

BOPNAMES = ["SET", "CLR", "TGL", "???", "TST", "XOR", "???", "???"]

COND_IF = ["EQ", "LT", "LE", "AC", "AV", "MV", "MS", "SV", "SZ", "FLAG0_IN", "FLAG1_IN",
           "FLAG2_IN", "FLAG3_IN", "TF", "BM", "NOT LCE", "NE", "GE", "GT", "NOT AC", "NOT AV",
           "NOT MV", "NOT MS", "NOT SV", "NOT SZ", "NOT FLAG0_IN", "NOT FLAG1_IN",
           "NOT FLAG2_IN", "NOT FLAG3_IN", "NOT TF", "NBM", ""]
COND_DO = COND_IF[:15] + ["LCE"] + COND_IF[16:31] + ["FOREVER"]

MR_REGS = ["MR0F", "MR1F", "MR2F", "MR0B", "MR1B", "MR2B"] + ["???"] * 10

# ADSP-21065L IOP registers (DM 0x0000-0x00FF), from the ADSP-21065L SHARC Technical Reference,
# Appendix E (IOP register table). DMA channel 8 = EPB0 (DMAC0/IIEP0..), channel 9 = EPB1.
IOP = {
    0x00: "SYSCON", 0x01: "VIRPT", 0x02: "WAIT", 0x03: "SYSTAT", 0x04: "EPB0", 0x05: "EPB1",
    0x08: "MSGR0", 0x09: "MSGR1", 0x0A: "MSGR2", 0x0B: "MSGR3",
    0x0C: "MSGR4", 0x0D: "MSGR5", 0x0E: "MSGR6", 0x0F: "MSGR7",
    0x18: "BMAX", 0x19: "BCNT", 0x1C: "DMAC0", 0x1D: "DMAC1", 0x20: "SDRDIV",
    0x28: "TPERIOD0", 0x29: "TPWIDTH0", 0x2A: "TCOUNT0",
    0x2B: "TPERIOD1", 0x2C: "TPWIDTH1", 0x2D: "TCOUNT1", 0x2E: "IOCTL", 0x2F: "IOSTAT",
    0x30: "IIR0B", 0x31: "IMR0B", 0x32: "CR0B", 0x33: "CPR0B", 0x34: "GPR0B", 0x37: "DMASTAT",
    0x38: "IIR1B", 0x39: "IMR1B", 0x3A: "CR1B", 0x3B: "CPR1B", 0x3C: "GPR1B",
    0x40: "IIEP0", 0x41: "IMEP0", 0x42: "CEP0", 0x43: "CPEP0",
    0x44: "GPEP0", 0x45: "EIEP0", 0x46: "EMEP0", 0x47: "ECEP0",
    0x48: "IIEP1", 0x49: "IMEP1", 0x4A: "CEP1", 0x4B: "CPEP1",
    0x4C: "GPEP1", 0x4D: "EIEP1", 0x4E: "EMEP1", 0x4F: "ECEP1",
    0x50: "IIT0B", 0x51: "IMT0B", 0x52: "CT0B", 0x53: "CPT0B", 0x54: "GPT0B",
    0x58: "IIT1B", 0x59: "IMT1B", 0x5A: "CT1B", 0x5B: "CPT1B", 0x5C: "GPT1B",
    0x60: "IIR0A", 0x61: "IMR0A", 0x62: "CR0A", 0x63: "CPR0A", 0x64: "GPR0A",
    0x68: "IIR1A", 0x69: "IMR1A", 0x6A: "CR1A", 0x6B: "CPR1A", 0x6C: "GPR1A",
    0x70: "IIT0A", 0x71: "IMT0A", 0x72: "CT0A", 0x73: "CPT0A", 0x74: "GPT0A",
    0x78: "IIT1A", 0x79: "IMT1A", 0x7A: "CT1A", 0x7B: "CPT1A", 0x7C: "GPT1A",
    0xE0: "STCTL0", 0xE1: "SRCTL0", 0xE2: "TX0_A", 0xE3: "RX0_A", 0xE4: "TDIV0", 0xE6: "RDIV0",
    0xE8: "MTCS0", 0xE9: "MRCS0", 0xEA: "MTCCS0", 0xEB: "MRCCS0", 0xEC: "KEYWD0", 0xED: "KEYMASK0",
    0xEE: "TX0_B", 0xEF: "RX0_B",
    0xF0: "STCTL1", 0xF1: "SRCTL1", 0xF2: "TX1_A", 0xF3: "RX1_A", 0xF4: "TDIV1", 0xF6: "RDIV1",
    0xF8: "MTCS1", 0xF9: "MRCS1", 0xFA: "MTCCS1", 0xFB: "MRCCS1", 0xFC: "KEYWD1", 0xFD: "KEYMASK1",
    0xFE: "TX1_B", 0xFF: "RX1_B",
}

# DMACx (0x1C/0x1D) bits, IOCTL (0x2E) bits
DMAC_BITS = {0: "DEN", 1: "CHEN", 2: "TRAN", 5: "DTYPE", 8: "MSWF", 9: "MASTER", 10: "HSHAKE",
             11: "INTIO", 12: "EXTERN", 13: "FLSH"}
IOCTL_BITS = dict([(i, "FLG%dO" % (i + 4)) for i in range(8)] + [(10, "DSDCTL"), (11, "DSDCK1"), (15, "SDSRF"),
                                                                    (24, "SDPM"), (28, "SDBUF"), (31, "SDPSS")])
SYSCON_BITS = {0: "SRST", 1: "BSO", 2: "IIVT", 6: "HMSWF", 7: "HPFLSH", 8: "IMDW0", 9: "IMDW1", 10: "ADREDY",
               11: "BHD", 18: "DCPR"}
MODE2_BITS = {0: "IRQ0E", 1: "IRQ1E", 2: "IRQ2E", 3: "PERIOD_CNT0", 4: "CADIS", 5: "TIMEN0", 6: "BUSLK",
              7: "PWMOUT0", 8: "INT_HI0", 9: "PULSE_HI0", 10: "PERIOD_CNT1", 11: "TIMEN1", 12: "PWMOUT1",
              13: "INT_HI1", 14: "PULSE_HI1", 15: "FLG0O", 16: "FLG1O", 17: "FLG2O", 18: "FLG3O", 19: "CAFRZ"}


def sext(v, bits):
    v &= (1 << bits) - 1
    return v - (1 << bits) if v & (1 << (bits - 1)) else v


def dag1(kind, i):
    return "%s%d" % (kind, i & 7)


def dag2(kind, i):
    return "%s%d" % (kind, 8 + (i & 7))


def ifcond(c):
    return "" if c == 31 else "IF %s " % COND_IF[c]


# --------------------------------------------------------------------------- compute field

def compute(op):
    opc = (op >> 12) & 0xFF
    cu = (op >> 20) & 3
    rn = (op >> 8) & 0xF
    rx = (op >> 4) & 0xF
    ry = op & 0xF
    rs = (op >> 12) & 0xF
    ra, rm = rn, rs
    if op & 0x400000:  # multifunction
        mop = (op >> 16) & 0x3F
        rxm, rym, rxa, rya = (op >> 6) & 3, ((op >> 4) & 3) + 4, ((op >> 2) & 3) + 8, (op & 3) + 12
        rxm_ = rxm
        tbl = {
            0x04: "R%d = R%d * R%d (SSFR), R%d = R%d + R%d", 0x05: "R%d = R%d * R%d (SSFR), R%d = R%d - R%d",
            0x06: "R%d = R%d * R%d (SSFR), R%d = (R%d + R%d)/2",
            0x0C: "R%d = MRF + R%d * R%d (SSFR), R%d = R%d + R%d",
            0x0D: "R%d = MRF + R%d * R%d (SSFR), R%d = R%d - R%d",
            0x0E: "R%d = MRF + R%d * R%d (SSFR), R%d = (R%d + R%d)/2",
            0x14: "R%d = MRF - R%d * R%d (SSFR), R%d = R%d + R%d",
            0x15: "R%d = MRF - R%d * R%d (SSFR), R%d = R%d - R%d",
            0x16: "R%d = MRF - R%d * R%d (SSFR), R%d = (R%d + R%d)/2",
            0x18: "F%d = F%d * F%d, F%d = F%d + F%d", 0x19: "F%d = F%d * F%d, F%d = F%d - F%d",
            0x1A: "F%d = F%d * F%d, F%d = FLOAT R%d BY R%d", 0x1B: "F%d = F%d * F%d, R%d = FIX F%d BY R%d",
            0x1C: "F%d = F%d * F%d, F%d = (F%d + F%d)/2", 0x1E: "F%d = F%d * F%d, F%d = MAX(F%d, F%d)",
            0x1F: "F%d = F%d * F%d, F%d = MIN(F%d, F%d)",
        }
        tbl_mrf = {
            0x08: "MRF = MRF + R%d * R%d (SSF), R%d = R%d + R%d",
            0x09: "MRF = MRF + R%d * R%d (SSF), R%d = R%d - R%d",
            0x0A: "MRF = MRF + R%d * R%d (SSF), R%d = (R%d + R%d)/2",
            0x10: "MRF = MRF - R%d * R%d (SSF), R%d = R%d + R%d",
            0x11: "MRF = MRF - R%d * R%d (SSF), R%d = R%d - R%d",
            0x12: "MRF = MRF - R%d * R%d (SSF), R%d = (R%d + R%d)/2",
        }
        if mop in tbl:
            return tbl[mop] % (rm, rxm_, rym, ra, rxa, rya)
        if mop in tbl_mrf:
            return tbl_mrf[mop] % (rxm_, rym, ra, rxa, rya)
        if mop == 0x1D:
            return "F%d = F%d * F%d, F%d = ABS F%d" % (rm, rxm_, rym, ra, rxa)
        if 0x20 <= mop <= 0x2F:
            return "R%d = R%d * R%d (SSFR), R%d = R%d + R%d, R%d = R%d - R%d" % (
                rm, rxm_, rym, ra, rxa, rya, (op >> 16) & 0xF, rxa, rya)
        if 0x30 <= mop <= 0x3F:
            return "F%d = F%d * F%d, F%d = F%d + F%d, F%d = F%d - F%d" % (
                rm, rxm_, rym, ra, rxa, rya, (op >> 16) & 0xF, rxa, rya)
        if mop == 0x00:
            return "R%d = %s" % ((op >> 8) & 0xF, MR_REGS[(op >> 12) & 0xF])
        if mop == 0x01:
            return "%s = R%d" % (MR_REGS[(op >> 12) & 0xF], (op >> 8) & 0xF)
        return "<multifunc compute 0x%06x>" % op
    if cu == 0:
        A = {
            0x01: "R{n} = R{x} + R{y}", 0x02: "R{n} = R{x} - R{y}", 0x05: "R{n} = R{x} + R{y} + CI",
            0x06: "R{n} = R{x} - R{y} + CI - 1", 0x09: "R{n} = (R{x} + R{y})/2", 0x0A: "COMP(R{x}, R{y})",
            0x25: "R{n} = R{x} + CI", 0x26: "R{n} = R{x} + CI - 1", 0x29: "R{n} = R{x} + 1",
            0x2A: "R{n} = R{x} - 1", 0x22: "R{n} = -R{x}", 0x30: "R{n} = ABS R{x}", 0x21: "R{n} = PASS R{x}",
            0x40: "R{n} = R{x} AND R{y}", 0x41: "R{n} = R{x} OR R{y}", 0x42: "R{n} = R{x} XOR R{y}",
            0x43: "R{n} = NOT R{x}", 0x61: "R{n} = MIN(R{x}, R{y})", 0x62: "R{n} = MAX(R{x}, R{y})",
            0x63: "R{n} = CLIP R{x} BY R{y}",
            0x81: "F{n} = F{x} + F{y}", 0x82: "F{n} = F{x} - F{y}", 0x91: "F{n} = ABS(F{x} + F{y})",
            0x92: "F{n} = ABS(F{x} - F{y})", 0x89: "F{n} = (F{x} + F{y})/2", 0x8A: "COMP(F{x}, F{y})",
            0xA2: "F{n} = -F{x}", 0xB0: "F{n} = ABS F{x}", 0xA1: "F{n} = PASS F{x}", 0xA5: "F{n} = RND F{x}",
            0xBD: "F{n} = SCALB F{x} BY R{y}", 0xAD: "R{n} = MANT F{x}", 0xC1: "R{n} = LOGB F{x}",
            0xD9: "R{n} = FIX F{x} BY R{y}", 0xC9: "R{n} = FIX F{x}", 0xDD: "R{n} = TRUNC F{x} BY R{y}",
            0xCD: "R{n} = TRUNC F{x}", 0xDA: "F{n} = FLOAT R{x} BY R{y}", 0xCA: "F{n} = FLOAT R{x}",
            0xC4: "F{n} = RECIPS F{x}", 0xC5: "F{n} = RSQRTS F{x}", 0xE0: "F{n} = F{x} COPYSIGN F{y}",
            0xE1: "F{n} = MIN(F{x}, F{y})", 0xE2: "F{n} = MAX(F{x}, F{y})", 0xE3: "F{n} = CLIP F{x} BY F{y}",
        }
        if opc in A:
            return A[opc].format(n=rn, x=rx, y=ry)
        if 0x70 <= opc <= 0x7F:
            return "R%d = R%d + R%d, R%d = R%d - R%d" % (ra, rx, ry, rs, rx, ry)
        if 0xF0 <= opc <= 0xFF:
            return "F%d = F%d + F%d, F%d = F%d - F%d" % (ra, rx, ry, rs, rx, ry)
        return "<alu op 0x%02x rn=%d rx=%d ry=%d>" % (opc, rn, rx, ry)
    if cu == 1:
        if opc == 0x30:
            return "F%d = F%d * F%d" % (rn, rx, ry)
        dst = ["R%d = " % rn, "R%d = " % rn, "MRF = ", "MRB = "][(opc >> 1) & 3]
        mr = "MRB" if opc & 2 else "MRF"
        g = (opc >> 6) & 3
        mod = " (%s%s%s)" % ("U" if not opc & 0x20 else "S", "U" if not opc & 0x10 else "S",
                            "F" if opc & 0x08 else "I")
        if g == 0:
            s = (opc >> 4) & 3
            if s == 0:
                src = "SAT %s" % mr
            elif s == 1:
                src = ("RND %s" % mr) if opc & 8 else "0"
            else:
                src = "<mul op 0x%02x>" % opc
            return dst + src
        if g == 1:
            return dst + "R%d * R%d%s" % (rx, ry, mod)
        if g == 2:
            return dst + "%s + R%d * R%d%s" % (mr, rx, ry, mod)
        return dst + "%s - R%d * R%d%s" % (mr, rx, ry, mod)
    if cu == 2:
        S = {
            0x00: "R{n} = LSHIFT R{x} BY R{y}", 0x20: "R{n} = R{n} OR LSHIFT R{x} BY R{y}",
            0x04: "R{n} = ASHIFT R{x} BY R{y}", 0x24: "R{n} = R{n} OR ASHIFT R{x} BY R{y}",
            0x08: "R{n} = ROT R{x} BY R{y}", 0xC4: "R{n} = BCLR R{x} BY R{y}", 0xC0: "R{n} = BSET R{x} BY R{y}",
            0xC8: "R{n} = BTGL R{x} BY R{y}", 0xCC: "BTST R{x} BY R{y}", 0x44: "R{n} = FDEP R{x} BY R{y}",
            0x64: "R{n} = R{n} OR FDEP R{x} BY R{y}", 0x4C: "R{n} = FDEP R{x} BY R{y} (SE)",
            0x6C: "R{n} = R{n} OR FDEP R{x} BY R{y} (SE)", 0x40: "R{n} = FEXT R{x} BY R{y}",
            0x48: "R{n} = FEXT R{x} BY R{y} (SE)", 0x80: "R{n} = EXP R{x}", 0x84: "R{n} = EXP R{x} (EX)",
            0x88: "R{n} = LEFTZ R{x}", 0x8C: "R{n} = LEFTO R{x}", 0x90: "R{n} = FPACK F{x}",
            0x94: "F{n} = FUNPACK R{x}",
        }
        if opc in S:
            return S[opc].format(n=rn, x=rx, y=ry)
        return "<shift op 0x%02x>" % opc
    return "<compute 0x%06x>" % op


def shiftop(shift, data, rn, rx):
    d8 = sext(data, 8)
    b6, ln = data & 0x3F, (data >> 6) & 0x3F
    T = {
        0x00: "R%d = LSHIFT R%d BY %d" % (rn, rx, d8), 0x08: "R%d = R%d OR LSHIFT R%d BY %d" % (rn, rn, rx, d8),
        0x01: "R%d = ASHIFT R%d BY %d" % (rn, rx, d8), 0x09: "R%d = R%d OR ASHIFT R%d BY %d" % (rn, rn, rx, d8),
        0x02: "R%d = ROT R%d BY %d" % (rn, rx, d8), 0x31: "R%d = BCLR R%d BY %d" % (rn, rx, data & 0xFF),
        0x30: "R%d = BSET R%d BY %d" % (rn, rx, data & 0xFF), 0x32: "R%d = BTGL R%d BY %d" % (rn, rx, data & 0xFF),
        0x33: "BTST R%d BY %d" % (rx, data & 0xFF),
        0x11: "R%d = FDEP R%d BY %d:%d" % (rn, rx, b6, ln),
        0x19: "R%d = R%d OR FDEP R%d BY %d:%d" % (rn, rn, rx, b6, ln),
        0x13: "R%d = FDEP R%d BY %d:%d (SE)" % (rn, rx, b6, ln),
        0x1B: "R%d = R%d OR FDEP R%d BY %d:%d (SE)" % (rn, rn, rx, b6, ln),
        0x10: "R%d = FEXT R%d BY %d:%d" % (rn, rx, b6, ln), 0x12: "R%d = FEXT R%d BY %d:%d (SE)" % (rn, rx, b6, ln),
        0x20: "R%d = EXP R%d" % (rn, rx), 0x21: "R%d = EXP R%d (EX)" % (rn, rx), 0x22: "R%d = LEFTZ R%d" % (rn, rx),
        0x23: "R%d = LEFTO R%d" % (rn, rx), 0x24: "R%d = FPACK F%d" % (rn, rx), 0x25: "F%d = FUNPACK R%d" % (rn, rx),
    }
    return T.get(shift, "<shiftop 0x%02x>" % shift)


def with_comp(comp, s):
    return (compute(comp) + ", " + s) if comp else s


# --------------------------------------------------------------------------- instruction decoder

class Insn:
    """Decoded instruction: text plus control-flow and memory-reference info."""
    __slots__ = ("text", "targets", "kind", "cond", "dmaddr", "pmaddr", "imm", "ureg", "write")

    def __init__(self, text):
        self.text = text
        self.targets = []      # static branch / loop-end targets
        self.kind = None       # 'jump','call','ret','rti','do','idle'
        self.cond = 31
        self.dmaddr = None     # absolute DM address referenced
        self.pmaddr = None
        self.imm = None
        self.ureg = None
        self.write = None


def decode(pc, w):
    top = (w >> 40) & 0xFF
    comp = w & 0x7FFFFF
    # Type 1: compute + dual move
    if top & 0xE0 == 0x20:
        dmi, dmm, pmi, pmm = (w >> 41) & 7, (w >> 38) & 7, (w >> 30) & 7, (w >> 27) & 7
        dmdreg, pmdreg = (w >> 33) & 0xF, (w >> 23) & 0xF
        dmd, pmd = (w >> 44) & 1, (w >> 37) & 1
        a = ("DM(I%d,M%d) = R%d" % (dmi, dmm, dmdreg)) if dmd else ("R%d = DM(I%d,M%d)" % (dmdreg, dmi, dmm))
        b = ("PM(I%d,M%d) = R%d" % (8 + pmi, 8 + pmm, pmdreg)) if pmd else \
            ("R%d = PM(I%d,M%d)" % (pmdreg, 8 + pmi, 8 + pmm))
        return Insn(with_comp(comp, a + ", " + b))
    # Type 3: ureg <-> DM|PM (Ii, Mm)
    if top & 0xE0 == 0x40:
        cond, g, d, i, m, u = (w >> 33) & 0x1F, (w >> 32) & 1, (w >> 31) & 1, (w >> 41) & 7, (w >> 38) & 7, (w >> 44) & 1
        ureg = UREG[(w >> 23) & 0xFF]
        sp, ii, mm = ("PM", 8 + i, 8 + m) if g else ("DM", i, m)
        ea = ("%s(I%d,M%d)" % (sp, ii, mm)) if u else ("%s(M%d,I%d)" % (sp, mm, ii))
        s = ("%s = %s" % (ea, ureg)) if d else ("%s = %s" % (ureg, ea))
        r = Insn(ifcond(cond) + with_comp(comp, s))
        r.cond = cond
        return r
    # Type 4: dreg <-> DM|PM (Ii, imm6)
    if top & 0xF0 == 0x60:
        cond, g, d, i, u = (w >> 33) & 0x1F, (w >> 40) & 1, (w >> 39) & 1, (w >> 41) & 7, (w >> 38) & 1
        dreg = "R%d" % ((w >> 23) & 0xF)
        data = sext((w >> 27) & 0x3F, 6)
        sp, ii = ("PM", 8 + i) if g else ("DM", i)
        ea = ("%s(I%d,%d)" % (sp, ii, data)) if u else ("%s(%d,I%d)" % (sp, data, ii))
        s = ("%s = %s" % (ea, dreg)) if d else ("%s = %s" % (dreg, ea))
        r = Insn(ifcond(cond) + with_comp(comp, s))
        r.cond = cond
        return r
    # Type 5: ureg = ureg
    if top & 0xF0 == 0x70:
        cond = (w >> 31) & 0x1F
        s = "%s = %s" % (UREG[(w >> 23) & 0xFF], UREG[(w >> 36) & 0xFF])
        r = Insn(ifcond(cond) + with_comp(comp, s))
        r.cond = cond
        return r
    # Type 6: imm shift + dreg <-> DM|PM
    if top & 0xF0 == 0x80:
        cond, g, d, i, m = (w >> 33) & 0x1F, (w >> 32) & 1, (w >> 31) & 1, (w >> 41) & 7, (w >> 38) & 7
        rn, rx, sh = (w >> 4) & 0xF, w & 0xF, (w >> 16) & 0x3F
        data = (((w >> 27) & 0xF) << 8) | ((w >> 8) & 0xFF)
        dreg = "R%d" % ((w >> 23) & 0xF)
        sp, ii, mm = ("PM", 8 + i, 8 + m) if g else ("DM", i, m)
        mv = ("%s(I%d,M%d) = %s" % (sp, ii, mm, dreg)) if d else ("%s = %s(I%d,M%d)" % (dreg, sp, ii, mm))
        r = Insn(ifcond(cond) + shiftop(sh, data, rn, rx) + ", " + mv)
        r.cond = cond
        return r
    # Type 16: DM|PM(Ii,Mm) = imm32
    if top & 0xF0 == 0x90:
        g, i, m = (w >> 37) & 1, (w >> 41) & 7, (w >> 38) & 7
        sp, ii, mm = ("PM", 8 + i, 8 + m) if g else ("DM", i, m)
        r = Insn("%s(I%d,M%d) = 0x%08X" % (sp, ii, mm, w & 0xFFFFFFFF))
        r.imm = w & 0xFFFFFFFF
        return r
    # Type 15: ureg <-> DM|PM(imm32, Ii)
    if top & 0xE0 == 0xA0:
        d, g, i = (w >> 40) & 1, (w >> 44) & 1, (w >> 41) & 7
        ureg = UREG[(w >> 32) & 0xFF]
        sp, ii = ("PM", 8 + i) if g else ("DM", i)
        ea = "%s(0x%X,I%d)" % (sp, w & 0xFFFFFFFF, ii)
        return Insn(("%s = %s" % (ea, ureg)) if d else ("%s = %s" % (ureg, ea)))
    # Type 10: indirect jump | compute, dreg <-> DM
    if top & 0xC0 == 0xC0:
        d, cond = (w >> 44) & 1, (w >> 33) & 0x1F
        pmi, pmm, dmi, dmm = (w >> 30) & 7, (w >> 27) & 7, (w >> 41) & 7, (w >> 38) & 7
        dreg = "R%d" % ((w >> 23) & 0xF)
        r = Insn("")
        if w & (1 << 45):
            t = pc + sext((w >> 27) & 0x3F, 6)
            tgt = "(PC,%d)" % sext((w >> 27) & 0x3F, 6)
            r.targets.append(t)
        else:
            tgt = "(M%d,I%d)" % (8 + pmm, 8 + pmi)
        mv = ("%s = DM(I%d,M%d)" % (dreg, dmi, dmm)) if d else ("DM(I%d,M%d) = %s" % (dmi, dmm, dreg))
        r.text = ifcond(cond) + "JUMP " + tgt + ", ELSE " + with_comp(comp, mv)
        r.kind, r.cond = "jump", cond
        return r
    if top == 0x00:
        return Insn("IDLE") if w & (1 << 39) else Insn("NOP")
    if top == 0x01:  # Type 2
        cond = (w >> 33) & 0x1F
        r = Insn(ifcond(cond) + compute(comp) if comp else ifcond(cond) + "<null compute>")
        r.cond = cond
        return r
    if top == 0x02:  # Type 6 without data move
        cond = (w >> 33) & 0x1F
        rn, rx, sh = (w >> 4) & 0xF, w & 0xF, (w >> 16) & 0x3F
        data = (((w >> 27) & 0xF) << 8) | ((w >> 8) & 0xFF)
        r = Insn(ifcond(cond) + shiftop(sh, data, rn, rx))
        r.cond = cond
        return r
    if top == 0x04:  # Type 7: compute + modify
        cond, g, i, m = (w >> 33) & 0x1F, (w >> 38) & 1, (w >> 30) & 7, (w >> 27) & 7
        s = "MODIFY(I%d,M%d)" % ((8 + i, 8 + m) if g else (i, m))
        r = Insn(ifcond(cond) + with_comp(comp, s))
        r.cond = cond
        return r
    if top in (0x06, 0x07):  # Type 8: direct jump/call
        cond = (w >> 33) & 0x1F
        call = (w >> 39) & 1
        addr = w & 0xFFFFFF
        t = (pc + sext(addr, 24)) & 0xFFFFFF if top & 1 else addr
        fl = []
        if (w >> 26) & 1:
            fl.append("DB")
        if (w >> 25) & 1 or (w >> 38) & 1:
            fl.append("LA")
        if (w >> 24) & 1:
            fl.append("CI")
        r = Insn(ifcond(cond) + ("CALL" if call else "JUMP") + " 0x%05X" % t +
                 (" (%s)" % ",".join(fl) if fl else "") + (" [PC-rel]" if top & 1 else ""))
        r.targets.append(t)
        r.kind, r.cond = ("call" if call else "jump"), cond
        return r
    if top in (0x08, 0x09):  # Type 9: indirect jump/call | compute
        cond, call = (w >> 33) & 0x1F, (w >> 39) & 1
        j, e, ci, la = (w >> 26) & 1, (w >> 25) & 1, (w >> 24) & 1, (w >> 38) & 1
        r = Insn("")
        if top & 1:
            rel = sext((w >> 27) & 0x3F, 6)
            t = pc + rel
            tgt = "0x%05X [PC%+d]" % (t, rel)
            r.targets.append(t)
        else:
            tgt = "(M%d,I%d)" % (8 + ((w >> 27) & 7), 8 + ((w >> 30) & 7))
        fl = [f for f, b in (("DB", j), ("LA", la), ("CI", ci)) if b]
        s = ifcond(cond) + ("CALL " if call else "JUMP ") + tgt + (" (%s)" % ",".join(fl) if fl else "")
        if comp:
            s += ", " + ("ELSE " if e else "") + compute(comp)
        r.text = s
        r.kind, r.cond = ("call" if call else "jump"), cond
        return r
    if top in (0x0A, 0x0B):  # Type 11: RTS/RTI
        cond = (w >> 33) & 0x1F
        j, e, lr = (w >> 26) & 1, (w >> 25) & 1, (w >> 24) & 1
        fl = [f for f, b in (("DB", j), ("LR", lr)) if b]
        s = ifcond(cond) + ("RTI" if top & 1 else "RTS") + (" (%s)" % ",".join(fl) if fl else "")
        if comp:
            s += ", " + ("ELSE " if e else "") + compute(comp)
        r = Insn(s)
        r.kind, r.cond = ("rti" if top & 1 else "ret"), cond
        return r
    if top in (0x0C, 0x0D):  # Type 12: LCNTR = ..., DO addr UNTIL LCE
        t = (pc + sext(w & 0xFFFFFF, 24)) & 0xFFFFFF
        if top & 1:
            s = "LCNTR = %s, DO 0x%05X UNTIL LCE" % (UREG[(w >> 32) & 0xFF], t)
        else:
            s = "LCNTR = 0x%04X, DO 0x%05X UNTIL LCE" % ((w >> 24) & 0xFFFF, t)
        r = Insn(s)
        r.targets.append(t)
        r.kind = "do"
        return r
    if top == 0x0E:  # Type 13: DO addr UNTIL term
        t = (pc + sext(w & 0xFFFFFF, 24)) & 0xFFFFFF
        r = Insn("DO 0x%05X UNTIL %s" % (t, COND_DO[(w >> 33) & 0x1F]))
        r.targets.append(t)
        r.kind = "do"
        return r
    if top == 0x0F:  # Type 17: ureg = imm32
        r = Insn("%s = 0x%08X" % (UREG[(w >> 32) & 0xFF], w & 0xFFFFFFFF))
        r.imm, r.ureg = w & 0xFFFFFFFF, UREG[(w >> 32) & 0xFF]
        return r
    if 0x10 <= top <= 0x13:  # Type 14: ureg <-> DM|PM(addr32)
        d, g = (w >> 40) & 1, (w >> 41) & 1
        ureg = UREG[(w >> 32) & 0xFF]
        a = w & 0xFFFFFFFF
        sp = "PM" if g else "DM"
        r = Insn(("%s(0x%05X) = %s" % (sp, a, ureg)) if d else ("%s = %s(0x%05X)" % (ureg, sp, a)))
        if g:
            r.pmaddr = a
        else:
            r.dmaddr = a
        r.write, r.ureg = bool(d), ureg
        return r
    if top == 0x14:  # Type 18: BIT op sreg imm32
        r = Insn("BIT %s %s 0x%08X" % (BOPNAMES[(w >> 37) & 7], UREG[0x70 | ((w >> 32) & 0xF)], w & 0xFFFFFFFF))
        r.imm, r.ureg = w & 0xFFFFFFFF, UREG[0x70 | ((w >> 32) & 0xF)]
        return r
    if top == 0x16:  # Type 19: MODIFY/BITREV (Ii, imm32)
        g, i = (w >> 38) & 1, (w >> 32) & 7
        ii = 8 + i if g else i
        return Insn("%s (I%d, %d)" % ("BITREV" if w & (1 << 39) else "MODIFY", ii, sext(w, 32)))
    if top == 0x17:  # Type 20: push/pop stacks, flush cache
        names = [(39, "PUSH LOOP"), (38, "POP LOOP"), (37, "PUSH STS"), (36, "POP STS"),
                 (35, "PUSH PCSTK"), (34, "POP PCSTK"), (33, "FLUSH CACHE")]
        return Insn(", ".join(n for b, n in names if (w >> b) & 1) or "<type20 empty>")
    if top in (0x18, 0x19):  # Type 25: CJUMP / RFRAME
        if top == 0x19 or (w >> 40) & 1:
            return Insn("RFRAME")
        r = Insn("CJUMP 0x%05X (DB)" % (w & 0xFFFFFF))
        r.targets.append(w & 0xFFFFFF)
        r.kind = "call"
        return r
    return Insn("<invalid 0x%012X>" % w)


# --------------------------------------------------------------------------- symbol annotation

class SymTab:
    def __init__(self, obj=None):
        self.pm = {}
        self.dm = {}
        if obj is None:
            return
        for s in obj.symbols:
            if s.scnum <= 0 or s.scnum > len(obj.sections):
                continue
            sec = obj.sections[s.scnum - 1]
            if s.name.startswith(".") or s.name == sec.name:
                continue
            tbl = self.pm if sec.space == "PM" else self.dm
            tbl.setdefault(s.value, []).append(s.name)

    def name(self, addr, space):
        t = self.pm if space == "PM" else self.dm
        n = t.get(addr)
        return "/".join(n) if n else None

    def near(self, addr, space):
        t = self.pm if space == "PM" else self.dm
        best = None
        for a in t:
            if a <= addr and (best is None or a > best) and addr - a < 0x100:
                best = a
        if best is None:
            return None
        off = addr - best
        return "%s%s" % ("/".join(t[best]), "+%d" % off if off else "")


def annotate(ins, syms):
    notes = []
    for t in ins.targets:
        n = syms.name(t, "PM") or syms.near(t, "PM")
        if n:
            notes.append("-> %s" % n)
    if ins.dmaddr is not None:
        a = ins.dmaddr
        if a < 0x100:
            notes.append("IOP %s" % IOP.get(a, "0x%02x" % a))
        else:
            n = syms.name(a, "DM") or syms.near(a, "DM")
            if n:
                notes.append(n)
    if ins.pmaddr is not None:
        n = syms.name(ins.pmaddr, "PM") or syms.near(ins.pmaddr, "PM")
        if n:
            notes.append(n)
    if ins.imm is not None and ins.ureg and ins.ureg[0] in "IB" and ins.ureg not in ("IMASK", "IRPTL", "IMASKP"):
        n = syms.name(ins.imm, "DM") or syms.name(ins.imm, "PM")
        if n:
            notes.append("=&" + n)
        elif ins.imm < 0x100:
            notes.append("=IOP %s" % IOP.get(ins.imm, "0x%02x" % ins.imm))
    if ins.ureg in ("IMASK", "IRPTL", "IMASKP") and ins.imm is not None:
        notes.append(irq_bits(ins.imm))
    if ins.ureg == "MODE1" and ins.imm is not None:
        notes.append(mode1_bits(ins.imm))
    if ins.ureg == "MODE2" and ins.imm is not None:
        notes.append(bitnames(ins.imm, MODE2_BITS))
    return notes


# ADSP-21065L interrupt latch bits (IRPTL/IMASK). Vector address = 0x8000 + 4*bit.
IRQ_BITS_21065L = {
    1: "RSTI", 3: "SOVFI", 4: "TMZHI", 5: "VIRPTI", 6: "IRQ2I", 7: "IRQ1I", 8: "IRQ0I",
    10: "SPR0I", 11: "SPR1I", 12: "SPT0I", 13: "SPT1I", 16: "EP0I", 17: "EP1I",
    21: "CB7I", 22: "CB15I", 23: "TMZLI", 24: "FIXI", 25: "FLTOI", 26: "FLTUI", 27: "FLTII",
    28: "SFT0I", 29: "SFT1I", 30: "SFT2I", 31: "SFT3I",
}

MODE1_BITS = {1: "BR0", 2: "SRCU", 3: "SRD1H", 4: "SRD1L", 5: "SRD2H", 6: "SRD2L", 7: "SRRFH",
              10: "SRRFL", 11: "NESTM", 12: "IRPTEN", 13: "ALUSAT", 14: "SSE", 15: "TRUNC",
              16: "RND32", 17: "CSEL0", 18: "CSEL1", 0: "BR8"}


def bitnames(v, tbl):
    return "{" + " ".join(tbl.get(b, "b%d" % b) for b in range(32) if v >> b & 1) + "}"


def describe_iop_value(reg, v):
    if reg in (0x1C, 0x1D):
        pm = ["PMODE=00", "PMODE=01(32b)", "PMODE=10(48b)", "PMODE=11(48b)"][(v >> 6) & 3]
        return bitnames(v & ~0xC0, DMAC_BITS)[:-1] + " " + pm + "}"
    if reg == 0x2E:
        return bitnames(v, IOCTL_BITS)
    if reg == 0x00:
        return bitnames(v & ~0x30, SYSCON_BITS)[:-1] + " HBW=%d}" % ((v >> 4) & 3)
    return None


def irq_bits(v):
    return "{" + " ".join(IRQ_BITS_21065L.get(b, "b%d" % b) for b in range(32) if v >> b & 1) + "}"


def mode1_bits(v):
    return "{" + " ".join(MODE1_BITS.get(b, "b%d" % b) for b in range(32) if v >> b & 1) + "}"


def fmt_line(addr, w, ins, syms, consts=None):
    lbl = syms.name(addr, "PM")
    out = []
    if lbl:
        out.append("%s:" % lbl)
        if consts is not None:
            consts.clear()
    notes = annotate(ins, syms)
    if consts is not None:
        # value tracking for IOP stores: last 'ureg = imm32' in straight-line code (heuristic)
        if ins.dmaddr is not None and ins.dmaddr < 0x100 and ins.write and ins.ureg in consts:
            v = consts[ins.ureg]
            d = describe_iop_value(ins.dmaddr, v)
            notes.append("<- 0x%X%s" % (v, (" " + d) if d else ""))
        if ins.text.startswith(("R", "M", "I", "L", "B")) and ins.imm is not None and ins.ureg \
                and ins.dmaddr is None and " = 0x" in ins.text and "(" not in ins.text:
            consts[ins.ureg] = ins.imm
        elif ins.ureg and ins.dmaddr is not None and not ins.write:
            consts.pop(ins.ureg, None)
        if ins.kind in ("call", "jump", "ret", "rti"):
            consts.clear()
    out.append("%05X  %012X  %-58s%s" % (addr, w, ins.text, ("; " + "; ".join(notes)) if notes else ""))
    return "\n".join(out)


def disasm_words(words, base, syms, lo=None, hi=None, out=sys.stdout):
    consts = {}
    for k, w in enumerate(words):
        a = base + k
        if lo is not None and a < lo:
            continue
        if hi is not None and a > hi:
            break
        out.write(fmt_line(a, w, decode(a, w), syms, consts) + "\n")


def main():
    ap = argparse.ArgumentParser(description="ADSP-2106x/21065L SHARC disassembler")
    ap.add_argument("file", nargs="?")
    ap.add_argument("--section", help="only this section (default: all PM sections)")
    ap.add_argument("--from", dest="lo", type=lambda s: int(s, 0))
    ap.add_argument("--to", dest="hi", type=lambda s: int(s, 0))
    ap.add_argument("--word", nargs="*", help="decode raw 48-bit hex words")
    ap.add_argument("--pc", type=lambda s: int(s, 0), default=0, help="PC for --word")
    ap.add_argument("--dm", action="store_true", help="also dump DM sections as data with symbols")
    a = ap.parse_args()
    if a.word:
        for k, h in enumerate(a.word):
            w = int(h, 16)
            print(fmt_line(a.pc + k, w, decode(a.pc + k, w), SymTab()))
        return 0
    if not a.file:
        ap.error("need a file or --word")
    import sc_decode
    obj, _ = sc_decode.load(a.file)
    syms = SymTab(obj)
    for s in obj.sections:
        if a.section and s.name != a.section:
            continue
        if s.space == "PM":
            print("; ---- section %s PM 0x%05X..0x%05X (%d words)" % (s.name, s.vaddr, s.vaddr + s.size // 6 - 1,
                                                                    s.size // 6))
            disasm_words(list(obj.words(s)), s.vaddr, syms, a.lo, a.hi)
        elif a.dm or (a.section and s.name == a.section):
            print("; ---- section %s DM 0x%05X (%d words)" % (s.name, s.vaddr, s.size // 5))
            for k, w in enumerate(obj.words(s)):
                ad = s.vaddr + k
                if (a.lo is not None and ad < a.lo) or (a.hi is not None and ad > a.hi):
                    continue
                n = syms.name(ad, "DM")
                print("%05X  %010X  %s" % (ad, w, n or ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
