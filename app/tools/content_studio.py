#!/usr/bin/env python3
"""Content Studio: a local web app for building the game's question bank.

    python3 tools/content_studio.py            # serves http://127.0.0.1:8765
    python3 tools/content_studio.py --port 9000 --no-open
    python3 tools/content_studio.py --lan      # also reachable from your phone on the same network (no login; trusted network only)

Everything is a *node* in a lineage graph, stored as plain JSON under
design/content/ (diff-friendly, lives in git):

    source  -> fact -> question -> variant (paraphrase / harder / easier /
    (web page,   (one    (a game     true_false ...)
     record)     claim)   card)

Each node records where it came from (which AI run, with what instructions,
which parent nodes), and every parent is one click away in the UI. Agent
stages shell out to the `claude` CLI headlessly (uses your existing Claude
Code login) or to a built-in `mock` backend for trying the app offline.
Approved question/variant nodes are exported into data/<topic>/curriculum.json
in the exact schema the game loads.

Standard library only; the UI is plain HTML/JS in tools/content_studio_ui/.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import socket
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTENT = Path(os.environ.get("STUDIO_CONTENT_DIR") or ROOT / "design" / "content")
UI_DIR = Path(__file__).resolve().parent / "content_studio_ui"
DATA = Path(os.environ.get("STUDIO_DATA_DIR") or ROOT / "data")
SRS_ENGINE = ROOT / "autoload" / "srs_engine.gd"

# The difficulty ladder. One list drives validation, prompts, scoring and the UI. Anchors are
# "what share of casual fans of the topic know the answer", which an AI can judge far better than
# a vague "hard"/"easy". To rename a tier or change its score, edit it here.
TIERS = [
    ("beginner", 100, "nearly everyone who has seen/read/used the subject knows it (about 70%+ of casual fans)"),
    ("easy", 125, "most casual fans know it (about 50-70%)"),
    ("intermediate", 150, "a fair number of casual fans know it (about 30-50%)"),
    ("advanced", 220, "only keen fans know it (about 10-30%)"),
    ("expert", 300, "only dedicated specialists know it (under 10%)"),
]
TIER_NAMES = [t[0] for t in TIERS]
DIFFICULTY_SCORES = {name: score for name, score, _ in TIERS}
SPREAD_SHARES = {"beginner": 0.20, "easy": 0.20, "intermediate": 0.30, "advanced": 0.20, "expert": 0.10}
EXPLANATION_MAX = 240
SNARK_MAX = 240     # the author's snarky response: a line or two
STORY_MAX = 1000    # a story beat can run longer
MAX_FACTS_PER_SOURCE = 40   # per run, so one response stays a sensible size; run it again for more
EXPLANATION_SPEC = ('"explanation" (optional real "did you know": a plain, true fact about the answer in one or two sentences, at most 30 words, each adding something new (no filler); '
                    'no jokes, sarcasm or story - the author writes those in separate fields; using ONLY information already in the fact, its context or the card shown - never add outside claims; "" if there is nothing to add)')
DEPARTMENTS = ["Geography", "Entertainment", "History", "Arts & Literature", "Science & Nature", "Sports & Leisure"]
KINDS = ("source", "fact", "question", "variant")
STATUSES = ("draft", "approved", "rejected")
QUESTION_KINDS = ("question", "variant")
# Fields whose change makes an earlier accuracy check out of date, per node kind.
CHECK_FIELDS = {"source": ("title", "url", "summary", "excerpt"), "fact": ("text", "context"),
                "question": ("question", "answer", "choices", "explanation"), "variant": ("question", "answer", "choices", "explanation")}
SUGGEST_FIELDS = ("question", "answer", "choices", "explanation")   # the only fields an accuracy check may propose changing

VARIANT_TYPES = {
    "paraphrase": "Rewrite the question with clearly different wording and sentence structure (not a synonym swap). Same fact, same difficulty. Use a different set of distractors where you can; the correct answer must stay correct.",
    "harder": "Write a harder version that tests the same fact more deeply: a less direct question and/or more plausible distractors. Move difficulty up one tier if possible (expert is the cap).",
    "easier": "Write an easier version of the same fact: a more direct question and clearly distinguishable distractors. Move difficulty down one tier if possible (beginner is the floor).",
    "true_false": "Turn the question into a single true-or-false statement about the same fact. Put the statement in `question`, set `answer` to \"True\" or \"False\", and `choices` to [\"True\", \"False\"]. The statement must be exactly true or exactly false, never ambiguous. Vary which it is across a batch.",
}

OLD_DIFFICULTY_SENTENCE = (
    "Difficulty: beginner = most players know it; intermediate = a keen fan knows it; "
    "advanced = specialist or surprising. Card titles are 2-4 word labels that do not give away the answer."
)
CANON_ANGLE = (
    "For works with a story or universe (books, films, shows, games): favour in-universe canon - characters, places, plot events, "
    "quotes, lore, and what a fan would remember after one watch or read. Use official sites and well-maintained fan wikis for canon. "
    "Production and publication trivia (box office, page counts, runtimes, release or premiere dates) should be at most about one "
    "question in six, and only when it is genuinely famous."
)
DEPARTMENT_ANGLES = {"Entertainment": CANON_ANGLE, "Arts & Literature": CANON_ANGLE}

DEFAULT_INSTRUCTIONS = {
    "global": (
        "You are helping build the question bank for a school-themed trivia roguelike deckbuilder. "
        "Players answer multiple-choice trivia cards and are graded like a report card.\n\n"
        "Voice: write like a sharp human trivia host, not an AI. Plain, concrete, natural phrasing. "
        "No filler (\"Which of the following\", \"According to\"), no hedging, no summarizing tone, "
        "no stock AI vocabulary. Prefer specific nouns to abstractions. A question fits in one or two short lines.\n\n"
        "Accuracy: every claim must be verifiable from the provided source or fact. Never invent anything. "
        "If you are unsure of a detail, leave it out.\n\n"
        "Distractors: plausible and in the same category as the answer; never jokes; never also-correct."
    ),
    "departments": dict(DEPARTMENT_ANGLES),
    "topics": {},
    "stages": {
        "find_sources": "Prefer encyclopedias, official organisation sites, and museum, archive or government pages.",
        "extract_facts": "Favour facts that are stable over time and have one clear correct answer.",
        "draft_questions": "Each question must have exactly one defensible correct answer.",
        "make_variants": "",
    },
}

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:limit] or "x"


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def extract_json(text: str):
    """Pull the first JSON object out of a model reply (tolerates fences/prose)."""
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        try:
            return json.loads(fence.group(1))
        except ValueError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start:end + 1])
    raise ValueError("no JSON object found in the model's reply")


def clean_str(value) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()


def source_ref(node: dict) -> str:
    """The url (or title) of the nearest source above a node, for citing what an explanation came from."""
    queue, seen = [node["id"]], set()
    while queue:
        nid = queue.pop(0)
        if nid in seen or nid not in STORE.nodes:
            continue
        seen.add(nid)
        n = STORE.nodes[nid]
        if n["kind"] == "source":
            return n["payload"].get("url") or n["payload"].get("title") or ""
        queue.extend(n["parent_ids"])
    return ""


def topic_facets(topic_id: str) -> list[str]:
    cfg = STORE.topic_cfg(topic_id) if topic_id else None
    return list(cfg.get("facets") or []) if cfg else []


def clean_facets(raw) -> list[str]:
    """A topic's facet list: plain short labels, no duplicates (case-insensitive), at most 12."""
    out: list[str] = []
    for item in raw or []:
        f = clean_str(item)
        if not f:
            continue
        if len(f) > 30:
            raise ApiError(400, f"facet {f[:20]!r}... is too long (30 characters max)")
        if f.lower() not in [x.lower() for x in out]:
            out.append(f)
    if len(out) > 12:
        raise ApiError(400, "a topic can have at most 12 facets")
    return out


def normalize_question(payload: dict, input_type: str | None = None, facets: list[str] | None = None) -> tuple[dict, list[str]]:
    """Coerce a question payload into the game's card shape; return (clean, problems)."""
    problems: list[str] = []
    itype = input_type or payload.get("input_type") or "multiple_choice"
    out = {
        "input_type": itype,
        "card_id": clean_str(payload.get("card_id")),
        "title": clean_str(payload.get("title")),
        "facet": clean_str(payload.get("facet")),
        "question": clean_str(payload.get("question")),
        "explanation": clean_str(payload.get("explanation")),
        "explanation_source": clean_str(payload.get("explanation_source")),
        "explanation_verified": bool(payload.get("explanation_verified")) and bool(clean_str(payload.get("explanation"))),
        "explanation_mock": bool(payload.get("explanation_mock")) and bool(clean_str(payload.get("explanation"))),
        "snark": clean_str(payload.get("snark")),   # the author's own snarky response (optional); the AI never writes it
        "story": clean_str(payload.get("story")),   # a story beat (optional, left blank for now); the AI never writes it
        "answer": clean_str(payload.get("answer")),
        "choices": [clean_str(c) for c in (payload.get("choices") or [])],
        "difficulty": clean_str(payload.get("difficulty")).lower(),
    }
    if out["difficulty"] not in DIFFICULTY_SCORES:
        problems.append("difficulty must be one of: " + ", ".join(TIER_NAMES))
        out["difficulty"] = out["difficulty"] if out["difficulty"] else "intermediate"
    out["base_score"] = DIFFICULTY_SCORES.get(out["difficulty"], 150)
    if not out["question"]:
        problems.append("question is empty")
    if len(out["explanation"]) > EXPLANATION_MAX:
        problems.append(f"explanation is {len(out['explanation'])} characters (max {EXPLANATION_MAX})")
    if len(out["snark"]) > SNARK_MAX:
        problems.append(f"snarky response is {len(out['snark'])} characters (max {SNARK_MAX})")
    if len(out["story"]) > STORY_MAX:
        problems.append(f"story is {len(out['story'])} characters (max {STORY_MAX})")
    if facets:
        # A card's face label is its facet. Older cards that only have an AI-written title stay valid until they get one.
        if out["facet"]:
            match = next((f for f in facets if f.lower() == out["facet"].lower()), None)
            if match:
                out["facet"] = match
                out["title"] = match
            else:
                problems.append(f"facet {out['facet']!r} isn't in this topic's list ({', '.join(facets)})")
        elif not out["title"]:
            problems.append("pick a facet from the topic's list")
    else:
        out["facet"] = ""
        if not out["title"]:
            problems.append("title is empty")
    if not out["answer"]:
        problems.append("answer is empty")
    if itype == "true_false":
        out["choices"] = ["True", "False"]
        if out["answer"].lower() in ("true", "false"):
            out["answer"] = out["answer"].capitalize()
        else:
            problems.append("true/false answer must be True or False")
    else:
        if len(out["choices"]) != 4:
            problems.append("needs exactly 4 choices (has %d)" % len(out["choices"]))
        if len(set(c.lower() for c in out["choices"])) != len(out["choices"]):
            problems.append("choices contain duplicates")
        if out["answer"] and out["answer"] not in out["choices"]:
            lowered = {c.lower(): c for c in out["choices"]}
            if out["answer"].lower() in lowered:
                out["answer"] = lowered[out["answer"].lower()]
            else:
                problems.append("answer is not one of the choices")
    return out, problems


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

