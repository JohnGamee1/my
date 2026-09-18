#!/usr/bin/env python3
"""
hidden_self_research.py
-----------------------
Research finder for The Hidden Self YouTube channel.

Give it a topic; it searches free scholarly + reference sources, ranks the
hits by relevance to the channel's core themes (Jung / shadow work,
Machiavellian strategy, stoicism, dark psychology, self-mastery), and
writes an organized markdown research brief you can hand straight to
scriptwriting.

Sources (all free, no API key required):
  - OpenAlex          -> peer-reviewed papers, open-access PDFs when available
  - Semantic Scholar  -> abstracts + citation counts
  - Wikipedia         -> canonical framing and reference lists
  - Project Gutenberg -> full text of the classics (Jung, Nietzsche, Marcus
                         Aurelius, Seneca, Machiavelli, Schopenhauer, ...)
  - Internet Archive  -> older books, lectures, essays

Usage:
    python hidden_self_research.py "the shadow archetype in modern relationships"
    python hidden_self_research.py "machiavellian strategy at work" --max 8 --out research/
    python hidden_self_research.py "stoic detachment" --years 15 --open

Only the Python standard library is required.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import html
import json
import os
import re
import sys
import textwrap
import time
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from typing import Iterable


# --------------------------------------------------------------------------- #
# Channel-specific vocabulary. This is what makes the tool actually good      #
# for The Hidden Self instead of a generic paper-search wrapper.              #
# --------------------------------------------------------------------------- #

CHANNEL_THEMES: dict[str, tuple[str, ...]] = {
    "jungian": (
        "carl jung", "jungian", "analytical psychology", "shadow",
        "archetype", "individuation", "collective unconscious", "anima",
        "animus", "persona", "complex", "projection", "active imagination",
    ),
    "shadow_work": (
        "shadow work", "shadow self", "repression", "denial",
        "unconscious", "dark side", "disowned self", "inner child",
    ),
    "machiavellian": (
        "machiavelli", "machiavellian", "the prince", "power",
        "strategy", "manipulation", "48 laws", "robert greene", "influence",
        "48 laws of power", "the art of seduction",
    ),
    "dark_psychology": (
        "dark triad", "narcissism", "psychopathy", "sociopathy",
        "manipulation", "gaslighting", "coercive control", "dark tetrad",
        "sadism", "machiavellianism",
    ),
    "stoicism": (
        "stoicism", "stoic", "marcus aurelius", "seneca", "epictetus",
        "meditations", "amor fati", "memento mori", "dichotomy of control",
        "apatheia", "logos",
    ),
    "self_mastery": (
        "self mastery", "self-discipline", "willpower", "self control",
        "detachment", "solitude", "silence", "high value", "sigma",
        "monk mode", "asceticism",
    ),
    "philosophy": (
        "nietzsche", "schopenhauer", "kierkegaard", "existentialism",
        "will to power", "eternal recurrence", "ubermensch", "the ego",
    ),
    "social_dynamics": (
        "status", "hierarchy", "social proof", "reciprocity",
        "in-group", "out-group", "authority", "conformity", "cialdini",
    ),
}

# Adjacent seed queries appended when the user's topic clearly sits in a
# theme cluster. This makes retrieval broader without asking the user to
# type five queries.
THEME_SEEDS: dict[str, tuple[str, ...]] = {
    "jungian": ("Jung shadow projection", "individuation process"),
    "shadow_work": ("shadow integration psychology", "repression and projection"),
    "machiavellian": ("Machiavelli The Prince analysis", "48 Laws of Power research"),
    "dark_psychology": ("Dark Triad personality research", "narcissistic manipulation tactics"),
    "stoicism": ("Stoicism modern psychology", "dichotomy of control research"),
    "self_mastery": ("self-control willpower ego depletion", "solitude psychological benefits"),
    "philosophy": ("Nietzsche will to power", "existential meaning research"),
    "social_dynamics": ("Cialdini influence principles", "status hierarchy psychology"),
}

# Foundational works we always want to surface for the relevant theme,
# even if a search engine ranks them low. Titles are as they appear on
# Project Gutenberg / Internet Archive.
CANON: dict[str, tuple[dict[str, str], ...]] = {
    "jungian": (
        {"title": "Psychology of the Unconscious", "author": "C. G. Jung"},
        {"title": "The Psychology of the Unconscious Processes", "author": "C. G. Jung"},
    ),
    "machiavellian": (
        {"title": "The Prince", "author": "Niccolò Machiavelli"},
        {"title": "Discourses on Livy", "author": "Niccolò Machiavelli"},
    ),
    "stoicism": (
        {"title": "Meditations", "author": "Marcus Aurelius"},
        {"title": "Letters from a Stoic", "author": "Seneca"},
        {"title": "Enchiridion", "author": "Epictetus"},
        {"title": "Discourses", "author": "Epictetus"},
    ),
    "philosophy": (
        {"title": "Thus Spake Zarathustra", "author": "Friedrich Nietzsche"},
        {"title": "Beyond Good and Evil", "author": "Friedrich Nietzsche"},
        {"title": "The World as Will and Representation", "author": "Arthur Schopenhauer"},
        {"title": "The Wisdom of Life", "author": "Arthur Schopenhauer"},
    ),
}


# --------------------------------------------------------------------------- #
# HTTP helpers                                                                #
# --------------------------------------------------------------------------- #

USER_AGENT = (
    "HiddenSelfResearchBot/1.0 "
    "(+contact via YouTube channel The Hidden Self)"
)


def _get_json(url: str, *, timeout: float = 20.0) -> dict | list | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except Exception as exc:  # noqa: BLE001 - network failures are expected
        print(f"[warn] {url[:80]}... -> {exc}", file=sys.stderr)
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _q(s: str) -> str:
    return urllib.parse.quote_plus(s)


# --------------------------------------------------------------------------- #
# Result model                                                                #
# --------------------------------------------------------------------------- #

@dataclass
class ResearchDoc:
    source: str                     # "OpenAlex" / "Semantic Scholar" / "Wikipedia" / ...
    kind: str                       # "paper" / "book" / "article" / "encyclopedia"
    title: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    url: str = ""
    pdf_url: str = ""               # direct PDF if we found one
    abstract: str = ""
    venue: str = ""                 # journal / publisher / site
    citations: int | None = None
    score: float = 0.0              # relevance score (filled by ranker)
    themes: list[str] = field(default_factory=list)  # matched theme names

    def bullets(self) -> list[str]:
        bits: list[str] = []
        if self.authors:
            bits.append(", ".join(self.authors[:4]) + (" et al." if len(self.authors) > 4 else ""))
        if self.year:
            bits.append(str(self.year))
        if self.venue:
            bits.append(self.venue)
        if self.citations is not None:
            bits.append(f"{self.citations} citations")
        return bits


# --------------------------------------------------------------------------- #
# Source: OpenAlex                                                            #
# --------------------------------------------------------------------------- #

def search_openalex(query: str, *, per_query: int, min_year: int | None) -> list[ResearchDoc]:
    filters = ["type:article|book|book-chapter"]
    if min_year:
        filters.append(f"from_publication_date:{min_year}-01-01")
    url = (
        "https://api.openalex.org/works"
        f"?search={_q(query)}"
        f"&per-page={per_query}"
        f"&filter={_q(','.join(filters))}"
        "&mailto=hiddenself.research@example.com"
    )
    payload = _get_json(url)
    if not payload or "results" not in payload:
        return []
    docs: list[ResearchDoc] = []
    for w in payload["results"]:
        title = (w.get("title") or "").strip()
        if not title:
            continue
        authors = [
            (a.get("author") or {}).get("display_name", "")
            for a in (w.get("authorships") or [])
        ]
        authors = [a for a in authors if a]

        # OpenAlex ships abstracts as an "inverted index" -- rebuild them.
        abstract = _openalex_abstract(w.get("abstract_inverted_index") or {})

        oa = w.get("open_access") or {}
        pdf = oa.get("oa_url") or ""

        venue = ""
        loc = (w.get("primary_location") or {}).get("source") or {}
        if loc:
            venue = loc.get("display_name") or ""

        docs.append(
            ResearchDoc(
                source="OpenAlex",
                kind="paper",
                title=title,
                authors=authors,
                year=w.get("publication_year"),
                url=w.get("doi") and f"https://doi.org/{w['doi'].replace('https://doi.org/', '')}" or w.get("id", ""),
                pdf_url=pdf,
                abstract=abstract,
                venue=venue,
                citations=w.get("cited_by_count"),
            )
        )
    return docs


def _openalex_abstract(inv: dict) -> str:
    if not inv:
        return ""
    positions: list[tuple[int, str]] = []
    for word, idxs in inv.items():
        for i in idxs:
            positions.append((i, word))
    positions.sort()
    return " ".join(w for _, w in positions)


# --------------------------------------------------------------------------- #
# Source: Semantic Scholar                                                    #
# --------------------------------------------------------------------------- #

def search_semantic_scholar(query: str, *, per_query: int, min_year: int | None) -> list[ResearchDoc]:
    fields = "title,abstract,year,authors,venue,citationCount,openAccessPdf,externalIds,url"
    year_filter = f"&year={min_year}-" if min_year else ""
    url = (
        "https://api.semanticscholar.org/graph/v1/paper/search"
        f"?query={_q(query)}&limit={per_query}{year_filter}&fields={fields}"
    )
    payload = _get_json(url)
    if not payload or "data" not in payload:
        return []
    docs: list[ResearchDoc] = []
    for p in payload["data"]:
        title = (p.get("title") or "").strip()
        if not title:
            continue
        pdf = (p.get("openAccessPdf") or {}).get("url") or ""
        docs.append(
            ResearchDoc(
                source="Semantic Scholar",
                kind="paper",
                title=title,
                authors=[a.get("name", "") for a in (p.get("authors") or []) if a.get("name")],
                year=p.get("year"),
                url=p.get("url", ""),
                pdf_url=pdf,
                abstract=p.get("abstract") or "",
                venue=p.get("venue") or "",
                citations=p.get("citationCount"),
            )
        )
    return docs


# --------------------------------------------------------------------------- #
# Source: Wikipedia                                                           #
# --------------------------------------------------------------------------- #

def search_wikipedia(query: str, *, per_query: int) -> list[ResearchDoc]:
    search_url = (
        "https://en.wikipedia.org/w/api.php"
        "?action=query&list=search&format=json&utf8=1"
        f"&srsearch={_q(query)}&srlimit={per_query}"
    )
    payload = _get_json(search_url)
    if not payload:
        return []
    hits = ((payload.get("query") or {}).get("search") or [])
    docs: list[ResearchDoc] = []
    for h in hits:
        title = h.get("title", "")
        if not title:
            continue
        summary_url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{_q(title.replace(' ', '_'))}"
        summary = _get_json(summary_url) or {}
        extract = _strip_html(summary.get("extract") or _strip_html(h.get("snippet", "")))
        docs.append(
            ResearchDoc(
                source="Wikipedia",
                kind="encyclopedia",
                title=title,
                url=(summary.get("content_urls") or {}).get("desktop", {}).get("page")
                or f"https://en.wikipedia.org/wiki/{_q(title.replace(' ', '_'))}",
                abstract=extract,
                venue="Wikipedia",
            )
        )
    return docs


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", html.unescape(s or "")).strip()


# --------------------------------------------------------------------------- #
# Source: Project Gutenberg (via Gutendex)                                    #
# --------------------------------------------------------------------------- #

def search_gutenberg(query: str, *, per_query: int) -> list[ResearchDoc]:
    url = f"https://gutendex.com/books/?search={_q(query)}&languages=en"
    payload = _get_json(url)
    if not payload or "results" not in payload:
        return []
    docs: list[ResearchDoc] = []
    for b in payload["results"][:per_query]:
        title = b.get("title", "").strip()
        if not title:
            continue
        authors = [a.get("name", "") for a in b.get("authors", []) if a.get("name")]
        formats = b.get("formats", {}) or {}
        text_url = (
            formats.get("text/html; charset=utf-8")
            or formats.get("text/html")
            or formats.get("text/plain; charset=utf-8")
            or formats.get("text/plain")
            or f"https://www.gutenberg.org/ebooks/{b.get('id')}"
        )
        docs.append(
            ResearchDoc(
                source="Project Gutenberg",
                kind="book",
                title=title,
                authors=authors,
                url=text_url,
                pdf_url=formats.get("application/pdf", ""),
                abstract=", ".join(b.get("subjects") or [])[:400],
                venue="Project Gutenberg",
            )
        )
    return docs


# --------------------------------------------------------------------------- #
# Source: Internet Archive                                                    #
# --------------------------------------------------------------------------- #

def search_archive(query: str, *, per_query: int) -> list[ResearchDoc]:
    q = f'({query}) AND mediatype:(texts)'
    url = (
        "https://archive.org/advancedsearch.php"
        f"?q={_q(q)}&fl[]=identifier&fl[]=title&fl[]=creator&fl[]=year&fl[]=description"
        f"&sort[]=downloads+desc&rows={per_query}&page=1&output=json"
    )
    payload = _get_json(url)
    if not payload:
        return []
    docs: list[ResearchDoc] = []
    for d in (payload.get("response") or {}).get("docs", []):
        title = d.get("title", "")
        if isinstance(title, list):
            title = title[0] if title else ""
        if not title:
            continue
        creators = d.get("creator") or []
        if isinstance(creators, str):
            creators = [creators]
        desc = d.get("description") or ""
        if isinstance(desc, list):
            desc = " ".join(desc)
        year = None
        y = d.get("year")
        if isinstance(y, list) and y:
            y = y[0]
        if isinstance(y, str) and y.isdigit():
            year = int(y)
        elif isinstance(y, int):
            year = y
        docs.append(
            ResearchDoc(
                source="Internet Archive",
                kind="book",
                title=title,
                authors=list(creators),
                year=year,
                url=f"https://archive.org/details/{d.get('identifier')}",
                abstract=_strip_html(desc)[:600],
                venue="Internet Archive",
            )
        )
    return docs


# --------------------------------------------------------------------------- #
# Query expansion + ranking                                                   #
# --------------------------------------------------------------------------- #

def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").lower()
    return re.sub(r"[^a-z0-9\s\-]+", " ", s)


def detect_themes(topic: str) -> list[str]:
    t = normalize(topic)
    hits: list[str] = []
    for theme, words in CHANNEL_THEMES.items():
        if any(w in t for w in words):
            hits.append(theme)
    # If the user's topic is generic ("power", "envy", "loneliness"),
    # default to Jungian + dark-psychology framing since that is the
    # channel's centre of gravity.
    if not hits:
        hits = ["jungian", "dark_psychology"]
    return hits


def build_queries(topic: str, themes: Iterable[str]) -> list[str]:
    topic = topic.strip()
    queries: list[str] = [topic]
    # Angled queries per detected theme.
    angle = {
        "jungian": f"{topic} Jungian psychology shadow",
        "shadow_work": f"{topic} shadow work integration",
        "machiavellian": f"{topic} power strategy Machiavellian",
        "dark_psychology": f"{topic} dark triad manipulation",
        "stoicism": f"{topic} stoicism virtue",
        "self_mastery": f"{topic} self-discipline detachment",
        "philosophy": f"{topic} existential philosophy",
        "social_dynamics": f"{topic} social influence status hierarchy",
    }
    for theme in themes:
        if theme in angle:
            queries.append(angle[theme])
        for seed in THEME_SEEDS.get(theme, ()):
            queries.append(f"{topic} {seed}")
    # De-duplicate preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for q in queries:
        key = normalize(q)
        if key not in seen:
            seen.add(key)
            ordered.append(q)
    return ordered


def score_doc(doc: ResearchDoc, topic_tokens: set[str], themes: list[str]) -> None:
    hay = normalize(f"{doc.title} {doc.abstract} {doc.venue} {' '.join(doc.authors)}")
    tokens = set(re.findall(r"[a-z0-9\-]{3,}", hay))

    # Topic overlap.
    overlap = len(topic_tokens & tokens)
    score = overlap * 2.0

    # Theme reinforcement — the payoff for being on-channel.
    matched_themes: list[str] = []
    for theme in themes:
        theme_hits = sum(1 for w in CHANNEL_THEMES[theme] if w in hay)
        if theme_hits:
            matched_themes.append(theme)
            score += theme_hits * 1.5

    # Also credit theme matches outside the user's inferred cluster; it
    # is often useful for a shadow-work topic to surface a stoic paper.
    for theme, words in CHANNEL_THEMES.items():
        if theme in themes:
            continue
        cross = sum(1 for w in words if w in hay)
        if cross:
            score += cross * 0.4
            matched_themes.append(theme)

    # Prefer open-access PDFs and reputable venues -- they save you a
    # trip to Sci-Hub and are more likely to be usable.
    if doc.pdf_url:
        score += 2.0
    if doc.citations:
        score += min(doc.citations, 500) / 100.0  # cap so a 20k-citation
                                                  # monograph doesn't
                                                  # crowd everything out
    if doc.source == "Wikipedia":
        score += 0.5  # cheap orientation, worth keeping near the top
    if doc.source == "Project Gutenberg":
        score += 1.5  # channel loves primary classical texts
    if doc.year and doc.year >= dt.date.today().year - 10:
        score += 0.5

    # Down-weight obvious dross.
    lowered = normalize(doc.title)
    for bad in ("erratum", "correction", "retraction notice", "editorial", "book review"):
        if bad in lowered:
            score -= 3.0

    doc.score = round(score, 2)
    # Preserve order + dedupe.
    doc.themes = list(dict.fromkeys(matched_themes))


def dedupe(docs: list[ResearchDoc]) -> list[ResearchDoc]:
    seen: dict[str, ResearchDoc] = {}
    for d in docs:
        key = normalize(d.title)[:120]
        if not key:
            continue
        existing = seen.get(key)
        if existing is None or d.score > existing.score:
            seen[key] = d
    return list(seen.values())


# --------------------------------------------------------------------------- #
# Canon injection                                                             #
# --------------------------------------------------------------------------- #

def inject_canon(themes: list[str]) -> list[ResearchDoc]:
    """Return foundational works so a search miss never hides them."""
    extras: list[ResearchDoc] = []
    for theme in themes:
        for entry in CANON.get(theme, ()):
            gut = search_gutenberg(f'{entry["title"]} {entry["author"]}', per_query=2)
            if gut:
                # Take the best-matching title.
                best = min(
                    gut,
                    key=lambda x: abs(len(normalize(x.title)) - len(normalize(entry["title"]))),
                )
                extras.append(best)
    return extras


# --------------------------------------------------------------------------- #
# Report writer                                                               #
# --------------------------------------------------------------------------- #

REPORT_PREFACE = """\
> Research brief for **The Hidden Self**.
> Use this as raw material for scripts on dark psychology, Jungian shadow
> work, Machiavellian strategy, and stoic self-mastery. Verify quotes
> against primary sources before recording.
"""


def slugify(topic: str) -> str:
    s = normalize(topic)
    s = re.sub(r"\s+", "-", s).strip("-")
    return s[:80] or "topic"


def render_report(topic: str, themes: list[str], docs: list[ResearchDoc]) -> str:
    lines: list[str] = []
    lines.append(f"# Research: {topic}")
    lines.append("")
    lines.append(f"_Generated {dt.datetime.now().strftime('%Y-%m-%d %H:%M')} · "
                 f"themes detected: {', '.join(themes) or '—'}_")
    lines.append("")
    lines.append(REPORT_PREFACE)
    lines.append("")

    # Group by source for scanability.
    order = ["Project Gutenberg", "Wikipedia", "OpenAlex", "Semantic Scholar", "Internet Archive"]
    grouped: dict[str, list[ResearchDoc]] = {k: [] for k in order}
    for d in docs:
        grouped.setdefault(d.source, []).append(d)

    lines.append("## Quick angles for the script")
    lines.append("")
    lines.append(_angles_paragraph(topic, themes))
    lines.append("")

    lines.append("## Top-ranked sources")
    lines.append("")
    for i, d in enumerate(docs[:15], 1):
        lines.append(f"{i}. **{d.title}** — _{d.source}_ · score {d.score}")
        meta = " · ".join(d.bullets())
        if meta:
            lines.append(f"   - {meta}")
        if d.themes:
            lines.append(f"   - themes: {', '.join(d.themes)}")
        if d.url:
            lines.append(f"   - link: {d.url}")
        if d.pdf_url and d.pdf_url != d.url:
            lines.append(f"   - PDF: {d.pdf_url}")
        if d.abstract:
            snippet = textwrap.shorten(d.abstract, width=500, placeholder=" …")
            lines.append(f"   - {snippet}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## Full results by source")
    lines.append("")
    for source in order:
        bucket = grouped.get(source) or []
        if not bucket:
            continue
        lines.append(f"### {source}")
        lines.append("")
        for d in bucket:
            authors = ", ".join(d.authors[:3])
            year = f", {d.year}" if d.year else ""
            lines.append(f"- [{d.title}]({d.url})"
                         f"{' — ' + authors if authors else ''}{year}"
                         f"{' · ' + str(d.citations) + ' cites' if d.citations else ''}")
            if d.pdf_url and d.pdf_url != d.url:
                lines.append(f"  - PDF: {d.pdf_url}")
            if d.abstract:
                snippet = textwrap.shorten(d.abstract, width=280, placeholder=" …")
                lines.append(f"  - {snippet}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _angles_paragraph(topic: str, themes: list[str]) -> str:
    theme_line = {
        "jungian": f"- Frame **{topic}** through Jung's shadow: what part of this the viewer refuses to see in themselves.",
        "shadow_work": f"- What does integrating **{topic}** look like versus repressing it? Concrete integration moves.",
        "machiavellian": f"- Where does **{topic}** show up in power dynamics? Cite The Prince / 48 Laws where it fits.",
        "dark_psychology": f"- How do dark-triad individuals weaponize **{topic}**? What are the tells and the counter-moves?",
        "stoicism": f"- What is inside your control regarding **{topic}** and what isn't? Marcus / Epictetus quote candidates.",
        "self_mastery": f"- Rituals, disciplines, and detachment practices tied to **{topic}**.",
        "philosophy": f"- Nietzschean/existential reading of **{topic}** — self-overcoming vs. resentment.",
        "social_dynamics": f"- Status, hierarchy, and social-proof mechanics attached to **{topic}**.",
    }
    picks = [theme_line[t] for t in themes if t in theme_line]
    if not picks:
        picks = [
            f"- Jungian: what shadow does **{topic}** conceal?",
            f"- Stoic: where does control end for **{topic}**?",
            f"- Machiavellian: how is **{topic}** used as a lever of power?",
        ]
    return "\n".join(picks)


# --------------------------------------------------------------------------- #
# Orchestration                                                               #
# --------------------------------------------------------------------------- #

def gather(topic: str, *, max_results: int, min_year: int | None, per_query: int) -> tuple[list[str], list[ResearchDoc]]:
    themes = detect_themes(topic)
    queries = build_queries(topic, themes)

    fetchers: list[tuple[str, callable]] = []
    for q in queries:
        fetchers.append((f"OpenAlex :: {q}",        lambda q=q: search_openalex(q, per_query=per_query, min_year=min_year)))
        fetchers.append((f"SemScholar :: {q}",      lambda q=q: search_semantic_scholar(q, per_query=per_query, min_year=min_year)))
        fetchers.append((f"Wikipedia :: {q}",       lambda q=q: search_wikipedia(q, per_query=max(3, per_query // 2))))
        fetchers.append((f"Gutenberg :: {q}",       lambda q=q: search_gutenberg(q, per_query=max(3, per_query // 2))))
        fetchers.append((f"Archive.org :: {q}",     lambda q=q: search_archive(q, per_query=max(3, per_query // 2))))

    all_docs: list[ResearchDoc] = []
    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        future_to_label = {pool.submit(fn): label for label, fn in fetchers}
        for fut in cf.as_completed(future_to_label):
            label = future_to_label[fut]
            try:
                results = fut.result() or []
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] {label}: {exc}", file=sys.stderr)
                continue
            print(f"  · {label}: {len(results)} hits", file=sys.stderr)
            all_docs.extend(results)

    all_docs.extend(inject_canon(themes))

    topic_tokens = set(re.findall(r"[a-z0-9\-]{3,}", normalize(topic)))
    for d in all_docs:
        score_doc(d, topic_tokens, themes)

    unique = dedupe(all_docs)
    unique.sort(key=lambda d: d.score, reverse=True)
    return themes, unique[:max_results]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Find research documents for The Hidden Self scripts.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("topic", nargs="+", help="Video topic, e.g. 'shadow projection in relationships'")
    ap.add_argument("--max", type=int, default=40, help="Max results to keep after ranking (default 40)")
    ap.add_argument("--per-query", type=int, default=8, help="Results per query per source (default 8)")
    ap.add_argument("--years", type=int, default=0, help="Restrict papers to the last N years (0 = no filter)")
    ap.add_argument("--out", default="research", help="Output directory for the markdown brief (default ./research)")
    ap.add_argument("--json", action="store_true", help="Also write raw results as JSON")
    ap.add_argument("--open", action="store_true", help="Open the brief in your default viewer when done")
    args = ap.parse_args(argv)

    topic = " ".join(args.topic).strip()
    if not topic:
        ap.error("empty topic")

    min_year = dt.date.today().year - args.years if args.years > 0 else None
    print(f"Researching: {topic}", file=sys.stderr)
    t0 = time.time()

    themes, docs = gather(topic, max_results=args.max, min_year=min_year, per_query=args.per_query)

    os.makedirs(args.out, exist_ok=True)
    stem = f"{dt.datetime.now().strftime('%Y%m%d-%H%M')}-{slugify(topic)}"
    md_path = os.path.join(args.out, f"{stem}.md")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(render_report(topic, themes, docs))

    if args.json:
        json_path = os.path.join(args.out, f"{stem}.json")
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "topic": topic,
                    "themes": themes,
                    "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
                    "docs": [asdict(d) for d in docs],
                },
                fh,
                indent=2,
                ensure_ascii=False,
            )

    print(
        f"Done in {time.time() - t0:0.1f}s · {len(docs)} results · themes: {', '.join(themes) or '—'}",
        file=sys.stderr,
    )
    print(md_path)

    if args.open:
        _open_file(md_path)

    return 0


def _open_file(path: str) -> None:
    try:
        if sys.platform.startswith("darwin"):
            os.system(f'open "{path}"')
        elif os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            os.system(f'xdg-open "{path}" >/dev/null 2>&1 &')
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    raise SystemExit(main())
