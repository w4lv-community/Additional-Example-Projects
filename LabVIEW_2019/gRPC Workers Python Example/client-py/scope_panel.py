"""Workers Scope - instrument front panel (PySide6).

The Python client of the Workers Scope example, styled like a real benchtop
oscilloscope: light instrument body, a dark scope screen with on-screen CH1 /
timebase / acquisition readouts, brand logo + model number, and physical-style
control keys down the side. Self-contained: the gRPC worker lives in this file;
the generated stubs (scope_pb2*.py) live in stubs/.

Run it directly (double-click, or `py scope_panel.py`); the server address is
set in the panel's CONNECTION field.
"""

import os
import sys
sys.dont_write_bytecode = True   # keep the folder clean: no __pycache__ from the stub imports

import queue
import threading
import time

import grpc
from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget,
)

# the generated stubs live in stubs/; anchor to this file so double-click works
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "stubs"))
import scope_pb2 as pb
import scope_pb2_grpc as pb_grpc

VMIN, VMAX = -5.5, 5.5  # fixed display scale in volts (full instrument range)

# --- brand identity ---------------------------------------------------------
COMPANY = "SCARFE CONTROLS"
MODEL = "SC-2400"
SERIES = "Data Acquisition Oscilloscope"

ACCENT = "#0e7c86"        # Scarfe Controls teal
ACCENT_DK = "#0a5a62"
RUN_GREEN = "#1faa59"
STOP_RED = "#d64545"
CH1 = "#f5c518"           # classic CH1 yellow (the trace)

HDIV, VDIV = 10, 8        # scope graticule divisions (10 wide x 8 tall)

STYLESHEET = f"""
QMainWindow, #panel {{ background: #e9ecf0; }}
QLabel {{ color: #1f2733; font-family: 'Segoe UI', system-ui, sans-serif; font-size: 13px;
          background: transparent; }}
QFrame#brandbar, QFrame#controls, QFrame#statusbar {{
    background: #ffffff; border: 1px solid #d4dae2; border-radius: 12px; }}
QFrame#bezel {{ background: #1b212a; border: 1px solid #11151b; border-radius: 12px; }}
QFrame#divider {{ background: #e3e7ec; max-height: 1px; min-height: 1px; border: none; }}

QLabel#brandName {{ font-size: 17px; font-weight: 800; color: #161b22; letter-spacing: 1px; }}
QLabel#model {{ font-size: 15px; font-weight: 700; color: {ACCENT}; }}
QLabel#series {{ font-size: 11px; color: #6b7682; }}
QLabel#bezelText {{ color: #8a93a0; font-size: 10px; font-weight: 700; font-family: Consolas, monospace; }}
QLabel#section {{ color: #8a93a0; font-size: 10px; font-weight: 800; }}
QLabel#panelLabel {{ color: #6b7682; font-size: 10px; font-weight: 700; }}
QLabel#statKey {{ color: #8a93a0; font-size: 10px; font-weight: 700; }}
QLabel#statVal {{ color: #1f2733; font-size: 14px; font-weight: 700;
                  font-family: Consolas, monospace; }}

QLineEdit {{ background: #ffffff; color: #1f2733; border: 1px solid #c2c9d2; border-radius: 8px;
             padding: 7px 10px; font-family: Consolas, monospace; font-size: 14px;
             selection-background-color: #bfe3e7; }}
QLineEdit:focus {{ border: 1px solid {ACCENT}; }}
QLineEdit:disabled {{ background: #eef1f4; color: #9aa3af; border-color: #dde2e8; }}

QPushButton {{ font-size: 13px; font-weight: 700; border-radius: 8px; padding: 9px 16px; }}
QPushButton#primary {{ background: {ACCENT}; color: #ffffff; border: 1px solid {ACCENT}; }}
QPushButton#primary:hover {{ background: {ACCENT_DK}; border-color: {ACCENT_DK}; }}
QPushButton#primary:disabled {{ background: #a9c9cd; border-color: #a9c9cd; color: #eef6f7; }}
QPushButton#ghost {{ background: #ffffff; color: #1f2733; border: 1px solid #c2c9d2; }}
QPushButton#ghost:hover {{ background: #f0f3f6; border-color: {ACCENT}; color: {ACCENT}; }}
QPushButton#ghost:disabled {{ color: #9aa3af; border-color: #dde2e8; }}
"""