class Store:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        (CONTENT / "topics").mkdir(parents=True, exist_ok=True)
        (CONTENT / "runs").mkdir(parents=True, exist_ok=True)
        self.nodes: dict[str, dict] = {}
        self.runs: dict[str, dict] = {}
        self.config = self._load_or_seed_config()
        self._load_all()
        self._migrate_topic_approval()
        notes = self.config.get("instructions", {}).get("stages", {})
        if "revise" in notes:   # the "revise with note" stage was removed
            del notes["revise"]
            self.save_config()

    # -- config ------------------------------------------------------------
    def _load_or_seed_config(self) -> dict:
        path = CONTENT / "config.json"
        if path.exists():
            cfg = json.loads(path.read_text(encoding="utf-8"))
        else:
            cfg = {
                "backend": "claude_cli",
                "model": "",
                "concurrency": 3,
                "budget_usd": 1.0,
                "timeout_s": 900,
                "game_input_types": ["multiple_choice"],
                "instructions": json.loads(json.dumps(DEFAULT_INSTRUCTIONS)),
                "topics": self._seed_topics(),
            }
            atomic_write(path, json.dumps(cfg, indent=2, ensure_ascii=False))
        ins = cfg.setdefault("instructions", json.loads(json.dumps(DEFAULT_INSTRUCTIONS)))
        migrated = False
        if OLD_DIFFICULTY_SENTENCE in ins.get("global", ""):
            ins["global"] = ins["global"].replace(OLD_DIFFICULTY_SENTENCE, "")
            migrated = True
        for sentence in ("\n\nCard titles are 2-4 word labels that do not give away the answer.", "Card titles are 2-4 word labels that do not give away the answer."):
            if sentence in ins.get("global", ""):  # a card's label is now its facet (or a plain title); the prompt says so per topic
                ins["global"] = ins["global"].replace(sentence, "").rstrip()
                migrated = True
        depts = ins.setdefault("departments", {})
        for dept, text in DEPARTMENT_ANGLES.items():
            if not depts.get(dept, "").strip():
                depts[dept] = text
                migrated = True
        if migrated and path.exists():
            atomic_write(path, json.dumps(cfg, indent=2, ensure_ascii=False))
        for key, value in (("backend", "claude_cli"), ("model", ""), ("concurrency", 3), ("budget_usd", 1.0),
                           ("timeout_s", 900), ("game_input_types", ["multiple_choice"]), ("require_verified_explanations", True), ("export_extras", True)):
            cfg.setdefault(key, value)
        return cfg

    def _seed_topics(self) -> list[dict]:
        folders: list[str] = []
        if SRS_ENGINE.exists():
            folders = re.findall(r"res://data/([A-Za-z0-9_]+)/curriculum\.json", SRS_ENGINE.read_text(encoding="utf-8"))
        for p in sorted(DATA.glob("*/curriculum.json")):
            if p.parent.name not in folders:
                folders.append(p.parent.name)
        topics = []
        for folder in folders:
            path = DATA / folder / "curriculum.json"
            if not path.exists():
                continue
            doc = json.loads(path.read_text(encoding="utf-8"))
            topics.append({
                "id": folder, "name": doc.get("topic", folder), "department": doc.get("department", ""),
                "folder": folder, "target": 24,
            })
        return topics

    def _migrate_topic_approval(self) -> None:
        """Topics gained an explicit 'approved' flag. Existing active topics that already have approved cards keep exporting."""
        changed = False
        for t in self.config["topics"]:
            if "approved" not in t:
                t["approved"] = t.get("stage", "active") == "active" and any(
                    n["topic"] == t["id"] and n["kind"] in QUESTION_KINDS and n["status"] == "approved" for n in self.nodes.values())
                changed = True
        if changed:
            self.save_config()

    def reconcile_approval(self, topic_id: str) -> None:
        """A topic stays approved only while it has at least one approved card."""
        t = self.topic_cfg(topic_id)
        if t and t.get("approved") and not any(n["topic"] == topic_id and n["kind"] in QUESTION_KINDS and n["status"] == "approved" for n in self.nodes.values()):
            t["approved"] = False
            self.save_config()

    def save_config(self) -> None:
        with self.lock:
            atomic_write(CONTENT / "config.json", json.dumps(self.config, indent=2, ensure_ascii=False))

    def topic_cfg(self, topic_id: str) -> dict | None:
        return next((t for t in self.config["topics"] if t["id"] == topic_id), None)

    def topics_in_stage(self, stage: str) -> list[dict]:
        return [t for t in self.config["topics"] if t.get("stage", "active") == stage]

    # -- loading / saving -------------------------------------------------
    def _load_all(self) -> None:
        for path in sorted((CONTENT / "topics").glob("*.json")):
            doc = json.loads(path.read_text(encoding="utf-8"))
            for node in doc.get("nodes", []):
                self.nodes[node["id"]] = node
        for path in sorted((CONTENT / "runs").glob("*.json")):
            run = json.loads(path.read_text(encoding="utf-8"))
            if run.get("status") in ("queued", "running"):
                run["status"] = "failed"
                run["error"] = "Interrupted: the studio was restarted while this run was in progress."
            self.runs[run["id"]] = run

    def save_topic(self, topic_id: str) -> None:
        with self.lock:
            nodes = sorted((n for n in self.nodes.values() if n["topic"] == topic_id), key=lambda n: (n["created_at"], n["id"]))
            atomic_write(CONTENT / "topics" / f"{topic_id}.json", json.dumps({"topic": topic_id, "nodes": nodes}, indent=2, ensure_ascii=False))

    def save_run(self, run: dict) -> None:
        with self.lock:
            atomic_write(CONTENT / "runs" / f"{run['id']}.json", json.dumps(run, indent=2, ensure_ascii=False))

    # -- nodes -------------------------------------------------------------
    def new_id(self, prefix: str, pool: dict) -> str:
        while True:
            candidate = f"{prefix}_{uuid.uuid4().hex[:6]}"
            if candidate not in pool:
                return candidate

    def unique_card_id(self, topic_id: str, title: str) -> str:
        taken = {n["payload"].get("card_id") for n in self.nodes.values() if n["kind"] in QUESTION_KINDS}
        base = f"{topic_id}_{slug(title, 28)}"
        candidate, i = base, 2
        while candidate in taken:
            candidate = f"{base}_{i}"
            i += 1
        return candidate

    def create_node(self, topic_id: str, kind: str, payload: dict, parent_ids: list[str] | None = None,
                    variant_type: str | None = None, status: str = "draft", provenance: dict | None = None) -> dict:
        with self.lock:
            if kind in QUESTION_KINDS and not payload.get("card_id"):
                payload["card_id"] = self.unique_card_id(topic_id, payload.get("title") or payload.get("question") or "card")
            node = {
                "id": self.new_id(kind[0], self.nodes),
                "kind": kind,
                "topic": topic_id,
                "parent_ids": list(parent_ids or []),
                "variant_type": variant_type,
                "status": status,
                "payload": payload,
                "provenance": provenance or {"origin": "human"},
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "history": [],
            }
            self.nodes[node["id"]] = node
            self.save_topic(topic_id)
            return node

    def children_of(self, node_id: str) -> list[dict]:
        return [n for n in self.nodes.values() if node_id in n["parent_ids"]]

    def descendants_of(self, node_id: str) -> list[dict]:
        """Everything derived from this node at any depth, each once, in creation order."""
        seen: set[str] = set()
        frontier = [node_id]
        while frontier:
            nid = frontier.pop()
            for child in self.children_of(nid):
                if child["id"] not in seen:
                    seen.add(child["id"])
                    frontier.append(child["id"])
        return sorted((self.nodes[i] for i in seen), key=lambda n: (n["created_at"], n["id"]))

    def ancestors_of(self, node_id: str) -> list[dict]:
        """All ancestors, roots first (ordered by depth from this node, deepest first)."""
        seen: dict[str, int] = {}
        frontier = [(p, 1) for p in self.nodes[node_id]["parent_ids"]]
        while frontier:
            nid, depth = frontier.pop(0)
            if nid not in self.nodes:
                continue
            if nid in seen and seen[nid] >= depth:
                continue
            seen[nid] = depth
            frontier.extend((p, depth + 1) for p in self.nodes[nid]["parent_ids"])
        ordered = sorted(seen.items(), key=lambda kv: -kv[1])
        return [dict(self.nodes[nid], depth=depth) for nid, depth in ordered]

    def snippet(self, node: dict) -> str:
        p = node["payload"]
        if node["kind"] == "source":
            return p.get("title") or p.get("url") or "(source)"
        if node["kind"] == "fact":
            return p.get("text", "")
        return p.get("question", "")

    def light(self, node: dict, child_counts: dict[str, int]) -> dict:
        prov = node.get("provenance", {})
        return {
            "id": node["id"], "kind": node["kind"], "variant_type": node["variant_type"], "status": node["status"],
            "snippet": self.snippet(node), "parent_ids": node["parent_ids"], "child_count": child_counts.get(node["id"], 0),
            "origin": prov.get("origin"), "mock": bool(prov.get("mock")), "created_at": node["created_at"],
            "difficulty": node["payload"].get("difficulty") or node["payload"].get("difficulty_hint"),
            "input_type": node["payload"].get("input_type"), "source_type": node["payload"].get("source_type"),
            "check": check_state(node) if node["kind"] in QUESTION_KINDS else None,
            "has_suggestion": bool(node.get("suggestion")),
            "has_fact": bool(fact_ancestors(node)) if node["kind"] in QUESTION_KINDS else None,
        }

    # -- summaries ---------------------------------------------------------
    def topic_summary(self, topic_cfg: dict) -> dict:
        tid = topic_cfg["id"]
        nodes = [n for n in self.nodes.values() if n["topic"] == tid]
        counts = {k: {s: 0 for s in STATUSES} for k in KINDS}
        for n in nodes:
            counts[n["kind"]][n["status"]] += 1
        parents_with_children: dict[str, set] = {}
        for n in nodes:
            for p in n["parent_ids"]:
                parents_with_children.setdefault(p, set()).add(n["kind"])
        pending = {
            "extract_facts": sum(1 for n in nodes if n["kind"] == "source" and n["status"] != "rejected" and "fact" not in parents_with_children.get(n["id"], ())),
            "draft_questions": sum(1 for n in nodes if n["kind"] == "fact" and n["status"] != "rejected" and "question" not in parents_with_children.get(n["id"], ())),
            "make_variants": sum(1 for n in nodes if n["kind"] == "question" and n["status"] == "approved" and "variant" not in parents_with_children.get(n["id"], ())),
        }
        supported = self.config["game_input_types"]
        exportable = sum(
            1 for n in nodes
            if n["kind"] in QUESTION_KINDS and n["status"] == "approved"
            and n["payload"].get("input_type") in supported and not n["provenance"].get("mock")
        )
        targets = tier_targets(topic_cfg.get("target", 24))
        spread = {name: {**c, "target": targets[name]} for name, c in spread_counts(tid).items()}
        approved_cards = sum(1 for n in nodes if n["kind"] in QUESTION_KINDS and n["status"] == "approved")
        out = {**topic_cfg, "stage": topic_cfg.get("stage", "active"), "counts": counts, "pending": pending, "exportable": exportable, "spread": spread,
               "approved": bool(topic_cfg.get("approved")), "approved_cards": approved_cards, "can_approve": approved_cards >= 1,
               "check": check_rollup(nodes)}
        facets = topic_cfg.get("facets") or []
        card_nodes = [n for n in nodes if n["kind"] in QUESTION_KINDS and n["status"] != "rejected"]
        out["facets"] = facets
        out["explanations"] = {"written": sum(1 for n in card_nodes if n["payload"].get("explanation")),
                               "verified": sum(1 for n in card_nodes if n["payload"].get("explanation") and n["payload"].get("explanation_verified")),
                               "missing": sum(1 for n in card_nodes if not n["payload"].get("explanation"))}
        out["facet_counts"] = {f: sum(1 for n in card_nodes if n["payload"].get("facet") == f) for f in facets}
        out["facet_unassigned"] = sum(1 for n in card_nodes if not n["payload"].get("facet")) if facets else 0
        out["facet_orphans"] = sum(1 for n in card_nodes if n["payload"].get("facet") and n["payload"]["facet"] not in facets) if facets else 0
        return out


STORE: Store


# --------------------------------------------------------------------------
# accuracy checks: what a check covers and when it goes stale
# --------------------------------------------------------------------------

