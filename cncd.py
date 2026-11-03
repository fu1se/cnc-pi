#!/usr/bin/env python3
"""cncd — веб-мост между Raspberry Pi и ЧПУ на GRBL 1.1.

Держит один последовательный порт, стримит G-код по протоколу подсчёта
символов (character-counting), раздаёт состояние станка по WebSocket
и обслуживает веб-интерфейс из static/.

  python3 cncd.py --port /dev/ttyUSB0 --baud 115200
  python3 cncd.py --simulate          # без станка, встроенный эмулятор GRBL
"""

import argparse
import asyncio
import contextlib
import glob
import json
import os
import re
import shutil
import time
from collections import deque
from pathlib import Path

from aiohttp import WSMsgType, web

BASE = Path(__file__).resolve().parent
GCODE_DIR = BASE / "gcode"
STATIC_DIR = BASE / "static"

RX_BUFFER = 127          # приёмный буфер GRBL 1.1 — 128 байт, один держим в запасе
STATUS_INTERVAL = 0.2    # как часто дёргаем '?'
MAX_LOG = 400            # строк консоли храним для новых клиентов

# Управляющие байты реального времени GRBL 1.1
RT = {
    "reset": b"\x18",
    "hold": b"!",
    "resume": b"~",
    "status": b"?",
    "jog_cancel": b"\x85",
    "feed_100": b"\x90", "feed_p10": b"\x91", "feed_m10": b"\x92",
    "feed_p1": b"\x93", "feed_m1": b"\x94",
    "rapid_100": b"\x95", "rapid_50": b"\x96", "rapid_25": b"\x97",
    "spindle_100": b"\x99", "spindle_p10": b"\x9a", "spindle_m10": b"\x9b",
    "spindle_stop": b"\x9e",
    "flood": b"\xa0", "mist": b"\xa1",
}

ERRORS = {
    1: "Буква команды G-кода не распознана",
    2: "Плохое число в команде G-кода",
    3: "Неверная команда '$'",
    4: "Отрицательное значение там, где нужно положительное",
    5: "Концевики отключены ($22=0), homing невозможен",
    8: "Команда '$' доступна только в состоянии Idle",
    9: "G-код заблокирован: станок в Alarm или Hold",
    10: "Soft limits требуют включённого homing ($22=1)",
    11: "Строка длиннее 80 символов",
    15: "Перемещение jog выходит за программные пределы",
    16: "Неверная команда $J= (нет осей или лишние слова)",
    17: "Лазерный режим требует ШИМ-шпинделя",
    20: "Нераспознанная или неподдерживаемая команда",
    21: "Две G-команды из одной модальной группы",
    22: "Не задана скорость подачи F",
    23: "Команда требует целого числа",
    24: "Две команды, требующие осевых слов, в одной строке",
    25: "Слово повторено дважды",
    31: "Заданы осевые слова там, где они не нужны",
    33: "Недопустимая цель движения (радиус/дуга)",
    34: "Дуга с ошибкой геометрии: радиус недостижим",
    38: "Номер инструмента вне диапазона",
}

ALARMS = {
    1: "Сработал концевик — позиция потеряна. Нужен $X и повторный homing",
    2: "Цель за программными пределами (soft limit)",
    3: "Аварийный сброс во время движения — позиция потеряна",
    4: "Ошибка щупа: контакт уже замкнут",
    5: "Ошибка щупа: контакт не сработал",
    6: "Homing прерван сбросом",
    7: "Homing: дверь открыта",
    8: "Homing: концевик не освободился при отходе",
    9: "Homing: концевик не найден в пределах хода оси",
}

STATUS_RE = re.compile(r"^<([^|>]+)\|?(.*)>$")


def clean_gcode(raw: str) -> str:
    """Убирает комментарии и пробелы — экономит место в буфере GRBL."""
    out = []
    depth = 0
    for ch in raw:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            if ch == ";":
                break
            if not ch.isspace():
                out.append(ch)
    return "".join(out)