def _fmt_per_div(t):
    if t <= 0:
        return "--"
    if t >= 1:
        return f"{t:.3g} s/DIV"
    if t >= 1e-3:
        return f"{t * 1e3:.3g} ms/DIV"
    if t >= 1e-6:
        return f"{t * 1e6:.3g} µs/DIV"
    return f"{t * 1e9:.3g} ns/DIV"


def _fmt_rate(r):
    if r >= 1e6:
        return f"{r / 1e6:.3g} MSa/s"
    if r >= 1e3:
        return f"{r / 1e3:.3g} kSa/s"
    return f"{r:.0f} Sa/s"


# --- gRPC worker --------------------------------------------------------------

class ScopeWorker(QObject):
    """Owns the connection. Runs a connect/read/reconnect loop on a background
    thread and emits signals that Qt delivers on the UI thread."""

    device_info = Signal(str, str)        # device, product_type
    waveform = Signal(list, int)          # y samples, acquisitions
    status = Signal(int)                  # State value
    conn_status = Signal(str, str)        # text, color name
    connected = Signal(bool)

    def __init__(self):
        super().__init__()
        self._want = False                # user wants to be connected
        self._address = ""
        self._thread = None
        self._outgoing = None             # queue.Queue of ToServer, or None
        self._call = None                 # active gRPC call, or None
        self._cfg_lock = threading.Lock()
        self._cfg = pb.Msg_ToServer_1(physical_channel="Dev1/ai0",   # update-config
                                      sample_rate_hz=1000, samples_per_read=200)
        self._last_state = pb.State.STOPPED

    # called from the UI thread
    def connect(self, address):
        self._address = address
        self._want = True
        self._last_state = pb.State.STOPPED  # fresh session: assume stopped until Status says otherwise
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def disconnect(self):
        self._want = False
        self._end_stream()

    def set_config(self, channel, rate, samples):
        with self._cfg_lock:
            self._cfg = pb.Msg_ToServer_1(physical_channel=channel,
                                          sample_rate_hz=rate, samples_per_read=samples)

    def send_configure(self):
        with self._cfg_lock:
            cfg = pb.Msg_ToServer_1(physical_channel=self._cfg.physical_channel,
                                    sample_rate_hz=self._cfg.sample_rate_hz,
                                    samples_per_read=self._cfg.samples_per_read)
        self._send(pb.ToServer(msg_1=cfg))                            # update-config

    def toggle_run(self):
        start = self._last_state != pb.State.RUNNING
        self._send(pb.ToServer(msg_2=pb.Msg_ToServer_2(run=start)))   # set-run

    # internals
    def _send(self, msg):
        q = self._outgoing
        if q is not None:
            q.put(msg)

    def _end_stream(self):
        if self._outgoing is not None:
            self._outgoing.put(None)      # sentinel ends the request generator
        if self._call is not None:
            try:
                self._call.cancel()
            except Exception:
                pass

    def _requests(self, q):
        while True:
            msg = q.get()
            if msg is None:               # sentinel: close the request stream
                return
            yield msg

    def _run(self):
        attempt = 0
        while self._want:
            attempt += 1
            if attempt == 1:
                self.conn_status.emit("Connecting...", "darkorange")
            self._outgoing = queue.Queue()
            was_connected = False
            channel = None
            try:
                channel = grpc.insecure_channel(self._address)
                # gRPC connects lazily: opening the stream "succeeds" even when no
                # server is listening, which would flash Connected/Disconnected on
                # every retry. Gate on the channel actually reaching READY before
                # claiming a connection.
                try:
                    grpc.channel_ready_future(channel).result(timeout=2.0)
                except grpc.FutureTimeoutError:
                    if self._want:
                        self.conn_status.emit(f"Server unreachable (attempt {attempt})", "red")
                    continue

                stub = pb_grpc.SessionStub(channel)
                call = stub.Connect(self._requests(self._outgoing))
                self._call = call
                was_connected = True
                attempt = 0
                self.connected.emit(True)
                self.conn_status.emit("Connected", "green")

                # send current Configure shortly after the stream opens (~150 ms)
                threading.Timer(0.15, self.send_configure).start()

                for msg in call:
                    kind = msg.WhichOneof("payload")
                    if kind == "msg_1":                               # device-info
                        self.device_info.emit(msg.msg_1.device, msg.msg_1.product_type)
                    elif kind == "msg_2":                             # waveform
                        self.waveform.emit(list(msg.msg_2.y), msg.msg_2.acquisitions)
                    elif kind == "msg_3":                             # status
                        self._last_state = msg.msg_3.state
                        self.status.emit(int(msg.msg_3.state))
            except grpc.RpcError as ex:
                if self._want:
                    self.conn_status.emit(f"Error: {ex.code().name}", "red")
            except Exception as ex:
                if self._want:
                    self.conn_status.emit(f"Error: {ex}", "red")
            finally:
                self._call = None
                self._outgoing = None
                # Run state is unknown once the stream ends; reset so the next
                # Run press after a reconnect sends run=true, not a stale stop.
                # The server's first Status re-syncs the real state either way.
                self._last_state = pb.State.STOPPED
                if was_connected:
                    self.connected.emit(False)
                if channel is not None:
                    try:
                        channel.close()
                    except Exception:
                        pass

            # Back off only after losing a real connection; the unreachable path
            # is already paced by the 2 s readiness timeout above.
            if self._want and was_connected:
                self.conn_status.emit("Reconnecting...", "darkorange")
                time.sleep(1.0)
        self.conn_status.emit("Disconnected", "gray")


