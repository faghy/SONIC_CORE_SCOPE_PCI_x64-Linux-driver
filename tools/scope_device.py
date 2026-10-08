#!/usr/bin/env python3
"""
scope_device.py - turn a SCOPE device file (.dev) into a load plan for pulsard.

A SCOPE device is a tree of RODBase modules.  Leaves with a ROCAtom algo are DSP modules (matched by name to a
.dsp file), leaves with a ROCAlgo algo are host-side "pep" scripts (knobs, text displays, value converters,
routing switches), and RODRouting objects wire RODPads together.  This tool

  * resolves every atom to its .dsp file (Sim2k FUN_10c02ba0: short name, long name, then file name),
  * computes the nets (pads joined by routings and pad links) and from them the DSP-to-DSP wires,
    the device's external ports and the constant inputs,
  * follows every user control (ScriptController based knobs/faders/buttons) through the host-side converter
    scripts to the DSP input pads it drives, together with its text display (unit, format, range, curve),
  * reports what blocks a complete plan (host-only atoms, polyphony, MIDI, ...).

  plan(dev_path, dsp_dir) -> dict          see docs/device_format.md §6 for the layout
  to_raw(param, display_value, rate)       32-bit raw value for the param's first DSP target
  targets_raw(param, display_value, rate)  [(key, in, raw)] for all targets
  knob_to_display(param, pos) / display_to_knob(param, value)   SCOPE knob position (0..1) <-> display value

CLI:
  scope_device.py plan FILE.dev [--dsp DIR] [--json]
  scope_device.py survey DIR [--dsp DIR] [--jobs N] [--json OUT]
  scope_device.py raw FILE.dev PARAM VALUE [--rate 48000]
  scope_device.py presets FILE.dev [N|NAME] [--pre FILE.pre] [--json]   presets / preset N as param values

Spec and evidence: docs/device_format.md.  Python 3 standard library only; uses scope_dev.py (container and
object parser), pulsar_modules.py (DSP module descriptors).
"""
import argparse
import json
import math
import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scope_dev as sd          # noqa: E402
import pulsar_modules as pm     # noqa: E402
import pulsar_delay as pdl      # noqa: E402  (host delay atoms, docs/pc_delay.md)

DEFAULT_DSP_DIR = "/var/lib/snd-pulsar/dsp"
INTMAX = 0x7FFFFFFF
REF_RATE = 48000.0              # base.dll DAT_100dc258: pad values are stored at a 48 kHz reference [C]

# ROCParameterInterpreter::m_unitStrings (base.dll 0x1013767c) = Unit attribute (0x1c) values [C]
UNIT_NAMES = {0: "", 1: "Hz", 2: "ms", 3: "dB0", 4: "dB12", 5: "dB24", 6: "kHz", 7: "sec", 8: "smpls",
              9: "BPM", 10: "%"}

# ScriptController curve types (ScriptController.pep) [C]
CURVE_NAMES = {0: "linear", 1: "exp", 2: "log", 3: "exp_bipolar", 4: "log_bipolar", 5: "table5"}

# ScriptController subclasses: the user's knobs/faders.  Value pad 'Val' in [Min, Max].
KNOB_PEPS = {"NewAni", "NewAni_AutoReturn", "Fader", "DynFaderVertical", "TextFader", "TextFaderMinMax",
             "TextFaderMax", "AniNull"}
# ScriptTextController subclasses: text displays bound to a knob's Val (Min/Max = display range,
# MinControl/MaxControl = Val range) [C]
TEXT_PEPS = {"ScriptTextController", "ScriptTextEditController", "TextEditMinMax", "TextEditMin"}
# discrete controls with Val in [Min, Max]
BUTTON_PEPS = {"Button", "Button2"}
# pure junctions (one pad, no logic)
JUNCTION_PEPS = {"Dummy", "DummySyn", "dummymidi", "DummyStr", "DummyIntArray"}
# scripts that change DSP wiring at run time; the saved routing state is used as a snapshot
ROUTING_PEPS = {"SignalSwitch", "SignalSwitch2", "SignalSwitch5", "SignalSwitchEx", "SignalSwitchFlt",
                "SignalSwitchTh", "OnOffSwitch", "AsyncSwitch", "AsyncSwitch2", "AsyncSwitch4",
                "AsyncSwitch4OutToIn", "AsyncSwitch8OutToIn", "SyncSwitch", "SyncSwitch2", "BusSwitch",
                "BusSwitch2", "InTo4OutSwitch", "InTo16OutSwitch", "Minus1Switch"}
# scripts that create or load modules while the device runs (only their saved children are seen here)
DYNAMIC_PEPS = {"ModuleLoader", "PepLoad", "DynamicDelay", "EffectInserter", "LayerRouting", "Layer",
                "DynVoicesOfParent", "NumVoicesOfParent", "NewDevice", "PresetListLoader"}
# GUI / housekeeping scripts that never feed a DSP pad: ignored on parameter paths
GUI_PEPS = {"Equalizer", "SurfaceInterface", "ParentTopChanger", "SimpleChildrenDisplay", "ChildrenDisplay",
            "PepDisplayObject", "NewText", "VUMeter", "CompressorDisplay", "ChildView", "Group", "MidiChannel",
            "Ctrl_IO_7Bit", "MidiCtrlInverter", "ChannelLinker", "MinimizedView", "ModularView", "EffectText",
            "NameOfParent", "Long2String", "Channel2StringTh", "EditIntCurve", "GenericCurve", "EditCurve",
            "LEDTimerTh", "SimpleAnimation", "ComboBox", "ComboBoxList", "TextFaderEx", "Controls@PresetList",
            "PresetListLoader", "SchubladeQuick", "MonoPadHide", "AuxPadHide", "AuxPadHideRM", "GetParent",
            "MenuFileEntry", "FileDialog", "RouteByContext", "EffectInsert", "ControllerHolder", "MRC",
            "MasterVerbRevCurve", "Controls@VUMultiTranslate", "Controls@DiscreteValueDisplay", "ValueSender"}
# host converters emulated by converter_steps()
CONVERTER_PEPS = {"Long2LongSyncAtom", "Long2LongSync", "Long2Long", "Double2Long", "Float2Long",
                  "LongAtom2LongSyncAtom", "LongSyncAtom2LongAtom", "Attenuator", "Long2Flt", "Inverter",
                  "ScriptAdd", "ScriptMultiplier"}
POLY_PEPS = {"DynVoicesOfParent", "NumVoicesOfParent"}

POLY_FLAG = 0x00400000          # module flags: polyphonic, 'VoiceDef' substitution (module_loading.md §1.2)
ONE_PER_BOARD = 0x20000000


# ---------------------------------------------------------------------------------------------------------
# curves and value conversion
# ---------------------------------------------------------------------------------------------------------

def curve_value(x, curve, intensity, reverse=False, table=None):
    """ScriptController.GetCurveVal (ScriptController.pep) == base.dll ROCParameter::GetCurveValue
    @10025390 / Value2Range::GetCurveValue @10013d60.  x, result in [0, 1]."""
    x = min(1.0, max(0.0, float(x)))
    crv = int(curve)
    if x in (0.0, 1.0):
        crv = 0
    if reverse:
        crv = {1: 2, 2: 1, 3: 4, 4: 3, 5: 6, 6: 5}.get(crv, crv)
    if crv == 0:
        return x
    if crv in (5, 6):
        xs = [0.0] + [table[2 * i] / 127.0 for i in range(3)] + [1.0] if table else [0, 32 / 127., 64 / 127., 110 / 127., 1]
        ys = [0.0] + [table[2 * i + 1] for i in range(3)] + [1.0] if table else [0, .01, .1, .4, 1]
        if crv == 6:
            xs, ys = ys, xs
        for i in range(4):
            if x < xs[i + 1]:
                break
        return ys[i] + (x - xs[i]) / (xs[i + 1] - xs[i]) * (ys[i + 1] - ys[i])
    inten = float(intensity)
    if inten <= 1.0:
        raise ValueError("curve %d needs Intensity > 1" % crv)
    z = 1.0 / inten                                   # fktZero
    if crv == 1:                                      # y = (I^x - 1) / (I - 1)
        return (z * 10 ** (x * math.log10(inten)) - z) / (1.0 - z)
    if crv == 2:                                      # inverse of 1
        xt = x * (1.0 - z) + z
        return 0.0 if xt <= z else 1.0 + math.log(xt) / math.log(inten)
    a = abs(2.0 * x - 1.0)
    if crv == 3:
        y = (z * 10 ** (a * math.log10(inten)) - z) / (1.0 - z)
    elif crv == 4:
        y = 1.0 + math.log(a * (1.0 - z) + z) / math.log(inten)
    else:
        raise ValueError("invalid curve %d" % crv)
    if x < 0.5:
        y = -y
    return y / 2.0 + 0.5


def c_format(fmt, v):
    """Lava format() with a C printf format such as '%1.2f', '%1.0fHz', '%d'."""
    if not fmt:
        return "%g" % v
    try:
        if re.search(r"%[-+ 0#]*\d*[diuxX]", fmt):
            return fmt % int(round(v))
        return fmt % v
    except (TypeError, ValueError):
        return "%g" % v


def _knob_val(k, pos):
    """ScriptController.SendVal: knob position (0..1, Invert applied) -> Val."""
    x = 1.0 - pos if k.get("invert") else pos
    y = curve_value(x, k["curve"], k["intensity"], False, k.get("table"))
    return round(k["min"] + y * (k["max"] - k["min"]))


def _knob_pos(k, val):
    """ScriptController.GetVal: Val -> knob position."""
    lo, hi = k["min"], k["max"]
    v = min(max(val, lo), hi)
    y = 1.0 if lo == hi else (v - lo) / float(hi - lo)
    x = curve_value(y, k["curve"], k["intensity"], True, k.get("table"))
    return 1.0 - x if k.get("invert") else x


def _text_from_val(t, val):
    """ScriptTextController.GetValFromText (+ TextEditMinMax Offset/Divisor, TextFader (Val+Offset)/Divisor)."""
    if t["kind"] == "fader":
        return (val + t.get("offset", 0)) / (t.get("divisor") or 1.0)
    lo, hi = t["min_control"], t["max_control"]
    x = (val - lo) / float(hi - lo)
    if x < 0.0:
        x = -x if lo >= 0 else 0.0
    if t.get("invert"):
        x = 1.0 - x
    x = min(x, 1.0)
    y = curve_value(x, t["curve"], t["intensity"], False, t.get("table"))
    d = t["min"] + y * (t["max"] - t["min"])
    if val < 0 and lo >= 0:
        d = -d
    if t["kind"] == "minmax":
        d = (d + t.get("offset", 0)) / (t.get("divisor") or 1.0)
    return d


def _val_from_text(t, d):
    """ScriptTextController.SetValFromText (TextEditMinMax.OnStrChanged undoes Divisor/Offset first)."""
    if t["kind"] == "fader":
        return round(d * (t.get("divisor") or 1.0) - t.get("offset", 0))
    if t["kind"] == "minmax":
        d = d * (t.get("divisor") or 1.0) - t.get("offset", 0)
    d = min(max(d, t["min"]), t["max"])
    y = (d - t["min"]) / float(t["max"] - t["min"])
    x = curve_value(y, t["curve"], t["intensity"], True, t.get("table"))
    if t.get("invert"):
        x = 1.0 - x
    return int(x * (t["max_control"] - t["min_control"]) + t["min_control"])


def val_to_display(param, val):
    t = param.get("display")
    return _text_from_val(t, val) if t else float(val)


def display_to_val(param, value):
    t = param.get("display")
    v = _val_from_text(t, float(value)) if t else round(float(value))
    return int(min(max(v, param["val_min"]), param["val_max"]))


def knob_to_display(param, pos):
    return val_to_display(param, _knob_val(param["knob"], pos)) if param.get("knob") else None


def display_to_knob(param, value):
    return _knob_pos(param["knob"], display_to_val(param, value)) if param.get("knob") else None


def apply_chain(chain, v):
    """Host converter scripts between the knob's Val and the DSP pad (Long2LongSyncAtom, Long2Long, ...)."""
    for st in chain:
        op = st["op"]
        if op == "linear":
            (a0, a1), (b0, b1) = st["a"], st["b"]
            v = b0 + (b1 - b0) * (v - a0) / (a1 - a0)
            if st.get("round"):
                v = round(v)
        elif op == "scale":
            v = v * st["k"]
        elif op == "add":
            v = v + st["k"]
        elif op == "one_minus":
            v = 1.0 - v
        elif op == "clip":
            v = min(max(v, st["lo"]), st["hi"])
    return v


