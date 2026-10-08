"""
pulsar_values - how a DSP pad value is encoded, and how a user-facing value (with unit and curve) maps to it.

A DSP module input is a 32-bit word whose meaning depends on the pad type (docs/io_format.md, pulsar_modules):
  type & 0xF == 1  fixed point 1.31 (-1..1), or a plain integer when the pad's max is a small number (selectors)
  type & 0xF == 2  IEEE single float (min/max are float bits)
  type & 0xF == 0xE MIDI (not settable as a constant)

PadModel turns the pad descriptor (type, min, max) into a numeric range and converts both ways.
Param adds a user mapping on top: unit, display range and curve (lin / log / db), used by device knobs.
"""

import math
import struct

FIX_ONE = 1 << 31


def s32(v):
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v & 0x80000000 else v


def f32(bits):
    return struct.unpack(">f", struct.pack(">I", bits & 0xFFFFFFFF))[0]


def f32_bits(x):
    return struct.unpack(">I", struct.pack(">f", x))[0]


class PadModel:
    """Numeric model of one DSP input pad."""

    def __init__(self, info):
        typ = info.get("type", 1)
        self.sync = bool(info.get("sync"))
        nib = typ & 0xF
        rmin, rmax = info.get("min", 0) & 0xFFFFFFFF, info.get("max", 0) & 0xFFFFFFFF
        if nib == 2:
            self.kind = "float"
            lo, hi = f32(rmin), f32(rmax)
            if not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo or abs(hi) > 1e9:
                lo, hi = -1.0, 1.0                    # unbounded float pad: assume -1..1
        elif nib == 1 and 0 < s32(rmax) < 0x10000:
            self.kind = "int"
            lo, hi = float(s32(rmin)), float(s32(rmax))
        elif nib == 0xE:
            self.kind = "midi"
            lo, hi = 0.0, 1.0
        else:
            self.kind = "fix"
            lo, hi = s32(rmin) / FIX_ONE, s32(rmax) / FIX_ONE
            if hi <= lo:
                lo, hi = -1.0, 1.0
        self.lo, self.hi = lo, hi
        # constants are almost never useful below 0 on unipolar audio-range pads: keep the full range anyway
        self.settable = self.kind != "midi"

    def to_raw(self, x):
        x = min(max(x, self.lo), self.hi)
        if self.kind == "float":
            return f32_bits(x)
        if self.kind == "int":
            return int(round(x)) & 0xFFFFFFFF
        return int(round(max(-1.0, min(x, 1.0 - 2 ** -31)) * FIX_ONE)) & 0xFFFFFFFF

    def from_raw(self, raw):
        if self.kind == "float":
            return f32(raw)
        if self.kind == "int":
            return float(s32(raw))
        return s32(raw) / FIX_ONE

    def default(self):
        return 0.0 if self.lo <= 0.0 <= self.hi else self.lo

    def fmt(self, x):
        if self.kind == "int":
            return "%d" % round(x)
        return "%.3f" % x


class Param:
    """A user-facing control: display range + unit + curve, mapped onto a PadModel.

    curve: "lin"  value = lo + t*(hi-lo)
           "log"  value = lo * (hi/lo)**t          (frequencies, times; lo > 0)
           "db"   display in dB, pad gets 10**(dB/20) (gains)
    to_pad: optional callable(display_value, rate) -> pad numeric value (e.g. Hz -> phase increment)."""

    def __init__(self, name, unit="", lo=0.0, hi=1.0, default=None, curve="lin", pad=None, to_pad=None, fmt=None,
                 from_pad=None):
        self.name, self.unit, self.lo, self.hi, self.curve = name, unit, float(lo), float(hi), curve
        self.default = self.lo if default is None else float(default)
        self.pad, self.to_pad, self.from_pad, self._fmt = pad, to_pad, from_pad, fmt

    # normalised knob position t in 0..1 <-> display value
    def from_t(self, t):
        t = min(max(t, 0.0), 1.0)
        if self.curve == "log" and self.lo > 0:
            return self.lo * (self.hi / self.lo) ** t
        return self.lo + t * (self.hi - self.lo)

    def to_t(self, v):
        if self.hi == self.lo:
            return 0.0
        if self.curve == "log" and self.lo > 0 and v > 0:
            return math.log(v / self.lo) / math.log(self.hi / self.lo)
        return (v - self.lo) / (self.hi - self.lo)

    def raw(self, v, rate=48000):
        """Display value -> 32-bit pad word."""
        if self.to_pad is not None:
            x = self.to_pad(v, rate)
        elif self.curve == "db":
            x = 0.0 if v <= self.lo else 10 ** (v / 20.0)
        else:
            x = v
        return self.pad.to_raw(x) if self.pad is not None else int(x) & 0xFFFFFFFF

    def from_raw(self, raw, rate=48000):
        """32-bit pad word -> display value (inverse of raw())."""
        x = self.pad.from_raw(raw) if self.pad is not None else float(raw)
        if self.from_pad is not None:
            return self.from_pad(x, rate)
        if self.curve == "db":
            return 20 * math.log10(x) if x > 0 else self.lo
        return x

    def fmt(self, v):
        if self._fmt:
            return self._fmt(v)
        if self.curve == "db" and v <= self.lo:
            return "-inf dB"
        if self.unit == "Hz" and v >= 1000:
            return "%.2f kHz" % (v / 1000.0)
        if abs(v) >= 100:
            s = "%.0f" % v
        elif abs(v) >= 10:
            s = "%.1f" % v
        else:
            s = "%.2f" % v
        return "%s %s" % (s, self.unit) if self.unit else s


def pad_param(info, name=None):
    """Generic knob for a bare module input (no unit known): the pad's own numeric range, linear."""
    m = PadModel(info)
    p = Param(name or info.get("long") or info.get("name") or "", "", m.lo, m.hi, m.default(), "lin", pad=m,
              fmt=m.fmt)
    return p


def phase_increment(hz, rate):
    """Oscillator frequency input (CSineR4 'f' style): a 32-bit phase accumulator step, i.e. hz/rate of a full
    2**32 turn. As a 1.31 fraction that is 2*hz/rate (1.0 = Nyquist), wrapped into -1..1."""
    return ((2.0 * hz / float(rate)) + 1.0) % 2.0 - 1.0


def phase_increment_inv(x, rate):
    return x * float(rate) / 2.0 if x >= 0 else (x + 2.0) * float(rate) / 2.0


# Known units for common bare modules: (module file lower-case, pad short name) -> Param factory(pad_info)
KNOWN = {
    ("csiner4.dsp", "f"): lambda i: Param("Frequency", "Hz", 20, 20000, 440, "log", PadModel(i),
                                          phase_increment, from_pad=phase_increment_inv),
    ("linvol.dsp", "vol"): lambda i: Param("Volume", "dB", -60, 0, -40, "db", PadModel(i)),
}


def param_for(module_file, info):
    f = KNOWN.get(((module_file or "").lower(), (info.get("name") or "").strip().lower()))
    return f(info) if f else pad_param(info)
