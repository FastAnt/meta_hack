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
import pathlib
import random
import re
from pathlib import Path

KINDS = ("quiz", "card")
# Content types, marked inside every file (GAME["type"] or the
# JSON "type" field). Legacy packs without a type resolve from
# kind: quiz -> game, card -> fact.
TYPES = ("game", "news", "idea", "fact")


def resolve_type(raw) -> str:
    t = raw.get("type")
    if t:
        return t
    return "game" if raw.get("kind", "quiz") == "quiz" else "fact"


CARD_VISUAL_KINDS = ("sections", "convert", "music", "blueprint",
                     "release", "news_text")

_STOPWORDS = {"the", "a", "an", "and", "or", "of", "to", "in",
              "for", "with", "from", "into", "that", "this", "now",
              "new", "one", "can", "puts", "what", "why", "how"}


def _visual_text(pack: dict) -> str:
    return " ".join(str(pack.get(k) or "") for k in
                    ("title", "body", "topic", "action_hint")).lower()


def _visual_keywords(pack: dict, limit: int = 3) -> list:
    words, seen = [], set()
    for word in re.findall(r"[A-Za-z][A-Za-z'-]{3,}",
                           str(pack.get("title") or "")):
        low = word.lower()
        if low in _STOPWORDS or low in seen:
            continue
        seen.add(low)
        words.append(word)
        if len(words) >= limit:
            break
    return words


def _visual_token(pack: dict) -> str:
    text = f"{pack.get('title') or ''} {pack.get('body') or ''}"
    m = re.search(r"\bv\d+(?:\.\d+)?(?:-mini|-wild)?\b", text, re.I) \
        or re.search(r"\d+(?:\.\d+)?\s?%", text) \
        or re.search(r"\b\d+(?:\.\d+)?x\b", text, re.I) \
        or re.search(r"\b(?:19|20)\d{2}\b", text)
    return m.group(0).upper() if m else ""


def visual_kind_for(pack: dict) -> str:
    """Which built-in visual describes this card's story.

    A researcher may declare it (`"visual": {"kind": …}` in the
    pack); otherwise the kind is derived from the pack's own
    words — conversion stories get a converter, section-editing
    stories a segmented bar with one lit section, music stories
    a staff with a playhead, ideas a blueprint route to a target,
    version/number stories the number itself. The generic
    headline card is only the last resort."""
    declared = (pack.get("visual") or {}).get("kind") \
        if isinstance(pack.get("visual"), dict) else None
    if declared in CARD_VISUAL_KINDS:
        return declared
    text = _visual_text(pack)
    if re.search(r"round.?trip|decompile|convert|back into|"
                 r"from_midi|to_midi|inverse|translate[sd]? (back|into)",
                 text):
        return "convert"
    if "section" in text or "chorus" in text or "segment" in text:
        return "sections"
    if resolve_type(pack) == "idea":
        # An idea is a proposal: the blueprint route describes
        # it, whatever the project's domain words say.
        return "blueprint"
    if re.search(r"\bmusic|\bsong|\bmidi|\bmelod|\bnote[rs]?\b|"
                 r"\baudio|\bchorus|\blyric", text):
        return "music"
    if _visual_token(pack):
        return "release"
    return "news_text"


def visual_params(pack: dict, kind: str) -> dict:
    """Facts for the painter, all measured from the pack text:
    title keywords, the explicit version/number token (if any),
    which segment is 'being edited', source/target labels for a
    conversion. Nothing is invented."""
    import hashlib
    text = _visual_text(pack)
    seed = int(hashlib.md5(
        (pack.get("title") or "").encode()).hexdigest()[:6], 16)
    sides = []
    for cand in ("midi", "code", "audio", "text", "data", "model",
                 "song", "program", "score"):
        m = re.search(rf"\b{cand}", text)
        if m:
            sides.append((m.start(), cand.upper()))
    sides = [c for _, c in sorted(sides)][:2]
    return {"keywords": _visual_keywords(pack),
            "token": _visual_token(pack),
            "highlight": seed % 5,
            "seed": seed,
            "source": sides[0] if sides else "",
            "target": sides[1] if len(sides) > 1 else "",
            "host": ""}


