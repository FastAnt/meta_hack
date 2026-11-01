"""Context-based content selection (replaces random serving).

Flow, per the owner's sketch:

1. Build a context: current project + its researched domain
   + the agent's current task.
2. Retrieve by vector similarity. Project content
   (`<project>/.aat/content`) first: the closest 10 not yet
   shown. If the project has nothing that fits, the global
   catalogue's top 10 — and in parallel we evaluate whether
   new project content should be generated.
3. System-1 (Liquid d1.1) picks the single best element from
   the 10; without it, the top-ranked element wins.
4. Reactions bend the ranking: similarity to the liked
   centroid boosts, similarity to the disliked centroid
   penalizes — and generation requests carry liked/disliked
   exemplars so new content avoids what the user rejected.

Everything degrades gracefully: no index yet, no embeddings,
no Liquid → the caller falls back to the plain deck.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from . import content_engine as ce
from .embeddings import (centroid, cosine, doc_text, pack_text,
                         text_hash)
from . import embeddings as _emb

GENERATION_COOLDOWN_S = 1800
PROJECT_FIT = 0.35
GLOBAL_FIT = 0.50


class ContentSelector:
    def __init__(self, engine, store, embedder, liquid, dlog,
                 project_path, project_content_dir):
        self.engine = engine
        self.store = store
        self.embedder = embedder
        self.liquid = liquid
        self.dlog = dlog or (lambda tag, msg: None)
        self.project_path = str(project_path)
        self.project_content_dir = Path(project_content_dir)
        self.project_ids: set[str] = set()
        self._index_lock = threading.Lock()
        self._gen_lock = threading.Lock()
        self.indexed = 0

    # -- indexing ------------------------------------------------
    def index(self):
        """Embed every catalogue pack whose text changed.
        Runs in a background thread; safe to call again."""
        with self._index_lock:
            try:
                packs, _ = ce.scan_packs([self.project_content_dir])
                self.project_ids = {p["id"] for p in packs}
            except Exception:
                self.project_ids = set()
            todo = []
            for pack in self.engine.packs:
                text = doc_text(pack)
                meta = self.store.item_meta(pack["id"])
                if meta and meta[0] == text_hash(text) \
                        and meta[1] == self.embedder.model_id:
                    continue
                todo.append((pack, text))
            if not todo:
                self.indexed = len(self.engine.packs)
                return
            try:
                vectors = self.embedder.embed(
                    [t for _, t in todo], kind="doc")
            except Exception as exc:
                if not isinstance(self.embedder,
                                  _emb.HashingEmbedder):
                    self.dlog("content",
                              "embedding model unavailable, "
                              "falling back to hashing vectors")
                    self.embedder = _emb.HashingEmbedder()
                    return self.index()
                self.dlog("content",
                          f"embedding failed: {exc}"[:120])
                return
            for (pack, text), vec in zip(todo, vectors):
                src = "project" if pack["id"] in self.project_ids \
                    else "global"
                self.store.upsert_item(
                    pack, src, self.embedder.model_id, vec,
                    text_hash(text))
            self.indexed = len(self.engine.packs)
            self.store.purge_other_models(self.embedder.model_id)
            self.dlog("content", f"vector index ready: "
                                 f"{self.indexed} items "
                                 f"({self.embedder.model_id})")

    # -- ranking ---------------------------------------------------
    def _rank(self, context_text, project_key, exclude, types=None):
        vectors = dict(self.store.all_vectors(
            self.embedder.model_id))
        if not vectors:
            return []
        query = self.embedder.embed([context_text],
                                    kind="query")[0]
        liked = centroid([vectors[i] for i in
                          self.store.reaction_ids(project_key, "like")
                          if i in vectors])
        disliked = centroid(
            [vectors[i] for i in
             self.store.reaction_ids(project_key, "dislike")
             if i in vectors])
        packs = {p["id"]: p for p in self.engine.packs}
        wanted = set(types) if types else None
        ranked = []
        for pid, vec in vectors.items():
            if pid in exclude or pid not in packs:
                continue
            if wanted and ce.resolve_type(packs[pid]) not in wanted:
                continue
            score = cosine(query, vec)
            if liked:
                score += 0.15 * cosine(liked, vec)
            if disliked:
                score -= 0.25 * cosine(disliked, vec)
            ranked.append((score, pid))
        ranked.sort(reverse=True)
        return ranked

    def pick(self, context_text, project_key, exclude, types=None):
        """Returns (pack_id, source, best_sim) or None.

        `types` restricts the pick to the pane's active content
        filters; the catalogue and the index stay global."""
        if not context_text.strip():
            return None
        try:
            ranked = self._rank(context_text, project_key, exclude,
                                types)
        except Exception as exc:
            self.dlog("content", f"selector failed: {exc}"[:120])
            return None
        if not ranked:
            return None
        packs = {p["id"]: p for p in self.engine.packs}
        project_pool = [r for r in ranked
                        if r[1] in self.project_ids]
        if project_pool:
            top, source = project_pool[:10], "project"
        else:
            top, source = ranked[:10], "global"
        chosen = top[0][1]
        if self.liquid is not None:
            try:
                pick = self.liquid.pick_best(
                    context_text,
                    [packs[pid] for _, pid in top])
            except Exception:
                pick = None
            if pick and pick[0] in packs:
                chosen = pick[0]
                self.dlog("content",
                          f"system-1 chose {chosen} "
                          f"(conf {pick[1]:.2f}) from top "
                          f"{len(top)} [{source}]")
        return chosen, source, top[0][0]

    # -- generation decision ---------------------------------------
    def evaluate_generation(self, context_text, project_key,
                            best_project_sim, best_global_sim):
        """Decide whether new project content should be created,
        and if so file a request the researcher agent consumes.
        Returns the request dict or None."""
        with self._gen_lock:
            reasons = []
            if not self.project_ids:
                reasons.append("no project content yet")
            elif best_project_sim < PROJECT_FIT:
                reasons.append("project content does not match "
                               "the current context")
            if not reasons:
                return None
            if best_global_sim >= GLOBAL_FIT:
                return None  # the global catalogue covers it
            last = self.store.last_generation(project_key)
            if last and time.time() - last < GENERATION_COOLDOWN_S:
                return None
            if self.liquid is not None:
                try:
                    answers = self.liquid.decide(
                        f"Project context: {context_text}\n"
                        f"Closest existing content similarity: "
                        f"project {best_project_sim:.2f}, global "
                        f"{best_global_sim:.2f}.", {
                            "worth_generating": {
                                "type": "noul",
                                "instructions":
                                    "Is there a real content gap "
                                    "here — would a new mini-game, "
                                    "fact, news item or idea "
                                    "tailored to this project and "
                                    "task serve the waiting "
                                    "developer better than generic "
                                    "catalogue content?"}})
                    ans = (answers or {}).get("worth_generating") or {}
                    if ans.get("noul") is not None \
                            and float(ans["noul"]) < 0.5:
                        self.dlog("content", "system-1: no content "
                                             "gap, no generation")
                        return None
                except Exception:
                    pass
            counts = {}
            packs = {p["id"]: p for p in self.engine.packs}
            for pid in self.project_ids:
                t = packs.get(pid, {}).get("type", "game")
                counts[t] = counts.get(t, 0) + 1
            want_type = min(
                ("game", "fact", "news", "idea"),
                key=lambda t: counts.get(t, 0))
            liked_titles = [packs[i].get("title", "") for i in
                            self.store.reaction_ids(project_key,
                                                    "like")
                            if i in packs][:3]
            avoid_titles = [packs[i].get("title", "") for i in
                            self.store.reaction_ids(project_key,
                                                    "dislike")
                            if i in packs][:3]
            request = {
                "at": int(time.time()),
                "project": project_key,
                "context": context_text[:600],
                "want_type": want_type,
                "reason": "; ".join(reasons),
                "liked_examples": liked_titles,
                "avoid_similar_to": avoid_titles,
            }
            out_dir = Path(self.project_path) / ".aat" / "research"
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
                with open(out_dir / "generation_requests.jsonl",
                          "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(request,
                                        ensure_ascii=False) + "\n")
            except Exception as exc:
                self.dlog("content",
                          f"generation request failed: {exc}"[:120])
                return None
            self.store.log_generation(project_key, request["reason"])
            self.dlog("content",
                      f"generation requested: {want_type} "
                      f"({request['reason']})")
            return request