def pad_encode(v, encoding, unit, rate):
    """ROCAtom value -> DSP word.  base.dll FUN_10075b40 (used by ROCAtom::SetInPad @10075ce0) [C]:
    Unit 1 (frequency, a phase increment at 48 kHz): raw = v * 48000 / fs;
    Unit 2 (time, samples at 48 kHz):                raw = v * fs / 48000;  other units: unchanged."""
    if unit == 1:
        v = v * REF_RATE / rate
    elif unit == 2:
        v = v * rate / REF_RATE
    if encoding == "float":
        return struct.unpack("<I", struct.pack("<f", float(v)))[0]
    iv = int(round(v))
    iv = max(-0x80000000, min(0x7FFFFFFF, iv)) if encoding != "uint" else iv
    return iv & 0xFFFFFFFF


def targets_raw(param, display_value, rate=48000):
    val = display_to_val(param, display_value)
    out = []
    for t in param["targets"]:
        v = apply_chain(t["chain"], val)
        if t.get("kind") == "switch":
            out.append((t["key"], "switch", int(round(v))))
        else:
            out.append((t["key"], t["in"], pad_encode(v, t["encoding"], t["unit"], rate)))
    return out


def to_raw(param, display_value, rate=48000, target=0):
    """32-bit value for SetValue on target `target` (default: the first DSP pad target) of `param` at sample
    rate `rate`.  (A 'switch' target yields the new Switch position instead, see rewire().)"""
    rows = targets_raw(param, display_value, rate)
    if target == 0:
        rows = [r for r in rows if r[1] != "switch"] or rows
    return rows[target][2]


def rewire(old_plan, new_plan):
    """Wire changes between two plans of the same device (e.g. after a routing switch moved):
    (disconnect [(dst_key, in)], connect [wire])."""
    ow = {(w["dst_key"], w["in"]): w for w in old_plan["wires"]}
    nw = {(w["dst_key"], w["in"]): w for w in new_plan["wires"]}
    disc = [k for k in ow if k not in nw or nw[k] != ow[k]]
    conn = [w for k, w in nw.items() if k not in ow or ow[k] != w]
    return disc, conn


def const_raw(const, rate=48000):
    return pad_encode(const["value"], const["encoding"], const["unit"], rate)


# ---------------------------------------------------------------------------------------------------------
# DSP name index (FUN_10c02ba0: short name +0x18, long name +0x1c, then the file's base name, stricmp)
# ---------------------------------------------------------------------------------------------------------

_IDX = {}
_CLS = {}
_ROOTS = {}


def dsp_index(dsp_dir):
    if dsp_dir not in _IDX:
        idx = {}
        for k, v in sd.dsp_name_index(dsp_dir).items():
            idx[k] = sorted(set(v))[0]
        files = {f.lower(): f for f in os.listdir(dsp_dir) if f.lower().endswith(".dsp")}
        _IDX[dsp_dir] = (idx, files)
    return _IDX[dsp_dir]


def resolve_atom(name, dsp_dir):
    idx, files = dsp_index(dsp_dir)
    if name in idx and idx[name].lower().endswith(".dsp"):
        return idx[name]
    return files.get(os.path.basename(name).lower())


def module_class(dsp_dir, fn):
    k = (dsp_dir, fn)
    if k not in _CLS:
        try:
            _CLS[k] = pm.ModuleClass(os.path.join(dsp_dir, fn))
        except Exception as e:          # other heaps, broken file
            _CLS[k] = e
    return _CLS[k]


# ---------------------------------------------------------------------------------------------------------
# device graph
# ---------------------------------------------------------------------------------------------------------

class UF:
    def __init__(self):
        self.p = {}

    def find(self, a):
        p = self.p
        p.setdefault(a, a)
        while p[a] != a:
            p[a] = p[p[a]]
            a = p[a]
        return a

    def union(self, a, b):
        a, b = self.find(a), self.find(b)
        if a != b:
            self.p[b] = a


def _attrs(v):
    return (v.f.get("param") or {}).get("attrs") or {}


def _pval(v):
    return (v.f.get("param") or {}).get("value")


def _sval(v):
    """String pad value (scope_dev.py gives non-ASCII strings as hex, e.g. '%1.1f' + degree sign)."""
    x = _pval(v)
    if isinstance(x, str) and re.fullmatch(r"(?:[0-9a-f]{2})+", x) and not x.isdigit() and _ptype(v) == "String":
        b = bytes.fromhex(x).rstrip(b"\0")
        if any(c >= 0x80 for c in b) and all(c >= 0x20 for c in b):
            return b.decode("latin-1")
    return x


def _ptype(v):
    return (v.f.get("param") or {}).get("type")


def _vname(v):
    return v.f.get("varname") or v.f.get("name")


def _num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


class Device:
    def __init__(self, path, dsp_dir, switch_values=None):
        self.path = path
        self.dsp_dir = dsp_dir
        root = _ROOTS.get(path)
        if root is None:
            plain, _ = sd.load(path)
            if not sd.is_object_archive(plain):
                raise sd.FormatError("not an object archive")
            _, root, _, _ = sd.parse_plain(plain)
            _ROOTS.clear()
            _ROOTS[path] = root
        self.root = root
        self.edges = []             # (id, id) from routings
        self.links = []             # (RODPad id, linked pad id)
        self.switches = {}          # key -> switch record
        self.unsupported = []
        self.warnings = []
        self.atoms = {}             # key -> dict
        self.algos = {}             # key -> dict (host scripts)
        self.pad_owner = {}         # pad id -> ("atom", key, dir, index) | ("algo", key, varname)
        self.pads = {}              # pad id -> Obj (ROCAtomPad/ROCPad)
        self.rodpads = {}           # RODPad id -> Obj
        self.mod_by_id = {}         # RODBase id -> key
        self.uf = UF()
        self.ext = []               # root RODPads
        self.placement = []
        self._walk(self.root, "", True)
        self._switches(switch_values or {})
        for a, b in self.edges + self.links:
            self.uf.union(a, b)
        self._nets()

    # -- tree walk ---------------------------------------------------------------------------------------
    def _walk(self, o, path, is_root=False):
        name = o.f.get("name") or o.cls
        key = name if is_root else path + "/" + name
        while key in self.atoms or key in self.algos:
            key += "'"
        if o.f.get("id"):
            self.mod_by_id[o.f["id"]] = key
        for x in (o.f.get("extras_mod") or []):
            if x["key"].startswith("0x10f"):
                self.placement += [dict(p, key=None) for p in x["val"] if isinstance(p, dict)]
            if x["key"].startswith("0x102") and _num(x["val"]) > 1:
                self.unsupported.append("polyphonic: %s has numVoices=%s (extras 0x102)" % (key, x["val"]))
        a = o.f.get("algo")
        if a is not None:
            vars_ = a.f.get("vars", [])
            for v in vars_:
                at = _attrs(v)
                if _num(at.get("NumVoices"), 0) > 1:
                    self.unsupported.append("polyphonic: %s.%s NumVoices=%s" % (key, _vname(v), at["NumVoices"]))
            if a.cls == "ROCAtom":
                ins = [v for v in vars_ if v.f.get("dir") == "in"]
                outs = [v for v in vars_ if v.f.get("dir") == "out"]
                self.atoms[key] = {"key": key, "module": a.f.get("module"), "id": o.f.get("id"),
                                   "ins": ins, "outs": outs, "obj": o}
                for i, v in enumerate(ins):
                    self.pad_owner[v.f["id"]] = ("atom", key, "in", i)
                    self.pads[v.f["id"]] = v
                for k, v in enumerate(outs):
                    self.pad_owner[v.f["id"]] = ("atom", key, "out", k)
                    self.pads[v.f["id"]] = v
            else:
                if (a.f.get("pep") or "").startswith("Surfaces@"):
                    _SURFACE_KEYS.add(key)
                self.algos[key] = {"key": key, "pep": a.f.get("pep") or a.f.get("name"),
                                   "vars": {_vname(v): v for v in vars_}, "obj": o}
                for v in vars_:
                    if v.f.get("id"):
                        self.pad_owner[v.f["id"]] = ("algo", key, _vname(v))
                        self.pads[v.f["id"]] = v
        for k in o.kids:
            if k.cls == "RODPad":
                self.rodpads[k.f["id"]] = k
                if is_root:
                    self.ext.append(k)
                lk = k.f.get("link")
                if lk and len(lk) == 1:
                    self.links.append((k.f["id"], lk[0]))
                elif lk:
                    self.warnings.append("pad %s links into a referenced file" % k.f.get("name"))
            elif k.cls == "<RefModule>":
                self.unsupported.append("references another file: %s" % k.f.get("path"))
            elif k.cls.startswith("ROD") and k.cls not in ("RODParameter",):
                self._walk(k, key)
        for rt in o.f.get("routings", []):
            a_, b_ = rt.f.get("from"), rt.f.get("to")
            if a_ and b_ and len(a_) == 1 and len(b_) == 1:
                self.edges.append((a_[0], b_[0]))
            else:
                self.warnings.append("routing into a referenced file ignored")

    # -- routing switches (SignalSwitch*, AsyncSwitch*, SyncSwitch, OnOffSwitch ...) ----------------------
    def _switches(self, override):
        """These scripts connect pad In<Switch+1> to Out (or In to Out<Switch+1>; OnOffSwitch: In to Out
        when Switch != 0) with PepAddRouting.  The routing they made is saved in the file; drop it and
        redo it from the Switch value (or `override[key]`), so that a plan can be recomputed for another
        switch position."""
        self._link = dict(self.links)

        def deref(pid):
            for _ in range(8):
                if pid not in self._link:
                    break
                pid = self._link[pid]
            return pid
        sw = {}
        for key, al in self.algos.items():
            vs = al["vars"]
            if "Switch" not in vs:
                continue
            ins = sorted((int(m.group(1)), n) for n in vs for m in [re.match(r"^In(\d+)$", n)] if m)
            outs = sorted((int(m.group(1)), n) for n in vs for m in [re.match(r"^Out(\d+)$", n)] if m)
            if ins and "Out" in vs:
                common, choices = "Out", [n for _, n in ins]
            elif outs and "In" in vs:
                common, choices = "In", [n for _, n in outs]
            elif al["pep"] == "OnOffSwitch" and "In" in vs and "Out" in vs:
                common, choices = "Out", [None, "In"]
            else:
                continue
            val = override.get(key, _pval(vs["Switch"]))
            try:
                val = int(round(float(val)))
            except (TypeError, ValueError):
                val = 0
            if al["pep"] == "OnOffSwitch":
                pick = "In" if val != 0 else None
            else:
                pick = choices[val] if 0 <= val < len(choices) else None
            sw[key] = {"key": key, "pep": al["pep"], "value": val, "choices": choices, "common": common,
                       "selected": pick, "pad_ids": {n: vs[n].f.get("id") for n in choices + [common] if n}}
        if not sw:
            return
        mine = {}
        for key, r in sw.items():
            for n, pid in r["pad_ids"].items():
                mine[pid] = key
        keep = []
        for a, b in self.edges:
            ka, kb = mine.get(deref(a)), mine.get(deref(b))
            if ka is not None and ka == kb:
                continue            # the switch's own (saved) routing
            keep.append((a, b))
        self.edges = keep
        for key, r in sw.items():
            if r["selected"]:
                self.edges.append((r["pad_ids"][r["selected"]], r["pad_ids"][r["common"]]))
            r.pop("pad_ids")
        self.switches = sw

    # -- nets ------------------------------------------------------------------------------------------
    def _nets(self):
        nets = {}
        for pid in list(self.pad_owner) + list(self.rodpads):
            r = self.uf.find(pid)
            n = nets.setdefault(r, {"dsp_in": [], "dsp_out": [], "host": [], "ext": []})
            own = self.pad_owner.get(pid)
            if own is None:
                continue
            if own[0] == "atom":
                n["dsp_in" if own[2] == "in" else "dsp_out"].append((own[1], own[3], pid))
            else:
                n["host"].append((own[1], own[2], pid))
        for e in self.ext:
            nets.setdefault(self.uf.find(e.f["id"]), {"dsp_in": [], "dsp_out": [], "host": [], "ext": []})["ext"].append(e)
        self.nets = nets

    def net_of(self, pid):
        return self.nets.get(self.uf.find(pid))


# ---------------------------------------------------------------------------------------------------------
# host scripts used on parameter paths
# ---------------------------------------------------------------------------------------------------------

def _linear(al, var_lo, var_hi, out_lo, out_hi, rnd=True):
    g = lambda n: _num(_pval(al["vars"][n])) if n in al["vars"] else None   # noqa: E731
    a0, a1, b0, b1 = g(var_lo), g(var_hi), g(out_lo), g(out_hi)
    if None in (a0, a1, b0, b1) or a0 == a1:
        return None
    return {"op": "linear", "a": [a0, a1], "b": [b0, b1], "round": rnd}