def with_card_visual(pack: dict) -> dict:
    """News and idea packs arrive as bare JSON (no scene, no
    paint): synthesize a visual that describes the card itself.
    Packs with their own visual, and every other type, pass
    through untouched; the input dict is never mutated."""
    if resolve_type(pack) == "pr" and not pack.get("scene"):
        return {**pack, "scene": {
            "type": "pr",
            "anim": {"type": "cardvisual", "speed": 0.12},
            "visual": {"number": pack.get("number"),
                       "components": pack.get("components") or [],
                       "files": pack.get("changed_files") or 0}}}
    if resolve_type(pack) not in ("news", "idea") \
            or pack.get("scene") or callable(pack.get("_paint")):
        return pack
    kind = visual_kind_for(pack)
    return {**pack, "scene": {
        "type": kind,
        "anim": {"type": "cardvisual", "speed": 0.12},
        "visual": visual_params(pack, kind)}}


def with_news_visual(pack: dict) -> dict:
    """Backwards-compatible name: news only."""
    if resolve_type(pack) != "news":
        return pack
    return with_card_visual(pack)
SCENES = ("orbits", "diagram", "network")
# Declarative scene animation: subtle motion beats a still
# picture. Values are speeds (relative; keep them slow).
ANIMS = {"orbits": ("orbit", "breathe"),
         "diagram": ("flow", "breathe"),
         "network": ("pulse", "breathe")}


def load_py_pack(path):
    """Import a .py game module. Returns (pack, None)|(None, why).

    A game module defines:
      GAME = {"id": ..., "kind": "quiz", "title": ..., ...meta...}
      def paint(cr, view): ...            # any cairo drawing
      def target_at(view, x, y): ...      # optional click targets
    Games are trusted local code (same trust as the agent that
    wrote them); the app only loads them from content dirs.
    """
    import hashlib
    import importlib.util
    path = pathlib.Path(path)
    try:
        src = path.read_bytes()
        name = "aat_game_" + hashlib.sha1(
            (str(path) + str(len(src))).encode()).hexdigest()[:12]
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as exc:
        return None, f"import failed: {exc}"
    meta = getattr(mod, "GAME", None)
    if not isinstance(meta, dict):
        return None, "module must define a GAME dict"
    if not callable(getattr(mod, "paint", None)):
        return None, "module must define paint(cr, view)"
    pack = dict(meta)
    pack["_paint"] = mod.paint
    if callable(getattr(mod, "target_at", None)):
        pack["_target_at"] = mod.target_at
    return pack, None


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
    ptype = resolve_type(raw)
    if ptype not in TYPES:
        return None, f"{pid}: unknown type {ptype!r}"
    if not raw.get("title"):
        return None, f"{pid}: missing title"
    if ptype in ("fact", "news", "idea") and not raw.get("body"):
        return None, f"{pid}: {ptype} needs a body"
    if ptype == "news":
        link = raw.get("link") or ""
        if not (link.startswith("https://")
                or link.startswith("http://")):
            return None, f"{pid}: news needs an http(s) link"
    if "_paint" in raw:
        # Code game: meta rules only — it draws itself.
        choices = raw.get("choices") or []
        cids = [c.get("id") for c in choices]
        if len(cids) != len(set(cids)):
            return None, f"{pid}: duplicate choice ids"
        if ptype == "game" and not raw.get("answer"):
            return None, f"{pid}: game needs an answer target id"
        pack = dict(raw)
        pack["type"] = ptype
        pack["kind"] = "quiz" if ptype == "game" else "card"
        pack["_source"] = source
        return pack, None
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
    if ptype == "game":
        valid = set(cids) | set(scene_targets(raw))
        if raw.get("answer") not in valid:
            return None, f"{pid}: answer {raw.get('answer')!r} not in targets"
        if not valid:
            return None, f"{pid}: game has no way to answer"
    pack = dict(raw)
    pack["type"] = ptype
    pack["kind"] = "quiz" if ptype == "game" else "card"
    pack["_source"] = source
    return pack, None


