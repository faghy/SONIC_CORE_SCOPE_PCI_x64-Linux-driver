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

The rack is autosaved to /var/lib/snd-pulsar/current-project.json after every change and restored at start.

Outputs are numbered async outputs first, then sync outputs (as the DSP module itself numbers them).
Node "pc_play" is the audio coming from the PC (ALSA playback, 2 sync outputs); "pc_rec" is the audio going
to the PC (ALSA capture, fixed to the analog inputs for now).
"""

import argparse
import grp
import json
import os
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

CORE_CLOCK = 60000000            # pluto coreClock (docs/clock_rate.md)
CYCLE_RESERVE = 0.25             # keep 25 % of every sample period for the OS


class GraphError(Exception):
    pass


def _s32(v):
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v & 0x80000000 else v


class Node:
    def __init__(self, nid, kind, title, mod=None, dsp=None, fixed=False):
        self.id, self.kind, self.title, self.mod, self.dsp, self.fixed = nid, kind, title, mod, dsp, fixed

    def inputs(self):
        if self.kind == "pc_play":
            return []
        if self.kind == "pc_rec":
            return [{"index": i, "name": n, "sync": True} for i, n in enumerate(("L", "R"))]
        return [{"index": p.num, "name": p.short, "long": p.long, "sync": p.sync, "type": p.type,
                 "min": _s32(p.min), "max": _s32(p.max)} for p in self.mod.cls.pads if p.kind == "in"]

    def outputs(self):
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
                     cycles=self.mod.cls.syncCycles)
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
            cyc = sum(m.cls.syncCycles for m in d.modules)
            out.append({"dsp": d.dspno, "modules": len(d.modules), "cycles": cyc, "budget": budget,
                        "pm_free": sum(n for _, n in d.pm.free_ranges()),
                        "dm_free": sum(n for _, n in d.dm.free_ranges())})
        return out

    def execute(self, ops):
        pm.execute(self.b, ops)

    # ---- commands
    def status(self):
        return {"rate": self.rate, "dsps": self.dsp_load(),
                "nodes": [n.describe() for n in self.nodes.values()],
                "wires": [{"src": s, "out": o, "dst": d, "in": i} for (d, i), (s, o) in self.wires.items()],
                "values": [{"id": n, "in": i, "value": v} for (n, i), v in self.values.items()],
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
            cat.append({"file": fn, "name": c.short, "long": c.long, "cycles": c.syncCycles,
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

    def _pick_dsp(self, cls):
        fixed = (cls.flags >> 17) & 0xF
        if fixed:
            return fixed - 1
        budget = CORE_CLOCK / self.rate * (1 - CYCLE_RESERVE)
        best = None
        for d in (2, 3, 4, 5, 1, 0):                # keep DSP0/1 (analog I/O) for last
            cyc = sum(m.cls.syncCycles for m in self.rack.dsp[d].modules)
            if cyc + cls.syncCycles <= budget and (best is None or cyc < best[1]):
                best = (d, cyc)
        if best is None:
            raise GraphError("no DSP has %d free cycles for %s" % (cls.syncCycles, cls.name))
        return best[0]

    def load(self, file, dsp=None, name=None):
        path = os.path.join(self.dsp_dir, os.path.basename(file))
        cls = pm.ModuleClass(path)
        if dsp is None:
            dsp = self._pick_dsp(cls)
        mod, ops = self.rack.load(path, int(dsp))
        self.execute(ops)
        return self.add_node("module", name or cls.long or cls.short, mod)

    def _src_addr(self, src, out, dst_node):
        """Source address for wiring (src may be the pseudo node pc_play)."""
        if src == "pc_play":
            if out not in (0, 1):
                raise GraphError("pc_play has outputs 0 and 1")
            return 0xC000 + 2 * pl.PLAY_SLOTS[out], []      # PC slots are broadcast to every DSP
        return None, None

    def connect(self, src, out, dst, inp):
        dn = self.node(dst)
        if dn.kind != "module":
            raise GraphError("%s has no wirable inputs" % dst)
        if inp >= len(dn.inputs()):
            raise GraphError("%s has no input %d" % (dst, inp))
        sn = self.node(src)
        if sn.kind == "pc_play":
            addr, _ = self._src_addr(src, out, dn)
            ops = self.rack.dsp[dn.dsp].link_input(dn.mod, inp, addr)
        elif sn.kind == "module":
            if out >= len(sn.outputs()):
                raise GraphError("%s has no output %d" % (src, out))
            ops = self.rack.connect(sn.mod, out, dn.mod, inp)
        else:
            raise GraphError("%s has no outputs" % src)
        self.execute(ops)
        self.values.pop((dst, inp), None)
        self.wires[(dst, inp)] = (src, out)

    def disconnect(self, dst, inp):
        dn = self.node(dst)
        if dn.kind != "module":
            raise GraphError("%s inputs cannot be changed" % dst)
        self.execute(self.rack.disconnect(dn.mod, inp))
        self.wires.pop((dst, inp), None)
        self.values.pop((dst, inp), None)

    def set_value(self, nid, inp, value):
        n = self.node(nid)
        if n.kind != "module":
            raise GraphError("%s has no settable inputs" % nid)
        if n.fixed:
            raise GraphError("%s is part of the base configuration (levels are in the ALSA mixer)" % nid)
        self.execute(self.rack.set_in_pad(n.mod, inp, int(value) & 0xFFFFFFFF))
        self.wires.pop((nid, inp), None)
        self.values[(nid, inp)] = int(value) & 0xFFFFFFFF

    def unload(self, nid):
        n = self.node(nid)
        if n.fixed:
            raise GraphError("%s is part of the base configuration" % nid)
        for (d, i), (s, o) in list(self.wires.items()):
            if s == nid and d != "pc_rec":
                self.disconnect(d, i)
        for (d, i) in list(self.wires):
            if d == nid:
                self.wires.pop((d, i))
        self.execute(self.rack.unload(n.mod))
        for k in [k for k in self.values if k[0] == nid]:
            self.values.pop(k)
        del self.nodes[nid]


    # ---- projects
    def export_project(self):
        mods = [{"id": n.id, "file": n.mod.cls.name, "dsp": n.dsp, "title": n.title}
                for n in self.nodes.values() if n.kind == "module" and not n.fixed]
        wires = [{"src": s, "out": o, "dst": d, "in": i} for (d, i), (s, o) in self.wires.items()
                 if self.default_wires.get((d, i)) != (s, o)]
        removed = [{"dst": d, "in": i} for (d, i) in self.default_wires if (d, i) not in self.wires]
        values = [{"id": n, "in": i, "value": v} for (n, i), v in self.values.items()]
        return {"format": "pulsar-project", "version": 1, "rate": self.rate, "modules": mods,
                "wires": wires, "removed": removed, "values": values, "gui": self.gui}

    def reset(self):
        """Back to the base configuration: unload every added module, restore the default wiring."""
        for nid in [n.id for n in self.nodes.values() if not n.fixed]:
            self.unload(nid)
        for (d, i), (src, out) in self.default_wires.items():
            if self.wires.get((d, i)) != (src, out) and d != "pc_rec":
                self.connect(src, out, d, i)
        self.gui = {}

    def import_project(self, prj):
        if prj.get("format") != "pulsar-project":
            raise GraphError("not a Pulsar project")
        self.reset()
        ids, errors = {}, []
        for m in prj.get("modules", []):
            try:
                ids[m["id"]] = self.load(m["file"], m.get("dsp"), m.get("title"))
            except Exception as e:                           # keep going: report what could not be restored
                errors.append("module %s (%s): %s" % (m.get("title"), m.get("file"), e))
        mapped = lambda nid: ids.get(nid, nid)
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
        for v in prj.get("values", []):
            try:
                self.set_value(mapped(v["id"]), int(v["in"]), int(v["value"]))
            except Exception as e:
                errors.append("value %s.%s: %s" % (v.get("id"), v.get("in"), e))
        gui = prj.get("gui") or {}
        self.gui = dict(gui, layout={mapped(k): v for k, v in (gui.get("layout") or {}).items()})
        return {"ids": ids, "errors": errors}

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


MUTATING = {"load", "unload", "connect", "disconnect", "set", "reset", "load_project", "set_gui"}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        g = self.server.graph
        for line in self.rfile:
            try:
                req = json.loads(line)
                cmd = req.get("cmd")
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
    ap.add_argument("--rate", type=int, default=48000, choices=(32000, 44100, 48000))
    ap.add_argument("--volume", type=float, default=-30.0)
    ap.add_argument("--monitor", type=float, default=-12.0)
    ap.add_argument("--no-monitor", action="store_true")
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
