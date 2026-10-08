#!/usr/bin/env python3
"""
Pulsar Scope - modular patching GUI for the Creamware / Sonic Core Pulsar II (Qt / PySide6).

Talks to pulsard over /run/pulsard.sock (users of group "audio"). Desktop independent (GNOME, Plasma,
LXQt/LXDE, XFCE...): it uses Qt's Fusion style with its own dark palette.

  - left: library of DSP modules (search, drag onto the rack or double-click)
  - centre: the rack; drag from an output pad to an input pad to wire, select a cable and press Delete
    (or right-click) to remove it, double-click a module to set its unconnected inputs
  - bottom: sample rate and per-DSP load
"""

import json
import math
import os
import socket
import sys

from PySide6.QtCore import QMimeData, QPointF, QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (QAction, QBrush, QColor, QDrag, QFont, QKeySequence, QPainter, QPainterPath,
                           QPalette, QPen)
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QDockWidget, QFileDialog, QFormLayout,
                               QGraphicsItem, QGraphicsPathItem, QGraphicsScene, QGraphicsView, QHBoxLayout,
                               QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QProgressBar, QSlider,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget, QGridLayout)

import pulsar_values
import scope_device
from pulsar_widgets import Knob
from PySide6.QtWidgets import QPushButton

SOCKET = os.environ.get("PULSARD_SOCKET", "/run/pulsard.sock")
LAYOUT_FILE = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
                           "pulsar-scope", "layout.json")
MIME = "application/x-pulsar-module"

# SCOPE-like colours
C_BG = QColor(40, 42, 46)
C_GRID = QColor(48, 50, 55)
C_PANEL = QColor(62, 64, 70)
C_PANEL_FIXED = QColor(52, 60, 72)
C_PANEL_DEV = QColor(78, 66, 50)
C_TITLE = QColor(28, 30, 33)
C_TEXT = QColor(220, 220, 220)
C_DIM = QColor(150, 150, 155)
C_SYNC = QColor(255, 160, 40)        # audio-rate (sync) pads and cables
C_ASYNC = QColor(90, 170, 255)       # control-rate (async) pads and cables
C_SEL = QColor(255, 255, 255)

PAD_R = 6
ROW_H = 20
NODE_W = 190
TITLE_H = 26


class DaemonError(Exception):
    pass