def converter_steps(al, invar):
    """[(outvar, [steps])] for host script `al` when its pad `invar` changes (pep sources: docs §4.3)."""
    pep = al["pep"]
    vs = al["vars"]
    no_scale = lambda: int(_num(_pval(vs["noScale"]), 1)) if "noScale" in vs else 1   # noqa: E731
    if pep == "Long2LongSyncAtom" and invar == "LongVar":
        return [("LongSyncAtomVar", [] if no_scale() else
                 [_linear(al, "AbsLongMin1", "AbsLongMax1", "AbsLongMin2", "AbsLongMax2")])]
    if pep == "Long2LongSync" and invar == "LongVar":
        return [("LongSyncVar", [] if no_scale() else
                 [_linear(al, "AbsLongMin", "AbsLongMax", "AbsLongSyncMin", "AbsLongSyncMax")])]
    if pep == "Long2Long":
        if invar == "LongVar1":
            return [("LongVar2", [_linear(al, "AbsLongMin1", "AbsLongMax1", "AbsLongMin2", "AbsLongMax2")])]
        if invar == "LongVar2":
            return [("LongVar1", [_linear(al, "AbsLongMin2", "AbsLongMax2", "AbsLongMin1", "AbsLongMax1")])]
    if pep == "Double2Long":
        if invar == "DoubleVar":
            return [("LongVar", [_linear(al, "AbsDoubleMin", "AbsDoubleMax", "AbsLongMin", "AbsLongMax")])]
        if invar == "LongVar":
            return [("DoubleVar", [_linear(al, "AbsLongMin", "AbsLongMax", "AbsDoubleMin", "AbsDoubleMax", False)])]
    if pep == "Float2Long":
        if invar == "FloatVar":
            return [("LongVar", [_linear(al, "AbsFloatMin", "AbsFloatMax", "AbsLongMin", "AbsLongMax")])]
        if invar == "LongVar":
            return [("FloatVar", [_linear(al, "AbsLongMin", "AbsLongMax", "AbsFloatMin", "AbsFloatMax", False)])]
    if pep == "LongAtom2LongSyncAtom" or pep == "LongSyncAtom2LongAtom":
        return []                   # done by its saved child atom (as_mult.dsp / ...), wired by saved routings
    if pep == "Attenuator" and invar == "In":
        k = _num(_pval(vs.get("Attenuator")) if "Attenuator" in vs else INTMAX) / INTMAX
        f = _num(_pval(vs.get("Factor")) if "Factor" in vs else 1.0, 1.0)
        return [("Out", [{"op": "scale", "k": k * f}, {"op": "clip", "lo": -2147483648.0, "hi": INTMAX}])]
    if pep == "Long2Flt" and invar == "Input":
        return [("Output", [{"op": "scale", "k": 1.0 / INTMAX}])]
    if pep == "Inverter" and invar == "InputVar":
        return [("OutputVar", [{"op": "one_minus"}])]
    if pep == "ScriptAdd" and invar in ("in1", "in2"):
        other = "in2" if invar == "in1" else "in1"
        return [("out", [{"op": "add", "k": _num(_pval(vs[other])) if other in vs else 0.0},
                         {"op": "clip", "lo": -2147483648.0, "hi": INTMAX}])]
    if pep == "ScriptMultiplier" and invar in ("in1", "in2"):
        other = "in2" if invar == "in1" else "in1"
        return [("out", [{"op": "scale", "k": _num(_pval(vs[other]), 1.0) if other in vs else 1.0}])]
    return _pc_delay_converter_steps(al, invar)


# ---------------------------------------------------------------------------------------------------------
# host delay atoms ("PC Master 4k/32k Delay", "PC 256k Delay", "PC Early Reflection"), docs/pc_delay.md
# ---------------------------------------------------------------------------------------------------------

def _pc_delay_converter_steps(al, invar):
    """DelayTimeCalcEx.pep [C]: Tout = min(Tin, Dmax) in direct mode (DirT != 0); in tempo mode (DirT == 0)
    Tout = int(6000/BPM * 192000 * num[Nidx]/384), driven by BPM/Nidx, not by the Tin knob.
    Values are sample ticks at 48 kHz; the delay atom's Del pad (Unit 2) rescales them to fs."""
    vs = al["vars"]
    if al["pep"] != "DelayTimeCalcEx":
        return None
    if invar != "Tin":
        return []                   # Tout/Tbpm are its outputs; DirT/BPM/Nidx/Dmax do not follow the knob
    if int(_num(_pval(vs["DirT"]), 1)) == 0 if "DirT" in vs else False:
        return []                   # tempo-sync mode: the time knob does not reach the delay
    dmax = _num(_pval(vs["Dmax"]), 0) if "Dmax" in vs else 0
    return [("Tout", [{"op": "clip", "lo": -2147483648.0, "hi": dmax if dmax > 0 else INTMAX}])]


def _pc_delay_module(m, at):
    """Mark plan module `m` as a host delay atom (instead of a blocker). True if it is one."""
    spec = pdl.atom_spec(at["module"])
    if spec is None:
        return False
    m["kind"] = "pc_delay"
    m["pc_delay"] = spec
    m["cycles"] = 0
    if len(at["ins"]) < 2 or not at["outs"]:
        return False
    return True


def _pc_delay_annotate(res, dv):
    """Per host delay atom: the source of its signal input, the taps in use, and how each delay/gain pad is
    fed (constant, parameter, or a DSP async output = 'host_async' wire, e.g. DLEXTM1 'DT')."""
    pcs = {m["key"]: m for m in res["modules"] if m.get("kind") == "pc_delay"}
    res["pc_delays"] = []
    if not pcs:
        return
    for key, m in pcs.items():
        spec = m["pc_delay"]
        ins = [w for w in res["wires"] if w["dst_key"] == key and w["in"] == 0]
        taps = sorted({w["out"] for w in res["wires"] if w["src_key"] == key})
        info = {"key": key, "type": spec["type"], "kind": spec["kind"], "input": None, "taps_used": taps,
                "pads": {}}
        if len(ins) != 1:
            dv.unsupported.append("%s: delay input must be fed by exactly one DSP output" % key)
        else:
            src = ins[0]["src_key"]
            sm = next((x for x in res["modules"] if x["key"] == src), None)
            if sm is None or sm.get("kind") == "pc_delay":
                dv.unsupported.append("%s: delay input fed by another host atom" % key)
            info["input"] = [src, ins[0]["out"]]
        if any(w["src_key"] == key and w["dst_key"] in pcs for w in res["wires"]):
            dv.unsupported.append("%s: delay output wired to another host atom" % key)
        for w in res["wires"]:
            if w["dst_key"] != key or w["in"] == 0:
                continue
            role = pdl.pad_role(spec, w["in"])
            sm = next((x for x in res["modules"] if x["key"] == w["src_key"]), None)
            if role[0] != "delay" or spec["kind"] == pdl.KIND_ER or sm is None or sm.get("kind") == "pc_delay":
                dv.unsupported.append("%s: pad %d %s cannot be driven by %s" % (key, w["in"], role, w["src_key"]))
                continue
            w["host_async"] = True              # DSP async out -> BAR dword -> kernel (PULSAR_DELAY_P_SOURCE)
            info["pads"][w["in"]] = "dsp"
        for c in res["consts"]:
            if c["key"] == key:
                info["pads"].setdefault(c["in"], "const")
        for q in res["params"]:
            for t in q["targets"]:
                if t.get("key") == key and "in" in t:
                    info["pads"][t["in"]] = "param"
        res["pc_delays"].append(info)
    if res["ports"] and any(k in pcs for p in res["ports"] for k, _ in (p.get("targets") or [])):
        dv.unsupported.append("device port wired straight to a host delay atom")


# ---------------------------------------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------------------------------------

def _encoding(cls_pad, rocpad, unit):
    t = cls_pad.type if cls_pad is not None else 1
    if (t & 0xF) == 2 or _ptype(rocpad) in ("Float", "Double"):
        return "float"
    if unit == 2 or (t & 0xF) not in (1,):
        return "int"
    return "fix31"


def _unit_of(rocpad, cls_pad):
    return int(_num(_attrs(rocpad).get("Unit"), 0))


