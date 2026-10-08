#!/usr/bin/env python3
"""
pulsard - Pulsar II DSP daemon.

Boots the card like `pulsar_loader.py boot --pcm` (default graph: PC playback/capture, analog I/O, direct
monitor, ALSA mixer controls) and then stays alive, owning the board, so that DSP modules can be loaded,
wired and unloaded at runtime by the GUI (Pulsar Scope) or `pulsarctl`.

Protocol: Unix stream socket (default /run/pulsard.sock, group "audio", mode 0660), one JSON object per
line in each direction. Requests: {"cmd": NAME, ...}; replies: {"ok": true, ...} or {"ok": false, "error": "..."}.

  status                                  board, DSP load and the whole graph (nodes + connections)
  catalog [refresh]                       loadable modules (file, names, pads, cycles, fixed DSP)
  load file [dsp] [name]                  load a module, returns its node id
  unload id                               disconnect everything touching the node, then unload it
  connect src out dst in                  wire output `out` of src to input `in` of dst
  disconnect dst in                       input back to silence
  set id in value                         feed an input with a constant (raw 32-bit, e.g. 1.31 gain)
  save_project                            the current rack as a project (modules, changed wires, values, gui)
  load_project project                    reset, then rebuild the rack from a project; returns id map + errors
  reset                                   back to the base configuration
  set_gui gui                             store GUI data (layout) with the rack
  devices [refresh]                       SCOPE devices (.dev) that can be built (no MIDI / licensed modules)
  load_device file [dsp] [name] [params]  build a device: one node with the device's ports and parameters
  set_param id name value                 set a device parameter in display units (Hz, dB, %...)

The rack is autosaved to /var/lib/snd-pulsar/current-project.json after every change and restored at start.

Outputs are numbered async outputs first, then sync outputs (as the DSP module itself numbers them).
Node "pc_play" is the audio coming from the PC (ALSA playback, 2 sync outputs); "pc_rec" is the audio going
to the PC (ALSA capture, fixed to the analog inputs for now).
"""

import argparse
import grp
import json
import math
import os
import re
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import pulsar_loader as pl       # noqa: E402
import pulsar_modules as pm      # noqa: E402
import pulsar_delay as pdl       # noqa: E402  (host delay atoms of SCOPE devices, docs/pc_delay.md)

CORE_CLOCK = 60000000            # pluto coreClock (docs/clock_rate.md)
CYCLE_RESERVE = 0.25             # keep 25 % of every sample period for the OS


class GraphError(Exception):
    pass


class _NeedRebuild(Exception):
    """A dynamic device grew past its DSP: rebuild it instead of adding the new modules elsewhere."""


POLY_FLAG = 0x00400000            # module flag: voice count set with SetVoices (32ADD, 16MIX, MVC ...)


def cycles(cls):
    """Sync cycles per sample of a module class (the descriptor word carries flags in its high bits)."""
    return cls.syncCycles & 0xFFFF


def mod_cycles(mod):
    """Sync cycles of a loaded module: polyphonic modules add (cycles >> 16) per voice (scope_device.voice_cycles)."""
    c = mod.cls.syncCycles
    if mod.cls.flags & POLY_FLAG:
        return (c & 0xFFFF) + (c >> 16) * max(1, getattr(mod, "voices", None) or 1)
    return c & 0xFFFF


def _s32(v):
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v & 0x80000000 else v


def _survey_one(path, dsp_dir, devices_dir):
    try:
        import scope_device as sd
        p = sd.plan(path, dsp_dir)
        full = None
        if p.get("dynamic_ports"):           # dynamic mixer: pulsard loads it with nothing connected
            full = sd.plan(path, dsp_dir, {"@connected": p["dynamic_ports"]})
            p = sd.plan(path, dsp_dir, {"@connected": []})
    except Exception:
        return None
    if not p.get("complete") or p.get("needs_midi"):
        return None
    import pulsar_license as plic
    lic = set()
    for m in p["modules"] + (full["modules"] if full else []):
        if m.get("kind") == "pc_delay" or not m.get("dsp_file"):
            continue
        try:
            cls = pm.ModuleClass(os.path.join(dsp_dir, m["dsp_file"]))
        except Exception:
            continue
        if plic.needs_unlock(cls):
            lic.add(tuple(plic.module_seg_id(cls)))
    rel = os.path.relpath(path, devices_dir)
    row = {"file": rel, "name": p["name"], "category": os.path.dirname(rel), "cycles": p["dsp_cycles"],
           "modules": len(p["modules"]),
           "inputs": [q["name"] for q in p["ports"] if q["dir"] == "in"],
           "outputs": [q["name"] for q in p["ports"] if q["dir"] == "out"],
           "params": [q["name"] for q in p["params"] if not q.get("hidden") and (q.get("targets") or q.get("inactive"))],
           "pc_delay": bool(p.get("pc_delays")), "licensed": [list(x) for x in sorted(lic)]}
    if p.get("midi_optional"):
        row["midi_optional"] = True
    if full is not None:
        row.update(dynamic=True, cycles_max=full["dsp_cycles"], modules_max=len(full["modules"]),
                   complete_max=bool(full.get("complete")))
    return row


class Node:
    def __init__(self, nid, kind, title, mod=None, dsp=None, fixed=False):
        self.id, self.kind, self.title, self.mod, self.dsp, self.fixed = nid, kind, title, mod, dsp, fixed
        self.parent = None          # device node id for the DSP modules inside a device
        self.dev = None             # device data (kind == "device")
        self.pcd = None             # pulsar_delay.PcDelay (kind == "pc_delay", inside a device)

    def _ports(self, direction):
        names = self.dev["in_names" if direction == "in" else "out_names"]
        ports = {q["name"]: q for q in self.dev["plan"]["ports"] if q["dir"] == direction}
        return [{"index": k, "name": n, "sync": ports[n]["sync"], "midi": ports[n].get("midi", False)}
                for k, n in enumerate(names)]

    def inputs(self):
        if self.kind == "device":
            return self._ports("in")
        if self.kind == "pc_delay":
            return self.pcd.inputs()
        if self.kind == "pc_play":
            return []
        if self.kind == "pc_rec":
            return [{"index": i, "name": n, "sync": True} for i, n in enumerate(("L", "R"))]
        return [{"index": p.num, "name": p.short, "long": p.long, "sync": p.sync, "type": p.type,
                 "min": _s32(p.min), "max": _s32(p.max)} for p in self.mod.cls.pads if p.kind == "in"]

    def outputs(self):
        if self.kind == "device":
            return self._ports("out")
        if self.kind == "pc_delay":
            return self.pcd.outputs()
        if self.kind == "pc_play":
            return [{"index": i, "name": n, "sync": True} for i, n in enumerate(("L", "R"))]
        if self.kind == "pc_rec":
            return []
        outs = [p for p in self.mod.cls.pads if p.kind == "async_out"] + \
               [p for p in self.mod.cls.pads if p.kind == "sync_out"]
        return [{"index": k, "name": p.short, "long": p.long, "sync": p.sync} for k, p in enumerate(outs)]

    def describe(self):
        d = {"id": self.id, "kind": self.kind, "title": self.title, "dsp": self.dsp, "fixed": self.fixed,
             "inputs": self.inputs(), "outputs": self.outputs()}
        if self.mod is not None:
            d.update(file=self.mod.cls.name, name=self.mod.cls.short, long=self.mod.cls.long,
                     cycles=mod_cycles(self.mod))
        if self.dev is not None:
            pl_ = self.dev["plan"]
            if pl_.get("dynamic_ports"):
                d.update(dynamic_ports=pl_["dynamic_ports"], dsps=self.dev.get("dsps"),
                         connected=self.dev["switch_values"].get("@connected", []))
            d.update(file=self.dev["file"], cycles=pl_["dsp_cycles"], modules=len(self.dev["inner"]),
                     params=[dict(name=q["name"], unit=q.get("unit") or "", min=q.get("min"), max=q.get("max"),
                                  default=q.get("default"), curve=q.get("curve"), discrete=bool(q.get("discrete")),
                                  format=q.get("display_format") or q.get("format"),
                                  value=self.dev["params"].get(q["name"]),
                                  **{k: q[k] for k in ("strip", "channel", "base_name", "group", "index",
                                                        "structure", "inactive") if k in q},
                                  spec={k: q.get(k) for k in ("knob", "display", "val_min", "val_max",
                                                               "val_default", "control")})
                             for q in self.dev["params_list"]])
        return d


