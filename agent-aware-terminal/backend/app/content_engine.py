"""Runtime-loadable content: mini games, quizzes, info cards.

Content packs are plain JSON files in content directories. They
are never baked into the application binary: anyone — including a
coding agent mid-session — can drop a new pack into a content
directory and the engine picks it up on the next game request.

Pack schema (see content/games/*.json for examples):

    {"id": "physics-orbits-001", "kind": "quiz" | "card",
     "kicker": "MINI GAME", "title": "...", "hint": "...",
     "scene": {"type": "orbits", "rings": 4,
               "dots": [{"ring": 2, "angle": 200}]}
            | {"type": "diagram",
               "nodes": [{"id", "label", "x", "y", "w", "h"}],
               "edges": [{"from", "to"}]}
            | {"type": "network", "layers": [3, 4, 2]}
            | null,
     "body": "optional paragraph under the scene",
     "question": "...", "choices": [{"id", "label"}],
     "answer": "<choice id or scene target id>",
     "explanation": "..."}

Scene targets: orbits expose ring-1..ring-N (1 = innermost),
diagram exposes its node ids, network exposes none.
"""
from __future__ import annotations

import json
import os
import random
from pathlib import Path

KINDS = ("quiz", "card")
SCENES = ("orbits", "diagram", "network")
# Declarative scene animation: subtle motion beats a still
# picture. Values are speeds (relative; keep them slow).
ANIMS = {"orbits": ("orbit", "breathe"),
         "diagram": ("flow", "breathe"),
         "network": ("pulse", "breathe")}


def scene_targets(pack: dict) -> list[str]:
    scene = pack.get("scene") or {}
    stype = scene.get("type")
    if stype == "orbits":
        return [f"ring-{i + 1}" for i in range(int(scene.get("rings", 0)))]
    if stype == "diagram":
        return [n.get("id") for n in scene.get("nodes") or []]
    return []


def validate_pack(raw, source: str = "") -> tuple[dict | None, str | None]:
    """Return (pack, None) or (None, reason). Strict but small."""
    if not isinstance(raw, dict):
        return None, "pack is not an object"
    pid = raw.get("id")
    if not pid or not isinstance(pid, str):
        return None, "missing string id"
    if raw.get("kind", "quiz") not in KINDS:
        return None, f"{pid}: unknown kind {raw.get('kind')!r}"
    if not raw.get("title"):
        return None, f"{pid}: missing title"
    scene = raw.get("scene")
    if scene is not None:
        if not isinstance(scene, dict) or scene.get("type") not in SCENES:
            return None, f"{pid}: unknown scene type"
        if scene["type"] == "diagram":
            ids = [n.get("id") for n in scene.get("nodes") or []]
            if not ids or any(not i for i in ids):
                return None, f"{pid}: diagram nodes need ids"
            for e in scene.get("edges") or []:
                if e.get("from") not in ids or e.get("to") not in ids:
                    return None, f"{pid}: edge references unknown node"
        if scene["type"] == "orbits" and int(scene.get("rings", 0)) < 2:
            return None, f"{pid}: orbits need >= 2 rings"
        if scene["type"] == "network" and \
                len(scene.get("layers") or []) < 2:
            return None, f"{pid}: network needs >= 2 layers"
        anim = scene.get("anim")
        if anim is not None and (
                not isinstance(anim, dict)
                or anim.get("type") not in ANIMS[scene["type"]]):
            return None, (f"{pid}: anim type not allowed for "
                          f"{scene['type']} scene")
    choices = raw.get("choices") or []
    cids = [c.get("id") for c in choices]
    if len(cids) != len(set(cids)):
        return None, f"{pid}: duplicate choice ids"
    if raw.get("kind", "quiz") == "quiz":
        valid = set(cids) | set(scene_targets(raw))
        if raw.get("answer") not in valid:
            return None, f"{pid}: answer {raw.get('answer')!r} not in targets"
        if not valid:
            return None, f"{pid}: quiz has no way to answer"
    pack = dict(raw)
    pack["_source"] = source
    return pack, None