class SimSerial:
    """Мини-эмулятор GRBL: отвечает ok, ведёт позицию, шлёт статусы."""

    def __init__(self):
        self._r, self._w = os.pipe()
        os.set_blocking(self._r, False)
        self.pos = [0.0, 0.0, 0.0]
        self.wco = [0.0, 0.0, 0.0]
        self.state = "Idle"
        self.feed = 0.0
        self.spindle = 0
        self.queue = deque()
        self.target = None
        self.rate = 0.0
        self.hold = False
        self.alarm = 0
        self._emit("Grbl 1.1f ['$' for help]")
        self._task = None

    # --- интерфейс, ожидаемый контроллером ---
    def fileno(self):
        return self._r

    def read(self, n):
        return os.read(self._r, n)

    def close(self):
        if self._task:
            self._task.cancel()
        os.close(self._r)
        os.close(self._w)

    def start(self, loop):
        self._task = loop.create_task(self._run())

    def _emit(self, line):
        os.write(self._w, (line + "\r\n").encode())

    def write(self, data: bytes):
        # реальное время обрабатываем побайтно, остальное — построчно
        buf = b""
        for byte in data:
            ch = bytes([byte])
            if ch in (b"?", b"!", b"~", b"\x18", b"\x85") or byte >= 0x90:
                self._realtime(ch)
            elif ch == b"\n":
                self._line(buf.decode(errors="replace").strip())
                buf = b""
            elif ch != b"\r":
                buf += ch
        if buf:
            self._line(buf.decode(errors="replace").strip())

    def _realtime(self, ch):
        if ch == b"?":
            wpos = [p - o for p, o in zip(self.pos, self.wco)]
            self._emit(
                "<{}|MPos:{:.3f},{:.3f},{:.3f}|Bf:15,128|FS:{:.0f},{}|WCO:{:.3f},{:.3f},{:.3f}>".format(
                    "Alarm" if self.alarm else self.state, *self.pos,
                    self.feed, self.spindle, *self.wco))
        elif ch == b"!":
            self.hold = True
            self.state = "Hold:0"
        elif ch == b"~":
            self.hold = False
            self.state = "Run" if (self.target or self.queue) else "Idle"
        elif ch == b"\x18":
            self.queue.clear()
            self.target = None
            self.hold = False
            self.alarm = 0
            self.state = "Idle"
            self._emit("Grbl 1.1f ['$' for help]")
        elif ch == b"\x85":
            self.target = None
            self.queue.clear()

    def _line(self, line):
        if not line:
            return
        if line.startswith("$$"):
            for k, v in (("$0", "10"), ("$30", "1000"), ("$32", "1"),
                         ("$100", "800.000"), ("$110", "3000.000"), ("$120", "20.000")):
                self._emit(f"{k}={v}")
            self._emit("ok")
        elif line in ("$X", "$I", "$G", "$#"):
            self.alarm = 0
            self._emit("ok")
        elif line == "$H":
            self.pos = [0.0, 0.0, 0.0]
            self._emit("ok")
        else:
            self.queue.append(line)

    async def _run(self):
        dt = 0.05
        while True:
            await asyncio.sleep(dt)
            if self.hold:
                continue
            if self.target is None:
                if not self.queue:
                    if self.state != "Idle":
                        self.state = "Idle"
                        self.feed = 0
                    continue
                self._begin(self.queue.popleft())
                continue
            # линейная интерполяция к цели
            d = [t - p for t, p in zip(self.target, self.pos)]
            dist = sum(x * x for x in d) ** 0.5
            step = self.rate / 60.0 * dt
            if dist <= step or dist < 1e-6:
                self.pos = list(self.target)
                self.target = None
                self._emit("ok")
            else:
                self.pos = [p + x / dist * step for p, x in zip(self.pos, d)]

    def _begin(self, line):
        up = line.upper()
        if up.startswith("G10"):
            m = dict(re.findall(r"([XYZ])(-?\d*\.?\d+)", up))
            for i, ax in enumerate("XYZ"):
                if ax in m:
                    self.wco[i] = self.pos[i] - float(m[ax])
            self._emit("ok")
            return
        rel = "G91" in up or up.startswith("$J=") and "G91" in up
        words = dict(re.findall(r"([XYZF])(-?\d*\.?\d+)", up.split("=", 1)[-1] if up.startswith("$J=") else up))
        if "F" in words:
            self.rate = float(words["F"])
        if "G0" in up and "F" not in words:
            self.rate = 3000.0
        m = re.search(r"S(\d+)", up)
        if m:
            self.spindle = int(m.group(1))
        if "M5" in up:
            self.spindle = 0
        tgt = list(self.pos)
        moved = False
        for i, ax in enumerate("XYZ"):
            if ax in words:
                v = float(words[ax])
                tgt[i] = self.pos[i] + v if rel else self.wco[i] + v
                moved = True
        if not moved or self.rate <= 0:
            self._emit("ok")
            return
        self.target = tgt
        self.feed = self.rate
        self.state = "Jog" if up.startswith("$J=") else "Run"


