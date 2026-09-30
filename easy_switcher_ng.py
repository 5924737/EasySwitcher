#!/usr/bin/env python3
"""easy-switcher-ng: исправление текста, набранного не в той раскладке (X11).

Отличия от старого easy-switcher, из-за которых он не теряет буквы под нагрузкой:
  * Клавиатура захватывается (EVIOCGRAB), все события идут через одно виртуальное
    uinput-устройство. Поэтому ввод пользователя во время исправления не
    перемешивается с вставляемыми символами — он ждёт в очереди и уходит после.
  * Смена раскладки не «ждёт N миллисекунд», а подтверждается: после нажатия
    комбинации переключения опрашиваем XKB-группу у X-сервера и перепечатываем
    слово только когда группа реально сменилась.
"""

import ctypes
import ctypes.util
import logging
import os
import queue
import select
import signal
import sys
import threading
import time
import tomllib
from dataclasses import dataclass, field

import evdev
from evdev import ecodes as e

log = logging.getLogger("easy-switcher-ng")

VIRTUAL_NAME = "easy-switcher-ng virtual keyboard"
CONFIG_PATH = os.path.expanduser("~/.config/easy-switcher-ng/config.toml")

# Клавиши, которые печатают символ и одинаково «перепечатываются» в любой раскладке.
PRINTABLE = {
    *range(e.KEY_1, e.KEY_EQUAL + 1),          # 1..0 - =
    *range(e.KEY_Q, e.KEY_RIGHTBRACE + 1),     # q..]
    *range(e.KEY_A, e.KEY_GRAVE + 1),          # a..`
    *range(e.KEY_BACKSLASH, e.KEY_SLASH + 1),  # \ z.. /
    e.KEY_SPACE, e.KEY_102ND,
}
SHIFTS = {e.KEY_LEFTSHIFT, e.KEY_RIGHTSHIFT}
MODIFIERS = SHIFTS | {e.KEY_LEFTCTRL, e.KEY_RIGHTCTRL, e.KEY_LEFTALT,
                      e.KEY_RIGHTALT, e.KEY_LEFTMETA, e.KEY_RIGHTMETA}


@dataclass
class Config:
    correct_key: int = e.KEY_PAUSE
    switch_keys: list[int] = field(default_factory=lambda: [e.KEY_LEFTCTRL, e.KEY_TAB])
    reverse_mode: bool = False
    switch_timeout_ms: int = 3000
    inject_delay_ms: float = 5.0
    settle_ms: int = 20
    max_buffer: int = 300


def key_code(name: str) -> int:
    name = name.strip().upper()
    if not name.startswith("KEY_"):
        name = "KEY_" + name
    code = e.ecodes.get(name)
    if code is None:
        raise ValueError(f"неизвестная клавиша: {name}")
    return code


def load_config(path: str) -> Config:
    cfg = Config()
    if not os.path.exists(path):
        log.info("конфиг %s не найден, используются значения по умолчанию", path)
        return cfg
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    if "correct_key" in raw:
        cfg.correct_key = key_code(raw["correct_key"])
    if "switch_keys" in raw:
        cfg.switch_keys = [key_code(k) for k in raw["switch_keys"].split("+")]
    for name in ("reverse_mode", "switch_timeout_ms", "inject_delay_ms", "settle_ms", "max_buffer"):
        if name in raw:
            setattr(cfg, name, type(getattr(cfg, name))(raw[name]))
    return cfg


class Xkb:
    """Текущая XKB-группа (раскладка) через libX11, без внешних зависимостей."""

    class _State(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ubyte) for n in (
            "group", "locked_group")] + [
            ("base_group", ctypes.c_ushort), ("latched_group", ctypes.c_ushort)] + [
            (n, ctypes.c_ubyte) for n in (
                "mods", "base_mods", "latched_mods", "locked_mods",
                "compat_state", "grab_mods", "compat_grab_mods",
                "lookup_mods", "compat_lookup_mods")] + [
            ("ptr_buttons", ctypes.c_ushort)]

    def __init__(self):
        self.lib = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        self.lib.XOpenDisplay.restype = ctypes.c_void_p
        self.lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.lib.XkbGetState.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                         ctypes.POINTER(self._State)]
        self.lib.XDefaultRootWindow.restype = ctypes.c_ulong
        self.lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        self.lib.XGrabKeyboard.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
        self.lib.XUngrabKeyboard.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.lib.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.dpy = self.lib.XOpenDisplay(None)
        if not self.dpy:
            raise RuntimeError("не удалось подключиться к X (DISPLAY=%s)"
                               % os.environ.get("DISPLAY"))
        self.root = self.lib.XDefaultRootWindow(self.dpy)

    def group(self) -> int:
        st = self._State()
        # XkbGetState — синхронный запрос, т.е. X-сервер уже обработал всё,
        # что ему пришло до этого момента.
        self.lib.XkbGetState(self.dpy, 0x0100, ctypes.byref(st))  # XkbUseCoreKbd
        return st.locked_group

    def keyboard_grabbed(self) -> bool:
        """Держит ли кто-то клавиатуру (например, переключатель раскладок GNOME).

        Пока GNOME держит клавиатуру, все нажатия уходят ему, а не в окно.
        Пробуем захватить её сами: если не вышло — занята. Во время этой проверки
        мы ничего не печатаем, поэтому наш мгновенный захват ничего не отнимает.
        """
        # GrabModeAsync = 1, CurrentTime = 0; результат GrabSuccess = 0
        ok = self.lib.XGrabKeyboard(self.dpy, self.root, 0, 1, 1, 0) == 0
        if ok:
            self.lib.XUngrabKeyboard(self.dpy, 0)
            self.lib.XSync(self.dpy, 0)
        return not ok