def fingerprint(node: dict) -> str:
    """A short hash of the content a check would care about (not status, difficulty or labels)."""
    p = node["payload"]
    data = {k: (sorted(clean_str(c) for c in (p.get(k) or [])) if k == "choices" else clean_str(p.get(k))) for k in CHECK_FIELDS[node["kind"]]}
    return hashlib.sha1(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


def fact_ancestors(node: dict) -> list[dict]:
    return [a for a in STORE.ancestors_of(node["id"]) if a["kind"] == "fact"]


def check_basis(node: dict) -> str:
    """The card plus the facts it rests on: if either changes, a past check no longer applies."""
    return "|".join([fingerprint(node)] + [fingerprint(f) for f in fact_ancestors(node)])


def check_state(node: dict) -> str:
    """unchecked | stale | ok | issues, for a question or variant."""
    c = node.get("check")
    if not c:
        return "unchecked"
    if c.get("basis") != check_basis(node):
        return "stale"
    return c["verdict"] if c.get("verdict") in ("ok", "issues") else "unchecked"


def needs_check(node: dict) -> bool:
    return (node["kind"] in QUESTION_KINDS and node["status"] != "rejected" and bool(fact_ancestors(node))
            and check_state(node) in ("unchecked", "stale"))


def check_rollup(nodes: list[dict]) -> dict:
    out = {"unchecked": 0, "stale": 0, "ok": 0, "issues": 0, "unsourced": 0}
    for n in nodes:
        if n["kind"] not in QUESTION_KINDS or n["status"] == "rejected":
            continue
        if not fact_ancestors(n):
            out["unsourced"] += 1
        else:
            out[check_state(n)] += 1
    return out


def fill_missing(node: dict) -> list[str]:
    """Which parts of a card are still blank (what 'fill in automatically' may write; it never overwrites)."""
    p, facets = node["payload"], topic_facets(node["topic"])
    missing = []
    if facets and not clean_str(p.get("facet")):
        missing.append("facet")
    elif not facets and not clean_str(p.get("title")):
        missing.append("title")
    if not clean_str(p.get("question")):
        missing.append("question")
    if not clean_str(p.get("answer")):
        missing.append("answer")
    if p.get("input_type") != "true_false":
        have = [c for c in (p.get("choices") or []) if clean_str(c)]
        if len(have) < 4 or (clean_str(p.get("answer")) and clean_str(p.get("answer")).lower() not in [clean_str(c).lower() for c in have]):
            missing.append("choices")
    if not clean_str(p.get("explanation")):
        missing.append("explanation")
    return missing


# --------------------------------------------------------------------------
# prompts
# --------------------------------------------------------------------------

JSON_TAIL = "\n\nRespond with ONLY the JSON object described above: no markdown fences, no commentary."

STAGE_LABELS = {
    "find_sources": "Find sources",
    "extract_facts": "Extract facts",
    "draft_questions": "Draft questions",
    "make_variants": "Make variants",
    "suggest_facets": "Suggest facets",
    "assign_facets": "Assign facets to cards",
    "add_explanations": "Add explanations",
    "seed_topic": "Find a source and facts",
    "check_accuracy": "Check accuracy",
    "fill_card": "Fill in blanks",
}
STAGE_TOOLS = {
    "find_sources": ["WebSearch", "WebFetch"],
    "extract_facts": ["WebFetch"],
    "draft_questions": [],
    "make_variants": [],
    "suggest_facets": [],
    "assign_facets": [],
    "add_explanations": ["WebSearch", "WebFetch"],
    "seed_topic": ["WebSearch", "WebFetch"],
    "check_accuracy": [],
    "fill_card": [],
}


def instruction_block(topic_cfg: dict, stage: str) -> str:
    ins = STORE.config["instructions"]
    parts = []
    if ins.get("global", "").strip():
        parts.append("## House rules\n" + ins["global"].strip())
    dept = ins.get("departments", {}).get(topic_cfg.get("department", ""), "").strip()
    if dept:
        parts.append(f"## Department notes ({topic_cfg['department']})\n" + dept)
    topic = ins.get("topics", {}).get(topic_cfg["id"], "").strip()
    if topic:
        parts.append(f"## Topic notes ({topic_cfg['name']})\n" + topic)
    stage_note = ins.get("stages", {}).get(stage, "").strip()
    if stage_note:
        parts.append(f"## Notes for this stage\n" + stage_note)
    return "\n\n".join(parts)


def card_fields_spec(topic_cfg: dict | None = None) -> str:
    facets = (topic_cfg or {}).get("facets") or []
    if facets:
        label = '"facet" (exactly one of: ' + " | ".join(facets) + "; copy it exactly - do NOT write a title)"
    else:
        label = '"title" (2-4 words, a plain label that does not hint at the answer)'
    return (label + ', "question", "answer", "choices" (exactly 4 distinct strings that include the answer), '
            '"difficulty" (' + " | ".join(TIER_NAMES) + '), ' + EXPLANATION_SPEC)


def card_key(topic_cfg: dict) -> str:
    return '"facet": ""' if topic_cfg.get("facets") else '"title": ""'


def card_view(n: dict) -> dict:
    """The card fields an agent sees for an existing question."""
    keys = ("facet", "title", "question", "answer", "choices", "difficulty", "explanation")
    return {k: n["payload"].get(k) for k in keys if n["payload"].get(k) not in (None, "")}


def difficulty_guidance() -> str:
    lines = "\n".join(f"- {name}: {anchor}" for name, _, anchor in TIERS)
    return ("## Difficulty scale (fixed by the studio)\n"
            "Rate every fact and question by how many casual fans of this topic would know the answer:\n" + lines + "\n"
            "Rate by how widely known the answer is, not by how obscure the wording feels. Hard-to-look-up figures (box office, page counts, "
            "runtimes, exact dates of minor events) make poor questions at ANY tier unless they are famous - skip them rather than labelling them advanced or expert. "
            "Always include the well-known core of the topic: what a casual fan would know comes before the deep cuts.")


def wanted_tier(params: dict) -> str:
    """The tier the author asked for on a draft run, or "" for automatic."""
    tier = clean_str(params.get("difficulty")).lower()
    return tier if tier in DIFFICULTY_SCORES else ""


def tier_targets(total: int) -> dict[str, int]:
    targets = {name: int(round(SPREAD_SHARES[name] * total)) for name in TIER_NAMES}
    targets["intermediate"] += total - sum(targets.values())  # absorb rounding drift
    return targets


def spread_counts(topic_id: str) -> dict[str, dict[str, int]]:
    out = {name: {"cards": 0, "facts": 0} for name in TIER_NAMES}
    for n in STORE.nodes.values():
        if n["topic"] != topic_id or n["status"] == "rejected":
            continue
        if n["kind"] in QUESTION_KINDS and n["payload"].get("difficulty") in out:
            out[n["payload"]["difficulty"]]["cards"] += 1
        elif n["kind"] == "fact" and n["payload"].get("difficulty_hint") in out:
            out[n["payload"]["difficulty_hint"]]["facts"] += 1
    return out


def spread_note(topic_cfg: dict, what: str) -> str:
    """Tell the agent where this topic's difficulty mix stands so repeated runs converge on the target spread."""
    key = "facts" if what == "facts" else "cards"
    total = topic_cfg.get("target", 24) if key == "cards" else int(round(topic_cfg.get("target", 24) * 1.5))
    targets, have = tier_targets(total), spread_counts(topic_cfg["id"])
    status = ", ".join(f"{n} {have[n][key]}/{targets[n]}" for n in TIER_NAMES)
    return (f"## Difficulty spread for this topic\nTarget mix for about {total} {what} (current/target): {status}.\n"
            "Prioritise the tiers furthest below target, and don't pad a tier with obscure filler just to hit a number.")


def facts_waiting(topic_id: str, under: str = "") -> list[dict]:
    """Facts with no question yet (rejected ones excluded): in the whole topic, or beneath one node at any depth."""
    S = STORE
    pool = S.descendants_of(under) if under and under in S.nodes else sorted((n for n in S.nodes.values() if n["topic"] == topic_id), key=lambda n: n["created_at"])
    return [n for n in pool if n["topic"] == topic_id and n["kind"] == "fact" and n["status"] != "rejected"
            and not any(c["kind"] == "question" for c in S.children_of(n["id"]))]


def select_inputs(stage: str, topic_id: str, params: dict) -> list[dict]:
    """Pick the input nodes for a stage: explicit ids, else sensible defaults."""
    S = STORE
    limit = int(params.get("limit") or 5)
    explicit = [S.nodes[i] for i in params.get("input_ids", []) if i in S.nodes]
    if explicit:
        return explicit
    if stage == "draft_questions" and params.get("under"):
        return facts_waiting(topic_id, clean_str(params.get("under")))[:limit]
    nodes = [n for n in S.nodes.values() if n["topic"] == topic_id]
    nodes.sort(key=lambda n: n["created_at"])
    has_child: dict[str, set] = {}
    for n in nodes:
        for p in n["parent_ids"]:
            has_child.setdefault(p, set()).add(n["kind"] + (":" + str(n["variant_type"]) if n["kind"] == "variant" else ""))
    if stage == "extract_facts":
        picked = [n for n in nodes if n["kind"] == "source" and n["status"] != "rejected" and "fact" not in has_child.get(n["id"], ())]
    elif stage == "draft_questions":
        picked = facts_waiting(topic_id)
    elif stage == "make_variants":
        vt = params.get("variant_type", "paraphrase")
        picked = [n for n in nodes if n["kind"] == "question" and n["status"] == "approved" and f"variant:{vt}" not in has_child.get(n["id"], ())]
    elif stage == "add_explanations":
        picked = [n for n in nodes if n["kind"] in QUESTION_KINDS and n["status"] != "rejected" and not n["payload"].get("explanation")]
        limit = int(params.get("limit") or 10)
    elif stage == "assign_facets":
        picked = [n for n in nodes if n["kind"] in QUESTION_KINDS and n["status"] != "rejected" and not n["payload"].get("facet")]
        limit = int(params.get("limit") or 40)
    elif stage == "check_accuracy":
        picked = [n for n in nodes if needs_check(n)]
        limit = int(params.get("limit") or 25)
    elif stage == "fill_card":
        picked = [n for n in nodes if n["kind"] in QUESTION_KINDS and n["status"] != "rejected" and fill_missing(n)]
        limit = int(params.get("limit") or 10)
    else:
        picked = []
    return picked[:limit]


def build_prompt(stage: str, topic_cfg: dict, params: dict, inputs: list[dict]) -> str:
    S = STORE
    name, dept = topic_cfg["name"], topic_cfg["department"]
    block = instruction_block(topic_cfg, stage)
    note = clean_str(params.get("note"))
    scope = clean_str(topic_cfg.get("note"))
    topic_line = f'Topic: "{name}" (department: {dept}).' + (f"\nScope notes from the author: {scope}" if scope else "")
    head = f"{topic_line}\n\n{block}\n\n" if block else f"{topic_line}\n\n"
    one_off = f"## Extra instruction for this run\n{note}\n\n" if note else ""

    if stage == "find_sources":
        count = int(params.get("count") or 5)
        known = [n["payload"].get("url") for n in S.nodes.values() if n["topic"] == topic_cfg["id"] and n["kind"] == "source" and n["payload"].get("url")]
        return (head + one_off +
                f"Task: use web search to find {count} reliable web sources with deep factual material for writing trivia about this topic. "
                "Open each page to confirm it exists and holds real facts. For each source give: title, url, summary (1-2 sentences on what factual material it holds), "
                "license (e.g. \"CC BY-SA\", \"public domain\", \"unknown\"), and excerpt (at most 40 words, paraphrased, showing the kind of facts available).\n"
                f"Already collected, do not repeat: {json.dumps(known)}\n\n"
                'Output: {"sources": [{"title": "", "url": "", "summary": "", "license": "", "excerpt": ""}]}' + JSON_TAIL)

    if stage == "extract_facts":
        per = max(1, min(int(params.get("per_item") or 6), MAX_FACTS_PER_SOURCE))
        listing = []
        for n in inputs:
            entry = {"id": n["id"], **{k: n["payload"].get(k, "") for k in ("title", "url", "summary", "excerpt", "source_type")}}
            have = [c["payload"].get("text", "") for c in S.children_of(n["id"]) if c["kind"] == "fact" and c["status"] != "rejected"]
            if have:
                entry["already_extracted"] = have
            listing.append(entry)
        again = any("already_extracted" in e for e in listing)
        return (head + one_off +
                f"Task: for each source below, extract up to {per} distinct, verifiable, quiz-worthy facts about this topic. "
                + ("A source with \"already_extracted\" facts has been mined before: return only NEW facts that are not the same claim as any listed one (a different item, not a rewording), "
                   "and return fewer than the limit, or none, if the source has nothing new left. Open the url and work through the whole page - a list page usually holds far more than the first screen. " if again else "") +
                "Each fact is one self-contained sentence stating the claim precisely (names, numbers, dates), restated in your own words rather than copied. "
                "Give each a difficulty_hint from the scale below, and an optional context: one sentence (at most 30 words) of surrounding detail that the source itself states and that makes the fact more interesting - a comparison, a ranking, a number, or the reason behind it. Leave context empty if the source gives nothing extra. Where a source has a url you may open it to read more. Skip anything that changes over time unless it is date-qualified.\n\n"
                f"{difficulty_guidance()}\n\n{spread_note(topic_cfg, 'facts')}\n\n"
                f"SOURCES:\n{json.dumps(listing, indent=2, ensure_ascii=False)}\n\n"
                'Output: {"facts": [{"source_id": "<id from above>", "text": "", "context": "", "difficulty_hint": "' + "|".join(TIER_NAMES) + '"}]}' + JSON_TAIL)

    if stage == "draft_questions":
        existing = [n["payload"].get("question") for n in S.nodes.values() if n["topic"] == topic_cfg["id"] and n["kind"] in QUESTION_KINDS and n["status"] != "rejected"]
        listing = [{"id": n["id"], "fact": n["payload"].get("text", ""), "context": n["payload"].get("context", ""), "difficulty_hint": n["payload"].get("difficulty_hint", "")} for n in inputs]
        target = wanted_tier(params)
        if target:
            anchor = dict((t[0], t[2]) for t in TIERS)[target]
            level = (f"## Requested difficulty: {target}\nThe author wants every question below to be a \"{target}\" question ({anchor}). "
                     "Choose the angle, wording and answer choices so the finished question genuinely fits that tier - for an easier tier ask about the most famous aspect of the fact "
                     "and keep the wrong answers clearly different; for a harder tier ask about the specific detail and make the wrong answers close and plausible. "
                     f"Set \"difficulty\" to your honest rating of the finished question; if a fact simply cannot be asked at {target}, still write the best question you can and rate it honestly.")
        else:
            level = "Use the fact's difficulty_hint as a starting point, but re-rate it against the scale if it looks wrong (plausible distractors can make a question harder, obvious ones easier)."
        return (head + one_off +
                "Task: turn each fact below into one multiple-choice question. Fields per question: " + card_fields_spec(topic_cfg) + ". "
                + ("\n\n" if target else "") + level + "\n\n"
                f"{difficulty_guidance()}\n\n" + ("" if target else f"{spread_note(topic_cfg, 'cards')}\n\n") +
                f"Questions that already exist in this topic (do not duplicate their facts): {json.dumps(existing, ensure_ascii=False)}\n\n"
                f"FACTS:\n{json.dumps(listing, indent=2, ensure_ascii=False)}\n\n"
                'Output: {"questions": [{"fact_id": "<id from above>", ' + card_key(topic_cfg) + ', "question": "", "answer": "", "choices": ["", "", "", ""], "difficulty": "", "explanation": ""}]}' + JSON_TAIL)

    if stage == "make_variants":
        vt = params.get("variant_type", "paraphrase")
        per = int(params.get("per_item") or 1)
        listing = [{"id": n["id"], **card_view(n)} for n in inputs]
        return (head + one_off +
                f"Task: for each question below write {per} variant(s) of type \"{vt}\". {VARIANT_TYPES.get(vt, '')} "
                "The variant must test the same underlying fact. Fields per variant: " + card_fields_spec(topic_cfg) + ".\n\n"
                f"{difficulty_guidance()}\n\n"
                f"QUESTIONS:\n{json.dumps(listing, indent=2, ensure_ascii=False)}\n\n"
                'Output: {"variants": [{"question_id": "<id from above>", ' + card_key(topic_cfg) + ', "question": "", "answer": "", "choices": ["", "", "", ""], "difficulty": "", "explanation": ""}]}' + JSON_TAIL)

    if stage == "add_explanations":
        listing = [{"id": n["id"], "question": n["payload"].get("question"), "answer": n["payload"].get("answer")} for n in inputs]
        return (head + one_off +
                "Task: for each card below, write a short \"did you know\" explanation of its answer. Use web search and open pages to find a reliable source that confirms the answer, "
                "then write one or two sentences only (at most 30 words; every sentence must add something new, with no filler) that add real context: a comparison, a ranking, a number, some history, or the reason behind it. "
                "Rules: every claim must be stated on the page you cite; if sources disagree on a number, use the one you cite and name it in the sentence; date-qualify anything that can change (for example \"as of 2026\"); do not just restate the question or the answer; plain, human phrasing. "
                "If you cannot confirm the answer on a reliable page, return an empty explanation and say why in note.\n\n"
                f"CARDS:\n{json.dumps(listing, indent=2, ensure_ascii=False)}\n\n"
                'Output: {"explanations": [{"id": "<id from above>", "explanation": "", "source_url": "", "note": ""}]}' + JSON_TAIL)

    if stage == "suggest_facets":
        sample = [n["payload"].get("question") or n["payload"].get("text") for n in S.nodes.values()
                  if n["topic"] == topic_cfg["id"] and n["kind"] in ("fact", "question", "variant") and n["status"] != "rejected"][:25]
        return (head + one_off +
                "Task: propose 5 to 8 facets for this topic. A facet is a short, plain, consistent category label (1-3 words, Title Case) that every card in the topic can be filed under, "
                "like the section headings of a textbook chapter (a region, an era, a kind of thing). Rules: plain nouns, never clever, punny or riddle-like; "
                "a label must not hint at any specific answer; labels should be mutually exclusive and together cover the topic; no 'Other' or 'Misc'.\n"
                + (f"A sample of the topic's material, to see what needs filing: {json.dumps(sample, ensure_ascii=False)}\n" if sample else "")
                + '\nOutput: {"facets": ["", ""]}' + JSON_TAIL)

    if stage == "assign_facets":
        facets = topic_cfg.get("facets") or []
        listing = [{"id": n["id"], "question": n["payload"].get("question"), "answer": n["payload"].get("answer")} for n in inputs]
        return (head + one_off +
                f"Task: file each card below under exactly one of this topic's facets: {json.dumps(facets, ensure_ascii=False)}. "
                "Choose the facet that best describes what the question is about, and copy the facet exactly as written.\n\n"
                f"CARDS:\n{json.dumps(listing, indent=2, ensure_ascii=False)}\n\n"
                'Output: {"assignments": [{"id": "<id from above>", "facet": ""}]}' + JSON_TAIL)

    if stage == "seed_topic":
        count = max(1, min(int(params.get("count") or 10), MAX_FACTS_PER_SOURCE))
        known = [n["payload"].get("url") for n in S.nodes.values() if n["topic"] == topic_cfg["id"] and n["kind"] == "source" and n["payload"].get("url")]
        return (head + one_off +
                f"Task: use web search to find ONE reliable web source with deep factual material about this topic. Open it and read it properly, then extract {count} distinct, verifiable, quiz-worthy facts from that one page. "
                "For the source give: title, url, summary (1-2 sentences on what it holds), license (e.g. \"CC BY-SA\", \"public domain\", \"unknown\"), and excerpt (at most 40 words, paraphrased). "
                "Each fact is one self-contained sentence stating the claim precisely (names, numbers, dates), restated in your own words rather than copied. "
                "Give each a difficulty_hint from the scale below, and an optional context: one sentence (at most 30 words) of surrounding detail that the source itself states. "
                "Skip anything that changes over time unless it is date-qualified.\n\n"
                f"{difficulty_guidance()}\n\n{spread_note(topic_cfg, 'facts')}\n\n"
                f"Sources already collected, do not use these: {json.dumps(known)}\n\n"
                'Output: {"source": {"title": "", "url": "", "summary": "", "license": "", "excerpt": ""}, "facts": [{"text": "", "context": "", "difficulty_hint": "' + "|".join(TIER_NAMES) + '"}]}' + JSON_TAIL)

    if stage == "check_accuracy":
        listing = [{"id": n["id"], "card": card_view(n),
                    "facts": [{"text": f["payload"].get("text", ""), "context": f["payload"].get("context", "")} for f in fact_ancestors(n)]} for n in inputs]
        return (head + one_off +
                "Task: check each card below ONLY against the fact(s) it was written from. Do not search the web, and do not use outside knowledge to override the fact; "
                "if the fact itself looks wrong, say so in notes but still judge the card against it. For each card decide:\n"
                '- "ok": the question is clear, the answer is supported by the fact (or its context), the explanation (if any) says nothing the fact does not support, '
                "exactly one choice is defensible, and no wrong choice is also correct.\n"
                '- "issues": anything else. In notes (one or two plain sentences) say exactly what is wrong.\n'
                'If a fix is possible using only the fact, give "suggested" containing ONLY the fields that should change (question, answer, choices as the full list of 4 including the answer, explanation); otherwise null. '
                "Do not suggest changes that are only matters of style or taste.\n\n"
                f"CARDS:\n{json.dumps(listing, indent=2, ensure_ascii=False)}\n\n"
                'Output: {"checks": [{"id": "<id from above>", "verdict": "ok|issues", "notes": "", "suggested": null}]}' + JSON_TAIL)

    if stage == "fill_card":
        listing = []
        for n in inputs:
            entry = {"id": n["id"], "card": card_view(n), "missing": fill_missing(n),
                     "facts": [{"text": f["payload"].get("text", ""), "context": f["payload"].get("context", "")} for f in fact_ancestors(n)]}
            if n["kind"] == "variant" and n.get("variant_type"):
                entry["variant_type"], entry["variant_instruction"] = n["variant_type"], VARIANT_TYPES.get(n["variant_type"], "")
            listing.append(entry)
        return (head + one_off +
                'Task: complete the blank parts of each card below, working only from the fact(s) given. Fill ONLY the fields named in "missing" and never change a field that is already present. '
                'For "choices" return the complete list of 4 distinct strings: every choice already on the card, the correct answer, and plausible wrong answers from the same category. '
                "Card fields: " + card_fields_spec(topic_cfg) + ".\n\n"
                f"{difficulty_guidance()}\n\n"
                f"CARDS:\n{json.dumps(listing, indent=2, ensure_ascii=False)}\n\n"
                'Output: {"cards": [{"id": "<id from above>", ' + card_key(topic_cfg) + ', "question": "", "answer": "", "choices": ["", "", "", ""], "explanation": ""}]}' + JSON_TAIL)

    raise ValueError(f"unknown stage {stage}")


# --------------------------------------------------------------------------
# result handling
# --------------------------------------------------------------------------

def apply_result(run: dict, result: dict, inputs: list[dict]) -> None:
    S = STORE
    stage, topic_id = run["stage"], run["topic"]
    input_ids = {n["id"] for n in inputs}
    prov = {
        "origin": "agent", "run_id": run["id"], "stage": stage, "backend": run["backend"],
        "model": run.get("model") or run["backend"], "mock": run["backend"] == "mock",
    }
    created: list[str] = []
    warnings: list[str] = run.setdefault("warnings", [])

    def add(kind, payload, parents, variant_type=None):
        node = S.create_node(topic_id, kind, payload, parents, variant_type, "draft", dict(prov))
        created.append(node["id"])
        return node

    if stage == "find_sources":
        known = {n["payload"].get("url") for n in S.nodes.values() if n["topic"] == topic_id and n["kind"] == "source"}
        for item in result.get("sources", []):
            url = clean_str(item.get("url"))
            if not url or url in known:
                warnings.append(f"skipped source without a new url: {clean_str(item.get('title'))[:60]}")
                continue
            known.add(url)
            add("source", {"source_type": "web", "title": clean_str(item.get("title")), "url": url, "summary": clean_str(item.get("summary")),
                           "license": clean_str(item.get("license")), "excerpt": clean_str(item.get("excerpt"))}, [])

    elif stage == "extract_facts":
        for item in result.get("facts", []):
            sid = clean_str(item.get("source_id"))
            if sid not in input_ids:
                warnings.append(f"fact referenced unknown source id {sid!r}; skipped")
                continue
            text = clean_str(item.get("text"))
            if not text:
                continue
            hint = clean_str(item.get("difficulty_hint")).lower()
            add("fact", {"text": text, "context": clean_str(item.get("context"))[:EXPLANATION_MAX], "difficulty_hint": hint if hint in DIFFICULTY_SCORES else "intermediate"}, [sid])

    elif stage in ("draft_questions", "make_variants"):
        if stage == "draft_questions":
            items, ref_key, kind = result.get("questions", []), "fact_id", "question"
        else:
            items, ref_key, kind = result.get("variants", []), "question_id", "variant"
        for item in items:
            ref = clean_str(item.get(ref_key))
            if ref not in input_ids:
                warnings.append(f"item referenced unknown id {ref!r}; skipped")
                continue
            vtype = run["params"].get("variant_type", "paraphrase") if stage == "make_variants" else None
            itype = "true_false" if vtype == "true_false" else "multiple_choice"
            clean, problems = normalize_question(item, itype, topic_facets(topic_id))
            clean["snark"], clean["story"] = "", ""   # human-only fields: ignore them even if the model returned some
            asked = wanted_tier(run["params"]) if stage == "draft_questions" else ""
            if asked:  # the author's choice wins; say so when the AI's own rating disagrees
                if clean["difficulty"] != asked:
                    warnings.append(f"{clean.get('facet') or clean.get('title') or ref}: the AI rated this '{clean['difficulty']}' but you asked for '{asked}' - set to '{asked}'; check it fits")
                clean["difficulty"], clean["base_score"] = asked, DIFFICULTY_SCORES[asked]
            parent_payload = S.nodes[ref]["payload"]
            if clean["explanation"]:
                if stage == "draft_questions":  # written from the fact: cite the source the fact came from
                    clean["explanation_source"], clean["explanation_verified"] = source_ref(S.nodes[ref]), False
                else:  # a variant of an existing card keeps its parent's citation, and its verified flag only if the text is unchanged
                    clean["explanation_source"] = parent_payload.get("explanation_source", "")
                    clean["explanation_verified"] = bool(parent_payload.get("explanation_verified")) and clean["explanation"] == parent_payload.get("explanation")
            if problems:
                warnings.append(f"{clean.get('title') or clean.get('question')[:40] or ref}: " + "; ".join(problems) + " (kept as draft - fix before approving)")
            add(kind, clean, [ref], vtype)

    elif stage == "add_explanations":
        updated_ids: list[str] = []
        with S.lock:
            for item in result.get("explanations", []):
                nid, text = clean_str(item.get("id")), clean_str(item.get("explanation"))
                if nid not in input_ids:
                    warnings.append(f"explanation referenced unknown id {nid!r}; skipped")
                    continue
                if not text:
                    warnings.append(f"{nid}: no explanation written ({clean_str(item.get('note')) or 'no reason given'})")
                    continue
                if len(text) > EXPLANATION_MAX:
                    warnings.append(f"{nid}: explanation was {len(text)} characters (max {EXPLANATION_MAX}); skipped")
                    continue
                node = S.nodes[nid]
                before = {k: node["payload"].get(k) for k in ("explanation", "explanation_source", "explanation_verified")}
                node["payload"].update(explanation=text, explanation_source=clean_str(item.get("source_url")), explanation_verified=False,
                                       explanation_mock=run["backend"] == "mock")
                node["history"].append({"at": now_iso(), "by": "agent", "action": "explanation", "run_id": run["id"], "before": before})
                node["updated_at"] = now_iso()
                updated_ids.append(nid)
            S.save_topic(topic_id)
        run["updated_node_ids"] = updated_ids

    elif stage == "suggest_facets":
        cfg = S.topic_cfg(topic_id)
        try:
            facets = clean_facets(result.get("facets", []))
        except ApiError as exc:
            facets = []
            warnings.append(exc.message)
        if len(facets) < 3:
            warnings.append("fewer than 3 usable facets came back; nothing was applied")
        elif cfg.get("facets"):
            warnings.append("this topic already has facets, so the suggestion was not applied (see the raw output)")
        else:
            cfg["facets"], cfg["facets_run"] = facets, run["id"]
            S.save_config()
            run["applied_facets"] = facets

    elif stage == "assign_facets":
        by_lower = {f.lower(): f for f in topic_facets(topic_id)}
        updated: list[str] = []
        with S.lock:
            for item in result.get("assignments", []):
                nid, facet = clean_str(item.get("id")), by_lower.get(clean_str(item.get("facet")).lower())
                if nid not in input_ids:
                    warnings.append(f"assignment referenced unknown id {nid!r}; skipped")
                    continue
                if not facet:
                    warnings.append(f"{nid}: facet {clean_str(item.get('facet'))!r} isn't in the topic's list; skipped")
                    continue
                node = S.nodes[nid]
                before = {k: node["payload"].get(k) for k in ("title", "facet")}
                node["payload"].setdefault("legacy_title", node["payload"].get("title", ""))
                node["payload"]["facet"] = node["payload"]["title"] = facet
                node["history"].append({"at": now_iso(), "by": "agent", "action": "facet", "run_id": run["id"], "before": before})
                node["updated_at"] = now_iso()
                updated.append(nid)
            S.save_topic(topic_id)
        run["updated_node_ids"] = updated

    elif stage == "seed_topic":
        src = result.get("source") or {}
        url = clean_str(src.get("url"))
        if not url:
            raise RuntimeError("the AI did not return a source url")
        existing = next((n for n in S.nodes.values() if n["topic"] == topic_id and n["kind"] == "source" and n["payload"].get("url") == url), None)
        if existing:
            warnings.append("that source was already in this topic; the new facts were attached to it")
            sid = existing["id"]
        else:
            sid = add("source", {"source_type": "web", "title": clean_str(src.get("title")), "url": url, "summary": clean_str(src.get("summary")),
                                 "license": clean_str(src.get("license")), "excerpt": clean_str(src.get("excerpt"))}, [])["id"]
        wanted = max(1, min(int(run["params"].get("count") or 10), MAX_FACTS_PER_SOURCE))
        for item in result.get("facts", [])[:wanted]:
            text = clean_str(item.get("text"))
            if text:
                hint = clean_str(item.get("difficulty_hint")).lower()
                add("fact", {"text": text, "context": clean_str(item.get("context"))[:EXPLANATION_MAX], "difficulty_hint": hint if hint in DIFFICULTY_SCORES else "intermediate"}, [sid])

    elif stage == "check_accuracy":
        checked: list[str] = []
        with S.lock:
            for item in result.get("checks", []):
                nid = clean_str(item.get("id"))
                if nid not in input_ids or nid not in S.nodes:
                    warnings.append(f"check referenced unknown id {nid!r}; skipped")
                    continue
                verdict = clean_str(item.get("verdict")).lower()
                if verdict not in ("ok", "issues"):
                    warnings.append(f"{nid}: unclear verdict {verdict!r}; skipped")
                    continue
                node = S.nodes[nid]
                basis = run.get("input_basis", {}).get(nid) or check_basis(node)   # the content that was actually checked
                notes = clean_str(item.get("notes"))[:600]
                node["check"] = {"basis": basis, "verdict": verdict, "notes": notes, "at": now_iso(), "run_id": run["id"]}
                node.pop("suggestion", None)
                sug = item.get("suggested")
                if verdict == "issues" and isinstance(sug, dict):
                    candidate = {**node["payload"], **{k: sug[k] for k in SUGGEST_FIELDS if k in sug}}
                    clean, problems = normalize_question(candidate, node["payload"].get("input_type"), topic_facets(topic_id))
                    fields = {k: clean[k] for k in SUGGEST_FIELDS if clean[k] != node["payload"].get(k)}
                    if problems:
                        warnings.append(f"{nid}: the suggested fix was unusable ({'; '.join(problems)}); showing the notes only")
                    elif fields:
                        node["suggestion"] = {"fields": fields, "notes": notes, "at": now_iso(), "run_id": run["id"], "basis": basis}
                node["history"].append({"at": now_iso(), "by": "agent", "action": "check", "verdict": verdict, "run_id": run["id"]})
                node["history"] = node["history"][-60:]
                checked.append(nid)
            S.save_topic(topic_id)
        run["updated_node_ids"] = checked

    elif stage == "fill_card":
        filled: list[str] = []
        with S.lock:
            for item in result.get("cards", []):
                nid = clean_str(item.get("id"))
                if nid not in input_ids or nid not in S.nodes:
                    warnings.append(f"card referenced unknown id {nid!r}; skipped")
                    continue
                node = S.nodes[nid]
                missing = fill_missing(node)
                if not missing:
                    continue
                before = json.loads(json.dumps(node["payload"]))
                new = dict(node["payload"])
                for key in missing:
                    if key == "choices":
                        have = [clean_str(c) for c in (new.get("choices") or []) if clean_str(c)]
                        for c in [clean_str(c) for c in (item.get("choices") or [])] + [clean_str(new.get("answer") or item.get("answer"))]:
                            if c and c.lower() not in [h.lower() for h in have] and len(have) < 4:
                                have.append(c)
                        new["choices"] = have
                    elif key in ("facet", "title"):
                        new[key] = clean_str(item.get(key))
                    else:
                        new[key] = clean_str(item.get(key))
                        if key == "explanation" and new[key]:
                            facts = fact_ancestors(node)
                            new["explanation_source"], new["explanation_verified"] = (source_ref(facts[0]) if facts else ""), False
                clean, problems = normalize_question(new, node["payload"].get("input_type"), topic_facets(topic_id))
                if problems:
                    warnings.append(f"{nid}: " + "; ".join(problems) + " (filled what could be filled - finish it by hand)")
                node["payload"] = clean
                node["history"].append({"at": now_iso(), "by": "agent", "action": "fill", "run_id": run["id"], "before": before})
                node["history"] = node["history"][-60:]
                node["updated_at"] = now_iso()
                node.pop("suggestion", None)
                filled.append(nid)
            S.save_topic(topic_id)
        run["updated_node_ids"] = filled

    run["created_node_ids"] = created


def mock_response(run: dict, inputs: list[dict], topic_cfg: dict) -> dict:
    """Deterministic fake model output, shaped exactly like the real thing."""
    stage, name = run["stage"], topic_cfg["name"]
    facets = topic_cfg.get("facets") or []

    def face(i: int, p: dict | None = None, suffix: str = "") -> dict:
        if facets:
            return {"facet": p["facet"] if p and p.get("facet") in facets else facets[i % len(facets)]}
        return {"title": ((p or {}).get("title") or "Mock Card") + suffix}

    n = len([x for x in STORE.nodes.values() if x["topic"] == topic_cfg["id"]])
    if stage == "find_sources":
        return {"sources": [{"title": f"MOCK source {n + i + 1} about {name}", "url": f"https://example.com/mock/{topic_cfg['id']}/{n + i + 1}",
                             "summary": "Placeholder source from the mock backend.", "license": "n/a", "excerpt": "Mock excerpt."} for i in range(int(run["params"].get("count") or 3))]}
    if stage == "extract_facts":
        per = min(int(run["params"].get("per_item") or 3), 3)
        return {"facts": [{"source_id": s["id"], "text": f"MOCK fact {j + 1} drawn from '{s['payload'].get('title', '')}'.", "context": f"MOCK context for fact {j + 1}.", "difficulty_hint": TIER_NAMES[(i * per + j) % len(TIER_NAMES)]}
                          for i, s in enumerate(inputs) for j in range(per)]}
    if stage == "draft_questions":
        return {"questions": [{"fact_id": f["id"], **face(i), "question": f"MOCK: {f['payload'].get('text', '')[:60]} - which is right?", "answer": "Correct answer",
                               "choices": ["Correct answer", "Wrong A", "Wrong B", "Wrong C"], "explanation": f"MOCK explanation: {f['payload'].get('context', '')}", "difficulty": f["payload"].get("difficulty_hint") or "intermediate",
                               "snark": "MOCK SNARK that must be ignored", "story": "MOCK STORY that must be ignored"} for i, f in enumerate(inputs)]}
    if stage == "make_variants":
        vt = run["params"].get("variant_type", "paraphrase")
        out = []
        for q in inputs:
            p = q["payload"]
            if vt == "true_false":
                out.append({"question_id": q["id"], **face(0, p), "question": f"MOCK T/F: {p.get('question')}", "answer": "True", "choices": ["True", "False"], "difficulty": p.get("difficulty"), "explanation": p.get("explanation", "")})
            else:
                out.append({"question_id": q["id"], **face(0, p, " II"), "question": f"MOCK {vt}: {p.get('question')}", "answer": p.get("answer"),
                            "choices": list(p.get("choices") or []), "difficulty": p.get("difficulty"), "explanation": p.get("explanation", "")})
        return {"variants": out}
    if stage == "add_explanations":
        return {"explanations": [{"id": n["id"], "explanation": f"MOCK explanation for: {n['payload'].get('question', '')[:50]}", "source_url": "https://example.com/mock-explanation", "note": ""} for n in inputs]}
    if stage == "seed_topic":
        k = int(run["params"].get("count") or 10)
        return {"source": {"title": f"MOCK source about {name}", "url": f"https://example.com/mock/{topic_cfg['id']}/seed{n}", "summary": "Placeholder.", "license": "n/a", "excerpt": "Mock excerpt."},
                "facts": [{"text": f"MOCK fact {j + 1} about {name}.", "context": f"MOCK context {j + 1}.", "difficulty_hint": TIER_NAMES[j % len(TIER_NAMES)]} for j in range(k)]}
    if stage == "check_accuracy":
        out = []
        for q in inputs:
            p = q["payload"]
            if "MOCKBAD" in (p.get("question") or ""):
                out.append({"id": q["id"], "verdict": "issues", "notes": "MOCK: the question contains a flagged word.", "suggested": {"question": p["question"].replace("MOCKBAD", "MOCKFIXED")}})
            else:
                out.append({"id": q["id"], "verdict": "ok", "notes": "", "suggested": None})
        return {"checks": out}
    if stage == "fill_card":
        return {"cards": [{"id": q["id"], **face(i, q["payload"]), "question": q["payload"].get("question") or "MOCK filled question", "answer": q["payload"].get("answer") or "Mock answer",
                           "choices": [q["payload"].get("answer") or "Mock answer", "Mock wrong A", "Mock wrong B", "Mock wrong C"], "explanation": "MOCK filled explanation",
                           "snark": "MOCK SNARK that must be ignored", "story": "MOCK STORY that must be ignored"} for i, q in enumerate(inputs)]}
    if stage == "suggest_facets":
        return {"facets": ["Mock One", "Mock Two", "Mock Three", "Mock Four"]}
    if stage == "assign_facets":
        return {"assignments": [{"id": n["id"], "facet": facets[i % len(facets)]} for i, n in enumerate(inputs)]}
    raise ValueError(stage)


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------

class Cancelled(Exception):
    pass


class Runner:
    def __init__(self) -> None:
        self.pool = ThreadPoolExecutor(max_workers=8)
        self.sem = threading.BoundedSemaphore(max(1, int(STORE.config.get("concurrency", 3))))
        self.procs: dict[str, subprocess.Popen] = {}

    def make_run(self, stage: str, topic_cfg: dict, params: dict, inputs: list[dict], prompt: str) -> dict:
        cfg = STORE.config
        with STORE.lock:
            run = {
                "id": STORE.new_id("run", STORE.runs), "stage": stage, "topic": topic_cfg["id"], "params": params,
                "status": "queued", "backend": cfg["backend"], "model": cfg.get("model", ""),
                "prompt": prompt, "tools": STAGE_TOOLS.get(stage, []), "instructions": instruction_block(topic_cfg, stage),
                "input_ids": [n["id"] for n in inputs], "created_at": now_iso(), "started_at": None, "ended_at": None,
                "input_basis": {n["id"]: check_basis(n) for n in inputs if n["kind"] in QUESTION_KINDS},
                "cost_usd": None, "raw_output": "", "error": "", "warnings": [], "created_node_ids": [],
            }
            STORE.runs[run["id"]] = run
            STORE.save_run(run)
        return run

    def submit(self, run: dict) -> None:
        self.pool.submit(self._execute, run["id"])

    def active_count(self) -> int:
        return sum(1 for r in STORE.runs.values() if r["status"] in ("queued", "running"))

    def cancel(self, run_id: str) -> None:
        run = STORE.runs[run_id]
        run["cancel_requested"] = True
        proc = self.procs.get(run_id)
        if proc and proc.poll() is None:
            proc.kill()
        if run["status"] == "queued":
            run["status"] = "cancelled"
            run["ended_at"] = now_iso()
            STORE.save_run(run)

    def _execute(self, run_id: str) -> None:
        run = STORE.runs[run_id]
        with self.sem:
            if run.get("cancel_requested"):
                return
            run["status"] = "running"
            run["started_at"] = now_iso()
            t0 = time.time()
            try:
                topic_cfg = STORE.topic_cfg(run["topic"])
                inputs = [STORE.nodes[i] for i in run["input_ids"] if i in STORE.nodes]
                if run["backend"] == "mock":
                    time.sleep(1.2)
                    raw = json.dumps(mock_response(run, inputs, topic_cfg))
                else:
                    raw = self._call_claude(run)
                run["raw_output"] = raw
                apply_result(run, extract_json(raw), inputs)
                run["status"] = "done"
            except Cancelled:
                run["status"] = "cancelled"
            except Exception as exc:  # noqa: BLE001 - surfaced to the UI
                run["status"] = "failed" if not run.get("cancel_requested") else "cancelled"
                run["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                run["ended_at"] = now_iso()
                run["duration_s"] = round(time.time() - t0, 1)
                self.procs.pop(run_id, None)
                STORE.save_run(run)
    def _call_claude(self, run: dict) -> str:
        cfg = STORE.config
        cmd = ["claude", "-p", "--output-format", "json", "--no-session-persistence", "--max-budget-usd", str(cfg["budget_usd"])]
        if cfg.get("model"):
            cmd += ["--model", cfg["model"]]
        tools = run.get("tools") or []
        cmd += ["--tools", ",".join(tools)]
        if tools:
            cmd += ["--allowedTools", ",".join(tools)]
        with tempfile.TemporaryDirectory(prefix="studio_") as cwd:  # empty cwd: the agent can't see or touch the repo
            try:
                proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            except FileNotFoundError as exc:
                raise RuntimeError("`claude` CLI not found on PATH") from exc
            self.procs[run["id"]] = proc
            try:
                out, err = proc.communicate(run["prompt"], timeout=int(cfg["timeout_s"]))
            except subprocess.TimeoutExpired:
                proc.kill()
                raise RuntimeError(f"timed out after {cfg['timeout_s']}s")
        if run.get("cancel_requested"):
            raise Cancelled()
        try:
            envelope = json.loads(out)
        except ValueError:
            raise RuntimeError(f"claude returned non-JSON output: {(out or err)[:400]}")
        run["cost_usd"] = envelope.get("total_cost_usd")
        if envelope.get("is_error"):
            raise RuntimeError(str(envelope.get("result") or err)[:600])
        return str(envelope.get("result", ""))

# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------

def export_topic(topic_id: str, write: bool) -> dict:
    S = STORE
    cfg = S.topic_cfg(topic_id)
    if not cfg or cfg.get("stage", "active") != "active":
        raise ApiError(400, "only active topics can be exported - promote the idea first")
    if not cfg.get("approved"):
        raise ApiError(400, "approve the topic first (it needs at least one approved question)")
    supported = S.config["game_input_types"]
    cards, skipped = [], []
    require_verified = S.config.get("require_verified_explanations", True)
    unverified = mock_expl = 0
    nodes = sorted((n for n in S.nodes.values() if n["topic"] == topic_id and n["kind"] in QUESTION_KINDS), key=lambda n: n["created_at"])
    used_ids: set[str] = set()
    for n in nodes:
        if n["status"] != "approved":
            continue
        p = n["payload"]
        if n["provenance"].get("mock"):
            skipped.append({"id": n["id"], "why": "mock backend output is never exported"})
            continue
        if p.get("input_type") not in supported:
            skipped.append({"id": n["id"], "why": f"input type {p.get('input_type')!r} is not supported by the game yet"})
            continue
        _, problems = normalize_question(p, p.get("input_type"), topic_facets(topic_id))
        if problems:
            skipped.append({"id": n["id"], "why": "; ".join(problems)})
            continue
        cid = p.get("card_id") or n["id"]
        if cid in used_ids:
            skipped.append({"id": n["id"], "why": f"duplicate card id {cid}"})
            continue
        used_ids.add(cid)
        choices = list(p["choices"])
        random.Random(cid).shuffle(choices)  # stable shuffle: the answer is not always first
        card = {"id": cid, "title": p["title"], "question": p["question"], "answer": p["answer"], "choices": choices,
                "difficulty": p["difficulty"], "base_score": DIFFICULTY_SCORES[p["difficulty"]], "input_type": p["input_type"]}
        if p.get("facet"):
            card["facet"] = p["facet"]
        if S.config.get("export_extras", True):   # snarky response / story: written into the file only when the card has them
            if p.get("snark"):
                card["snark"] = p["snark"]
            if p.get("story"):
                card["story"] = p["story"]
        if p.get("explanation") and p.get("explanation_mock"):
            mock_expl += 1
        elif p.get("explanation"):
            if p.get("explanation_verified") or not require_verified:
                card["explanation"] = p["explanation"]
                if p.get("explanation_source"):
                    card["explanation_source"] = p["explanation_source"]
            else:
                unverified += 1
        root = n
        while True:
            parents = [S.nodes[i] for i in root["parent_ids"] if i in S.nodes and S.nodes[i]["kind"] in QUESTION_KINDS]
            if not parents:
                break
            root = parents[0]
        if root["id"] != n["id"] and root["payload"].get("card_id"):
            card["group"] = root["payload"]["card_id"]
        cards.append(card)

    path = DATA / cfg["folder"] / "curriculum.json"
    existing_ids: set[str] = set()
    if path.exists():
        try:
            existing_ids = {c["id"] for c in json.loads(path.read_text(encoding="utf-8")).get("cards", [])}
        except ValueError:
            pass
    new_ids = {c["id"] for c in cards}
    warnings = []
    if cfg.get("facets"):
        legacy = sum(1 for c in cards if "facet" not in c)
        if legacy:
            warnings.append(f"{legacy} card(s) still carry their old AI-written title because they have no facet yet (use Assign facets on the topic page)")
    if mock_expl:
        warnings.append(f"{mock_expl} explanation(s) came from the mock backend and were left out of the file")
    if unverified:
        warnings.append(f"{unverified} explanation(s) were left out of the file because they aren't marked verified yet (Settings can relax this)")
    if SRS_ENGINE.exists() and f"res://data/{cfg['folder']}/curriculum.json" not in SRS_ENGINE.read_text(encoding="utf-8"):
        warnings.append(f"autoload/srs_engine.gd TOPIC_PATHS does not list res://data/{cfg['folder']}/curriculum.json yet")
    parts = ROOT / "scripts" / "card_parts.gd"
    if parts.exists():
        known = set(re.findall(r'"(\w+)"\s*:', (re.search(r"LEVELS\s*:?=\s*\{([^}]*)\}", parts.read_text(encoding="utf-8")) or [None, ""])[1]))
        unknown = sorted({c["difficulty"] for c in cards} - known)
        if known and unknown:
            warnings.append(f"scripts/card_parts.gd doesn't know the difficulty tier(s) {', '.join(unknown)} yet - those cards will show one dot and no label until LEVELS/LABELS are extended")
    if write and cards:
        doc = {"topic": cfg["name"], "department": cfg["department"], "cards": cards}
        atomic_write(path, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    return {
        "topic": topic_id, "written": bool(write and cards), "path": str(path.relative_to(DATA.parent)) if DATA.parent in path.parents else str(path), "card_count": len(cards),
        "added": sorted(new_ids - existing_ids), "removed": sorted(existing_ids - new_ids), "skipped": skipped, "warnings": warnings,
        "note": "" if cards else "No approved, exportable questions - nothing written.",
    }


def import_game_data() -> dict:
    """Bring existing data/<topic>/curriculum.json cards in as approved questions (idempotent)."""
    S = STORE
    imported = 0
    with S.lock:
        have = {n["payload"].get("card_id") for n in S.nodes.values() if n["kind"] in QUESTION_KINDS}
        for cfg in S.config["topics"]:
            path = DATA / cfg["folder"] / "curriculum.json"
            if not path.exists():
                continue
            for card in json.loads(path.read_text(encoding="utf-8")).get("cards", []):
                if card["id"] in have:
                    continue
                payload = {k: card.get(k) for k in ("title", "question", "answer", "choices", "difficulty", "input_type")}
                payload["card_id"] = card["id"]
                clean, _ = normalize_question(payload, card.get("input_type", "multiple_choice"))
                S.create_node(cfg["id"], "question", clean, [], None, "approved",
                              {"origin": "import", "note": f"Imported from data/{cfg["folder"]}/curriculum.json (written before the studio existed)", "mock": False})
                have.add(card["id"])
                imported += 1
    return {"imported": imported}


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, status: int, message: str, extra: dict | None = None) -> None:
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra or {}


def run_summary(run: dict, full: bool = False) -> dict:
    keys = ("id", "stage", "topic", "status", "backend", "model", "created_at", "started_at", "ended_at", "duration_s", "cost_usd", "error", "params", "chain", "chain_parent")
    out = {k: run.get(k) for k in keys}
    out["created_count"] = len(run.get("created_node_ids", []))
    out["warning_count"] = len(run.get("warnings", []))
    if full:
        out.update({k: run.get(k) for k in ("prompt", "tools", "instructions", "input_ids", "raw_output", "warnings", "created_node_ids", "updated_node_ids", "applied_facets")})
    return out


def api_state() -> dict:
    S = STORE
    return {
        "config": {k: S.config[k] for k in ("backend", "model", "concurrency", "budget_usd", "timeout_s", "game_input_types", "require_verified_explanations", "export_extras")},
        "topics": [S.topic_summary(t) for t in S.topics_in_stage("active")],
        "ideas": [S.topic_summary(t) for t in S.topics_in_stage("idea")],
        "archived": [S.topic_summary(t) for t in S.topics_in_stage("archived")],
        "departments": DEPARTMENTS,
        "tiers": [{"name": n, "score": sc, "anchor": a} for n, sc, a in TIERS],
        "active_runs": RUNNER.active_count(),
        "review_count": sum(1 for n in S.nodes.values() if n["kind"] in QUESTION_KINDS and n["status"] == "draft"),
        "stages": STAGE_LABELS,
        "variant_types": {k: v for k, v in VARIANT_TYPES.items()},
    }


def api_topic_nodes(topic_id: str) -> dict:
    S = STORE
    if not S.topic_cfg(topic_id):
        raise ApiError(404, "unknown topic")
    nodes = [n for n in S.nodes.values() if n["topic"] == topic_id]
    child_counts: dict[str, int] = {}
    for n in nodes:
        for p in n["parent_ids"]:
            child_counts[p] = child_counts.get(p, 0) + 1
    nodes.sort(key=lambda n: n["created_at"])
    return {"nodes": [S.light(n, child_counts) for n in nodes]}


def api_node(node_id: str) -> dict:
    S = STORE
    node = S.nodes.get(node_id)
    if not node:
        raise ApiError(404, "unknown node")
    counts: dict[str, int] = {}
    brief = lambda n: {**S.light(n, counts), "depth": n.get("depth", 0)}  # noqa: E731
    run = STORE.runs.get(node["provenance"].get("run_id", ""))
    return {
        "node": node,
        "ancestors": [brief(n) for n in S.ancestors_of(node_id)],
        "children": [brief(n) for n in S.children_of(node_id)],
        "run": run_summary(run) if run else None,
        "facts_waiting": 0 if node["kind"] in QUESTION_KINDS or node["kind"] == "fact" else len(facts_waiting(node["topic"], node_id)),
        "problems": normalize_question(node["payload"], node["payload"].get("input_type"), topic_facets(node["topic"]))[1] if node["kind"] in QUESTION_KINDS else [],
        "check": {
            "state": check_state(node) if node["kind"] in QUESTION_KINDS else None,
            "record": node.get("check"),
            "suggestion": node.get("suggestion"),
            "has_fact": bool(fact_ancestors(node)) if node["kind"] in QUESTION_KINDS else None,
            "missing": fill_missing(node) if node["kind"] in QUESTION_KINDS else None,
            "rollup": check_rollup(S.descendants_of(node_id)) if node["kind"] not in QUESTION_KINDS else None,
        },
    }


def api_update_node(node_id: str, body: dict) -> dict:
    S = STORE
    with S.lock:
        node = S.nodes.get(node_id)
        if not node:
            raise ApiError(404, "unknown node")
        by = clean_str(body.get("edited_by"))[:40] or "human"
        if "payload" in body:
            before = json.loads(json.dumps(node["payload"]))
            new_payload = {**node["payload"], **body["payload"]}
            if node["kind"] in QUESTION_KINDS:
                clean, _ = normalize_question(new_payload, new_payload.get("input_type"), topic_facets(node["topic"]))
                new_payload = clean
                if not new_payload.get("card_id"):
                    new_payload["card_id"] = S.unique_card_id(node["topic"], new_payload.get("title") or "card")
                taken = [n for n in S.nodes.values() if n["id"] != node_id and n["kind"] in QUESTION_KINDS and n["payload"].get("card_id") == new_payload["card_id"]]
                if taken:
                    raise ApiError(400, f"card id {new_payload['card_id']!r} is already used by {taken[0]['id']}")
            elif node["kind"] == "fact":
                new_payload["text"] = clean_str(new_payload.get("text"))
            if new_payload.get("explanation") != before.get("explanation"):  # a human rewrote it: no longer unchecked machine text
                if "explanation_verified" not in body["payload"]:
                    new_payload["explanation_verified"] = False
                if "explanation_mock" not in body["payload"]:
                    new_payload["explanation_mock"] = False
            if new_payload != before:
                node["payload"] = new_payload
                node.pop("suggestion", None)   # it was written for the old text
                node["history"].append({"at": now_iso(), "by": by, "action": "edit", "before": before})
        if "status" in body:
            status = body["status"]
            if status not in STATUSES:
                raise ApiError(400, "bad status")
            if status == "approved" and node["kind"] in QUESTION_KINDS:
                problems = normalize_question(node["payload"], node["payload"].get("input_type"), topic_facets(node["topic"]))[1]
                if problems:
                    raise ApiError(400, "cannot approve: " + "; ".join(problems), {"problems": problems})
            if status != node["status"]:
                node["history"].append({"at": now_iso(), "by": by, "action": "status", "from": node["status"], "to": status})
                node["status"] = status
        node["history"] = node["history"][-60:]
        node["updated_at"] = now_iso()
        S.save_topic(node["topic"])
        S.reconcile_approval(node["topic"])
    return api_node(node_id)


def api_approve_bulk(topic_id: str, body: dict) -> dict:
    """Approve every draft under one node (at any depth), or in the whole topic. Rejected nodes stay rejected;
    cards that fail validation are left as drafts and listed, so nothing invalid gets approved by accident."""
    S = STORE
    if not S.topic_cfg(topic_id):
        raise ApiError(404, "unknown topic")
    node_id = clean_str(body.get("node_id"))
    with S.lock:
        if node_id:
            if node_id not in S.nodes or S.nodes[node_id]["topic"] != topic_id:
                raise ApiError(404, "unknown node")
            pool = S.descendants_of(node_id)
        else:
            pool = [n for n in S.nodes.values() if n["topic"] == topic_id]
        todo, blocked = [], []
        for n in pool:
            if n["status"] != "draft":
                continue
            problems = normalize_question(n["payload"], n["payload"].get("input_type"), topic_facets(topic_id))[1] if n["kind"] in QUESTION_KINDS else []
            if n["kind"] == "fact" and not clean_str(n["payload"].get("text")):
                problems = ["the fact has no text"]
            (blocked if problems else todo).append((n, problems))
        by_kind: dict[str, int] = {}
        for n, _ in todo:
            by_kind[n["kind"]] = by_kind.get(n["kind"], 0) + 1
        result = {"approved": len(todo), "by_kind": by_kind, "blocked": [{"id": n["id"], "snippet": S.snippet(n), "why": "; ".join(pr)} for n, pr in blocked][:20],
                  "blocked_count": len(blocked), "preview": bool(body.get("preview"))}
        if not body.get("preview") and todo:
            stamp = now_iso()
            for n, _ in todo:
                n["history"].append({"at": stamp, "by": "human (bulk)", "action": "status", "from": "draft", "to": "approved"})
                n["history"] = n["history"][-60:]
                n["status"], n["updated_at"] = "approved", stamp
            S.save_topic(topic_id)
    return result


def api_create_node(body: dict) -> dict:
    S = STORE
    topic_id, kind = body.get("topic"), body.get("kind")
    if not S.topic_cfg(topic_id or "") or kind not in KINDS:
        raise ApiError(400, "need a valid topic and kind")
    parents = [p for p in body.get("parent_ids", []) if p in S.nodes]
    payload = body.get("payload") or {}
    vtype = body.get("variant_type")
    if kind in QUESTION_KINDS:
        payload, _ = normalize_question(payload, payload.get("input_type") or "multiple_choice", topic_facets(topic_id))
    node = S.create_node(topic_id, kind, payload, parents, vtype, "draft", {"origin": "human"})
    return api_node(node["id"])


def delete_plan(node_id: str) -> tuple[set[str], list[str]]:
    """Everything that goes with a node: its descendants that hang only from deleted nodes. Anything that also
    descends from a surviving node is kept and just loses this parent."""
    S = STORE
    doomed = {node_id}
    changed = True
    while changed:
        changed = False
        for c in S.nodes.values():
            if c["id"] not in doomed and c["parent_ids"] and any(p in doomed for p in c["parent_ids"]) \
                    and all(p in doomed or p not in S.nodes for p in c["parent_ids"]):
                doomed.add(c["id"])
                changed = True
    detached = [c["id"] for c in S.nodes.values() if c["id"] not in doomed and any(p in doomed for p in c["parent_ids"])]
    return doomed, detached


def api_delete_node(node_id: str, body: dict | None = None) -> dict:
    """Permanent delete - no archive, no record. Children go too only with cascade; body {"preview": true} just counts."""
    S, body = STORE, body or {}
    with S.lock:
        node = S.nodes.get(node_id)
        if not node:
            raise ApiError(404, "unknown node")
        doomed, detached = delete_plan(node_id)
        kids = doomed - {node_id}
        by_kind: dict[str, int] = {}
        for i in kids:
            by_kind[S.nodes[i]["kind"]] = by_kind.get(S.nodes[i]["kind"], 0) + 1
        info = {"children": len(kids), "by_kind": by_kind, "detached": len(detached)}
        if body.get("preview"):
            return {"preview": True, **info}
        if kids and not body.get("cascade"):
            raise ApiError(400, f"this node has {len(kids)} derived item(s) that would be deleted with it", info)
        for i in doomed:
            del S.nodes[i]
        for i in detached:
            S.nodes[i]["parent_ids"] = [p for p in S.nodes[i]["parent_ids"] if p in S.nodes]
        S.save_topic(node["topic"])
        S.reconcile_approval(node["topic"])
    return {"deleted": sorted(doomed), **info}


def api_suggestion(node_id: str, body: dict) -> dict:
    """Act on an accuracy-check suggestion: accept (apply it), dismiss (drop it), keep (drop it and mark the card fine as written), undo (reverse an accept)."""
    S = STORE
    action = clean_str(body.get("action"))
    with S.lock:
        node = S.nodes.get(node_id)
        if not node or node["kind"] not in QUESTION_KINDS:
            raise ApiError(404, "unknown card")
        stamp = now_iso()
        if action == "undo":
            last = node["history"][-1] if node["history"] else {}
            if last.get("action") != "accept_suggestion":
                raise ApiError(400, "nothing to undo - the last change wasn't an accepted suggestion")
            node["payload"] = last["before"]
            if last.get("prev_check"):
                node["check"] = last["prev_check"]
            else:
                node.pop("check", None)
            if last.get("prev_suggestion"):
                node["suggestion"] = last["prev_suggestion"]
            node["history"].pop()
        else:
            sug = node.get("suggestion")
            if action not in ("accept", "dismiss", "keep"):
                raise ApiError(400, "action must be accept, dismiss, keep or undo")
            if not sug:
                raise ApiError(400, "this card has no pending suggestion")
            if action == "accept":
                if sug.get("basis") != check_basis(node):
                    raise ApiError(409, "the card changed after this suggestion was made - run the check again")
                before = json.loads(json.dumps(node["payload"]))
                new_payload, _ = normalize_question({**node["payload"], **sug["fields"]}, node["payload"].get("input_type"), topic_facets(node["topic"]))
                if new_payload.get("explanation") != before.get("explanation"):
                    new_payload["explanation_verified"], new_payload["explanation_mock"] = False, False
                node["history"].append({"at": stamp, "by": "human (accepted suggestion)", "action": "accept_suggestion", "before": before,
                                        "prev_check": node.get("check"), "prev_suggestion": sug})
                node["payload"] = new_payload
                node["check"] = {"basis": check_basis(node), "verdict": "ok", "notes": "applied the suggested fix", "at": stamp, "run_id": sug.get("run_id")}
                node.pop("suggestion", None)
            elif action == "keep":
                node["check"] = {"basis": check_basis(node), "verdict": "ok", "notes": "kept as written", "at": stamp, "run_id": (node.get("check") or {}).get("run_id")}
                node.pop("suggestion", None)
                node["history"].append({"at": stamp, "by": "human", "action": "keep_as_written"})
            else:
                node.pop("suggestion", None)
                node["history"].append({"at": stamp, "by": "human", "action": "dismiss_suggestion"})
        node["history"] = node["history"][-60:]
        node["updated_at"] = stamp
        S.save_topic(node["topic"])
    return api_node(node_id)


def api_review(body: dict) -> dict:
    """Cards to look through one by one. mode "drafts" = cards still in draft; "attention" = drafts plus any card that
    has a fact but is unchecked, changed since its check, or has issues. Ordered by topic, then creation."""
    S = STORE
    mode, only = clean_str(body.get("mode")) or "drafts", clean_str(body.get("topic"))
    tmap = {t["id"]: t for t in S.config["topics"]}
    order = {t["id"]: i for i, t in enumerate(S.config["topics"])}

    def wanted(n: dict) -> bool:
        if n["kind"] not in QUESTION_KINDS or n["status"] == "rejected" or n["topic"] not in tmap or tmap[n["topic"]].get("stage") == "archived":
            return False
        if only and n["topic"] != only:
            return False
        return n["status"] == "draft" or (mode == "attention" and needs_check(n))

    # Same ordering as a topic's own list (creation time, then insertion order), so Review and the topic screen agree.
    pool = sorted((n for n in S.nodes.values() if wanted(n)), key=lambda n: (order.get(n["topic"], 999), n["created_at"]))
    per_topic: dict[str, int] = {}
    for n in pool:
        per_topic[n["topic"]] = per_topic.get(n["topic"], 0) + 1
    cards = []
    for n in pool[:400]:
        p, rec, t = n["payload"], n.get("check") or {}, tmap[n["topic"]]
        facts = fact_ancestors(n)
        src = next((a for a in S.ancestors_of(n["id"]) if a["kind"] == "source"), None)
        state = check_state(n)
        cards.append({
            "id": n["id"], "topic": n["topic"], "topic_name": t["name"], "department": t["department"], "kind": n["kind"], "variant_type": n.get("variant_type"),
            "status": n["status"], "payload": {k: p.get(k) for k in ("title", "facet", "question", "answer", "choices", "difficulty", "explanation", "snark", "story", "input_type")},
            "missing": fill_missing(n),
            "check": state, "notes": rec.get("notes", "") if state in ("ok", "issues") else "", "suggestion": n.get("suggestion"),
            "has_fact": bool(facts), "fact": {"text": facts[0]["payload"].get("text", ""), "context": facts[0]["payload"].get("context", "")} if facts else None,
            "source": {"title": src["payload"].get("title", ""), "url": src["payload"].get("url", "")} if src else None,
            "problems": normalize_question(p, p.get("input_type"), topic_facets(n["topic"]))[1],
        })
    return {"cards": cards, "total": len(pool), "topics": [{"id": tid, "name": tmap[tid]["name"], "count": c} for tid, c in sorted(per_topic.items(), key=lambda kv: order.get(kv[0], 999))]}


def api_search(body: dict) -> dict:
    S = STORE
    q = clean_str(body.get("q")).lower()
    if len(q) < 2:
        return {"topics": [], "nodes": []}
    topics = [{"id": t["id"], "name": t["name"], "department": t["department"], "stage": t.get("stage", "active"), "approved": bool(t.get("approved"))}
              for t in S.config["topics"] if q in " ".join([t["name"], t["department"], t["id"], t.get("note", "")]).lower()]
    names = {t["id"]: t["name"] for t in S.config["topics"]}
    hits = []
    for n in sorted(S.nodes.values(), key=lambda n: (n["topic"], n["created_at"])):
        p = n["payload"]
        hay = " ".join(str(x) for k in ("question", "answer", "title", "facet", "text", "context", "summary", "explanation", "url") for x in [p.get(k, "")]) + " " + " ".join(p.get("choices") or [])
        if q in hay.lower():
            hits.append({"id": n["id"], "topic": n["topic"], "topic_name": names.get(n["topic"], n["topic"]), "kind": n["kind"], "status": n["status"], "snippet": S.snippet(n)})
    return {"topics": topics, "nodes": hits[:60], "total_nodes": len(hits)}


def api_run(body: dict) -> dict:
    S = STORE
    stage, topic_id = body.get("stage"), body.get("topic")
    topic_cfg = S.topic_cfg(topic_id or "")
    if not topic_cfg or stage not in STAGE_LABELS:
        raise ApiError(400, "need a valid topic and stage")
    params = body.get("params") or {}
    if stage == "make_variants" and params.get("variant_type", "paraphrase") not in VARIANT_TYPES:
        raise ApiError(400, "unknown variant type")
    if stage == "draft_questions" and clean_str(params.get("difficulty")).lower() not in ("", "auto", *TIER_NAMES):
        raise ApiError(400, "difficulty must be auto or one of: " + ", ".join(TIER_NAMES))
    if stage == "suggest_facets" and topic_cfg.get("facets"):
        raise ApiError(400, "this topic already has facets - edit them on the topic page")
    if stage == "assign_facets" and not topic_cfg.get("facets"):
        raise ApiError(400, "give the topic facets first (type them in, or use Suggest facets)")
    if stage == "check_accuracy":
        for nid in params.get("input_ids") or []:
            nd = S.nodes.get(nid)
            if not nd or nd["kind"] not in QUESTION_KINDS or not fact_ancestors(nd):
                raise ApiError(400, "check_accuracy needs cards that were written from a fact")
    inputs = select_inputs(stage, topic_id, params)
    if stage in ("extract_facts", "draft_questions", "make_variants", "assign_facets", "add_explanations", "check_accuracy", "fill_card") and not inputs:
        raise ApiError(400, {"extract_facts": "no sources are waiting for fact extraction", "draft_questions": "no facts are waiting for questions",
                             "make_variants": "no approved questions are waiting for that variant type",
                             "assign_facets": "every card in this topic already has a facet",
                             "add_explanations": "every card in this topic already has an explanation",
                             "check_accuracy": "nothing needs checking - every card with a fact is checked and unchanged",
                             "fill_card": "no card has blanks to fill"}[stage])
    prompt = build_prompt(stage, topic_cfg, params, inputs)
    override = clean_str(body.get("prompt"))
    if override:   # the author edited the generated prompt before running it
        if "Output:" not in override:
            raise ApiError(400, "keep the 'Output:' line at the end of the prompt - the studio needs it to read the answer")
        prompt = override
    followup = ""
    if stage == "draft_questions" and not params.get("input_ids"):
        rest = len(facts_waiting(topic_id, clean_str(params.get("under")))) - len(inputs)
        if rest > 0:
            followup = f"{rest} more fact(s) are still waiting after this batch. Run it again when this one finishes."
    if body.get("preview"):
        return {"preview": True, "followup": followup, "stage": stage, "label": STAGE_LABELS[stage], "backend": S.config["backend"],
                "model": S.config.get("model") or "(CLI default)", "tools": STAGE_TOOLS.get(stage, []), "prompt": prompt,
                "input_ids": [n["id"] for n in inputs], "inputs": [{"id": n["id"], "kind": n["kind"], "snippet": S.snippet(n)} for n in inputs]}
    run = RUNNER.make_run(stage, topic_cfg, params, inputs, prompt)
    RUNNER.submit(run)
    return run_summary(run)


def _scope(body: dict) -> tuple[dict, dict | None]:
    S = STORE
    t = S.topic_cfg(body.get("topic") or "")
    if not t:
        raise ApiError(400, "need a valid topic")
    node_id = clean_str(body.get("node_id"))
    node = S.nodes.get(node_id) if node_id else None
    if node_id and (not node or node["topic"] != t["id"]):
        raise ApiError(404, "unknown node")
    return t, node


def api_fill(body: dict) -> dict:
    """'Fill in automatically' at any level: it works out the next stage that applies, and only ever fills what is blank."""
    S = STORE
    t, node = _scope(body)
    params: dict = {"note": clean_str(body.get("note"))}
    if clean_str(body.get("what")) == "questions":
        # "Write questions for every fact" under a source/topic (30 per run; run it again for the rest).
        if node and node["kind"] in (*QUESTION_KINDS, "fact"):
            raise ApiError(400, "use 'Fill in automatically' on a single fact or card; this writes questions for the facts beneath a source or topic")
        scope = node["id"] if node else ""
        if not facts_waiting(t["id"], scope):
            raise ApiError(400, "every fact here already has a question")
        params.update(under=scope, limit=max(1, min(int(body.get("limit") or 30), 30)), difficulty=clean_str(body.get("difficulty")))
        return api_run({"stage": "draft_questions", "topic": t["id"], "params": params, "preview": body.get("preview"), "prompt": body.get("prompt")})
    if node:
        if node["kind"] in QUESTION_KINDS:
            if not fill_missing(node):
                raise ApiError(400, "nothing is blank on this card")
            stage, params["input_ids"] = "fill_card", [node["id"]]
        elif node["kind"] == "fact":
            if any(c["kind"] == "question" for c in S.children_of(node["id"])):
                raise ApiError(400, "this fact already has a question - open it to fill any blanks")
            stage, params["input_ids"], params["difficulty"] = "draft_questions", [node["id"]], clean_str(body.get("difficulty"))
        else:
            stage, params["input_ids"], params["per_item"] = "extract_facts", [node["id"]], int(body.get("count") or 10)
    else:
        mine = [n for n in S.nodes.values() if n["topic"] == t["id"]]
        sources = [n for n in mine if n["kind"] == "source" and n["status"] != "rejected"]
        if not sources:
            stage, params["count"] = "seed_topic", int(body.get("count") or 10)
        elif any("fact" not in {c["kind"] for c in S.children_of(n["id"])} for n in sources):
            stage, params["limit"], params["per_item"] = "extract_facts", 5, int(body.get("count") or 10)
        elif facts_waiting(t["id"]):
            stage, params["limit"] = "draft_questions", 10
        elif any(n["kind"] in QUESTION_KINDS and n["status"] != "rejected" and fill_missing(n) for n in mine):
            stage = "fill_card"
        else:
            raise ApiError(400, "nothing left to fill in automatically")
    return api_run({"stage": stage, "topic": t["id"], "params": params, "preview": body.get("preview"), "prompt": body.get("prompt")})


def api_check(body: dict) -> dict:
    """'Check for accuracy' at any level: checks the cards beneath it that are new or changed since their last check."""
    S = STORE
    t, node = _scope(body)
    if node and node["kind"] in QUESTION_KINDS:
        if not fact_ancestors(node):
            raise ApiError(400, "this card has no fact to check against")
        todo = [node]
    else:
        pool = S.descendants_of(node["id"]) if node else [n for n in S.nodes.values() if n["topic"] == t["id"]]
        todo = [n for n in sorted(pool, key=lambda n: n["created_at"]) if needs_check(n)]
        if not todo:
            raise ApiError(400, "nothing to check - every card here is checked and unchanged" if any(n["kind"] in QUESTION_KINDS and fact_ancestors(n) for n in pool)
                           else "there are no cards with a fact to check against")
    ids = [n["id"] for n in todo][:25]
    return api_run({"stage": "check_accuracy", "topic": t["id"], "params": {"input_ids": ids, "note": clean_str(body.get("note"))}, "preview": body.get("preview"), "prompt": body.get("prompt")})


def api_create_topic(body: dict) -> dict:
    S = STORE
    name = clean_str(body.get("name"))
    department = clean_str(body.get("department"))
    if not name or len(name) > 60:
        raise ApiError(400, "give the topic a name (60 characters max)")
    if department not in DEPARTMENTS:
        raise ApiError(400, "pick one of the six departments")
    with S.lock:
        if any(t["name"].lower() == name.lower() for t in S.config["topics"]):
            raise ApiError(400, f"a topic named {name!r} already exists")
        base = slug(name, 30)
        tid, i = base, 2
        while S.topic_cfg(tid) or (DATA / tid).exists():
            tid = f"{base}_{i}"
            i += 1
        S.config["topics"].append({"id": tid, "name": name, "department": department, "folder": tid, "target": 24, "stage": "idea",
                                   "note": clean_str(body.get("note")), "facets": clean_facets(body.get("facets")), "created_at": now_iso()})
        S.save_config()
    return S.topic_summary(S.topic_cfg(tid))


def api_update_topic(topic_id: str, body: dict) -> dict:
    S = STORE
    with S.lock:
        t = S.topic_cfg(topic_id)
        if not t:
            raise ApiError(404, "unknown topic")
        current = t.get("stage", "active")
        if "name" in body:
            name = clean_str(body["name"])
            if not name or len(name) > 60:
                raise ApiError(400, "give the topic a name (60 characters max)")
            if any(o["id"] != topic_id and o["name"].lower() == name.lower() for o in S.config["topics"]):
                raise ApiError(400, f"a topic named {name!r} already exists")
            t["name"] = name
        if "department" in body:
            if body["department"] not in DEPARTMENTS:
                raise ApiError(400, "pick one of the six departments")
            t["department"] = body["department"]
        if "note" in body:
            t["note"] = clean_str(body["note"])
        if "facets" in body:
            t["facets"] = clean_facets(body["facets"])
        if "approved" in body:
            want = bool(body["approved"])
            if want and current == "archived":
                raise ApiError(400, "an archived topic can't be approved")
            if want and not any(n["topic"] == topic_id and n["kind"] in QUESTION_KINDS and n["status"] == "approved" for n in S.nodes.values()):
                raise ApiError(400, "approve at least one question in this topic first")
            t["approved"] = want
            if want and current == "idea":   # an approved topic is in the game, so it is active
                t["stage"] = current = "active"
        if "stage" in body and body["stage"] != current:
            new = body["stage"]
            allowed = {"idea": ("active", "archived"), "archived": ("idea",), "active": ()}
            if new not in allowed.get(current, ()):
                raise ApiError(400, f"a topic can't move from {current} to {new}")
            t["stage"] = new
        S.save_config()
    return S.topic_summary(t)


def api_delete_topic(topic_id: str, body: dict | None = None) -> dict:
    """Permanently delete a topic and everything in it. An approved topic has to be un-approved first, so game content
    can't go by accident. body {"preview": true} counts what would go; {"cascade": true} is required if it has content."""
    S, body = STORE, body or {}
    with S.lock:
        t = S.topic_cfg(topic_id)
        if not t:
            raise ApiError(404, "unknown topic")
        if t.get("approved"):
            raise ApiError(400, "this topic is approved for the game - un-approve it first")
        ids = [n["id"] for n in S.nodes.values() if n["topic"] == topic_id]
        by_kind: dict[str, int] = {}
        for i in ids:
            by_kind[S.nodes[i]["kind"]] = by_kind.get(S.nodes[i]["kind"], 0) + 1
        info = {"nodes": len(ids), "by_kind": by_kind}
        if body.get("preview"):
            return {"preview": True, **info}
        if ids and not body.get("cascade"):
            raise ApiError(400, f"this topic still has {len(ids)} item(s); delete with cascade to remove them too", info)
        for i in ids:
            del S.nodes[i]
        S.config["topics"].remove(t)
        S.save_config()
        (CONTENT / "topics" / f"{topic_id}.json").unlink(missing_ok=True)
    return {"deleted": topic_id, **info}


def api_test_backend() -> dict:
    cfg = STORE.config
    if cfg["backend"] == "mock":
        return {"ok": True, "detail": "mock backend is selected - nothing to test"}
    try:
        proc = subprocess.run(["claude", "-p", "--output-format", "json", "--no-session-persistence", "--tools", "", "--max-budget-usd", "0.25"] + (["--model", cfg["model"]] if cfg.get("model") else []),
                              input='Reply with exactly: {"ok": true}', capture_output=True, text=True, timeout=120, cwd=tempfile.gettempdir())
        env = json.loads(proc.stdout)
        if env.get("is_error"):
            return {"ok": False, "detail": str(env.get("result"))}
        return {"ok": True, "detail": f"claude replied: {str(env.get('result'))[:120]} (cost ${env.get('total_cost_usd')})"}
    except FileNotFoundError:
        return {"ok": False, "detail": "`claude` CLI not found on PATH"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}


def api_put_config(body: dict) -> dict:
    S = STORE
    with S.lock:
        for key in ("backend", "model", "concurrency", "budget_usd", "timeout_s", "game_input_types", "require_verified_explanations", "export_extras"):
            if key in body:
                S.config[key] = body[key]
        if S.config["backend"] not in ("claude_cli", "mock"):
            raise ApiError(400, "backend must be claude_cli or mock")
        if "instructions" in body:
            S.config["instructions"] = body["instructions"]
        if "topics" in body:
            for incoming in body["topics"]:
                t = S.topic_cfg(incoming["id"])
                if t:
                    for key in ("name", "department", "folder", "target"):
                        if key in incoming:
                            t[key] = incoming[key]
        S.save_config()
    return {"ok": True, "instructions": S.config["instructions"], "topics": S.config["topics"], **api_state()["config"]}


ROUTES: list[tuple[str, re.Pattern, object]] = []


def route(method: str, pattern: str):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
        return fn
    return deco


route("GET", r"/api/state")(lambda m, b: api_state())
route("GET", r"/api/config")(lambda m, b: {**STORE.config})
route("PUT", r"/api/config")(lambda m, b: api_put_config(b))
route("GET", r"/api/topics/([\w-]+)/nodes")(lambda m, b: api_topic_nodes(m.group(1)))
route("GET", r"/api/nodes/([\w-]+)")(lambda m, b: api_node(m.group(1)))
route("PUT", r"/api/nodes/([\w-]+)")(lambda m, b: api_update_node(m.group(1), b))
route("DELETE", r"/api/nodes/([\w-]+)")(lambda m, b: api_delete_node(m.group(1), b))
route("POST", r"/api/nodes/([\w-]+)/suggestion")(lambda m, b: api_suggestion(m.group(1), b))
route("GET", r"/api/search")(lambda m, b: api_search(b))
route("GET", r"/api/review")(lambda m, b: api_review(b))
route("POST", r"/api/fill")(lambda m, b: api_fill(b))
route("POST", r"/api/check")(lambda m, b: api_check(b))
route("POST", r"/api/nodes")(lambda m, b: api_create_node(b))
route("POST", r"/api/run")(lambda m, b: api_run(b))
route("GET", r"/api/runs")(lambda m, b: {"runs": [run_summary(r) for r in sorted(STORE.runs.values(), key=lambda r: r["created_at"], reverse=True)]})
route("GET", r"/api/runs/([\w-]+)")(lambda m, b: run_summary(STORE.runs[m.group(1)], full=True) if m.group(1) in STORE.runs else (_ for _ in ()).throw(ApiError(404, "unknown run")))
route("POST", r"/api/runs/([\w-]+)/cancel")(lambda m, b: (RUNNER.cancel(m.group(1)), {"ok": True})[1])
route("POST", r"/api/export")(lambda m, b: {"results": [export_topic(t, bool(b.get("write"))) for t in ([b["topic"]] if b.get("topic") else [x["id"] for x in STORE.topics_in_stage("active") if x.get("approved")])]})
route("POST", r"/api/topics")(lambda m, b: api_create_topic(b))
route("PUT", r"/api/topics/([\w-]+)")(lambda m, b: api_update_topic(m.group(1), b))
route("DELETE", r"/api/topics/([\w-]+)")(lambda m, b: api_delete_topic(m.group(1), b))
route("POST", r"/api/topics/([\w-]+)/approve")(lambda m, b: api_approve_bulk(m.group(1), b))
route("POST", r"/api/import-game-data")(lambda m, b: import_game_data())
route("POST", r"/api/test-backend")(lambda m, b: api_test_backend())

STATIC = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/style.css": "style.css"}
MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}


# --------------------------------------------------------------------------
# network access
# --------------------------------------------------------------------------
# Default: bound to 127.0.0.1, so only this laptop can reach it. With --lan it listens on every
# interface. The app has NO login of its own: access control is expected to be enforced in front of
# it (Cloudflare Access on the public hostname; keep the port off untrusted networks).

LAN_MODE = False
# Extra Host names accepted in LAN mode, e.g. "content.example.com" when a tunnel/proxy fronts the studio.
# Anything else is still refused, so a hostile web page can't reach the studio under a name it controls.
ALLOWED_HOSTS: set[str] = {h.strip().lower() for h in os.environ.get("STUDIO_ALLOWED_HOSTS", "").split(",") if h.strip()}


def host_allowed(host_header: str) -> bool:
    """Reject unexpected Host headers (blocks DNS-rebinding from a web page the user visits)."""
    host = host_header.strip().lower()
    host = host[1:].split("]")[0] if host.startswith("[") else host.split(":")[0]
    if host in ("127.0.0.1", "localhost", "::1"):
        return True
    if not LAN_MODE:
        return False
    try:
        ipaddress.ip_address(host)  # a bare IP can't be an attacker-controlled DNS name
        return True
    except ValueError:
        pass
    mine = socket.gethostname().lower()
    return host in ALLOWED_HOSTS or host.endswith(".local") or host == mine or host == mine.split(".")[0]


def lan_addresses() -> list[str]:
    found = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))  # no packet is sent; this just picks the outbound interface
            found.append(sock.getsockname()[0])
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip not in found and not ip.startswith("127."):
                found.append(ip)
    except OSError:
        pass
    return found