class Graph:
    """The DSP graph of one board: wraps pulsar_modules.Rack and remembers nodes, wires and values."""

    def __init__(self, board, dsp_dir, rate, handles):
        self.b, self.dsp_dir, self.rate = board, dsp_dir, rate
        self.rack = handles["rack"]
        self.nodes, self.wires, self.values = {}, {}, {}     # wires: (dst, in) -> (src, out)
        self.lock = threading.Lock()
        self._next = 1
        self._catalog = None
        self._devices = None
        self.devices_dir = None
        self.midi_dsp = None
        self.lic = None            # pulsar_license.License (user's own key file), None = no licence
        self.unlock = None         # PlutoDsp.load unlock hook (uC computes the magic word, docs/presets_license.md)
        self.add_node("pc_play", "PC Playback", dsp=None, fixed=True, nid="pc_play")
        self.add_node("pc_rec", "PC Record", dsp=None, fixed=True, nid="pc_rec")
        h = handles
        ain = self.add_node("module", "Analog Init", h["ainit"], fixed=True)
        ano = self.add_node("module", "Analog Out 1/2", h["ano"], fixed=True)
        ani = self.add_node("module", "Analog In 1/2", h["ani"], fixed=True)
        del ain
        for ch in range(2):
            pc = self.add_node("module", "PC Volume %s" % "LR"[ch], h["pc_vols"][ch], fixed=True)
            self.wires[(pc, 0)] = ("pc_play", ch)
            if h["mon_vols"]:
                mon = self.add_node("module", "Monitor Volume %s" % "LR"[ch], h["mon_vols"][ch], fixed=True)
                add = self.add_node("module", "Mix %s" % "LR"[ch], h["adds"][ch], fixed=True)
                self.wires[(mon, 0)] = (ani, ch)
                self.wires[(add, 0)] = (pc, 0)
                self.wires[(add, 1)] = (mon, 0)
                self.wires[(ano, ch)] = (add, 0)
            else:
                self.wires[(ano, ch)] = (pc, 0)
            self.wires[("pc_rec", ch)] = (ani, ch)
        self.default_wires = dict(self.wires)
        self.gui = {}                                        # opaque GUI data (layout), kept in projects
        self.state_file = None                               # autosave target (set by main)
        self.host_words = pdl.HostWords()                    # BAR dwords for DSP async -> host delay times
        self._pc_delay_ok = None
        self._bulk = False                                   # project import: re-plan dynamic devices once at the end
        self._slot_pool = {d.dspno: [] for d in self.rack.dsp}
        for d in self.rack.dsp:
            d.alloc_sync_output = self._reuse_sync_slot(d, d.alloc_sync_output)

    # ---- cross-DSP sync slots: pulsar_modules never gives a slot back (tcb_count only grows, 11 per DSP);
    # pulsard keeps the slots of unloaded modules and hands them out again (their readers were re-linked)
    def _reuse_sync_slot(self, d, orig):
        pool = self._slot_pool[d.dspno]

        def alloc(mod, j):
            if mod.sync_slots[j] >= 0 or not pool:
                return orig(mod, j)
            slot = pool.pop(0)
            mod.sync_slots[j] = slot
            ops = []
            if mod.loaded and mod.syncout_sites.get(j):
                op = d._patch_op(mod.syncout_sites[j], slot, "%s: sync out %d -> reused slot 0x%x" % (mod.name, j, slot))
                if op is not None:
                    ops.append(op)
            return slot, ops
        return alloc

    def _unload_mod(self, mod):
        ops = self.rack.unload(mod)
        self._slot_pool[mod.dsp] += [x for x in mod.sync_slots if x >= 0]
        mod.sync_slots = [-1] * len(mod.sync_slots)
        return ops

    # ---- bookkeeping
    def add_node(self, kind, title, mod=None, dsp=None, fixed=False, nid=None):
        if nid is None:
            nid = "n%d" % self._next
            self._next += 1
        self.nodes[nid] = Node(nid, kind, title, mod, mod.dsp if mod is not None else dsp, fixed)
        return nid

    def node(self, nid):
        if nid not in self.nodes:
            raise GraphError("no node %r" % nid)
        return self.nodes[nid]

    def dsp_load(self):
        budget = int(CORE_CLOCK / self.rate * (1 - CYCLE_RESERVE))
        out = []
        for d in self.rack.dsp:
            cyc = sum(mod_cycles(m) for m in d.modules)
            out.append({"dsp": d.dspno, "modules": len(d.modules), "cycles": cyc, "budget": budget,
                        "pm_free": sum(n for _, n in d.pm.free_ranges()),
                        "dm_free": sum(n for _, n in d.dm.free_ranges())})
        return out

    def execute(self, ops):
        pm.execute(self.b, ops)

    # ---- commands
    def _visible(self, nid):
        n = self.nodes.get(nid)
        return n is not None and n.parent is None

    def status(self):
        return {"rate": self.rate, "dsps": self.dsp_load(),
                "nodes": [n.describe() for n in self.nodes.values() if n.parent is None],
                "wires": [{"src": s_, "out": o, "dst": d, "in": i} for (d, i), (s_, o) in self.wires.items()
                          if self._visible(d) and self._visible(s_)],
                "values": [{"id": n, "in": i, "value": v} for (n, i), v in self.values.items() if self._visible(n)],
                "gui": self.gui}

    def catalog(self, refresh=False):
        if self._catalog is not None and not refresh:
            return self._catalog
        cache = "/var/cache/pulsard/catalog-v2.json"
        if not refresh and os.path.exists(cache):
            try:
                with open(cache) as f:
                    self._catalog = json.load(f)
                return self._catalog
            except (OSError, ValueError):
                pass
        cat = []
        for fn in sorted(os.listdir(self.dsp_dir)):
            if not fn.lower().endswith(".dsp"):
                continue
            try:
                c = pm.ModuleClass(os.path.join(self.dsp_dir, fn))
            except Exception:                       # libraries, other boards, unsupported heaps
                continue
            fixed = (c.flags >> 17) & 0xF
            cat.append({"file": fn, "name": c.short, "long": c.long, "cycles": cycles(c),
                        "fixed_dsp": fixed - 1 if fixed else None,
                        "inputs": [{"name": p.short, "long": p.long, "sync": p.sync, "type": p.type,
                                    "min": _s32(p.min), "max": _s32(p.max)} for p in c.pads if p.kind == "in"],
                        "outputs": [{"name": p.short, "long": p.long, "sync": p.sync}
                                    for p in c.pads if p.kind != "in"]})
        self._catalog = cat
        try:
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            with open(cache, "w") as f:
                json.dump(cat, f)
        except OSError:
            pass
        return cat

    def _dsp_cycles(self, d):
        return sum(mod_cycles(m) for m in self.rack.dsp[d].modules)

    def _pick_dsp(self, cls):
        fixed = (cls.flags >> 17) & 0xF
        if fixed:
            return fixed - 1
        budget = CORE_CLOCK / self.rate * (1 - CYCLE_RESERVE)
        best = None
        for d in (2, 3, 4, 5, 1, 0):                # keep DSP0/1 (analog I/O) for last
            cyc = self._dsp_cycles(d)
            if cyc + cycles(cls) <= budget and (best is None or cyc < best[1]):
                best = (d, cyc)
        if best is None:
            raise GraphError("no DSP has %d free cycles for %s" % (cycles(cls), cls.name))
        return best[0]

    def load(self, file, dsp=None, name=None):
        path = os.path.join(self.dsp_dir, os.path.basename(file))
        cls = pm.ModuleClass(path)
        if dsp is None:
            dsp = self._pick_dsp(cls)
        try:
            mod, ops = self.rack.load(path, int(dsp), unlock=self.unlock)
        except Exception as e:
            if type(e).__name__ == "LicenseError":
                raise GraphError("no licence for %s: %s" % (os.path.basename(path), e))
            raise
        self.execute(ops)
        return self.add_node("module", name or cls.long or cls.short, mod)

    # ---- wiring through endpoints: a device port maps to inner module pads (or passes another port through)
    def _src_ep(self, src, out, depth=0):
        """Real source (node id, output) of output `out` of node `src`, or None (silence)."""
        n = self.node(src)
        if n.kind in ("pc_play", "module", "pc_delay"):
            if out >= len(n.outputs()):
                raise GraphError("%s has no output %d" % (src, out))
            return (src, out)
        if n.kind != "device":
            raise GraphError("%s has no outputs" % src)
        names = n.dev["out_names"]
        if out >= len(names):
            raise GraphError("%s has no output %d" % (src, out))
        port = next(q for q in n.dev["plan"]["ports"] if q["dir"] == "out" and q["name"] == names[out])
        if port.get("target"):
            key, k = port["target"]
            return (n.dev["inner"][key], k)
        pf = port.get("passthrough_from")
        if isinstance(pf, (list, tuple)):                    # scope_device gives a list of input port names
            pf = next((x for x in pf if x in n.dev["in_names"]), None)
        if pf and depth < 8 and pf in n.dev["in_names"]:
            w = self.wires.get((src, n.dev["in_names"].index(pf)))
            return self._src_ep(w[0], w[1], depth + 1) if w else None
        return None

    def _dst_eps(self, dst, inp):
        n = self.node(dst)
        if n.kind in ("module", "pc_delay"):
            if inp >= len(n.inputs()):
                raise GraphError("%s has no input %d" % (dst, inp))
            return [(dst, inp)]
        if n.kind != "device":
            raise GraphError("%s has no wirable inputs" % dst)
        names = n.dev["in_names"]
        if inp >= len(names):
            raise GraphError("%s has no input %d" % (dst, inp))
        port = next(q for q in n.dev["plan"]["ports"] if q["dir"] == "in" and q["name"] == names[inp])
        return [(n.dev["inner"][k], i) for k, i in port.get("targets") or []]

    def _link(self, ep, dnid, i):
        """Wire source endpoint ep (or silence when None) to input i of module dnid."""
        dn = self.nodes[dnid]
        if dn.kind == "pc_delay":
            self._link_pc_delay(ep, dnid, i)
            return
        if ep is None:
            ops = self.rack.disconnect(dn.mod, i)
        elif self.nodes[ep[0]].kind == "pc_delay":          # tap k -> PC window slot (broadcast)
            ops = self.rack.dsp[dn.dsp].link_input(dn.mod, i, self.nodes[ep[0]].pcd.tap_addr(ep[1]))
        elif ep[0] == "pc_play":
            if ep[1] not in (0, 1):
                raise GraphError("pc_play has outputs 0 and 1")
            ops = self.rack.dsp[dn.dsp].link_input(dn.mod, i, 0xC000 + 2 * pl.PLAY_SLOTS[ep[1]])  # broadcast
        else:
            ops = self.rack.connect(self.nodes[ep[0]].mod, ep[1], dn.mod, i)
        self.execute(ops)
        self.values.pop((dnid, i), None)

    def _refresh_from(self, dev):
        """Re-link everything fed by the outputs of device `dev` (after its inputs or switches changed)."""
        for (d, i), (s_, o) in list(self.wires.items()):
            if s_ == dev:
                ep = self._src_ep(s_, o)
                for dn, ii in self._dst_eps(d, i):
                    self._link(ep, dn, ii)

    def connect(self, src, out, dst, inp):
        if self.node(dst).kind not in ("module", "device"):
            raise GraphError("%s has no wirable inputs" % dst)
        targets = self._dst_eps(dst, inp)
        ep = self._src_ep(src, out)
        prev = self.wires.get((dst, inp))
        self.wires[(dst, inp)] = (src, out)
        if self._is_dynamic(src) or self._is_dynamic(dst):
            try:                                 # a mixer channel appears when its pad gets connected
                if prev is not None and prev[0] != src:
                    self._restructure(prev[0])
                self._restructure(src)
                self._restructure(dst)
            except Exception:
                if prev is None:
                    self.wires.pop((dst, inp), None)
                else:
                    self.wires[(dst, inp)] = prev
                raise
            targets = self._dst_eps(dst, inp)
            ep = self._src_ep(src, out)
        for dn, i in targets:
            self._link(ep, dn, i)
        self.values.pop((dst, inp), None)
        if self.nodes[dst].kind == "device":
            self._refresh_from(dst)

    def _is_dynamic(self, nid):
        n = self.nodes.get(nid)
        return n is not None and n.kind == "device" and bool(n.dev["plan"].get("dynamic_ports"))

    def disconnect(self, dst, inp):
        dn = self.node(dst)
        if dn.kind not in ("module", "device"):
            raise GraphError("%s inputs cannot be changed" % dst)
        for t, i in self._dst_eps(dst, inp):
            self._link(None, t, i)
        w = self.wires.pop((dst, inp), None)
        self.values.pop((dst, inp), None)
        if w is not None and self._is_dynamic(w[0]):
            self._restructure(w[0])
        if self._is_dynamic(dst):
            self._restructure(dst)
        if dn.kind == "device":
            self._refresh_from(dst)

    def set_value(self, nid, inp, value):
        n = self.node(nid)
        if n.kind == "pc_delay":
            try:
                n.pcd.set_pad(self.b.bar, inp, int(value))
            except pdl.DelayError as e:
                raise GraphError(str(e))
            self.wires.pop((nid, inp), None)
            self.values[(nid, inp)] = int(value) & 0xFFFFFFFF
            return
        if n.kind != "module":
            raise GraphError("%s has no settable inputs" % nid)
        if n.fixed:
            raise GraphError("%s is part of the base configuration (levels are in the ALSA mixer)" % nid)
        self.execute(self.rack.set_in_pad(n.mod, inp, int(value) & 0xFFFFFFFF))
        self.wires.pop((nid, inp), None)
        self.values[(nid, inp)] = int(value) & 0xFFFFFFFF

    def unload(self, nid, _inner=False):
        n = self.node(nid)
        if n.fixed:
            raise GraphError("%s is part of the base configuration" % nid)
        if n.parent is not None and not _inner:
            raise GraphError("%s belongs to device %s: remove the device" % (nid, n.parent))
        if n.kind == "device":
            n.dev["unloading"] = True
        for (d, i), (s_, o) in list(self.wires.items()):
            if s_ == nid and d != "pc_rec":
                self.disconnect(d, i)
        for (d, i) in list(self.wires):
            if d == nid:
                self.wires.pop((d, i))
        if n.kind == "device":
            self._teardown(n)
        elif n.kind == "pc_delay":
            self._free_pc_delay(n)
        else:
            self.execute(self._unload_mod(n.mod))
        for k in [k for k in self.values if k[0] == nid]:
            self.values.pop(k)
        del self.nodes[nid]

    # ---- SCOPE devices (.dev): several DSP modules + internal wires + parameters with units (scope_device.py)
    def _device_path(self, file):
        path = file if os.path.isabs(file) else os.path.join(self.devices_dir or "", file)
        if not os.path.isfile(path):
            raise GraphError("device file not found: %s" % file)
        return path

    # ---- MIDI from the PC (ALSA sequencer client "Pulsar2 MIDI" -> SNC2MIDI FIFO, docs/midi_synths.md)
    def start_midi(self, dsp=2):
        import pulsar_midi as pmid
        mod, ops, info = pmid.midi_source_ops(self.rack, dsp, self.dsp_dir)
        self.execute(ops)
        nid = self.add_node("module", "PC MIDI In", mod, fixed=True, nid="pc_midi")
        self.midi_fifo = pmid.ScopeMidiFifo(self.b, info, lock=self.lock)
        try:
            self.midi_bridge = pmid.MidiBridge(self.midi_fifo, "Pulsar2 MIDI")
        except Exception as e:                     # no ALSA sequencer (e.g. dry run in a container)
            print("pulsard: MIDI client unavailable: %s" % e, flush=True)
            return nid
        threading.Thread(target=self.midi_bridge.serve, daemon=True, name="midi").start()
        print("pulsard: ALSA MIDI client 'Pulsar2 MIDI' ready (PC MIDI In on DSP%d)" % dsp, flush=True)
        return nid

    # built-in instruments made of unprotected SCOPE modules (factory synths carry copy-protection atoms)
    BUILTINS = {"builtin:test_synth": "Pulsar Test Synth"}

    def builtin_entries(self):
        return [{"file": "builtin:test_synth", "name": "Pulsar Test Synth (4 voices)", "category": "Instruments",
                 "cycles": 0, "modules": 0, "inputs": ["MIDI In"], "outputs": ["Out"],
                 "params": ["Attack", "Decay", "Sustain", "Release", "Waveform", "Volume"]}]

    def _build_test_synth(self, dev_id, dsp, voices=4):
        import pulsar_midi as pmid
        import scope_device as sd
        inner, keys = {}, []

        def mod(file, key):
            nid = self.load(file, dsp, key)
            self.nodes[nid].parent = dev_id
            inner[key] = nid
            keys.append(key)
            return self.nodes[nid].mod

        mvc = mod(pmid.MVC_EASY16, "MVC")
        mix = mod("16MIX.dsp", "Mixer")
        lay_mvc, lay_mix = sd.voice_layout(mvc.cls), sd.voice_layout(mix.cls)
        voices = max(1, min(voices, lay_mvc["n"], lay_mix["n"]))
        ops = pmid.set_voices_ops(self.rack, mvc, voices) + pmid.set_voices_ops(self.rack, mix, voices)
        # Tune Tab (in 1) is left alone: its default input word points at Tunedef.ol's tunedeftab (absolute phase
        # increments, per sample rate); a table of zeros would give every note frequency 0
        ops += self.rack.set_in_pad(mvc, 3, 16)                                   # omni
        ops += self.rack.set_in_pad(mix, 0, 0x7FFFFFFF // voices)                 # master gain 1/voices
        self.execute(ops)
        out = mod("LINVOL.dsp", "Out")
        for v in range(voices):
            osc = mod("MMOSC6.dsp", "Osc%d" % v)
            eg = mod("ADSR-EG5.dsp", "Env%d" % v)
            vca = mod("LINVOL.dsp", "VCA%d" % v)
            ops = self.rack.connect(mvc, sd.voice_out_pad(lay_mvc, 8, v), osc, 0)      # frequency
            ops += self.rack.connect(mvc, sd.voice_out_pad(lay_mvc, 7, v), eg, 0)      # gate
            ops += self.rack.connect(eg, 0, mvc, sd.voice_in_pad(lay_mvc, 8, v))       # voice release sync
            ops += self.rack.connect(osc, 0, vca, 0)
            ops += self.rack.connect(eg, 1, vca, 1)
            ops += self.rack.connect(vca, 0, mix, sd.voice_in_pad(lay_mix, 1, v))
            ops += self.rack.set_in_pad(eg, 8, 5)                                   # slope
            self.execute(ops)
        self.execute(self.rack.connect(mix, 0, out, 0))
        envs = [k for k in keys if k.startswith("Env")]
        oscs = [k for k in keys if k.startswith("Osc")]
        ms = lambda i: {"kind": "ms", "keys": envs, "in": i}                     # noqa: E731
        params = [
            {"name": "Attack", "unit": "ms", "min": 1, "max": 5000, "default": 5, "curve": "log", "b": ms(4)},
            {"name": "Decay", "unit": "ms", "min": 1, "max": 5000, "default": 300, "curve": "log", "b": ms(5)},
            {"name": "Sustain", "unit": "%", "min": 0, "max": 100, "default": 70, "curve": "lin",
             "b": {"kind": "pct", "keys": envs, "in": 6}},
            {"name": "Release", "unit": "ms", "min": 1, "max": 10000, "default": 250, "curve": "log", "b": ms(7)},
            {"name": "Waveform", "unit": "", "min": 0, "max": 5, "default": 4, "curve": "lin", "discrete": True,
             "b": {"kind": "int", "keys": oscs, "in": 1}},
            {"name": "Volume", "unit": "dB", "min": -60, "max": 0, "default": -20, "curve": "db",
             "b": {"kind": "db", "keys": ["Out"], "in": 1}},
        ]
        plan = {"name": "Pulsar Test Synth", "order": keys, "modules": [], "wires": [], "consts": [],
                "dsp_cycles": sum(mod_cycles(self.nodes[n].mod) for n in inner.values()), "params": params,
                "ports": [{"name": "MIDI In", "dir": "in", "sync": False, "midi": True,
                           "target": ["MVC", 0], "targets": [["MVC", 0]]},
                          {"name": "Out", "dir": "out", "sync": True, "target": ["Out", 0], "targets": [["Out", 0]]}]}
        return plan, inner

    def _builtin_raw(self, q, value):
        b = q["b"]
        if b["kind"] == "ms":
            return [(k, b["in"], int(round(value * self.rate / 1000.0))) for k in b["keys"]]
        if b["kind"] == "pct":
            return [(k, b["in"], min(0x7FFFFFFF, int(value / 100.0 * 0x7FFFFFFF))) for k in b["keys"]]
        if b["kind"] == "db":
            return [(k, b["in"], 0 if value <= q["min"] else min(0x7FFFFFFF, int(10 ** (value / 20.0) * 0x7FFFFFFF)))
                    for k in b["keys"]]
        return [(k, b["in"], int(round(value)) & 0xFFFFFFFF) for k in b["keys"]]

    def _plan(self, path, switch_values=None):
        import scope_device as sd
        p = sd.plan(path, self.dsp_dir, switch_values or None)
        if not p.get("complete"):
            raise GraphError("%s cannot be built yet: %s" % (os.path.basename(path),
                                                            "; ".join(p.get("unsupported") or ["incomplete"])))
        if p.get("needs_midi"):
            raise GraphError("%s needs MIDI, not supported yet" % os.path.basename(path))
        missing = self.unlicensed(p)
        if missing:
            raise GraphError("%s needs a SCOPE licence you do not have: %s" % (os.path.basename(path), ", ".join(missing)))
        return p

    def unlicensed(self, p):
        """Names of the plan's licensed modules (magicProt + seg_id) not covered by the user's key file."""
        import pulsar_license as plic
        out = []
        for m in p["modules"]:
            if m.get("kind") == "pc_delay" or not m.get("dsp_file"):
                continue
            cls = pm.ModuleClass(os.path.join(self.dsp_dir, m["dsp_file"]))
            if plic.needs_unlock(cls):
                if self.lic is None or self.lic.find(plic.module_seg_id(cls)) is None:
                    out.append(m["dsp_file"])
        return sorted(set(out))

    def load_device(self, file, dsp=None, title=None, params=None, structure=None):
        """Build a SCOPE device as one node.  `structure` = plan inputs of dynamic devices (docs/device_format.md
        §10): {"@connected": [port names]} for the mixers whose channels exist only while their pads are
        connected; knob overrides are set through the structure parameters instead."""
        import scope_device as sd
        if file in self.BUILTINS:
            return self._load_builtin(file, dsp, title, params)
        path = self._device_path(file)
        p0 = self._plan(path)
        sv = {}
        if p0.get("dynamic_ports"):                 # pulsard state: nothing is connected to a new device
            sv["@connected"] = sorted(set((structure or {}).get("@connected") or []) & set(p0["dynamic_ports"]))
        # routing switch / structure controls given up front: one plan instead of a re-plan per control
        plist = {q["name"]: q for q in self._params_list(p0)}
        for name, v in (params or {}).items():
            if name in plist and "b" not in plist[name] and v is not None:
                for key, inp, raw in sd.targets_raw(plist[name], float(v), self.rate):
                    if inp == "switch":
                        sv[key] = raw
        p = self._plan(path, sv) if sv else p0
        dev_id = "n%d" % self._next
        self._next += 1
        rel = os.path.relpath(path, self.devices_dir) if self.devices_dir and path.startswith(self.devices_dir) else path
        node = Node(dev_id, "device", title or p["name"], dsp=dsp)
        node.dev = {"file": rel, "path": path, "plan": p, "switch_values": sv, "applied": dict(sv), "inner": {},
                    "in_names": [q["name"] for q in p["ports"] if q["dir"] == "in"],
                    "out_names": [q["name"] for q in p["ports"] if q["dir"] == "out"],
                    "params_list": self._params_list(p), "params": {}, "user_params": set(),
                    "host_funcs": self._host_funcs(p), "dsps": []}
        self.nodes[dev_id] = node
        try:
            self._build(node, dsp)
        except Exception:
            self._teardown(node)
            del self.nodes[dev_id]
            raise
        for q in node.dev["params_list"]:
            node.dev["params"][q["name"]] = q.get("default")
        for name, v in (params or {}).items():
            if name in node.dev["params"] and v is not None:
                self.set_param(dev_id, name, v)
        return dev_id

    def _build(self, n, dsp=None):
        """Load and wire all modules of the device's current plan (n.dev["inner"] must be empty)."""
        import scope_device as sd
        p, inner = n.dev["plan"], n.dev["inner"]
        place = self._place(p, [m for m in p["modules"] if m.get("kind") != "pc_delay"], dsp, p["name"])
        for key in p["order"]:
            m = next(x for x in p["modules"] if x["key"] == key)
            if m.get("kind") == "pc_delay":
                nid = self._new_pc_delay(m, key)
            else:
                nid = self.load(m["dsp_file"], place[key], key.split("/")[-1])
            self.nodes[nid].parent = n.id
            inner[key] = nid
        self._set_voices(p, inner)
        self._setup_pc_delays(p, inner)          # before the wires: sources move to comm slots
        for w in p["wires"]:
            if self.nodes[inner[w["dst_key"]]].kind == "pc_delay" and w["in"] == 0:
                continue                         # done by _setup_pc_delays
            self._link((inner[w["src_key"]], w["out"]), inner[w["dst_key"]], w["in"])
        for c in p["consts"]:
            self.set_value(inner[c["key"]], c["in"], sd.const_raw(c, self.rate))
        n.dev["dsps"] = sorted({self.nodes[x].dsp for x in inner.values() if self.nodes[x].dsp is not None})
        n.dsp = n.dev["dsps"][0] if n.dev["dsps"] else dsp
        self._push_host_funcs(n)
        for k, name in enumerate(n.dev["in_names"]):         # pads already wired (rebuild)
            w = self.wires.get((n.id, k))
            if w:
                port = next((q for q in p["ports"] if q["dir"] == "in" and q["name"] == name), None)
                ep = self._src_ep(*w)
                for key, i in (port or {}).get("targets") or []:
                    self._link(ep, inner[key], i)

    def _teardown(self, n):
        inner = n.dev["inner"]
        order = [k for k in reversed(n.dev["plan"]["order"]) if k in inner] + \
                [k for k in inner if k not in n.dev["plan"]["order"]]
        for k in order:
            nid = inner.pop(k)
            if nid in self.nodes:
                try:
                    self.unload(nid, _inner=True)
                except Exception:
                    pass

    def _rebuild(self, n):
        """Unload every module of the device and build it again from its switch values (keeps the node, its
        external wires and the parameter values)."""
        self._teardown(n)
        p = self._plan(n.dev["path"], n.dev["switch_values"])
        n.dev.update(plan=p, params_list=self._params_list(p), host_funcs=self._host_funcs(p))
        self._build(n)
        n.dev["applied"] = dict(n.dev["switch_values"])
        for name in sorted(n.dev.get("user_params") or ()):
            if name in n.dev["params"] and any(q["name"] == name for q in n.dev["params_list"]):
                self.set_param(n.id, name, n.dev["params"][name], _reapply=True)
        self._refresh_from(n.id)

    @staticmethod
    def _params_list(p):
        return [q for q in p["params"] if not q.get("hidden") and (q.get("targets") or q.get("inactive"))]

    @staticmethod
    def _host_funcs(p):
        return [dict(h, state=dict(h["state"])) for h in p.get("host_funcs") or []]

    def _set_voices(self, p, inner, only=None):
        """SetVoices of the single-instance polyphonic modules (32ADD bus adders: number of inputs summed)."""
        import pulsar_midi as pmid
        for sv in p.get("set_voices") or []:
            if sv["key"] in inner and (only is None or sv["key"] in only):
                self.execute(pmid.set_voices_ops(self.rack, self.nodes[inner[sv["key"]]].mod, sv["voices"]))

    def _push_host_funcs(self, n, only=None):
        """Write the outputs of the emulated host scripts (Pan, Ch1632X ...) from their current state."""
        import scope_device as sd
        for hf in n.dev.get("host_funcs") or []:
            if only is not None and hf["key"] not in only:
                continue
            for key, inp, raw in sd.host_func_outputs(hf, self.rate):
                if inp == "host":
                    rows = sd.host_func_update(n.dev["host_funcs"], key, raw[0], raw[1], self.rate)
                else:
                    rows = [(key, inp, raw)]
                for k2, i2, r2 in rows:
                    if i2 not in ("switch", "host") and k2 in n.dev["inner"]:
                        self.set_value(n.dev["inner"][k2], i2, r2)

    def _sync_slots_left(self, d):
        x = self.rack.dsp[d]
        lim, c, n = x.tcb_base_limit(), x.tcb_count, 0
        while x.tcb_base + 2 * (c + 1) + 1 < lim:
            c += 1
            n += 1
        return n + len(self._slot_pool.get(d, ()))

    def _place(self, p, mods, dsp=None, name="device", prefer=None, grow=False):
        """DSP of every plan module in `mods`: fixed modules keep theirs; the rest goes on one DSP when it fits
        (least loaded, DSP0/1 last), else the device tree is split over several DSPs (whole sub-trees first,
        so a mixer channel stays together).  Cross-DSP sync wires need a slot in the source DSP's sync block."""
        import scope_device as sd
        budget = CORE_CLOCK / self.rate * (1 - CYCLE_RESERVE)
        load = {d: self._dsp_cycles(d) for d in range(len(self.rack.dsp))}
        out = {}
        free = []
        for m in mods:
            if m.get("fixed_dsp") is not None:
                out[m["key"]] = m["fixed_dsp"]
                load[m["fixed_dsp"]] += m["cycles"] or 0
            else:
                free.append(m)
        order = (2, 3, 4, 5, 1, 0)
        need = sum(m["cycles"] or 0 for m in free)
        if dsp is not None:
            for m in free:
                out[m["key"]] = int(dsp)
            return out
        if not free:
            return out
        # many host delay lines = many capture slots: only DSP5's sync block can grow past 11 (pc_delay.md §6)
        if len(p.get("pc_delays") or []) > 8 and load[5] + need <= budget:
            return dict(out, **{m["key"]: 5 for m in free})
        fits = [d for d in order if load[d] + need <= budget]
        near = [d for d in (prefer or ()) if d in fits]        # a growing device stays where it is
        if grow and not near:
            raise _NeedRebuild()                 # place the whole device again (split by sub-trees)
        if near or fits:
            d = near[0] if near else min(fits, key=lambda x: (load[x], order.index(x)))
            return dict(out, **{m["key"]: d for m in free})
        # split: tree of module keys
        root = {"mods": [], "kids": {}, "cyc": 0}
        for m in free:
            node = root
            for comp in m["key"].split("/")[1:]:
                node = node["kids"].setdefault(comp, {"mods": [], "kids": {}, "cyc": 0})
            node["mods"].append(m)

        def total(nd):
            nd["cyc"] = sum(x["cycles"] or 0 for x in nd["mods"]) + sum(total(k) for k in nd["kids"].values())
            return nd["cyc"]

        def leaves(nd):
            return list(nd["mods"]) + [x for k in nd["kids"].values() for x in leaves(k)]

        used = [d for d in (prefer or ()) if d in load]

        def pick(c):
            cands = [d for d in used if load[d] + c <= budget] or \
                    sorted((d for d in order if d not in used and load[d] + c <= budget), key=lambda x: load[x])
            return cands[0] if cands else None

        def assign(nd):
            d = pick(nd["cyc"])
            if d is not None:
                for x in leaves(nd):
                    out[x["key"]] = d
                load[d] += nd["cyc"]
                if d not in used:
                    used.append(d)
                return
            if not nd["kids"]:
                if len(nd["mods"]) > 1:
                    for x in nd["mods"]:
                        assign({"mods": [x], "kids": {}, "cyc": x["cycles"] or 0})
                    return
                raise GraphError("no DSP has %d free cycles for %s (%s)" % (nd["cyc"], nd["mods"][0]["key"], name))
            for x in nd["mods"]:
                assign({"mods": [x], "kids": {}, "cyc": x["cycles"] or 0})
            for k in nd["kids"].values():
                assign(k)

        total(root)
        assign(root)
        # cross-DSP sync outputs per source DSP
        cls_of = {m["key"]: m for m in mods}
        need_slots = {}
        for w in p["wires"]:
            sk, dk = w["src_key"], w["dst_key"]
            if sk in out and dk in out and out[sk] != out[dk]:
                c = sd.module_class(self.dsp_dir, cls_of[sk]["dsp_file"])
                if not isinstance(c, Exception) and w["out"] >= c.numAsyncOut:
                    need_slots.setdefault(out[sk], set()).add((sk, w["out"]))
        for d, sl in need_slots.items():
            if len(sl) > self._sync_slots_left(d):
                raise GraphError("%s does not fit: %d cross-DSP sync wires from DSP%d, %d slots left"
                                 % (name, len(sl), d, self._sync_slots_left(d)))
        return out

    def _load_builtin(self, file, dsp, title, params):
        if dsp is None:
            dsp = self.midi_dsp if self.midi_dsp is not None else 2
        dev_id = "n%d" % self._next
        self._next += 1
        plan, inner = self._build_test_synth(dev_id, int(dsp))
        node = Node(dev_id, "device", title or self.BUILTINS[file], dsp=int(dsp))
        node.dev = {"file": file, "path": file, "plan": plan, "switch_values": {}, "inner": inner,
                    "in_names": ["MIDI In"], "out_names": ["Out"], "params_list": plan["params"], "params": {}}
        self.nodes[dev_id] = node
        for q in plan["params"]:
            self.set_param(dev_id, q["name"], (params or {}).get(q["name"], q["default"]))
        if "pc_midi" in self.nodes:                                   # play it right away from the PC
            self.connect("pc_midi", 0, dev_id, 0)
        return dev_id

    def set_param(self, dev_id, name, value, _reapply=False):
        import scope_device as sd
        n = self.node(dev_id)
        if n.kind != "device":
            raise GraphError("%s is not a device" % dev_id)
        q = next((x for x in n.dev["params_list"] if x["name"] == name), None)
        if q is None:
            raise GraphError("%s has no parameter %r" % (dev_id, name))
        value = float(value)
        switched = False
        rows = self._builtin_raw(q, value) if "b" in q else sd.targets_raw(q, value, self.rate)
        todo = list(rows)
        while todo:
            key, inp, raw = todo.pop(0)
            if inp == "switch":                  # routing switch position or structure knob (re-plan)
                if n.dev["switch_values"].get(key) != raw:
                    n.dev["switch_values"][key] = raw
                    switched = True
            elif inp == "host":                  # emulated host script input (Pan, Ch1632X ...)
                todo += sd.host_func_update(n.dev.get("host_funcs") or [], key, raw[0], raw[1], self.rate)
            elif key in n.dev["inner"]:
                self.set_value(n.dev["inner"][key], inp, raw)
        n.dev["params"][name] = value
        if not _reapply:
            n.dev.setdefault("user_params", set()).add(name)
        if switched:
            self._replan(n)

    def meter_map(self, n):
        """The device's SCOPE VU meters (vumulti*.dsp): [(inner key, meter j, strip, side)]. Meter j measures VU input
        3+2j; its level is async output 3j (1.31, decaying peak). A source that is the target of input port
        In<n>/IR<n> belongs to strip Ch<n> (L/R), a source under .../Master/... to the master strip."""
        p = n.dev["plan"]
        cache = n.dev.get("_meters")
        if cache is not None and cache[0] is p:
            return cache[1]
        vus = {m["key"] for m in p["modules"] if (m.get("dsp_file") or "").lower().startswith("vumulti")}
        by_src = {}
        for q in p["ports"]:
            m = re.match(r"(In|IR|Ax)(\d+)$", q["name"]) if q["dir"] == "in" else None
            for t in q.get("targets") or []:
                if m:
                    strip = ("Ax" if m.group(1) == "Ax" else "Ch") + m.group(2)
                    by_src[t[0]] = (strip, "R" if m.group(1) == "IR" else "L")
        out, master = [], 0
        for w in sorted(p["wires"], key=lambda w: (w["dst_key"], w["in"])):
            if w["dst_key"] not in vus or w["in"] < 3 or (w["in"] - 3) % 2 or w["dst_key"] not in n.dev["inner"]:
                continue
            j = (w["in"] - 3) // 2
            if w["src_key"] in by_src:
                strip, side = by_src[w["src_key"]]
            elif "/Master/" in w["src_key"] and "Folded" not in w["src_key"] and "Aux" not in w["src_key"]:
                strip, side = "Master", "LR"[master % 2]
                master += 1
            else:
                continue
            out.append((w["dst_key"], j, strip, side))
        n.dev["_meters"] = (p, out)
        return out

    def meters(self, dev_id):
        """Current levels of the device's VU meters in dBFS (None = silence)."""
        n = self.node(dev_id)
        if n.kind != "device":
            raise GraphError("%s is not a device" % dev_id)
        res = []
        for key, j, strip, side in self.meter_map(n):
            mod = self.nodes[n.dev["inner"][key]].mod
            v = self.b.get_value(mod.dsp, mod.seg_mod + mod.cls.off_async_out(3 * j))
            db = None if v <= 0 or v >= 0x80000000 else round(20 * math.log10(v / 2147483648.0), 1)
            res.append({"strip": strip, "side": side, "db": db})
        return res

    def peek(self, req):
        """Debug: read DSP words. {dsp, addr, count} or {id, [key], pad: in|ao|so, index, [count]}; for inputs also the
        word the input points at."""
        n = self.node(req["id"]) if "id" in req else None
        if n is not None and req.get("key"):
            n = self.node(n.dev["inner"][req["key"]])
        if n is not None:
            m = n.mod
            off = {"in": m.cls.off_input, "ao": m.cls.off_async_out, "so": m.cls.off_sync_out}[req.get("pad", "so")]
            dsp, addr = m.dsp, m.seg_mod + off(int(req.get("index", 0)))
        elif "sym" in req:
            dsp = int(req["dsp"])
            d = self.rack.dsp[dsp]
            hits = [(a, nm) for (sp, a, nm) in d.placed_syms if nm.split(".")[-1] == req["sym"] and sp != "PM"]
            if not hits and req["sym"] in d.syms:
                hits = [(d.syms[req["sym"]], req["sym"])]
            if not hits:
                raise GraphError("DSP%d: no DM symbol %s" % (dsp, req["sym"]))
            addr = hits[-1][0]
        else:
            dsp, addr = int(req["dsp"]), int(req["addr"])
        out = []
        for k in range(min(64, int(req.get("count", 1)))):
            v = self.b.get_value(dsp, addr + k)
            row = {"dsp": dsp, "addr": addr + k, "value": v}
            if n is not None and req.get("pad") == "in" and 0 < v < 0x10000:
                row["points_to"] = self.b.get_value(dsp, v)
            out.append(row)
        return out

    def presets(self, dev_id):
        import scope_device as sd
        n = self.node(dev_id)
        if n.kind != "device" or n.dev["file"] in self.BUILTINS:
            return []
        return [{"index": i, "name": q["name"], "source": q.get("source"), "category": q.get("category")}
                for i, q in enumerate(sd.presets(n.dev["path"]))]

    def load_preset(self, dev_id, preset):
        import scope_device as sd
        n = self.node(dev_id)
        if n.kind != "device" or n.dev["file"] in self.BUILTINS:
            raise GraphError("%s has no presets" % dev_id)
        r = sd.preset_values(n.dev["path"], preset, rate=self.rate, dsp_dir=self.dsp_dir, the_plan=n.dev["plan"])
        names = {q["name"] for q in n.dev["params_list"]}
        switch_first = [k for k in r["params"] if k in names and
                        any(t.get("kind") == "switch" for q in n.dev["params_list"] if q["name"] == k for t in q["targets"])]
        for k in switch_first + [k for k in r["params"] if k not in switch_first]:
            if k in names:
                self.set_param(dev_id, k, r["params"][k])
        for w in r.get("raw", []):
            if w.get("key") in n.dev["inner"]:
                self.set_value(n.dev["inner"][w["key"]], w["in"], w["word"])
        n.dev["preset"] = r.get("name")
        return {"name": r.get("name"), "warnings": r.get("check", [])}

    def _replan(self, n):
        """A routing switch, a structure control or a pad connection changed: re-plan the device and apply the
        difference (modules that got / lost their voices, SetVoices, internal wires, constants of new modules,
        port links, emulated host scripts), then re-send the parameters the user set.  If that fails (no DSP
        room), the change is undone by rebuilding the device from the previous switch values."""
        try:
            try:
                self._replan_diff(n)
            except _NeedRebuild:
                self._rebuild(n)
            n.dev["applied"] = dict(n.dev["switch_values"])
        except Exception as e:
            n.dev["switch_values"] = dict(n.dev.get("applied") or {})
            try:
                self._rebuild(n)
            except Exception as e2:
                raise GraphError("%s: %s; restoring the device failed too (%s), remove it" % (n.id, e, e2))
            raise GraphError("%s: change undone: %s" % (n.id, e))

    def _replan_diff(self, n):
        import scope_device as sd
        old = n.dev["plan"]
        new = self._plan(n.dev["path"], n.dev["switch_values"])
        inner = n.dev["inner"]
        new_mods = {m["key"]: m for m in new["modules"]}
        removed = [k for k in reversed(old["order"]) if k in inner and k not in new_mods]
        added = [k for k in new["order"] if k in new_mods and k not in inner]
        if any(new_mods[k].get("kind") == "pc_delay" for k in added) or \
                any(self.nodes[inner[k]].kind == "pc_delay" for k in removed):
            raise GraphError("%s: host delay lines cannot change at run time" % n.id)
        # modules that lost their voices: silence their readers (inside and outside the device), unload
        gone = set(removed)
        for (d, i), (s_, o) in list(self.wires.items()):
            if s_ == n.id:
                ep = self._src_ep(s_, o)
                if ep is not None and ep[0] in {inner[k] for k in gone}:
                    for dn, ii in self._dst_eps(d, i):
                        self._link(None, dn, ii)
        for w in old["wires"]:
            if w["src_key"] in gone and w["dst_key"] in inner and w["dst_key"] not in gone:
                self._link(None, inner[w["dst_key"]], w["in"])
        for k in removed:
            self.unload(inner.pop(k), _inner=True)
        # modules that got voices
        if added:
            place = self._place(new, [new_mods[k] for k in added], None, new["name"], prefer=n.dev.get("dsps"),
                                grow=True)
            for k in added:
                nid = self.load(new_mods[k]["dsp_file"], place[k], k.split("/")[-1])
                self.nodes[nid].parent = n.id
                inner[k] = nid
        ov = {x["key"]: x["voices"] for x in old.get("set_voices") or []}
        self._set_voices(new, inner, only={x["key"] for x in new.get("set_voices") or []
                                           if x["key"] in added or ov.get(x["key"]) != x["voices"]})
        disc, conn = sd.rewire(old, new)
        for dk, i in disc:
            if dk in inner and dk not in added:
                self._link(None, inner[dk], i)
        for w in conn:
            self._link((inner[w["src_key"]], w["out"]), inner[w["dst_key"]], w["in"])
        oc = {(c["key"], c["in"]): sd.const_raw(c, self.rate) for c in old["consts"]}
        for c in new["consts"]:
            raw = sd.const_raw(c, self.rate)
            if c["key"] in added or oc.get((c["key"], c["in"])) != raw:
                self.set_value(inner[c["key"]], c["in"], raw)
        old_t = {q["name"]: q.get("targets") or [] for q in old["ports"] if q["dir"] == "in"}
        n.dev["plan"] = new
        n.dev["params_list"] = self._params_list(new)
        n.dev["host_funcs"] = self._host_funcs(new)
        n.dev["dsps"] = sorted({self.nodes[x].dsp for x in inner.values() if self.nodes[x].dsp is not None})
        for k, name in enumerate(n.dev["in_names"]):
            new_t = next((q.get("targets") or [] for q in new["ports"] if q["dir"] == "in" and q["name"] == name), [])
            for key, i in old_t.get(name, []):
                if [key, i] not in [list(t) for t in new_t] and key in inner:
                    self._link(None, inner[key], i)
            w = self.wires.get((n.id, k))
            ep = self._src_ep(*w) if w else None
            for key, i in new_t:
                self._link(ep, inner[key], i)
        self._push_host_funcs(n)
        for name in sorted(n.dev.get("user_params") or ()):
            if name in n.dev["params"] and any(q["name"] == name for q in n.dev["params_list"]):
                self.set_param(n.id, name, n.dev["params"][name], _reapply=True)
        self._refresh_from(n.id)

    def _restructure(self, nid):
        """Dynamic devices (factory mixers): a channel exists while one of its pads is connected (RouteByContext
        'Connected' -> DynVoicesOfParent); re-plan when the set of connected dynamic pads changed."""
        n = self.nodes.get(nid)
        if n is None or n.kind != "device" or not n.dev["plan"].get("dynamic_ports") or n.dev.get("unloading") \
                or self._bulk:
            return
        dyn = set(n.dev["plan"]["dynamic_ports"])
        conn = {n.dev["in_names"][i] for (d, i) in self.wires if d == nid and i < len(n.dev["in_names"])}
        conn |= {n.dev["out_names"][o] for (_, _), (s_, o) in self.wires.items()
                 if s_ == nid and o < len(n.dev["out_names"])}
        conn = sorted(conn & dyn)
        if n.dev["switch_values"].get("@connected") != conn:
            n.dev["switch_values"]["@connected"] = conn
            self._replan(n)

    def devices(self, refresh=False):
        ok = [d for d in self._scope_devices(refresh)
              if all(self.lic is not None and self.lic.find(tuple(x)) is not None for x in d.get("licensed", []))]
        return self.builtin_entries() + ok

    def _scope_devices(self, refresh=False):
        """Usable SCOPE devices (complete plan, no MIDI), cached; devices() drops the ones the licence does not cover."""
        if self._devices is not None and not refresh:
            return self._devices_usable()
        cache = "/var/cache/pulsard/devices-v4.json"
        if not refresh and os.path.exists(cache):
            try:
                with open(cache) as f:
                    self._devices = json.load(f)
                return self._devices_usable()
            except (OSError, ValueError):
                pass
        files = []
        for root, _, names in os.walk(self.devices_dir or "/nonexistent"):
            files += [os.path.join(root, fn) for fn in names if fn.lower().endswith(".dev")]
        # "spawn", not fork: pulsard is multi-threaded (MIDI client, socket server) and forked workers deadlock
        import multiprocessing
        from concurrent.futures import ProcessPoolExecutor
        ctx = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(max(1, min(6, (os.cpu_count() or 2) - 1)), mp_context=ctx) as ex:
            rows = list(ex.map(_survey_one, sorted(files), [self.dsp_dir] * len(files),
                               [self.devices_dir] * len(files)))
        self._devices = [r for r in rows if r]
        try:
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            with open(cache, "w") as f:
                json.dump(self._devices, f)
        except OSError:
            pass
        return self._devices_usable()

    # ---- host delay lines ("PC Master 4k/32k Delay", "PC 256k Delay", "PC Early Reflection"), docs/pc_delay.md
    def _devices_usable(self):
        if self._pc_delay_ok is None:
            self._pc_delay_ok = pdl.supported(self.b.bar)
        return [r for r in self._devices if self._pc_delay_ok or not r.get("pc_delay")]

    def _new_pc_delay(self, m, key):
        nid = self.add_node("pc_delay", key.split("/")[-1], dsp=None)
        self.nodes[nid].pcd = pdl.PcDelay(m["pc_delay"], key.split("/")[-1])
        return nid

    def _setup_pc_delays(self, p, inner):
        """Move each delay input's source to a comm slot (capture side of the host ring), seed the delays from
        the saved constants and let the kernel allocate the rings and tap slots."""
        import scope_device as sd
        for info in p.get("pc_delays") or []:
            n = self.nodes[inner[info["key"]]]
            src_key, out = info["input"]
            smod = self.nodes[inner[src_key]].mod
            j = out - smod.cls.numAsyncOut
            if j < 0:
                raise GraphError("%s: delay input fed by an async output" % info["key"])
            addr, ops = self.rack.dsp[smod.dsp].alloc_sync_output(smod, j)
            self.execute(ops)
            for c in p["consts"]:
                if c["key"] == info["key"]:
                    n.pcd.set_pad(None, c["in"], sd.const_raw(c, self.rate))
            try:
                n.pcd.allocate(self.b.bar, addr, max(info["taps_used"] or [0]) + 1)
            except pdl.DelayError as e:
                raise GraphError("%s: %s" % (info["key"], e))
            n.pcd_input = (inner[src_key], out)

    def _link_pc_delay(self, ep, dnid, i):
        n = self.nodes[dnid]
        role = pdl.pad_role(n.pcd.spec, i)
        if role[0] == "in":
            if ep is not None and tuple(ep) != getattr(n, "pcd_input", None):
                raise GraphError("%s: the input of a PC delay cannot be rewired at runtime" % dnid)
            return
        old = n.pcd.sources.pop(i, None)
        if old is not None:                      # detach the previous DSP source
            smod, k, word = old
            self.execute(pdl.unexport_host(self.rack, smod, k, word))
            n.pcd.set_source(self.b.bar, i, None)
            self.host_words.release(word)
        if ep is None:
            return
        src = self.nodes[ep[0]]
        if src.kind != "module" or ep[1] >= src.mod.cls.numAsyncOut:
            raise GraphError("%s: pad %d takes a value or a DSP async output only" % (dnid, i))
        word = self.host_words.alloc()
        try:
            n.pcd.set_source(self.b.bar, i, word)
        except pdl.DelayError as e:
            self.host_words.release(word)
            raise GraphError(str(e))
        n.pcd.sources[i] = (src.mod, ep[1], word)
        self.execute(pdl.export_to_host(self.rack, src.mod, ep[1], word))

    def _free_pc_delay(self, n):
        for i in list(n.pcd.sources):
            smod, k, word = n.pcd.sources.pop(i)
            if smod.loaded:
                self.execute(pdl.unexport_host(self.rack, smod, k, word))
            self.host_words.release(word)
        try:
            n.pcd.free(self.b.bar)
        except pdl.DelayError as e:
            raise GraphError(str(e))


    # ---- projects
    def export_project(self):
        mods = [{"id": n.id, "file": n.mod.cls.name, "dsp": n.dsp, "title": n.title}
                for n in self.nodes.values() if n.kind == "module" and not n.fixed and n.parent is None]
        mods += [dict({"id": n.id, "device": n.dev["file"], "title": n.title, "params": n.dev["params"],
                       # dynamic / multi-DSP devices are placed again when they are rebuilt
                       "dsp": None if n.dev["plan"].get("dynamic_ports") or len(n.dev.get("dsps") or ()) > 1
                       else n.dsp},
                      **({"structure": {"@connected": n.dev["switch_values"]["@connected"]}}
                         if n.dev["switch_values"].get("@connected") else {}))
                 for n in self.nodes.values() if n.kind == "device"]
        wires = [{"src": s, "out": o, "dst": d, "in": i} for (d, i), (s, o) in self.wires.items()
                 if self.default_wires.get((d, i)) != (s, o)]
        removed = [{"dst": d, "in": i} for (d, i) in self.default_wires if (d, i) not in self.wires]
        values = [{"id": n, "in": i, "value": v} for (n, i), v in self.values.items() if self._visible(n)]
        wires = [w for w in wires if self._visible(w["src"]) and self._visible(w["dst"])]
        return {"format": "pulsar-project", "version": 1, "rate": self.rate, "next_id": self._next, "modules": mods,
                "wires": wires, "removed": removed, "values": values, "gui": self.gui}

    def reset(self):
        """Back to the base configuration: unload every added module, restore the default wiring."""
        for nid in [n.id for n in self.nodes.values() if not n.fixed and n.parent is None]:
            self.unload(nid)
        for (d, i), (src, out) in self.default_wires.items():
            if self.wires.get((d, i)) != (src, out) and d != "pc_rec":
                self.connect(src, out, d, i)
        self.gui = {}

    def import_project(self, prj):
        if prj.get("format") != "pulsar-project":
            raise GraphError("not a Pulsar project")
        self.reset()
        self._next = max(self._next, int(prj.get("next_id") or 0))   # never reuse ids across daemon restarts
        ids, errors = {}, []
        for m in prj.get("modules", []):
            try:
                if "device" in m:
                    ids[m["id"]] = self.load_device(m["device"], m.get("dsp"), m.get("title"), m.get("params"),
                                                    m.get("structure"))
                else:
                    ids[m["id"]] = self.load(m["file"], m.get("dsp"), m.get("title"))
            except Exception as e:                           # keep going: report what could not be restored
                errors.append("module %s (%s): %s" % (m.get("title"), m.get("file") or m.get("device"), e))
        mapped = lambda nid: ids.get(nid, nid)
        self._bulk = True
        try:
            self._import_wires(prj, mapped, errors)
        finally:
            self._bulk = False
        for nid in list(ids.values()):
            try:
                self._restructure(nid)
            except Exception as e:
                errors.append("device %s: %s" % (nid, e))
        for v in prj.get("values", []):
            try:
                self.set_value(mapped(v["id"]), int(v["in"]), int(v["value"]))
            except Exception as e:
                errors.append("value %s.%s: %s" % (v.get("id"), v.get("in"), e))
        gui = prj.get("gui") or {}
        self.gui = dict(gui, layout={mapped(k): v for k, v in (gui.get("layout") or {}).items()})
        return {"ids": ids, "errors": errors}

    def _import_wires(self, prj, mapped, errors):
        for r in prj.get("removed", []):
            try:
                self.disconnect(mapped(r["dst"]), int(r["in"]))
            except Exception as e:
                errors.append("disconnect %s.%s: %s" % (r.get("dst"), r.get("in"), e))
        for w in prj.get("wires", []):
            try:
                self.connect(mapped(w["src"]), int(w["out"]), mapped(w["dst"]), int(w["in"]))
            except Exception as e:
                errors.append("wire %s.%s -> %s.%s: %s" % (w.get("src"), w.get("out"), w.get("dst"), w.get("in"), e))

    def autosave(self):
        if not self.state_file:
            return
        tmp = self.state_file + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(self.export_project(), f)
            os.replace(tmp, self.state_file)
        except OSError:
            pass


MUTATING = {"load", "unload", "connect", "disconnect", "set", "reset", "load_project", "set_gui", "load_device",
            "set_param", "load_preset"}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        g = self.server.graph
        for line in self.rfile:
            try:
                req = json.loads(line)
                cmd = req.get("cmd")
                if cmd == "devices":                         # slow the first time: outside the board lock
                    res = {"devices": g.devices(bool(req.get("refresh"))), "ok": True}
                    self.wfile.write((json.dumps(res) + "\n").encode())
                    self.wfile.flush()
                    continue
                with g.lock:
                    if cmd == "status":
                        res = g.status()
                    elif cmd == "catalog":
                        res = {"modules": g.catalog(bool(req.get("refresh")))}
                    elif cmd == "load":
                        res = {"id": g.load(req["file"], req.get("dsp"), req.get("name"))}
                    elif cmd == "unload":
                        g.unload(req["id"])
                        res = {}
                    elif cmd == "connect":
                        g.connect(req["src"], int(req["out"]), req["dst"], int(req["in"]))
                        res = {}
                    elif cmd == "disconnect":
                        g.disconnect(req["dst"], int(req["in"]))
                        res = {}
                    elif cmd == "set":
                        g.set_value(req["id"], int(req["in"]), int(req["value"]))
                        res = {}
                    elif cmd == "load_device":
                        res = {"id": g.load_device(req["file"], req.get("dsp"), req.get("name"), req.get("params"),
                                                   req.get("structure"))}
                    elif cmd == "meters":
                        res = {"meters": g.meters(req["id"])}
                    elif cmd == "peek":
                        res = {"words": g.peek(req)}
                    elif cmd == "presets":
                        res = {"presets": g.presets(req["id"])}
                    elif cmd == "load_preset":
                        res = g.load_preset(req["id"], req["preset"])
                    elif cmd == "license":
                        res = {"entries": [] if g.lic is None else
                               [e.describe(g.lic.board_sno, g.lic.uc_info) for e in g.lic.entries]}
                    elif cmd == "set_param":
                        g.set_param(req["id"], req["name"], req["value"])
                        res = {}
                    elif cmd == "save_project":
                        res = {"project": g.export_project()}
                    elif cmd == "load_project":
                        res = g.import_project(req["project"])
                    elif cmd == "reset":
                        g.reset()
                        res = {}
                    elif cmd == "set_gui":
                        g.gui = req.get("gui") or {}
                        res = {}
                    else:
                        raise GraphError("unknown command %r" % cmd)
                    if cmd in MUTATING:
                        g.autosave()
                res["ok"] = True
            except (GraphError, pm.LinkError, pl.LoaderError, KeyError, ValueError, OSError) as e:
                res = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
            self.wfile.write((json.dumps(res) + "\n").encode())
            self.wfile.flush()


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def sd_notify(msg):
    path = os.environ.get("NOTIFY_SOCKET")
    if not path:
        return
    if path.startswith("@"):
        path = "\0" + path[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
        s.sendto(msg.encode(), path)


def main():
    ap = argparse.ArgumentParser(description="Pulsar II DSP daemon")
    ap.add_argument("--socket", default="/run/pulsard.sock")
    ap.add_argument("--group", default="audio", help="group allowed to use the socket")
    ap.add_argument("--resource", help="hwdep device (/dev/snd/hwC<n>D0); default: auto")
    ap.add_argument("--dsp-dir", default=pl.DEFAULT_DSP_DIR)
    ap.add_argument("--devices-dir", default=os.environ.get("PULSAR_DEVICES_DIR"),
                    help="SCOPE Devices folder (.dev); default /var/lib/snd-pulsar/devices or <dsp-dir>/../../Devices")
    ap.add_argument("--rate", type=int, default=48000, choices=(32000, 44100, 48000))
    ap.add_argument("--volume", type=float, default=-30.0)
    ap.add_argument("--monitor", type=float, default=-12.0)
    ap.add_argument("--no-monitor", action="store_true")
    ap.add_argument("--no-midi", action="store_true", help="no ALSA MIDI client / PC MIDI In node")
    ap.add_argument("--state", default=None,
                    help="autosave of the current rack, restored at start ('' disables; default "
                         "/var/lib/snd-pulsar/current-project.json, none with --dry-run)")
    ap.add_argument("--dry-run", action="store_true", help="simulated card (development)")
    ap.add_argument("--log", default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    # accept the loader options used in /etc/default/snd-pulsar
    ap.add_argument("--pcm", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()

    args = argparse.Namespace(dry_run=a.dry_run, log=a.log or ("/dev/null" if a.dry_run else None),
                              resource=a.resource, dsp_dir=a.dsp_dir, verbose=a.verbose, bus_master=True,
                              irq=True, no_finish=False, rate=a.rate, clock="internal", pcm=True, tone=None,
                              volume=a.volume, monitor=None if a.no_monitor else a.monitor)
    b, ok, handles = pl.boot_card(args)
    if not ok or handles is None:
        print("pulsard: card boot failed", file=sys.stderr)
        return 1
    graph = Graph(b, a.dsp_dir, a.rate, handles)
    try:
        import pulsar_license as plic
        lic_path = os.environ.get("PULSAR_LICENSE") or "/var/lib/snd-pulsar/license"
        if plic.key_files(lic_path) and getattr(b, "uc_serial", None) is not None:
            graph.lic = plic.License(lic_path, b.uc_serial, b.uc_info)
            syms = {k: graph.rack.dsp[plic.UC_DSP].sym(k) for k in ("ucMagicDest", "ucDataOut", "ucBytePosOut", "ucCmdOut")}
            graph.unlock = plic.unlock_hook(graph.lic, syms)
            print("pulsard: SCOPE licence loaded (%d valid entries)" % sum(1 for e in graph.lic.entries if e.valid),
                  flush=True)
    except Exception as e:
        print("pulsard: licence not loaded: %s" % e, flush=True)
    if not a.no_midi:
        try:
            graph.midi_dsp = 2
            graph.start_midi(graph.midi_dsp)
        except Exception as e:
            graph.midi_dsp = None
            print("pulsard: MIDI input disabled: %s" % e, flush=True)
    for cand in (a.devices_dir, "/var/lib/snd-pulsar/devices",
                 os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(a.dsp_dir))), "Devices")):
        if cand and os.path.isdir(cand):
            graph.devices_dir = os.path.abspath(cand)
            break
    if a.state is None:
        a.state = "" if a.dry_run else "/var/lib/snd-pulsar/current-project.json"
    if a.state:
        graph.state_file = a.state
        if os.path.exists(a.state):
            try:
                with open(a.state) as f:
                    r = graph.import_project(json.load(f))
                print("pulsard: restored previous session (%d modules%s)" %
                      (len(r["ids"]), ", errors: " + "; ".join(r["errors"]) if r["errors"] else ""), flush=True)
            except (OSError, ValueError, GraphError) as e:
                print("pulsard: could not restore previous session: %s" % e, flush=True)
    if not a.dry_run:
        subprocess.run(["alsactl", "restore", "Pulsar2"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        os.unlink(a.socket)
    except FileNotFoundError:
        pass
    srv = Server(a.socket, Handler)
    srv.graph = graph
    try:
        os.chown(a.socket, -1, grp.getgrnam(a.group).gr_gid)
        os.chmod(a.socket, 0o660)
    except (KeyError, PermissionError):
        os.chmod(a.socket, 0o600)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=srv.shutdown).start())
    print("pulsard: card ready at %d Hz, listening on %s" % (a.rate, a.socket), flush=True)
    sd_notify("READY=1")
    try:
        srv.serve_forever()
    finally:
        srv.server_close()
        try:
            os.unlink(a.socket)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