class Grbl:
    """Контроллер станка: порт, очередь, стриминг задания, состояние."""

    def __init__(self, port, baud, simulate=False):
        self.port_name = port
        self.baud = baud
        self.simulate = simulate
        self.ser = None
        self.loop = None

        self.pending = deque()      # (длина, № строки) отправленного, но не подтверждённого
        self.buf_used = 0
        self.mdi = deque()          # ручные команды — идут вперёд задания
        self._held = None
        self._held_line = None
        self._space = asyncio.Event()
        self._rx = b""

        self.state = {
            "connected": False, "port": port, "state": "—",
            "mpos": [0.0, 0.0, 0.0], "wpos": [0.0, 0.0, 0.0], "wco": [0.0, 0.0, 0.0],
            "feed": 0, "spindle": 0, "ov": [100, 100, 100], "pins": "",
            "buf": [0, 0], "version": "", "message": "",
        }
        self.job = {
            "name": None, "running": False, "paused": False,
            "line": 0, "done": 0, "total": 0, "sent": 0, "started": 0.0, "elapsed": 0.0,
        }
        self._line_no = None         # номер строки файла для команды, уходящей в буфер
        self._job_iter = None
        self._job_fh = None
        self._job_done_pending = False

        self.clients = set()
        self.log = deque(maxlen=MAX_LOG)
        self.settings = {}

    # ---------- соединение ----------
    async def connect(self):
        # Веб-интерфейс должен подниматься всегда — даже без станка.
        # Поэтому connect() никогда не бросает исключение: для реального
        # железа подключение и переподключение ведёт фоновый супервизор.
        self.loop = asyncio.get_running_loop()
        if self.simulate:
            self.ser = SimSerial()
            self.ser.start(self.loop)
            self._attach()
        else:
            self._supervisor_task = self.loop.create_task(self._supervise())

    def _attach(self):
        """Порт открыт (self.ser готов) — навесить чтение и запустить задачи."""
        self._space.set()
        self.loop.add_reader(self.ser.fileno(), self._on_readable)
        self.state["connected"] = True
        self._tasks = [
            self.loop.create_task(self._pump()),
            self.loop.create_task(self._poll()),
        ]
        self.emit_log("sys", f"порт открыт: {self.state['port']}")

    def _detach(self):
        """Снять чтение, остановить задачи, закрыть порт."""
        for t in getattr(self, "_tasks", []):
            t.cancel()
        self._tasks = []
        if self.ser is not None:
            with contextlib.suppress(Exception):
                self.loop.remove_reader(self.ser.fileno())
            with contextlib.suppress(Exception):
                self.ser.close()
        self.ser = None
        self.state["connected"] = False

    def _reset_link(self):
        """Сброс буфера стриминга под новое соединение; активное задание обрывается."""
        self.pending.clear()
        self.buf_used = 0
        self._held = None
        self._held_line = None
        self._line_no = None
        self._rx = b""
        self.mdi.clear()
        self._space.set()
        if self.job["running"]:
            self.job["running"] = False
            self.job["paused"] = False
            self._job_iter = None
            if self._job_fh:
                with contextlib.suppress(Exception):
                    self._job_fh.close()
                self._job_fh = None
            self.emit_log("err", "соединение прервано — задание остановлено")

    async def _supervise(self):
        """Открывает порт станка и переподключается при обрыве. Не роняет сервер."""
        import serial
        warned = False
        while True:
            if self.port_name and os.path.exists(self.port_name):
                port = self.port_name
            else:
                port = autodetect_port()
            if not port:
                if not warned:
                    self.emit_log("sys", "жду станок (нет /dev/ttyUSB* и /dev/ttyACM*)…")
                    warned = True
                self.state["port"] = ""
                await asyncio.sleep(2.0)
                continue
            warned = False
            try:
                self.ser = serial.Serial(port, self.baud, timeout=0)
            except Exception as e:
                self.emit_log("err", f"не удалось открыть {port}: {e}")
                self.ser = None
                await asyncio.sleep(2.0)
                continue
            self.state["port"] = port
            self._reset_link()
            self._attach()
            while self.state["connected"]:
                await asyncio.sleep(0.5)
            self._detach()
            self.emit_log("sys", "станок отключён — переподключение…")
            await asyncio.sleep(1.5)

    async def close(self):
        t = getattr(self, "_supervisor_task", None)
        if t:
            t.cancel()
        self._detach()

    # ---------- приём ----------
    def _on_readable(self):
        try:
            data = self.ser.read(4096)
        except (OSError, BlockingIOError):
            return
        if not data:
            return
        self._rx += data
        while b"\n" in self._rx:
            raw, self._rx = self._rx.split(b"\n", 1)
            line = raw.decode(errors="replace").strip()
            if line:
                self._handle(line)

    def _handle(self, line):
        if line.startswith("<") and line.endswith(">"):
            self._parse_status(line)
            return

        if line == "ok" or line.startswith("error:"):
            if self.pending:
                used, lineno = self.pending.popleft()
                self.buf_used -= used
                if lineno is not None:
                    self.job["done"] = lineno
                self._space.set()
            if line.startswith("error:"):
                code = int(line.split(":")[1]) if line.split(":")[1].isdigit() else 0
                self.emit_log("err", f"{line} — {ERRORS.get(code, 'см. документацию GRBL')}")
                if self.job["running"]:
                    self.emit_log("sys", "задание остановлено из-за ошибки")
                    self._abort_job()
            self._check_job_done()
            return

        if line.startswith("ALARM:"):
            code = int(line.split(":")[1]) if line.split(":")[1].isdigit() else 0
            reason = ALARMS.get(code, "аварийная остановка")
            self.state["message"] = f"ALARM:{code} — {reason}"
            self.emit_log("err", f"{line} — {reason}")
            self._abort_job()
        elif line.startswith("Grbl "):
            self.state["version"] = line
            self.pending.clear()
            self.buf_used = 0
            self._held = None
            self._space.set()
            self.emit_log("rx", line)
        elif line.startswith("[MSG:"):
            self.state["message"] = line[5:-1]
            self.emit_log("rx", line)
        elif re.match(r"^\$\d+=", line):
            k, v = line[1:].split("=", 1)
            self.settings[k] = v.split(" ")[0]
            self.emit_log("rx", line)
        else:
            self.emit_log("rx", line)

    def _parse_status(self, line):
        m = STATUS_RE.match(line)
        if not m:
            return
        st = self.state
        st["state"] = m.group(1)
        st["pins"] = ""
        for field in m.group(2).split("|"):
            if not field:
                continue
            key, _, val = field.partition(":")
            nums = [float(x) for x in val.split(",")] if val and key in (
                "MPos", "WPos", "WCO", "FS", "Bf", "Ov") else None
            if key == "MPos":
                st["mpos"] = nums
            elif key == "WPos":
                st["wpos"] = nums
                st["mpos"] = [w + o for w, o in zip(nums, st["wco"])]
            elif key == "WCO":
                st["wco"] = nums
            elif key == "FS":
                st["feed"], st["spindle"] = nums[0], nums[1]
            elif key == "F":
                st["feed"] = float(val)
            elif key == "Bf":
                st["buf"] = [int(nums[0]), int(nums[1])]
            elif key == "Ov":
                st["ov"] = [int(x) for x in nums]
            elif key == "Pn":
                st["pins"] = val
        if st.get("mpos") and st.get("wco"):
            st["wpos"] = [p - o for p, o in zip(st["mpos"], st["wco"])]

    # ---------- передача ----------
    def _take_next(self):
        if self.mdi:
            self._line_no = None
            return self.mdi.popleft()
        if self.job["running"] and not self.job["paused"] and self._job_iter:
            for raw in self._job_iter:
                self.job["line"] += 1
                code = clean_gcode(raw)
                if code:
                    self.job["sent"] += 1
                    self._line_no = self.job["line"]
                    return code
            self._job_iter = None
            self._job_done_pending = True
        return None

    async def _pump(self):
        while True:
            if self._held is None:
                self._held = self._take_next()
                self._held_line = self._line_no
            if self._held is None:
                self._check_job_done()
                await asyncio.sleep(0.02)
                continue
            need = len(self._held) + 1
            if self.buf_used + need > RX_BUFFER:
                self._space.clear()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._space.wait(), 2.0)
                continue
            line, self._held = self._held, None
            self.pending.append((need, self._held_line))
            self.buf_used += need
            try:
                self.ser.write((line + "\n").encode())
            except Exception as e:  # порт отвалился
                self.emit_log("err", f"ошибка записи в порт: {e}")
                self.state["connected"] = False
                return
            self.emit_log("tx", line)

    async def _poll(self):
        while True:
            try:
                self.ser.write(RT["status"])
            except Exception:
                self.state["connected"] = False  # порт отвалился — сигнал супервизору
                return
            if self.job["running"] and not self.job["paused"]:
                self.job["elapsed"] = time.time() - self.job["started"]
            await self.broadcast_state()
            await asyncio.sleep(STATUS_INTERVAL)

    # ---------- публичные команды ----------
    def send(self, line: str):
        line = line.strip()
        if line:
            self.mdi.append(line)

    def realtime(self, name: str):
        b = RT.get(name)
        if not b:
            return False
        self.ser.write(b)
        self.emit_log("tx", f"<{name}>")
        if name == "reset":
            self.pending.clear()
            self.buf_used = 0
            self._held = None
            self.mdi.clear()
            self._abort_job()
            self._space.set()
        return True

    def jog(self, axis_values: dict, feed: float, relative=True):
        parts = "".join(f"{ax}{v:.4f}" for ax, v in axis_values.items())
        self.send(f"$J=G21{'G91' if relative else 'G90'}{parts}F{feed:.0f}")

    # ---------- задание ----------
    def start_job(self, name: str):
        path = safe_gcode_path(name)
        if self.job["running"]:
            raise RuntimeError("задание уже выполняется")
        total = 0
        with open(path, "r", errors="replace") as fh:
            for _ in fh:
                total += 1
        self._job_fh = open(path, "r", errors="replace")
        self._job_iter = iter(self._job_fh)
        self.job.update(name=name, running=True, paused=False, line=0, done=0,
                        total=total, sent=0, started=time.time(), elapsed=0.0)
        self._job_done_pending = False
        self.emit_log("sys", f"старт задания: {name} ({total} строк)")

    def pause_job(self):
        if self.job["running"] and not self.job["paused"]:
            self.job["paused"] = True
            self.realtime("hold")

    def resume_job(self):
        if self.job["running"] and self.job["paused"]:
            self.job["paused"] = False
            self.job["started"] = time.time() - self.job["elapsed"]
            self.realtime("resume")

    def _abort_job(self):
        if self._job_fh:
            with contextlib.suppress(Exception):
                self._job_fh.close()
        self._job_fh = None
        self._job_iter = None
        self._job_done_pending = False
        if self.job["running"]:
            self.job.update(running=False, paused=False)

    def stop_job(self):
        if not self.job["running"]:
            return
        self.emit_log("sys", "задание прервано пользователем")
        self._abort_job()
        self.mdi.clear()
        self._held = None
        self.realtime("hold")
        self.loop.call_later(0.3, lambda: self.realtime("reset"))

    def _check_job_done(self):
        if self._job_done_pending and not self.pending and self._held is None and not self.mdi:
            self._job_done_pending = False
            elapsed = time.time() - self.job["started"]
            self.emit_log("sys", f"задание завершено за {fmt_dur(elapsed)}")
            self._abort_job()
            self.job["elapsed"] = elapsed

    # ---------- вещание ----------
    def emit_log(self, direction, line):
        entry = {"t": "log", "dir": direction, "line": line, "ts": time.time()}
        self.log.append(entry)
        self._push(entry)

    def _push(self, obj):
        data = json.dumps(obj, ensure_ascii=False)
        for ws in list(self.clients):
            if not ws.closed:
                asyncio.ensure_future(ws.send_str(data))

    async def broadcast_state(self):
        if self.clients:
            self._push({"t": "state", "s": self.state, "job": self.job})


