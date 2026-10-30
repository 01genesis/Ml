#!/usr/bin/env python3
"""
TKD 01 Electronic — PC + mandos + móviles
Marcador con ajustes completos, botones en pantalla, teclado y gamepad.
Móviles: http://IP:8080
Autor: 01gen
"""

from __future__ import annotations

import copy
import hashlib
import os
import traceback
import re
import json
import random
import struct
import subprocess
import sys
import urllib.request
import socket
import threading
import time
from dataclasses import dataclass, field, asdict
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import pygame

SAVE_PATH = Path.home() / ".tkd01_config.json"
TV_STATE_PATH = Path.home() / ".tkd01_tv_state.json"
TV_UDP_PORT = 28765  # respaldo UDP
# Memoria compartida binaria (RAM) — latencia ~0 entre PC y TV
# v2 layout:
# 12i: red,blue,rf,bf,rw,bw,round,rounds,maxF,flags,banner_side,seq
#  6i: r1r,r1b,r2r,r2b,r3r,r3b  (-1 = sin score de ese round)
#  6i: redR,redG,redB,blueR,blueG,blueB
#  2d: secondsF, timeoutLeft
# 64s×2: redName, blueName
# 16s: banner_reason (utf-8, null-padded)
# flags bits: running, resting, inTimeout, winnerR, winnerB, drawn, golden, sim, has_banner
#             + 2 bits por round winner (bits 9-14)
TV_SHM_FMT = "<24i2d64s64s16s"
TV_SHM_SIZE = struct.calcsize(TV_SHM_FMT)
TV_SHM_NAME = "tkd01_tv_shm_v2"
_tv_seq = 0  # contador monótono de paquetes PC → TV


def admin_file() -> Path:
    return SAVE_PATH.with_name(".tkd01_admin.json")


def _phash(p: str) -> str:
    return hashlib.sha256(("tkd01|" + p).encode("utf-8")).hexdigest()


def admin_check(p: str) -> bool:
    try:
        h = json.loads(admin_file().read_text(encoding="utf-8"))["h"]
    except Exception:
        h = _phash(DEFAULT_ADMIN_PASS)
    return _phash(p) == h


def admin_set(p: str):
    admin_file().write_text(json.dumps({"h": _phash(p)}), encoding="utf-8")


def admin_reset():
    try:
        admin_file().unlink()
    except FileNotFoundError:
        pass
HTTP_PORT = 8080
DEFAULT_PORT = 8080
LOG_PATH = Path.home() / "TKD01_errores.txt"
DEFAULT_ADMIN_PASS = "01gen"  # contraseña inicial del modo admin (se cambia desde ADMIN)
# Carpeta de recursos: la del script, o la temporal de PyInstaller si va empaquetado
BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
LOGO_PATH = BASE_DIR / "tkd01_logo.jpg"
TV_SPLASH_PATH = BASE_DIR / "tv_splash.jpg"
STAR_PATH = BASE_DIR / "tkd01_star.png"  # estrella (solo en la ventana principal, NO en la pantalla grande)
VOTE_WINDOW = 2.2

# Colores UI
RED = (180, 35, 45)
BLUE = (25, 45, 120)
WINE = (95, 20, 35)
WINE_LIGHT = (130, 35, 50)
NAVY = (15, 30, 75)
NAVY_LIGHT = (30, 50, 110)
YELLOW = (255, 220, 50)
WHITE = (250, 250, 255)
BLACK = (0, 0, 0)
DARK = (12, 14, 22)
DARK2 = (22, 26, 40)
PANEL = (28, 32, 48)
GREEN = (80, 220, 120)
GRAY = (140, 145, 165)
ORANGE = (255, 160, 50)
BTN = (45, 50, 70)
BTN_H = (70, 75, 100)
GOLD = (255, 200, 60)


def _def_map(i: int) -> dict:
    """Distribución de botones que usan TODOS los mandos (cada mando es un juez).
    i = 0 → ROJO (botones de la derecha) · i = 1 → AZUL (cruceta y gatillo izquierdo).
    '*' = cualquier mando. Solo puntos: los mandos no restan ni ponen faltas."""
    if i == 0:
        return {"pts1": "*|b0", "pts2": "*|b1", "pts3": "*|b2", "pts4": "*|b3", "pts5": "*|b5"}
    return {"pts1": "*|h0D", "pts2": "*|h0L", "pts3": "*|h0R", "pts4": "*|h0U", "pts5": "*|b4"}


@dataclass
class Config:
    red_name: str = "ROJO"
    blue_name: str = "AZUL"
    round_seconds: int = 120
    rest_seconds: int = 60
    rounds: int = 3
    max_fouls: int = 5
    max_point_diff: int = 15  # 0 = off
    timeout_seconds: int = 60  # tiempo muerto (0 = off)
    expected_judges: int = 3  # jueces móviles esperados (0 = no comprobar)
    min_votes: int = 2  # teléfonos distintos necesarios para validar un punto
    vote_window_ms: int = 1000  # tiempo máximo entre las señales de los teléfonos
    block_missing: int = 0  # 1 = no contar puntos si faltan jueces
    debounce_ms: int = 150  # anti-rebote de mandos y teléfonos
    foul_give: int = 1  # puntos que recibe el rival por cada falta
    foul_take: int = 1  # puntos que pierde el infractor por cada falta
    golden_point: int = 0  # 1 = empate de round → punto de oro (en vez de sorteo)
    golden_seconds: int = 60
    draw_seconds: int = 3  # duración del aviso de sorteo
    banner_seconds: int = 3  # aviso "gana el round"
    winner_seconds: int = 4  # pantalla de ganador del combate
    tv_auto: int = 0  # 0 = no abrir TV sola (evita superposición); 1 = abrir al detectar monitor
    tv_display: int = 0  # pantalla del modo TV (0 = automática: la más grande)
    primary_role: int = 0  # 0 = PC (la más pequeña) es principal; 1 = TV (la más grande) es principal
    red_color: list = field(default_factory=lambda: [211, 47, 47])
    blue_color: list = field(default_factory=lambda: [30, 63, 216])
    # cada acción: "<mando>|<botón>"  ("#0" = mando número 1, "" = sin asignar)
    pad_map_red: dict = field(default_factory=lambda: _def_map(0))
    pad_map_blue: dict = field(default_factory=lambda: _def_map(1))


@dataclass
class State:
    red: int = 0
    blue: int = 0
    red_fouls: int = 0
    blue_fouls: int = 0
    red_won: int = 0
    blue_won: int = 0
    round: int = 1
    seconds_left: float = 120
    running: bool = False
    resting: bool = False
    round_scores: list = field(default_factory=list)
    round_winners: list = field(default_factory=list)  # RED / BLUE por cada round ya cerrado
    winner: str | None = None
    drawn: bool = False
    round_drawn: bool = False
    show_winner_until: float = 0.0
    show_draw_until: float = 0.0
    draw_side: str | None = None
    round_win_reason: str = ""  # "", "sorteo", "faltas", "diff"
    in_timeout: bool = False
    timeout_left: float = 0.0
    golden: bool = False


class Match:
    def __init__(self):
        self.cfg = Config()
        self.st = State(seconds_left=self.cfg.round_seconds)
        self.undo_stack: list[State] = []
        self.lock = threading.Lock()
        self.mobile_judges: dict[str, float] = {}
        self.votes: list[tuple] = []
        self.last_signals: list[dict] = []
        self._pending_draw = True
        self._pending_draw_apply = False
        self.test_mode = False
        self.test_log: list = []
        self._last_vote: dict = {}
        self.blocked: dict = {}  # jid -> hasta cuándo (inf = permanente)
        self.jstats: dict = {}
        self.diag: list = []
        self.fps = 0.0
        self.sim = False
        self.sim_backup = None
        self.sim_speed = 1
        self.sim_interval = 2.0
        self.sim_fouls = True
        self.sim_timeouts = False
        self._sim_next = 0.0
        self._sim_wait = 0.0
        self._load()
        global RED, BLUE
        try:
            RED = tuple(max(0, min(255, int(v))) for v in self.cfg.red_color[:3])
            BLUE = tuple(max(0, min(255, int(v))) for v in self.cfg.blue_color[:3])
        except Exception:
            pass

    def _load(self):
        if SAVE_PATH.exists():
            try:
                d = json.loads(SAVE_PATH.read_text(encoding="utf-8"))
                for k, v in d.items():
                    if hasattr(self.cfg, k):
                        cur = getattr(self.cfg, k)
                        if isinstance(cur, dict):
                            cur.update(v)
                        else:
                            setattr(self.cfg, k, type(cur)(v))
                for mp in (self.cfg.pad_map_red, self.cfg.pad_map_blue):
                    mp.pop("minus1", None)
                    mp.pop("foul", None)
                self.st.seconds_left = float(self.cfg.round_seconds)
            except Exception:
                pass

    def save(self):
        SAVE_PATH.write_text(json.dumps(asdict(self.cfg), indent=2), encoding="utf-8")

    def apply_config_reset(self):
        """Aplica cfg y reinicia combate."""
        with self.lock:
            self.undo_stack.clear()
            self.votes.clear()
            self.last_signals.clear()
            self._pending_draw_apply = False
            self.st = State(seconds_left=float(self.cfg.round_seconds))
        self.save()

    def judge_count(self) -> int:
        now = time.time()
        for k in [k for k, t in self.mobile_judges.items() if now - t > 4]:
            del self.mobile_judges[k]
        return len(self.mobile_judges)

    def judge_status(self):
        """[(id, segundos desde la última señal)] de los teléfonos conectados."""
        self.judge_count()
        now = time.time()
        return sorted((k, now - t) for k, t in list(self.mobile_judges.items()))

    def is_blocked(self, jid: str) -> bool:
        until = self.blocked.get(jid)
        if until is None:
            return False
        if time.time() >= until:
            self.blocked.pop(jid, None)
            return False
        return True

    def ping_judge(self, jid: str) -> bool:
        if self.is_blocked(jid):
            self.mobile_judges.pop(jid, None)
            return False
        self.mobile_judges[jid] = time.time()
        return True

    def kick(self, jid: str, seconds: float = 60.0):
        """Expulsa un teléfono durante unos segundos."""
        with self.lock:
            self.blocked[jid] = time.time() + seconds
            self.mobile_judges.pop(jid, None)
            self.votes = [x for x in self.votes if x[0] != jid]
            self._diag(f"{jid} expulsado {int(seconds)} s")

    def block(self, jid: str):
        """Bloquea un teléfono hasta que se permita de nuevo."""
        with self.lock:
            self.blocked[jid] = float("inf")
            self.mobile_judges.pop(jid, None)
            self.votes = [x for x in self.votes if x[0] != jid]
            self._diag(f"{jid} bloqueado")

    def allow(self, jid: str):
        with self.lock:
            self.blocked.pop(jid, None)
            self._diag(f"{jid} permitido")

    def _js(self, jid: str) -> dict:
        return self.jstats.setdefault(jid, {"sent": 0, "ok": 0, "deb": 0, "exp": 0, "blk": 0, "state": 0})

    def _diag(self, msg: str):
        self.diag.insert(0, f"{time.strftime('%H:%M:%S')}  {msg}")
        self.diag = self.diag[:14]

    # ---- simulación ----
    def sim_start(self):
        with self.lock:
            if self.sim:
                return
            self.sim_backup = (copy.deepcopy(self.st), copy.deepcopy(self.undo_stack))
            self.undo_stack = []
            self._pending_draw_apply = False
            self.st = State(seconds_left=float(self.cfg.round_seconds))
            self.sim = True
            self._sim_next = time.time() + 1.0
            self._sim_wait = 0.0

    def sim_stop(self):
        with self.lock:
            if not self.sim:
                return
            if self.sim_backup:
                self.st, self.undo_stack = self.sim_backup
            self.sim_backup = None
            self._pending_draw_apply = False
            self.sim = False

    def sim_step(self):
        """Genera acciones aleatorias para probar pantallas y mandos."""
        now = time.time()
        if not self.sim or now < self._sim_next:
            return
        s = self.st
        if s.winner:
            if not self._sim_wait:
                self._sim_wait = now + 6.0
            elif now > self._sim_wait:
                self._sim_wait = 0.0
                self.dismiss_winner()
            return
        if s.show_draw_until > now or s.in_timeout:
            return
        if not s.running and not s.resting:
            self.toggle()
            self._sim_next = now + 1.0
            return
        if s.resting:
            return
        red = random.choice([True, False])
        r = random.random()
        if self.sim_timeouts and r < 0.04:
            self.toggle_timeout()
        elif self.sim_fouls and r < 0.14:
            self.foul_direct(red)
        else:
            self.add_points_direct(red, random.choices([1, 2, 3, 4, 5], [40, 25, 15, 10, 10])[0])
        self._sim_next = now + self.sim_interval * random.uniform(0.6, 1.4)

    def snapshot(self):
        s = self.st
        self.undo_stack.append(State(**{f: getattr(s, f) for f in s.__dataclass_fields__}))
        if len(self.undo_stack) > 40:
            self.undo_stack.pop(0)

    def undo(self):
        with self.lock:
            if self.undo_stack and not self.st.winner:
                self.st = self.undo_stack.pop()
            else:
                return
        self._notify_tv()

    def _blocked(self) -> bool:
        s = self.st
        return bool(s.winner or s.resting or s.show_draw_until > time.time())

    def add_points_direct(self, red: bool, pts: int):
        with self.lock:
            if self._blocked():
                return
            self.snapshot()
            if red:
                self.st.red = max(0, self.st.red + pts)
            else:
                self.st.blue = max(0, self.st.blue + pts)
            self._check_gap()
        self._notify_tv()

    def _apply_foul(self, red: bool):
        c, st = self.cfg, self.st
        if red:
            st.red = max(0, st.red - c.foul_take)
            st.blue += c.foul_give
            st.red_fouls += 1
            if st.red_fouls >= c.max_fouls:
                self._finish_round(False, False, "faltas")
                return
        else:
            st.blue = max(0, st.blue - c.foul_take)
            st.red += c.foul_give
            st.blue_fouls += 1
            if st.blue_fouls >= c.max_fouls:
                self._finish_round(True, False, "faltas")
                return
        self._check_gap()

    def foul_direct(self, red: bool):
        with self.lock:
            if self._blocked():
                return
            self.snapshot()
            self._apply_foul(red)
        self._notify_tv()

    def mobile_vote(self, jid: str, kind: str, red: bool, v: int):
        # Los jueces (celulares y mandos) SOLO pueden sumar puntos: nada de restar ni faltas.
        if kind != "P" or not (1 <= v <= 5):
            return
        scored = False
        with self.lock:
            alive = self.ping_judge(jid)
            js = self._js(jid)
            js["sent"] += 1
            now = time.time()
            label = (f"+{v}" if kind == "P" else "FALTA") + (" R" if red else " A")
            if not alive:
                js["blk"] += 1
                self._diag(f"{jid} {label} → juez bloqueado")
                return
            sig_key = (jid, kind, red, v)
            if now - self._last_vote.get(sig_key, 0.0) < self.cfg.debounce_ms / 1000.0:
                js["deb"] += 1
                self._diag(f"{jid} {label} → anti-rebote")
                return
            self._last_vote[sig_key] = now
            if not self.test_mode and self._blocked():
                js["state"] += 1
                self._diag(f"{jid} {label} → ignorada (combate detenido)")
                return
            jc = self.judge_count()
            window = max(0.1, self.cfg.vote_window_ms / 1000.0)
            if (not self.test_mode and self.cfg.block_missing and self.cfg.expected_judges > 0
                    and jc < self.cfg.expected_judges):
                self.last_signals.insert(0, {"from": jid, "kind": kind, "red": red, "v": v, "ok": False, "t": now, "blocked": True})
                self.last_signals = self.last_signals[:8]
                js["blk"] += 1
                self._diag(f"{jid} {label} → faltan jueces")
                return
            for x in [x for x in self.votes if now - x[4] > window]:
                self._js(x[0])["exp"] += 1
                self._diag(f"{x[0]} sin pareja a tiempo")
            self.votes = [x for x in self.votes if now - x[4] <= window]
            self.votes = [x for x in self.votes if not (x[0] == jid and x[1] == kind and x[2] == red and x[3] == v)]
            self.votes.append((jid, kind, red, v, now))
            self.last_signals.insert(0, {"from": jid, "kind": kind, "red": red, "v": v, "ok": False, "t": now})
            self.last_signals = self.last_signals[:8]
            need = 1 if jc <= 1 else min(max(1, self.cfg.min_votes), jc)
            same = [x for x in self.votes if x[1] == kind and x[2] == red and x[3] == v]
            unique = {x[0] for x in same}
            if len(unique) >= need:
                self.votes = [x for x in self.votes if not (x[1] == kind and x[2] == red and x[3] == v)]
                for sg in self.last_signals:
                    if sg["kind"] == kind and sg["red"] == red and sg["v"] == v and sg["from"] in unique:
                        sg["ok"] = True
                ts = [x[4] for x in same]
                for j in unique:
                    self._js(j)["ok"] += 1
                self._diag(f"{' + '.join(sorted(unique))} {label} ✓ {max(ts) - min(ts):.2f} s")
                if self.test_mode:  # prueba: se valida pero NO se suma al marcador
                    self.test_log.insert(0, {"who": sorted(unique), "kind": kind, "red": red, "v": v,
                                             "dt": max(ts) - min(ts), "t": now})
                    self.test_log = self.test_log[:5]
                    return
                self.snapshot()
                if kind == "P":
                    if red:
                        self.st.red = max(0, self.st.red + v)
                    else:
                        self.st.blue = max(0, self.st.blue + v)
                    self._check_gap()
                else:
                    self._apply_foul(red)
                scored = True
        if scored:
            self._notify_tv()

    def _check_gap(self):
        if self.st.golden:  # punto de oro: gana quien anote primero
            if self.st.red > self.st.blue:
                self._finish_round(True, False, "oro")
            elif self.st.blue > self.st.red:
                self._finish_round(False, False, "oro")
            return
        d = self.cfg.max_point_diff
        if d <= 0:
            return
        diff = self.st.red - self.st.blue
        if diff >= d:
            self._finish_round(True, False, "diff")
        elif -diff >= d:
            self._finish_round(False, False, "diff")

    def toggle(self):
        with self.lock:
            if self.st.winner or self.st.show_draw_until > time.time():
                return
            if self.st.in_timeout:  # terminar el tiempo muerto y reanudar
                self.st.in_timeout = False
                self.st.timeout_left = 0.0
                self.st.running = True
                return
            self.st.running = not self.st.running

    def toggle_timeout(self):
        with self.lock:
            s = self.st
            if s.winner or s.resting or s.show_draw_until > time.time():
                return
            if s.in_timeout:
                s.in_timeout = False
                s.timeout_left = 0.0
            elif self.cfg.timeout_seconds > 0:
                s.running = False
                s.in_timeout = True
                s.timeout_left = float(self.cfg.timeout_seconds)

    def tick(self, dt: float):
        with self.lock:
            if self.st.in_timeout:
                self.st.timeout_left -= dt
                if self.st.timeout_left <= 0:
                    self.st.in_timeout = False
                    self.st.timeout_left = 0.0
                return
            if not self.st.running or self.st.winner or self.st.show_draw_until > time.time():
                return
            self.st.seconds_left -= dt
            if self.st.seconds_left <= 0:
                self.st.seconds_left = 0
                self.st.running = False
                if self.st.resting:
                    self._start_next_round()
                else:
                    self._end_round_time()

    def _end_round_time(self):
        s = self.st
        if s.red > s.blue:
            self._finish_round(True, False, "puntos")
        elif s.blue > s.red:
            self._finish_round(False, False, "puntos")
        else:
            if self.cfg.golden_point and not s.golden:
                s.golden = True
                s.seconds_left = float(self.cfg.golden_seconds)
                s.running = False
                return
            red_wins = random.choice([True, False])
            self.st.draw_side = "RED" if red_wins else "BLUE"
            self.st.round_drawn = True
            self.st.round_win_reason = "sorteo"
            self._pending_draw = red_wins
            self._pending_draw_apply = True
            self.st.show_draw_until = time.time() + self.cfg.draw_seconds
            self.st.running = False

    def update_overlays(self):
        with self.lock:
            now = time.time()
            if self.st.show_draw_until and now >= self.st.show_draw_until:
                self.st.show_draw_until = 0
                if getattr(self, "_pending_draw_apply", False):
                    # Tras banner de sorteo: aplicar round sin volver a mostrar banner
                    self._pending_draw_apply = False
                    self._finish_round(self._pending_draw, True, "sorteo", show_banner=False)
                elif getattr(self.st, "_pending_rest", False):
                    c = self.cfg
                    s = self.st
                    if not s.winner:
                        s.resting = True
                        s.running = True
                        s.seconds_left = float(c.rest_seconds)
                    s._pending_rest = False
            if self.st.show_winner_until and now >= self.st.show_winner_until:
                self.st.show_winner_until = 0

    def _finish_round(self, red_wins: bool, drawn: bool, reason: str = "", show_banner: bool = True):
        s, c = self.st, self.cfg
        s.golden = False
        scores = list(s.round_scores) + [(s.red, s.blue)]
        rw = s.red_won + (1 if red_wins else 0)
        bw = s.blue_won + (0 if red_wins else 1)
        rounds_left = c.rounds - s.round
        match_over = s.round >= c.rounds or rw > bw + rounds_left or bw > rw + rounds_left
        s.round_scores, s.red_won, s.blue_won = scores, rw, bw
        s.round_winners = list(s.round_winners) + ["RED" if red_wins else "BLUE"]
        s.round_drawn, s.running = drawn, False
        s.draw_side = "RED" if red_wins else "BLUE"
        s.round_win_reason = reason if reason else ("sorteo" if drawn else "")
        if match_over:
            if rw > bw:
                w, md = "RED", False
            elif bw > rw:
                w, md = "BLUE", False
            else:
                w, md = random.choice(["RED", "BLUE"]), True
            s.winner, s.drawn = w, md
            s.show_winner_until = time.time() + c.winner_seconds
            s.show_draw_until = 0
            s.resting = False
        elif show_banner:
            # Banner de ganador del round (faltas / diff / puntos), luego descanso
            s.show_draw_until = time.time() + c.banner_seconds
            s.resting = False
            s._pending_rest = True
        else:
            s.resting = True
            s.running = True
            s.seconds_left = float(c.rest_seconds)
            s._pending_rest = False

    def _start_next_round(self):
        s = self.st
        s.round += 1
        s.golden = False
        s.red = s.blue = s.red_fouls = s.blue_fouls = 0
        s.resting = s.running = False
        s.seconds_left = float(self.cfg.round_seconds)
        s.round_drawn, s.draw_side, s.round_win_reason = False, None, ""

    def dismiss_winner(self):
        with self.lock:
            if self.st.winner:
                self.undo_stack.clear()
                self._pending_draw_apply = False
                self.st = State(seconds_left=float(self.cfg.round_seconds))

    def public_state(self) -> dict:
        with self.lock:
            s, c = self.st, self.cfg
            return {
                "red": s.red, "blue": s.blue,
                "redFouls": s.red_fouls, "blueFouls": s.blue_fouls,
                "redWon": s.red_won, "blueWon": s.blue_won,
                "round": s.round, "rounds": c.rounds,
                "seconds": int(s.seconds_left),
                "running": s.running, "resting": s.resting,
                "redName": c.red_name, "blueName": c.blue_name,
                "winner": s.winner, "judges": self.judge_count(),
                "maxFouls": c.max_fouls, "maxDiff": c.max_point_diff,
                "signals": self.last_signals[:5],
                "secondsF": round(s.seconds_left, 2),
                "redColor": list(RED), "blueColor": list(BLUE),
                "inTimeout": s.in_timeout, "timeoutLeft": round(s.timeout_left, 1),
                "drawn": s.drawn, "golden": s.golden, "sim": self.sim,
                "roundScores": [list(x) for x in s.round_scores],
                "roundWinners": list(s.round_winners),
                "banner": ({"side": s.draw_side, "reason": s.round_win_reason}
                           if s.show_draw_until > time.time() and s.draw_side else None),
            }

    def tv_state(self) -> dict:
        """Estado mínimo para la TV (sin jueces/señales → más rápido de serializar)."""
        with self.lock:
            s, c = self.st, self.cfg
            return {
                "red": s.red, "blue": s.blue,
                "redFouls": s.red_fouls, "blueFouls": s.blue_fouls,
                "redWon": s.red_won, "blueWon": s.blue_won,
                "round": s.round, "rounds": c.rounds,
                "seconds": int(s.seconds_left),
                "running": s.running, "resting": s.resting,
                "redName": c.red_name, "blueName": c.blue_name,
                "winner": s.winner,
                "maxFouls": c.max_fouls,
                "secondsF": round(s.seconds_left, 2),
                "redColor": list(RED), "blueColor": list(BLUE),
                "inTimeout": s.in_timeout, "timeoutLeft": round(s.timeout_left, 1),
                "drawn": s.drawn, "golden": s.golden, "sim": self.sim,
                "roundScores": [list(x) for x in s.round_scores],
                "roundWinners": list(s.round_winners),
                "banner": ({"side": s.draw_side, "reason": s.round_win_reason}
                           if s.show_draw_until > time.time() and s.draw_side else None),
            }

    def push_tv_state(self):
        """Escribe el marcador en memoria compartida (RAM) + UDP de respaldo (protocolo v2)."""
        global _tv_shm, _tv_udp_sock, _tv_seq
        try:
            d = self.tv_state()
            flags = 0
            if d.get("running"): flags |= 1
            if d.get("resting"): flags |= 2
            if d.get("inTimeout"): flags |= 4
            if d.get("winner") == "RED": flags |= 8
            if d.get("winner") == "BLUE": flags |= 16
            if d.get("drawn"): flags |= 32
            if d.get("golden"): flags |= 64
            if d.get("sim"): flags |= 128
            ban = d.get("banner")
            if ban: flags |= 256
            for i, side in enumerate(d.get("roundWinners", [])[:3]):
                code = 1 if side == "RED" else (2 if side == "BLUE" else 0)
                flags |= code << (9 + 2 * i)
            bside = 1 if (ban and ban.get("side") == "RED") else (2 if ban else 0)
            rn = (d.get("redName") or "").encode("utf-8")[:63]
            bn = (d.get("blueName") or "").encode("utf-8")[:63]
            reason = ((ban or {}).get("reason") or "").encode("utf-8")[:15]

            # Round scores: hasta 3 rounds, -1 = no hay dato
            scores = d.get("roundScores") or []
            sc_ints = []
            for i in range(3):
                if i < len(scores) and scores[i] is not None and len(scores[i]) >= 2:
                    sc_ints.extend([int(scores[i][0]), int(scores[i][1])])
                else:
                    sc_ints.extend([-1, -1])

            # Colores personalizados (fallback a globales)
            rc = d.get("redColor") or list(RED)
            bc = d.get("blueColor") or list(BLUE)
            while len(rc) < 3:
                rc.append(0)
            while len(bc) < 3:
                bc.append(0)
            color_ints = [
                max(0, min(255, int(rc[0]))), max(0, min(255, int(rc[1]))), max(0, min(255, int(rc[2]))),
                max(0, min(255, int(bc[0]))), max(0, min(255, int(bc[1]))), max(0, min(255, int(bc[2]))),
            ]

            _tv_seq = (_tv_seq + 1) & 0x7FFFFFFF
            packed = struct.pack(
                TV_SHM_FMT,
                int(d.get("red", 0)), int(d.get("blue", 0)),
                int(d.get("redFouls", 0)), int(d.get("blueFouls", 0)),
                int(d.get("redWon", 0)), int(d.get("blueWon", 0)),
                int(d.get("round", 1)), int(d.get("rounds", 3)),
                int(d.get("maxFouls", 5)), int(flags), int(bside), int(_tv_seq),
                sc_ints[0], sc_ints[1], sc_ints[2], sc_ints[3], sc_ints[4], sc_ints[5],
                color_ints[0], color_ints[1], color_ints[2],
                color_ints[3], color_ints[4], color_ints[5],
                float(d.get("secondsF", 0)), float(d.get("timeoutLeft", 0)),
                rn, bn, reason,
            )
            # --- RAM compartida ---
            if _tv_shm is None:
                try:
                    from multiprocessing import shared_memory
                    try:
                        _tv_shm = shared_memory.SharedMemory(name=TV_SHM_NAME, create=True, size=TV_SHM_SIZE)
                    except FileExistsError:
                        # Segmento huérfano de una ejecución anterior: reutilizar
                        _tv_shm = shared_memory.SharedMemory(name=TV_SHM_NAME, create=False)
                        if len(_tv_shm.buf) < TV_SHM_SIZE:
                            _tv_shm.close()
                            try:
                                _tv_shm.unlink()
                            except Exception:
                                pass
                            _tv_shm = shared_memory.SharedMemory(name=TV_SHM_NAME, create=True, size=TV_SHM_SIZE)
                except Exception as e:
                    log_error(f"shm create: {e}")
            if _tv_shm is not None:
                _tv_shm.buf[:len(packed)] = packed
            # --- UDP respaldo ---
            try:
                if _tv_udp_sock is None:
                    _tv_udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                _tv_udp_sock.sendto(packed, ("127.0.0.1", TV_UDP_PORT))
            except Exception:
                pass
        except Exception as e:
            try:
                log_error(f"push_tv_state: {e}")
            except Exception:
                pass

    def _notify_tv(self):
        """Push inmediato al marcar puntos."""
        try:
            self.push_tv_state()
        except Exception:
            pass