def plan(dev_path, dsp_dir=DEFAULT_DSP_DIR, switch_values=None):
    """Load plan of a .dev.  switch_values = {switch key: value} recomputes the wiring for other positions
    of the routing switches (see rewire())."""
    dv = Device(dev_path, dsp_dir, switch_values)
    res = {"name": dv.root.f.get("name"), "file": os.path.basename(dev_path), "modules": [], "wires": [],
           "ports": [], "consts": [], "params": [], "unmapped_params": [], "switches": list(dv.switches.values()),
           "needs_midi": False, "unsupported": dv.unsupported, "warnings": dv.warnings}
    mods = {}
    for key, at in dv.atoms.items():
        fn = resolve_atom(at["module"], dsp_dir)
        m = {"key": key, "atom": at["module"], "dsp_file": fn, "fixed_dsp": None, "same_dsp_group": None,
             "cycles": None, "placement": None}
        if fn is None and _pc_delay_module(m, at):
            pass
        elif fn is None:
            dv.unsupported.append("no DSP file for atom %r (%s) - host/PC-side or other-board module"
                                  % (at["module"], key))
        else:
            c = module_class(dsp_dir, fn)
            if isinstance(c, Exception):
                dv.unsupported.append("%s: %s" % (fn, c))
            else:
                at["cls"] = c
                fx = (c.flags >> 17) & 0xF
                m["fixed_dsp"] = fx - 1 if fx not in (0, 0xF) else None
                m["cycles"] = c.syncCycles
                if c.flags & POLY_FLAG:
                    dv.unsupported.append("polyphonic DSP module %s (%s)" % (fn, key))
                cin = [p for p in c.pads if p.kind == "in"]
                cout = [p for p in c.pads if p.kind != "in"]
                if len(cin) != len(at["ins"]) or len(cout) != len(at["outs"]):
                    dv.unsupported.append("%s: pad count differs from %s (%d/%d in, %d/%d out)" % (
                        key, fn, len(at["ins"]), len(cin), len(at["outs"]), len(cout)))
                else:
                    for lst, cl in ((at["ins"], cin), (at["outs"], cout)):
                        for v, p in zip(lst, cl):
                            if p.short and _vname(v) and p.short != _vname(v) and p.long != _vname(v):
                                dv.warnings.append("%s: pad %r is %r in %s" % (key, _vname(v), p.short, fn))
                    at["cin"], at["cout"] = cin, cout
                for p in c.pads:
                    if (p.type & 0xF) == 0xE:
                        at["midi"] = True
        mods[key] = m
        res["modules"].append(m)
    for p in dv.placement:
        key = dv.mod_by_id.get(int(p["module"].split(".")[-1], 16)) if p.get("module") else None
        if key in mods and p.get("dsp") and p["dsp"] != [-1]:
            mods[key]["placement"] = p["dsp"]
    # OnSameDSP / DSPId attributes on atom pads
    for key, at in dv.atoms.items():
        for v in at["ins"] + at["outs"]:
            a = _attrs(v)
            if "OnSameDSP" in a and key in mods:
                mods[key]["same_dsp_group"] = a["OnSameDSP"]
            if "DSPId" in a and key in mods:
                mods[key]["fixed_dsp"] = a["DSPId"]

    def cpad(key, d, i):
        at = dv.atoms[key]
        lst = at.get("cin" if d == "in" else "cout")
        return lst[i] if lst and i < len(lst) else None

    # wires, constants, ports
    for r, n in dv.nets.items():
        if len(n["dsp_out"]) > 1:
            dv.unsupported.append("net with %d DSP outputs: %s" % (
                len(n["dsp_out"]), ", ".join("%s.%d" % (k, o) for k, o, _ in n["dsp_out"])))
            continue
        if n["dsp_out"]:
            sk, so, _ = n["dsp_out"][0]
            for dk, di, _ in n["dsp_in"]:
                res["wires"].append({"src_key": sk, "out": so, "dst_key": dk, "in": di})
        for e in n["ext"]:
            if not (n["dsp_in"] or n["dsp_out"] or len(n["ext"]) > 1):
                continue
            # RODPad padbyte: 0 = pad drawn on the input (left) side, 2 = output (right) side [L]
            side = "out" if e.f.get("padbyte") == 2 else "in"
            pdir = "out" if n["dsp_out"] else ("in" if n["dsp_in"] else side)
            tgt = [[k, i] for k, i, _ in (n["dsp_out"] if pdir == "out" else n["dsp_in"])]
            pads = [dv.pads[pid] for _, _, pid in (n["dsp_out"] or n["dsp_in"])]
            sync = any(_attrs(v).get("Sync") for v in pads)
            midi = any(_ptype(v) == "MIDI" for v in pads) or any(
                (cpad(k, pdir, i) is not None and (cpad(k, pdir, i).type & 0xF) == 0xE) for k, i in tgt)
            port = {"name": e.f.get("name"), "dir": pdir, "sync": bool(sync), "midi": midi,
                    "target": tgt[0] if tgt else None, "targets": tgt}
            others = [x.f.get("name") for x in n["ext"] if x is not e]
            if others and side != pdir:
                port["dir"] = side
            if others and not n["dsp_out"] and side == "out":
                port["passthrough_from"] = others      # e.g. a bypassed effect: Out is wired to In
                port["targets"], port["target"] = [], None
            res["ports"].append(port)
            if midi:
                res["needs_midi"] = True
                dv.warnings.append("MIDI port %r: pulsard has no MIDI routing yet" % e.f.get("name"))
        if not n["dsp_out"]:
            for dk, di, pid in n["dsp_in"]:
                v = dv.pads[pid]
                val = _pval(v)
                if isinstance(val, (int, float)) and val != 0 and not any(
                        x["dir"] == "in" and [dk, di] in x["targets"] for x in res["ports"]) \
                        and not any(dv.uf.find(e.f["id"]) == r for e in dv.ext):
                    cp = cpad(dk, "in", di)
                    unit = _unit_of(v, cp)
                    res["consts"].append({"key": dk, "in": di, "pad": _vname(v), "value": val, "unit": unit,
                                          "encoding": _encoding(cp, v, unit)})

    # parameters: every knob/fader/button net, followed through converters to DSP inputs
    seen_nets = set()
    for key, al in dv.algos.items():
        pep = al["pep"]
        if pep not in KNOB_PEPS and pep not in BUTTON_PEPS:
            continue
        vv = al["vars"].get("Val")
        if vv is None or not vv.f.get("id"):
            continue
        r0 = dv.uf.find(vv.f["id"])
        if r0 in seen_nets:
            continue
        seen_nets.add(r0)
        targets, blocked = [], []
        todo, visited = [(r0, [])], set()
        while todo:
            r, chain = todo.pop()
            if r in visited or len(visited) > 50:
                continue
            visited.add(r)
            n = dv.nets.get(r)
            if n is None:
                continue
            if n["dsp_out"]:
                blocked.append("net also driven by DSP output")
                continue
            for dk, di, pid in n["dsp_in"]:
                v = dv.pads[pid]
                cp = cpad(dk, "in", di)
                unit = _unit_of(v, cp)
                targets.append({"key": dk, "in": di, "pad": _vname(v), "unit": unit,
                                "unit_name": UNIT_NAMES.get(unit, str(unit)),
                                "encoding": _encoding(cp, v, unit), "chain": chain,
                                "sync": bool(cp.sync) if cp is not None else bool(_attrs(v).get("Sync"))})
            for hk, hv, pid in n["host"]:
                h = dv.algos[hk]
                if hk == key or h["pep"] in KNOB_PEPS or h["pep"] in TEXT_PEPS or h["pep"] in JUNCTION_PEPS \
                        or h["pep"] in BUTTON_PEPS:
                    continue
                if hk in dv.switches and hv == "Switch":
                    targets.append({"kind": "switch", "key": hk, "chain": chain, "pep": h["pep"],
                                    "choices": dv.switches[hk]["choices"]})
                    continue
                if h["pep"] in GUI_PEPS or h["pep"].startswith("Surfaces@"):
                    continue
                cs = converter_steps(h, hv)
                if cs is None and h["pep"] in CONVERTER_PEPS:
                    continue                # an output pad of a converter (the value came from there)
                if cs is None:
                    blocked.append("host script %s (%s) not emulated" % (h["pep"], hk.rsplit("/", 1)[-1]))
                    continue
                for outvar, steps in cs:
                    if any(s is None for s in steps):
                        blocked.append("bad converter range in %s" % hk)
                        continue
                    ov = h["vars"].get(outvar)
                    if ov is not None and ov.f.get("id"):
                        todo.append((dv.uf.find(ov.f["id"]), chain + steps))
        if not targets:
            if blocked and _under_surface(key):
                res["unmapped_params"].append({"name": _param_name(al, key), "key": key,
                                               "reasons": sorted(set(blocked))})
            continue
        n0 = dv.nets[r0]
        knobs = [dv.algos[hk] for hk, hv, _ in n0["host"] if hv == "Val" and dv.algos[hk]["pep"] in KNOB_PEPS]
        texts = [dv.algos[hk] for hk, hv, _ in n0["host"] if hv == "Val" and dv.algos[hk]["pep"] in TEXT_PEPS]
        knob = (knobs or [al])[0]
        g = lambda a, n, d=0.0: _num(_pval(a["vars"][n]), d) if n in a["vars"] else d    # noqa: E731
        kd = {"pep": knob["pep"], "min": g(knob, "Min"), "max": g(knob, "Max", INTMAX), "curve": int(g(knob, "Curve")),
              "intensity": g(knob, "Intensity", 10.0), "invert": int(g(knob, "Invert")),
              "step": int(g(knob, "Step", 1))}
        if knob["pep"] in BUTTON_PEPS:
            kd["curve"] = 0
        disp = None
        if texts:
            t = texts[0]
            disp = {"pep": t["pep"], "kind": "minmax" if t["pep"] == "TextEditMinMax" else "text",
                    "min": g(t, "Min"), "max": g(t, "Max", INTMAX), "curve": int(g(t, "Curve")),
                    "intensity": g(t, "Intensity", 10.0), "invert": int(g(t, "Invert")),
                    "min_control": g(t, "MinControl", 0), "max_control": g(t, "MaxControl", INTMAX),
                    "format": _sval(t["vars"]["Format"]) if "Format" in t["vars"] else "%1.4f"}
            if disp["kind"] == "minmax":
                disp["offset"] = g(t, "Offset")
                disp["divisor"] = g(t, "Divisor", 1.0) or 1.0
                for s in ("strMin", "strMax", "strMid"):
                    if s in t["vars"] and _sval(t["vars"][s]):
                        disp[s] = _sval(t["vars"][s])
            if disp["max"] <= disp["min"] or disp["max_control"] <= disp["min_control"]:
                disp = None
        elif knob["pep"].startswith("TextFader"):
            fmt = _sval(knob["vars"]["Format"]) if "Format" in knob["vars"] else "%d"
            disp = {"pep": knob["pep"], "kind": "fader", "format": "%.0f" if fmt == "%d" else fmt,
                    "offset": g(knob, "Offset"), "divisor": g(knob, "Divisor", 1.0) or 1.0}
        name = _param_name(knob, knob["key"])
        val = int(g(knob, "Val", kd["min"]))
        p = {"name": name, "key": knob["key"], "control": knob["pep"], "hidden": not _under_surface(knob["key"]),
             "knob": kd,
             "val_min": int(min(kd["min"], kd["max"])), "val_max": int(max(kd["min"], kd["max"])),
             "val_default": val, "display": disp, "targets": targets}
        fmt = disp["format"] if disp else None
        p["display_format"] = fmt
        dsp_t = [t for t in targets if t.get("kind") != "switch"]
        p["unit"] = _unit_from_format(fmt) or ""
        p["pad_unit"] = UNIT_NAMES.get(dsp_t[0]["unit"], "") if dsp_t else ""
        try:
            if disp:
                lo, hi = _text_from_val(disp, p["val_min"]), _text_from_val(disp, p["val_max"])
            else:
                lo, hi = p["val_min"], p["val_max"]
            p["min"], p["max"] = lo, hi
            p["default"] = val_to_display(p, val)
            p["curve"] = _describe_curve(kd, disp)
        except (ValueError, ZeroDivisionError) as e:
            blocked.append("display: %s" % e)
            p["min"], p["max"], p["default"], p["curve"] = p["val_min"], p["val_max"], val, "?"
        if blocked:
            p["partial"] = sorted(set(blocked))
        if kd["step"] > 1 or knob["pep"] in BUTTON_PEPS or kd["max"] - kd["min"] <= 10:
            p["discrete"] = True
        res["params"].append(p)
    # self-check: SCOPE saved each text display's string; recompute it from the saved Val
    chk = [0, 0, []]
    for key, al in dv.algos.items():
        if al["pep"] not in TEXT_PEPS or "Str" not in al["vars"] or "Val" not in al["vars"]:
            continue
        g = lambda n, d=0.0: _num(_pval(al["vars"][n]), d) if n in al["vars"] else d    # noqa: E731
        t = {"kind": "minmax" if al["pep"] == "TextEditMinMax" else "text", "min": g("Min"),
             "max": g("Max", INTMAX), "curve": int(g("Curve")), "intensity": g("Intensity", 10.0),
             "invert": int(g("Invert")), "min_control": g("MinControl", 0), "max_control": g("MaxControl", INTMAX),
             "offset": g("Offset"), "divisor": g("Divisor", 1.0) or 1.0}
        fmt = _sval(al["vars"]["Format"]) if "Format" in al["vars"] else "%1.4f"
        saved = _sval(al["vars"]["Str"])
        if not isinstance(saved, str) or not saved or t["max"] <= t["min"] or t["max_control"] <= t["min_control"]:
            continue
        try:
            mine = c_format(fmt, _text_from_val(t, g("Val")))
        except (ValueError, ZeroDivisionError):
            mine = "?"
        chk[1] += 1
        if mine.strip() == saved.strip() or mine.strip() == ("-" + saved.strip()).replace("--", "-") \
                or _close(mine, saved):
            chk[0] += 1
        else:
            chk[2].append("%s: saved %r, computed %r" % (key, saved, mine))
    res["display_check"] = {"ok": chk[0], "total": chk[1], "mismatch": chk[2][:10]}
    res["license_atoms"] = sorted({m["atom"].strip() for m in res["modules"] if "Package" in (m["atom"] or "")})
    for a in res["license_atoms"]:
        dv.warnings.append("license atom %r: its output stays 0 until the host unlocks it at init" % a)
    if not res["modules"]:
        dv.unsupported.append("no DSP modules (host-only device or empty template)")
    names = {}
    for q in res["params"]:
        names[q["name"]] = names.get(q["name"], 0) + 1
    for q in res["params"]:
        if names[q["name"]] > 1:
            q["name"] = "%s (%s)" % (q["name"], q["key"].rsplit("/", 2)[-2] if q["key"].count("/") > 1 else q["key"])
    res["dsp_cycles"] = sum(m["cycles"] or 0 for m in res["modules"])
    _pc_delay_annotate(res, dv)
    res["complete"] = not dv.unsupported
    dv.unsupported[:] = sorted(set(dv.unsupported))
    res["order"] = _topo(res)
    return res


def _close(a, b):
    """Same number up to the last printed digit (rounding of x.5 cases)."""
    try:
        fa = float(re.match(r"\s*([-+0-9.eE]+)", a).group(1))
        fb = float(re.match(r"\s*([-+0-9.eE]+)", b).group(1))
    except (AttributeError, ValueError):
        return False
    m = re.search(r"\.(\d+)", b)
    q = 10.0 ** -(len(m.group(1)) if m else 0)
    return abs(fa - fb) <= q * 1.01 or (fb != 0 and abs(fa - fb) / abs(fb) < 1e-3)


def _under_surface(key):
    """Panel controls live below the device's surface module (pep Surfaces@BasicSurface ...); knobs elsewhere
    are internal presets of the device."""
    return any(seg in _SURFACE_KEYS for seg in _prefixes(key))


_SURFACE_KEYS = set()


def _prefixes(key):
    parts = key.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def _param_name(al, key):
    name = al["obj"].f.get("name") or key.rsplit("/", 1)[-1]
    if name.startswith("@"):
        name = name[1:]
    if name in ("untitled", "Fader", "Button") or name.startswith("Poti"):
        name = key.rsplit("/", 2)[-2] if key.count("/") > 1 else name
    return name


def _unit_from_format(fmt):
    if not fmt:
        return None
    m = re.search(r"%[-+ 0#]*\d*(?:\.\d+)?[a-zA-Z]([^%]*)$", fmt)
    s = m.group(1).strip() if m else ""
    return s or None


def _describe_curve(kd, disp):
    """How the display value moves with the knob position: knob curve composed with the display curve."""
    names = CURVE_NAMES
    if disp and disp.get("kind") != "fader" and disp["curve"] in (1, 2, 3, 4) and kd["curve"] in (1, 2, 3, 4) \
            and {disp["curve"], kd["curve"]} in ({1, 2}, {3, 4}) and abs(disp["intensity"] - kd["intensity"]) < 1e-9:
        return "linear"             # display curve undoes the knob curve (TextEditMinMax help text)
    k = names.get(kd["curve"], "?")
    if kd["curve"]:
        k += "(%g)" % kd["intensity"]
    if disp and disp.get("kind") != "fader" and disp["curve"]:
        k += "+display_%s(%g)" % (names.get(disp["curve"], "?"), disp["intensity"])
    return k