def scan_packs(dirs) -> tuple[list[dict], list[str]]:
    packs, errors, seen = [], [], {}
    for d in dirs:
        try:
            files = sorted(Path(d).rglob("*.json"))
        except Exception:
            continue
        for f in files:
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))
            except Exception as exc:
                errors.append(f"{f}: {exc}")
                continue
            pack, err = validate_pack(raw, str(f))
            if err:
                errors.append(f"{f}: {err}")
            elif pack["id"] in seen:
                # The same pack reachable from two dirs is fine;
                # a different pack with the same id is a conflict.
                twin = {k: v for k, v in seen[pack["id"]].items()
                        if k != "_source"}
                if twin != {k: v for k, v in pack.items()
                            if k != "_source"}:
                    errors.append(f"{f}: duplicate id {pack['id']}")
            else:
                seen[pack["id"]] = pack
                packs.append(pack)
    return packs, errors


# ---------------------------------------------------------------------------
# Scene geometry (pure; the GUI draws with these)
# ---------------------------------------------------------------------------

def ring_radii(w: float, h: float, n: int) -> list[float]:
    base = min(w, h) / 2 - 14
    return [base * (i + 1) / n for i in range(n)]


def orbit_hit(cx, cy, radii, x, y):
    """Nearest ring within tolerance -> 'ring-i' (1 = innermost)."""
    import math
    d = math.hypot(x - cx, y - cy)
    best, best_dist = None, 1e9
    for i, r in enumerate(radii):
        if abs(d - r) < best_dist:
            best, best_dist = i, abs(d - r)
    if best is not None and best_dist <= max(16.0, 0.09 * radii[-1]):
        return f"ring-{best + 1}"
    return None


def node_rects(nodes, w: float, h: float):
    """Pack nodes use 0..1 coordinates; returns {id: (x, y, w, h)}."""
    out = {}
    for n in nodes:
        nw, nh = float(n.get("w", 0.26)) * w, float(n.get("h", 0.13)) * h
        out[n["id"]] = (float(n["x"]) * w - nw / 2,
                        float(n["y"]) * h - nh / 2, nw, nh)
    return out


def node_hit(rects, x, y):
    for nid, (rx, ry, rw, rh) in rects.items():
        if rx <= x <= rx + rw and ry <= y <= ry + rh:
            return nid
    return None


def network_positions(layers, w: float, h: float):
    """[(x, y)] row-major per layer, columns spread across w."""
    pts = []
    cols = len(layers)
    for li, count in enumerate(layers):
        x = w * (li + 1) / (cols + 1)
        for i in range(count):
            y = h * (i + 1) / (count + 1)
            pts.append((x, y, li))
    return pts