# ---- HTTP móviles ----
MOBILE_HTML = r"""<!DOCTYPE html>
<html lang="es"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no"/>
<title>TKD 01 Juez</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent;user-select:none;-webkit-user-select:none}
html,body{height:100%}
body{font-family:system-ui,sans-serif;background:#0a0a0f;color:#fff;display:flex;flex-direction:column;touch-action:manipulation}
header{padding:10px 14px;background:#12121a;display:flex;justify-content:space-between;align-items:center;font-size:14px}
header b{color:#ffe600}
.grid{flex:1;display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:10px}
.col{display:flex;flex-direction:column;gap:8px}
.name{text-align:center;font-size:15px;font-weight:800;padding:8px;border-radius:12px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.col.r .name{background:#d32f2f}.col.b .name{background:#1e3fd8}
button{flex:1;border:0;border-radius:14px;font-size:30px;font-weight:900;color:#fff;min-height:56px}
button:active{transform:scale(.96);opacity:.8}
.col.r button{background:#b71c1c}.col.b button{background:#0d47a1}
.sig{padding:8px 12px;font-size:13px;min-height:36px;text-align:center;color:#aaa}
.ok{color:#69f0ae}.pend{color:#ffb74d}
</style></head><body>
<header>
<div>TKD 01 · <b>Juez</b> <span id="jid"></span></div>
<div id="link">conectando…</div>
</header>
<div class="grid">
<div class="col r">
<div class="name" id="rn">ROJO</div>
<button onclick="vote(1,true)">+1</button>
<button onclick="vote(2,true)">+2</button>
<button onclick="vote(3,true)">+3</button>
<button onclick="vote(4,true)">+4</button>
<button onclick="vote(5,true)">+5</button>
</div>
<div class="col b">
<div class="name" id="bn">AZUL</div>
<button onclick="vote(1,false)">+1</button>
<button onclick="vote(2,false)">+2</button>
<button onclick="vote(3,false)">+3</button>
<button onclick="vote(4,false)">+4</button>
<button onclick="vote(5,false)">+5</button>
</div>
</div>
<div class="sig" id="sig"></div>
<script>
const JID=(localStorage.tkdJid||=String(1000+Math.floor(Math.random()*9000)));
document.getElementById('jid').textContent='#'+JID;
async function vote(v,red){try{await fetch('/api/vote?id='+JID+'&k=P&v='+v+'&red='+(red?1:0))}catch(e){}if(navigator.vibrate)navigator.vibrate(25)}
async function poll(){
try{
const r=await fetch('/api/state?id='+JID,{cache:'no-store',keepalive:true});const s=await r.json();
document.getElementById('rn').textContent=s.redName;
document.getElementById('bn').textContent=s.blueName;
document.getElementById('link').textContent=s.blocked?'BLOQUEADO por el árbitro':'OK · '+s.judges+' jueces';
document.getElementById('link').style.color=s.blocked?'#ff5252':'#69f0ae';
document.getElementById('sig').innerHTML=(s.signals||[]).filter(x=>x.kind==='P').slice(0,4).map(x=>{
const side=x.red?'R':'A';
return '<span class="'+(x.ok?'ok':'pend')+'">'+x.from+' +'+x.v+side+(x.ok?' ✓':' …')+'</span>';
}).join(' · ')||'—';
}catch(e){document.getElementById('link').textContent='sin conexión';document.getElementById('link').style.color='#ff5252'}
}
setInterval(poll,150);poll();
</script></body></html>
"""

match = None
_tv_udp_sock = None  # socket UDP reutilizado PC → TV
_tv_shm = None  # memoria compartida PC → TV


def cleanup_tv_ipc():
    """Cierra y elimina el bloque de memoria compartida + socket UDP."""
    global _tv_shm, _tv_udp_sock
    if _tv_shm is not None:
        try:
            _tv_shm.close()
        except Exception:
            pass
        try:
            _tv_shm.unlink()
        except Exception:
            pass
        _tv_shm = None
    if _tv_udp_sock is not None:
        try:
            _tv_udp_sock.close()
        except Exception:
            pass
        _tv_udp_sock = None


def unpack_tv_state(buf: bytes):
    """Desempaqueta el paquete binario v2 (SHM / UDP). Usado por el proceso TV y por tests."""
    if not buf or len(buf) < TV_SHM_SIZE:
        return None
    try:
        t = struct.unpack(TV_SHM_FMT, buf[:TV_SHM_SIZE])
    except Exception:
        return None
    (red, blue, rf, bf, rw, bw, rnd, rounds, maxf, flags, bside, seq,
     s1r, s1b, s2r, s2b, s3r, s3b,
     rr, rg, rb, br, bg, bb,
     secf, tleft, rn, bn, reason_raw) = t
    winner = "RED" if (flags & 8) else ("BLUE" if (flags & 16) else None)
    reason = reason_raw.split(b"\0", 1)[0].decode("utf-8", "ignore")
    banner = None
    if flags & 256:
        banner = {"side": "RED" if bside == 1 else "BLUE", "reason": reason}
    round_winners = []
    for i in range(3):
        code = (flags >> (9 + 2 * i)) & 0b11
        round_winners.append("RED" if code == 1 else ("BLUE" if code == 2 else None))
    while round_winners and round_winners[-1] is None:
        round_winners.pop()
    round_scores = []
    for a, b in ((s1r, s1b), (s2r, s2b), (s3r, s3b)):
        if a < 0 and b < 0:
            break
        round_scores.append([max(0, a), max(0, b)])
    return {
        "red": red, "blue": blue, "redFouls": rf, "blueFouls": bf,
        "redWon": rw, "blueWon": bw, "round": rnd, "rounds": rounds,
        "maxFouls": maxf, "secondsF": secf, "timeoutLeft": tleft,
        "running": bool(flags & 1), "resting": bool(flags & 2),
        "inTimeout": bool(flags & 4), "winner": winner,
        "drawn": bool(flags & 32), "golden": bool(flags & 64),
        "sim": bool(flags & 128), "banner": banner,
        "redName": rn.split(b"\0", 1)[0].decode("utf-8", "ignore"),
        "blueName": bn.split(b"\0", 1)[0].decode("utf-8", "ignore"),
        "redColor": [rr, rg, rb], "blueColor": [br, bg, bb],
        "roundScores": round_scores, "roundWinners": round_winners,
        "seq": seq,
    }


def log_error(msg: str):
    """Guarda errores en ~/TKD01_errores.txt (útil cuando la app no tiene consola)."""
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 400_000:
            LOG_PATH.write_text(LOG_PATH.read_text(encoding="utf-8", errors="ignore")[-150_000:], encoding="utf-8")
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} · Python {sys.version.split()[0]} · {sys.platform} ===\n"
                     f"{msg.rstrip()}\n")
    except Exception:
        pass


def _error_hook(exc_type, exc, tb):
    log_error("".join(traceback.format_exception(exc_type, exc, tb)))


def install_error_hooks():
    sys.excepthook = _error_hook
    try:
        threading.excepthook = lambda a: _error_hook(a.exc_type, a.exc_value, a.exc_traceback)
    except Exception:
        pass


def show_crash(tb_text: str):
    """Muestra el error en pantalla por si la app se abrió sin consola."""
    try:
        if not pygame.get_init():
            pygame.init()
        scr = pygame.display.set_mode((960, 540))
        pygame.display.set_caption("TKD 01 · error")
        fb = pygame.font.SysFont("dejavusans", 26, bold=True)
        fs = pygame.font.SysFont("dejavusansmono,consolas,couriernew,monospace", 15)
        lines = tb_text.strip().splitlines()[-14:]
        t0 = time.time()
        while time.time() - t0 < 120:
            for ev in pygame.event.get():
                if ev.type in (pygame.QUIT, pygame.KEYDOWN, pygame.MOUSEBUTTONDOWN):
                    return
            scr.fill((30, 10, 12))
            draw_text(scr, "TKD 01 · Ocurrió un error inesperado", fb, WHITE, topleft=(24, 20))
            draw_text(scr, "Se guardó en: " + str(LOG_PATH), fs, GOLD, topleft=(24, 70))
            y = 110
            for ln in lines:
                draw_text(scr, ln[:120], fs, (240, 200, 200), topleft=(24, y))
                y += 20
            draw_text(scr, "Pulsa cualquier tecla para cerrar", fs, GRAY, topleft=(24, 500))
            pygame.display.flip()
            pygame.time.wait(50)
    except Exception:
        pass


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        if path in ("/", "/index.html"):
            body = MOBILE_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/tv":
            data = json.dumps(match.public_state()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/state":
            jid = (qs.get("id") or ["anon"])[0][:16]
            match.ping_judge(jid)
            ps = match.public_state()
            ps["blocked"] = match.is_blocked(jid)
            data = json.dumps(ps).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/vote":
            jid = (qs.get("id") or ["anon"])[0][:16]
            kind = (qs.get("k") or ["P"])[0]
            try:
                v = int((qs.get("v") or ["1"])[0])
            except ValueError:
                v = 1
            red = (qs.get("red") or ["1"])[0] == "1"
            if kind not in ("P", "F"):
                kind = "P"
            match.mobile_vote(jid, kind, red, v)
            data = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_error(404)


def local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class _Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request, client_address):
        et = sys.exc_info()[0]
        if et is not None and issubclass(et, OSError):
            return  # teléfonos que se desconectan: no es un error real
        log_error(traceback.format_exc())


def start_server():
    """Abre el servidor de los jueces; si el puerto está ocupado prueba los 9 siguientes."""
    global HTTP_PORT
    base = HTTP_PORT
    for port in range(base, base + 10):
        try:
            server = _Server(("0.0.0.0", port), Handler)
        except OSError:
            continue
        HTTP_PORT = port
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return port
    log_error(f"No se pudo abrir ningún puerto entre {base} y {base + 9}.")
    return None


# ======================= CÓDIGO QR (sin librerías externas) =======================
# Modo byte, corrección de errores M, versiones 1-10 (hasta ~213 caracteres).
_QR_EC = {  # versión: (cw de corrección por bloque, [(nº de bloques, cw de datos por bloque), ...])
    1: (10, [(1, 16)]), 2: (16, [(1, 28)]), 3: (26, [(1, 44)]), 4: (18, [(2, 32)]),
    5: (24, [(2, 43)]), 6: (16, [(4, 27)]), 7: (18, [(4, 31)]), 8: (22, [(2, 38), (2, 39)]),
    9: (22, [(3, 36), (2, 37)]), 10: (26, [(4, 43), (1, 44)]),
}
_QR_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
             7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50]}
_GF_EXP = [0] * 512
_GF_LOG = [0] * 256
_x = 1
for _i in range(255):
    _GF_EXP[_i] = _x
    _GF_LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _GF_EXP[_i] = _GF_EXP[_i - 255]


def _gf_mul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_encode(data: list, n: int) -> list:
    g = [1]
    for i in range(n):
        nxt = [0] * (len(g) + 1)
        for j, c in enumerate(g):
            nxt[j] ^= c
            nxt[j + 1] ^= _gf_mul(c, _GF_EXP[i])
        g = nxt
    res = [0] * n
    for b in data:
        f = b ^ res[0]
        res = res[1:] + [0]
        if f:
            for i in range(n):
                res[i] ^= _gf_mul(g[i + 1], f)
    return res


def _bch(data: int, poly: int, deg: int) -> int:
    rem = data << deg
    top = poly.bit_length() - 1
    for i in range(rem.bit_length() - 1, top - 1, -1):
        if (rem >> i) & 1:
            rem ^= poly << (i - top)
    return rem


def _qr_penalty(M) -> int:
    n = len(M)
    score = 0
    for grid in (M, [list(col) for col in zip(*M)]):
        for line in grid:
            run = 1
            for i in range(1, n):
                if line[i] == line[i - 1]:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + run - 5
                    run = 1
            if run >= 5:
                score += 3 + run - 5
            s = "".join("1" if v else "0" for v in line)
            score += 40 * (s.count("10111010000") + s.count("00001011101"))
    for y in range(n - 1):
        for x in range(n - 1):
            if M[y][x] == M[y][x + 1] == M[y + 1][x] == M[y + 1][x + 1]:
                score += 3
    dark = sum(sum(1 for v in row if v) for row in M)
    score += 10 * int(abs(dark * 100 / (n * n) - 50) / 5)
    return score