def _topo(res):
    """Load order: producers before consumers on sync wires (one sample less latency, module_loading.md §5.3)."""
    keys = [m["key"] for m in res["modules"]]
    deps = {k: set() for k in keys}
    for w in res["wires"]:
        if w["src_key"] in deps and w["dst_key"] in deps and w["src_key"] != w["dst_key"]:
            deps[w["dst_key"]].add(w["src_key"])
    out, done = [], set()

    def visit(k, stack=()):
        if k in done or k in stack:
            return
        for d in sorted(deps[k]):
            visit(d, stack + (k,))
        done.add(k)
        out.append(k)
    for k in keys:
        visit(k)
    return out


# ---------------------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------------------

def pulsard_requests(p, rate=48000, inputs=None, outputs=None):
    """pulsard JSON requests that instantiate plan `p` (module node ids are filled in from the replies of
    the 'load' requests: every request refers to modules as {"$": key}).  inputs / outputs map a device port
    name to an existing (node, out) source / list of (node, in) destinations, e.g.
    inputs={"In": ("n4", 0)}, outputs={"Out": [("n6", 0)]}."""
    if p.get("pc_delays"):
        raise ValueError("%s uses host delay lines (%d): build it with 'pulsarctl load_device', the step-by-step "
                         "requests cannot allocate them (docs/pc_delay.md)" % (p["name"], len(p["pc_delays"])))
    reqs = []
    for key in p["order"]:
        m = next(x for x in p["modules"] if x["key"] == key)
        r = {"cmd": "load", "file": m["dsp_file"], "name": key.split("/", 1)[-1]}
        if m["fixed_dsp"] is not None:
            r["dsp"] = m["fixed_dsp"]
        reqs.append(r)
    for w in p["wires"]:
        reqs.append({"cmd": "connect", "src": {"$": w["src_key"]}, "out": w["out"], "dst": {"$": w["dst_key"]},
                     "in": w["in"]})
    for c in p["consts"]:
        reqs.append({"cmd": "set", "id": {"$": c["key"]}, "in": c["in"], "value": const_raw(c, rate)})
    for port in p["ports"]:
        if port["dir"] == "in" and inputs and port["name"] in inputs:
            src, out = inputs[port["name"]]
            for k, i in port["targets"]:
                reqs.append({"cmd": "connect", "src": src, "out": out, "dst": {"$": k}, "in": i})
        if port["dir"] == "out" and outputs and port["name"] in outputs and port["target"]:
            k, o = port["target"]
            for dst, i in outputs[port["name"]]:
                reqs.append({"cmd": "connect", "src": {"$": k}, "out": o, "dst": dst, "in": i})
    return reqs


def _json_default(o):
    return str(o)


def print_plan(p, rate=48000, out=sys.stdout):
    w = out.write
    w("%s (%s): %d modules, %d wires, %d ports, %d params, %d consts, %s cycles  %s\n" % (
        p["name"], p["file"], len(p["modules"]), len(p["wires"]), len(p["ports"]), len(p["params"]),
        len(p["consts"]), p["dsp_cycles"], "COMPLETE" if p["complete"] else "INCOMPLETE"))
    for m in p["modules"]:
        w("  MOD  %-40s %-14s %s%s\n" % (m["key"], m["dsp_file"], "fixed DSP%s " % m["fixed_dsp"]
                                          if m["fixed_dsp"] is not None else "", "%s cyc" % m["cycles"]))
    for x in p["wires"]:
        w("  WIRE %s.out%d -> %s.in%d\n" % (x["src_key"], x["out"], x["dst_key"], x["in"]))
    for x in p["ports"]:
        w("  PORT %-4s %-12s %s -> %s%s\n" % (x["dir"], x["name"], "sync" if x["sync"] else "async",
                                            ", ".join("%s.%s%d" % (k, x["dir"], i) for k, i in x["targets"]),
                                            "(passthrough from %s)" % x["passthrough_from"]
                                            if x.get("passthrough_from") else ""))
    for c in p["consts"]:
        w("  SET  %s.in%d (%s) = %s  [%s, unit %s] raw@%d=0x%08x\n" % (
            c["key"], c["in"], c["pad"], c["value"], c["encoding"], c["unit"], rate, const_raw(c, rate)))
    for q in p["params"]:
        w("  PARAM%s %-18s %s .. %s %s default %s  curve %s  fmt %r%s\n" % (
            "(hidden)" if q.get("hidden") else "", q["name"], _f(q["min"]), _f(q["max"]), q["unit"], _f(q["default"]), q["curve"], q["display_format"],
            "  PARTIAL: " + "; ".join(q["partial"]) if q.get("partial") else ""))
        for t in q["targets"]:
            if t.get("kind") == "switch":
                w("        -> switch %s (%s, choices %s) chain=%s\n" % (
                    t["key"], t["pep"], t["choices"], "+".join(s["op"] for s in t["chain"]) or "id"))
                continue
            w("        -> %s.in%d (%s) %s unit=%s chain=%s raw(default)@%d=0x%08x\n" % (
                t["key"], t["in"], t["pad"], t["encoding"], t["unit_name"] or t["unit"],
                "+".join(s["op"] for s in t["chain"]) or "id", rate,
                pad_encode(apply_chain(t["chain"], q["val_default"]), t["encoding"], t["unit"], rate)))
    for x in p["switches"]:
        w("  SWITCH %s (%s) = %s: %s -> %s\n" % (x["key"], x["pep"], x["value"], x["selected"], x["common"]))
    for q in p["unmapped_params"]:
        w("  NOPARAM %s: %s\n" % (q["name"], "; ".join(q["reasons"])))
    for u in p["unsupported"]:
        w("  BLOCK %s\n" % u)
    for u in sorted(set(p["warnings"]))[:20]:
        w("  warn  %s\n" % u)


def _f(v):
    return "%g" % v if isinstance(v, (int, float)) else str(v)


def _survey_one(args):
    path, dsp_dir = args
    try:
        p = plan(path, dsp_dir)
        return {"file": path, "name": p["name"], "complete": p["complete"], "unsupported": p["unsupported"],
                "modules": len(p["modules"]), "params": len(p["params"]), "wires": len(p["wires"]),
                "ports": [(x["name"], x["dir"]) for x in p["ports"]], "cycles": p["dsp_cycles"],
                "partial": sum(1 for q in p["params"] if q.get("partial")),
                "needs_midi": p["needs_midi"], "display_check": [p["display_check"]["ok"],
                                                                 p["display_check"]["total"]],
                "license": p["license_atoms"], "unmapped": [q["name"] for q in p["unmapped_params"]],
                "switch_params": sum(1 for q in p["params"] if any(t.get("kind") == "switch" for t in q["targets"])),
                }
    except Exception as e:
        return {"file": path, "error": "%s: %s" % (type(e).__name__, e)}


def _reason_class(u):
    if u.startswith("no DSP file"):
        m = re.search(r"atom '([^']*)'|atom \"([^\"]*)\"", u)
        return "host/PC atom " + (m.group(1) or m.group(2) if m else "")
    if u.startswith("polyphonic"):
        return "polyphonic"
    if u.startswith("MIDI port"):
        return "MIDI port"
    if u.startswith("net with"):
        return "net with several DSP outputs"
    if "pad count differs" in u:
        return "voice-array pads (atom pad count != DSP pad count: MIDI Voice Control / voice mixers)"
    return re.sub(r"[\d.]+", "N", u.split(":")[0])[:60]


def survey(paths, dsp_dir, jobs=4):
    files = []
    for p in paths:
        if os.path.isdir(p):
            for r, _, fs in os.walk(p):
                files += [os.path.join(r, f) for f in fs if f.lower().endswith(".dev")]
        else:
            files.append(p)
    files.sort()
    dsp_index(dsp_dir)
    if jobs > 1:
        from multiprocessing import Pool
        with Pool(jobs) as pool:
            rows = pool.map(_survey_one, [(f, dsp_dir) for f in files], chunksize=1)
    else:
        rows = [_survey_one((f, dsp_dir)) for f in files]
    return rows


def main():
    ap = argparse.ArgumentParser(description="SCOPE .dev -> pulsard load plan")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("plan")
    a1.add_argument("file")
    a1.add_argument("--dsp", default=DEFAULT_DSP_DIR)
    a1.add_argument("--json", action="store_true")
    a1.add_argument("--rate", type=int, default=48000)
    a2 = sub.add_parser("survey")
    a2.add_argument("paths", nargs="+")
    a2.add_argument("--dsp", default=DEFAULT_DSP_DIR)
    a2.add_argument("--jobs", type=int, default=4)
    a2.add_argument("--json", metavar="OUT")
    a4 = sub.add_parser("calls", help="print the pulsarctl commands that build the device")
    a4.add_argument("file")
    a4.add_argument("--dsp", default=DEFAULT_DSP_DIR)
    a4.add_argument("--rate", type=int, default=48000)
    a4.add_argument("--in", dest="ins", action="append", default=[], metavar="PORT=NODE:OUT")
    a4.add_argument("--out", dest="outs", action="append", default=[], metavar="PORT=NODE:IN")
    a3 = sub.add_parser("raw")
    a3.add_argument("file")
    a3.add_argument("param")
    a3.add_argument("value", type=float)
    a3.add_argument("--dsp", default=DEFAULT_DSP_DIR)
    a3.add_argument("--rate", type=int, default=48000)
    a5 = sub.add_parser("presets", help="list a device's presets, or show preset N mapped to its parameters")
    a5.add_argument("file", help=".dev (presets of the device) or .pre (raw preset file)")
    a5.add_argument("preset", nargs="?", help="preset index or name")
    a5.add_argument("--pre", help="preset file (default: Presets/<Caption>.pre, see find_preset_file)")
    a5.add_argument("--dev", help="with a .pre FILE: device that names the values")
    a5.add_argument("--dsp", default=DEFAULT_DSP_DIR)
    a5.add_argument("--rate", type=int, default=48000)
    a5.add_argument("--json", action="store_true")
    a = ap.parse_args()
    sys.setrecursionlimit(20000)
    if a.cmd == "presets":
        return _presets_cli(a)
    if a.cmd == "plan":
        p = plan(a.file, a.dsp)
        if a.json:
            json.dump(p, sys.stdout, indent=1, default=_json_default)
            print()
        else:
            print_plan(p, a.rate)
        return 0
    if a.cmd == "calls":
        p = plan(a.file, a.dsp)
        ins = {x.split("=")[0]: (x.split("=")[1].split(":")[0], int(x.split(":")[1])) for x in a.ins}
        outs = {}
        for x in a.outs:
            outs.setdefault(x.split("=")[0], []).append((x.split("=")[1].split(":")[0], int(x.split(":")[1])))
        ref = lambda v: ("<%s>" % v["$"].split("/", 1)[-1]) if isinstance(v, dict) else v   # noqa: E731
        for r in pulsard_requests(p, a.rate, ins, outs):
            if r["cmd"] == "load":
                print("pulsarctl load %s%s name=%r     # -> node id of <%s>" % (
                    r["file"], " dsp=%d" % r["dsp"] if "dsp" in r else "", r["name"], r["name"]))
            elif r["cmd"] == "connect":
                print("pulsarctl connect src=%s out=%d dst=%s in=%d" % (ref(r["src"]), r["out"], ref(r["dst"]), r["in"]))
            else:
                print("pulsarctl set id=%s in=%d value=0x%08x" % (ref(r["id"]), r["in"], r["value"]))
        if not p["complete"]:
            print("# INCOMPLETE: " + "; ".join(p["unsupported"]))
        return 0
    if a.cmd == "raw":
        p = plan(a.file, a.dsp)
        for q in p["params"]:
            if q["name"] == a.param:
                for k, i, r in targets_raw(q, a.value, a.rate):
                    print("%s in%d = 0x%08x (%d)" % (k, i, r, r - (1 << 32) if r & 0x80000000 else r))
                return 0
        print("no param %r; have: %s" % (a.param, ", ".join(q["name"] for q in p["params"])))
        return 1
    rows = survey(a.paths, a.dsp, a.jobs)
    from collections import Counter
    reasons, ok, err = Counter(), [], []
    for r in rows:
        if "error" in r:
            err.append(r)
            continue
        if r["complete"]:
            ok.append(r)
        for c in {_reason_class(u) for u in r["unsupported"]}:
            reasons[c] += 1
    print("devices: %d, complete plans: %d, incomplete: %d, errors: %d" % (
        len(rows), len(ok), len(rows) - len(ok) - len(err), len(err)))
    print("blocking features (number of devices):")
    for c, n in reasons.most_common(40):
        print("  %4d  %s" % (n, c))
    print("complete: %d (of which need MIDI input: %d, contain a license atom: %d)" % (
        len(ok), sum(1 for r in ok if r["needs_midi"]), sum(1 for r in ok if r["license"])))
    dc = [r["display_check"] for r in rows if "display_check" in r]
    print("display formula check (saved text strings recomputed from saved values): %d / %d" % (
        sum(a for a, b in dc), sum(b for a, b in dc)))
    base = os.path.commonpath([x["file"] for x in rows]) if len(rows) > 1 else "."
    for r in sorted(ok, key=lambda r: (r["needs_midi"], r["modules"], r["file"])):
        print("  %-50s mods %3d cyc %5s params %3d (partial %d, switch %d, unmapped %d)%s ports %s" % (
            os.path.relpath(r["file"], base), r["modules"], r["cycles"], r["params"], r["partial"],
            r["switch_params"], len(r["unmapped"]), (" MIDI" if r["needs_midi"] else "") +
            (" LIC" if r["license"] else ""),
            " ".join("%s:%s" % tuple(pr) for pr in r["ports"])))
    for r in err:
        print("  ERROR %s: %s" % (r["file"], r["error"]))
    if a.json:
        with open(a.json, "w") as f:
            json.dump(rows, f, indent=1, default=_json_default)
    return 0