def paint_scene(cr, pack, w: float, h: float, colors: dict,
                chosen: str | None = None, answered: bool = False,
                t: float = 0.0):
    """Paint a pack scene with cairo. `colors` maps square/text/
    muted/bg to (r, g, b) tuples. `t` is seconds since the game
    was served — animated scenes (scene.anim) move with it. The
    app and tools/test_game.py share this painter, so previews
    are faithful."""
    import math
    scene = pack.get("scene") or {}
    stype = scene.get("type")
    anim = scene.get("anim") or {}
    atype = anim.get("type")
    speed = float(anim.get("speed", 0.15))
    strong = colors["square"]
    text = colors["text"]
    muted = colors["muted"]
    bg = colors["bg"]

    def rr(x, y, rw, rh, r):
        cr.new_path()
        cr.arc(x + rw - r, y + r, r, -1.5708, 0)
        cr.arc(x + rw - r, y + rh - r, r, 0, 1.5708)
        cr.arc(x + r, y + rh - r, r, 1.5708, 3.1416)
        cr.arc(x + r, y + r, r, 3.1416, 4.7124)
        cr.close_path()

    if atype == "breathe":
        s = 1 + 0.012 * math.sin(t * 1.4)
        cr.translate(w / 2, h / 2)
        cr.scale(s, s)
        cr.translate(-w / 2, -h / 2)

    if stype == "orbits":
        cx, cy = w / 2, h / 2
        radii = ring_radii(w, h, int(scene.get("rings", 4)))
        drift = t * speed * 45 if atype == "orbit" else 0.0
        for i, r in enumerate(radii):
            cr.arc(cx, cy, r, 0, 6.2832)
            if answered and pack.get("answer") == f"ring-{i + 1}":
                cr.set_source_rgba(*strong, 0.95)
                cr.set_line_width(2.8)
            elif answered and chosen == f"ring-{i + 1}":
                cr.set_source_rgba(*text, 0.9)
                cr.set_line_width(2.2)
            else:
                cr.set_source_rgba(*muted, 0.55)
                cr.set_line_width(1.2)
            cr.stroke()
        cr.arc(cx, cy, 15, 0, 6.2832)
        cr.set_source_rgba(*strong, 0.95)
        cr.set_line_width(1.6)
        cr.stroke()
        cr.set_source_rgba(*strong, 0.75)
        cr.set_line_width(1.3)
        rot = t * speed * 0.6 if atype == "orbit" else 0.0
        for k in range(8):
            a = k * 0.7854 + rot
            cr.move_to(cx + 21 * math.cos(a), cy + 21 * math.sin(a))
            cr.line_to(cx + 29 * math.cos(a), cy + 29 * math.sin(a))
        cr.stroke()
        for dot in scene.get("dots") or []:
            r = radii[int(dot["ring"]) - 1]
            a = math.radians(float(dot.get("angle", 0)) + drift)
            cr.arc(cx + r * math.cos(a), cy + r * math.sin(a),
                   5, 0, 6.2832)
            cr.set_source_rgba(*strong, 0.95)
            cr.fill()
    elif stype == "diagram":
        rects = node_rects(scene.get("nodes") or [], w, h)
        edges = scene.get("edges") or []
        for e in edges:
            a = rects.get(e.get("from"))
            b = rects.get(e.get("to"))
            if not a or not b:
                continue
            ax, ay = a[0] + a[2] / 2, a[1] + a[3] / 2
            bx, by = b[0] + b[2] / 2, b[1] + b[3] / 2
            cr.move_to(ax, ay)
            cr.line_to(bx, by)
            cr.set_source_rgba(*muted, 0.45)
            cr.set_line_width(1.2)
            cr.stroke()
            ang = math.atan2(by - ay, bx - ax)
            px, py = ax + (bx - ax) * 0.66, ay + (by - ay) * 0.66
            cr.move_to(px, py)
            cr.line_to(px - 9 * math.cos(ang - 0.42),
                       py - 9 * math.sin(ang - 0.42))
            cr.line_to(px - 9 * math.cos(ang + 0.42),
                       py - 9 * math.sin(ang + 0.42))
            cr.close_path()
            cr.set_source_rgba(*text, 0.8)
            cr.fill()
        if atype == "flow" and edges:
            span = (t * max(speed, 0.01)) % len(edges)
            idx = int(span)
            frac = span - idx
            e = edges[idx]
            a = rects.get(e.get("from"))
            b = rects.get(e.get("to"))
            if a and b:
                ax, ay = a[0] + a[2] / 2, a[1] + a[3] / 2
                bx, by = b[0] + b[2] / 2, b[1] + b[3] / 2
                cr.arc(ax + (bx - ax) * frac, ay + (by - ay) * frac,
                       4.5, 0, 6.2832)
                cr.set_source_rgba(*text, 0.9)
                cr.fill()
        cr.select_font_face("Inter", 0, 0)
        cr.set_font_size(15)
        for n in scene.get("nodes") or []:
            x, y, nw, nh = rects[n["id"]]
            right = answered and pack.get("answer") == n["id"]
            wrong = answered and chosen == n["id"] and not right
            rr(x, y, nw, nh, 12)
            if right:
                cr.set_source_rgba(*strong, 0.95)
                cr.fill()
                cr.set_source_rgba(*bg, 1)
            else:
                cr.set_source_rgba(*bg, 1)
                cr.fill_preserve()
                cr.set_source_rgba(*text, 0.85 if wrong else 0.5)
                cr.set_line_width(2.2 if wrong else 1.2)
                cr.stroke()
                cr.set_source_rgba(*text, 1)
            ext = cr.text_extents(n.get("label", ""))
            cr.move_to(x + nw / 2 - (ext.width / 2 + ext.x_bearing),
                       y + nh / 2 - (ext.height / 2 + ext.y_bearing))
            cr.show_text(n.get("label", ""))
    elif stype == "network":
        layers = scene.get("layers") or [3, 4, 2]
        pts = network_positions(layers, w, h)
        by_layer: dict[int, list] = {}
        for x, y, li in pts:
            by_layer.setdefault(li, []).append((x, y))
        cr.set_source_rgba(*muted, 0.18)
        cr.set_line_width(1.0)
        for li in sorted(by_layer)[:-1]:
            for x1, y1 in by_layer[li]:
                for x2, y2 in by_layer[li + 1]:
                    cr.move_to(x1, y1)
                    cr.line_to(x2, y2)
        cr.stroke()
        for x, y, li in pts:
            r = 13
            alpha = 0.9
            if atype == "pulse":
                wave = math.sin(t * 2.0 - li * 1.1)
                r = 13 + 2.5 * wave
                alpha = 0.6 + 0.35 * wave
            cr.arc(x, y, r, 0, 6.2832)
            cr.set_source_rgba(*bg, 1)
            cr.fill_preserve()
            cr.set_source_rgba(*strong, alpha)
            cr.set_line_width(1.4)
            cr.stroke()


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class ContentEngine:
    """A shuffled deck of packs with scoring and hot reload.

    No pack is served twice until every pack has been served
    once (a full cycle); the served set optionally persists to
    `state_path` so the rule survives app restarts too."""

    def __init__(self, dirs, seed: int | None = None,
                 state_path=None):
        self.dirs = [Path(d) for d in dirs]
        self._rng = random.Random(seed)
        self.packs: list[dict] = []
        self.errors: list[str] = []
        self.deck: list[dict] = []
        self._current: dict | None = None
        self._signature: tuple | None = None
        self.score = {"answered": 0, "correct": 0, "streak": 0,
                      "best_streak": 0}
        self.state_path = Path(state_path) if state_path else None
        self._served: list[str] = []
        self._last: str | None = None
        if self.state_path:
            try:
                saved = json.loads(
                    self.state_path.read_text(encoding="utf-8"))
                self._served = list(saved.get("served") or [])
                self._last = saved.get("last")
            except Exception:
                pass
        self.reload()

    def _dir_signature(self):
        sig = []
        for d in self.dirs:
            try:
                for f in sorted(d.rglob("*.json")):
                    sig.append((str(f), f.stat().st_mtime_ns))
            except Exception:
                continue
        return tuple(sig)

    def reload(self):
        self._signature = self._dir_signature()
        self.packs, self.errors = scan_packs(self.dirs)
        self.deck = list(self.packs)
        self._rng.shuffle(self.deck)
        ids = {p["id"] for p in self.packs}
        self._served = [i for i in self._served if i in ids]
        if self._current and self._current["id"] not in ids:
            self._current = None

    def maybe_reload(self):
        if self._dir_signature() != self._signature:
            self.reload()
            return True
        return False

    def _save_state(self):
        if not self.state_path:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(
                {"served": self._served, "last": self._last}),
                encoding="utf-8")
        except Exception:
            pass

    def next_game(self) -> dict | None:
        self.maybe_reload()
        if not self.packs:
            return None
        game = next((p for p in self.deck
                     if p["id"] not in self._served), None)
        if game is None:
            # Full cycle done: start over, but never serve the
            # same pack twice in a row across the boundary.
            self._served = []
            self._rng.shuffle(self.deck)
            if len(self.deck) > 1 and self.deck[0]["id"] == self._last:
                self.deck.append(self.deck.pop(0))
            game = self.deck[0]
        self._served.append(game["id"])
        self._last = game["id"]
        self._current = game
        self._save_state()
        return game

    def current(self) -> dict | None:
        return self._current

    def progress(self) -> tuple[int, int]:
        return (len(self._served), len(self.packs))

    def answer(self, target_id: str) -> dict:
        """Score an answer to the current pack. Cards never score."""
        game = self._current
        if game is None:
            return {"correct": None, "explanation": ""}
        if game.get("kind", "quiz") == "card":
            return {"correct": None,
                    "explanation": game.get("explanation", "")}
        correct = target_id == game.get("answer")
        self.score["answered"] += 1
        if correct:
            self.score["correct"] += 1
            self.score["streak"] += 1
            self.score["best_streak"] = max(self.score["best_streak"],
                                            self.score["streak"])
        else:
            self.score["streak"] = 0
        return {"correct": correct,
                "explanation": game.get("explanation", "")}


def default_content_dirs(data_dir, res_root=None, executable=None):
    """Where packs may live, most specific first. Missing is fine."""
    out = []
    env = os.environ.get("AAT_CONTENT_DIR")
    if env:
        out.append(Path(env))
    out.append(Path(data_dir) / "content")
    if executable:
        try:
            exe_dir = Path(executable).resolve().parent
            out.append(exe_dir / "content")
            out.append(exe_dir / "games-base")
        except Exception:
            pass
    out.append(Path.cwd() / "content")
    if res_root:
        out.append(Path(res_root) / "content")
        out.append(Path(res_root) / "games-base")
    seen, uniq = set(), []
    for d in out:
        try:
            key = str(d.resolve())
        except Exception:
            key = str(d)
        if key not in seen:
            seen.add(key)
            uniq.append(d)
    return uniq