def is_keyboard(dev: evdev.InputDevice) -> bool:
    keys = dev.capabilities().get(e.EV_KEY, [])
    return e.KEY_A in keys and e.KEY_SPACE in keys


def is_pointer(dev: evdev.InputDevice) -> bool:
    return e.BTN_LEFT in dev.capabilities().get(e.EV_KEY, [])


class Reader(threading.Thread):
    """Читает все клавиатуры и мыши и складывает события в очередь.

    Отдельный поток нужен, чтобы ядерный буфер evdev не переполнялся, пока
    основной поток ждёт смены раскладки под нагрузкой.
    """

    def __init__(self, out: queue.Queue, on_leds_needed):
        super().__init__(daemon=True)
        self.out = out
        self.keyboards: dict[str, evdev.InputDevice] = {}
        self.pointers: dict[str, evdev.InputDevice] = {}
        self.on_leds_needed = on_leds_needed
        self.lock = threading.Lock()
        self.stop = False

    def rescan(self):
        known = set(self.keyboards) | set(self.pointers)
        for path in evdev.list_devices():
            if path in known:
                continue
            try:
                dev = evdev.InputDevice(path)
                if dev.name == VIRTUAL_NAME:
                    dev.close()
                elif is_keyboard(dev):
                    if dev.active_keys():
                        # Захват при зажатой клавише оставит её «залипшей» в X.
                        dev.close()
                        continue
                    dev.grab()
                    with self.lock:
                        self.keyboards[path] = dev
                    log.info("клавиатура: %s (%s)", dev.name, path)
                    self.on_leds_needed()
                elif is_pointer(dev):
                    self.pointers[path] = dev
                    log.info("указатель: %s (%s)", dev.name, path)
                else:
                    dev.close()
            except OSError as err:
                log.debug("пропуск %s: %s", path, err)

    def drop(self, dev):
        with self.lock:
            self.keyboards.pop(dev.path, None)
        self.pointers.pop(dev.path, None)
        try:
            dev.close()
        except OSError:
            pass
        log.info("устройство отключено: %s", dev.path)

    def run(self):
        last_scan = 0.0
        while not self.stop:
            now = time.monotonic()
            if now - last_scan > 2:
                self.rescan()
                last_scan = now
            devs = list(self.keyboards.values()) + list(self.pointers.values())
            try:
                ready, _, _ = select.select(devs, [], [], 0.5)
            except (OSError, ValueError):
                ready = []
            for dev in ready:
                kb = dev.path in self.keyboards
                try:
                    for ev in dev.read():
                        if ev.type == e.EV_KEY:
                            self.out.put(("kbd" if kb else "ptr", ev.code, ev.value))
                except OSError:
                    self.drop(dev)
                    if kb:
                        self.out.put(("kbd_gone", 0, 0))

    def release(self):
        self.stop = True
        with self.lock:
            for dev in self.keyboards.values():
                try:
                    dev.ungrab()
                except OSError:
                    pass