def scan_packs(dirs) -> tuple[list[dict], list[str]]:
    packs, errors, seen = [], [], {}
    for d in dirs:
        try:
            files = sorted(
                list(Path(d).rglob("*.json"))
                + list(Path(d).rglob("*.py")))
        except Exception:
            continue
        for f in files:
            if f.suffix == ".py":
                pack, err = load_py_pack(f)
                if pack is not None:
                    pack["_source"] = str(f)
                    pack, err = validate_pack(pack, str(f))
            else:
                try:
                    raw = json.loads(f.read_text(encoding="utf-8"))
                except Exception as exc:
                    errors.append(f"{f}: {exc}")
                    continue
                pack, err = validate_pack(raw, str(f))
                if pack is not None:
                    pack["_source"] = str(f)
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


class GameView:
    """What a code game's paint()/target_at() receives."""

    def __init__(self, w, h, t, colors, chosen=None, answered=False):
        self.w, self.h, self.t = w, h, t
        self.colors = colors          # square/text/muted/bg -> (r,g,b)
        self.chosen = chosen
        self.answered = answered

    # geometry helpers (same math the engine uses)
    ring_radii = staticmethod(ring_radii)
    node_rects = staticmethod(node_rects)
    network_positions = staticmethod(network_positions)
    orbit_hit = staticmethod(orbit_hit)
    node_hit = staticmethod(node_hit)