def fmt_dur(sec):
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def autodetect_port():
    for pattern in ("/dev/ttyUSB*", "/dev/ttyACM*", "/dev/serial/by-id/*"):
        found = sorted(glob.glob(pattern))
        if found:
            return found[0]
    return None


def safe_gcode_path(name: str) -> Path:
    p = (GCODE_DIR / os.path.basename(name)).resolve()
    if p.parent != GCODE_DIR.resolve():
        raise web.HTTPBadRequest(text="плохое имя файла")
    if not p.exists():
        raise web.HTTPNotFound(text="файл не найден")
    return p


# ============================ HTTP API ============================

def api(handler):
    async def wrapper(request):
        grbl = request.app["grbl"]
        try:
            body = await request.json() if request.can_read_body else {}
        except json.JSONDecodeError:
            body = {}
        try:
            result = handler(grbl, body, request)
            if asyncio.iscoroutine(result):
                result = await result
        except web.HTTPException:
            raise
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        return web.json_response({"ok": True, **(result or {})})
    return wrapper


@api
def h_command(g, body, req):
    line = (body.get("line") or "").strip()
    if not line:
        raise ValueError("пустая команда")
    g.send(line)


@api
def h_realtime(g, body, req):
    if not g.realtime(body.get("name", "")):
        raise ValueError("неизвестная realtime-команда")


