"""
pulsar_widgets - SCOPE-style controls for Pulsar Scope (Qt / PySide6).

Knob: dark round knob with an orange value arc, name above and value + unit below.
  drag up/down to turn (Shift = fine), mouse wheel, double-click = default value.
It works on a normalised position t (0..1) and a pulsar_values.Param that maps t to the displayed value.

Fader: SCOPE-style vertical channel fader (same Param interface as Knob; Shift = fine, double-click = default).

Meter: vertical level meter in dBFS (-60..0) with a peak-hold line.

Piano: on-screen keyboard (mouse, glissando by dragging, computer keys in two rows like a tracker), emits
note_on(note, velocity) / note_off(note).
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


class Fader(QWidget):
    """Vertical fader: name on top, track with a cap, value below; works on t (0..1) like Knob."""
    changed = Signal(float)

    def __init__(self, param, value=None, parent=None, height=170, label=None):
        super().__init__(parent)
        self.param, self.label = param, label if label is not None else param.name
        self.t = min(max(param.to_t(param.default if value is None else value), 0.0), 1.0)
        self._drag = None
        self.setFixedSize(56, height)
        self.setToolTip(param.name)
        self._emit = QTimer(self)
        self._emit.setSingleShot(True)
        self._emit.timeout.connect(lambda: self.changed.emit(self.value()))

    def value(self):
        return self.param.from_t(self.t)

    def set_value(self, v):
        self.t = min(max(self.param.to_t(v), 0.0), 1.0)
        self.update()

    def _set_t(self, t):
        t = min(max(t, 0.0), 1.0)
        if t != self.t:
            self.t = t
            self.update()
            self._emit.start(30)

    def _track(self):
        return QRectF(self.width() / 2 - 3, 18, 6, self.height() - 40)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            tr = self._track()
            cap_y = tr.bottom() - self.t * tr.height()
            if abs(e.position().y() - cap_y) > 10:       # click on the track: jump there
                self._set_t((tr.bottom() - e.position().y()) / tr.height())
            self._drag = (e.position().y(), self.t)

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            y0, t0 = self._drag
            scale = 0.1 if e.modifiers() & Qt.ShiftModifier else 1.0
            self._set_t(t0 + (y0 - e.position().y()) / self._track().height() * scale)

    def mouseReleaseEvent(self, e):
        self._drag = None

    def wheelEvent(self, e):
        step = 0.005 if e.modifiers() & Qt.ShiftModifier else 0.02
        self._set_t(self.t + step * (1 if e.angleDelta().y() > 0 else -1))

    def mouseDoubleClickEvent(self, e):
        self._set_t(self.param.to_t(self.param.default))

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        f = QFont()
        f.setPointSizeF(7.5)
        p.setFont(f)
        p.setPen(C_DIM)
        p.drawText(QRectF(0, 0, self.width(), 16), Qt.AlignCenter,
                   p.fontMetrics().elidedText(self.label, Qt.ElideRight, self.width()))
        tr = self._track()
        p.setPen(Qt.NoPen)
        p.setBrush(C_TRACK)
        p.drawRoundedRect(tr, 3, 3)
        p.setBrush(C_ARC)
        p.drawRoundedRect(QRectF(tr.left() + 1, tr.bottom() - self.t * tr.height(), tr.width() - 2,
                                 self.t * tr.height()), 2, 2)
        p.setPen(QPen(QColor(70, 70, 75), 1))                       # scale ticks
        for k in range(11):
            y = tr.top() + k * tr.height() / 10
            p.drawLine(QPointF(tr.left() - 8, y), QPointF(tr.left() - 3, y))
            p.drawLine(QPointF(tr.right() + 3, y), QPointF(tr.right() + 8, y))
        cy = tr.bottom() - self.t * tr.height()
        cap = QRectF(self.width() / 2 - 16, cy - 7, 32, 14)
        p.setPen(QPen(QColor(15, 15, 15), 1))
        p.setBrush(C_KNOB_HI)
        p.drawRoundedRect(cap, 3, 3)
        p.setPen(QPen(C_TEXT, 1.5))
        p.drawLine(QPointF(cap.left() + 4, cy), QPointF(cap.right() - 4, cy))
        p.setPen(C_TEXT)
        p.drawText(QRectF(0, self.height() - 18, self.width(), 16), Qt.AlignCenter, self.param.fmt(self.value()))


class Meter(QWidget):
    """Vertical dBFS meter: green below -12 dB, yellow up to -3 dB, red above; the peak line holds for ~1.5 s."""
    FLOOR = -60.0

    def __init__(self, parent=None, height=150, width=7):
        super().__init__(parent)
        self.db, self.peak, self.peak_age = None, None, 0
        self.setFixedSize(width, height)

    def set_db(self, db):
        self.db = db
        if db is not None and (self.peak is None or db >= self.peak or self.peak_age > 15):
            self.peak, self.peak_age = db, 0
        else:
            self.peak_age += 1
            if self.peak_age > 15 and self.peak is not None:
                self.peak = max(self.FLOOR, self.peak - 1.5)
        self.update()

    def _y(self, db, h):
        f = 0.0 if db is None else min(1.0, max(0.0, (db - self.FLOOR) / -self.FLOOR))
        return h - f * h

    def paintEvent(self, _):
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, C_TRACK)
        y = self._y(self.db, h)
        for lo, hi, col in ((self.FLOOR, -12, QColor(60, 180, 75)), (-12, -3, QColor(230, 200, 40)),
                            (-3, 0, QColor(220, 60, 50))):
            top, bot = max(y, self._y(hi, h)), self._y(lo, h)
            if bot > top:
                p.fillRect(QRectF(0, top, w, bot - top), col)
        if self.peak is not None and self.peak > self.FLOOR:
            py = self._y(self.peak, h)
            p.fillRect(QRectF(0, py - 1, w, 2), QColor(240, 240, 240))
        p.setPen(QPen(QColor(15, 15, 15), 1))
        for db in (-48, -36, -24, -12, -6, -3):
            yy = self._y(db, h)
            p.drawLine(QPointF(0, yy), QPointF(w, yy))


# ---------------------------------------------------------------- on-screen keyboard
C_WHITE = QColor(232, 232, 228)
C_BLACK = QColor(28, 28, 30)
C_DOWN = QColor(255, 160, 40)
_WHITE_STEPS = (0, 2, 4, 5, 7, 9, 11)
_BLACK_AFTER = {0: 1, 1: 3, 3: 6, 4: 8, 5: 10}           # white key index in the octave -> black note offset
# computer keys: lower row from the base note, upper row one octave up (tracker layout)
KEY_ROWS = (("Z", "S", "X", "D", "C", "V", "G", "B", "H", "N", "J", "M", ",", "L", ".", ";", "/"),
            ("Q", "2", "W", "3", "E", "R", "5", "T", "6", "Y", "7", "U", "I", "9", "O", "0", "P", "[", "=", "]"))


class Piano(QWidget):
    note_on = Signal(int, int)
    note_off = Signal(int)

    def __init__(self, parent=None, octaves=4, base=36):
        super().__init__(parent)
        self.octaves, self.base, self.velocity = octaves, base, 100
        self.down = set()                     # notes currently held (mouse or keys)
        self.mouse_note = None
        self.key_notes = {}                   # Qt key -> note
        self.setMinimumHeight(70)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(90)

    def set_base(self, base):
        self.all_off()
        self.base = max(0, min(127 - 12 * self.octaves, base))
        self.update()

    # geometry
    def _keys(self):
        """[(note, rect, black)] white keys first, then black keys (drawn on top)."""
        nw = 7 * self.octaves + 1
        w = self.width() / nw
        h = self.height() - 1
        whites, blacks = [], []
        for i in range(nw):
            o, k = divmod(i, 7)
            note = self.base + 12 * o + _WHITE_STEPS[k]
            whites.append((note, QRectF(i * w, 0, w, h), False))
            if k in _BLACK_AFTER and i < nw - 1:
                bn = self.base + 12 * o + _BLACK_AFTER[k]
                blacks.append((bn, QRectF((i + 1) * w - w * 0.3, 0, w * 0.6, h * 0.6), True))
        return whites, blacks

    def note_at(self, pos):
        whites, blacks = self._keys()
        for note, r, _ in blacks + whites:
            if r.contains(pos):
                return note
        return None

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        whites, blacks = self._keys()
        f = QFont()
        f.setPointSizeF(7)
        p.setFont(f)
        for note, r, _ in whites:
            p.setPen(QPen(QColor(90, 90, 90), 1))
            p.setBrush(C_DOWN if note in self.down else C_WHITE)
            p.drawRect(r)
            if note % 12 == 0:
                p.setPen(QColor(110, 110, 110))
                p.drawText(r.adjusted(0, 0, 0, -3), Qt.AlignBottom | Qt.AlignHCenter, "C%d" % (note // 12 - 1))
        for note, r, _ in blacks:
            p.setPen(QPen(QColor(10, 10, 10), 1))
            p.setBrush(C_DOWN.darker(130) if note in self.down else C_BLACK)
            p.drawRect(r)

    # notes
    def press(self, note):
        if note is None or note in self.down or not 0 <= note <= 127:
            return
        self.down.add(note)
        self.note_on.emit(note, self.velocity)
        self.update()

    def release(self, note):
        if note in self.down:
            self.down.discard(note)
            self.note_off.emit(note)
            self.update()

    def all_off(self):
        for n in list(self.down):
            self.release(n)
        self.key_notes.clear()
        self.mouse_note = None

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.mouse_note = self.note_at(e.position())
            self.press(self.mouse_note)

    def mouseMoveEvent(self, e):
        if self.mouse_note is None:
            return
        n = self.note_at(e.position())
        if n is not None and n != self.mouse_note:
            self.release(self.mouse_note)
            self.mouse_note = n
            self.press(n)

    def mouseReleaseEvent(self, e):
        if self.mouse_note is not None:
            self.release(self.mouse_note)
            self.mouse_note = None

    def _key_note(self, e):
        t = e.text().upper()
        for row, start in ((KEY_ROWS[0], self.base + 12), (KEY_ROWS[1], self.base + 24)):
            if t and t in row:
                return start + row.index(t)
        return None

    def keyPressEvent(self, e):
        if e.isAutoRepeat():
            return
        n = self._key_note(e)
        if n is None:
            return super().keyPressEvent(e)
        self.key_notes[e.key()] = n
        self.press(n)

    def keyReleaseEvent(self, e):
        if e.isAutoRepeat():
            return
        n = self.key_notes.pop(e.key(), None)
        if n is None:
            return super().keyReleaseEvent(e)
        self.release(n)

    def focusOutEvent(self, e):
        self.all_off()
        super().focusOutEvent(e)