# --- company logo mark ------------------------------------------------------

class LogoMark(QWidget):
    """A small teal badge with a stylized pulse - the Scarfe Controls mark."""

    def __init__(self, size=40):
        super().__init__()
        self.setFixedSize(size, size)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(r, 10, 10)
        p.fillPath(path, QColor(ACCENT))
        pen = QPen(QColor("#ffffff"), 2.3)
        pen.setJoinStyle(Qt.RoundJoin)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        w, h, x0, y0 = r.width(), r.height(), r.left(), r.top()
        mid = y0 + h * 0.5
        pts = [QPointF(x0 + w * 0.15, mid), QPointF(x0 + w * 0.35, mid),
               QPointF(x0 + w * 0.45, y0 + h * 0.26), QPointF(x0 + w * 0.57, y0 + h * 0.74),
               QPointF(x0 + w * 0.67, mid), QPointF(x0 + w * 0.86, mid)]
        p.drawPolyline(QPolygonF(pts))


# --- the scope screen -------------------------------------------------------

class ScreenWidget(QWidget):
    """The dark display: graticule, glowing CH1 trace, and on-screen readouts
    (CH1 V/div, timebase, sample rate, ACQ count, RUN/STOP) like a real scope."""

    def __init__(self):
        super().__init__()
        self._y = []
        self._rate = 1000.0
        self._samples = 200
        self._state = "stopped"
        self._acq = 0
        self.setMinimumSize(440, 300)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_y(self, y):
        self._y = y
        self.update()

    def set_config(self, rate, samples):
        self._rate, self._samples = rate, samples
        self.update()

    def set_status(self, state, acq):
        self._state, self._acq = state, acq
        self.update()

    def _y_to_px(self, v, top, h):
        return top + (1 - (v - VMIN) / (VMAX - VMIN)) * h

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        W, H = self.width(), self.height()

        # screen background (slightly inset from the bezel)
        screen = QRectF(6, 6, W - 12, H - 12)
        p.fillRect(self.rect(), QColor("#1b212a"))
        bg = QPainterPath()
        bg.addRoundedRect(screen, 6, 6)
        p.fillPath(bg, QColor("#0a0e14"))
        p.setClipPath(bg)

        x0, y0, w, h = screen.left(), screen.top(), screen.width(), screen.height()
        cx, cy = x0 + w / 2, y0 + h / 2  # 0 V is mid-screen on the symmetric scale

        # graticule
        p.setPen(QPen(QColor("#172230"), 1))
        for i in range(HDIV + 1):
            x = x0 + w * i / HDIV
            p.drawLine(int(x), int(y0), int(x), int(y0 + h))
        for j in range(VDIV + 1):
            y = y0 + h * j / VDIV
            p.drawLine(int(x0), int(y), int(x0 + w), int(y))

        # brighter centre cross + minor tick marks (the classic scope detail)
        p.setPen(QPen(QColor("#2b3a4d"), 1))
        p.drawLine(int(x0), int(cy), int(x0 + w), int(cy))
        p.drawLine(int(cx), int(y0), int(cx), int(y0 + h))
        for k in range(HDIV * 5 + 1):
            x = x0 + w * k / (HDIV * 5)
            p.drawLine(int(x), int(cy - 3), int(x), int(cy + 3))
        for k in range(VDIV * 5 + 1):
            y = y0 + h * k / (VDIV * 5)
            p.drawLine(int(cx - 3), int(y), int(cx + 3), int(y))

        # trace
        if len(self._y) >= 2:
            n = len(self._y)
            pts = [QPointF(x0 + i / (n - 1) * w, self._y_to_px(v, y0, h)) for i, v in enumerate(self._y)]
            poly = QPolygonF(pts)
            fill = QPainterPath(QPointF(pts[0].x(), cy))
            for pt in pts:
                fill.lineTo(pt)
            fill.lineTo(pts[-1].x(), cy)
            fill.closeSubpath()
            p.fillPath(fill, QBrush(QColor(245, 197, 24, 18)))
            pen = QPen(QColor(245, 197, 24, 60), 4.0)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.drawPolyline(poly)
            pen = QPen(QColor(CH1), 1.7)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.drawPolyline(poly)

        # on-screen readouts
        v_per_div = (VMAX - VMIN) / VDIV
        t_per_div = (self._samples / self._rate / HDIV) if self._rate > 0 else 0
        mono = QFont("Consolas", 9)
        mono_b = QFont("Consolas", 9, QFont.Bold)

        p.setFont(mono_b)
        p.setPen(QColor(CH1))
        p.drawText(int(x0 + 10), int(y0 + 20), f"CH1  {v_per_div:.3g} V/DIV")

        p.setFont(mono)
        p.setPen(QColor("#9fb0c4"))
        tb = f"{_fmt_per_div(t_per_div)}   {_fmt_rate(self._rate)}"
        p.drawText(int(x0 + w - 230), int(y0 + 20), 220, 14, Qt.AlignRight, tb)
        p.drawText(int(x0 + 10), int(y0 + h - 10), f"ACQ {self._acq:05d}")

        # RUN / STOP indicator, top-right corner
        label = {"running": "RUN", "error": "ERR", "stopped": "STOP"}.get(self._state, "STOP")
        color = {"running": RUN_GREEN, "error": STOP_RED, "stopped": "#8a93a0"}.get(self._state, "#8a93a0")
        badge = QRectF(x0 + w - 64, y0 + h - 26, 54, 18)
        bp = QPainterPath()
        bp.addRoundedRect(badge, 4, 4)
        p.fillPath(bp, QColor(color))
        p.setPen(QColor("#0a0e14"))
        p.setFont(mono_b)
        p.drawText(badge, Qt.AlignCenter, label)