@api
def h_jog(g, body, req):
    axes = {k.upper(): float(v) for k, v in (body.get("axes") or {}).items()
            if k.upper() in "XYZ"}
    if not axes:
        raise ValueError("не заданы оси")
    g.jog(axes, float(body.get("feed", 1000)), bool(body.get("relative", True)))


@api
def h_zero(g, body, req):
    axes = body.get("axes") or ["X", "Y", "Z"]
    g.send("G10L20P0" + "".join(f"{a.upper()}0" for a in axes))


@api
def h_job(g, body, req):
    action = body.get("action")
    if action == "start":
        g.start_job(body["name"])
    elif action == "pause":
        g.pause_job()
    elif action == "resume":
        g.resume_job()
    elif action == "stop":
        g.stop_job()
    else:
        raise ValueError("неизвестное действие")


@api
def h_status(g, body, req):
    """Состояние без WebSocket — то же, что в сообщениях {t:"state"}: для клиентов вроде плагина Filum."""
    return {"s": g.state, "job": g.job}


@api
def h_settings(g, body, req):
    if req.method == "POST":
        for k, v in (body.get("values") or {}).items():
            g.send(f"${int(k)}={v}")
    elif not req.query.get("read"):
        g.send("$$")          # ?read=1 — только отдать уже прочитанное
    return {"settings": g.settings}