class Handler(BaseHTTPRequestHandler):
    server_version = "ContentStudio/1.0"

    def log_message(self, fmt, *args) -> None:  # quiet
        pass

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _handle(self, method: str) -> None:
        if not host_allowed(self.headers.get("Host") or ""):
            return self._json(403, {"error": "unexpected Host header"})
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if method == "GET" and path in STATIC:
            file = UI_DIR / STATIC[path]
            return self._send(200, file.read_bytes(), MIME[file.suffix]) if file.exists() else self._json(404, {"error": "ui file missing"})
        if not path.startswith("/api/"):
            return self._json(404, {"error": "not found"})
        body: dict = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()} if method == "GET" else {}
        if method in ("POST", "PUT", "DELETE"):
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                return self._json(415, {"error": "JSON only"})  # forces a CORS preflight for foreign pages
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._json(400, {"error": "bad JSON"})
        for m, pattern, fn in ROUTES:
            match = pattern.match(path)
            if m == method and match:
                try:
                    return self._json(200, fn(match, body))
                except ApiError as exc:
                    return self._json(exc.status, {"error": exc.message, **exc.extra})
                except Exception as exc:  # noqa: BLE001
                    import traceback
                    traceback.print_exc()
                    return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
        return self._json(404, {"error": "no such endpoint"})

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_DELETE(self) -> None:
        self._handle("DELETE")