def request(req, timeout=30):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(SOCKET)
            s.sendall((json.dumps(req) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(1 << 20)
                if not chunk:
                    break
                buf += chunk
    except OSError as e:
        raise DaemonError("cannot talk to pulsard (%s): %s" % (SOCKET, e))
    res = json.loads(buf)
    if not res.get("ok"):
        raise DaemonError(res.get("error", "unknown error"))
    return res


class CatalogThread(QThread):
    done = Signal(object)

    def __init__(self, what="catalog"):
        super().__init__()
        self.what = what

    def run(self):
        try:
            r = request({"cmd": self.what}, timeout=900)
            self.done.emit(r["modules"] if self.what == "catalog" else r["devices"])
        except DaemonError as e:
            self.done.emit(e)


# --------------------------------------------------------------------------- graphics items

class PadItem(QGraphicsItem):
    def __init__(self, node, kind, info):
        super().__init__(node)
        self.node, self.kind, self.info = node, kind, info      # kind: "in" / "out"
        self.index = info["index"]
        self.setAcceptHoverEvents(True)
        self.setToolTip("%s %d: %s%s" % ("input" if kind == "in" else "output", self.index,
                                         info.get("long") or info.get("name") or "",
                                         " (audio)" if info.get("sync") else " (control)"))
        self.hover = False

    def color(self):
        return C_SYNC if self.info.get("sync") else C_ASYNC

    def boundingRect(self):
        return QRectF(-PAD_R - 1, -PAD_R - 1, 2 * PAD_R + 2, 2 * PAD_R + 2)

    def paint(self, p, *_):
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(C_SEL if self.hover else self.color().darker(150), 1.5))
        p.setBrush(self.color() if self.node.is_connected(self) else C_TITLE)
        p.drawEllipse(QPointF(0, 0), PAD_R, PAD_R)

    def hoverEnterEvent(self, e):
        self.hover = True
        self.update()

    def hoverLeaveEvent(self, e):
        self.hover = False
        self.update()

    def scene_pos(self):
        return self.scenePos()



class NodeItem(QGraphicsItem):
    def __init__(self, window, desc):
        super().__init__()
        self.win = window
        self.setFlags(QGraphicsItem.ItemIsMovable | QGraphicsItem.ItemIsSelectable |
                      QGraphicsItem.ItemSendsGeometryChanges)
        self.pads_in, self.pads_out = [], []
        self.update_desc(desc)

    def update_desc(self, desc):
        self.desc = desc
        self.id = desc["id"]
        for p in self.pads_in + self.pads_out:
            p.setParentItem(None)
            if p.scene():
                p.scene().removeItem(p)
        self.pads_in = [PadItem(self, "in", i) for i in desc["inputs"]]
        self.pads_out = [PadItem(self, "out", o) for o in desc["outputs"]]
        rows = max(len(self.pads_in), len(self.pads_out), 1)
        self.h = TITLE_H + rows * ROW_H + 8
        for k, p in enumerate(self.pads_in):
            p.setPos(0, TITLE_H + 10 + k * ROW_H)
        for k, p in enumerate(self.pads_out):
            p.setPos(NODE_W, TITLE_H + 10 + k * ROW_H)
        self.prepareGeometryChange()
        self.update()

    def is_connected(self, pad):
        return self.win.pad_connected(self.id, pad.kind, pad.index)

    def boundingRect(self):
        return QRectF(-PAD_R - 2, -2, NODE_W + 2 * PAD_R + 4, self.h + 4)

    def paint(self, p, *_):
        d = self.desc
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(0, 0, NODE_W, self.h)
        p.setPen(QPen(C_SEL if self.isSelected() else QColor(20, 20, 22), 1.5))
        p.setBrush(C_PANEL_FIXED if d.get("fixed") else (C_PANEL_DEV if d.get("kind") == "device" else C_PANEL))
        p.drawRoundedRect(r, 5, 5)
        p.setBrush(C_TITLE)
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(QRectF(1, 1, NODE_W - 2, TITLE_H - 2), 4, 4)
        f = QFont()
        f.setPointSizeF(8.5)
        f.setBold(True)
        p.setFont(f)
        p.setPen(C_TEXT)
        title = d.get("title") or d.get("name") or d["id"]
        p.drawText(QRectF(8, 0, NODE_W - 60, TITLE_H), Qt.AlignVCenter | Qt.AlignLeft,
                   p.fontMetrics().elidedText(title, Qt.ElideRight, NODE_W - 60))
        f.setBold(False)
        f.setPointSizeF(7.5)
        p.setFont(f)
        p.setPen(C_DIM)
        badge = "DSP%d" % d["dsp"] if d.get("dsp") is not None else "PC"
        if d.get("kind") == "device":
            badge = "DEV " + badge
        p.drawText(QRectF(NODE_W - 52, 0, 46, TITLE_H), Qt.AlignVCenter | Qt.AlignRight, badge)
        p.setPen(C_TEXT)
        for pad in self.pads_in:
            name = pad.info.get("name") or str(pad.index)
            val = self.win.pad_value(self.id, pad.index)
            if val is not None and not self.is_connected(pad):
                prm = pulsar_values.param_for(d.get("file"), pad.info)
                name += " = %s" % prm.fmt(prm.from_raw(val, (self.win.status or {}).get("rate", 48000)))
            p.drawText(QRectF(PAD_R + 4, pad.y() - 8, NODE_W / 2 + 30, 16), Qt.AlignVCenter | Qt.AlignLeft, name)
        for pad in self.pads_out:
            p.drawText(QRectF(NODE_W / 2 - 10, pad.y() - 8, NODE_W / 2 - PAD_R, 16), Qt.AlignVCenter | Qt.AlignRight,
                       pad.info.get("name") or str(pad.index))

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged:
            self.win.node_moved(self)
        return super().itemChange(change, value)

    def mouseDoubleClickEvent(self, e):
        self.win.edit_values(self)

    def contextMenuEvent(self, e):
        m = QMenu()
        a_vals = m.addAction("Set input values…")
        a_del = m.addAction("Remove module")
        a_del.setEnabled(not self.desc.get("fixed"))
        act = m.exec(e.screenPos())
        if act == a_vals:
            self.win.edit_values(self)
        elif act == a_del:
            self.win.remove_node(self.id)


class WireItem(QGraphicsPathItem):
    def __init__(self, wire, src_pad, dst_pad):
        super().__init__()
        self.wire, self.src, self.dst = wire, src_pad, dst_pad
        self.setFlags(QGraphicsItem.ItemIsSelectable)
        self.setZValue(-1)
        self.sync = src_pad.info.get("sync", True)
        self.update_path()

    def update_path(self):
        self.setPath(bezier(self.src.scene_pos(), self.dst.scene_pos()))

    def paint(self, p, opt, w=None):
        c = C_SYNC if self.sync else C_ASYNC
        self.setPen(QPen(C_SEL if self.isSelected() else c, 3 if self.isSelected() else 2.2,
                         Qt.SolidLine, Qt.RoundCap))
        p.setRenderHint(QPainter.Antialiasing)
        super().paint(p, opt, w)

    def shape(self):
        s = QPainterPath()
        s.addPath(self.path())
        from PySide6.QtGui import QPainterPathStroker
        st = QPainterPathStroker()
        st.setWidth(10)
        return st.createStroke(self.path())

    def contextMenuEvent(self, e):
        m = QMenu()
        a = m.addAction("Remove cable")
        if m.exec(e.screenPos()) == a:
            self.scene().views()[0].win.disconnect_wire(self.wire)


def bezier(a, b):
    path = QPainterPath(a)
    dx = max(40.0, abs(b.x() - a.x()) * 0.5)
    path.cubicTo(QPointF(a.x() + dx, a.y()), QPointF(b.x() - dx, b.y()), b)
    return path


def s32(v):
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v & 0x80000000 else v


def slider_range(info):
    """Slider covers 0..max of the pad (negative values are left out: they are rarely useful as constants)."""
    hi = info.get("max", 0)
    return (0, hi) if hi > 0 else (0, 0x7FFFFFFF)


def fmt_value(v, info):
    lo, hi = slider_range(info)
    return "%.1f%%" % (100.0 * (s32(v) - lo) / (hi - lo))


# --------------------------------------------------------------------------- view

class RackView(QGraphicsView):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setScene(QGraphicsScene(-2000, -2000, 6000, 4000))
        self.setRenderHint(QPainter.Antialiasing)
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self.setAcceptDrops(True)
        self.setBackgroundBrush(C_BG)
        self.temp = None
        self.temp_from = None

    def drawBackground(self, p, rect):
        p.fillRect(rect, C_BG)
        p.setPen(QPen(C_GRID, 1))
        step = 24
        x = math.floor(rect.left() / step) * step
        while x < rect.right():
            p.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            x += step
        y = math.floor(rect.top() / step) * step
        while y < rect.bottom():
            p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            y += step

    # ---- wiring by drag
    def start_wire(self, pad):
        self.temp_from = pad
        self.temp = QGraphicsPathItem()
        self.temp.setPen(QPen(pad.color(), 2, Qt.DashLine))
        self.temp.setZValue(10)
        self.scene().addItem(self.temp)

    def mousePressEvent(self, e):
        # a press on a pad starts a cable; the view keeps receiving move/release events for the whole drag
        if e.button() == Qt.LeftButton:
            for it in self.items(e.position().toPoint()):
                if isinstance(it, PadItem):
                    self.start_wire(it)
                    e.accept()
                    return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self.temp is not None:
            a = self.temp_from.scene_pos()
            b = self.mapToScene(e.position().toPoint())
            self.temp.setPath(bezier(a, b) if self.temp_from.kind == "out" else bezier(b, a))
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self.temp is not None:
            self.scene().removeItem(self.temp)
            self.temp = None
            target = None
            for it in self.items(e.position().toPoint()):
                if isinstance(it, PadItem):
                    target = it
                    break
            src = self.temp_from
            self.temp_from = None
            if target is not None and target.kind != src.kind and target.node is not src.node:
                out, inp = (src, target) if src.kind == "out" else (target, src)
                self.win.connect_pads(out, inp)
            return
        super().mouseReleaseEvent(e)

    def wheelEvent(self, e):
        f = 1.15 if e.angleDelta().y() > 0 else 1 / 1.15
        self.scale(f, f)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            for it in self.scene().selectedItems():
                if isinstance(it, WireItem):
                    self.win.disconnect_wire(it.wire)
                elif isinstance(it, NodeItem) and not it.desc.get("fixed"):
                    self.win.remove_node(it.id)
            return
        super().keyPressEvent(e)

    # ---- drop from the library
    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat(MIME):
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasFormat(MIME):
            e.acceptProposedAction()

    def dropEvent(self, e):
        f = bytes(e.mimeData().data(MIME)).decode()
        pos = self.mapToScene(e.position().toPoint())
        if f.startswith("dev:"):
            self.win.load_device(f[4:], pos)
        else:
            self.win.load_module(f, pos)
        e.acceptProposedAction()


class Library(QTreeWidget):
    def __init__(self):
        super().__init__()
        self.setHeaderHidden(True)
        self.setDragEnabled(True)

    def startDrag(self, actions):
        it = self.currentItem()
        if it is None or it.data(0, Qt.UserRole) is None:
            return
        m = it.data(0, Qt.UserRole)
        md = QMimeData()
        md.setData(MIME, (("dev:" + m["device"]) if "device" in m else m["file"]).encode())
        d = QDrag(self)
        d.setMimeData(md)
        d.exec(Qt.CopyAction)


# modules for other boards / hardware I/O (already in the base rack) are not offered
HIDDEN = ("apollo", "be-plate", "elektra", "luna", "c-plate", "xite", "satellite", "sdram", "bridge",
          " dest", " source", "adat", "s/pdif", "sp-dif", "spdif", "test")
CATEGORIES = [            # first match wins: the more specific groups come first
    ("MIDI", ("midi", "voice", "arpeg", "sequen", "note", "velocity")),
    ("Dynamics", ("comp", "limit", "gate", "expander", "dynamic")),
    ("Effects", ("reverb", "verb", "delay", "echo", "chorus", "flang", "phas", "distort", "drive", "pitch",
                 "vocod", "ring", "tremolo", "rotor", "leslie", "bitcrush", "rectif")),
    ("Filters & EQ", ("filter", "eq", "lowpass", "highpass", "bandpass", "notch", "formant", "vowel")),
    ("Envelopes", ("env", "adsr", "ahd")),
    ("Oscillators", ("osc", "sine", "saw", "pulse", "noise", "lfo")),
    ("Mixers & volume", ("mix", "add", "pan", "vol", "vca", "gain", "atten", "amp", "level", "fader", "split",
                         "nix", "sum")),
    ("Logic & control", ("and", "or ", "xor", "not", "flip", "sample", "hold", "switch", "multipl", "mult",
                         "logic", "compar", "trigger", "counter", "inv", "const", "slew", "smooth")),
]


def category(m):
    text = " %s %s %s " % (m["long"], m["name"], m["file"])
    t = text.lower()
    for cat, keys in CATEGORIES:
        if any(k in t for k in keys):
            return cat
    return "Other"


# --------------------------------------------------------------------------- value editor

class DevParam:
    """Adapter giving a device parameter (scope_device param spec) the Param interface used by Knob."""

    def __init__(self, q):
        self.q, self.name, self.unit = q, q["name"], q.get("unit") or ""
        self.spec = dict(q.get("spec") or {}, name=q["name"])
        self.lo = q["min"] if q.get("min") is not None else 0.0
        self.hi = q["max"] if q.get("max") is not None else 1.0
        self.default = q["default"] if q.get("default") is not None else self.lo

    def from_t(self, t):
        if self.spec.get("knob"):
            v = scope_device.knob_to_display(self.spec, min(max(t, 0.0), 1.0))
            if v is not None:
                return v
        return self.lo + t * (self.hi - self.lo)

    def to_t(self, v):
        if self.spec.get("knob"):
            t = scope_device.display_to_knob(self.spec, v)
            if t is not None:
                return t
        return 0.0 if self.hi == self.lo else (v - self.lo) / (self.hi - self.lo)

    def fmt(self, v):
        fmt = self.q.get("format")
        if fmt:
            try:
                return scope_device.c_format(fmt, v)
            except Exception:
                pass
        return pulsar_values.Param(self.name, self.unit).fmt(v)


class DevicePanel(QDialog):
    """Front panel of a SCOPE device: one knob per parameter (switches as buttons), applied live."""

    COLS = 6

    def __init__(self, win, node):
        super().__init__(win)
        self.win, self.node = win, node
        self.setWindowTitle(node.desc.get("title") or node.id)
        lay = QVBoxLayout(self)
        grid = QGridLayout()
        lay.addLayout(grid)
        k = 0
        for q in node.desc.get("params", []):
            cur = q.get("value") if q.get("value") is not None else q.get("default")
            if q.get("discrete") and (q.get("max") or 0) - (q.get("min") or 0) <= 1:
                b = QPushButton(q["name"])
                b.setCheckable(True)
                b.setChecked(bool(cur))
                b.setMinimumWidth(80)
                b.toggled.connect(lambda on, name=q["name"]: self.win.set_param_live(self.node.id, name, 1 if on else 0))
                w = b
            else:
                prm = DevParam(q)
                w = Knob(prm, cur)
                w.changed.connect(lambda val, name=q["name"]: self.win.set_param_live(self.node.id, name, val))
            grid.addWidget(w, k // self.COLS, k % self.COLS, Qt.AlignCenter)
            k += 1
        if k == 0:
            lay.addWidget(QLabel("This device has no parameters."))
        info = QLabel("%s - %d DSP modules on DSP%s, %d cycles" % (
            node.desc.get("file", ""), node.desc.get("modules", 0), node.desc.get("dsp"), node.desc.get("cycles", 0)))
        info.setStyleSheet("color: #888")
        lay.addWidget(info)


class KnobPanel(QDialog):
    """SCOPE-like control panel of a module: one knob per unconnected input, applied live."""

    COLS = 6

    def __init__(self, win, node, values):
        super().__init__(win)
        self.win, self.node = win, node
        self.setWindowTitle(node.desc.get("title") or node.id)
        rate = (win.status or {}).get("rate", 48000)
        lay = QVBoxLayout(self)
        grid = QGridLayout()
        lay.addLayout(grid)
        k = 0
        for pad in node.pads_in:
            info = pad.info
            name = info.get("long") or info.get("name") or str(pad.index)
            if node.is_connected(pad):
                lab = QLabel("%s\n(connected)" % (info.get("name") or name))
                lab.setAlignment(Qt.AlignCenter)
                lab.setStyleSheet("color: #888")
                w = lab
            else:
                param = pulsar_values.param_for(node.desc.get("file"), info)
                if not param.pad.settable:
                    continue
                raw = values.get(pad.index)
                v = param.from_raw(raw, rate) if raw is not None else param.default
                w = Knob(param, v)
                w.changed.connect(lambda val, idx=pad.index, prm=param: self.win.set_value_live(
                    self.node.id, idx, prm.raw(val, rate)))
            grid.addWidget(w, k // self.COLS, k % self.COLS, Qt.AlignCenter)
            k += 1
        if k == 0:
            lay.addWidget(QLabel("This module has no settable inputs."))
        warn = QLabel("Changes are applied immediately. Careful: oscillators are full scale.")
        warn.setStyleSheet("color: #e0a040")
        lay.addWidget(warn)


# --------------------------------------------------------------------------- main window

class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Pulsar Scope")
        self.resize(1300, 800)
        self.view = RackView(self)
        self.setCentralWidget(self.view)
        self.nodes, self.wire_items, self.status = {}, [], None
        self.layout = self.load_layout()

        # library
        dock = QDockWidget("Modules", self)
        w = QWidget()
        v = QVBoxLayout(w)
        self.search = QLineEdit()
        self.search.setPlaceholderText("search modules…")
        self.search.textChanged.connect(self.filter_library)
        self.lib = Library()
        self.lib.itemDoubleClicked.connect(self.library_double_click)
        v.addWidget(self.search)
        v.addWidget(self.lib)
        self.lib_info = QLabel("loading catalog…")
        self.lib_info.setWordWrap(True)
        v.addWidget(self.lib_info)
        dock.setWidget(w)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)
        self.lib.currentItemChanged.connect(self.show_module_info)

        # status bar: rate + DSP load
        self.rate_label = QLabel()
        self.statusBar().addWidget(self.rate_label)
        self.dsp_bars = []
        for d in range(6):
            b = QProgressBar()
            b.setFormat("DSP%d %%p%%" % d)
            b.setMaximumWidth(110)
            b.setTextVisible(True)
            self.statusBar().addPermanentWidget(b)
            self.dsp_bars.append(b)

        a = QAction("Refresh", self)
        a.setShortcut(QKeySequence.Refresh)
        a.triggered.connect(self.refresh)
        self.addAction(a)

        self.project_path, self.dirty = None, False
        fm = self.menuBar().addMenu("&File")
        for text, key, slot in (("&New (base configuration)", QKeySequence.New, self.new_project),
                                ("&Open project…", QKeySequence.Open, self.open_project),
                                ("&Save project", QKeySequence.Save, self.save_project),
                                ("Save project &as…", QKeySequence.SaveAs, self.save_project_as),
                                (None, None, None),
                                ("&Quit", QKeySequence.Quit, self.close)):
            if text is None:
                fm.addSeparator()
                continue
            act = fm.addAction(text)
            act.setShortcut(key)
            act.triggered.connect(slot)
        self.update_title()

        self.refresh()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(3000)
        self.cat_thread = CatalogThread()
        self.cat_thread.done.connect(self.fill_library)
        self.cat_thread.start()
        self.dev_thread = CatalogThread("devices")
        self.dev_thread.done.connect(self.fill_devices)
        self.dev_thread.start()

    # ---- daemon calls
    def call(self, req):
        try:
            return request(req)
        except DaemonError as e:
            QMessageBox.warning(self, "Pulsar Scope", str(e))
            return None

    def refresh(self):
        try:
            st = request({"cmd": "status"})
        except DaemonError as e:
            self.rate_label.setText(str(e))
            return
        self.status = st
        self.values = {(v["id"], v["in"]): v["value"] for v in st["values"]}
        self.rate_label.setText("  %d Hz  " % st["rate"])
        for d in st["dsps"]:
            b = self.dsp_bars[d["dsp"]]
            b.setMaximum(max(1, d["budget"]))
            b.setValue(min(d["cycles"], d["budget"]))
            b.setToolTip("DSP%d: %d modules, %d/%d cycles per sample, PM free %d, DM free %d words" %
                         (d["dsp"], d["modules"], d["cycles"], d["budget"], d["pm_free"], d["dm_free"]))
        seen = set()
        for k, desc in enumerate(st["nodes"]):
            seen.add(desc["id"])
            n = self.nodes.get(desc["id"])
            if n is None:
                n = NodeItem(self, desc)
                self.view.scene().addItem(n)
                pos = (st.get("gui", {}).get("layout", {}).get(desc["id"]) or self.layout.get(desc["id"])
                       or self.default_pos(desc, k))
                n.setPos(QPointF(*pos))
                self.nodes[desc["id"]] = n
            elif n.desc != desc:
                n.update_desc(desc)
        for nid in list(self.nodes):
            if nid not in seen:
                self.view.scene().removeItem(self.nodes.pop(nid))
        self.redraw_wires()

    def default_pos(self, desc, k):
        cols = {"pc_play": (0, 0), "n3": (0, 200), "n1": (0, 380),
                "n4": (300, 0), "n5": (300, 120), "n7": (300, 240), "n8": (300, 360),
                "n6": (600, 60), "n9": (600, 300), "n2": (900, 150), "pc_rec": (900, 360)}
        if desc["id"] in cols:
            return cols[desc["id"]]
        return (300 + 40 * (k % 10), 520 + 30 * (k % 10))

    def redraw_wires(self):
        for w in self.wire_items:
            self.view.scene().removeItem(w)
        self.wire_items = []
        self.connected = set()
        for w in self.status["wires"]:
            s, d = self.nodes.get(w["src"]), self.nodes.get(w["dst"])
            if s is None or d is None or w["out"] >= len(s.pads_out) or w["in"] >= len(d.pads_in):
                continue
            item = WireItem(w, s.pads_out[w["out"]], d.pads_in[w["in"]])
            self.view.scene().addItem(item)
            self.wire_items.append(item)
            self.connected.add((w["src"], "out", w["out"]))
            self.connected.add((w["dst"], "in", w["in"]))
        for n in self.nodes.values():
            n.update()

    def pad_connected(self, nid, kind, idx):
        return (nid, kind, idx) in getattr(self, "connected", set())

    def pad_value(self, nid, idx):
        return getattr(self, "values", {}).get((nid, idx))

    def node_moved(self, node):
        for w in self.wire_items:
            if w.src.node is node or w.dst.node is node:
                w.update_path()
        self.layout[node.id] = (node.pos().x(), node.pos().y())
        if not getattr(self, "_refreshing", False) and node.isUnderMouse():
            self.mark_dirty()
        self.save_layout_later()

    # ---- actions
    def connect_pads(self, out_pad, in_pad):
        if self.call({"cmd": "connect", "src": out_pad.node.id, "out": out_pad.index,
                      "dst": in_pad.node.id, "in": in_pad.index}) is not None:
            self.mark_dirty()
            self.refresh()

    def disconnect_wire(self, w):
        if self.call({"cmd": "disconnect", "dst": w["dst"], "in": w["in"]}) is not None:
            self.mark_dirty()
            self.refresh()

    def remove_node(self, nid):
        if self.call({"cmd": "unload", "id": nid}) is not None:
            self.layout.pop(nid, None)
            self.mark_dirty()
            self.refresh()

    def load_module(self, file, pos):
        r = self.call({"cmd": "load", "file": file})
        if r is not None:
            self.layout[r["id"]] = (pos.x(), pos.y())
            self.save_layout_later()
            self.mark_dirty()
            self.refresh()

    def set_value(self, nid, idx, v):
        if self.call({"cmd": "set", "id": nid, "in": idx, "value": v}) is not None:
            self.mark_dirty()
            self.refresh()

    def edit_values(self, node):
        if node.desc.get("kind") == "device":
            DevicePanel(self, node).show()
            return
        if node.desc.get("fixed"):
            QMessageBox.information(self, "Pulsar Scope", "The levels of the base configuration are in the ALSA "
                                    "mixer (alsamixer: DSP Out, Input Monitor).")
            return
        vals = {i: v for (n, i), v in self.values.items() if n == node.id}
        KnobPanel(self, node, vals).show()

    def set_param_live(self, nid, name, value):
        try:
            request({"cmd": "set_param", "id": nid, "name": name, "value": value})
        except DaemonError as e:
            self.rate_label.setText(str(e))
            return
        n = self.nodes.get(nid)
        if n is not None:
            for q in n.desc.get("params", []):
                if q["name"] == name:
                    q["value"] = value
        self.mark_dirty()

    def load_device(self, file, pos):
        r = self.call({"cmd": "load_device", "file": file})
        if r is not None:
            self.layout[r["id"]] = (pos.x(), pos.y())
            self.save_layout_later()
            self.mark_dirty()
            self.refresh()

    def set_value_live(self, nid, idx, raw):
        """Knob moved: send the value without rebuilding the rack."""
        try:
            request({"cmd": "set", "id": nid, "in": idx, "value": raw})
        except DaemonError as e:
            self.rate_label.setText(str(e))
            return
        self.values[(nid, idx)] = raw
        self.mark_dirty()
        n = self.nodes.get(nid)
        if n is not None:
            n.update()

    # ---- library
    def library_double_click(self, it, _col=0):
        m = it.data(0, Qt.UserRole)
        if m is None:
            return
        pos = self.view.mapToScene(self.view.viewport().rect().center())
        if "device" in m:
            self.load_device(m["device"], pos)
        else:
            self.load_module(m["file"], pos)

    def fill_devices(self, devs):
        if isinstance(devs, Exception) or not devs:
            return
        top = QTreeWidgetItem(["SCOPE devices (%d)" % len(devs)])
        cats = {}
        for d in sorted(devs, key=lambda d: (d["category"], d["name"].lower())):
            cat = d["category"] or "Other"
            if cat not in cats:
                cats[cat] = QTreeWidgetItem([cat.replace("/", " / ")])
                top.addChild(cats[cat])
            it = QTreeWidgetItem([d["name"]])
            it.setData(0, Qt.UserRole, {"device": d["file"], "long": d["name"], "name": d["name"], "file": d["file"],
                                        "inputs": [{"name": n} for n in d["inputs"]],
                                        "outputs": [{"name": n} for n in d["outputs"]], "cycles": d["cycles"],
                                        "params": d["params"]})
            it.setToolTip(0, "%s\n%d DSP modules, %d cycles\nparameters: %s" % (
                d["file"], d["modules"], d["cycles"], ", ".join(d["params"]) or "-"))
            cats[cat].addChild(it)
        self.lib.insertTopLevelItem(0, top)
        top.setExpanded(True)

    def fill_library(self, cat):
        if isinstance(cat, Exception):
            self.lib_info.setText("catalog unavailable: %s" % cat)
            return
        mods = []
        for m in cat:
            m = dict(m, long=m["long"].strip(), name=m["name"].strip())
            label = (m["long"] or m["name"]).lower()
            if not m["long"] or m["fixed_dsp"] is not None or any(h in " " + label for h in HIDDEN):
                continue
            mods.append(m)
        groups = {}
        for m in sorted(mods, key=lambda m: m["long"].lower()):
            groups.setdefault(category(m), []).append(m)
        self.lib.clear()
        for cat_name in [c for c, _ in CATEGORIES] + ["Other"]:
            if cat_name not in groups:
                continue
            top = QTreeWidgetItem(["%s (%d)" % (cat_name, len(groups[cat_name]))])
            top.setFlags(top.flags() & ~Qt.ItemIsDragEnabled)
            for m in groups[cat_name]:
                it = QTreeWidgetItem([m["long"]])
                it.setData(0, Qt.UserRole, m)
                it.setToolTip(0, "%s - %s (%d cycles)" % (m["name"], m["file"], m["cycles"]))
                top.addChild(it)
            self.lib.addTopLevelItem(top)
        self.lib_info.setText("%d modules. Drag one onto the rack or double-click it." % len(mods))

    def filter_library(self, text):
        t = text.lower()

        def walk(item):
            m = item.data(0, Qt.UserRole)
            if m is not None:
                hide = bool(t) and t not in (m.get("long", "") + " " + m.get("name", "") + " " + m.get("file", "")).lower()
                item.setHidden(hide)
                return not hide
            shown = sum(walk(item.child(j)) for j in range(item.childCount()))
            item.setHidden(shown == 0)
            if t:
                item.setExpanded(shown > 0)
            return shown > 0

        for i in range(self.lib.topLevelItemCount()):
            walk(self.lib.topLevelItem(i))

    def show_module_info(self, it, _prev=None):
        if it is None or it.data(0, Qt.UserRole) is None:
            return
        m = it.data(0, Qt.UserRole)
        ins = ", ".join(p["name"] for p in m["inputs"]) or "-"
        outs = ", ".join(p["name"] for p in m["outputs"]) or "-"
        extra = ("<br>parameters: %s" % (", ".join(m["params"]) or "-")) if "device" in m else ""
        self.lib_info.setText("<b>%s</b><br>%s<br>in: %s<br>out: %s<br>%d cycles%s" %
                              (m["long"], m["file"], ins, outs, m["cycles"], extra))

    # ---- layout persistence
    def load_layout(self):
        try:
            with open(LAYOUT_FILE) as f:
                return {k: tuple(v) for k, v in json.load(f).items()}
        except (OSError, ValueError):
            return {}

    def save_layout_later(self):
        if not hasattr(self, "_save_timer"):
            self._save_timer = QTimer(self)
            self._save_timer.setSingleShot(True)
            self._save_timer.timeout.connect(self.save_layout)
        self._save_timer.start(1000)

    def save_layout(self):
        layout = {nid: (n.pos().x(), n.pos().y()) for nid, n in self.nodes.items()}
        self.layout.update(layout)
        try:
            request({"cmd": "set_gui", "gui": {"layout": layout}})     # kept with the rack and in projects
        except DaemonError:
            pass
        os.makedirs(os.path.dirname(LAYOUT_FILE), exist_ok=True)
        with open(LAYOUT_FILE, "w") as f:
            json.dump(self.layout, f)

    # ---- projects
    def update_title(self):
        name = os.path.basename(self.project_path) if self.project_path else "untitled"
        self.setWindowTitle("%s%s - Pulsar Scope" % ("*" if self.dirty else "", name))

    def mark_dirty(self):
        self.dirty = True
        self.update_title()

    def confirm_discard(self):
        if not self.dirty:
            return True
        r = QMessageBox.question(self, "Pulsar Scope", "The current rack has unsaved changes. Discard them?",
                                 QMessageBox.Discard | QMessageBox.Cancel)
        return r == QMessageBox.Discard

    def clear_rack(self):
        for n in list(self.nodes.values()):
            self.view.scene().removeItem(n)
        self.nodes.clear()
        self.layout = {}

    def new_project(self):
        if not self.confirm_discard():
            return
        if self.call({"cmd": "reset"}) is None:
            return
        self.clear_rack()
        self.project_path, self.dirty = None, False
        self.update_title()
        self.refresh()

    def open_project(self):
        if not self.confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open project", os.path.expanduser("~"),
                                              "Pulsar projects (*.pulsar);;All files (*)")
        if not path:
            return
        try:
            with open(path) as f:
                prj = json.load(f)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Pulsar Scope", "Cannot read %s: %s" % (path, e))
            return
        r = self.call({"cmd": "load_project", "project": prj})
        if r is None:
            return
        self.clear_rack()
        self.project_path, self.dirty = path, False
        self.update_title()
        self.refresh()
        if r.get("errors"):
            QMessageBox.warning(self, "Pulsar Scope", "Project loaded with problems:\n\n" + "\n".join(r["errors"]))

    def save_project(self):
        if not self.project_path:
            return self.save_project_as()
        self.save_layout()
        r = self.call({"cmd": "save_project"})
        if r is None:
            return False
        try:
            with open(self.project_path, "w") as f:
                json.dump(r["project"], f, indent=1)
        except OSError as e:
            QMessageBox.warning(self, "Pulsar Scope", "Cannot save %s: %s" % (self.project_path, e))
            return False
        self.dirty = False
        self.update_title()
        return True

    def save_project_as(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save project", self.project_path or os.path.expanduser("~/untitled.pulsar"),
                                              "Pulsar projects (*.pulsar)")
        if not path:
            return False
        if not path.endswith(".pulsar"):
            path += ".pulsar"
        self.project_path = path
        return self.save_project()

    def closeEvent(self, e):
        if self.confirm_discard():
            e.accept()
        else:
            e.ignore()


def dark_palette(app):
    app.setStyle("Fusion")
    p = QPalette()
    p.setColor(QPalette.Window, QColor(45, 47, 51))
    p.setColor(QPalette.WindowText, C_TEXT)
    p.setColor(QPalette.Base, QColor(32, 34, 37))
    p.setColor(QPalette.AlternateBase, QColor(45, 47, 51))
    p.setColor(QPalette.Text, C_TEXT)
    p.setColor(QPalette.Button, QColor(55, 57, 62))
    p.setColor(QPalette.ButtonText, C_TEXT)
    p.setColor(QPalette.Highlight, C_SYNC.darker(120))
    p.setColor(QPalette.HighlightedText, QColor(20, 20, 20))
    p.setColor(QPalette.ToolTipBase, QColor(30, 30, 30))
    p.setColor(QPalette.ToolTipText, C_TEXT)
    app.setPalette(p)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Pulsar Scope")
    app.setDesktopFileName("pulsar-scope")
    dark_palette(app)
    w = Window()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