async def h_files(request):
    files = []
    for p in sorted(GCODE_DIR.glob("*")):
        if p.is_file():
            st = p.stat()
            files.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime})
    return web.json_response({"ok": True, "files": files})


async def h_upload(request):
    reader = await request.multipart()
    saved = []
    while True:
        field = await reader.next()
        if field is None:
            break
        if field.name != "file" or not field.filename:
            continue
        name = os.path.basename(field.filename)
        if not re.match(r"^[\w .\-()\[\]]+$", name):
            name = re.sub(r"[^\w.\-]", "_", name)
        dest = GCODE_DIR / name
        size = 0
        with open(dest, "wb") as fh:
            while chunk := await field.read_chunk(65536):
                size += len(chunk)
                fh.write(chunk)
        saved.append({"name": name, "size": size})
    return web.json_response({"ok": True, "files": saved})


async def h_file_raw(request):
    path = safe_gcode_path(request.match_info["name"])
    return web.FileResponse(path, headers={"Content-Type": "text/plain; charset=utf-8"})


async def h_file_delete(request):
    path = safe_gcode_path(request.match_info["name"])
    g = request.app["grbl"]
    if g.job["running"] and g.job["name"] == path.name:
        return web.json_response({"ok": False, "error": "файл выполняется"}, status=400)
    path.unlink()
    return web.json_response({"ok": True})


async def h_ws(request):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    g = request.app["grbl"]
    g.clients.add(ws)
    await ws.send_str(json.dumps({"t": "hello", "s": g.state, "job": g.job,
                                 "log": list(g.log), "settings": g.settings},
                                ensure_ascii=False))
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if data.get("t") == "cmd":
                    g.send(data.get("line", ""))
                elif data.get("t") == "rt":
                    g.realtime(data.get("name", ""))
                elif data.get("t") == "jog":
                    axes = {k.upper(): float(v) for k, v in (data.get("axes") or {}).items()}
                    if axes:
                        g.jog(axes, float(data.get("feed", 1000)))
    finally:
        g.clients.discard(ws)
    return ws


async def h_index(request):
    return web.FileResponse(STATIC_DIR / "index.html")