# ---------------------------------------------------------------------------------------------------------
# Polyphony: voice arrays and per-voice instances (docs/midi_synths.md §3)
#
# base.dll ROCAtom::LoadAtom @100bb3d0 / VoiceModuleIn @100532e0 / VoiceModuleOut @100533e0 /
# RouteInput @10053580 / ChangeVoices @1008ade0 / ForceSingleVoice @1008b030, Sim2k SetVoices @10c06a00 ->
# FUN_10c17de0.  Kept apart from plan(): voice_plan() post-processes a finished plan.
# ---------------------------------------------------------------------------------------------------------

VOICE_MARK = 0x2000             # pad type bit: first pad of a voice record [C]
SINGLE_ATOM = 0x10000           # module flag: one instance for all voices (array pads + SetVoices) [C]
ARRAY_PARAM_MASK = 0x00F00000   # input pad type bits of a PROCParam (table) input: 0x80/0x20/0x10 [L]
FORCE_SINGLE_EXTRA = "0x108"    # ROCAtom extras chunk 0x108 -> ForceSingleVoice (ROCAtom::ReadExtraChunk) [C]


def voice_layout(cls):
    """Voice-array structure of a DSP module class, as base.dll LoadAtom derives it from the pad types.

    A record starts at a pad with type bit 0x2000; the distance to the next marker is the record length.
    Returns None for modules without arrays, else a dict:
      n                internal voices (array length)
      in_first/in_rec  first array input / inputs per voice (0 = no input array)
      ao_first/ao_rec  same for async outputs, so_first/so_rec for sync outputs
      atom_in/atom_ao/atom_so  pad counts the atom shows (first record only)
    """
    ins = [p for p in cls.pads if p.kind == "in"]
    aos = [p for p in cls.pads if p.kind == "async_out"]
    sos = [p for p in cls.pads if p.kind == "sync_out"]

    def rec(pads):
        marks = [i for i, p in enumerate(pads) if p.type & VOICE_MARK]
        if len(marks) < 2:
            return 0, 0, 0
        first, ln = marks[0], marks[1] - marks[0]
        n = (len(pads) - first) // ln
        return first, ln, n

    (inf, inr, inn), (aof, aor, aon), (sof, sor, son) = rec(ins), rec(aos), rec(sos)
    ns = [x for x in (inn, aon, son) if x > 1]
    if not ns:
        return None
    n = min(ns)
    return {"n": n, "consistent": len(set(ns)) == 1,
            "in_first": inf, "in_rec": inr, "ao_first": aof, "ao_rec": aor, "so_first": sof, "so_rec": sor,
            "atom_in": len(ins) - (n - 1) * inr, "atom_ao": len(aos) - (n - 1) * aor,
            "atom_so": len(sos) - (n - 1) * sor, "num_ao": len(aos)}


def _vmap(i, first, ln, n, v):
    """Atom pad index -> module pad index for voice v (globals before and after the array keep their place)."""
    if not ln or i < first:
        return i
    if i < first + ln:
        return i + v * ln
    return i + (n - 1) * ln


def voice_in_pad(lay, i, v):
    """Module input pad of atom input i for voice v."""
    return i if lay is None else _vmap(i, lay["in_first"], lay["in_rec"], lay["n"], v)


def voice_out_pad(lay, k, v):
    """Module output pad (async outs first, then sync) of atom output k for voice v."""
    if lay is None:
        return k
    if k < lay["atom_ao"]:
        return _vmap(k, lay["ao_first"], lay["ao_rec"], lay["n"], v)
    j = k - lay["atom_ao"]
    return lay["num_ao"] + _vmap(j, lay["so_first"], lay["so_rec"], lay["n"], v)


def _is_array_in(lay, i):
    return lay is not None and lay["in_rec"] and lay["in_first"] <= i < lay["in_first"] + lay["in_rec"]


def _is_array_out(lay, k):
    if lay is None:
        return False
    if k < lay["atom_ao"]:
        return bool(lay["ao_rec"]) and lay["ao_first"] <= k < lay["ao_first"] + lay["ao_rec"]
    j = k - lay["atom_ao"]
    return bool(lay["so_rec"]) and lay["so_first"] <= j < lay["so_first"] + lay["so_rec"]


def voice_cycles(cycles, voices):
    """seg_desc cycle words of voice-array modules: low 16 bits fixed + high 16 bits per voice [L]
    (16MIX 0x20008, M_V_M16E 0xa0013)."""
    if cycles is None:
        return None
    return (cycles & 0xFFFF) + (cycles >> 16) * max(1, voices)


def protected_atoms(p, dsp_dir=DEFAULT_DSP_DIR):
    """Plan modules whose DSP file carries the copy-protection pattern (symbol 'magicProt': seg_init only
    enables the real code when the host wrote the right word).  More general than the 'Package' name test:
    every factory synth has exactly one of these (EDS 16i, Poison FM, Synthesizer Package I/II, ...)."""
    out = []
    for m in p["modules"]:
        c = module_class(dsp_dir, m["dsp_file"]) if m.get("dsp_file") else None
        if c is None or isinstance(c, Exception):
            continue
        if any(s.name == "magicProt" for s in c.obj.symbols):
            out.append(m["key"])
    return out


def _input_default_symbols(cls):
    """input index -> symbol the module's own seg_mod relocation points the input word at (e.g. VelTab,
    def_E_Sync); 'inputN' / '_null' mean 'no own default'."""
    res = {}
    sec = next((s for s in cls.obj.sections if s.name.startswith("seg_mod")), None)
    if sec is None:
        return res
    for (vaddr, symndx, rtype) in sec.relocs:
        off = vaddr - sec.vaddr
        i = off - cls.off_input(0)
        if 0 <= i < cls.numIn:
            name = cls.obj.symbyidx[symndx].name
            if not re.fullmatch(r"input\d+|_null", name):
                res[i] = name
    return res


def voice_plan(dev_path, voices=4, dsp_dir=DEFAULT_DSP_DIR, scope_exact=False, base=None):
    """Plan of a polyphonic device with the voices expanded for pulsard / pulsar_modules.

    * Voice-array atoms (module flag 0x10000 or extras 0x108 = single instance, with 0x2000 pad records, e.g.
      MIDI Voice Control, Mixer 16) stay ONE module; their atom pads are mapped to the module pads of each
      voice (wires/consts/ports then use real module pad numbers) and the module gets SetVoices(n)
      (entry in "set_voices").
    * Poly-capable atoms (flag 0x10000 clear, no 0x108) that depend on a voice (fed, directly or through other
      such atoms, by an array output) are instantiated once per voice: key, key#v1, key#v2 ...
      SCOPE instantiates every poly-capable atom n times and connects only voice 0 into mono inputs; the extra
      copies of atoms that see only mono signals compute the same thing, so they are skipped unless
      scope_exact=True.
    * PROCParam table inputs (Tune/Velocity/Aftertouch tables, MIDI FIFOs) are listed in "tables" with the
      element count and the values stored in the device; "keep_default" tells that the module has its own table
      (seg_mod relocation of the input word) and the stored values are all zero.

    voices is clamped to the smallest array of the device.  The result has the plan() layout plus
    "voices", "set_voices" [{key, voices}], "tables" [{key, in, pad, elements, values, keep_default}] and
    per module "voice" / "voice_of" (copies) or "voice_layout" (array modules)."""
    p = base if base is not None else plan(dev_path, dsp_dir)
    dv = Device(dev_path, dsp_dir)
    mods = {m["key"]: m for m in p["modules"]}
    kind, lay, cls_of = {}, {}, {}
    unsupported = [u for u in p["unsupported"]]
    for key, m in mods.items():
        c = module_class(dsp_dir, m["dsp_file"]) if m.get("dsp_file") else None
        if c is None or isinstance(c, Exception):
            kind[key] = "single"
            continue
        cls_of[key] = c
        at = dv.atoms.get(key)
        a = at["obj"].f.get("algo") if at else None
        forced = any(str(x.get("key", "")).startswith(FORCE_SINGLE_EXTRA)
                     for x in ((a.f.get("extras") or []) if a is not None else []))
        L = voice_layout(c)
        lay[key] = L
        if L is not None:
            if not (c.flags & SINGLE_ATOM or forced):
                unsupported.append("%s: poly module %s with internal voices (VoiceDef groups) not handled"
                                   % (key, m["dsp_file"]))
            kind[key] = "array"
            if at is not None and (len(at["ins"]) != L["atom_in"] or
                                   len(at["outs"]) != L["atom_ao"] + L["atom_so"]):
                continue                     # leave the 'pad count differs' blocker in place
            unsupported = [u for u in unsupported if not (u.startswith(key + ": pad count differs") or
                                                          u == "polyphonic DSP module %s (%s)" % (m["dsp_file"], key))]
            if not L["consistent"]:
                p["warnings"].append("%s: input/output voice counts differ in %s" % (key, m["dsp_file"]))
        elif c.flags & SINGLE_ATOM or forced:
            kind[key] = "single"
        else:
            kind[key] = "poly"
    unsupported = [u for u in unsupported if not u.startswith("polyphonic: ")]
    arrays = [k for k, v in kind.items() if v == "array"]
    n = max(1, min([voices] + [lay[k]["n"] for k in arrays]))

    # voice-dependent poly atoms (fixpoint over the wires)
    vd = set(k for k, v in kind.items() if v == "poly") if scope_exact else set()
    changed = not scope_exact
    while changed:
        changed = False
        for w in p["wires"]:
            s, d = w["src_key"], w["dst_key"]
            if kind.get(d) != "poly" or d in vd:
                continue
            if s in vd or (kind.get(s) == "array" and _is_array_out(lay[s], w["out"])):
                vd.add(d)
                changed = True

    def copies(key):
        return [key] + ["%s#v%d" % (key, v) for v in range(1, n)] if key in vd else [key]

    def dst_list(key, i):
        """[(instance key, module in pad, voice)] that atom input (key, i) stands for."""
        if key in vd:
            return [(k, i, v) for v, k in enumerate(copies(key))]
        L = lay.get(key)
        if kind.get(key) == "array" and _is_array_in(L, i):
            return [(key, voice_in_pad(L, i, v), v) for v in range(n)]
        return [(key, voice_in_pad(L, i, 0), 0)]

    def src_for(key, k, v):
        """(instance key, module out pad) feeding voice v from atom output (key, k)."""
        if key in vd:
            return copies(key)[v], k
        L = lay.get(key)
        if kind.get(key) == "array":
            return key, voice_out_pad(L, k, v if _is_array_out(L, k) else 0)
        return key, k

    res = dict(p)
    res["voices"] = n
    res["modules"] = []
    for m in p["modules"]:
        key = m["key"]
        if key in vd:
            for v, k in enumerate(copies(key)):
                res["modules"].append(dict(m, key=k, voice=v, voice_of=key))
        elif kind.get(key) == "array":
            c = cls_of[key]
            res["modules"].append(dict(m, voice_layout=lay[key], voices=n,
                                       cycles=voice_cycles(c.syncCycles, n)))
        else:
            res["modules"].append(dict(m))
    res["wires"] = []
    for w in p["wires"]:
        for dk, di, v in dst_list(w["dst_key"], w["in"]):
            sk, so = src_for(w["src_key"], w["out"], v)     # mono destination: v = 0 (RouteInput)
            res["wires"].append({"src_key": sk, "out": so, "dst_key": dk, "in": di})
    res["consts"] = []
    for cst in p["consts"]:
        for dk, di, v in dst_list(cst["key"], cst["in"]):
            res["consts"].append(dict(cst, key=dk, **{"in": di}))
    res["params"] = []
    for q in p["params"]:
        q2 = dict(q)
        tg = []
        for t in q.get("targets", []):
            if t.get("kind") == "switch" or "in" not in t:
                tg.append(t)
                continue
            for dk, di, v in dst_list(t["key"], t["in"]):
                tg.append(dict(t, key=dk, **{"in": di}))
        q2["targets"] = tg
        res["params"].append(q2)
    res["ports"] = []
    for port in p["ports"]:
        q = dict(port)
        if port["dir"] == "in":
            q["targets"] = [[dk, di] for k, i in port["targets"] for dk, di, _ in dst_list(k, i)]
        else:
            q["targets"] = [list(src_for(k, o, 0)) for k, o in port["targets"]]
        q["target"] = q["targets"][0] if q["targets"] else None
        res["ports"].append(q)
    res["set_voices"] = [{"key": k, "voices": n} for k in arrays]
    # PROCParam tables
    tables = []
    for key, at in dv.atoms.items():
        c = cls_of.get(key)
        if c is None or key not in mods:
            continue
        defaults = _input_default_symbols(c)
        for i, v in enumerate(at["ins"]):
            mp = voice_in_pad(lay.get(key), i, 0)
            if mp >= c.numIn or not (c.pads[mp].type & ARRAY_PARAM_MASK):
                continue
            par = v.f.get("param") or {}
            items = [it.get("value", 0) for it in (par.get("items") or [])]
            elements = int(_num((par.get("attrs") or {}).get("DataElements"), len(items) or 0))
            vals = [int(_num(x)) for x in items]
            for k2 in copies(key) if key in vd else [key]:
                tables.append({"key": k2, "in": mp, "pad": _vname(v), "elements": elements, "values": vals,
                               "default_symbol": defaults.get(mp),
                               "keep_default": bool(defaults.get(mp)) and not any(vals)})
    res["tables"] = tables
    lic = protected_atoms(res, dsp_dir)
    res["protected_atoms"] = sorted({m["voice_of"] if "voice_of" in m else m["key"]
                                     for m in res["modules"] if m["key"] in lic})
    res["unsupported"] = sorted(set(unsupported))
    res["dsp_cycles"] = sum(m["cycles"] or 0 for m in res["modules"])
    res["complete"] = not res["unsupported"]
    res["order"] = _topo(res)
    return res