# --- front panel window -----------------------------------------------------

def _panel_field(label, widget):
    box = QVBoxLayout()
    box.setContentsMargins(0, 0, 0, 0)
    box.setSpacing(4)
    lab = QLabel(label.upper())
    lab.setObjectName("panelLabel")
    box.addWidget(lab)
    box.addWidget(widget)
    w = QWidget()
    w.setLayout(box)
    return w


def _divider():
    d = QFrame()
    d.setObjectName("divider")
    return d


class FrontPanel(QMainWindow):
    def __init__(self, server):
        super().__init__()
        self.setWindowTitle(f"{COMPANY}  {MODEL}")
        self.resize(1000, 660)
        self._server = server
        self._last_state = "stopped"   # from Status messages; the screen readout
        self._last_acq = 0             # needs both this and acq (from Waveform)

        self.worker = ScopeWorker()
        self.worker.device_info.connect(self._on_device_info)
        self.worker.waveform.connect(self._on_waveform)
        self.worker.status.connect(self._on_status)
        self.worker.conn_status.connect(self._on_conn_status)
        self.worker.connected.connect(self._on_connected)

        panel = QWidget()
        panel.setObjectName("panel")
        self.setCentralWidget(panel)
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)

        outer.addWidget(self._build_brandbar())

        mid = QHBoxLayout()
        mid.setSpacing(12)
        mid.addWidget(self._build_bezel(), stretch=1)
        mid.addWidget(self._build_controls())
        outer.addLayout(mid, stretch=1)

        outer.addWidget(self._build_statusbar())

        self._set_connected_ui(False)
        self._set_led(self.conn_led, "#c4ccd6")

    # --- brand bar ---
    def _build_brandbar(self):
        bar = QFrame()
        bar.setObjectName("brandbar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(12)
        lay.addWidget(LogoMark(40))
        name_box = QVBoxLayout()
        name_box.setSpacing(0)
        name = QLabel(COMPANY)
        name.setObjectName("brandName")
        series = QLabel(SERIES)
        series.setObjectName("series")
        name_box.addWidget(name)
        name_box.addWidget(series)
        lay.addLayout(name_box)
        lay.addStretch()
        model = QLabel(MODEL)
        model.setObjectName("model")
        lay.addWidget(model)
        # power LED (instrument is powered while the app runs)
        self.pwr_led = QLabel()
        self._set_led(self.pwr_led, RUN_GREEN)
        lay.addWidget(self.pwr_led)
        pwr = QLabel("PWR")
        pwr.setObjectName("statKey")
        lay.addWidget(pwr)
        return bar

    # --- screen bezel ---
    def _build_bezel(self):
        bezel = QFrame()
        bezel.setObjectName("bezel")
        lay = QVBoxLayout(bezel)
        lay.setContentsMargins(12, 12, 12, 8)
        lay.setSpacing(8)
        self.screen = ScreenWidget()
        lay.addWidget(self.screen, stretch=1)
        strip = QHBoxLayout()
        l = QLabel(COMPANY)
        l.setObjectName("bezelText")
        r = QLabel(f"{MODEL}  ·  ANALOG IN")
        r.setObjectName("bezelText")
        strip.addWidget(l)
        strip.addStretch()
        strip.addWidget(r)
        lay.addLayout(strip)
        return bezel

    # --- control column ---
    def _build_controls(self):
        ctl = QFrame()
        ctl.setObjectName("controls")
        ctl.setFixedWidth(250)
        lay = QVBoxLayout(ctl)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(10)

        lay.addWidget(self._section("ACQUISITION"))
        self.run_btn = QPushButton("RUN")
        self.run_btn.setMinimumHeight(54)
        self.run_btn.clicked.connect(self.worker.toggle_run)
        lay.addWidget(self.run_btn)

        lay.addWidget(_divider())
        lay.addWidget(self._section("VERTICAL  /  HORIZONTAL"))
        self.channel_box = QLineEdit("Dev1/ai0")
        self.rate_box = QLineEdit("1000")
        self.samples_box = QLineEdit("200")
        lay.addWidget(_panel_field("Channel", self.channel_box))
        rs = QHBoxLayout()
        rs.setSpacing(8)
        rs.addWidget(_panel_field("Rate (Hz)", self.rate_box))
        rs.addWidget(_panel_field("Samples", self.samples_box))
        lay.addLayout(rs)
        self.apply_btn = QPushButton("APPLY")
        self.apply_btn.setObjectName("ghost")
        self.apply_btn.clicked.connect(self._on_apply)
        lay.addWidget(self.apply_btn)

        lay.addWidget(_divider())
        lay.addWidget(self._section("CONNECTION"))
        self.server_box = QLineEdit(self._server)
        lay.addWidget(_panel_field("Server", self.server_box))
        self.connect_btn = QPushButton("CONNECT")
        self.connect_btn.setObjectName("primary")
        self.connect_btn.clicked.connect(self._on_connect_clicked)
        lay.addWidget(self.connect_btn)

        lay.addStretch()
        return ctl

    # --- status bar ---
    def _build_statusbar(self):
        bar = QFrame()
        bar.setObjectName("statusbar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(16, 10, 16, 10)
        lay.setSpacing(10)
        self.state_val = self._readout(lay, "STATE", "STOPPED")
        lay.addSpacing(20)
        self.device_val = self._readout(lay, "DEVICE", "—")
        lay.addSpacing(20)
        self.acq_val = self._readout(lay, "ACQ", "0")
        lay.addStretch()
        self.conn_led = QLabel()
        lay.addWidget(self.conn_led)
        self.conn_text = QLabel("OFFLINE")
        self.conn_text.setObjectName("statVal")
        lay.addWidget(self.conn_text)
        return bar

    def _section(self, text):
        l = QLabel(text)
        l.setObjectName("section")
        return l

    def _readout(self, layout, key, value):
        box = QVBoxLayout()
        box.setSpacing(1)
        k = QLabel(key)
        k.setObjectName("statKey")
        v = QLabel(value)
        v.setObjectName("statVal")
        box.addWidget(k)
        box.addWidget(v)
        holder = QWidget()
        holder.setLayout(box)
        layout.addWidget(holder)
        return v

    @staticmethod
    def _set_led(label, color):
        label.setText("●")
        label.setStyleSheet(f"color:{color}; font-size:14px;")

    def _style_run(self, running, enabled):
        if not enabled:
            bg, txt = "#cdd3da", "RUN"
        elif running:
            bg, txt = STOP_RED, "■  STOP"
        else:
            bg, txt = RUN_GREEN, "▶  RUN"
        self.run_btn.setText(txt)
        self.run_btn.setStyleSheet(
            f"background:{bg}; color:white; border:none; border-radius:8px; "
            f"font-weight:800; font-size:15px;")

    # --- button handlers ---
    def _on_connect_clicked(self):
        if self.connect_btn.text() == "CONNECT":
            self._push_config()
            self.connect_btn.setText("DISCONNECT")
            self.connect_btn.setObjectName("ghost")
            self._restyle(self.connect_btn)
            self.worker.connect(self.server_box.text().strip())
        else:
            self.connect_btn.setText("CONNECT")
            self.connect_btn.setObjectName("primary")
            self._restyle(self.connect_btn)
            self.worker.disconnect()

    def _on_apply(self):
        self._push_config()
        self.worker.send_configure()

    def _push_config(self):
        rate = self._to_float(self.rate_box.text())
        samples = self._to_int(self.samples_box.text())
        self.worker.set_config(self.channel_box.text(), rate, samples)
        self.screen.set_config(rate, samples)

    # --- worker signals ---
    def _on_device_info(self, device, product):
        self.device_val.setText(f"{device} · {product}")

    def _on_waveform(self, y, acquisitions):
        # the acquisition count rides with each waveform block
        self._last_acq = acquisitions
        self.acq_val.setText(str(acquisitions))
        self.screen.set_y(y)
        self.screen.set_status(self._last_state, acquisitions)

    def _on_status(self, state):
        label = {pb.State.RUNNING: "running", pb.State.ERROR: "error"}.get(state, "stopped")
        self._last_state = label
        self.state_val.setText(label.upper())
        self.screen.set_status(label, self._last_acq)
        self._style_run(state == pb.State.RUNNING, self.run_btn.isEnabled())

    def _on_conn_status(self, text, color):
        led = {"green": RUN_GREEN, "red": STOP_RED}.get(color, "#c4ccd6")
        self._set_led(self.conn_led, led)
        self.conn_text.setText(text.upper())

    def _on_connected(self, connected):
        self._set_connected_ui(connected)

    def _set_connected_ui(self, connected):
        for w in (self.channel_box, self.rate_box, self.samples_box, self.apply_btn, self.run_btn):
            w.setEnabled(connected)
        if not connected:
            self._last_state, self._last_acq = "stopped", 0
            self.state_val.setText("STOPPED")
            self.acq_val.setText("0")
            self.screen.set_status("stopped", 0)
        self._style_run(False, connected)

    @staticmethod
    def _restyle(w):
        w.style().unpolish(w)
        w.style().polish(w)

    def closeEvent(self, e):
        self.worker.disconnect()
        super().closeEvent(e)

    @staticmethod
    def _to_float(s):
        try:
            return float(s)
        except ValueError:
            return 0.0

    @staticmethod
    def _to_int(s):
        try:
            return int(s)
        except ValueError:
            return 0


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLESHEET)
    win = FrontPanel("localhost:50070")   # editable in the panel's CONNECTION field
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