def main() -> None:
    global STORE, RUNNER, LAN_MODE
    sys.stdout.reconfigure(line_buffering=True)  # show the banner and links promptly even when output goes to a log file
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true", help="don't open a browser tab")
    parser.add_argument("--lan", action="store_true", help="also listen on your local network (phone access); there is no login, so keep it on a trusted network")
    parser.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                        help="also accept this Host name (repeatable), e.g. a public hostname behind a tunnel")
    parser.add_argument("--import-game-data", action="store_true", help="import existing data/*/curriculum.json cards, then exit")
    args = parser.parse_args()
    STORE = Store()
    RUNNER = Runner()
    if args.import_game_data or not any(STORE.nodes.values()):
        print("imported", import_game_data()["imported"], "existing game cards as approved questions")
        if args.import_game_data:
            return
    LAN_MODE = args.lan
    ALLOWED_HOSTS.update(h.strip().lower() for h in args.allow_host if h.strip())
    server = ThreadingHTTPServer(("0.0.0.0" if LAN_MODE else "127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Content Studio running at {url}  (Ctrl+C to stop)")
    if LAN_MODE and ALLOWED_HOSTS:
        print(f"\nServer mode: answering to {', '.join(sorted(ALLOWED_HOSTS))}. No login: access control is handled upstream.\n")
    elif LAN_MODE:
        print("\nLAN mode ON (no login). Reachable on:")
        names = lan_addresses() + ([socket.gethostname()] if socket.gethostname().endswith(".local") else [])
        for name in names:
            print(f"  http://{name}:{args.port}/")
        print("Only use this on a network you trust. macOS may ask whether Python can accept incoming connections: choose Allow.\n")
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