def print_voice_plan(p, out=sys.stdout):
    out.write("%s: %d voices, %d modules, %d wires, %d cycles%s\n" % (
        p["name"], p["voices"], len(p["modules"]), len(p["wires"]), p["dsp_cycles"],
        "" if p["complete"] else "  INCOMPLETE"))
    for m in p["modules"]:
        extra = ""
        if "voice_layout" in m:
            L = m["voice_layout"]
            extra = "ARRAY n=%d (in %d+%d*v, async out %d+%d*v, sync out %d+%d*v)" % (
                L["n"], L["in_first"], L["in_rec"], L["ao_first"], L["ao_rec"], L["so_first"], L["so_rec"])
        elif "voice" in m:
            extra = "voice %d" % m["voice"]
        out.write("  MOD  %-60s %-14s %6s cyc  %s\n" % (m["key"], m["dsp_file"], m["cycles"], extra))
    for s in p["set_voices"]:
        out.write("  SETVOICES %s = %d\n" % (s["key"], s["voices"]))
    for t in p["tables"]:
        out.write("  TABLE %s in%d (%s): %d elements%s%s\n" % (
            t["key"], t["in"], t["pad"], t["elements"], ", nonzero" if any(t["values"]) else ", zero",
            " (module default %s kept)" % t["default_symbol"] if t["keep_default"] else ""))
    for a in p["protected_atoms"]:
        out.write("  PROTECTED %s (copy-protection atom: silent until unlocked by the host)\n" % a)
    for u in p["unsupported"]:
        out.write("  BLOCKER %s\n" % u)


# ---------------------------------------------------------------------------------------------------------
# presets (.pre files and the preset list embedded in a .dev) - docs/presets_license.md
# ---------------------------------------------------------------------------------------------------------
#
# File (.pre): container = plain gzip (format -2) or S3 (format -3), see scope_dev.unpack_container.
#   u32 header version (1 or 2), "Creamware Scope technology preset file\0", u32 size, then `size` bytes read
#   by a MemInterpreter (base.dll Presets::LoadFromExternalFile @100b85e0, header FUN_10025c80,
#   Presets::Read @100b3860 -> Presets::Read2(MemInterpreter&) @100ab0d0).
# MemInterpreter (cwWindows.dll) alignment, relative to the start of the block: LONG/DWORD 4 bytes,
#   GetBlock 8 bytes (@1023b0f0), BYTE and NUL-terminated strings unaligned.
# The device's own list is the same block in pad 'PLData' of its Controls@PresetList script.

PRESET_MAGIC = b"Creamware Scope technology preset file\0"
PRESET_INFO_TYPES = {1: "date", 2: "author", 3: "description", 4: "category", 5: "ask_synapse"}  # preset.ped


class _PresetReader:
    """cwWindows MemInterpreter read side."""

    def __init__(self, data, base):
        self.d, self.base, self.p = data, base, base

    def _align(self, n):
        r = (self.p - self.base) % n
        if r:
            self.p += n - r

    def byte(self):
        if self.p >= len(self.d):
            raise sd.FormatError("preset data truncated")
        v = self.d[self.p]
        self.p += 1
        return v

    def long(self):
        self._align(4)
        if self.p + 4 > len(self.d):
            raise sd.FormatError("preset data truncated")
        v = struct.unpack_from("<i", self.d, self.p)[0]
        self.p += 4
        return v

    def count(self, limit=100000):
        n = self.long()
        if n < 0 or n > limit:
            raise sd.FormatError("bad preset count %d @0x%x" % (n, self.p - 4))
        return n

    def block(self, n):
        self._align(8)
        if n < 0 or self.p + n > len(self.d):
            raise sd.FormatError("preset block out of range @0x%x" % self.p)
        v = self.d[self.p:self.p + n]
        self.p += n
        return v

    def string(self):
        try:
            e = self.d.index(b"\0", self.p)
        except ValueError:
            raise sd.FormatError("unterminated preset string @0x%x" % self.p)
        v = self.d[self.p:e].decode("latin-1")
        self.p = e + 1
        return v

    def rocparam(self):
        """base.dll FUN_1007b280: LONG DataType, then (type 10 = array) LONG n + n params, else LONG size +
        block.  Returns (type, python value)."""
        t = self.long()
        if t == 10:
            return t, [self.rocparam() for _ in range(self.count())]
        n = self.count(0x1000000)
        b = self.block(n) if n > 0 else b""
        if t == 1 and n == 4:
            return t, struct.unpack("<i", b)[0]
        if t == 2 and n == 4:
            return t, struct.unpack("<f", b)[0]
        if t == 3 and n == 8:
            return t, struct.unpack("<d", b)[0]
        if t == 5:
            return t, b.split(b"\0")[0].decode("latin-1")
        return t, b.hex()

    def info_user(self):
        """PresetInfoUser::Read2Mem @1009e9f0: name, LONG number, LONG n, n x PresetInfo::Read2 @1007d0f0
        (name, ROCParam, LONG type)."""
        name, number = self.string(), self.long()
        infos = {}
        for _ in range(self.count()):
            iname = self.string()
            _t, v = self.rocparam()
            itype = self.long()
            infos[PRESET_INFO_TYPES.get(itype, iname or str(itype))] = v
        return name, number, infos

    def ref_preset(self):
        """ReferencePreset (FUN_100740a0): values for a child device loaded at run time."""
        name = self.string()
        path = [self.long() & 0xFFFFFFFF for _ in range(self.count())]
        vals = []
        for _ in range(self.count()):
            uid = self.block(16).hex()
            pcid = self.long() & 0xFFFFFFFF
            v = self.rocparam() if self.byte() else None
            vals.append({"uid": uid, "pcid": pcid, "type": v and v[0], "value": v and v[1]})
        subs = [self.ref_preset() for _ in range(self.count())]
        return {"device": name, "pcid_path": path, "values": vals, "subs": subs}

    def category(self):
        """PresetCategory::Read2 @100aac10."""
        name, number, infos = self.info_user()
        idx = [self.long() for _ in range(self.count())]
        subs = [self.category() for _ in range(self.count())]
        return {"name": name, "number": number, "infos": infos, "presets": idx, "subs": subs}

    def presets(self):
        """Presets::Read2(MemInterpreter&) @100ab0d0."""
        ver = self.long()
        if ver >= 0 or ver < -3:
            raise sd.FormatError("preset list format %d not supported" % ver)
        if ver == -1:
            raise sd.FormatError("old preset list format -1 (Preset::ReadOld @100b3390) not implemented")
        if ver == -2:                       # two flag bytes (+2 DWORDs) and a DWORD, not used here
            self.byte()
            if self.byte():
                self.long(), self.long()
            self.long()
        params = []
        for _ in range(self.count()):       # UnresolvedPresetParameter: ParamUID (+ module PCID in -3)
            uid = self.block(16).hex()
            params.append({"uid": uid, "pcid": (self.long() & 0xFFFFFFFF) if ver == -3 else 0})
        out = []
        for _ in range(self.count()):
            # -2: Preset::Read2 @100b35e0 (values: LONG ref, BYTE has, ROCParam)
            # -3: Preset::Read2b @100aa6a0 (values: BYTE has, ROCParam, LONG ref)
            name, number, infos = self.info_user()
            vals = []
            for _ in range(self.count()):
                if ver == -2:
                    ref = self.long()
                    v = self.rocparam() if self.byte() else None
                else:
                    v = self.rocparam() if self.byte() else None
                    ref = self.long()
                vals.append((ref, v))
            cats = [self.long() for _ in range(self.count())]
            if ver == -2:
                refs = [self.presets() for _ in range(self.count())]
            else:
                refs = [self.ref_preset() for _ in range(self.count())]
            out.append({"name": name, "number": number, "infos": infos, "values": vals, "categories": cats,
                        "refs": refs})
        root = self.category()
        return {"format": ver, "params": params, "presets": out, "root": root}


def read_preset_block(data, base=0):
    """Decode one Presets block (the payload of a .pre file or of a PLData pad)."""
    return _PresetReader(data, base).presets()


def read_pre_file(path):
    """Decode a .pre file: {'format', 'params': [{uid, pcid}], 'presets': [...], 'root': category tree}."""
    with open(path, "rb") as f:
        plain, _ = sd.unpack_container(f.read())
    if len(plain) < 8 + len(PRESET_MAGIC) or plain[4:4 + len(PRESET_MAGIC)] != PRESET_MAGIC:
        raise sd.FormatError("%s: not a SCOPE preset file" % path)
    hv = struct.unpack_from("<I", plain, 0)[0]
    if hv < 1 or hv > 2:
        raise sd.FormatError("%s: preset header version %d" % (path, hv))
    p = 4 + len(PRESET_MAGIC)
    size = struct.unpack_from("<I", plain, p)[0]
    rd = _PresetReader(plain[:p + 4 + size], p + 4)
    res = rd.presets()
    if rd.p != p + 4 + size:
        raise sd.FormatError("%s: %d bytes left after the preset list" % (path, p + 4 + size - rd.p))
    return res


def _category_paths(root):
    """preset index -> category path ('Factory Presets/Pads' ...)."""
    out = {}

    def walk(c, path):
        here = path + [c["name"]] if c["name"] and (not path or path[-1] != c["name"]) else path
        for i in c["presets"]:
            out.setdefault(i, "/".join(here))
        for s in c["subs"]:
            walk(s, here)
    walk(root, [])
    return out


class _RawParser(sd.Parser):
    """scope_dev.Parser that keeps the raw bytes of every ROCParam (needed for PLData)."""

    def rocparam(self):
        start = self.r.p
        p = super().rocparam()
        size = struct.unpack_from("<i", self.d, start)[0]
        p["_raw"] = self.d[start + 4:start + 4 + size]
        return p


_PDEV = {}