def paint_scene(cr, pack, w: float, h: float, colors: dict,
                chosen: str | None = None, answered: bool = False,
                t: float = 0.0):
    """Paint a pack scene with cairo. `colors` maps square/text/
    muted/bg to (r, g, b) tuples. `t` is seconds since the game
    was served — animated scenes (scene.anim) move with it. The
    app and tools/test_game.py share this painter, so previews
    are faithful."""
    import math
    if callable(pack.get("_paint")):
        view = GameView(w, h, t, colors, chosen, answered)
        pack["_paint"](cr, view)
        return
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

    if stype == "news_text":
        # Minimal text graphic for news: the headline set large,
        # a reading line a square travels along, and abstract
        # title-derived bars (decoration, not data — no axes, no
        # numbers). Everything comes from the pack itself.
        import hashlib
        from urllib.parse import urlparse
        mx = max(36.0, w * 0.07)
        max_w = w - 2 * mx
        size = 30.0
        lines: list = []
        while size > 15:
            cr.select_font_face("Inter", 0, 1)
            cr.set_font_size(size)
            lines, cur = [], ""
            for word in (pack.get("title") or "").split():
                trial = f"{cur} {word}".strip()
                if cr.text_extents(trial).width <= max_w or not cur:
                    cur = trial
                else:
                    lines.append(cur)
                    cur = word
            if cur:
                lines.append(cur)
            if len(lines) <= 3:
                break
            size -= 3.0
        shown = lines[:3]
        y = h * 0.30
        for i, line in enumerate(shown):
            cr.set_source_rgba(
                *text, 0.5 + 0.5 * (i + 1) / max(len(shown), 1))
            cr.move_to(mx, y)
            cr.show_text(line)
            y += size * 1.25
        ry = min(y + 12, h * 0.70)
        cr.move_to(mx, ry)
        cr.line_to(mx + max_w, ry)
        cr.set_source_rgba(*muted, 0.4)
        cr.set_line_width(1.2)
        cr.stroke()
        sx = mx + ((t * speed) % 1.0) * max_w
        cr.rectangle(sx - 4.5, ry - 4.5, 9, 9)
        cr.set_source_rgba(*strong, 0.95)
        cr.fill()
        digest = hashlib.md5(
            (pack.get("title") or "").encode()).digest()
        n = 24
        bw = max_w / n
        base = h * 0.88
        for i in range(n):
            bh = 5 + digest[i % len(digest)] % 17
            cr.rectangle(mx + i * bw + bw * 0.22, base - bh,
                         bw * 0.5, bh)
            cr.set_source_rgba(*muted, 0.30 + 0.25 * (i % 3) / 2)
            cr.fill()
        host = urlparse(pack.get("link") or "").netloc
        label = " · ".join(
            x for x in (host, pack.get("topic") or "") if x)
        if label:
            cr.select_font_face("JetBrains Mono", 0, 0)
            cr.set_font_size(10.5)
            cr.set_source_rgba(*muted, 0.8)
            cr.move_to(mx, h * 0.96)
            cr.show_text(label.upper())
    elif stype in ("sections", "convert", "music", "blueprint",
                   "release", "pr"):
        # Content-describing visuals for news/idea cards. All
        # labels come from the pack itself (scene.visual, filled
        # by visual_params from the pack's own words); geometry
        # is the story's shape, not decoration.
        import hashlib
        from urllib.parse import urlparse
        vis = scene.get("visual") or {}
        kws = vis.get("keywords") or []
        seed = int(vis.get("seed") or 0)
        mx = max(36.0, w * 0.07)
        max_w = w - 2 * mx

        def _mono(text_, x, y, size=10.5, alpha=0.8, bold=False):
            cr.select_font_face("JetBrains Mono", 0,
                                1 if bold else 0)
            cr.set_font_size(size)
            cr.set_source_rgba(*muted, alpha)
            cr.move_to(x, y)
            cr.show_text(str(text_).upper())

        def _caption():
            host = urlparse(pack.get("link") or "").netloc
            label = " · ".join(
                x for x in (host, pack.get("topic") or "") if x)
            if label:
                _mono(label, mx, h * 0.96)
            if kws:
                _mono(" ".join(kws), mx, h * 0.14, alpha=0.55)

        if stype == "pr":
            # The story: a pull request and the components it
            # touches. Component boxes (name · files changed,
            # both from gh + the project index), a central PR
            # node, a dot travelling the lines.
            comps = (vis.get("components") or [])[:4]
            cx, cy = w / 2, h * 0.44
            nw, nh = 128.0, 52.0
            rr(cx - nw / 2, cy - nh / 2, nw, nh, 10)
            cr.set_source_rgba(*text, 0.92)
            cr.fill()
            cr.select_font_face("JetBrains Mono", 0, 1)
            cr.set_font_size(15)
            cr.set_source_rgba(*bg, 1)
            label = f"PR #{vis.get('number') or '?'}"
            ext = cr.text_extents(label)
            cr.move_to(cx - ext.width / 2, cy + 5)
            cr.show_text(label)
            cr.select_font_face("JetBrains Mono", 0, 0)
            cr.set_font_size(10.5)
            cr.set_source_rgba(*muted, 0.9)
            stats = (f"+{pack.get('additions') or 0} "
                     f"-{pack.get('deletions') or 0} · "
                     f"{vis.get('files') or 0} files")
            ext = cr.text_extents(stats)
            cr.move_to(cx - ext.width / 2, cy + nh / 2 + 18)
            cr.show_text(stats)
            if not comps:
                _mono("no component map for the changed files",
                      mx, cy + nh / 2 + 38, alpha=0.6)
            slots = [(mx, h * 0.20), (mx + max_w - 190, h * 0.20),
                     (mx, h * 0.66), (mx + max_w - 190, h * 0.66)]
            for (bx, by), comp in zip(slots, comps):
                bw_, bh_ = 190.0, 44.0
                rr(bx, by, bw_, bh_, 8)
                cr.set_source_rgba(*bg, 1)
                cr.fill_preserve()
                cr.set_source_rgba(*text, 0.6)
                cr.set_line_width(1.2)
                cr.stroke()
                cr.move_to(cx, cy)
                tx = bx + (bw_ if bx < cx else 0)
                cr.line_to(tx, by + bh_ / 2)
                cr.set_source_rgba(*muted, 0.4)
                cr.set_line_width(1.0)
                cr.stroke()
                frac = (t * 0.3 + (bx + by) % 1.0) % 1.0
                dx = cx + (tx - cx) * frac
                dy = cy + (by + bh_ / 2 - cy) * frac
                cr.rectangle(dx - 3.5, dy - 3.5, 7, 7)
                cr.set_source_rgba(*strong, 0.9)
                cr.fill()
                cr.select_font_face("JetBrains Mono", 0, 1)
                cr.set_font_size(11)
                cr.set_source_rgba(*text, 0.9)
                cr.move_to(bx + 10, by + 19)
                cr.show_text(str(comp.get("name") or "?")[:22])
                cr.select_font_face("JetBrains Mono", 0, 0)
                cr.set_font_size(10)
                cr.set_source_rgba(*muted, 0.85)
                cr.move_to(bx + 10, by + 35)
                cr.show_text(f"{comp.get('files') or 0} files")
            branches = " -> ".join(
                x for x in (pack.get("head"), pack.get("base")) if x)
            if branches:
                _mono(branches, mx, h * 0.90)
            _caption()
        elif stype == "sections":
            # The story: one section is rewritten, the rest of
            # the song/document stays exactly as it was.
            n, gap = 5, 10.0
            sw = (max_w - gap * (n - 1)) / n
            sy, sh = h * 0.38, h * 0.20
            hot = int(vis.get("highlight") or 0) % n
            pulse = 0.75 + 0.25 * math.sin(t * 2.2)
            for i in range(n):
                x = mx + i * (sw + gap)
                rr(x, sy, sw, sh, 8)
                if i == hot:
                    cr.set_source_rgba(*strong, pulse)
                    cr.fill()
                    cr.select_font_face("JetBrains Mono", 0, 1)
                    cr.set_font_size(11)
                    cr.set_source_rgba(*bg, 1)
                    ext = cr.text_extents("EDIT")
                    cr.move_to(x + sw / 2 - ext.width / 2,
                               sy + sh / 2 + 4)
                    cr.show_text("EDIT")
                else:
                    cr.set_source_rgba(*bg, 1)
                    cr.fill_preserve()
                    cr.set_source_rgba(*text, 0.45)
                    cr.set_line_width(1.2)
                    cr.stroke()
            cr.move_to(mx, sy + sh + 26)
            cr.line_to(mx + max_w, sy + sh + 26)
            cr.set_source_rgba(*muted, 0.35)
            cr.set_line_width(1.0)
            cr.stroke()
            _mono("one section changes · the rest stays intact",
                  mx, sy + sh + 48, alpha=0.65)
            _caption()
        elif stype == "convert":
            # The story: one representation becomes another —
            # a square travels the arrow (and back, for a
            # round-trip).
            text_all = _visual_text(pack)
            two_way = bool(re.search(
                r"round.?trip|back|inverse", text_all))
            bw_, bh_ = max_w * 0.26, h * 0.34
            lx, rx = mx, mx + max_w - bw_
            by = h * 0.30
            for x, lab in ((lx, vis.get("source") or "INPUT"),
                           (rx, vis.get("target") or "OUTPUT")):
                rr(x, by, bw_, bh_, 10)
                cr.set_source_rgba(*bg, 1)
                cr.fill_preserve()
                cr.set_source_rgba(*text, 0.6)
                cr.set_line_width(1.2)
                cr.stroke()
                cr.select_font_face("JetBrains Mono", 0, 1)
                cr.set_font_size(12)
                cr.set_source_rgba(*text, 0.9)
                ext = cr.text_extents(str(lab))
                cr.move_to(x + bw_ / 2 - ext.width / 2,
                           by + bh_ / 2 + 4)
                cr.show_text(str(lab))
            mid_l, mid_r = lx + bw_ + 14, rx - 14
            for yy, forward in ((by + bh_ * 0.38, True),
                                (by + bh_ * 0.66, False)):
                if not forward and not two_way:
                    continue
                cr.move_to(mid_l, yy)
                cr.line_to(mid_r, yy)
                cr.set_source_rgba(*muted, 0.5)
                cr.set_line_width(1.2)
                cr.stroke()
                frac = (t * 0.25) % 1.0
                sx = mid_l + (frac if forward else 1 - frac) \
                    * (mid_r - mid_l)
                cr.rectangle(sx - 4.5, yy - 4.5, 9, 9)
                cr.set_source_rgba(*strong, 0.95)
                cr.fill()
            _caption()
        elif stype == "music":
            # The story is music: a staff, notes from the title's
            # hash, and a playhead sweeping them.
            top, bot = h * 0.30, h * 0.62
            cr.set_source_rgba(*muted, 0.45)
            cr.set_line_width(1.0)
            for i in range(5):
                yy = top + i * (bot - top) / 4
                cr.move_to(mx, yy)
                cr.line_to(mx + max_w, yy)
            cr.stroke()
            digest = hashlib.md5(
                (pack.get("title") or "").encode()).digest()
            play = (t * 0.07) % 1.0
            for i in range(14):
                fx = (i + 0.5) / 14
                x = mx + fx * max_w
                yy = bot - (digest[i % 16] % 9) / 8 * (bot - top)
                cr.arc(x, yy, 5.5, 0, 6.2832)
                if fx <= play:
                    cr.set_source_rgba(*strong, 0.95)
                    cr.fill()
                else:
                    cr.set_source_rgba(*bg, 1)
                    cr.fill_preserve()
                    cr.set_source_rgba(*muted, 0.8)
                    cr.set_line_width(1.1)
                    cr.stroke()
                cr.move_to(x + 5.5, yy)
                cr.line_to(x + 5.5, yy - 18)
                cr.set_source_rgba(*muted, 0.7)
                cr.stroke()
            px = mx + play * max_w
            cr.move_to(px, top - 12)
            cr.line_to(px, bot + 12)
            cr.set_source_rgba(*strong, 0.8)
            cr.set_line_width(1.4)
            cr.stroke()
            _caption()
        elif stype == "blueprint":
            # An idea: a route on a blueprint grid from where
            # the project is to the target, with the first step
            # written under it (the pack's own action_hint).
            cr.set_source_rgba(*muted, 0.14)
            cr.set_line_width(1.0)
            gx = mx
            while gx <= mx + max_w:
                cr.move_to(gx, h * 0.16)
                cr.line_to(gx, h * 0.72)
                gx += 28
            gy = h * 0.16
            while gy <= h * 0.72:
                cr.move_to(mx, gy)
                cr.line_to(mx + max_w, gy)
                gy += 28
            cr.stroke()
            x0, y0 = mx + max_w * 0.08, h * 0.58
            x1, y1 = mx + max_w * 0.86, h * 0.26
            cr.set_dash([5, 5])
            cr.move_to(x0, y0)
            cr.line_to(x0 + (x1 - x0) * 0.45, y0)
            cr.line_to(x0 + (x1 - x0) * 0.45, y1)
            cr.line_to(x1, y1)
            cr.set_source_rgba(*text, 0.75)
            cr.set_line_width(1.4)
            cr.stroke()
            cr.set_dash([])
            frac = (t * 0.18) % 1.0
            seg = frac * 3
            si = int(seg)
            sf = seg - si
            pts = [(x0, y0),
                   (x0 + (x1 - x0) * 0.45, y0),
                   (x0 + (x1 - x0) * 0.45, y1), (x1, y1)]
            ax, ay = pts[si]
            bx, by = pts[si + 1]
            cr.arc(ax + (bx - ax) * sf, ay + (by - ay) * sf,
                   4.5, 0, 6.2832)
            cr.set_source_rgba(*strong, 0.95)
            cr.fill()
            cr.rectangle(x0 - 5, y0 - 5, 10, 10)
            cr.set_source_rgba(*strong, 0.9)
            cr.fill()
            ring = 9 + 2.5 * (0.5 + 0.5 * math.sin(t * 2.0))
            cr.arc(x1, y1, ring, 0, 6.2832)
            cr.set_source_rgba(*strong, 0.9)
            cr.set_line_width(1.6)
            cr.stroke()
            hint = str(pack.get("action_hint") or "")
            if hint:
                _mono("first step", mx, h * 0.82, alpha=0.55)
                cr.select_font_face("Inter", 0, 0)
                cr.set_font_size(12.5)
                cr.set_source_rgba(*text, 0.85)
                words, lines, cur = hint.split(), [], ""
                for word in words:
                    trial = f"{cur} {word}".strip()
                    if cr.text_extents(trial).width <= max_w or not cur:
                        cur = trial
                    else:
                        lines.append(cur)
                        cur = word
                if cur:
                    lines.append(cur)
                yy = h * 0.90
                for line in lines[:2]:
                    cr.move_to(mx, yy)
                    cr.show_text(line)
                    yy += 16
            _caption()
        elif stype == "release":
            # The story is a number: a version, a multiplier, a
            # date — set huge, ringed, on a timeline.
            token = vis.get("token") or (kws[0] if kws else "NEW")
            cr.select_font_face("Inter", 0, 1)
            cr.set_font_size(64)
            ext = cr.text_extents(str(token))
            cx, cy = w / 2, h * 0.46
            cr.set_source_rgba(*text, 0.95)
            cr.move_to(cx - ext.width / 2 - ext.x_bearing,
                       cy - ext.height / 2 - ext.y_bearing)
            cr.show_text(str(token))
            ring = max(ext.width, 60) * 0.72 \
                + 6 * (0.5 + 0.5 * math.sin(t * 1.8))
            cr.arc(cx, cy, ring, 0, 6.2832)
            cr.set_source_rgba(*muted, 0.5)
            cr.set_line_width(1.2)
            cr.stroke()
            ty = h * 0.78
            cr.move_to(mx, ty)
            cr.line_to(mx + max_w, ty)
            cr.set_source_rgba(*muted, 0.4)
            cr.stroke()
            for i, fx in enumerate((0.12, 0.5, 0.88)):
                x = mx + fx * max_w
                cr.arc(x, ty, 5 if i == 1 else 3.5, 0, 6.2832)
                if i == 1:
                    cr.set_source_rgba(*strong, 0.95)
                    cr.fill()
                else:
                    cr.set_source_rgba(*muted, 0.7)
                    cr.fill()
            _caption()
    elif stype == "orbits":
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