@web.middleware
async def auth_mw(request, handler):
    token = request.app["token"]
    if not token:
        return await handler(request)
    given = (request.query.get("token") or request.cookies.get("cnc_token")
             or request.headers.get("X-Token"))
    if given != token:
        return web.Response(status=401, text="нужен ?token=...")
    resp = await handler(request)
    with contextlib.suppress(Exception):
        resp.set_cookie("cnc_token", token, max_age=30 * 86400, samesite="Lax")
    return resp


# ─────────────────────────── камера ───────────────────────────
def detect_camera():
    """Путь к устройству камеры: CNC_CAMERA, иначе первый /dev/video*."""
    dev = os.environ.get("CNC_CAMERA", "").strip()
    if dev.lower() in ("off", "none", "нет", "0"):
        return None
    if dev:
        return dev if os.path.exists(dev) else None
    vids = sorted(glob.glob("/dev/video*"),
                  key=lambda p: int(re.sub(r"\D", "", p) or 0))
    return vids[0] if vids else None


class Camera:
    """Захват MJPEG с USB-камеры через ffmpeg и раздача многим зрителям.

    ffmpeg берёт готовые JPEG-кадры (-input_format mjpeg) и отдаёт их
    без перекодирования (-c:v copy) — нагрузка на Pi минимальна. Захват
    стартует по первому зрителю и гасится, когда все ушли.
    """
    SOI = b"\xff\xd8"          # начало JPEG
    EOI = b"\xff\xd9"          # конец JPEG

    def __init__(self, device, width=640, height=480, fps=10):
        self.device = device
        self.width = width
        self.height = height
        self.fps = fps
        self.available = bool(device) and shutil.which("ffmpeg") is not None
        self.proc = None
        self._task = None
        self._stop_task = None
        self.clients = 0
        self.latest = None        # последний целый JPEG-кадр
        self.seq = 0
        self.error = ""

    async def _reader(self):
        args = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "v4l2", "-input_format", "mjpeg",
            "-video_size", f"{self.width}x{self.height}",
            "-framerate", str(self.fps),
            "-i", self.device,
            "-c:v", "copy", "-f", "image2pipe", "-",
        ]
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
        except Exception as e:
            self.error = f"ffmpeg не запустился: {e}"
            return
        buf = b""
        try:
            while True:
                chunk = await self.proc.stdout.read(65536)
                if not chunk:
                    err = b""
                    with contextlib.suppress(Exception):
                        err = await self.proc.stderr.read()
                    self.error = err.decode(errors="replace").strip() or "поток камеры прерван"
                    break
                buf += chunk
                while True:                       # вырезаем все целые кадры
                    a = buf.find(self.SOI)
                    if a < 0:
                        if len(buf) > 4_000_000:
                            buf = b""
                        break
                    b = buf.find(self.EOI, a + 2)
                    if b < 0:
                        if a > 0:
                            buf = buf[a:]
                        break
                    self.latest = buf[a:b + 2]
                    self.seq += 1
                    buf = buf[b + 2:]
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.error = str(e)
        finally:
            await self._kill_proc()

    async def _kill_proc(self):
        p, self.proc = self.proc, None
        if p and p.returncode is None:
            with contextlib.suppress(Exception):
                p.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(p.wait(), 2)
            if p.returncode is None:
                with contextlib.suppress(Exception):
                    p.kill()

    def ensure_running(self):
        if self._stop_task:
            self._stop_task.cancel()
            self._stop_task = None
        if not self._task or self._task.done():
            self.error = ""
            self.latest = None
            self._task = asyncio.get_running_loop().create_task(self._reader())

    def add_client(self):
        self.clients += 1
        self.ensure_running()

    def remove_client(self):
        self.clients = max(0, self.clients - 1)
        if self.clients == 0:                     # погасить с небольшой задержкой
            async def _later():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.sleep(3)
                    if self.clients == 0:
                        await self.stop()
            self._stop_task = asyncio.get_running_loop().create_task(_later())

    async def stop(self):
        for t in (self._stop_task, self._task):
            if t:
                t.cancel()
        self._stop_task = None
        if self._task:
            with contextlib.suppress(Exception):
                await self._task
            self._task = None
        await self._kill_proc()
        self.latest = None


async def h_camera_info(request):
    cam = request.app["cam"]
    return web.json_response({
        "available": cam.available, "device": cam.device or "",
        "width": cam.width, "height": cam.height, "fps": cam.fps,
    })