def qr_matrix(text: str, force_mask=None):
    """Devuelve la matriz del QR (lista de listas de bool). True = módulo oscuro."""
    data = text.encode("utf-8")
    for ver in range(1, 11):
        ec, blocks = _QR_EC[ver]
        total = sum(k * d for k, d in blocks)
        cc = 8 if ver < 10 else 16
        if 4 + cc + 8 * len(data) <= total * 8:
            break
    else:
        raise ValueError("Texto demasiado largo para el QR")
    bits = []

    def put(val, n):
        bits.extend((val >> (n - 1 - i)) & 1 for i in range(n))
    put(0b0100, 4)
    put(len(data), cc)
    for b in data:
        put(b, 8)
    cap = total * 8
    put(0, min(4, cap - len(bits)))
    while len(bits) % 8:
        bits.append(0)
    k = 0
    while len(bits) < cap:
        put((0xEC, 0x11)[k % 2], 8)
        k += 1
    cw = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    blks, pos = [], 0
    for cnt, d in blocks:
        for _ in range(cnt):
            blks.append(cw[pos:pos + d])
            pos += d
    eccs = [_rs_encode(b, ec) for b in blks]
    out = []
    for i in range(max(len(b) for b in blks)):
        for b in blks:
            if i < len(b):
                out.append(b[i])
    for i in range(ec):
        for e in eccs:
            out.append(e[i])
    stream = [(byte >> (7 - i)) & 1 for byte in out for i in range(8)]
    stream += [0] * (7 if 2 <= ver <= 6 else 0)

    n = 17 + 4 * ver
    M = [[False] * n for _ in range(n)]
    F = [[False] * n for _ in range(n)]

    def setf(x, y, v):
        M[y][x] = bool(v)
        F[y][x] = True
    for (cx, cy) in ((0, 0), (n - 7, 0), (0, n - 7)):
        for dy in range(-1, 8):
            for dx in range(-1, 8):
                x, y = cx + dx, cy + dy
                if 0 <= x < n and 0 <= y < n:
                    inside = 0 <= dx <= 6 and 0 <= dy <= 6
                    setf(x, y, inside and (dx in (0, 6) or dy in (0, 6) or (2 <= dx <= 4 and 2 <= dy <= 4)))
    for i in range(8, n - 8):
        setf(i, 6, i % 2 == 0)
        setf(6, i, i % 2 == 0)
    ap = _QR_ALIGN[ver]
    for cy in ap:
        for cx in ap:
            if (cx == 6 and cy == 6) or (cx == 6 and cy == ap[-1]) or (cx == ap[-1] and cy == 6):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    setf(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)
    for i in range(9):
        if not F[i][8]:
            setf(8, i, False)
        if not F[8][i]:
            setf(i, 8, False)
    for i in range(8):
        setf(n - 1 - i, 8, False)
    for i in range(7):
        setf(8, n - 1 - i, False)
    setf(8, n - 8, True)
    if ver >= 7:
        vinfo = (ver << 12) | _bch(ver, 0x1F25, 12)
        for i in range(18):
            bit = (vinfo >> i) & 1
            a, b = n - 11 + i % 3, i // 3
            setf(a, b, bit)
            setf(b, a, bit)
    i = 0
    right = n - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(n):
            for j in range(2):
                x = right - j
                y = n - 1 - vert if ((right + 1) & 2) == 0 else vert
                if not F[y][x] and i < len(stream):
                    M[y][x] = bool(stream[i])
                    i += 1
        right -= 2

    masks = [lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
             lambda x, y: (x + y) % 3 == 0, lambda x, y: (y // 2 + x // 3) % 2 == 0,
             lambda x, y: (x * y) % 2 + (x * y) % 3 == 0, lambda x, y: ((x * y) % 2 + (x * y) % 3) % 2 == 0,
             lambda x, y: ((x + y) % 2 + (x * y) % 3) % 2 == 0]

    def with_mask(m):
        R = [row[:] for row in M]
        for y in range(n):
            for x in range(n):
                if not F[y][x] and masks[m](x, y):
                    R[y][x] = not R[y][x]
        fmt = ((((0b00 << 3) | m) << 10) | _bch((0b00 << 3) | m, 0x537, 10)) ^ 0x5412

        def sf(x, y, v):
            R[y][x] = bool(v)
        for i in range(6):
            sf(8, i, (fmt >> i) & 1)
        sf(8, 7, (fmt >> 6) & 1)
        sf(8, 8, (fmt >> 7) & 1)
        sf(7, 8, (fmt >> 8) & 1)
        for i in range(9, 15):
            sf(14 - i, 8, (fmt >> i) & 1)
        for i in range(8):
            sf(n - 1 - i, 8, (fmt >> i) & 1)
        for i in range(8, 15):
            sf(8, n - 15 + i, (fmt >> i) & 1)
        sf(8, n - 8, True)
        return R
    if force_mask is not None:
        return with_mask(force_mask)
    return min((with_mask(m) for m in range(8)), key=_qr_penalty)


_QR_CACHE = {}


def draw_qr(surf, text: str, x: int, y: int, size: int, centered: bool = False):
    """Dibuja el QR (fondo blanco con margen) dentro de un cuadro de 'size' px."""
    try:
        if text not in _QR_CACHE:
            _QR_CACHE[text] = qr_matrix(text)
    except Exception:
        return
    m = _QR_CACHE[text]
    n = len(m)
    cell = max(2, size // (n + 8))
    total = (n + 8) * cell
    key = (text, cell)
    if centered:
        x += (size - total) // 2
        y += (size - total) // 2
    if key not in _QR_CACHE:
        img = pygame.Surface((total, total))
        img.fill((255, 255, 255))
        for yy in range(n):
            for xx in range(n):
                if m[yy][xx]:
                    img.fill((0, 0, 0), (4 * cell + xx * cell, 4 * cell + yy * cell, cell, cell))
        _QR_CACHE[key] = img
    surf.blit(_QR_CACHE[key], (x, y))
    return total


def draw_qr_screen(surf, fonts, url: str):
    """Pantalla completa con el QR para que los jueces entren con la cámara."""
    w, h = surf.get_size()
    surf.fill((255, 255, 255))
    ink = (20, 20, 30)
    draw_text(surf, "ESCANEA PARA ENTRAR COMO JUEZ", fonts["med"], ink, center=(w // 2, int(h * 0.06)))
    size = int(min(w * 0.9, h * 0.72))
    draw_qr(surf, url, (w - size) // 2, int(h * 0.11), size, centered=True)
    draw_text(surf, url, fonts["small"], ink, center=(w // 2, int(h * 0.11) + size + 26))
    draw_text(surf, "Misma Wi-Fi que la PC · cualquier tecla o clic para volver", fonts["tiny"], (110, 110, 120),
              center=(w // 2, h - 18))


class InputHub:
    """Lee botones, cruceta y gatillos de cualquier mando como 'tokens'."""

    def __init__(self):
        pygame.joystick.init()
        self.pads = []
        self._prev = []
        self._refresh()

    @staticmethod
    def _active(pad) -> set:
        act = set()
        try:
            for b in range(min(pad.get_numbuttons(), 32)):
                if pad.get_button(b):
                    act.add(f"b{b}")
            for hi in range(pad.get_numhats()):
                x, y = pad.get_hat(hi)
                if x < 0: act.add(f"h{hi}L")
                if x > 0: act.add(f"h{hi}R")
                if y > 0: act.add(f"h{hi}U")
                if y < 0: act.add(f"h{hi}D")
            for ax in range(pad.get_numaxes()):
                value = float(pad.get_axis(ax))
                if value < -0.6:
                    act.add(f"a{ax}-")
                elif value > 0.6:
                    act.add(f"a{ax}+")
        except Exception:
            pass
        return act

    def _refresh(self):
        self.pads = []
        for i in range(pygame.joystick.get_count()):
            j = pygame.joystick.Joystick(i)
            j.init()
            self.pads.append(j)
        self._prev = [self._active(p) for p in self.pads]

    def pad_key(self, pi: int) -> str:
        """Identificador estable del mando: GUID + nº entre mandos iguales."""
        try:
            g = self.pads[pi].get_guid()
        except Exception:
            try:
                g = self.pads[pi].get_name()
            except Exception:
                g = "pad"
        n = 0
        for j in range(pi):
            try:
                if self.pads[j].get_guid() == g:
                    n += 1
            except Exception:
                pass
        return f"{g}#{n}"

    def pad_name(self, pi: int) -> str:
        try:
            return self.pads[pi].get_name()
        except Exception:
            return "Mando"

    def poll(self):
        if pygame.joystick.get_count() != len(self.pads):
            self._refresh()
        out = []
        for pi, pad in enumerate(self.pads):
            now = self._active(pad)
            for tok in sorted(now - self._prev[pi]):
                out.append((pi, tok))
            self._prev[pi] = now
        return out


def clock_str(sec):
    s = max(0, int(sec))
    return f"{s // 60}:{s % 60:02d}"


def draw_text(surf, text, font, color, center=None, topleft=None):
    img = font.render(str(text), True, color)
    rect = img.get_rect(center=center) if center else img.get_rect(topleft=topleft or (0, 0))
    surf.blit(img, rect)
    return rect


def vert_gradient(surf, rect, c1, c2):
    x, y, w, h = rect
    if h <= 0:
        return
    for i in range(h):
        t = i / max(1, h - 1)
        col = (
            int(c1[0] + (c2[0] - c1[0]) * t),
            int(c1[1] + (c2[1] - c1[1]) * t),
            int(c1[2] + (c2[2] - c1[2]) * t),
        )
        pygame.draw.line(surf, col, (x, y + i), (x + w - 1, y + i))


def soft_panel(surf, rect, base, edge=None, radius=16):
    """Panel con borde suave."""
    pygame.draw.rect(surf, base, rect, border_radius=radius)
    if edge:
        pygame.draw.rect(surf, edge, rect, width=2, border_radius=radius)


def draw_button(surf, rect, label, font, bg=BTN, fg=WHITE, border=None):
    pygame.draw.rect(surf, bg, rect, border_radius=10)
    pygame.draw.rect(surf, border or (90, 95, 120), rect, width=1, border_radius=10)
    # brillo superior sutil
    top = pygame.Rect(rect.x + 2, rect.y + 2, rect.w - 4, max(3, rect.h // 4))
    s = pygame.Surface((top.w, top.h), pygame.SRCALPHA)
    s.fill((255, 255, 255, 28))
    surf.blit(s, top.topleft)
    draw_text(surf, label, font, fg, center=rect.center)
    return rect


def side_point_btn(surf, rect, label, wine=True):
    """Botón de puntos estilo vino / azul marino."""
    c1 = WINE if wine else NAVY
    c2 = WINE_LIGHT if wine else NAVY_LIGHT
    # fondo degradado aproximado
    pygame.draw.rect(surf, c1, rect, border_radius=12)
    inner = pygame.Rect(rect.x + 2, rect.y + 2, rect.w - 4, rect.h // 2)
    s = pygame.Surface((inner.w, max(1, inner.h)), pygame.SRCALPHA)
    s.fill((255, 255, 255, 22))
    surf.blit(s, inner.topleft)
    pygame.draw.rect(surf, (255, 255, 255, 40) if False else (c2[0]+20, c2[1]+20, c2[2]+20), rect, width=1, border_radius=12)
    return rect



PALETTE = [
    (211, 47, 47), (30, 63, 216), (46, 125, 50), (249, 168, 37),
    (106, 27, 154), (0, 131, 143), (239, 108, 0), (55, 71, 79),
]



PAD_ACTIONS = [
    ("pts1", "+1 punto"), ("pts2", "+2 puntos"), ("pts3", "+3 puntos"),
    ("pts4", "+4 puntos"), ("pts5", "+5 puntos"),
]
TABS = ["GENERAL", "JUECES", "MANDOS", "PANTALLA", "ADMIN"]
ARROWS = {"U": "↑", "D": "↓", "L": "←", "R": "→"}
DEBOUNCE_ROW = ("Anti-rebote", "debounce_ms", (0, 1000, 50))
C_BG = (13, 14, 19)
C_LINE = (38, 42, 54)
C_TXT = (232, 235, 244)
C_MUTED = (140, 146, 164)
C_BTN = (72, 79, 102)


def token_name(t) -> str:
    if not t:
        return "sin asignar"
    if t[0] == "b":
        return f"Botón {t[1:]}"
    if t[0] == "h":
        return f"Cruceta {ARROWS.get(t[-1], t[-1])}"
    if t[0] == "a":
        return f"Gatillo / eje {t[1:-1]}"
    return t


def fmt_val(attr, val):
    if attr == "vote_window_ms":
        return f"{val / 1000:.1f} s"
    if attr == "debounce_ms":
        return "OFF" if val == 0 else f"{val} ms"
    if attr in ("block_missing", "tv_auto", "golden_point"):
        return "SÍ" if val else "NO"
    if attr == "tv_display" and val == 0:
        return "AUTO"
    if attr in ("max_point_diff", "expected_judges", "timeout_seconds") and val == 0:
        return "OFF"
    return str(val)


def get_displays():
    try:
        return list(pygame.display.get_desktop_sizes())
    except Exception:
        return []


class SettingsUI:
    def __init__(self, cfg: Config, match: "Match", url: str, inputs=None):
        self.draft = Config(**asdict(cfg))
        self.red_color = tuple(getattr(cfg, "red_color", RED))
        self.blue_color = tuple(getattr(cfg, "blue_color", BLUE))
        self.match = match
        self.url = url
        self.inputs = inputs
        self.tab = 0
        self.focus = 0
        self.editing = False
        self.listening = None  # (lado, acción) esperando un botón
        self.queue = []
        self.mside = "R"
        self.last_input = "—"
        self.pad_flash = {}
        self.tv_running = False
        self.displays = []
        self.pw = ""
        self.pw_err = False
        self.open_admin = False
        self.rows = {
            0: [
                ("Nombre rojo", "red_name", None),
                ("Nombre azul", "blue_name", None),
                ("Duración del round (s)", "round_seconds", (10, 600, 10)),
                ("Descanso (s)", "rest_seconds", (0, 300, 5)),
                ("Rounds", "rounds", (1, 5, 1)),
                ("Faltas máximas", "max_fouls", (1, 20, 1)),
                ("Diferencia de puntos", "max_point_diff", (0, 30, 1)),
                ("Tiempo muerto (s)", "timeout_seconds", (0, 180, 10)),
            ],
            1: [
                ("Jueces esperados", "expected_judges", (0, 8, 1)),
                ("Votos necesarios", "min_votes", (1, 5, 1)),
                ("Ventana entre votos", "vote_window_ms", (200, 3000, 100)),
                ("Bloquear si faltan jueces", "block_missing", (0, 1, 1)),
                ("Anti-rebote", "debounce_ms", (0, 1000, 50)),
            ],
            3: [("Abrir modo TV al detectar una TV", "tv_auto", (0, 1, 1)),
                ("Pantalla del modo TV", "tv_display", (0, 8, 1))],
        }
        self._hit = {}

    # ---------- navegación ----------
    def n_rows(self):
        if self.tab == 4:
            return 1
        return 1 + len(PAD_ACTIONS) if self.tab == 2 else len(self.rows[self.tab])

    def try_admin(self):
        # Entrar al modo admin sin contraseña (solo botón ENTRAR)
        self.open_admin = True
        self.pw_err = False
        self.pw = ""

    def cur_row(self):
        if self.tab == 4:
            return None
        if self.tab == 2:
            return DEBOUNCE_ROW if self.focus == 0 else None
        return self.rows[self.tab][self.focus]

    def set_tab(self, i):
        self.tab = i % len(TABS)
        self.focus = 0
        self.editing = False
        self.listening = None
        self.queue = []
        self.pw = ""
        self.pw_err = False

    def _map(self, side=None):
        return self.draft.pad_map_red if (side or self.mside) == "R" else self.draft.pad_map_blue

    def handle_key(self, k):
        if self.listening:
            return
        if self.tab == 4:  # modo admin (sin contraseña)
            if k == pygame.K_TAB:
                self.set_tab(self.tab + 1)
            elif k == pygame.K_RETURN:
                self.try_admin()
            return
        if self.editing:
            if k in (pygame.K_RETURN, pygame.K_ESCAPE, pygame.K_TAB):
                self.editing = False
            elif k == pygame.K_BACKSPACE:
                row = self.cur_row()
                if row and row[1] in ("red_name", "blue_name"):
                    setattr(self.draft, row[1], getattr(self.draft, row[1])[:-1])
            return
        if k == pygame.K_TAB:
            self.set_tab(self.tab + 1)
        elif k == pygame.K_UP:
            self.focus = (self.focus - 1) % self.n_rows()
        elif k == pygame.K_DOWN:
            self.focus = (self.focus + 1) % self.n_rows()
        elif k in (pygame.K_LEFT, pygame.K_RIGHT):
            d = -1 if k == pygame.K_LEFT else 1
            if self.tab == 2 and self.focus > 0:
                self.mside = "B" if self.mside == "R" else "R"
            else:
                self._step(d)
        elif k == pygame.K_RETURN:
            if self.tab == 2 and self.focus > 0:
                self.listening = (self.mside, PAD_ACTIONS[self.focus - 1][0])
            else:
                row = self.cur_row()
                if row and row[2] is None:
                    self.editing = True
        elif k in (pygame.K_DELETE, pygame.K_BACKSPACE) and self.tab == 2 and self.focus > 0:
            self._map()[PAD_ACTIONS[self.focus - 1][0]] = ""

    def handle_text(self, text: str):
        if self.tab == 4:
            if text and text.isprintable() and len(self.pw) < 24:
                self.pw += text
                self.pw_err = False
            return
        if not self.editing or self.tab != 0:
            return
        row = self.cur_row()
        if row and row[1] in ("red_name", "blue_name") and text and text.isprintable():
            cur = getattr(self.draft, row[1])
            if len(cur) < 14:
                setattr(self.draft, row[1], cur + text)

    def _step(self, d: int):
        row = self.cur_row()
        if not row or not row[2]:
            return
        _, attr, (lo, hi, step) = row
        setattr(self.draft, attr, max(lo, min(hi, getattr(self.draft, attr) + d * step)))

    # ---------- mandos ----------
    def pad_label(self, val: str) -> str:
        if not val or "|" not in val:
            return "sin asignar"
        pk, tok = val.split("|", 1)
        who = "Mando ?"
        if pk == "*":
            return f"Todos los mandos · {token_name(tok)}"
        if pk.startswith("#"):
            who = f"Mando {int(pk[1:]) + 1}"
        elif self.inputs:
            for i in range(len(self.inputs.pads)):
                if self.inputs.pad_key(i) == pk:
                    who = f"Mando {i + 1}"
                    break
            else:
                who = "Mando desconectado"
        return f"{who} · {token_name(tok)}"

    def on_pad(self, pi: int, token: str):
        name = self.inputs.pad_name(pi)[:22] if self.inputs else "Mando"
        self.last_input = f"Mando {pi + 1} ({name}) · {token_name(token)}"
        self.pad_flash[pi] = time.time()
        if self.listening:
            side, act = self.listening
            val = f"*|{token}"  # la misma distribución para todos los mandos
            for other in (self.draft.pad_map_red, self.draft.pad_map_blue):
                for k, v in list(other.items()):
                    if v.split("|", 1)[-1] == token:
                        other[k] = ""
            self._map(side)[act] = val
            self.listening = (side, self.queue.pop(0)) if self.queue else None

    # ---------- ratón ----------
    def click(self, mx, my):
        for key, rect in self._hit.items():
            if not rect.collidepoint(mx, my):
                continue
            if key == "adminok":
                self.try_admin(); return "admin"
            if key.startswith("tab"):
                self.set_tab(int(key[3:])); return "tab"
            if key.startswith("minus"):
                self.focus = int(key[5:]); self._step(-1); return "step"
            if key.startswith("plus"):
                self.focus = int(key[4:]); self._step(1); return "step"
            if key.startswith("mapclr"):
                self._map()[PAD_ACTIONS[int(key[6:]) - 1][0]] = ""; return "map"
            if key.startswith("map"):
                self.focus = int(key[3:])
                self.listening = (self.mside, PAD_ACTIONS[self.focus - 1][0]); return "map"
            if key in ("sideR", "sideB"):
                self.mside = key[-1]; self.listening = None; self.queue = []; return "side"
            if key == "assignall":
                acts = [a for a, _ in PAD_ACTIONS]
                self.listening = (self.mside, acts[0]); self.queue = acts[1:]; return "map"
            if key == "test":
                self.match.test_mode = not self.match.test_mode
                self.match.test_log = []
                self.match.last_signals = []
                self.match.votes = []
                return "test"
            if key in ("tv_on", "tv_off", "tv_win", "tv_invert", "tv_normal", "tv_primary"):
                return key
            if key.startswith("row"):
                self.focus = int(key[3:])
                row = self.cur_row()
                if self.tab == 0 and row and row[2] is None:
                    self.editing = True
                return "row"
            if key.startswith("cred"):
                self.red_color = PALETTE[int(key[4:])]; return "color"
            if key.startswith("cblue"):
                self.blue_color = PALETTE[int(key[5:])]; return "color"
            if key == "reset":
                # Restablecer = como si estuviera desde cero: nombres, colores y todo lo demás
                self.draft = Config()
                self.red_color = tuple(self.draft.red_color)
                self.blue_color = tuple(self.draft.blue_color)
                return "reset"
            if key in ("save", "cancel"):
                return key
        return None

    # ---------- dibujo (minimalista) ----------
    def _btn(self, surf, rect, label, font, active=False, color=None):
        pygame.draw.rect(surf, color or (GOLD if active else C_BTN), rect, width=1, border_radius=6)
        draw_text(surf, label, font, color or (C_TXT if active else C_MUTED), center=rect.center)

    def _rows(self, surf, fonts, x0, cw, rows, y, rh, offset=0):
        f = fonts["small"]
        for n, (label, attr, rng) in enumerate(rows):
            i = n + offset
            sel = i == self.focus
            val = getattr(self.draft, attr)
            row = pygame.Rect(x0, y, cw, rh)
            if rng:
                bs = rh - 14
                minus = pygame.Rect(row.right - 150, row.y + 7, bs, bs)
                plus = pygame.Rect(row.right - bs, row.y + 7, bs, bs)
                self._hit[f"minus{i}"] = minus
                self._hit[f"plus{i}"] = plus
            self._hit[f"row{i}"] = row
            if sel:
                pygame.draw.rect(surf, GOLD, (x0 - 12, y + 8, 3, rh - 16))
            draw_text(surf, label, f, C_TXT if sel else C_MUTED, topleft=(x0, row.centery - f.get_height() // 2))
            if rng:
                self._btn(surf, minus, "−", f)
                self._btn(surf, plus, "+", f)
                draw_text(surf, fmt_val(attr, val), f, GOLD, center=((minus.right + plus.left) // 2, row.centery))
            else:
                shown = str(val) + ("|" if self.editing and sel else "")
                draw_text(surf, shown, f, GOLD, center=(row.right - 75, row.centery))
            pygame.draw.line(surf, C_LINE, (x0, y + rh - 1), (x0 + cw, y + rh - 1))
            y += rh
        return y

    def draw(self, surf, fonts):
        w, h = surf.get_size()
        self._hit = {}
        surf.fill(C_BG)
        cw = min(w - 48, 760)
        x0 = (w - cw) // 2
        f, ft = fonts["small"], fonts["tiny"]

        # pestañas
        ty = int(h * 0.04)
        tw = cw // len(TABS)
        for i, name in enumerate(TABS):
            r = pygame.Rect(x0 + i * tw, ty, tw, f.get_height() + 14)
            act = i == self.tab
            draw_text(surf, name, f, C_TXT if act else C_MUTED, center=(r.centerx, r.y + f.get_height() // 2 + 4))
            if act:
                pygame.draw.line(surf, GOLD, (r.x + 14, r.bottom - 2), (r.right - 14, r.bottom - 2), 2)
            self._hit[f"tab{i}"] = r
        pygame.draw.line(surf, C_LINE, (x0, ty + f.get_height() + 14), (x0 + cw, ty + f.get_height() + 14))
        y0 = ty + f.get_height() + 26
        rh = max(34, int(h * 0.052))

        if self.tab == 0:
            self._draw_general(surf, fonts, x0, cw, y0, rh)
        elif self.tab == 1:
            self._draw_judges(surf, fonts, x0, cw, y0, rh)
        elif self.tab == 2:
            self._draw_pads(surf, fonts, x0, cw, y0, rh)
        elif self.tab == 3:
            self._draw_screen(surf, fonts, x0, cw, y0, rh)
        else:
            self._draw_admin(surf, fonts, x0, cw, y0, rh)

        # pie
        fy = h - 46
        bw, bh = 130, 32
        for i, (key, label, col) in enumerate((("cancel", "ATRÁS", None), ("reset", "RESTABLECER", None), ("save", "GUARDAR", GREEN))):
            r = pygame.Rect(x0 + cw - (3 - i) * (bw + 10) + 10, fy, bw, bh)
            self._btn(surf, r, label, ft, active=key == "save", color=col)
            self._hit[key] = r
        draw_text(surf, "TAB pestaña · ↑↓ fila · ←→ ajustar · Enter editar/asignar", ft, (90, 96, 114), topleft=(x0, fy + 8))

    def _draw_general(self, surf, fonts, x0, cw, y, rh):
        y = self._rows(surf, fonts, x0, cw, self.rows[0], y, rh)
        y += 10
        ft = fonts["tiny"]
        for label, cur, tag in (("Color rojo", self.red_color, "cred"), ("Color azul", self.blue_color, "cblue")):
            draw_text(surf, label, ft, C_MUTED, topleft=(x0, y + 5))
            for i, col in enumerate(PALETTE):
                r = pygame.Rect(x0 + cw - (len(PALETTE) - i) * 32, y, 24, 24)
                pygame.draw.rect(surf, col, r, border_radius=12)
                if col == cur:
                    pygame.draw.rect(surf, WHITE, r.inflate(6, 6), width=2, border_radius=15)
                self._hit[f"{tag}{i}"] = r
            y += 36

    def _draw_judges(self, surf, fonts, x0, cw, y, rh):
        f, ft = fonts["small"], fonts["tiny"]
        y = self._rows(surf, fonts, x0, cw, self.rows[1], y, rh)
        d = self.draft
        m = self.match
        status = m.judge_status()
        jc = len(status)
        need = 1 if jc <= 1 else min(max(1, d.min_votes), jc)
        win = d.vote_window_ms / 1000

        y += 14
        draw_text(surf, "PRUEBA DE ENVÍO", ft, C_MUTED, topleft=(x0, y + 8))
        tb = pygame.Rect(x0 + cw - 150, y, 150, 30)
        self._btn(surf, tb, "DETENER" if m.test_mode else "INICIAR PRUEBA", ft, active=m.test_mode,
                  color=ORANGE if m.test_mode else None)
        self._hit["test"] = tb
        y += 42

        # estado de conexión
        if d.expected_judges > 0 and jc < d.expected_judges:
            col, txt = ORANGE, f"Conectados {jc}/{d.expected_judges} · faltan {d.expected_judges - jc}"
        elif d.expected_judges > 0:
            col, txt = GREEN, f"Todos conectados {jc}/{d.expected_judges}"
        else:
            col, txt = C_MUTED, f"Conectados: {jc}"
        draw_text(surf, txt, f, col, topleft=(x0, y))
        xx = x0 + cw
        for jid, age in reversed(status[:8]):
            ok = age < 2.5
            lab = f"{jid}"
            tw_ = ft.size(lab)[0]
            xx -= tw_ + 26
            pygame.draw.circle(surf, GREEN if ok else ORANGE, (xx + 6, y + f.get_height() // 2), 5)
            draw_text(surf, lab, ft, C_TXT if ok else ORANGE, topleft=(xx + 16, y + 3))
        y += f.get_height() + 12

        if not m.test_mode:
            draw_text(surf, f"Un punto cuenta si {need} juez(ces) distinto(s) (celulares o mandos) envían la misma señal "
                            f"con menos de {win:.1f} s entre ellas.", ft, C_MUTED, topleft=(x0, y))
            y += ft.get_height() + 6
            draw_text(surf, f"Los jueces entran a  {self.url}", ft, C_MUTED, topleft=(x0, y))
            y += ft.get_height() + 14
            room = surf.get_height() - y - 70
            if self.url.startswith("http") and room >= 110:
                tot = draw_qr(surf, self.url, x0, y, min(200, room))
                if tot:
                    draw_text(surf, "Escanea con la cámara del celular", ft, C_MUTED,
                              topleft=(x0 + tot + 16, y + tot // 2 - ft.get_height() // 2))
                    draw_text(surf, "(en el marcador: tecla J = QR grande)", ft, (90, 96, 114),
                              topleft=(x0 + tot + 16, y + tot // 2 + ft.get_height()))
            return
        draw_text(surf, f"Pide a {max(2, need)} jueces (celulares o mandos) pulsar el MISMO botón (ej. +1 ROJO). No suma al marcador.",
                  ft, C_MUTED, topleft=(x0, y))
        y += ft.get_height() + 10
        now = time.time()
        sigs = list(m.last_signals[:4])
        if not sigs:
            draw_text(surf, "Esperando señales de los teléfonos…", f, C_MUTED, topleft=(x0, y))
        for sg in sigs:
            lab = f"+{sg['v']}" if sg["kind"] == "P" else "FALTA"
            side = "ROJO" if sg["red"] else "AZUL"
            if sg["ok"]:
                st_, col = "✓ válida", GREEN
            elif now - sg["t"] > win:
                st_, col = "sin pareja a tiempo", (255, 110, 110)
            else:
                st_, col = "esperando otro juez…", ORANGE
            draw_text(surf, f"{sg['from']}  ·  {lab} {side}", ft, C_TXT, topleft=(x0, y))
            draw_text(surf, st_, ft, col, topleft=(x0 + 230, y))
            y += ft.get_height() + 6
        if m.test_log:
            t = m.test_log[0]
            y += 4
            draw_text(surf, f"Última validación: {' + '.join(t['who'])} en {t['dt']:.2f} s", f, GREEN, topleft=(x0, y))

    def _draw_pads(self, surf, fonts, x0, cw, y, rh):
        f, ft = fonts["small"], fonts["tiny"]
        pads = self.inputs.pads if self.inputs else []
        if not pads:
            draw_text(surf, "No se detectan mandos · conecta uno por USB o Bluetooth", ft, ORANGE, topleft=(x0, y + 4))
            y += 28
        now = time.time()
        for pi in range(min(len(pads), 4)):
            lit = now - self.pad_flash.get(pi, 0) < 0.4
            pygame.draw.circle(surf, GREEN if lit else (60, 66, 84), (x0 + 6, y + 10), 5)
            pad = self.inputs.pads[pi]
            try:
                details = f"{pad.get_numbuttons()} botones · {pad.get_numaxes()} ejes · {pad.get_numhats()} crucetas"
            except Exception:
                details = "controles detectados"
            draw_text(surf, f"Mando {pi + 1} · {self.inputs.pad_name(pi)[:28]}", ft, C_TXT, topleft=(x0 + 20, y + 3))
            y += 17
            draw_text(surf, details, fonts["tiny"], C_MUTED, topleft=(x0 + 20, y))
            y += 19
        y += 8

        # selector de lado
        for key, label, col in (("sideR", "ROJO", RED), ("sideB", "AZUL", BLUE)):
            r = pygame.Rect(x0 + (0 if key == "sideR" else 96), y, 86, 30)
            act = self.mside == key[-1]
            pygame.draw.rect(surf, col if act else C_LINE, r, width=2 if act else 1, border_radius=6)
            draw_text(surf, label, ft, C_TXT if act else C_MUTED, center=r.center)
            self._hit[key] = r
        ar = pygame.Rect(x0 + cw - 150, y, 150, 30)
        self._btn(surf, ar, "ASIGNAR TODO", ft)
        self._hit["assignall"] = ar
        y += 40

        y = self._rows(surf, fonts, x0, cw, [DEBOUNCE_ROW], y, rh - 4, offset=0)
        mp = self._map()
        for k, (act, label) in enumerate(PAD_ACTIONS, start=1):
            sel = k == self.focus
            row = pygame.Rect(x0, y, cw, rh - 4)
            asg = pygame.Rect(row.right - 150, row.y + 5, 100, row.h - 10)
            clr = pygame.Rect(row.right - 40, row.y + 5, 40, row.h - 10)
            self._hit[f"map{k}"] = asg
            self._hit[f"mapclr{k}"] = clr
            if sel:
                pygame.draw.rect(surf, GOLD, (x0 - 12, y + 8, 3, row.h - 16))
            draw_text(surf, label, f, C_TXT if sel else C_MUTED, topleft=(x0, row.centery - f.get_height() // 2))
            listening = self.listening == (self.mside, act)
            val = "pulsa un botón…" if listening else self.pad_label(mp.get(act, ""))
            draw_text(surf, val, ft, ORANGE if listening else GOLD, topleft=(x0 + 190, row.centery - ft.get_height() // 2))
            self._btn(surf, asg, "ASIGNAR", ft)
            self._btn(surf, clr, "X", ft)
            pygame.draw.line(surf, C_LINE, (x0, y + row.h), (x0 + cw, y + row.h))
            y += rh - 4
        draw_text(surf, f"Última entrada:  {self.last_input}", ft, C_MUTED, topleft=(x0, y + 10))

    def _draw_admin(self, surf, fonts, x0, cw, y, rh):
        f, ft = fonts["small"], fonts["tiny"]
        draw_text(surf, "MODO ADMIN", f, C_TXT, topleft=(x0, y))
        y += f.get_height() + 6
        draw_text(surf, "Muestra el código fuente completo del programa.", ft, C_MUTED, topleft=(x0, y))
        y += ft.get_height() + 24
        draw_text(surf, "Pulsa ENTRAR para abrir el modo admin.", ft, C_MUTED, topleft=(x0, y))
        y += ft.get_height() + 16
        ok = pygame.Rect(x0, y, 160, 42)
        self._btn(surf, ok, "ENTRAR", f, active=True, color=GREEN)
        self._hit["adminok"] = ok
        y += 56

    def _draw_screen(self, surf, fonts, x0, cw, y, rh):
        f, ft = fonts["small"], fonts["tiny"]
        y = self._rows(surf, fonts, x0, cw, self.rows[3], y, rh)
        y += 14
        n = len(self.displays)
        draw_text(surf, "PANTALLAS", ft, C_MUTED, topleft=(x0, y))
        y += ft.get_height() + 8
        want = self.draft.tv_display
        used = (min(want, n) - 1) if want > 0 else (1 if n >= 2 else 0)
        for i, sz in enumerate(self.displays):
            tag = ("   (principal)" if i == 0 else "") + ("   ← TV" if i == used and n >= 2 else "")
            draw_text(surf, f"{i + 1}  ·  {sz[0]} × {sz[1]}{tag}", f, GOLD if (i == used and n >= 2) else C_TXT,
                      topleft=(x0, y))
            y += f.get_height() + 6
        y += 8
        if n >= 2:
            draw_text(surf, "● TV / monitor detectado", f, GREEN, topleft=(x0, y))
        else:
            draw_text(surf, "● Sin TV · conéctese a una TV o monitor", f, ORANGE, topleft=(x0, y))
        y += f.get_height() + 22

        if self.tv_running:
            b1 = pygame.Rect(x0, y, 230, 38)
            self._btn(surf, b1, "CERRAR MODO TV", f, active=True, color=ORANGE)
            self._hit["tv_off"] = b1
            y += 52
        else:
            b2 = pygame.Rect(x0, y, cw, 42)
            self._btn(surf, b2, "MODO TV EN VENTANA (recomendado)", f, active=True, color=GREEN)
            self._hit["tv_win"] = b2
            y += 56
        b3 = pygame.Rect(x0, y, cw, 38)
        self._btn(surf, b3, "INVERTIR MONITOR TV  (1 ↔ 2)", f, active=True, color=GOLD)
        self._hit["tv_invert"] = b3
        y += 46
        # Cambiar principal
        role = getattr(self.draft, "primary_role", 0)
        lab = "PRINCIPAL: ahora TV (grande)  ·  pulsa para PC" if role else "PRINCIPAL: ahora PC (pequeña)  ·  pulsa para TV"
        b5 = pygame.Rect(x0, y, cw, 38)
        self._btn(surf, b5, lab, f, active=True, color=NAVY_LIGHT)
        self._hit["tv_primary"] = b5
        y += 46
        b4 = pygame.Rect(x0, y, cw, 38)
        self._btn(surf, b4, "VOLVER A LA NORMALIDAD", f, active=True, color=ORANGE)
        self._hit["tv_normal"] = b4
        y += 52
        draw_text(surf, "Esc cierra el modo TV.  La TV se elige sola (pantalla más grande).", ft, C_MUTED, topleft=(x0, y))
        y += ft.get_height() + 4
        draw_text(surf, "MODO TV no empuja ventanas si ya estás en extendido. NORMALIDAD restaura PC principal.", ft,
                  C_MUTED, topleft=(x0, y))




# ======================= MODO TV (proceso aparte) =======================
def _dpi_aware():
    """Windows: usar píxeles reales para que las coordenadas de cada pantalla coincidan."""
    if os.name != "nt":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def restore_single_display():
    """Intenta devolver el escritorio a una sola pantalla al cerrar TKD 01.

    Windows: usa DisplaySwitch /internal.
    Linux/X11: conserva la salida marcada como principal y desactiva las demás.
    No reinicia el equipo ni cambia resoluciones guardadas por el usuario.
    """
    try:
        if os.name == "nt":
            candidates = [
                os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "DisplaySwitch.exe"),
                "DisplaySwitch.exe",
            ]
            for cmd in candidates:
                try:
                    result = subprocess.run([cmd, "/internal"], timeout=8,
                                            stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL, check=False)
                    if result.returncode == 0:
                        log_error("Pantallas restauradas: Windows /internal")
                        return True
                except (OSError, subprocess.SubprocessError):
                    continue
            log_error("No se pudo ejecutar DisplaySwitch /internal")
            return False

        if sys.platform.startswith("linux") and os.environ.get("DISPLAY"):
            query = subprocess.run(["xrandr", "--query"], capture_output=True,
                                   text=True, timeout=5, check=False)
            if query.returncode != 0:
                log_error("No se pudo consultar xrandr para restaurar pantalla")
                return False
            active = xrandr_outputs()
            if not active:
                log_error("xrandr no encontró pantallas activas; no se cambió nada")
                return False
            primary = next((item for item in active if item[1]), active[0])
            name = primary[0]
            result = subprocess.run(["xrandr", "--output", name, "--primary", "--auto", "--pos", "0x0"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=5, check=False)
            if result.returncode != 0:
                log_error(f"xrandr no pudo restaurar la pantalla principal: {name}")
                return False
            for item in active:
                if item[0] != name:
                    subprocess.run(["xrandr", "--output", item[0], "--off"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=5, check=False)
            log_error(f"Pantallas restauradas: Linux/X11, principal={name}")
            return True
    except Exception as e:
        log_error(f"restore_single_display: {e}")
    return False


def monitor_rects():
    """[(x, y, ancho, alto, es_principal)] de las pantallas. Principal primero si se puede."""
    out = []
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class MONITORINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                            ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
            proc_t = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.POINTER(wintypes.RECT), ctypes.c_void_p)

            def cb(hmon, hdc, lprc, lparam):
                mi = MONITORINFO()
                mi.cbSize = ctypes.sizeof(MONITORINFO)
                ctypes.windll.user32.GetMonitorInfoW(ctypes.c_void_p(hmon), ctypes.byref(mi))
                r = mi.rcMonitor
                out.append((r.left, r.top, r.right - r.left, r.bottom - r.top, bool(mi.dwFlags & 1)))
                return 1
            ctypes.windll.user32.EnumDisplayMonitors(None, None, proc_t(cb), 0)
        elif sys.platform.startswith("linux"):
            txt = subprocess.run(["xrandr", "--query"], capture_output=True, text=True, timeout=4).stdout
            out = parse_xrandr(txt)
    except Exception as e:
        log_error(f"monitor_rects: {e}")
        return []
    return sorted(out, key=lambda m: (not m[4], m[0], m[1]))


def parse_xrandr(txt: str):
    """Devuelve [(x, y, w, h, primary), ...] desde salida de xrandr --query."""
    out = []
    for line in txt.splitlines():
        # Ej: HDMI-1 connected primary 1920x1080+0+0 ...
        # Ej: HDMI-1 connected 1920x1080+1600+0 ...
        m = re.search(
            r"^(\S+)\s+connected(?:\s+primary)?\s+(\d+)x(\d+)\+(-?\d+)\+(-?\d+)",
            line
        )
        if m:
            primary = " primary" in line.split(" connected")[1][:20] if " connected" in line else False
            # Más fiable:
            primary = bool(re.search(r"connected\s+primary\s+", line))
            out.append((int(m.group(4)), int(m.group(5)), int(m.group(2)), int(m.group(3)), primary))
    return out


def xrandr_outputs():
    """Lista [(nombre, primary, x, y, w, h), ...] de salidas connected con modo activo."""
    out = []
    try:
        txt = subprocess.run(["xrandr", "--query"], capture_output=True, text=True, timeout=4).stdout
        for line in txt.splitlines():
            m = re.search(
                r"^(\S+)\s+connected(?:\s+primary)?\s+(\d+)x(\d+)\+(-?\d+)\+(-?\d+)",
                line
            )
            if m:
                primary = bool(re.search(r"connected\s+primary\s+", line))
                out.append((m.group(1), primary, int(m.group(4)), int(m.group(5)),
                            int(m.group(2)), int(m.group(3))))
    except Exception as e:
        log_error(f"xrandr_outputs: {e}")
    return out


def pick_monitor(mons, choice: int):
    """choice -1 = secundaria (no principal); N = índice N."""
    if not mons:
        return None
    if choice >= 0:
        return mons[min(choice, len(mons) - 1)]
    for m in mons:
        if not m[4]:
            return m
    return mons[-1] if len(mons) > 1 else mons[0]



def _xrandr_connected():
    """Lista de salidas connected: dict name, primary, w, h, x, y, pixels, is_laptop_name."""
    out = []
    try:
        txt = subprocess.check_output(["xrandr", "--query"], text=True, stderr=subprocess.DEVNULL, timeout=4)
    except Exception as e:
        log_error(f"_xrandr_connected: {e}")
        return out
    for line in txt.splitlines():
        m = re.match(r"^(\S+)\s+connected(.*)$", line)
        if not m:
            continue
        name, rest = m.group(1), m.group(2)
        primary = bool(re.search(r"\bprimary\b", rest))
        geo = re.search(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", rest)
        if geo:
            w, h, x, y = map(int, geo.groups())
        else:
            # sin modo activo: mirar modo preferido (*)
            w = h = x = y = 0
        nlow = name.lower()
        is_lap = nlow.startswith(("edp", "lvds", "dsi", "emdp"))
        out.append({
            "name": name, "primary": primary,
            "w": w, "h": h, "x": x, "y": y,
            "pixels": w * h, "is_laptop": is_lap,
        })
    return out


def detect_display_roles(primary_role: int = 0):
    """Decide PC vs TV por tamaño (y nombre eDP).

    - La más grande (más píxeles) = TV
    - La más pequeña o eDP/LVDS = PC
    primary_role: 0 = PC principal, 1 = TV principal
    Devuelve dict: pc, tv, primary_name, tv_name, pc_idx, tv_idx (índices pygame aprox 0..)
    """
    roles = {"pc": None, "tv": None, "primary_name": None, "tv_name": None, "pc_idx": 0, "tv_idx": 1}
    if sys.platform.startswith("linux"):
        outs = _xrandr_connected()
        if not outs:
            return roles
        # Preferir eDP como PC si existe
        lap = next((o for o in outs if o["is_laptop"]), None)
        rest = [o for o in outs if o is not lap] if lap else list(outs)
        if lap and rest:
            # TV = la de más píxeles entre el resto (o la única)
            tv = max(rest, key=lambda o: o["pixels"] or 0)
            pc = lap
        elif len(outs) >= 2:
            ordered = sorted(outs, key=lambda o: o["pixels"] or 0)
            pc, tv = ordered[0], ordered[-1]  # pequeña=PC, grande=TV
            if pc["pixels"] == tv["pixels"]:
                pc, tv = outs[0], outs[1]
        else:
            pc = outs[0]
            tv = outs[0]
        if primary_role == 1:
            primary_name, other = tv["name"], pc["name"]
        else:
            primary_name, other = pc["name"], tv["name"]
        roles.update({
            "pc": pc, "tv": tv,
            "primary_name": primary_name, "tv_name": tv["name"],
            "pc_name": pc["name"],
        })
        # Índices pygame: orden get_desktop_sizes suele ser el del servidor X
        try:
            sizes = list(pygame.display.get_desktop_sizes()) if pygame.display.get_init() else []
        except Exception:
            sizes = []
        if sizes and tv["pixels"]:
            # buscar índice cuya resolución coincida con TV
            for i, (sw, sh) in enumerate(sizes):
                if sw == tv["w"] and sh == tv["h"]:
                    roles["tv_idx"] = i
                if sw == pc["w"] and sh == pc["h"]:
                    roles["pc_idx"] = i
            if roles["tv_idx"] == roles["pc_idx"] and len(sizes) >= 2:
                roles["tv_idx"] = 1 if roles["pc_idx"] == 0 else 0
        log_error(f"roles: PC={pc['name']} {pc['w']}x{pc['h']} TV={tv['name']} {tv['w']}x{tv['h']} primary={primary_name}")
        return roles

    # Windows / otros: por tamaños pygame
    try:
        sizes = list(pygame.display.get_desktop_sizes())
    except Exception:
        sizes = []
    if len(sizes) >= 2:
        pix = [(i, w * h, w, h) for i, (w, h) in enumerate(sizes)]
        pix_sorted = sorted(pix, key=lambda t: t[1])
        pc_i, tv_i = pix_sorted[0][0], pix_sorted[-1][0]
        roles["pc_idx"], roles["tv_idx"] = pc_i, tv_i
        if primary_role == 1:
            roles["pc_idx"], roles["tv_idx"] = tv_i, pc_i  # si TV es "principal" del sistema, el marcador igual va a la grande
            roles["tv_idx"] = tv_i  # marcador siempre en la más grande
        log_error(f"roles win/sizes: {sizes} pc_idx={roles['pc_idx']} tv_idx={roles['tv_idx']}")
    return roles


def apply_display_layout(primary_role: int = 0, force_reorder: bool = False):
    """Activa extendido sin voltear de más. PC/TV por tamaño. primary según primary_role."""
    if os.name == "nt":
        try:
            subprocess.Popen(
                ["DisplaySwitch.exe", "/extend"],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            time.sleep(0.35)  # antes 1.0: retrasaba la apertura del modo TV
            log_error("layout Windows: /extend")
        except Exception as e:
            log_error(f"layout Windows: {e}")
        return detect_display_roles(primary_role)

    if not sys.platform.startswith("linux"):
        return detect_display_roles(primary_role)

    roles = detect_display_roles(primary_role)
    pc, tv = roles.get("pc"), roles.get("tv")
    if not pc:
        return roles
    primary_name = roles["primary_name"]
    # Si solo hay una salida
    if not tv or tv["name"] == pc["name"]:
        try:
            subprocess.call(
                ["xrandr", "--output", pc["name"], "--primary", "--auto"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
            )
        except Exception as e:
            log_error(f"layout single: {e}")
        return roles

    # ¿Ya están ambos activos y sin need reorder?
    both_on = pc["pixels"] > 0 and tv["pixels"] > 0
    try:
        if force_reorder or not both_on:
            # Un solo layout estable: no alternar left/right en cada MODO TV
            if primary_role == 0:
                cmd = [
                    "xrandr",
                    "--output", pc["name"], "--primary", "--auto",
                    "--output", tv["name"], "--auto", "--right-of", pc["name"],
                ]
            else:
                cmd = [
                    "xrandr",
                    "--output", tv["name"], "--primary", "--auto",
                    "--output", pc["name"], "--auto", "--left-of", tv["name"],
                ]
            subprocess.call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=6)
            time.sleep(0.35)  # antes 0.9: menos espera al abrir TV
            log_error(f"layout force: {' '.join(cmd)}")
        else:
            # Solo asegurar primary, sin mover posiciones (no desplaza ventanas)
            if primary_name:
                subprocess.call(
                    ["xrandr", "--output", primary_name, "--primary"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=4,
                )
                log_error(f"layout soft primary={primary_name}")
    except Exception as e:
        log_error(f"apply_display_layout: {e}")
    return detect_display_roles(primary_role)


def restore_normal_displays(settings=None):
    """Vuelve a la normalidad: PC (pantalla pequeña) principal, TV grande al lado, sin mirror."""
    try:
        role = 0
        if settings is not None:
            settings.draft.tv_display = 0
            settings.draft.primary_role = 0
            match.cfg.tv_display = 0
            match.cfg.primary_role = 0
            match.save()
            role = 0
        apply_display_layout(primary_role=role, force_reorder=True)
        log_error("restore_normal_displays OK")
    except Exception as e:
        log_error(f"restore_normal_displays: {e}")


def invert_displays(settings=None):
    """Solo cambia en qué monitor se abre el modo TV (1 ↔ 2). No mueve el layout."""
    try:
        if settings is None:
            return
        cur = settings.draft.tv_display
        if cur <= 0:
            settings.draft.tv_display = 1
        elif cur == 1:
            settings.draft.tv_display = 2
        else:
            settings.draft.tv_display = 1
        match.cfg.tv_display = settings.draft.tv_display
        match.save()
        log_error(f"invert: tv_display → {settings.draft.tv_display}")
    except Exception as e:
        log_error(f"invert_displays: {e}")


def toggle_primary_role(settings=None):
    """Cambia cuál es la pantalla principal: PC (pequeña) <-> TV (grande)."""
    try:
        if settings is None:
            return
        settings.draft.primary_role = 0 if settings.draft.primary_role else 1
        match.cfg.primary_role = settings.draft.primary_role
        match.save()
        apply_display_layout(primary_role=settings.draft.primary_role, force_reorder=True)
        log_error(f"toggle_primary_role → {settings.draft.primary_role}")
    except Exception as e:
        log_error(f"toggle_primary_role: {e}")


def arrange_tv_left():
    """En Kali/Linux: activa la TV, la pone a la IZQUIERDA y deja la laptop como primary.
    Devuelve (mons, target_tv).
    """
    try:
        if sys.platform.startswith("linux"):
            outs = xrandr_outputs()
            log_error(f"xrandr outs antes: {outs}")
            primary_name = None
            secondary_names = []
            # También salidas connected sin modo (aún no activadas)
            txt = subprocess.run(["xrandr", "--query"], capture_output=True, text=True, timeout=4).stdout
            all_connected = []
            for line in txt.splitlines():
                m = re.match(r"^(\S+)\s+connected", line)
                if m:
                    name = m.group(1)
                    all_connected.append(name)
                    if " primary" in line:
                        primary_name = name
            if not primary_name and outs:
                primary_name = next((n for n, p, *_ in outs if p), outs[0][0])
            secondary_names = [n for n in all_connected if n != primary_name]
            if primary_name and secondary_names:
                tv = secondary_names[0]
                # 1) Activar TV con su modo preferido
                subprocess.call(["xrandr", "--output", tv, "--auto"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(0.4)
                # 2) TV a la izquierda, laptop primary a la derecha
                subprocess.call(
                    ["xrandr",
                     "--output", tv, "--auto", "--left-of", primary_name,
                     "--output", primary_name, "--primary", "--auto"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                time.sleep(1.0)
            mons = monitor_rects()
            log_error(f"xrandr mons después: {mons}")
            target = pick_monitor(mons, -1)
            return mons, target
        elif os.name == "nt":
            try:
                subprocess.Popen(
                    ["DisplaySwitch.exe", "/extend"],
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                time.sleep(1.0)
            except Exception:
                pass
            mons = monitor_rects()
            return mons, pick_monitor(mons, -1)
    except Exception as e:
        log_error(f"arrange_tv_left: {e}")
    mons = monitor_rects()
    return mons, pick_monitor(mons, -1)


def _linux_move_window(title: str, x: int, y: int, w: int, h: int):
    """Mueve la ventana por título con wmctrl o xdotool (Kali)."""
    # No declarar éxito si la herramienta existe pero no pudo mover la ventana.
    try:
        result = subprocess.run(
            ["wmctrl", "-r", title, "-e", f"0,{x},{y},{w},{h}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
            check=False,
        )
        if result.returncode == 0:
            return True
    except (OSError, subprocess.SubprocessError):
        pass
    # xdotool devuelve un ID por línea; actuar sobre cada ventana encontrada.
    try:
        found = subprocess.run(
            ["xdotool", "search", "--name", title], capture_output=True,
            text=True, stderr=subprocess.DEVNULL, timeout=2, check=False,
        )
        if found.returncode != 0 or not found.stdout.strip():
            return False
        moved = False
        for window_id in found.stdout.split():
            result = subprocess.run(
                ["xdotool", "windowmove", window_id, str(x), str(y),
                 "windowsize", window_id, str(w), str(h)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
                check=False,
            )
            moved = moved or result.returncode == 0
        return moved
    except (OSError, subprocess.SubprocessError):
        return False


def _linux_fullscreen(title: str) -> bool:
    """Pide al gestor de ventanas que la ventana ocupe TODA la pantalla donde está.
    No cambia la resolución ni la frecuencia, así que la opción de 60 Hz se conserva."""
    try:
        r = subprocess.run(
            ["wmctrl", "-r", title, "-b", "add,fullscreen"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2, check=False,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _ensure_tv_60hz_linux():
    """Elige en la TV el modo de mayor resolución que admita ~60 Hz (X11/Kali)."""
    try:
        roles = detect_display_roles()
        tv = roles.get("tv") or {}
        output = tv.get("name")
        if not output:
            log_error("60Hz Linux: no se identificó salida TV; no se cambia nada")
            return False
        txt = subprocess.run(["xrandr", "--query"], capture_output=True, text=True,
                             timeout=5, check=False).stdout
        lines = txt.splitlines()
        start = next((i for i, line in enumerate(lines)
                      if re.match(r"^" + re.escape(output) + r"\s+connected\b", line)), None)
        if start is None:
            log_error(f"60Hz Linux: salida {output} no conectada")
            return False
        candidates=[]
        for line in lines[start+1:]:
            if line and not line[0].isspace():
                break
            m=re.match(r"\s+(\d+)x(\d+)\s+(.+)$", line)
            if not m:
                continue
            w,h=map(int,m.group(1,2))
            # Las frecuencias de xrandr pueden incluir * (actual) y + (preferida).
            for token in re.findall(r"(?<![\w.])(\d+(?:\.\d+)?)\s*([*+]?)", m.group(3)):
                hz=float(token[0])
                if 59.0 <= hz <= 61.0:
                    candidates.append((w*h, w, h, hz))
        if not candidates:
            log_error(f"60Hz Linux: {output} no anuncia modos de 59-61 Hz; se conserva configuración actual")
            return False
        # Mayor resolución a 60 Hz; en empate, frecuencia más cercana a 60.
        _,w,h,hz=max(candidates, key=lambda x:(x[0],-abs(x[3]-60.0)))
        cmd=["xrandr","--output",output,"--mode",f"{w}x{h}","--rate",f"{hz:.2f}"]
        result=subprocess.run(cmd,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,
                              text=True,timeout=8,check=False)
        if result.returncode:
            log_error(f"60Hz Linux: fallo al aplicar {cmd}: {result.stderr.strip()}")
            return False
        time.sleep(0.35)
        verify=subprocess.run(["xrandr","--query"],capture_output=True,text=True,timeout=5).stdout
        active=False
        in_tv=False
        for line in verify.splitlines():
            if re.match(r"^"+re.escape(output)+r"\s+connected\b",line):
                in_tv=True
            elif line and not line[0].isspace():
                in_tv=False
            elif in_tv:
                m=re.match(r"\s+(\d+)x(\d+)\s+(.+)$",line)
                if m and (int(m.group(1)),int(m.group(2)))==(w,h):
                    active=any(mark=="*" and 59.0<=float(rate)<=61.0
                               for rate,mark in re.findall(r"(?<![\w.])(\d+(?:\.\d+)?)\s*([*+]?)",m.group(3)))
        log_error(f"60Hz Linux: {output} {w}x{h} @~{hz:.2f}; verificación={'OK' if active else 'no confirmada'}")
        return active
    except Exception as e:
        log_error(f"_ensure_tv_60hz_linux: {e}")
        return False


def _ensure_tv_60hz_windows():
    """Busca y prueba modos de la pantalla secundaria con la API nativa de Windows."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        class DISPUNION(ctypes.Union):
            _fields_=[("dmPosition", wintypes.POINT), ("dmDisplayOrientation", wintypes.DWORD),
                      ("dmDisplayFixedOutput", wintypes.DWORD)]
        class DISPUNION2(ctypes.Union):
            _fields_=[("dmDisplayFlags",wintypes.DWORD),("dmDisplayFrequency",wintypes.DWORD),
                      ("dmNup",wintypes.DWORD),("dmICMMethodUnion",wintypes.DWORD)]
        class DEVMODEW(ctypes.Structure):
            _anonymous_=("u1","u2")
            _fields_=[("dmDeviceName",wintypes.WCHAR*32),("dmSpecVersion",wintypes.WORD),
                ("dmDriverVersion",wintypes.WORD),("dmSize",wintypes.WORD),
                ("dmDriverExtra",wintypes.WORD),("dmFields",wintypes.DWORD),
                ("u1",DISPUNION),("dmColor",ctypes.c_short),("dmDuplex",ctypes.c_short),
                ("dmYResolution",ctypes.c_short),("dmTTOption",ctypes.c_short),
                ("dmCollate",ctypes.c_short),("dmFormName",wintypes.WCHAR*32),
                ("dmLogPixels",wintypes.WORD),("dmBitsPerPel",wintypes.DWORD),
                ("dmPelsWidth",wintypes.DWORD),("dmPelsHeight",wintypes.DWORD),
                ("u2",DISPUNION2),
                ("dmICMMethod",wintypes.DWORD),("dmICMIntent",wintypes.DWORD),
                ("dmMediaType",wintypes.DWORD),("dmDitherType",wintypes.DWORD),
                ("dmReserved1",wintypes.DWORD),("dmReserved2",wintypes.DWORD),
                ("dmPanningWidth",wintypes.DWORD),("dmPanningHeight",wintypes.DWORD)]
        class DISPLAY_DEVICEW(ctypes.Structure):
            _fields_=[("cb",wintypes.DWORD),("DeviceName",wintypes.WCHAR*32),
                      ("DeviceString",wintypes.WCHAR*128),("StateFlags",wintypes.DWORD),
                      ("DeviceID",wintypes.WCHAR*128),("DeviceKey",wintypes.WCHAR*128)]
        user=ctypes.windll.user32
        user.EnumDisplayDevicesW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,ctypes.POINTER(DISPLAY_DEVICEW),wintypes.DWORD]
        user.EnumDisplayDevicesW.restype=wintypes.BOOL
        user.EnumDisplaySettingsW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,ctypes.POINTER(DEVMODEW)]
        user.EnumDisplaySettingsW.restype=wintypes.BOOL
        user.ChangeDisplaySettingsExW.argtypes=[wintypes.LPCWSTR,ctypes.POINTER(DEVMODEW),wintypes.HWND,wintypes.DWORD,wintypes.LPVOID]
        user.ChangeDisplaySettingsExW.restype=wintypes.LONG
        ENUM_CURRENT_SETTINGS=0xFFFFFFFF; CDS_TEST=2; CDS_FULLSCREEN=4
        DM_PELSWIDTH=0x80000; DM_PELSHEIGHT=0x100000; DM_DISPLAYFREQUENCY=0x400000
        DISPLAY_DEVICE_ATTACHED_TO_DESKTOP=1; DISPLAY_DEVICE_PRIMARY_DEVICE=4
        devices=[]
        i=0
        while True:
            d=DISPLAY_DEVICEW(); d.cb=ctypes.sizeof(d)
            if not user.EnumDisplayDevicesW(None,i,ctypes.byref(d),0): break
            if d.StateFlags & DISPLAY_DEVICE_ATTACHED_TO_DESKTOP:
                devices.append(d)
            i+=1
        target=next((d for d in devices if not (d.StateFlags & DISPLAY_DEVICE_PRIMARY_DEVICE)),None)
        if target is None:
            log_error("60Hz Windows: no se encontró una pantalla secundaria")
            return False
        modes=[]; idx=0
        while True:
            dm=DEVMODEW(); dm.dmSize=ctypes.sizeof(dm)
            if not user.EnumDisplaySettingsW(target.DeviceName,idx,ctypes.byref(dm)): break
            if 59 <= dm.dmDisplayFrequency <= 61 and dm.dmPelsWidth and dm.dmPelsHeight:
                modes.append((dm.dmPelsWidth*dm.dmPelsHeight,dm.dmPelsWidth,dm.dmPelsHeight,dm.dmDisplayFrequency))
            idx+=1
        for _,w,h,hz in sorted(modes,reverse=True):
            dm=DEVMODEW(); dm.dmSize=ctypes.sizeof(dm)
            dm.dmPelsWidth=w; dm.dmPelsHeight=h; dm.dmDisplayFrequency=hz
            dm.dmFields=DM_PELSWIDTH|DM_PELSHEIGHT|DM_DISPLAYFREQUENCY
            if user.ChangeDisplaySettingsExW(target.DeviceName,ctypes.byref(dm),None,CDS_TEST,None)==0:
                result=user.ChangeDisplaySettingsExW(target.DeviceName,ctypes.byref(dm),None,CDS_FULLSCREEN,None)
                if result==0:
                    time.sleep(0.4)
                    current=DEVMODEW(); current.dmSize=ctypes.sizeof(current)
                    ok=bool(user.EnumDisplaySettingsW(target.DeviceName,ENUM_CURRENT_SETTINGS,ctypes.byref(current))
                            and current.dmDisplayFrequency>=59 and current.dmPelsWidth==w and current.dmPelsHeight==h)
                    log_error(f"60Hz Windows: {target.DeviceName} {w}x{h}@{hz}; verificación={'OK' if ok else 'no confirmada'}")
                    if ok: return True
        log_error(f"60Hz Windows: {target.DeviceName} no tiene un modo seguro de 59-61 Hz; no se fuerza 30 Hz")
        return False
    except Exception as e:
        log_error(f"_ensure_tv_60hz_windows: {e}")
        return False


def ensure_tv_60hz():
    """Aplica la mayor resolución anunciada por la TV que mantenga aproximadamente 60 Hz."""
    if sys.platform.startswith("linux"):
        return _ensure_tv_60hz_linux()
    if os.name == "nt":
        return _ensure_tv_60hz_windows()
    return False


def run_tv(display_idx: int = -1, windowed: bool = False, port: int = HTTP_PORT):
    """Marcador en la TV (pygame multi-monitor). Elige la pantalla más grande si display_idx < 0."""
    _dpi_aware()
    os.environ.pop("SDL_VIDEO_WINDOW_POS", None)
    os.environ.pop("SDL_VIDEO_CENTERED", None)

    if pygame.get_init():
        try:
            pygame.display.quit()
        except Exception:
            pass
    pygame.display.init()
    pygame.init()
    pygame.display.set_caption("TKD 01 · TV")

    # Priorizar 60 Hz antes de consultar tamaños para que el marcador use
    # el modo real ya negociado por Windows/X11. Nunca se fuerza un modo de 30 Hz.
    ensure_tv_60hz()
    try:
        sizes = list(pygame.display.get_desktop_sizes())
    except Exception:
        sizes = []
    n = len(sizes)
    log_error(f"run_tv sizes={sizes} display_idx={display_idx}")

    if display_idx >= 0 and n > 0:
        idx = min(display_idx, n - 1)
    elif n >= 2:
        # La más grande = TV
        idx = max(range(n), key=lambda i: sizes[i][0] * sizes[i][1])
    else:
        idx = 0

    screen = None
    if n >= 1:
        w, h = sizes[idx]
        if windowed:
            # En Kali/X11, una ventana sin bordes evita que el modo FULLSCREEN
            # de SDL pierda/oculte la ventana secundaria al cambiar el foco.
            try:
                # Bordes cero y tamaño exacto del modo de escritorio actual; no se fuerza
                # frecuencia ni resolución, así que se conserva el refresco configurado
                # por Windows/X11 (recomendado mantenerlo en 50 Hz o más).
                screen = pygame.display.set_mode((w, h), pygame.NOFRAME, display=idx)
                log_error(f"TV NOFRAME/estable display={idx} {w}x{h}; frecuencia de escritorio conservada")
                if sys.platform.startswith("linux"):
                    try:
                        # Preferir la salida que detectó el sistema como TV, en vez
                        # de asumir que siempre será el monitor con más píxeles.
                        # El gestor de ventanas puede tardar un instante en registrar
                        # la ventana recién creada, así que se intenta mover dos veces.
                        roles = detect_display_roles()
                        target = roles.get("tv")
                        if target and target.get("w", 0) > 0 and target.get("h", 0) > 0:
                            rect = (target["x"], target["y"], target["w"], target["h"])
                        else:
                            mons = monitor_rects()
                            rect = None
                            if len(mons) >= 2:
                                secondary = next((m for m in mons if not m[4]), None)
                                target_rect = secondary or max(mons, key=lambda m: m[2] * m[3])
                                rect = target_rect[:4]
                        if rect:
                            time.sleep(0.12)
                            if not _linux_move_window("TKD 01 · TV", *rect):
                                time.sleep(0.18)
                                _linux_move_window("TKD 01 · TV", *rect)
                            # una vez colocada en la TV, que ocupe toda la pantalla (sin bordes ni barras)
                            time.sleep(0.12)
                            if not _linux_fullscreen("TKD 01 · TV"):
                                time.sleep(0.18)
                                _linux_fullscreen("TKD 01 · TV")
                    except Exception as move_error:
                        log_error(f"TV mover ventana: {move_error}")
            except Exception as e:
                log_error(f"TV NOFRAME display={idx} falló: {e}")
        else:
            try:
                screen = pygame.display.set_mode((w, h), pygame.FULLSCREEN, display=idx)
                log_error(f"TV FULLSCREEN display={idx} {w}x{h}")
            except Exception as e:
                log_error(f"FULLSCREEN display={idx} falló: {e}")
                try:
                    screen = pygame.display.set_mode((w, h), pygame.NOFRAME, display=idx)
                    log_error(f"TV NOFRAME display={idx} {w}x{h}")
                except Exception as e2:
                    log_error(f"NOFRAME display={idx} falló: {e2}")

    if screen is None:
        try:
            w, h = (sizes[idx] if sizes else (1280, 720))
            screen = pygame.display.set_mode((min(w, 1600), min(h, 900)), pygame.RESIZABLE)
            log_error(f"TV ventana normal {screen.get_size()} — arrástrala a la TV")
        except Exception:
            screen = pygame.display.set_mode((1280, 720), pygame.RESIZABLE)

    pygame.mouse.set_visible(True)
    clock = pygame.time.Clock()

    # Splash al iniciar (logo, no texto)
    splash_img = None
    splash_scaled = None
    splash_size = (0, 0)
    for sp in (TV_SPLASH_PATH, BASE_DIR / "tv_splash.jpg", Path(__file__).resolve().parent / "tv_splash.jpg"):
        try:
            if sp.exists():
                splash_img = pygame.image.load(str(sp)).convert()
                break
        except Exception as e:
            log_error(f"splash load: {e}")
    splash_until = time.time() + 5.0  # la estrella se muestra 5 segundos al abrir
    connected_once = False

    shared = {"d": None, "run": True}

    def poll():
        """1) Memoria compartida (RAM)  2) UDP binario de respaldo."""
        shm = None
        try:
            from multiprocessing import shared_memory
            # Esperar a que el PC cree el bloque
            for _ in range(50):
                try:
                    shm = shared_memory.SharedMemory(name=TV_SHM_NAME, create=False)
                    break
                except Exception:
                    time.sleep(0.05)
        except Exception as e:
            log_error(f"TV shm attach: {e}")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", TV_UDP_PORT))
            sock.settimeout(0.01)
        except Exception:
            sock = None
        log_error(f"TV poll: shm={'OK' if shm else 'no'} udp={'OK' if sock else 'no'}")
        while shared["run"]:
            got = False
            # RAM primero
            if shm is not None:
                try:
                    d = unpack_tv_state(bytes(shm.buf[:TV_SHM_SIZE]))
                    if d is not None:
                        shared["d"] = d
                        got = True
                except Exception:
                    pass
            # UDP respaldo
            if sock is not None:
                try:
                    last = None
                    while True:
                        try:
                            pkt, _ = sock.recvfrom(4096)
                            last = pkt
                        except socket.timeout:
                            break
                        except BlockingIOError:
                            break
                    if last:
                        d = unpack_tv_state(last)
                        if d is not None:
                            shared["d"] = d
                            got = True
                except Exception:
                    pass
            time.sleep(0.004 if got else 0.008)
        try:
            if shm: shm.close()
        except Exception:
            pass
        try:
            if sock: sock.close()
        except Exception:
            pass

    threading.Thread(target=poll, daemon=True).start()
    cache = {}

    def font(sz):
        sz = max(10, int(sz))
        if sz not in cache:
            cache[sz] = pygame.font.SysFont("dejavusans", sz, bold=True)
        return cache[sz]

    def put(text, fnt, col, center=None, left=None, right=None, y=None):
        img = fnt.render(text, True, col)
        if center:
            r = img.get_rect(center=center)
        elif left is not None:
            r = img.get_rect(midleft=(left, y))
        else:
            r = img.get_rect(midright=(right, y))
        screen.blit(img, r)

    def shade(c, k):
        return tuple(max(0, min(255, int(v * k))) for v in c)

    def draw_splash(w, h):
        nonlocal splash_scaled, splash_size
        screen.fill((0, 0, 0))
        if splash_img is not None:
            if splash_size != (w, h):
                # Caber en pantalla manteniendo proporción
                iw, ih = splash_img.get_size()
                scale = min(w / max(1, iw), h / max(1, ih))
                nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
                splash_scaled = pygame.transform.smoothscale(splash_img, (nw, nh))
                splash_size = (w, h)
            r = splash_scaled.get_rect(center=(w // 2, h // 2))
            screen.blit(splash_scaled, r)
        else:
            put("TKD 01", font(h * 0.08), WHITE, center=(w // 2, h // 2))

    last_d = None
    running = True
    while running:
        try:
            clock.tick(60)
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    running = False
                elif ev.type == pygame.KEYDOWN and ev.key in (pygame.K_ESCAPE, pygame.K_q):
                    running = False
            w, h = screen.get_size()
            d = shared["d"]
            if d is not None:
                last_d = d
                connected_once = True
            else:
                d = last_d  # nunca perder el último marcador válido

            # Splash solo al abrir, antes del primer dato
            if time.time() < splash_until or (not connected_once and d is None):
                draw_splash(w, h)
                pygame.display.flip()
                continue
            if d is None:
                draw_splash(w, h)
                pygame.display.flip()
                continue

            screen.fill((8, 9, 14))

            rc, bc = tuple(d["redColor"]), tuple(d["blueColor"])
            half = w // 2
            vert_gradient(screen, (0, 0, half, h), shade(rc, 1.0), shade(rc, 0.42))
            vert_gradient(screen, (half, 0, w - half, h), shade(bc, 1.0), shade(bc, 0.42))

            # ---- marcador lo más grande posible ----
            txt = max(str(d["red"]), str(d["blue"]), key=len)
            base = font(200)
            size = min(200 * (half * 0.94) / max(1, base.size(txt)[0]), h * 0.64 / 0.74)
            big = font(size)
            cy = int(h * 0.56)
            for val, cx in ((d["red"], half // 2), (d["blue"], half + (w - half) // 2)):
                for col, off in (((0, 0, 0), max(3, int(h * 0.006))), (WHITE, 0)):
                    img = big.render(str(val), True, col)
                    bb = img.get_bounding_rect()
                    screen.blit(img, (cx - bb.centerx + off, cy - bb.centery + off))

            # ---- nombres (arriba, esquinas) ----
            fn = font(h * 0.05)
            put(d["redName"], fn, WHITE, left=int(w * 0.02), y=int(h * 0.05))
            put(d["blueName"], fn, WHITE, right=int(w * 0.98), y=int(h * 0.05))

            # ---- faltas (esquinas de abajo) ----
            fl, fv, fs = font(h * 0.035), font(h * 0.11), font(h * 0.05)
            mx = d["maxFouls"]
            for n, left in ((d["redFouls"], True), (d["blueFouls"], False)):
                x = int(w * 0.02) if left else int(w * 0.98)
                tag = fl.render("FALTAS", True, (255, 220, 120))
                num = fv.render(str(n), True, (255, 220, 120))
                den = fs.render(f"/{mx}", True, (230, 230, 240))
                tw = max(tag.get_width(), num.get_width() + den.get_width() + 8)
                x0 = x if left else x - tw
                screen.blit(tag, (x0, int(h * 0.815)))
                screen.blit(num, (x0, int(h * 0.86)))
                screen.blit(den, (x0 + num.get_width() + 8, int(h * 0.86) + num.get_height() - den.get_height() - 6))

            # ---- centro arriba: round · tiempo · rounds ganados ----
            bw, bh = int(w * 0.15), int(h * 0.165)
            box = pygame.Rect(w // 2 - bw // 2, 0, bw, bh)
            pygame.draw.rect(screen, (10, 11, 18), box, border_bottom_left_radius=22, border_bottom_right_radius=22)
            pygame.draw.rect(screen, GOLD, box, width=2, border_bottom_left_radius=22, border_bottom_right_radius=22)
            lab = "DESCANSO" if d["resting"] else ("PUNTO DE ORO" if d.get("golden") else f"ROUND {d['round']}/{d['rounds']}")
            put(lab, font(h * 0.028), GOLD if (d["resting"] or d.get("golden")) else (200, 205, 220), center=(w // 2, int(h * 0.03)))
            put(clock_str(d["secondsF"]), font(h * 0.07), WHITE, center=(w // 2, int(h * 0.085)))
            sc = d.get("roundScores", [])
            winners = d.get("roundWinners", [])
            gap_ = int(w * 0.004)
            cw_ = min(int(w * 0.045), (bw - 20 - gap_ * (d["rounds"] - 1)) // max(1, d["rounds"]))
            x_ = w // 2 - (d["rounds"] * cw_ + (d["rounds"] - 1) * gap_) // 2
            for i in range(d["rounds"]):
                r = pygame.Rect(x_ + i * (cw_ + gap_), int(h * 0.12), cw_, int(h * 0.032))
                if i < len(winners) and winners[i] in ("RED", "BLUE"):
                    col = rc if winners[i] == "RED" else bc
                    lab_ = f"R{i + 1}"
                    if i < len(sc):
                        lab_ += f" {sc[i][0]}-{sc[i][1]}"
                elif i < len(sc):
                    col = rc if sc[i][0] > sc[i][1] else bc if sc[i][1] > sc[i][0] else (70, 75, 90)
                    lab_ = f"{sc[i][0]}-{sc[i][1]}"
                else:
                    col, lab_ = (40, 44, 60), "–"
                pygame.draw.rect(screen, col, r, border_radius=6)
                put(f"R{i + 1} {lab_}" if i < len(sc) else f"R{i + 1}", font(h * 0.018), WHITE, center=r.center)

            if d.get("sim"):
                put("SIMULACIÓN", font(h * 0.028), ORANGE, center=(w // 2, int(h * 0.975)))

            # ---- tiempo muerto ----
            if d["inTimeout"]:
                tb = pygame.Rect(0, 0, int(w * 0.30), int(h * 0.17))
                tb.center = (w // 2, int(h * 0.88))
                pygame.draw.rect(screen, (10, 11, 18), tb, border_radius=18)
                pygame.draw.rect(screen, ORANGE, tb, width=3, border_radius=18)
                put("TIEMPO MUERTO", font(h * 0.04), ORANGE, center=(tb.centerx, tb.y + int(tb.h * 0.28)))
                put(clock_str(d["timeoutLeft"]), font(h * 0.085), WHITE, center=(tb.centerx, tb.y + int(tb.h * 0.68)))

            # ---- fin de round / ganador ----
            if d.get("banner"):
                ov = pygame.Surface((w, h), pygame.SRCALPHA)
                ov.fill((0, 0, 0, 200))
                screen.blit(ov, (0, 0))
                red = d["banner"]["side"] == "RED"
                put("GANA EL ROUND", font(h * 0.07), GOLD, center=(w // 2, int(h * 0.35)))
                nm = d["redName"] if red else d["blueName"]
                put(nm, font(min(h * 0.22, 200 * (w * 0.9) / max(1, font(200).size(nm)[0]))), rc if red else bc,
                    center=(w // 2, int(h * 0.58)))
                reason = (d["banner"].get("reason") or "").strip()
                if reason:
                    put(reason.upper(), font(h * 0.04), (220, 220, 230), center=(w // 2, int(h * 0.78)))
            if d["winner"]:
                red = d["winner"] == "RED"
                col = rc if red else bc
                flash = (time.time() % 1.0) < 0.5  # parpadeo cada 1 s
                if flash:
                    screen.fill(WHITE)
                    fg = col
                else:
                    vert_gradient(screen, (0, 0, w, h), col, shade(col, 0.5))
                    fg = WHITE
                put("GANADOR", font(h * 0.08), fg, center=(w // 2, int(h * 0.28)))
                nm = d["redName"] if red else d["blueName"]
                put(nm, font(min(h * 0.28, 200 * (w * 0.9) / max(1, font(200).size(nm)[0]))), fg,
                    center=(w // 2, int(h * 0.56)))
            pygame.display.flip()

        except Exception as e:
            # Nunca cerrar la TV por un error de dibujo / lectura
            try:
                log_error(f"TV frame: {e}")
            except Exception:
                pass
            try:
                pygame.display.flip()
            except Exception:
                pass
            time.sleep(0.02)

    shared["run"] = False
    try:
        pygame.quit()
    except Exception:
        pass


ADMIN_PAGES = ["CÓDIGO", "JUECES", "CORRECCIÓN", "SIMULACIÓN", "REGLAS", "DIAGNÓSTICO"]
RULE_ROWS = [
    ("Puntos al rival por falta", "foul_give", (0, 3, 1)),
    ("Puntos que pierde el infractor", "foul_take", (0, 3, 1)),
    ("Punto de oro en empate", "golden_point", (0, 1, 1)),
    ("Duración del punto de oro (s)", "golden_seconds", (10, 180, 10)),
    ("Duración del sorteo (s)", "draw_seconds", (1, 10, 1)),
    ("Aviso de ganador de round (s)", "banner_seconds", (1, 10, 1)),
    ("Pantalla de ganador (s)", "winner_seconds", (2, 15, 1)),
]
SIM_SPEEDS = [1, 2, 5, 10, 20, 50]


def load_star():
    """Estrella para la esquina del marcador principal (None si falta el archivo)."""
    try:
        return pygame.image.load(str(STAR_PATH)).convert_alpha()
    except Exception:
        return None


def load_logo():
    try:
        return pygame.image.load(str(LOGO_PATH)).convert()
    except Exception:
        return None


def new_admin(settings=None, inputs=None, url=""):
    try:
        src = (BASE_DIR / "tkd01_pc.py").read_text(encoding="utf-8").expandtabs(4).splitlines()
    except Exception:
        src = ["# No se pudo leer el código fuente."]
    return {"t0": time.time(), "splash": True, "lines": src, "scroll": 0, "hscroll": 0,
            "visible": 30, "logo": load_logo(), "cache": {}, "page": 0, "focus": 0, "hit": {},
            "settings": settings, "inputs": inputs, "url": url,
            "pw": {"cur": "", "new": "", "rep": ""}, "pwf": "cur", "pwmsg": ("", None)}


def admin_cfg_set(st, attr, val):
    """Aplica una regla al combate y a los ajustes abiertos, y la guarda."""
    setattr(match.cfg, attr, val)
    if st.get("settings") is not None:
        setattr(st["settings"].draft, attr, val)
    match.save()


def fix_edit(fn):
    """Corrección manual del combate (se puede deshacer con Z en el marcador)."""
    if match.sim or match.st.winner:
        return
    with match.lock:
        match.snapshot()
        fn(match.st)


def _fix_clock(z, delta):
    c = match.cfg
    top = c.golden_seconds if z.golden else (c.rest_seconds if z.resting else c.round_seconds)
    z.seconds_left = float(max(0, min(top, int(z.seconds_left) + delta)))


def adm_rows(st):
    """[(etiqueta, valor, función_paso(d))] de las páginas con filas ajustables."""
    rows = []
    if st["page"] == 4:  # REGLAS
        for label, attr, (lo, hi, step) in RULE_ROWS:
            def stepper(d, attr=attr, lo=lo, hi=hi, step=step):
                admin_cfg_set(st, attr, max(lo, min(hi, getattr(match.cfg, attr) + d * step)))
            rows.append((label, fmt_val(attr, getattr(match.cfg, attr)), stepper))
    elif st["page"] == 2:  # CORRECCIÓN
        c, x = match.cfg, match.st

        def fix(fn):
            fix_edit(fn)
        rows = [
            ("Puntos rojo", str(x.red), lambda d: fix(lambda z: setattr(z, "red", max(0, z.red + d)))),
            ("Puntos azul", str(x.blue), lambda d: fix(lambda z: setattr(z, "blue", max(0, z.blue + d)))),
            ("Faltas rojo", f"{x.red_fouls}/{c.max_fouls}",
             lambda d: fix(lambda z: setattr(z, "red_fouls", max(0, min(c.max_fouls - 1, z.red_fouls + d))))),
            ("Faltas azul", f"{x.blue_fouls}/{c.max_fouls}",
             lambda d: fix(lambda z: setattr(z, "blue_fouls", max(0, min(c.max_fouls - 1, z.blue_fouls + d))))),
            ("Reloj  (±1 s)", clock_str(x.seconds_left), lambda d: fix(lambda z: _fix_clock(z, d))),
            ("Reloj  (±10 s)", clock_str(x.seconds_left), lambda d: fix(lambda z: _fix_clock(z, 10 * d))),
            ("Round actual", f"{x.round}/{c.rounds}",
             lambda d: fix(lambda z: setattr(z, "round", max(1, min(c.rounds, z.round + d))))),
            ("Rounds ganados rojo", str(x.red_won),
             lambda d: fix(lambda z: setattr(z, "red_won", max(0, min(c.rounds // 2, z.red_won + d))))),
            ("Rounds ganados azul", str(x.blue_won),
             lambda d: fix(lambda z: setattr(z, "blue_won", max(0, min(c.rounds // 2, z.blue_won + d))))),
        ]
    elif st["page"] == 3:  # SIMULACIÓN
        def tog_sim(d):
            match.sim_stop() if match.sim else match.sim_start()

        def spd(d):
            i = SIM_SPEEDS.index(match.sim_speed) if match.sim_speed in SIM_SPEEDS else 0
            match.sim_speed = SIM_SPEEDS[max(0, min(len(SIM_SPEEDS) - 1, i + d))]

        def itv(d):
            match.sim_interval = max(0.5, min(10.0, round(match.sim_interval + d * 0.5, 1)))

        def tg_f(d):
            match.sim_fouls = not match.sim_fouls

        def tg_t(d):
            match.sim_timeouts = not match.sim_timeouts
        rows = [
            ("Simulación", "ACTIVA" if match.sim else "APAGADA", tog_sim),
            ("Velocidad del reloj", f"×{match.sim_speed}", spd),
            ("Intervalo entre acciones", f"{match.sim_interval:.1f} s", itv),
            ("Incluir faltas", "SÍ" if match.sim_fouls else "NO", tg_f),
            ("Incluir tiempos muertos", "SÍ" if match.sim_timeouts else "NO", tg_t),
        ]
    return rows


def pw_apply(st, reset=False):
    f = st["pw"]
    if not admin_check(f["cur"]):
        st["pwmsg"] = ("Contraseña actual incorrecta", (255, 110, 110))
    elif reset:
        admin_reset()
        st["pwmsg"] = (f"Contraseña restaurada a {DEFAULT_ADMIN_PASS}", GREEN)
    elif len(f["new"]) < 4:
        st["pwmsg"] = ("La nueva contraseña necesita al menos 4 caracteres", (255, 110, 110))
    elif f["new"] != f["rep"]:
        st["pwmsg"] = ("Las contraseñas nuevas no coinciden", (255, 110, 110))
    else:
        admin_set(f["new"])
        st["pwmsg"] = ("Contraseña cambiada", GREEN)
    if st["pwmsg"][1] == GREEN:
        st["pw"] = {"cur": "", "new": "", "rep": ""}
        st["pwf"] = "cur"
    else:
        f["cur"] = ""


def admin_text(st, text):
    # Ya no hay página de contraseña
    pass


def admin_click(st, mx, my):
    if st["splash"]:
        st["splash"] = False
        return
    for key, rect in st["hit"].items():
        if not rect.collidepoint(mx, my):
            continue
        if key.startswith("atab"):
            st["page"] = int(key[4:]); st["focus"] = 0
        elif key[:1] in "mp" and key[1:].isdigit():
            rows = adm_rows(st)
            i = int(key[1:])
            if i < len(rows):
                st["focus"] = i
                rows[i][2](-1 if key[0] == "m" else 1)
        elif key[:1] == "r" and key[1:].isdigit():
            st["focus"] = int(key[1:])
        elif key.startswith("kick:"):
            match.kick(key[5:], 60)
        elif key.startswith("block:"):
            match.block(key[6:])
        elif key.startswith("allow:"):
            match.allow(key[6:])
        elif key == "simtoggle":
            match.sim_stop() if match.sim else match.sim_start()
        elif key == "diagclear":
            match.jstats.clear(); match.diag.clear()
        return


def admin_key(st, k):
    """Devuelve True si hay que salir del modo admin."""
    if st["splash"]:
        st["splash"] = False
        return False
    if k == pygame.K_ESCAPE:
        return True
    if k == pygame.K_TAB:
        st["page"] = (st["page"] + 1) % len(ADMIN_PAGES)
        st["focus"] = 0
        return False
    page = st["page"]
    if page == 0:
        pg = max(1, st["visible"] - 2)
        if k == pygame.K_UP: st["scroll"] -= 1
        elif k == pygame.K_DOWN: st["scroll"] += 1
        elif k == pygame.K_PAGEUP: st["scroll"] -= pg
        elif k == pygame.K_PAGEDOWN: st["scroll"] += pg
        elif k == pygame.K_HOME: st["scroll"] = 0; st["hscroll"] = 0
        elif k == pygame.K_END: st["scroll"] = len(st["lines"])
        elif k == pygame.K_LEFT: st["hscroll"] -= 8
        elif k == pygame.K_RIGHT: st["hscroll"] += 8
    elif page in (2, 3, 4):
        rows = adm_rows(st)
        if rows:
            if k == pygame.K_UP: st["focus"] = (st["focus"] - 1) % len(rows)
            elif k == pygame.K_DOWN: st["focus"] = (st["focus"] + 1) % len(rows)
            elif k == pygame.K_LEFT: rows[st["focus"]][2](-1)
            elif k in (pygame.K_RIGHT, pygame.K_RETURN): rows[st["focus"]][2](1)
    return False


def _afont(st, size, bold=False, mono=False):
    key = ("f", int(size), bold, mono)
    if key not in st["cache"]:
        st["cache"][key] = pygame.font.SysFont("dejavusansmono,consolas,couriernew,monospace" if mono else "dejavusans",
                                               int(size), bold=bold)
    return st["cache"][key]


def _rtext(surf, txt, font, col, right, cy):
    img = font.render(txt, True, col)
    surf.blit(img, img.get_rect(midright=(right, cy)))


def _abtn(surf, st, key, rect, label, font, col=(72, 79, 102), fg=None):
    pygame.draw.rect(surf, col, rect, width=1, border_radius=6)
    draw_text(surf, label, font, fg or col if col != (72, 79, 102) else (200, 205, 220), center=rect.center)
    st["hit"][key] = rect


def draw_admin(surf, st):
    """Modo admin: logo al inicio y luego las secciones de administración."""
    w, h = surf.get_size()
    now = time.time()
    logo = st["logo"]

    def scaled(box_w, box_h):
        key = ("logo", box_w, box_h)
        if key not in st["cache"]:
            lw, lh = logo.get_size()
            k = min(box_w / lw, box_h / lh)
            st["cache"][key] = pygame.transform.smoothscale(logo, (max(1, int(lw * k)), max(1, int(lh * k))))
        return st["cache"][key]

    if st["splash"]:
        surf.fill((0, 0, 0))
        if logo:
            img = scaled(w, h)
            surf.blit(img, img.get_rect(center=(w // 2, h // 2)))
        else:
            draw_text(surf, "01gen", _afont(st, max(40, h // 6), True), WHITE, center=(w // 2, h // 2))
        if now - st["t0"] > 2.5:
            st["splash"] = False
        return

    st["hit"] = {}
    surf.fill((12, 13, 18))
    hh = max(40, int(h * 0.075))
    pygame.draw.rect(surf, (6, 7, 10), (0, 0, w, hh))
    pygame.draw.line(surf, GOLD, (0, hh), (w, hh))
    x = 12
    if logo:
        img = scaled(int(hh * 1.8), hh - 8)
        surf.blit(img, (x, 4 + (hh - 8 - img.get_height()) // 2))
        x += img.get_width() + 14
    ftitle = _afont(st, max(14, hh * 0.42), True)
    fhint = _afont(st, max(11, hh * 0.28))
    draw_text(surf, "MODO ADMIN", ftitle, WHITE, topleft=(x, hh // 2 - ftitle.get_height() // 2))
    hint = fhint.render("TAB cambia de sección · Esc salir" + (" · ↑↓ PgUp/PgDn Home/End ←→ rueda" if st["page"] == 0 else ""),
                        True, GRAY)
    surf.blit(hint, (w - hint.get_width() - 14, hh // 2 - hint.get_height() // 2))

    # pestañas
    fs = _afont(st, max(12, h * 0.024))
    ty = hh + 6
    th = fs.get_height() + 12
    tw = w // len(ADMIN_PAGES)
    for i, name in enumerate(ADMIN_PAGES):
        r = pygame.Rect(i * tw, ty, tw, th)
        act = i == st["page"]
        draw_text(surf, name, fs, WHITE if act else (140, 146, 164), center=r.center)
        if act:
            pygame.draw.line(surf, GOLD, (r.x + 12, r.bottom - 1), (r.right - 12, r.bottom - 1), 2)
        st["hit"][f"atab{i}"] = r
    pygame.draw.line(surf, (38, 42, 54), (0, ty + th), (w, ty + th))
    y0 = ty + th + 14
    cw = min(w - 48, 900)
    x0 = (w - cw) // 2
    ft = _afont(st, max(11, h * 0.019))
    page = st["page"]
    if page == 0:
        _adm_code(surf, st, y0, w, h)
    elif page == 1:
        _adm_judges(surf, st, x0, cw, y0, fs, ft)
    elif page in (2, 3, 4):
        _adm_rowspage(surf, st, x0, cw, y0, h, fs, ft)
    else:
        _adm_diag(surf, st, x0, cw, y0, h, fs, ft)


def _adm_code(surf, st, y0, w, h):
    fs = max(12, int(h * 0.021))
    mono = _afont(st, fs, mono=True)
    lh = mono.get_linesize() + 1
    lines = st["lines"]
    area_h = h - y0 - 6
    visible = max(1, area_h // lh)
    st["visible"] = visible
    st["scroll"] = max(0, min(max(0, len(lines) - visible), st["scroll"]))
    gutter = mono.size("0" * len(str(len(lines))))[0] + 18
    cw_ = max(1, mono.size("M")[0])
    maxch = max(10, (w - gutter - 24) // cw_)
    st["hscroll"] = max(0, st["hscroll"])
    y = y0
    for i in range(visible):
        idx = st["scroll"] + i
        if idx >= len(lines):
            break
        line = lines[idx]
        t = line.strip()
        if t.startswith("#"):
            col = (110, 160, 115)
        elif t.startswith(("def ", "class ", "@dataclass")):
            col = GOLD
        elif t.startswith(("import ", "from ")):
            col = (150, 190, 240)
        else:
            col = (215, 220, 232)
        draw_text(surf, str(idx + 1), mono, (85, 91, 110), topleft=(8, y))
        draw_text(surf, line[st["hscroll"]:st["hscroll"] + maxch], mono, col, topleft=(gutter, y))
        y += lh
    if len(lines) > visible:
        bh = max(24, int(area_h * visible / len(lines)))
        by = y0 + int((area_h - bh) * st["scroll"] / max(1, len(lines) - visible))
        pygame.draw.rect(surf, (60, 66, 90), (w - 8, by, 5, bh), border_radius=3)


def _adm_pass(surf, st, x0, cw, y, fs, ft):
    draw_text(surf, "Cambiar contraseña del modo admin", fs, WHITE, topleft=(x0, y))
    y += fs.get_height() + 6
    draw_text(surf, "Se guarda cifrada en un archivo junto a tu configuración.", ft, (140, 146, 164), topleft=(x0, y))
    y += ft.get_height() + 22
    for key, label in (("cur", "Contraseña actual"), ("new", "Nueva contraseña"), ("rep", "Repetir nueva contraseña")):
        draw_text(surf, label, ft, (140, 146, 164), topleft=(x0, y))
        y += ft.get_height() + 6
        box = pygame.Rect(x0, y, min(cw, 380), 38)
        act = st["pwf"] == key
        pygame.draw.rect(surf, GOLD if act else (60, 66, 90), box, width=2 if act else 1, border_radius=6)
        shown = "•" * len(st["pw"][key]) + ("|" if act and int(time.time() * 2) % 2 == 0 else "")
        draw_text(surf, shown, fs, WHITE, topleft=(box.x + 12, box.centery - fs.get_height() // 2))
        st["hit"][f"pf:{key}"] = box
        y += 48
    b1 = pygame.Rect(x0, y + 4, 170, 38)
    _abtn(surf, st, "pwgo", b1, "CAMBIAR", fs, GREEN)
    b2 = pygame.Rect(b1.right + 14, y + 4, 290, 38)
    _abtn(surf, st, "pwdef", b2, f"RESTAURAR A {DEFAULT_ADMIN_PASS}", fs, ORANGE)
    y += 56
    msg, col = st["pwmsg"]
    if msg:
        draw_text(surf, msg, fs, col, topleft=(x0, y))


def _adm_judges(surf, st, x0, cw, y, fs, ft):
    status = match.judge_status()
    draw_text(surf, "TELÉFONOS CONECTADOS", ft, (140, 146, 164), topleft=(x0, y))
    y += ft.get_height() + 8
    if not status:
        draw_text(surf, "Ningún teléfono conectado", fs, (140, 146, 164), topleft=(x0, y))
        y += fs.get_height() + 10
    rh = fs.get_height() + 16
    for jid, age in status[:8]:
        row = pygame.Rect(x0, y, cw, rh - 4)
        ok = age < 2.5
        js = match._js(jid)
        pygame.draw.circle(surf, GREEN if ok else ORANGE, (row.x + 10, row.centery), 6)
        draw_text(surf, f"Juez {jid}", fs, WHITE, topleft=(row.x + 28, row.centery - fs.get_height() // 2))
        rej = js["deb"] + js["exp"] + js["blk"] + js["state"]
        draw_text(surf, f"env {js['sent']} · ok {js['ok']} · rechazadas {rej}", ft, (140, 146, 164),
                  topleft=(row.x + 230, row.centery - ft.get_height() // 2))
        b2 = pygame.Rect(row.right - 110, row.y + 3, 110, row.h - 6)
        b1 = pygame.Rect(b2.x - 168, row.y + 3, 160, row.h - 6)
        _abtn(surf, st, f"kick:{jid}", b1, "EXPULSAR 60 s", ft, ORANGE)
        _abtn(surf, st, f"block:{jid}", b2, "BLOQUEAR", ft, (255, 110, 110))
        pygame.draw.line(surf, (38, 42, 54), (x0, row.bottom + 1), (x0 + cw, row.bottom + 1))
        y += rh
    y += 14
    draw_text(surf, "BLOQUEADOS", ft, (140, 146, 164), topleft=(x0, y))
    y += ft.get_height() + 8
    bl = [(j, u) for j, u in match.blocked.items() if match.is_blocked(j)]
    if not bl:
        draw_text(surf, "Ninguno", fs, (140, 146, 164), topleft=(x0, y))
    for jid, until in bl:
        row = pygame.Rect(x0, y, cw, rh - 4)
        left = "permanente" if until == float("inf") else f"{max(0, until - time.time()):.0f} s"
        draw_text(surf, f"Juez {jid}", fs, WHITE, topleft=(row.x + 6, row.centery - fs.get_height() // 2))
        draw_text(surf, left, ft, ORANGE, topleft=(row.x + 230, row.centery - ft.get_height() // 2))
        _abtn(surf, st, f"allow:{jid}", pygame.Rect(row.right - 130, row.y + 3, 130, row.h - 6), "PERMITIR", ft, GREEN)
        y += rh
    y += 10
    draw_text(surf, "Un teléfono bloqueado sigue viendo el marcador pero sus señales se ignoran.",
              ft, (110, 116, 134), topleft=(x0, y))


def _adm_rowspage(surf, st, x0, cw, y, h, fs, ft):
    rows = adm_rows(st)
    st["focus"] = max(0, min(len(rows) - 1, st["focus"]))
    if st["page"] == 2:  # CORRECCIÓN
        draw_text(surf, "Corrige el marcador a mano. Cada cambio se puede deshacer con la tecla Z en el marcador.", ft,
                  (140, 146, 164), topleft=(x0, y))
        y += ft.get_height() + 6
        if match.sim:
            draw_text(surf, "Detén la simulación para corregir el combate real.", ft, ORANGE, topleft=(x0, y))
        elif match.st.winner:
            draw_text(surf, "El combate ya terminó: inicia uno nuevo para corregir.", ft, ORANGE, topleft=(x0, y))
        y += ft.get_height() + 12
    elif st["page"] == 3:  # SIMULACIÓN
        draw_text(surf, "Simula un combate para probar el marcador, el modo TV y los mandos.", ft,
                  (140, 146, 164), topleft=(x0, y))
        y += ft.get_height() + 4
        draw_text(surf, "Al apagarla vuelve tu combate real, sin perder nada.", ft, (140, 146, 164), topleft=(x0, y))
        y += ft.get_height() + 16
        b = pygame.Rect(x0, y, 280, 42)
        _abtn(surf, st, "simtoggle", b, "DETENER SIMULACIÓN" if match.sim else "INICIAR SIMULACIÓN", fs,
              ORANGE if match.sim else GREEN)
        y += 60
    else:  # REGLAS (page 4)
        draw_text(surf, "Reglas avanzadas · se aplican y se guardan al momento.", ft, (140, 146, 164), topleft=(x0, y))
        y += ft.get_height() + 16
    rh = max(40, int(h * 0.062))
    for i, (label, val, _) in enumerate(rows):
        row = pygame.Rect(x0, y, cw, rh)
        bs = rh - 14
        minus = pygame.Rect(row.right - 190, row.y + 7, bs, bs)
        plus = pygame.Rect(row.right - bs, row.y + 7, bs, bs)
        sel = i == st["focus"]
        if sel:
            pygame.draw.rect(surf, GOLD, (x0 - 12, y + 8, 3, rh - 16))
        draw_text(surf, label, fs, WHITE if sel else (170, 176, 194), topleft=(x0, row.centery - fs.get_height() // 2))
        for rect, sym, key in ((minus, "−", f"m{i}"), (plus, "+", f"p{i}")):
            pygame.draw.rect(surf, (72, 79, 102), rect, width=1, border_radius=6)
            draw_text(surf, sym, fs, (200, 205, 220), center=rect.center)
            st["hit"][key] = rect
        draw_text(surf, val, fs, GOLD, center=((minus.right + plus.left) // 2, row.centery))
        st["hit"][f"r{i}"] = row
        pygame.draw.line(surf, (38, 42, 54), (x0, y + rh - 1), (x0 + cw, y + rh - 1))
        y += rh
    if st["page"] == 4:  # REGLAS
        y += 12
        draw_text(surf, "Punto de oro: si hay empate al final de un round, se juega un tiempo extra y gana quien anote primero.",
                  ft, (110, 116, 134), topleft=(x0, y))
        y += ft.get_height() + 4
        draw_text(surf, "Si se acaba ese tiempo sin puntos, se decide por sorteo.", ft, (110, 116, 134), topleft=(x0, y))


def _adm_diag(surf, st, x0, cw, y, h, fs, ft):
    inputs = st.get("inputs")
    pads = inputs.pads if inputs else []
    mono = _afont(st, max(11, h * 0.02), mono=True)
    gray = (140, 146, 164)
    bc = pygame.Rect(x0 + cw - 120, y - 4, 120, 28)
    _abtn(surf, st, "diagclear", bc, "LIMPIAR", ft, (72, 79, 102))
    draw_text(surf, "SISTEMA", ft, gray, topleft=(x0, y))
    y += ft.get_height() + 6
    lines = [
        f"Jueces entran a:  {st.get('url', '')}",
        f"Puerto: {HTTP_PORT}" + ("   (el 8080 estaba ocupado)" if HTTP_PORT != DEFAULT_PORT else ""),
        f"Registro de errores: {LOG_PATH}  ("
        + (f"{LOG_PATH.stat().st_size} bytes" if LOG_PATH.exists() else "sin errores") + ")",
        f"FPS: {match.fps:.0f}   ·   Python {sys.version.split()[0]}   ·   pygame {pygame.version.ver}",
        f"Pantallas: {len(get_displays())}   ·   Mandos: {len(pads)}"
        + ("   (" + ", ".join(inputs.pad_name(i)[:18] for i in range(min(3, len(pads)))) + ")" if pads else ""),
        f"Simulación: {'ACTIVA' if match.sim else 'apagada'}   ·   Prueba de jueces: {'ACTIVA' if match.test_mode else 'apagada'}",
    ]
    for ln in lines:
        draw_text(surf, ln, mono, (215, 220, 232), topleft=(x0, y))
        y += mono.get_linesize() + 2
    y += 10
    draw_text(surf, "JUECES", ft, gray, topleft=(x0, y))
    y += ft.get_height() + 6
    cols = [("juez", 0), ("visto", 130), ("env", 240), ("ok", 310), ("antirr.", 370), ("sin par", 460),
            ("bloq.", 560), ("estado", 640)]
    for name, dx in cols:
        draw_text(surf, name, mono, gray, topleft=(x0 + dx, y))
    y += mono.get_linesize() + 2
    ids = sorted(set(match.jstats) | {j for j, _ in match.judge_status()})
    ages = dict(match.judge_status())
    if not ids:
        draw_text(surf, "— sin datos —", mono, gray, topleft=(x0, y))
        y += mono.get_linesize() + 2
    for jid in ids[:6]:
        js = match._js(jid)
        age = ages.get(jid)
        vals = [jid[:12], "—" if age is None else f"{age:.1f}s", js["sent"], js["ok"], js["deb"], js["exp"],
                js["blk"], js["state"]]
        for (name, dx), v in zip(cols, vals):
            draw_text(surf, str(v), mono, (215, 220, 232), topleft=(x0 + dx, y))
        y += mono.get_linesize() + 2
    y += 10
    draw_text(surf, "REGISTRO DE SEÑALES", ft, gray, topleft=(x0, y))
    y += ft.get_height() + 6
    if not match.diag:
        draw_text(surf, "— vacío —", mono, gray, topleft=(x0, y))
    for ln in match.diag:
        if y > h - mono.get_linesize() - 8:
            break
        col = (130, 220, 150) if "✓" in ln else (255, 150, 120) if "→" in ln else (200, 205, 220)
        draw_text(surf, ln, mono, col, topleft=(x0, y))
        y += mono.get_linesize() + 1



def main():
    global match, RED, BLUE
    match = Match()
    port = start_server()
    ip = local_ip()
    url = f"http://{ip}:{HTTP_PORT}" if port else "sin servidor (puertos 8080-8089 ocupados)"
    print(f"\n  TKD 01 PC listo\n  Móviles → {url}\n")

    pygame.init()
    pygame.display.set_caption(f"TKD 01 | Móviles: {url}")
    screen = pygame.display.set_mode((1280, 720), pygame.RESIZABLE)
    fullscreen = False
    clock = pygame.time.Clock()
    inputs = InputHub()
    star_img = load_star()
    star_cache = {}
    mode = "board"  # board | settings
    tvs = {"proc": None, "closed": False, "n": 0, "t": 0.0}
    admin = None
    reset_arm = {"until": 0.0}
    debounce = {}

    def tv_running():
        return tvs["proc"] is not None and tvs["proc"].poll() is None

    def _extend_displays():
        """Extiende sin mover de más: PC/TV por tamaño; solo reordena si hace falta."""
        try:
            role = getattr(match.cfg, "primary_role", 0)
            apply_display_layout(primary_role=role, force_reorder=False)
        except Exception as e:
            log_error(f"No se pudo extender pantallas: {e}")

    def launch_tv(windowed=False):
        if tv_running():
            return
        # 1) Extendido suave (no empuja ventanas si ya está extendido)
        _extend_displays()
        # 2) Índice de la TV: manual o la más grande
        want = match.cfg.tv_display
        if want > 0:
            idx = want - 1
        else:
            roles = detect_display_roles(getattr(match.cfg, "primary_role", 0))
            idx = roles.get("tv_idx", 1)
        base = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, str(Path(__file__).resolve())]
        args = base + ["--tv", "--display", str(idx), "--port", str(HTTP_PORT)] + (["--windowed"] if windowed else [])
        try:
            tvs["proc"] = subprocess.Popen(args)
            tvs["closed"] = False
        except Exception as e:
            print("No se pudo abrir el modo TV:", e)
            log_error(f"No se pudo abrir el modo TV: {e}")

    def close_tv():
        if tv_running():
            tvs["proc"].terminate()
        tvs["proc"] = None
        tvs["closed"] = True
    settings = SettingsUI(match.cfg, match, url, inputs)
    # zonas clicables del marcador
    hit = {}

    def fonts(h, side_w=None):
        huge = max(64, int(h * 0.46))
        if side_w:
            huge = max(64, min(huge, int(side_w * 0.62)))
        return {
            "huge": pygame.font.SysFont("dejavusans", huge, bold=True),
            "big": pygame.font.SysFont("dejavusans", max(28, h // 12), bold=True),
            "med": pygame.font.SysFont("dejavusans", max(18, h // 28), bold=True),
            "small": pygame.font.SysFont("dejavusans", max(14, h // 40), bold=True),
            "tiny": pygame.font.SysFont("dejavusans", max(12, h // 50)),
        }

    running = True
    while running:
        dt = clock.tick(60) / 1000.0
        match.fps = clock.get_fps()
        # Con TV abierta el combate sigue el tiempo aunque estés en ajustes
        if mode in ("board", "qr") or match.sim or tv_running():
            match.tick(dt * (match.sim_speed if match.sim else 1))
            match.update_overlays()
        if match.sim:
            match.sim_step()
        # TV por UDP: cada frame (~60 Hz) + push inmediato al marcar puntos
        if tv_running():
            match.push_tv_state()

        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type in (getattr(pygame, "JOYDEVICEADDED", -999), getattr(pygame, "JOYDEVICEREMOVED", -998)):
                try:
                    inputs._refresh()
                    settings.inputs = inputs
                except Exception as e:
                    log_error(f"Actualizando mandos conectados: {e}")
            elif ev.type == pygame.MOUSEWHEEL and mode == "admin":
                if admin["page"] == 0:
                    admin["scroll"] -= ev.y * 3
            elif ev.type == pygame.KEYDOWN:
                if mode == "qr":
                    mode = "board"
                    continue
                if mode == "admin":
                    if admin_key(admin, ev.key):
                        mode = "settings"
                    elif ev.unicode and not admin["splash"]:
                        admin_text(admin, ev.unicode)
                    continue
                if mode == "settings":
                    if ev.key == pygame.K_ESCAPE and settings.listening:
                        settings.listening = None
                    elif ev.key == pygame.K_ESCAPE and not settings.editing:
                        mode = "board"
                    elif ev.key == pygame.K_ESCAPE and settings.editing:
                        settings.editing = False
                    else:
                        settings.handle_key(ev.key)
                        if ev.unicode:
                            settings.handle_text(ev.unicode)
                    continue

                k = ev.key
                if k == pygame.K_ESCAPE:
                    running = False
                elif k == pygame.K_F11:
                    fullscreen = not fullscreen
                    screen = pygame.display.set_mode(
                        (0, 0) if fullscreen else (1280, 720),
                        pygame.FULLSCREEN if fullscreen else pygame.RESIZABLE,
                    )
                elif k == pygame.K_SPACE:
                    match.toggle()
                elif k == pygame.K_z:
                    match.undo()
                elif k == pygame.K_t:
                    match.toggle_timeout()
                elif k == pygame.K_j and url.startswith("http"):
                    mode = "qr"
                elif k in (pygame.K_n, pygame.K_RETURN):
                    match.dismiss_winner()
                elif k == pygame.K_s and not match.st.running:
                    settings = SettingsUI(match.cfg, match, url, inputs)
                    mode = "settings"
                elif k == pygame.K_1: match.add_points_direct(True, 1)
                elif k == pygame.K_2: match.add_points_direct(True, 2)
                elif k == pygame.K_3: match.add_points_direct(True, 3)
                elif k == pygame.K_4: match.add_points_direct(True, 4)
                elif k == pygame.K_5: match.add_points_direct(True, 5)
                elif k == pygame.K_q: match.foul_direct(True)
                elif k == pygame.K_a: match.add_points_direct(True, -1)
                elif k == pygame.K_6: match.add_points_direct(False, 1)
                elif k == pygame.K_7: match.add_points_direct(False, 2)
                elif k == pygame.K_8: match.add_points_direct(False, 3)
                elif k == pygame.K_9: match.add_points_direct(False, 4)
                elif k == pygame.K_0: match.add_points_direct(False, 5)
                elif k == pygame.K_e: match.foul_direct(False)
                elif k == pygame.K_d: match.add_points_direct(False, -1)

            elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                mx, my = ev.pos
                if mode == "qr":
                    mode = "board"
                    continue
                if mode == "admin":
                    admin_click(admin, mx, my)
                    continue
                if mode == "settings":
                    action = settings.click(mx, my)
                    if action == "save":
                        old_cfg = match.cfg
                        match.cfg = Config(**asdict(settings.draft))
                        # aplicar colores a pintura global simple
                        global RED, BLUE
                        RED = tuple(settings.red_color)
                        BLUE = tuple(settings.blue_color)
                        match.cfg.red_color = list(RED)
                        match.cfg.blue_color = list(BLUE)
                        if any(getattr(old_cfg, k) != getattr(match.cfg, k) for k in
                               ("round_seconds", "rest_seconds", "rounds", "max_fouls", "max_point_diff")):
                            match.apply_config_reset()
                        else:
                            match.save()
                        mode = "board"
                    elif action == "cancel":
                        mode = "board"
                    elif action in ("tv_on", "tv_win"):
                        # Solo modo ventana (detecta TV y coloca el marcador ahí)
                        launch_tv(True)
                    elif action == "tv_off":
                        close_tv()
                    elif action == "tv_invert":
                        close_tv()
                        invert_displays(settings)
                        try:
                            settings.displays = get_displays()
                        except Exception:
                            pass
                    elif action == "tv_primary":
                        close_tv()
                        toggle_primary_role(settings)
                        try:
                            settings.displays = get_displays()
                        except Exception:
                            pass
                    elif action == "tv_normal":
                        close_tv()
                        restore_normal_displays(settings)
                        try:
                            settings.displays = get_displays()
                        except Exception:
                            pass
                else:
                    # botones del marcador
                    for key, rect in hit.items():
                        if rect.collidepoint(mx, my):
                            if key == "toggle":
                                match.toggle()
                            elif key == "reset":
                                if time.time() < reset_arm["until"]:
                                    match.apply_config_reset()  # combate desde cero
                                    reset_arm["until"] = 0.0
                                else:
                                    reset_arm["until"] = time.time() + 3.0
                            elif key == "timeout":
                                match.toggle_timeout()
                            elif key == "simstop":
                                match.sim_stop()
                            elif key == "settings" and not match.st.running:
                                settings = SettingsUI(match.cfg, match, url, inputs)
                                mode = "settings"
                            elif key.startswith("rp"):
                                match.add_points_direct(True, int(key[2:]))
                            elif key.startswith("bp"):
                                match.add_points_direct(False, int(key[2:]))
                            elif key == "rf":
                                match.foul_direct(True)
                            elif key == "bf":
                                match.foul_direct(False)
                            elif key == "rm1":
                                match.add_points_direct(True, -1)
                            elif key == "bm1":
                                match.add_points_direct(False, -1)
                            break

        pad_events = inputs.poll()
        for _pi in range(len(inputs.pads)):
            match.ping_judge(f"Mando{_pi + 1}")  # cada mando conectado cuenta como juez
        if mode == "settings":
            for pi, tok in pad_events:
                settings.on_pad(pi, tok)
        elif mode == "board":
            nowms = time.time() * 1000
            deb = match.cfg.debounce_ms
            for pi, tok in pad_events:
                pk = inputs.pad_key(pi)
                for red, mp in ((True, match.cfg.pad_map_red), (False, match.cfg.pad_map_blue)):
                    for act, val in mp.items():
                        if not act.startswith("pts") or not val or "|" not in val:
                            continue  # los mandos solo ponen puntos
                        who, t = val.split("|", 1)
                        if t != tok:
                            continue
                        if who == "*":
                            pass  # cualquier mando
                        elif who.startswith("#"):
                            if who != f"#{pi}":
                                continue
                        elif who != pk:
                            continue
                        if nowms - debounce.get((pi, tok, red), 0) < deb:
                            continue  # anti-rebote
                        debounce[(pi, tok, red)] = nowms
                        # el mando vota como un juez más (mismas reglas que un celular)
                        match.mobile_vote(f"Mando{pi + 1}", "P", red, int(act[3:]))

        if mode == "settings" and settings.open_admin:
            settings.open_admin = False
            admin = new_admin(settings, inputs, url)
            mode = "admin"

        # estado de pantallas / modo TV
        if mode == "settings":
            settings.tv_running = tv_running()
            settings.displays = get_displays()
        if tvs["proc"] is not None and tvs["proc"].poll() is not None:
            tvs["proc"] = None
            tvs["closed"] = True
        if time.time() - tvs["t"] > 1.0:
            tvs["t"] = time.time()
            nd = len(get_displays())
            if nd != tvs["n"]:
                tvs["n"] = nd
                tvs["closed"] = False
            if nd >= 2 and match.cfg.tv_auto and not tv_running() and not tvs["closed"]:
                # Kali/Linux: mantener el marcador TV como ventana sin bordes,
                # no como fullscreen SDL que puede ocultarse al cambiar el foco.
                launch_tv(sys.platform.startswith("linux"))
        if mode != "settings":
            match.test_mode = False

        w, h = screen.get_size()
        colw = max(46, int(w * 0.06))
        f = fonts(h, int(w * 0.43) - colw - 24)

        if mode == "settings":
            settings.draw(screen, f)
            pygame.display.flip()
            continue
        if mode == "admin":
            draw_admin(screen, admin)
            pygame.display.flip()
            continue
        if mode == "qr":
            draw_qr_screen(screen, f, url)
            pygame.display.flip()
            continue


        # ---- MARCADOR ----
        st, cfg = match.st, match.cfg
        side_w = int(w * 0.43)
        center_w = w - 2 * side_w
        cx = side_w + center_w // 2
        hit = {}

        # Fondo general degradado
        vert_gradient(screen, (0, 0, w, h), (18, 20, 32), (8, 10, 18))

        # Paneles laterales con degradado
        for x0, c1, c2 in ((0, (140, 30, 40), (90, 18, 28)), (w - side_w, (20, 35, 100), (12, 22, 60))):
            for i in range(h):
                t = i / max(1, h - 1)
                col = (
                    int(c1[0] + (c2[0] - c1[0]) * t),
                    int(c1[1] + (c2[1] - c1[1]) * t),
                    int(c1[2] + (c2[2] - c1[2]) * t),
                )
                pygame.draw.line(screen, col, (x0, i), (x0 + side_w - 1, i))

        # Viñeta suave en el centro
        soft_panel(screen, pygame.Rect(side_w + 4, 6, center_w - 8, h - 12), (20, 22, 32), edge=(40, 44, 60), radius=10)

        # Nombres
        draw_text(screen, cfg.red_name, f["med"], WHITE, center=(side_w // 2, int(h * 0.06)))
        draw_text(screen, cfg.blue_name, f["med"], WHITE, center=(w - side_w // 2, int(h * 0.06)))

        # Puntuación grande, centrada en el espacio libre (botones en los bordes)
        red_cx = (colw + 12 + side_w) // 2
        blue_cx = (w - side_w + w - colw - 12) // 2
        for val, px in ((st.red, red_cx), (st.blue, blue_cx)):
            img = f["huge"].render(str(val), True, (0, 0, 0))
            screen.blit(img, img.get_rect(center=(px + 2, int(h * 0.36) + 2)))
            draw_text(screen, str(val), f["huge"], WHITE, center=(px, int(h * 0.36)))

        # Botones compactos en columna, pegados al borde exterior de cada panel
        btn_h = max(26, int(h * 0.05))
        gap = 5
        by0 = int(h * 0.36)
        items = [(f"+{n}", f"p{n}") for n in range(1, 6)] + [("-1", "m1"), ("FALTA", "f")]
        for i, (label, code) in enumerate(items):
            y = by0 + i * (btn_h + gap)
            for red, x in ((True, 6), (False, w - colw - 6)):
                r = pygame.Rect(x, y, colw, btn_h)
                side_point_btn(screen, r, label, wine=red)
                is_f = code == "f"
                draw_text(screen, label, f["tiny"] if is_f else f["small"], GOLD if is_f else WHITE, center=r.center)
                pre = "r" if red else "b"
                key = {"m1": "m1", "f": "f"}.get(code, code)
                hit[f"{pre}{key}" if code in ("m1", "f") else f"{pre}p{code[1:]}"] = r

        # Stats
        draw_text(screen, f"GAM-JEOM  {st.red_fouls}/{cfg.max_fouls}", f["tiny"], (255, 220, 220), center=(side_w // 2, int(h * 0.88)))
        draw_text(screen, f"ROUNDS  {st.red_won}", f["tiny"], GREEN, center=(side_w // 2, int(h * 0.94)))
        draw_text(screen, f"GAM-JEOM  {st.blue_fouls}/{cfg.max_fouls}", f["tiny"], (220, 230, 255), center=(w - side_w // 2, int(h * 0.88)))
        draw_text(screen, f"ROUNDS  {st.blue_won}", f["tiny"], GREEN, center=(w - side_w // 2, int(h * 0.94)))

        # Centro
        status = "DESCANSO" if st.resting else ("PUNTO DE ORO" if st.golden else f"ROUND  {st.round}/{cfg.rounds}")
        if st.round_drawn and st.resting:
            status += "  ·  sorteo"
        draw_text(screen, status, f["small"] if st.golden else f["med"], GOLD if (st.resting or st.golden) else WHITE,
                  center=(cx, int(h * 0.07)))

        # Reloj
        clock_rect = pygame.Rect(cx - 52, int(h * 0.12), 104, 36)
        soft_panel(screen, clock_rect, (16, 18, 28), edge=GOLD, radius=12)
        draw_text(screen, clock_str(st.seconds_left), f["med"], WHITE, center=clock_rect.center)

        if st.in_timeout:
            draw_text(screen, f"T.MUERTO  {clock_str(st.timeout_left)}", f["small"], ORANGE,
                      center=(cx, clock_rect.bottom + 14))

        if match.sim:
            sb = pygame.Rect(cx - 115, 4, 230, 24)
            draw_button(screen, sb, "● SIMULACIÓN · tocar para parar", f["tiny"], bg=(150, 95, 30))
            hit["simstop"] = sb

        # Botones centro
        t_rect = pygame.Rect(cx - 55, int(h * 0.34), 110, 32)
        u_rect = pygame.Rect(cx - 55, int(h * 0.34) + 38, 110, 28)
        s_rect = pygame.Rect(cx - 55, int(h * 0.34) + 70, 110, 28)
        draw_button(screen, t_rect, "PAUSAR" if st.running else "INICIAR", f["small"],
                    bg=ORANGE if st.running else (40, 130, 70), border=(255, 200, 100) if st.running else (80, 200, 120))
        armed = time.time() < reset_arm["until"]
        draw_button(screen, u_rect, "¿SEGURO?" if armed else "RESETEAR", f["tiny"],
                    bg=(170, 40, 45) if armed else (50, 55, 75), border=(255, 120, 120) if armed else None)
        draw_button(screen, s_rect, "AJUSTES", f["tiny"],
                    bg=(55, 60, 100) if not st.running else (40, 42, 55))
        o_rect = pygame.Rect(cx - 55, int(h * 0.34) + 102, 110, 28)
        draw_button(screen, o_rect, "FIN T.MUERTO" if st.in_timeout else "T. MUERTO", f["tiny"],
                    bg=(150, 95, 30) if st.in_timeout else (50, 55, 75))
        hit["timeout"] = o_rect
        hit["toggle"] = t_rect
        hit["reset"] = u_rect
        hit["settings"] = s_rect

        # Rounds strip
        y0 = int(h * 0.58)
        for i in range(1, cfg.rounds + 1):
            done = st.round_scores[i - 1] if i - 1 < len(st.round_scores) else None
            side_winner = st.round_winners[i - 1] if i - 1 < len(st.round_winners) else None
            if done is None:
                col, label = (40, 44, 60), f"R{i}  –"
            elif side_winner == "RED" or (side_winner is None and done[0] > done[1]):
                # El historial usa colores semánticos fijos: ROJO siempre rojo,
                # AZUL siempre azul. No depende de colores personalizados cargados
                # en la ventana de control, que podían estar intercambiados.
                col, label = (180, 35, 45), f"R{i}  {done[0]}-{done[1]}"
            elif side_winner == "BLUE" or (side_winner is None and done[1] > done[0]):
                col, label = (25, 45, 120), f"R{i}  {done[0]}-{done[1]}"
            else:
                col, label = (70, 75, 90), f"R{i}  {done[0]}-{done[1]}"
            rr = pygame.Rect(cx - 58, y0 + (i - 1) * 26, 116, 22)
            pygame.draw.rect(screen, col, rr, border_radius=6)
            draw_text(screen, label, f["tiny"], WHITE, center=rr.center)

        # Señales móviles
        sig_y = int(h * 0.78)
        draw_text(screen, "JUECES MOVIL", f["tiny"], GOLD, center=(cx, sig_y))
        for i, sig in enumerate(match.last_signals[:3]):
            side = "R" if sig["red"] else "A"
            t = f"{sig['from']} +{sig['v']}{side}" if sig["kind"] == "P" else f"{sig['from']} F{side}"
            col = GREEN if sig["ok"] else ORANGE
            draw_text(screen, t + ("  OK" if sig["ok"] else "  ..."), f["tiny"], col, center=(cx, sig_y + 14 + i * 14))

        jc = match.judge_count()
        exp = cfg.expected_judges
        missing = exp > 0 and jc < exp
        jtxt = f"jueces {jc}/{exp}" if exp > 0 else f"jueces {jc}"
        draw_text(screen, f"{url}   ·   {jtxt}   ·   mandos {len(inputs.pads)}   ·   S = ajustes   ·   J = QR",
                  f["tiny"], ORANGE if missing else GRAY, center=(cx, h - 16))
        if missing:
            draw_text(screen, f"! FALTAN {exp - jc} JUEZ(ES)", f["tiny"], (255, 110, 110), center=(cx, h - 36))

        # Estrella en la esquina inferior izquierda (no tapa botones ni marcador)
        if star_img is not None:
            ss = max(36, int(h * 0.09))
            if ss not in star_cache:
                star_cache.clear()
                star_cache[ss] = pygame.transform.smoothscale(star_img, (ss, ss))
            screen.blit(star_cache[ss], (8, h - ss - 8))

        # Overlay sorteo
        if st.show_draw_until > time.time() and st.draw_side:
            ov = pygame.Surface((w, h), pygame.SRCALPHA)
            ov.fill((0, 0, 0, 210))
            screen.blit(ov, (0, 0))
            col = RED if st.draw_side == "RED" else BLUE
            name = cfg.red_name if st.draw_side == "RED" else cfg.blue_name
            reason = st.round_win_reason or ("sorteo" if st.round_drawn else "")
            title = {"sorteo": "SORTEO", "faltas": "POR FALTAS", "diff": "POR DIFERENCIA", "puntos": "FIN DEL ROUND", "oro": "PUNTO DE ORO"}.get(reason, "FIN DEL ROUND")
            draw_text(screen, title, f["med"], GOLD, center=(w // 2, int(h * 0.28)))
            draw_text(screen, "GANA EL ROUND", f["med"], WHITE, center=(w // 2, int(h * 0.42)))
            draw_text(screen, name, f["huge"], col, center=(w // 2, int(h * 0.58)))

        # Overlay ganador
        if st.winner and st.show_winner_until > time.time():
            col = RED if st.winner == "RED" else BLUE
            name = cfg.red_name if st.winner == "RED" else cfg.blue_name
            flash = (time.time() % 1.0) < 0.5  # parpadeo cada 1 s
            if flash:
                screen.fill(WHITE)
            else:
                vert_gradient(screen, (0, 0, w, h), col, (max(0, col[0]-40), max(0, col[1]-40), max(0, col[2]-40)))
            fg = col if flash else WHITE
            draw_text(screen, "GANADOR", f["med"], fg, center=(w // 2, int(h * 0.28)))
            draw_text(screen, name, f["huge"], fg, center=(w // 2, int(h * 0.50)))
            if st.drawn:
                draw_text(screen, "Empate · ganador por sorteo", f["small"], WHITE, center=(w // 2, int(h * 0.65)))
            draw_text(screen, "Enter / N  —  nuevo combate", f["tiny"], WHITE, center=(w // 2, int(h * 0.80)))
        elif st.winner:
            draw_text(screen, "Enter / N  —  nuevo combate", f["small"], GOLD, center=(cx, int(h * 0.92)))

        pygame.display.flip()


    close_tv()
    match.sim_stop()
    match.save()
    cleanup_tv_ipc()
    # Evita que el siguiente inicio/sesión quede desplazado a la TV secundaria.
    restore_single_display()
    pygame.quit()


if __name__ == "__main__":
    install_error_hooks()
    try:
        if "--tv" in sys.argv:
            def _arg(name, default):
                return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default
            run_tv(int(_arg("--display", -1)), "--windowed" in sys.argv, int(_arg("--port", HTTP_PORT)))
        else:
            main()
    except SystemExit:
        raise
    except Exception:
        _tb = traceback.format_exc()
        log_error(_tb)
        if "--tv" not in sys.argv:
            show_crash(_tb)
        raise SystemExit(1)