def pack_target_at(pack, w: float, h: float, x: float, y: float):
    """Resolve a click to a target id for any pack kind."""
    if callable(pack.get("_target_at")):
        view = GameView(w, h, 0.0, {})
        try:
            return pack["_target_at"](view, x, y)
        except Exception:
            return None
    scene = pack.get("scene") or {}
    if scene.get("type") == "orbits":
        radii = ring_radii(w, h, int(scene.get("rings", 4)))
        return orbit_hit(w / 2, h / 2, radii, x, y)
    if scene.get("type") == "diagram":
        return node_hit(node_rects(scene.get("nodes") or [], w, h),
                        x, y)
    return None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class ContentEngine:
    """A shuffled deck of packs with scoring and hot reload.

    No pack is served twice until every pack has been served
    once (a full cycle); the served set optionally persists to
    `state_path` so the rule survives app restarts too."""

    def __init__(self, dirs, seed: int | None = None,
                 state_path=None, only_types=None):
        self.only_types = set(only_types) if only_types else None
        # Runtime filter chosen in the pane (All / News / Facts /
        # Quiz / Ideas). None means every type. It never changes
        # the catalogue or the persisted deck, only what is served.
        self.filter_types: set[str] | None = None
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
        self.project_dir = next(
            (d for d in self.dirs
             if d.name == "content" and d.parent.name == ".aat"),
            None)
        self.reload()

    def _dir_signature(self):
        sig = []
        for d in self.dirs:
            try:
                files = sorted(list(d.rglob("*.json"))
                               + list(d.rglob("*.py")))
                for f in files:
                    sig.append((str(f), f.stat().st_mtime_ns))
            except Exception:
                continue
        return tuple(sig)

    def reload(self):
        self._signature = self._dir_signature()
        self.packs, self.errors = scan_packs(self.dirs)
        if self.only_types:
            self.packs = [pk for pk in self.packs
                          if resolve_type(pk) in self.only_types]
        self.deck = list(self.packs)
        self._rng.shuffle(self.deck)
        ids = {p["id"] for p in self.packs}
        self._served = [i for i in self._served if i in ids]
        if self._current and self._current["id"] not in ids:
            self._current = None

    def project_packs(self, types=None) -> list:
        """Packs deposited for the current project (its
        .aat/content dir), optionally of the given types. The
        generic catalogue is not project content: creator
        bookkeeping (the unread cap) counts these only."""
        if not getattr(self, "project_dir", None):
            return []
        base = str(Path(self.project_dir))
        return [p for p in self.packs_for(types)
                if str(p.get("_source") or "").startswith(base)]

    def set_live_packs(self, packs) -> None:
        """Replace the live packs (pull requests) in the pool.

        Live packs are not catalogue files: they are re-read
        from the project's GitHub remote and re-injected. They
        join the same deck and the same seen bookkeeping as
        every other card."""
        packs = list(packs)
        self.packs = [p for p in self.packs
                      if resolve_type(p) != "pr"] + packs
        self.deck = [p for p in self.deck
                     if resolve_type(p) != "pr"] + packs

    def set_project_dir(self, new_dir) -> bool:
        """Re-point the project content directory (the terminal
        wandered into another project). The project dir is the
        tracked `project_dir` entry of `dirs`; swapping it and
        reloading makes the new project's packs first-class."""
        new_dir = Path(new_dir)
        old = getattr(self, "project_dir", None)
        if old is not None and Path(old) == new_dir:
            return False
        if old is not None and Path(old) in self.dirs:
            self.dirs[self.dirs.index(Path(old))] = new_dir
        else:
            self.dirs.insert(0, new_dir)
        self.project_dir = new_dir
        self.reload()
        return True

    def maybe_reload(self):
        if self._dir_signature() != self._signature:
            self.reload()
            return True
        return False

    # -- type filter (All / News / Facts / Quiz / Ideas) -----------
    def set_type_filter(self, types) -> bool:
        """Serve only these content types from now on.

        `None`, empty or all four types mean "All". The filter is
        a view over the same catalogue: it changes neither the
        persisted deck nor the vector index."""
        new = set(types) if types else None
        if new is not None and new >= set(TYPES):
            new = None
        if new == self.filter_types:
            return False
        self.filter_types = new
        return True

    def has_unseen(self) -> bool:
        """Any pack in the current filter not yet served?"""
        return any(p["id"] not in self._served
                   for p in self.packs_for())

    def matches(self, pack: dict) -> bool:
        return self.filter_types is None or \
            resolve_type(pack) in self.filter_types

    def packs_for(self, types=None) -> list[dict]:
        chosen = set(types) if types else self.filter_types
        if not chosen:
            return list(self.packs)
        return [p for p in self.packs
                if resolve_type(p) in chosen]

    def type_counts(self) -> dict:
        counts = {t: 0 for t in TYPES}
        for pack in self.packs:
            counts[resolve_type(pack)] = \
                counts.get(resolve_type(pack), 0) + 1
        return counts

    def reset_history(self) -> int:
        """Forget which packs were watched: the deck starts over.

        Reactions and the catalogue are untouched — this is the
        "reset watched history" action, not a preference wipe."""
        n = len(self._served)
        self._served = []
        self._last = None
        self.deck = list(self.packs)
        self._rng.shuffle(self.deck)
        self._save_state()
        return n

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
        pool = [p for p in self.deck if self.matches(p)]
        if not pool:
            return None
        game = next((p for p in pool
                     if p["id"] not in self._served), None)
        if game is None:
            # Full cycle done: start this pool over, but never
            # serve the same pack twice in a row across the
            # boundary. Only the pool's own ids become unseen
            # again — other types keep their deck progress.
            ids = {p["id"] for p in pool}
            self._served = [i for i in self._served if i not in ids]
            self._rng.shuffle(self.deck)
            candidates = [p for p in self.deck if self.matches(p)]
            if len(candidates) > 1 \
                    and candidates[0]["id"] == self._last:
                candidates.append(candidates.pop(0))
            game = candidates[0] if candidates else None
            if game is None:
                return None
        self._served.append(game["id"])
        self._last = game["id"]
        self._current = game
        self._save_state()
        return game

    def focus(self, pack: dict) -> dict:
        """Make an already-served pack current without deck
        bookkeeping (the pane's back/forward history)."""
        self._current = pack
        return pack

    def serve_specific(self, pack: dict) -> dict:
        """Serve one chosen pack (selector path) with the same
        no-repeat bookkeeping as deck serving."""
        self.maybe_reload()
        self._served.append(pack["id"])
        self._last = pack["id"]
        self._current = pack
        self._save_state()
        return pack

    def pack_by_id(self, pack_id: str) -> dict | None:
        return next((p for p in self.packs
                     if p["id"] == pack_id), None)

    def current(self) -> dict | None:
        return self._current

    def progress(self) -> tuple[int, int]:
        pool = self.packs_for()
        ids = {p["id"] for p in pool}
        served = sum(1 for i in self._served if i in ids)
        return (served, len(pool))

    def answer(self, target_id: str) -> dict:
        """Score an answer to the current pack. Cards never score."""
        game = self._current
        if game is None:
            return {"correct": None, "explanation": ""}
        if resolve_type(game) != "game":
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