class Switcher:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.xkb = Xkb()
        keys = [c for c in range(1, e.KEY_MAX) if not 0x100 <= c < 0x160]
        self.ui = evdev.UInput({e.EV_KEY: keys,
                                e.EV_LED: [e.LED_NUML, e.LED_CAPSL, e.LED_SCROLLL]},
                               name=VIRTUAL_NAME)
        self.events: queue.Queue = queue.Queue()
        self.reader = Reader(self.events, self.sync_leds)
        self.down: set[int] = set()        # что X сейчас считает нажатым (через нас)
        self.buffer: list[tuple[int, bool]] = []   # (клавиша, был ли Shift)
        self.leds: set[int] = set()
        self.swallowed = False               # нажатие correct_key не ушло в X

    # --- вывод -------------------------------------------------------------
    def emit(self, code: int, value: int):
        self.ui.write(e.EV_KEY, code, value)
        self.ui.syn()
        if value == 1:
            self.down.add(code)
        elif value == 0:
            self.down.discard(code)

    def tap(self, code: int):
        self.emit(code, 1)
        self.emit(code, 0)
        if self.cfg.inject_delay_ms:
            time.sleep(self.cfg.inject_delay_ms / 1000)

    # --- светодиоды: X ставит их виртуальному устройству, дублируем на реальные ---
    def pump_leds(self):
        try:
            while True:
                ev = self.ui.read_one()
                if ev is None:
                    break
                if ev.type == e.EV_LED:
                    (self.leds.add if ev.value else self.leds.discard)(ev.code)
                    self.sync_leds()
        except (OSError, BlockingIOError):
            pass

    def sync_leds(self):
        with self.reader.lock:
            for dev in self.reader.keyboards.values():
                for led in (e.LED_NUML, e.LED_CAPSL, e.LED_SCROLLL):
                    try:
                        dev.set_led(led, 1 if led in self.leds else 0)
                    except OSError:
                        pass

    # --- логика ------------------------------------------------------------
    def shift_down(self) -> bool:
        return bool(self.down & SHIFTS)

    def other_mods_down(self) -> bool:
        return bool(self.down & (MODIFIERS - SHIFTS))

    def handle_key(self, code: int, value: int):
        if code == self.cfg.correct_key:
            # Работает только если есть что исправлять и не зажаты Ctrl/Alt/Win,
            # иначе клавиша ведёт себя как обычно (важно, если это Delete).
            if value == 1 and self.buffer and not self.other_mods_down():
                self.correct(phrase=self.shift_down() != self.cfg.reverse_mode)
                self.swallowed = True
                return
            if value != 1 and self.swallowed:
                if value == 0:
                    self.swallowed = False
                return

        if value in (1, 2):
            if code in PRINTABLE and not self.other_mods_down():
                self.buffer.append((code, self.shift_down()))
                del self.buffer[:-self.cfg.max_buffer]
            elif code == e.KEY_BACKSPACE and not self.other_mods_down():
                if self.buffer:
                    self.buffer.pop()
            elif code not in MODIFIERS and code != e.KEY_CAPSLOCK:
                self.buffer.clear()          # стрелки, Enter, Ctrl+V, F-клавиши и т.п.

        self.emit(code, value)

    def correct(self, phrase: bool):
        if phrase:
            chunk = list(self.buffer)
        else:
            i = len(self.buffer)
            while i > 0 and self.buffer[i - 1][0] == e.KEY_SPACE:   # хвостовые пробелы
                i -= 1
            while i > 0 and self.buffer[i - 1][0] != e.KEY_SPACE:   # само слово
                i -= 1
            chunk = self.buffer[i:]
        if not chunk:
            return

        held = [c for c in self.down if c in MODIFIERS]
        for c in held:                       # отпускаем Shift и пр., чтобы не мешали
            self.emit(c, 0)

        for _ in chunk:
            self.tap(e.KEY_BACKSPACE)

        before = self.xkb.group()
        started = time.monotonic()
        for c in self.cfg.switch_keys:
            self.emit(c, 1)
        for c in reversed(self.cfg.switch_keys):
            self.emit(c, 0)

        deadline = time.monotonic() + self.cfg.switch_timeout_ms / 1000
        while self.xkb.group() == before:
            if time.monotonic() > deadline:
                log.warning("раскладка не сменилась за %d мс, печатаю как было",
                            self.cfg.switch_timeout_ms)
                break
            time.sleep(0.005)
        switched = time.monotonic()
        # GNOME меняет раскладку ещё до того, как отпустит клавиатуру после Ctrl+Tab:
        # печатать раньше — значит отдать буквы ему, и они пропадут.
        while self.xkb.keyboard_grabbed():
            if time.monotonic() > deadline:
                log.warning("клавиатура занята дольше %d мс", self.cfg.switch_timeout_ms)
                break
            time.sleep(0.005)
        released = time.monotonic()
        time.sleep(self.cfg.settle_ms / 1000)

        for code, shifted in chunk:
            if shifted:
                self.emit(e.KEY_LEFTSHIFT, 1)
            self.tap(code)
            if shifted:
                self.emit(e.KEY_LEFTSHIFT, 0)

        for c in held:
            self.emit(c, 1)
        # Сам текст не пишем: в журнал могли бы попасть пароли.
        log.info("исправлено: %d симв. (%s), раскладка %d → %d, ожидание смены %d мс, "
                 "клавиатура занята ещё %d мс",
                 len(chunk), "фраза" if phrase else "слово", before, self.xkb.group(),
                 (switched - started) * 1000, (released - switched) * 1000)

    def release_all(self):
        for c in list(self.down):
            self.emit(c, 0)

    def run(self):
        self.reader.start()
        while True:
            try:
                kind, code, value = self.events.get(timeout=0.2)
            except queue.Empty:
                self.pump_leds()
                continue
            if kind == "kbd":
                self.handle_key(code, value)
            elif kind == "ptr":
                if value == 1:
                    self.buffer.clear()      # клик мышью = курсор мог переместиться
            elif kind == "kbd_gone":
                self.release_all()
                self.buffer.clear()
            self.pump_leds()

    def close(self):
        self.reader.release()
        self.release_all()
        self.ui.close()


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(levelname)s %(message)s")
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else CONFIG_PATH)
    sw = Switcher(cfg)

    def stop(*_):
        sw.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("запущен: исправление=%s, переключение=%s",
             e.KEY[cfg.correct_key],
             "+".join(str(e.KEY[c]) for c in cfg.switch_keys))
    try:
        sw.run()
    finally:
        sw.close()


if __name__ == "__main__":
    main()