async def h_camera_stream(request):
    cam = request.app["cam"]
    if not cam.available:
        return web.json_response({"ok": False, "error": "камера недоступна"}, status=404)
    resp = web.StreamResponse(headers={
        "Content-Type": "multipart/x-mixed-replace; boundary=frame",
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
    })
    await resp.prepare(request)
    cam.add_client()
    last_seq, stale = -1, 0
    try:
        while True:
            if cam.seq != last_seq and cam.latest is not None:
                last_seq, frame, stale = cam.seq, cam.latest, 0
                await resp.write(
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
            else:
                await asyncio.sleep(0.03)
                stale += 1
                if cam.error and cam.latest is None:
                    break
                if stale > 500:                   # ~15 c без новых кадров
                    break
    except (asyncio.CancelledError, ConnectionResetError, ConnectionError):
        pass
    finally:
        cam.remove_client()
    return resp


async def h_camera_snapshot(request):
    cam = request.app["cam"]
    if not cam.available:
        return web.json_response({"ok": False, "error": "камера недоступна"}, status=404)
    cam.add_client()
    try:
        start = cam.seq
        # ждём несколько свежих кадров, чтобы автоэкспозиция устоялась
        for _ in range(200):
            if cam.latest is not None and cam.seq >= start + 5:
                break
            if cam.error and cam.latest is None:
                break
            await asyncio.sleep(0.05)
        if cam.latest is None:
            return web.json_response(
                {"ok": False, "error": cam.error or "нет кадра"}, status=503)
        return web.Response(body=cam.latest, content_type="image/jpeg",
                            headers={"Cache-Control": "no-store"})
    finally:
        cam.remove_client()


def build_app(grbl, token):
    app = web.Application(client_max_size=256 * 1024 * 1024,
                          middlewares=[auth_mw] if token else [])
    app["grbl"] = grbl
    app["token"] = token
    app.router.add_get("/", h_index)
    app.router.add_get("/ws", h_ws)
    app.router.add_get("/api/status", h_status)
    app.router.add_post("/api/command", h_command)
    app.router.add_post("/api/realtime", h_realtime)
    app.router.add_post("/api/jog", h_jog)
    app.router.add_post("/api/zero", h_zero)
    app.router.add_post("/api/job", h_job)
    app.router.add_get("/api/settings", h_settings)
    app.router.add_post("/api/settings", h_settings)
    app.router.add_get("/api/files", h_files)
    app.router.add_post("/api/files", h_upload)
    app.router.add_get("/api/files/{name}", h_file_raw)
    app.router.add_delete("/api/files/{name}", h_file_delete)
    app.router.add_get("/api/camera", h_camera_info)
    app.router.add_get("/camera/stream", h_camera_stream)
    app.router.add_get("/camera/snapshot", h_camera_snapshot)
    app.router.add_static("/static/", STATIC_DIR)
    return app


async def main_async(args):
    GCODE_DIR.mkdir(exist_ok=True)
    grbl = Grbl(args.port, args.baud, simulate=args.simulate)
    await grbl.connect()
    cam = Camera(detect_camera(),
                 width=int(os.environ.get("CNC_CAMERA_WIDTH", 640)),
                 height=int(os.environ.get("CNC_CAMERA_HEIGHT", 480)),
                 fps=int(os.environ.get("CNC_CAMERA_FPS", 10)))
    app = build_app(grbl, args.token or os.environ.get("CNC_TOKEN", ""))
    app["cam"] = cam
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, args.host, args.http_port)
    await site.start()
    print(f"cncd: http://{args.host}:{args.http_port}  порт станка: {grbl.state['port']}"
          f"  камера: {cam.device or 'нет'}")
    try:
        await asyncio.Event().wait()
    finally:
        await cam.stop()
        await grbl.close()
        await runner.cleanup()


def main():
    ap = argparse.ArgumentParser(description="Веб-мост Raspberry Pi ↔ ЧПУ (GRBL)")
    ap.add_argument("-p", "--port", default=os.environ.get("CNC_PORT", ""),
                    help="последовательный порт (по умолчанию — автопоиск)")
    ap.add_argument("-b", "--baud", type=int, default=int(os.environ.get("CNC_BAUD", 115200)))
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--http-port", type=int, default=int(os.environ.get("CNC_HTTP_PORT", 8080)))
    ap.add_argument("--token", default="", help="пароль-токен для доступа к интерфейсу")
    ap.add_argument("--simulate", action="store_true", help="эмулятор GRBL вместо станка")
    args = ap.parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
