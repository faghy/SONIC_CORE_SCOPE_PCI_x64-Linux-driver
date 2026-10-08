"""
pulsar_widgets - SCOPE-style controls for Pulsar Scope (Qt / PySide6).

Knob: dark round knob with an orange value arc, name above and value + unit below.
  drag up/down to turn (Shift = fine), mouse wheel, double-click = default value.
It works on a normalised position t (0..1) and a pulsar_values.Param that maps t to the displayed value.
"""

import math

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import QSizePolicy, QWidget

C_KNOB_HI = QColor(96, 99, 106)
C_KNOB_LO = QColor(34, 35, 38)
C_TRACK = QColor(26, 27, 30)
C_ARC = QColor(255, 160, 40)
C_TEXT = QColor(220, 220, 220)
C_DIM = QColor(150, 150, 155)

START_DEG, SPAN_DEG = 225.0, 270.0           # 7 o'clock .. 5 o'clock


class Knob(QWidget):
    changed = Signal(float)                  # display value, emitted live (throttled)

    def __init__(self, param, value=None, parent=None, size=64):
        super().__init__(parent)
        self.param = param
        self.t = param.to_t(param.default if value is None else value)
        self.size = size
        self.setMinimumSize(size + 24, size + 34)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setToolTip("%s\ndrag up/down (Shift = fine), wheel, double-click = default" % param.name)
        self._drag = None
        self._emit = QTimer(self)
        self._emit.setSingleShot(True)
        self._emit.timeout.connect(lambda: self.changed.emit(self.value()))

    def sizeHint(self):
        return self.minimumSize()

    def value(self):
        return self.param.from_t(self.t)

    def set_value(self, v, emit=False):
        self.t = min(max(self.param.to_t(v), 0.0), 1.0)
        self.update()
        if emit:
            self._emit.start(30)

    def _set_t(self, t):
        t = min(max(t, 0.0), 1.0)
        if t != self.t:
            self.t = t
            self.update()
            self._emit.start(30)

    # ---- input
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = (e.position().y(), self.t)

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            y0, t0 = self._drag
            scale = 1000.0 if e.modifiers() & Qt.ShiftModifier else 180.0
            self._set_t(t0 + (y0 - e.position().y()) / scale)

    def mouseReleaseEvent(self, e):
        self._drag = None

    def wheelEvent(self, e):
        step = 0.002 if e.modifiers() & Qt.ShiftModifier else 0.02
        self._set_t(self.t + step * (1 if e.angleDelta().y() > 0 else -1))

    def mouseDoubleClickEvent(self, e):
        self._set_t(self.param.to_t(self.param.default))

    # ---- drawing
    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        f = QFont()
        f.setPointSizeF(7.5)
        p.setFont(f)
        p.setPen(C_DIM)
        p.drawText(QRectF(0, 0, w, 14), Qt.AlignCenter,
                   p.fontMetrics().elidedText(self.param.name, Qt.ElideRight, w))
        s = self.size
        c = QPointF(w / 2.0, 16 + s / 2.0)
        r = s / 2.0 - 6
        # value track + arc
        rect = QRectF(c.x() - r - 4, c.y() - r - 4, 2 * r + 8, 2 * r + 8)
        p.setPen(QPen(C_TRACK, 4, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(rect, int(START_DEG * 16), int(-SPAN_DEG * 16))
        p.setPen(QPen(C_ARC, 4, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(rect, int(START_DEG * 16), int(-SPAN_DEG * self.t * 16))
        # knob body
        g = QRadialGradient(c.x() - r * 0.3, c.y() - r * 0.3, r * 1.4)
        g.setColorAt(0, C_KNOB_HI)
        g.setColorAt(1, C_KNOB_LO)
        p.setPen(QPen(QColor(15, 15, 17), 1.2))
        p.setBrush(g)
        p.drawEllipse(c, r - 2, r - 2)
        # pointer
        a = math.radians(START_DEG - SPAN_DEG * self.t)
        tip = QPointF(c.x() + math.cos(a) * (r - 6), c.y() - math.sin(a) * (r - 6))
        mid = QPointF(c.x() + math.cos(a) * (r * 0.25), c.y() - math.sin(a) * (r * 0.25))
        p.setPen(QPen(C_TEXT, 2.4, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(mid, tip)
        # value
        p.setPen(C_TEXT)
        p.drawText(QRectF(0, 16 + s - 2, w, 16), Qt.AlignCenter, self.param.fmt(self.value()))
