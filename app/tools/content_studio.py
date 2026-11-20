#!/usr/bin/env python3
"""Content Studio: a local web app for building the game's question bank.

    python3 tools/content_studio.py            # serves http://127.0.0.1:8765
    python3 tools/content_studio.py --port 9000 --no-open
    python3 tools/content_studio.py --lan      # also reachable from your phone on the same network (needs the access token)

Everything is a *node* in a lineage graph, stored as plain JSON under
design/content/ (diff-friendly, lives in git):

    source  -> fact -> question -> variant (paraphrase / harder / easier /
    (web page,   (one    (a game     true_false / revision ...)
     OpenTDB     claim)   card)
     record)

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
import getpass
import hashlib
import hmac
import ipaddress
import secrets
import socket
import html
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
import urllib.request
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
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
MAX_FACTS_PER_SOURCE = 40   # per run, so one response stays a sensible size; run it again for more
EXPLANATION_SPEC = ('"explanation" (optional "did you know" line: one or two sentences, at most 30 words, each adding something new (no filler), telling the player more about the answer, '
                    'using ONLY information already in the fact, its context or the card shown - never add outside claims; "" if there is nothing to add)')
DEPARTMENTS = ["Geography", "Entertainment", "History", "Arts & Literature", "Science & Nature", "Sports & Leisure"]
# What "worth promoting" means for an idea: enough distinct facts, from more than one independent site.
VET_MIN_FACTS = 30
VET_MIN_DOMAINS = 2
KINDS = ("source", "fact", "question", "variant")
STATUSES = ("draft", "approved", "rejected")
QUESTION_KINDS = ("question", "variant")

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
        "revise": "",
    },
}

OPENTDB_HINTS = {
    "world_capitals": ([22], ["capital", "country", "countries"]),
    "us_geography": ([22], ["united states", "america", "texas", "california", "new york", "state"]),
    "ww2": ([23], ["world war ii", "world war 2", "hitler", "nazi", "pearl harbor", "normandy", "allied", "d-day"]),
    "cold_war": ([23], ["cold war", "soviet", "stalin", "berlin wall", "vietnam", "mao", "khrushchev", "gorbachev"]),
    "solar_system": ([17], ["planet", "solar system", "sun", "moon", "star", "galaxy", "orbit"]),
    "human_biology": ([17], ["bone", "organ", "muscle", "heart", "brain", "dna", "cell", "blood", "lung"]),
    "famous_painters": ([25], []),
    "harry_potter": ([10], ["harry potter", "hogwarts", "rowling", "horcrux"]),
    "soccer": ([21], ["fifa", "world cup", "football", "soccer", "uefa", "premier league", "manchester united", "real madrid", "ronaldo", "messi"]),
    "formula_one": ([21, 28], ["formula", "grand prix", "f1", "schumacher", "hamilton", "senna", "le mans"]),
    "star_wars": ([11, 14], ["star wars", "jedi", "sith", "darth", "skywalker"]),
    "doctor_who": ([14], ["doctor who", "tardis", "dalek", "gallifrey"]),
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
                           ("timeout_s", 900), ("game_input_types", ["multiple_choice"]), ("require_verified_explanations", True)):
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
            cats, words = OPENTDB_HINTS.get(folder, ([], []))
            topics.append({
                "id": folder, "name": doc.get("topic", folder), "department": doc.get("department", ""),
                "folder": folder, "target": 24, "opentdb": {"categories": cats, "keywords": words},
            })
        return topics

    def save_config(self) -> None:
        with self.lock:
            atomic_write(CONTENT / "config.json", json.dumps(self.config, indent=2, ensure_ascii=False))

    def topic_cfg(self, topic_id: str) -> dict | None:
        return next((t for t in self.config["topics"] if t["id"] == topic_id), None)

    def topics_in_stage(self, stage: str) -> list[dict]:
        return [t for t in self.config["topics"] if t.get("stage", "active") == stage]

    def vet_summary(self, topic_id: str) -> dict:
        nodes = [n for n in self.nodes.values() if n["topic"] == topic_id]
        sources = [n for n in nodes if n["kind"] == "source" and n["status"] != "rejected"]
        facts = [n for n in nodes if n["kind"] == "fact" and n["status"] != "rejected"]
        domains = set()
        for n in sources:
            if n["payload"].get("source_type") == "web" and n["payload"].get("url"):
                host = urllib.parse.urlparse(n["payload"]["url"]).netloc.lower()
                domains.add(host[4:] if host.startswith("www.") else host)
        runs = [r for r in self.runs.values() if r["topic"] == topic_id and r["stage"] in ("find_sources", "extract_facts")]
        running = any(r["status"] in ("queued", "running") for r in runs)
        if running:
            verdict = "vetting"
        elif not runs and not sources and not facts:
            verdict = "unvetted"
        elif len(facts) >= VET_MIN_FACTS and len(domains) >= VET_MIN_DOMAINS:
            verdict = "strong"
        elif len(facts) >= VET_MIN_FACTS // 2:
            verdict = "maybe"
        else:
            verdict = "thin"
        return {"verdict": verdict, "sources": len(sources), "domains": len(domains), "facts": len(facts),
                "mock": any(n["provenance"].get("mock") for n in sources + facts),
                "min_facts": VET_MIN_FACTS, "min_domains": VET_MIN_DOMAINS}

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
        out = {**topic_cfg, "stage": topic_cfg.get("stage", "active"), "counts": counts, "pending": pending, "exportable": exportable, "spread": spread}
        facets = topic_cfg.get("facets") or []
        card_nodes = [n for n in nodes if n["kind"] in QUESTION_KINDS and n["status"] != "rejected"]
        out["facets"] = facets
        out["explanations"] = {"written": sum(1 for n in card_nodes if n["payload"].get("explanation")),
                               "verified": sum(1 for n in card_nodes if n["payload"].get("explanation") and n["payload"].get("explanation_verified")),
                               "missing": sum(1 for n in card_nodes if not n["payload"].get("explanation"))}
        out["facet_counts"] = {f: sum(1 for n in card_nodes if n["payload"].get("facet") == f) for f in facets}
        out["facet_unassigned"] = sum(1 for n in card_nodes if not n["payload"].get("facet")) if facets else 0
        out["facet_orphans"] = sum(1 for n in card_nodes if n["payload"].get("facet") and n["payload"]["facet"] not in facets) if facets else 0
        if out["stage"] != "active":
            out["vet"] = self.vet_summary(tid)
        return out


STORE: Store


# --------------------------------------------------------------------------
# prompts
# --------------------------------------------------------------------------

JSON_TAIL = "\n\nRespond with ONLY the JSON object described above: no markdown fences, no commentary."

STAGE_LABELS = {
    "opentdb": "Import from OpenTDB",
    "find_sources": "Find sources",
    "extract_facts": "Extract facts",
    "draft_questions": "Draft questions",
    "make_variants": "Make variants",
    "revise": "Revise with note",
    "suggest_facets": "Suggest facets",
    "assign_facets": "Assign facets to cards",
    "add_explanations": "Add explanations",
}
STAGE_TOOLS = {
    "find_sources": ["WebSearch", "WebFetch"],
    "extract_facts": ["WebFetch"],
    "draft_questions": [],
    "make_variants": [],
    "revise": [],
    "suggest_facets": [],
    "assign_facets": [],
    "add_explanations": ["WebSearch", "WebFetch"],
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

    if stage == "revise":
        n = inputs[0]
        current = card_view(n)
        return (head + one_off +
                "Task: revise the question below according to the reviewer's note in the extra instruction. Keep it factually accurate and keep the same underlying fact "
                "unless the note says otherwise. Fields: " + card_fields_spec(topic_cfg) + ".\n\n"
                f"{difficulty_guidance()}\n\n"
                f"CURRENT QUESTION:\n{json.dumps(current, indent=2, ensure_ascii=False)}\n\n"
                'Output: {"question": {' + card_key(topic_cfg) + ', "question": "", "answer": "", "choices": ["", "", "", ""], "difficulty": "", "explanation": ""}}' + JSON_TAIL)

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

    elif stage in ("draft_questions", "make_variants", "revise"):
        if stage == "draft_questions":
            items, ref_key, kind = result.get("questions", []), "fact_id", "question"
        elif stage == "make_variants":
            items, ref_key, kind = result.get("variants", []), "question_id", "variant"
        else:
            items, ref_key, kind = [dict(result.get("question") or {}, **{"__ref": inputs[0]["id"]})], "__ref", "variant"
        for item in items:
            ref = clean_str(item.get(ref_key))
            if ref not in input_ids:
                warnings.append(f"item referenced unknown id {ref!r}; skipped")
                continue
            vtype = run["params"].get("variant_type", "paraphrase") if stage == "make_variants" else ("revision" if stage == "revise" else None)
            itype = "true_false" if vtype == "true_false" else "multiple_choice"
            clean, problems = normalize_question(item, itype, topic_facets(topic_id))
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
            if stage == "revise":
                clean["card_id"] = ""  # a revision is its own card; the original keeps its id
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
                               "choices": ["Correct answer", "Wrong A", "Wrong B", "Wrong C"], "explanation": f"MOCK explanation: {f['payload'].get('context', '')}", "difficulty": f["payload"].get("difficulty_hint") or "intermediate"} for i, f in enumerate(inputs)]}
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
    if stage == "revise":
        p = inputs[0]["payload"]
        return {"question": {**face(0, p), "question": f"MOCK revised ({clean_str(run['params'].get('note'))[:40]}): {p.get('question')}",
                             "answer": p.get("answer"), "choices": list(p.get("choices") or []), "difficulty": p.get("difficulty"), "explanation": p.get("explanation", "")}}
    if stage == "add_explanations":
        return {"explanations": [{"id": n["id"], "explanation": f"MOCK explanation for: {n['payload'].get('question', '')[:50]}", "source_url": "https://example.com/mock-explanation", "note": ""} for n in inputs]}
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
                "status": "queued", "backend": "opentdb" if stage == "opentdb" else cfg["backend"], "model": cfg.get("model", ""),
                "prompt": prompt, "tools": STAGE_TOOLS.get(stage, []), "instructions": instruction_block(topic_cfg, stage),
                "input_ids": [n["id"] for n in inputs], "created_at": now_iso(), "started_at": None, "ended_at": None,
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
                if run["stage"] == "opentdb":
                    self._run_opentdb(run, topic_cfg)
                else:
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
        if run["status"] == "done" and run.get("chain") == "vet" and run["created_node_ids"]:
            self._chain_extract_facts(run)

    def _chain_extract_facts(self, parent: dict) -> None:
        topic_cfg = STORE.topic_cfg(parent["topic"])
        sources = [STORE.nodes[i] for i in parent["created_node_ids"] if i in STORE.nodes][:5]
        params = {"limit": 5, "per_item": 8, "input_ids": [n["id"] for n in sources], "note": "Vetting pass: extract as many distinct, high-quality facts as the sources support."}
        run = self.make_run("extract_facts", topic_cfg, params, sources, build_prompt("extract_facts", topic_cfg, params, sources))
        run["chain_parent"] = parent["id"]
        STORE.save_run(run)
        self.submit(run)

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

    def _run_opentdb(self, run: dict, topic_cfg: dict) -> None:
        params = run["params"]
        categories = [int(c) for c in params.get("categories", [])]
        keywords = [k.lower() for k in params.get("keywords", []) if k.strip()]
        pages = max(1, min(int(params.get("pages") or 2), 6))
        if not categories:
            raise RuntimeError("no OpenTDB categories configured for this topic")
        known = {n["payload"].get("opentdb_id") for n in STORE.nodes.values() if n["topic"] == topic_cfg["id"] and n["kind"] == "source"}
        created: list[str] = []

        def fetch(url: str) -> dict:
            with urllib.request.urlopen(url, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))

        token = fetch("https://opentdb.com/api_token.php?command=request").get("token", "")
        seen = scanned = 0
        for cat in categories:
            for _ in range(pages):
                if run.get("cancel_requested"):
                    raise Cancelled()
                time.sleep(5.3)  # OpenTDB allows one request per ~5s
                data = fetch(f"https://opentdb.com/api.php?amount=50&category={cat}&type=multiple&token={token}")
                if data.get("response_code") == 5:
                    time.sleep(6)
                    data = fetch(f"https://opentdb.com/api.php?amount=50&category={cat}&type=multiple&token={token}")
                if data.get("response_code") != 0:
                    break
                for item in data["results"]:
                    scanned += 1
                    q = html.unescape(item["question"])
                    if keywords and not any(k in q.lower() for k in keywords):
                        continue
                    ans = html.unescape(item["correct_answer"])
                    wrong = [html.unescape(w) for w in item["incorrect_answers"]]
                    oid = "otdb_" + re.sub(r"\W+", "", q.lower())[:60]
                    if oid in known:
                        continue
                    known.add(oid)
                    seen += 1
                    node = STORE.create_node(topic_cfg["id"], "source", {
                        "source_type": "opentdb", "opentdb_id": oid, "title": q[:90], "url": "https://opentdb.com/",
                        "summary": f"Q: {q}  A: {ans}  (distractors: {', '.join(wrong)})",
                        "excerpt": f"Q: {q} -> {ans}", "license": "CC BY-SA 4.0 (Open Trivia DB) - use as facts/structure, rewrite the wording",
                        "opentdb": {"category": item.get("category"), "difficulty": item.get("difficulty"), "question": q, "correct": ans, "incorrect": wrong},
                    }, [], None, "draft", {"origin": "opentdb", "run_id": run["id"], "stage": "opentdb", "backend": "opentdb", "mock": False})
                    created.append(node["id"])
        run["created_node_ids"] = created
        run["raw_output"] = f"scanned {scanned} OpenTDB questions, kept {seen} matching the keyword filter"


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------

def export_topic(topic_id: str, write: bool) -> dict:
    S = STORE
    cfg = S.topic_cfg(topic_id)
    if not cfg or cfg.get("stage", "active") != "active":
        raise ApiError(400, "only active topics can be exported - promote the idea first")
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
        "config": {k: S.config[k] for k in ("backend", "model", "concurrency", "budget_usd", "timeout_s", "game_input_types", "require_verified_explanations")},
        "topics": [S.topic_summary(t) for t in S.topics_in_stage("active")],
        "ideas": [S.topic_summary(t) for t in S.topics_in_stage("idea")],
        "archived": [S.topic_summary(t) for t in S.topics_in_stage("archived")],
        "departments": DEPARTMENTS,
        "tiers": [{"name": n, "score": sc, "anchor": a} for n, sc, a in TIERS],
        "active_runs": RUNNER.active_count(),
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


def api_delete_node(node_id: str) -> dict:
    S = STORE
    with S.lock:
        node = S.nodes.get(node_id)
        if not node:
            raise ApiError(404, "unknown node")
        if S.children_of(node_id):
            raise ApiError(400, "this node has derived children; reject it instead, or delete those first")
        del S.nodes[node_id]
        S.save_topic(node["topic"])
    return {"deleted": node_id}


def api_run(body: dict) -> dict:
    S = STORE
    stage, topic_id = body.get("stage"), body.get("topic")
    topic_cfg = S.topic_cfg(topic_id or "")
    if not topic_cfg or stage not in STAGE_LABELS:
        raise ApiError(400, "need a valid topic and stage")
    params = body.get("params") or {}
    if stage == "opentdb":
        hints = topic_cfg.get("opentdb", {})
        params = {"categories": params.get("categories") or hints.get("categories", []),
                  "keywords": params.get("keywords") if params.get("keywords") is not None else hints.get("keywords", []),
                  "pages": params.get("pages") or 2}
        prompt = (f"(no AI) Fetch up to {params['pages']} page(s) of 50 questions from each OpenTDB category {params['categories']} "
                  f"and keep those mentioning any of: {params['keywords'] or 'anything'}. Each match becomes a source node (CC BY-SA - rewrite, don't copy).")
        inputs: list[dict] = []
    else:
        if stage == "revise":
            nid = (params.get("input_ids") or [None])[0]
            if nid not in S.nodes or S.nodes[nid]["kind"] not in QUESTION_KINDS:
                raise ApiError(400, "revise needs a question or variant node")
            if not clean_str(params.get("note")):
                raise ApiError(400, "write a note describing what to change")
        if stage == "make_variants" and params.get("variant_type", "paraphrase") not in VARIANT_TYPES:
            raise ApiError(400, "unknown variant type")
        if stage == "draft_questions" and clean_str(params.get("difficulty")).lower() not in ("", "auto", *TIER_NAMES):
            raise ApiError(400, "difficulty must be auto or one of: " + ", ".join(TIER_NAMES))
        if stage == "suggest_facets" and topic_cfg.get("facets"):
            raise ApiError(400, "this topic already has facets - edit them on the topic page")
        if stage == "assign_facets" and not topic_cfg.get("facets"):
            raise ApiError(400, "give the topic facets first (type them in, or use Suggest facets)")
        inputs = select_inputs(stage, topic_id, params)
        if stage in ("extract_facts", "draft_questions", "make_variants", "revise", "assign_facets", "add_explanations") and not inputs:
            raise ApiError(400, {"extract_facts": "no sources are waiting for fact extraction", "draft_questions": "no facts are waiting for questions",
                                 "make_variants": "no approved questions are waiting for that variant type", "revise": "nothing to revise",
                                 "assign_facets": "every card in this topic already has a facet",
                                 "add_explanations": "every card in this topic already has an explanation"}[stage])
        prompt = build_prompt(stage, topic_cfg, params, inputs)
    followup = ""
    if stage == "draft_questions" and not params.get("input_ids"):
        rest = len(facts_waiting(topic_id, clean_str(params.get("under")))) - len(inputs)
        if rest > 0:
            followup = f"{rest} more fact(s) are still waiting after this batch. Run it again when this one finishes."
    if body.get("preview"):
        return {"preview": True, "followup": followup, "stage": stage, "label": STAGE_LABELS[stage], "backend": "opentdb" if stage == "opentdb" else S.config["backend"],
                "model": S.config.get("model") or "(CLI default)", "tools": STAGE_TOOLS.get(stage, []), "prompt": prompt,
                "input_ids": [n["id"] for n in inputs], "inputs": [{"id": n["id"], "kind": n["kind"], "snippet": S.snippet(n)} for n in inputs]}
    run = RUNNER.make_run(stage, topic_cfg, params, inputs, prompt)
    RUNNER.submit(run)
    return run_summary(run)


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
                                   "note": clean_str(body.get("note")), "facets": clean_facets(body.get("facets")), "created_at": now_iso(), "opentdb": {"categories": [], "keywords": []}})
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
        if "stage" in body and body["stage"] != current:
            new = body["stage"]
            allowed = {"idea": ("active", "archived"), "archived": ("idea",), "active": ()}
            if new not in allowed.get(current, ()):
                raise ApiError(400, f"a topic can't move from {current} to {new}")
            t["stage"] = new
        S.save_config()
    return S.topic_summary(t)


def api_delete_topic(topic_id: str) -> dict:
    S = STORE
    with S.lock:
        t = S.topic_cfg(topic_id)
        if not t:
            raise ApiError(404, "unknown topic")
        if t.get("stage", "active") == "active":
            raise ApiError(400, "active topics can't be deleted")
        if any(n["topic"] == topic_id for n in S.nodes.values()):
            raise ApiError(400, "this idea has sources/facts attached - archive it instead")
        S.config["topics"].remove(t)
        S.save_config()
    return {"deleted": topic_id}


def api_vet(topic_id: str, body: dict) -> dict:
    """Vetting = find sources, then (automatically, on success) extract facts from them."""
    S = STORE
    t = S.topic_cfg(topic_id)
    if not t:
        raise ApiError(404, "unknown topic")
    if t.get("stage", "active") != "idea":
        raise ApiError(400, "only ideas are vetted")
    if S.vet_summary(topic_id)["verdict"] == "vetting":
        raise ApiError(400, "a vetting run is already in progress for this idea")
    params = {"count": 5, "note": "Vetting pass: look for sources with large amounts of discrete factual material (long articles, lists, databases), from at least two independent sites."}
    prompt = build_prompt("find_sources", t, params, [])
    if body.get("preview"):
        return {"preview": True, "stage": "find_sources", "label": "Vet idea (step 1 of 2: find sources)", "backend": S.config["backend"],
                "model": S.config.get("model") or "(CLI default)", "tools": STAGE_TOOLS["find_sources"], "prompt": prompt, "input_ids": [], "inputs": [],
                "followup": "When this finishes, the studio automatically runs 'Extract facts' on the sources it found (step 2) - you'll see a second run appear. The idea is judged on how many distinct facts and independent sites come out."}
    run = RUNNER.make_run("find_sources", t, params, [], prompt)
    run["chain"] = "vet"
    S.save_run(run)
    RUNNER.submit(run)
    return run_summary(run)


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
        for key in ("backend", "model", "concurrency", "budget_usd", "timeout_s", "game_input_types", "require_verified_explanations"):
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
                    if "opentdb" in incoming:
                        t["opentdb"] = incoming["opentdb"]
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
route("DELETE", r"/api/nodes/([\w-]+)")(lambda m, b: api_delete_node(m.group(1)))
route("POST", r"/api/nodes")(lambda m, b: api_create_node(b))
route("POST", r"/api/run")(lambda m, b: api_run(b))
route("GET", r"/api/runs")(lambda m, b: {"runs": [run_summary(r) for r in sorted(STORE.runs.values(), key=lambda r: r["created_at"], reverse=True)]})
route("GET", r"/api/runs/([\w-]+)")(lambda m, b: run_summary(STORE.runs[m.group(1)], full=True) if m.group(1) in STORE.runs else (_ for _ in ()).throw(ApiError(404, "unknown run")))
route("POST", r"/api/runs/([\w-]+)/cancel")(lambda m, b: (RUNNER.cancel(m.group(1)), {"ok": True})[1])
route("POST", r"/api/export")(lambda m, b: {"results": [export_topic(t, bool(b.get("write"))) for t in ([b["topic"]] if b.get("topic") else [x["id"] for x in STORE.topics_in_stage("active")])]})
route("POST", r"/api/topics")(lambda m, b: api_create_topic(b))
route("PUT", r"/api/topics/([\w-]+)")(lambda m, b: api_update_topic(m.group(1), b))
route("DELETE", r"/api/topics/([\w-]+)")(lambda m, b: api_delete_topic(m.group(1)))
route("POST", r"/api/topics/([\w-]+)/approve")(lambda m, b: api_approve_bulk(m.group(1), b))
route("POST", r"/api/topics/([\w-]+)/vet")(lambda m, b: api_vet(m.group(1), b))
route("POST", r"/api/import-game-data")(lambda m, b: import_game_data())
route("POST", r"/api/test-backend")(lambda m, b: api_test_backend())

STATIC = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/style.css": "style.css"}
MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}


# --------------------------------------------------------------------------
# network access
# --------------------------------------------------------------------------
# Default: bound to 127.0.0.1, so only this laptop can reach it. With --lan it listens on every
# interface so a phone on the same Wi-Fi (or a VPN into it) can use it, but every request that
# doesn't come from this laptop must carry the access token. The token is a long random secret kept
# in ~/.content_studio_token (outside the repo, mode 600). The phone enters it once through the
# printed link, which sets a cookie and redirects to a clean URL. There is no HTTPS, so the token is
# visible to anyone who can sniff your network: fine on home Wi-Fi or a VPN, not on a cafe network.
# A password can be set with --set-password; /login then signs a device in with it (same cookie as the link,
# stored hashed with scrypt, wrong guesses are slowed and paused after 10 in 10 minutes).

TOKEN_FILE = Path(os.environ.get("STUDIO_TOKEN_FILE") or Path.home() / ".content_studio_token")
COOKIE_NAME = "studio_token"
LAN_MODE = False
ACCESS_TOKEN = ""
# Extra Host names accepted in LAN mode, e.g. "content.example.com" when a tunnel/proxy fronts the studio.
# Anything else is still refused, so a hostile web page can't reach the studio under a name it controls.
ALLOWED_HOSTS: set[str] = {h.strip().lower() for h in os.environ.get("STUDIO_ALLOWED_HOSTS", "").split(",") if h.strip()}


def load_access_token(rotate: bool = False) -> str:
    if TOKEN_FILE.exists() and not rotate:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if len(token) >= 20:
            return token
    token = secrets.token_urlsafe(18)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(token + "\n")
    os.chmod(TOKEN_FILE, 0o600)
    return token


def token_ok(candidate: str) -> bool:
    return bool(candidate) and hmac.compare_digest(candidate.encode("utf-8"), ACCESS_TOKEN.encode("utf-8"))


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


PASSWORD_FILE = Path(os.environ.get("STUDIO_PASSWORD_FILE") or Path.home() / ".content_studio_password")
PASSWORD_MIN = 8
LOGIN_WINDOW_S, LOGIN_MAX_FAILS = 600, 10   # after 10 wrong passwords in 10 minutes, logins pause (the ?token= link still works)
_login_fails: list[float] = []
_login_lock = threading.Lock()


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def password_ok(candidate: str) -> bool:
    try:
        _, salt_hex, digest_hex = PASSWORD_FILE.read_text(encoding="utf-8").strip().split("$")
        expected = bytes.fromhex(digest_hex)
        got = hashlib.scrypt(candidate.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=2 ** 14, r=8, p=1, dklen=32)
    except (OSError, ValueError):
        return False
    return hmac.compare_digest(got, expected)


def set_password_interactively() -> None:
    first = getpass.getpass("New studio password: ")
    if len(first) < PASSWORD_MIN:
        sys.exit(f"Use at least {PASSWORD_MIN} characters.")
    if getpass.getpass("Again: ") != first:
        sys.exit("Those didn't match; nothing changed.")
    fd = os.open(PASSWORD_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(hash_password(first) + "\n")
    os.chmod(PASSWORD_FILE, 0o600)
    print(f"Password saved to {PASSWORD_FILE}. Phones sign in at /login (already signed-in devices stay signed in).")


def login_locked() -> bool:
    with _login_lock:
        cutoff = time.time() - LOGIN_WINDOW_S
        _login_fails[:] = [t for t in _login_fails if t > cutoff]
        return len(_login_fails) >= LOGIN_MAX_FAILS


def login_page(message: str = "") -> bytes:
    note = f"<p class=err>{html.escape(message)}</p>" if message else ""
    if not PASSWORD_FILE.exists():
        note = "<p class=err>No password is set on this studio yet. Open the access link instead, or set one with --set-password.</p>"
    return (
        "<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        "<meta name=color-scheme content='dark light'><title>Content Studio - sign in</title>"
        "<style>:root{--bg:#14151a;--card:#1d1f27;--text:#e6e6ea;--muted:#9a9ca8;--line:#2e313c;--accent:#e0a458;--err:#ff8a80}"
        "@media (prefers-color-scheme:light){:root{--bg:#f4f1ea;--card:#fff;--text:#22232a;--muted:#6b6d78;--line:#d9d5ca;--accent:#9a5b00;--err:#b3261e}}"
        "*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,sans-serif;padding:16px}"
        "form{width:100%;max-width:340px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:26px}"
        "h1{font-size:20px;margin:0 0 4px}p{margin:0 0 16px;color:var(--muted);font-size:14px}p.err{color:var(--err)}"
        "input{width:100%;font:inherit;padding:11px 12px;border-radius:9px;border:1px solid var(--line);background:var(--bg);color:var(--text)}"
        "button{width:100%;margin-top:14px;font:inherit;font-weight:600;padding:11px;border:0;border-radius:9px;background:var(--accent);color:#1a1205}</style>"
        "<form method=post action=/login><h1>Content Studio</h1><p>Sign in to continue.</p>" + note +
        "<input type=text name=username value=studio autocomplete=username hidden>"
        "<input type=password name=password autocomplete=current-password placeholder=Password autofocus required>"
        "<button type=submit>Sign in</button></form></html>"
    ).encode("utf-8")


UNAUTHORIZED_PAGE = (b"<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
                     b"<body style='font:16px system-ui;background:#14151a;color:#e6e6ea;padding:28px;line-height:1.5'>"
                     b"<h2>Content Studio</h2><p><a href=/login style=color:#e0a458>Sign in</a>, or open the full access link "
                     b"(it ends in <code>?token=...</code>).</p></body>")


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

    def _from_this_laptop(self) -> bool:
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def _cookie_token(self) -> str:
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE_NAME:
                return value
        return ""

    def _session_cookie(self) -> str:
        https = "https" in (self.headers.get("X-Forwarded-Proto") or "").lower() or "https" in (self.headers.get("CF-Visitor") or "").lower()
        return f"{COOKIE_NAME}={ACCESS_TOKEN}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000" + ("; Secure" if https else "")

    def _redirect(self, where: str, cookie: str = "") -> None:
        self.send_response(302)
        self.send_header("Location", where)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _login(self, method: str) -> None:
        signed_in = not LAN_MODE or self._from_this_laptop() or token_ok(self._cookie_token())
        if method == "GET":
            return self._redirect("/") if signed_in else self._send(200, login_page(), "text/html; charset=utf-8")
        if "application/x-www-form-urlencoded" not in (self.headers.get("Content-Type") or ""):
            return self._json(415, {"error": "form post only"})
        raw = self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 4096)).decode("utf-8", "replace")
        password = urllib.parse.parse_qs(raw).get("password", [""])[0]
        if login_locked():
            return self._send(429, login_page("Too many wrong passwords. Try again in a few minutes."), "text/html; charset=utf-8")
        if password_ok(password):
            return self._redirect("/", self._session_cookie())
        with _login_lock:
            _login_fails.append(time.time())
        time.sleep(1.0)  # slow down guessing
        self._send(401, login_page("That password isn't right."), "text/html; charset=utf-8")

    def _logout(self) -> None:
        self._redirect("/login", f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")

    def _authorised(self, parsed) -> bool:
        """True if the request may proceed; otherwise a response has already been sent."""
        if not LAN_MODE or self._from_this_laptop():
            return True
        supplied = urllib.parse.parse_qs(parsed.query).get("token", [""])[0]
        if supplied:
            if token_ok(supplied):  # first visit from a new device: set the cookie, then drop the token from the URL
                self._redirect("/", self._session_cookie())
                return False
            time.sleep(0.5)  # slow down guessing
        elif token_ok(self._cookie_token()):
            return True
        if parsed.path.startswith("/api/"):
            self._json(401, {"error": "access token required"})
        elif parsed.path == "/":
            self._redirect("/login")
        else:
            self._send(401, UNAUTHORIZED_PAGE, "text/html; charset=utf-8")
        return False

    def _handle(self, method: str) -> None:
        if not host_allowed(self.headers.get("Host") or ""):
            return self._json(403, {"error": "unexpected Host header"})
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/login" and method in ("GET", "POST"):
            return self._login(method)
        if parsed.path == "/logout" and method == "POST":
            return self._logout()
        if not self._authorised(parsed):
            return
        path = parsed.path
        if method == "GET" and path in STATIC:
            file = UI_DIR / STATIC[path]
            return self._send(200, file.read_bytes(), MIME[file.suffix]) if file.exists() else self._json(404, {"error": "ui file missing"})
        if not path.startswith("/api/"):
            return self._json(404, {"error": "not found"})
        body: dict = {}
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
    global STORE, RUNNER, LAN_MODE, ACCESS_TOKEN
    sys.stdout.reconfigure(line_buffering=True)  # show the banner and links promptly even when output goes to a log file
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true", help="don't open a browser tab")
    parser.add_argument("--lan", action="store_true", help="also listen on your local network (phone access); other devices need the access token")
    parser.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                        help="also accept this Host name (repeatable), e.g. a public hostname behind a tunnel; keeps the token out of the log banner")
    parser.add_argument("--rotate-token", action="store_true", help="generate a new access token (signs out every phone), then continue")
    parser.add_argument("--set-password", action="store_true", help="choose the password for the /login page (stored hashed in the password file), then exit")
    parser.add_argument("--import-game-data", action="store_true", help="import existing data/*/curriculum.json cards, then exit")
    args = parser.parse_args()
    if args.set_password:
        return set_password_interactively()

    STORE = Store()
    RUNNER = Runner()
    if args.import_game_data or not any(STORE.nodes.values()):
        print("imported", import_game_data()["imported"], "existing game cards as approved questions")
        if args.import_game_data:
            return
    LAN_MODE = args.lan
    ALLOWED_HOSTS.update(h.strip().lower() for h in args.allow_host if h.strip())
    if LAN_MODE:
        ACCESS_TOKEN = load_access_token(args.rotate_token)
    server = ThreadingHTTPServer(("0.0.0.0" if LAN_MODE else "127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Content Studio running at {url}  (Ctrl+C to stop)")
    if LAN_MODE and ALLOWED_HOSTS:
        print(f"\nServer mode: answering to {', '.join(sorted(ALLOWED_HOSTS))}. Other devices need the access token stored in {TOKEN_FILE} (not printed here).\n")
    elif LAN_MODE:
        print("\nLAN mode ON. On your phone (same Wi-Fi, or VPN) open one of these once; it remembers you afterwards:")
        names = lan_addresses() + ([socket.gethostname()] if socket.gethostname().endswith(".local") else [])
        for name in names:
            print(f"  http://{name}:{args.port}/?token={ACCESS_TOKEN}")
        print(f"The token lives in {TOKEN_FILE} (run with --rotate-token to replace it). macOS may ask whether Python can accept incoming connections: choose Allow.\n")
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
