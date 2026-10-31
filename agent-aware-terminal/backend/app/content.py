"""Phase 4: ContextContentProvider.

Builds overlay content from the ArchitectureModel. Two layers:
  * deterministic cards — facts computed from the real project index
    (file counts, connections, entry points); never hallucinated;
  * optional Muse Spark enrichment — one-sentence explanations and
    a quiz per component, requested as structured JSON and cached.
Cards can be liked/saved for later review (TZ 4.5).
"""
from __future__ import annotations

import hashlib
import json
import os


def deterministic_cards(project: dict, touched_component: dict | None = None,
                        agent_activity: str = "") -> list[dict]:
    cards: list[dict] = []
    comps = {c["id"]: c for c in project["components"]}
    cards.append({
        "id": "overview", "kind": "overview",
        "title": project["name"],
        "body": project["summary"] or
                f"{project['total_files']} files · "
                f"{len(project['components'])} components",
        "meta": {"components": len(project["components"]),
                 "files": project["total_files"],
                 "entry_points": project["entry_points"]},
        "source": "project_indexer",
    })
    ordered = sorted(project["components"], key=lambda c: -c["file_count"])
    if touched_component:
        ordered.sort(key=lambda c: 0 if c["id"] == touched_component["id"] else 1)
    for comp in ordered[:8]:
        links = [comps[c]["name"] for c in comp["connections"] if c in comps]
        body = comp["description"]
        if links:
            body += f" Connected to: {', '.join(links)}."
        cards.append({
            "id": f"comp-{comp['id']}", "kind": "component",
            "title": comp["name"], "body": body,
            "component_id": comp["id"], "path": comp["path"],
            "files": comp["files"][:5],
            "meta": {"file_count": comp["file_count"],
                     "languages": comp["languages"],
                     "connections": links},
            "highlight": bool(touched_component
                              and comp["id"] == touched_component["id"]),
            "source": "project_indexer",
        })
    if agent_activity:
        cards.insert(1, {
            "id": "agent-now", "kind": "agent",
            "title": "Agent right now",
            "body": agent_activity,
            "component_id": touched_component["id"] if touched_component else None,
            "highlight": True, "source": "agent_event",
        })
    return cards


SPARK_SYSTEM = (
    "You explain a software project to its own developer while a coding "
    "agent works on it. Be concrete, use only the provided architecture "
    "data, never invent files or connections."
)


def spark_cards(project: dict, meta) -> list[dict]:
    """Ask Muse Spark for explanations + one quiz; returns extra cards."""
    compact = {
        "name": project["name"], "summary": project["summary"][:500],
        "components": [{
            "name": c["name"], "files": c["files"][:8],
            "file_count": c["file_count"],
            "connections": [o["name"] for o in project["components"]
                            if o["id"] in c["connections"]],
        } for c in project["components"][:8]],
    }
    user = (
        "Architecture data:\n" + json.dumps(compact, ensure_ascii=False) +
        "\n\nReturn JSON: {\"explanations\": [{\"component\": name, "
        "\"text\": one sentence, in English, what this component is for}], "
        "\"quiz\": {\"question\": a question in English about how the "
        "components relate, \"options\": [3 short options], "
        "\"answer_index\": int, \"why\": one sentence in English}}"
    )
    data = meta.chat_json(SPARK_SYSTEM, user)
    if not isinstance(data, dict):
        return []
    cards: list[dict] = []
    by_name = {c["name"]: c for c in project["components"]}
    for item in data.get("explanations") or []:
        comp = by_name.get(item.get("component"))
        if comp and item.get("text"):
            cards.append({
                "id": f"spark-{comp['id']}", "kind": "explanation",
                "title": comp["name"], "body": item["text"],
                "component_id": comp["id"], "path": comp["path"],
                "source": "muse_spark",
            })
    quiz = data.get("quiz") or {}
    if quiz.get("question") and quiz.get("options"):
        cards.append({
            "id": "quiz-1", "kind": "quiz", "title": "Check yourself",
            "body": quiz["question"], "options": quiz["options"],
            "answer_index": quiz.get("answer_index", 0),
            "why": quiz.get("why", ""), "source": "muse_spark",
        })
    return cards


class CardStore:
    """Saved/liked cards persisted as JSON (LocalStorage analogue, TZ 3.3)."""

    def __init__(self, path: str):
        self.path = path
        self.cards: list[dict] = []
        if os.path.isfile(path):
            try:
                self.cards = json.loads(open(path).read())
            except Exception:
                self.cards = []

    def save(self, card: dict) -> dict:
        card = dict(card)
        card["saved_at"] = card.get("saved_at") or \
            __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime())
        key = card.get("id") or hashlib.sha1(
            json.dumps(card, sort_keys=True).encode()).hexdigest()[:10]
        card["id"] = key
        self.cards = [c for c in self.cards if c.get("id") != key]
        self.cards.append(card)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as fh:
            json.dump(self.cards, fh, ensure_ascii=False, indent=1)
        return card