def device_parameters(dev_path):
    """ROCParameter objects of a .dev (extras chunk 0x110 of the modules: u32 count + objects, parsed by
    scope_dev.p_rocparameter = ROCParameter::Serialize @100858d0).  {uid hex: {name, id, target}}; target
    is the pad id the parameter drives (ROCAtomPad, ROCPad or RODPad)."""
    if dev_path in _PDEV:
        return _PDEV[dev_path]
    plain, _ = sd.load(dev_path)
    out = {}
    for m in re.finditer(rb"\x10\x01\x00\x00(....)(....)(?=\x0cROCParameter)", plain, re.S):
        ln, n = struct.unpack("<II", m.group(1) + m.group(2))
        ps = sd.Parser(plain)
        ps.r.p = m.end()
        try:
            for _ in range(n):
                o = ps.obj(ps.classname())
                uid = o.f.get("raw16")
                if uid and o.cls == "ROCParameter":
                    out[uid] = {"name": o.f.get("name"), "id": o.f.get("id"), "target": o.f.get("target")}
        except sd.FormatError:
            continue
    _PDEV.clear()
    _PDEV[dev_path] = out
    return out


def embedded_presets(dev_path):
    """The preset lists stored in the .dev itself: pad 'PLData' of every Controls@PresetList script.
    [(script key, decoded block)]."""
    dv = Device(dev_path, None)
    plain, _ = sd.load(dev_path)
    out = []
    for key, al in dv.algos.items():
        if al["pep"] != "Controls@PresetList" or "PLData" not in al["vars"]:
            continue
        ps = _RawParser(plain)
        ps.r.p = al["vars"]["PLData"].off
        try:
            raw = ps.obj(al["vars"]["PLData"].cls).f["param"].get("_raw") or b""
        except sd.FormatError:
            continue
        if len(raw) >= 8:
            try:
                out.append((key, read_preset_block(raw)))
            except sd.FormatError:
                pass
    return out


def preset_caption(dev_path):
    """Name SCOPE uses for the default preset file: Caption pad of the root Controls@PresetList (PresetList.pep:
    <StaticPath Presets>/<Caption>.pre), else the device name."""
    dv = Device(dev_path, None)
    best = None
    for key, al in dv.algos.items():
        if al["pep"] == "Controls@PresetList" and "Caption" in al["vars"]:
            c = _sval(al["vars"]["Caption"])
            if c and (best is None or key.count("/") < best[0]):
                best = (key.count("/"), c)
    return best[1] if best else dv.root.f.get("name")


PRESET_DIRS = ["/var/lib/snd-pulsar/presets"]


def find_preset_file(dev_path, extra_dirs=None):
    """Default .pre of a device: a 'Presets' folder next to (or above) the devices folder, or PRESET_DIRS."""
    name = preset_caption(dev_path) + ".pre"
    dirs = list(extra_dirs or []) + PRESET_DIRS
    d = os.path.dirname(os.path.abspath(dev_path))
    for _ in range(8):
        dirs.append(os.path.join(d, "Presets"))
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    for d in dirs:
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
        try:                                    # case-insensitive match (files copied from Windows)
            for f in os.listdir(d):
                if f.lower() == name.lower():
                    return os.path.join(d, f)
        except OSError:
            pass
    return None


def _list_presets(block, source, names=None):
    names = names or {}
    cats = _category_paths(block["root"])
    out = []
    for i, p in enumerate(block["presets"]):
        vals, uids = {}, {}
        for ref, v in p["values"]:
            if v is None or not 0 <= ref < len(block["params"]):
                continue
            uid = block["params"][ref]["uid"]
            uids[uid] = v[1]
            n = names.get(uid, {}).get("name") or uid
            k, j = n, 2
            while k in vals:                    # two parameters with the same name (PosX of two panels)
                k, j = "%s#%d" % (n, j), j + 1
            vals[k] = v[1]
        e = {"index": i, "name": p["name"], "source": source, "category": cats.get(i, ""), "values": vals,
             "uids": uids}
        if p["number"] >= 0:
            e["number"] = p["number"]
        info = {k: v for k, v in p["infos"].items() if v not in ("", None)}
        if info:
            e["info"] = info
        if p["refs"]:
            e["sub_presets"] = len(p["refs"])
        out.append(e)
    return out


def presets(path, dev_path=None, pre_path=None):
    """Presets of a device or of a .pre file: [{index, name, source, category, values: {param name: raw pad
    value}, ...}].  With a .dev: the default preset file (find_preset_file, or `pre_path`) first, then the
    presets stored in the device.  Values are named by the device's ROCParameter names (raw 16-byte uid if no
    device is known)."""
    if path.lower().endswith(".pre"):
        names = device_parameters(dev_path) if dev_path else {}
        return _list_presets(read_pre_file(path), os.path.basename(path), names)
    dev_path = path
    names = device_parameters(dev_path)
    out = []
    pre = pre_path or find_preset_file(dev_path)
    if pre:
        out += _list_presets(read_pre_file(pre), os.path.basename(pre), names)
    for key, blk in embedded_presets(dev_path):
        out += _list_presets(blk, "device", names)
    for i, e in enumerate(out):
        e["index"] = i
    return out


def _inverse_chain(chain, v):
    for st in reversed(chain):
        op = st["op"]
        if op == "linear":
            (a0, a1), (b0, b1) = st["a"], st["b"]
            if b1 == b0:
                return None
            v = a0 + (a1 - a0) * (v - b0) / float(b1 - b0)
        elif op == "scale":
            if not st["k"]:
                return None
            v = v / st["k"]
        elif op == "add":
            v = v - st["k"]
        elif op == "one_minus":
            v = 1.0 - v
    return v


def _param_nets(dv, p):
    """net root -> host converter chain from the param's Val (the same walk as plan())."""
    al = dv.algos.get(p["key"])
    vv = al and al["vars"].get("Val")
    if vv is None or not vv.f.get("id"):
        return {}
    out = {}
    todo = [(dv.uf.find(vv.f["id"]), [])]
    while todo and len(out) < 60:
        r, chain = todo.pop()
        if r in out:
            continue
        out[r] = chain
        n = dv.nets.get(r)
        if n is None or n["dsp_out"]:
            continue
        for hk, hv, pid in n["host"]:
            h = dv.algos[hk]
            if hk == p["key"] or h["pep"] in KNOB_PEPS or h["pep"] in TEXT_PEPS or h["pep"] in GUI_PEPS:
                continue
            cs = converter_steps(h, hv)
            for outvar, steps in (cs or []):
                ov = h["vars"].get(outvar)
                if ov is not None and ov.f.get("id") and not any(s is None for s in steps):
                    todo.append((dv.uf.find(ov.f["id"]), chain + steps))
    return out


def _class_in_pad(dv, key, i, dsp_dir):
    try:
        c = module_class(dsp_dir, resolve_atom(dv.atoms[key]["module"], dsp_dir))
        cin = [p for p in c.pads if p.kind == "in"]
        return cin[i] if i < len(cin) else None
    except Exception:           # no DSP file: encoding from the pad type only
        return None


_PCTX = {}


def preset_values(dev_path, preset, pre_path=None, rate=48000, dsp_dir=DEFAULT_DSP_DIR, the_plan=None):
    """Values of preset `preset` (index into presets(dev_path, pre_path=...) or its name) for the device's
    plan: {'name', 'params': {plan param name: display value}, 'vals': {plan param name: knob Val},
    'raw': [{name, key, in, pad, value, word}] for DSP pads no plan param drives, 'host': [names of host-only
    values (GUI state, unemulated scripts)], 'check': [...problems...]}.
    Apply with pulsard set_param (params) and set (raw words)."""
    ck = (os.path.abspath(dev_path), pre_path, dsp_dir)
    ctx = _PCTX.get(ck)
    if ctx is None:
        p = the_plan or plan(dev_path, dsp_dir)
        dv = Device(dev_path, dsp_dir)
        net_param = {}                      # net root -> [(plan param, host converter chain from its Val)]
        for q in p["params"]:
            for r, chain in _param_nets(dv, q).items():
                net_param.setdefault(r, []).append((q, chain))
        rp = device_parameters(dev_path)
        ctx = (presets(dev_path, pre_path=pre_path), rp, dv, net_param)
        _PCTX.clear()
        _PCTX[ck] = ctx
    lst, rp, dv, net_param = ctx
    if isinstance(preset, int) or (isinstance(preset, str) and preset.isdigit()):
        sel = lst[int(preset)]
    else:
        m = [e for e in lst if e["name"] == preset] or [e for e in lst if e["name"].lower() == str(preset).lower()]
        if not m:
            raise KeyError("no preset %r" % preset)
        sel = m[0]
    res = {"name": sel["name"], "index": sel["index"], "source": sel["source"], "params": {}, "vals": {},
           "raw": [], "host": [], "check": []}
    for uid, value in sel["uids"].items():
        pname = rp[uid]["name"] if uid in rp else uid
        tgt = rp[uid]["target"] if uid in rp else None
        if not tgt or len(tgt) != 1 or not isinstance(value, (int, float)):
            res["host"].append(pname)
            continue
        pid = tgt[0]
        hits = []
        own = dv.pad_owner.get(pid)
        for q, chain in net_param.get(dv.uf.find(pid), []):
            val = _inverse_chain(chain, value)
            if val is None:
                continue
            val = int(round(val))
            # the parameter's own knob first, then a knob whose Val range holds the value (several buttons
            # can share one Switch net)
            hits.append((own is not None and own[1] == q["key"], q["val_min"] <= val <= q["val_max"], -len(chain),
                         len(hits), q, val))
        if hits:
            *_, q, val = max(hits)
            if not q["val_min"] <= val <= q["val_max"]:
                res["check"].append("%s -> %s: Val %d outside %d..%d" % (pname, q["name"], val, q["val_min"],
                                                                      q["val_max"]))
                val = min(max(val, q["val_min"]), q["val_max"])
            res["vals"][q["name"]] = val
            try:
                res["params"][q["name"]] = val_to_display(q, val)
            except (ValueError, ZeroDivisionError):
                res["params"][q["name"]] = float(val)
            continue
        n = dv.net_of(pid)
        if n and n["dsp_in"] and not n["dsp_out"]:
            for key, i, padid in n["dsp_in"]:
                v = dv.pads[padid]
                unit = _unit_of(v, None)
                enc = _encoding(_class_in_pad(dv, key, i, dsp_dir), v, unit)
                res["raw"].append({"name": pname, "key": key, "in": i, "pad": _vname(v), "value": value,
                                   "unit": unit, "word": pad_encode(value, enc, unit, rate)})
        else:
            res["host"].append(pname)
    return res


def _presets_cli(a):
    """scope_device.py presets FILE [--pre FILE.pre] [--dev FILE.dev] [--json] [N|NAME]"""
    if a.file.lower().endswith(".pre"):
        lst = presets(a.file, dev_path=a.dev)
        if a.preset is not None:
            lst = [e for e in lst if str(e["index"]) == a.preset or e["name"] == a.preset]
        for e in lst:
            e.pop("uids", None)
        if a.json:
            json.dump(lst, sys.stdout, indent=1, default=_json_default)
            print()
        else:
            for e in lst:
                print("%4d  %-32s %-28s %s" % (e["index"], e["name"], e["category"], ", ".join(
                    "%s=%s" % kv for kv in e["values"].items())))
        return 0
    if a.preset is None:
        lst = presets(a.file, pre_path=a.pre)
        for e in lst:
            e.pop("uids", None)
        if a.json:
            json.dump(lst, sys.stdout, indent=1, default=_json_default)
            print()
        else:
            if not lst:
                print("no presets (looked for %s.pre in %s and the device's PLData)" % (
                    preset_caption(a.file), ", ".join(PRESET_DIRS + ["Presets/ folders above the .dev"])))
            for e in lst:
                print("%4d  %-32s %-12s %s" % (e["index"], e["name"], e["source"], e["category"]))
        return 0
    r = preset_values(a.file, int(a.preset) if a.preset.isdigit() else a.preset, a.pre, a.rate, a.dsp)
    if a.json:
        json.dump(r, sys.stdout, indent=1, default=_json_default)
        print()
        return 0
    p = plan(a.file, a.dsp)
    fmt = {q["name"]: q.get("display_format") for q in p["params"]}
    print("preset %d %r (%s)" % (r["index"], r["name"], r["source"]))
    for k, v in r["params"].items():
        print("  PARAM %-40s %s" % (k, c_format(fmt.get(k), v) if fmt.get(k) else "%g" % v))
    for x in r["raw"]:
        print("  RAW   %-40s %s in%d (%s) = 0x%08x" % (x["name"], x["key"], x["in"], x["pad"], x["word"]))
    if r["host"]:
        print("  HOST  " + ", ".join(r["host"]))
    for c in r["check"]:
        print("  CHECK " + c)
    return 0


if __name__ == "__main__":
    sys.exit(main())
