"""Parse full-text XML into sections, and map their headings onto a small closed set.

Section structure varies between journals and between schemas, so nothing here navigates
by a fixed path. Every lookup is by `local-name()`, which ignores namespaces and survives
the difference between a JATS `sec` and an Elsevier `ce:section` — Europe PMC serves the
former, but a parser that only handles one shape drops papers silently.

Why bother with sections at all: Jev's jaggedness notes say accuracy falls as the state
fills with content unrelated to the decision. A 40-page paper sent whole is that failure
mode exactly. Sections are what let each criterion be asked against the two or three pages
that bear on it.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from lxml import etree
from pydantic import BaseModel, Field

from ..protocol import Section

#: Heading patterns, checked in order — the first match wins, so put the specific
#: (participants, measures) ahead of the general (methods).
_CANONICAL_PATTERNS: list[tuple[Section, re.Pattern[str]]] = [
    (Section.ABSTRACT, re.compile(r"\babstract|summary\b")),
    (Section.PARTICIPANTS, re.compile(
        r"\bparticipant|subject|sample|population|recruit|eligibilit|inclusion criteria|"
        r"study setting|demographic"
    )),
    (Section.INTERVENTION, re.compile(
        r"\bintervention|treatment|procedure|protocol|programme|program|training|"
        r"game|materials|apparatus|stimuli|session"
    )),
    (Section.MEASURES, re.compile(
        r"\bmeasure|instrument|outcome|assessment|questionnaire|scale|test batter|variable"
    )),
    (Section.ANALYSIS, re.compile(
        r"\banalys|analyz|statistic|data processing|model(l)?ing|power calculation"
    )),
    (Section.LIMITATIONS, re.compile(r"\blimitation|threats to validity|caveat")),
    (Section.METHODS, re.compile(r"\bmethod|methodolog|design|study design|approach")),
    (Section.RESULTS, re.compile(r"\bresult|finding|outcome of the")),
    (Section.DISCUSSION, re.compile(r"\bdiscussion|interpretation|implication|conclusion")),
    (Section.INTRODUCTION, re.compile(r"\bintroduction|background|related work|rationale|aim")),
    (Section.OTHER, re.compile(
        r"\breference|bibliograph|acknowledg|funding|conflict|ethic|appendix|"
        r"supplementar|author contribution|declaration|data availability|abbreviation"
    )),
]

#: Headings that are numbered ("2.1. Participants") or all-caps need tidying first.
_LEADING_NUMBER = re.compile(r"^\s*(?:[ivxlc]+\.|\d+(?:\.\d+)*\.?)\s*", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")


def canonicalise(title: str) -> Section | None:
    """Map a real section heading onto a canonical `Section`.

    Returns `None` when nothing matches, which is the signal for the caller to spend one
    cheap Jev Choice on it rather than guessing.
    """
    cleaned = _LEADING_NUMBER.sub("", (title or "").strip().lower())
    if not cleaned:
        return None
    for section, pattern in _CANONICAL_PATTERNS:
        if pattern.search(cleaned):
            return section
    return None


class ParsedSection(BaseModel):
    title: str
    text: str
    level: int = 0
    canonical: Section | None = None

    @property
    def tokens(self) -> int:
        return max(1, len(self.text) // 4)


class ParsedSections(BaseModel):
    """A paper broken into sections, plus the bits that live outside them."""

    title: str = ""
    abstract: str = ""
    sections: list[ParsedSection] = Field(default_factory=list)

    @property
    def unresolved(self) -> list[ParsedSection]:
        """Sections whose heading the regex canonicaliser could not place."""
        return [s for s in self.sections if s.canonical is None]

    def text_for(self, wanted: Iterable[Section]) -> dict[str, str]:
        """Collect the text of every requested canonical section.

        `title` and `abstract` are drawn from the paper's front matter rather than its body.
        When a requested body section is simply absent — plenty of papers have no heading a
        matcher would call "participants" — it is left out, and `screen_fulltext` decides
        what to fall back on.
        """
        out: dict[str, str] = {}
        for section in wanted:
            if section is Section.TITLE:
                if self.title:
                    out["title"] = self.title
                continue
            if section is Section.ABSTRACT:
                if self.abstract:
                    out["abstract"] = self.abstract
                continue
            parts = [s.text for s in self.sections if s.canonical is section and s.text]
            if parts:
                out[section.value] = "\n\n".join(parts)
        return out

    @property
    def available(self) -> set[Section]:
        found = {s.canonical for s in self.sections if s.canonical is not None}
        if self.title:
            found.add(Section.TITLE)
        if self.abstract:
            found.add(Section.ABSTRACT)
        return found


def _local(element: Any) -> str:
    tag = element.tag
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _flatten_text(element: Any) -> str:
    """All text under an element, whitespace-normalised.

    Cross-references, figure labels and bibliography markers are dropped: they are noise in
    a screening state, and noise is the documented way to lose accuracy.
    """
    skip = {"cross-ref", "cross-refs", "xref", "label", "float-anchor", "bib-reference"}
    parts: list[str] = []

    def walk(node: Any) -> None:
        if _local(node) in skip:
            if node.tail:
                parts.append(node.tail)
            return
        if node.text:
            parts.append(node.text)
        for child in node:
            walk(child)
        if node.tail:
            parts.append(node.tail)

    walk(element)
    return _WHITESPACE.sub(" ", "".join(parts)).strip()


def _paragraph_text(element: Any) -> str:
    """Text of an element's own paragraphs, excluding any nested subsection's."""
    parts: list[str] = []
    for child in element:
        name = _local(child)
        if name in ("para", "simple-para", "p"):
            text = _flatten_text(child)
            if text:
                parts.append(text)
        elif name in ("list", "display", "table", "figure"):
            text = _flatten_text(child)
            if text:
                parts.append(text)
    return "\n".join(parts)


def _walk_sections(element: Any, level: int, out: list[ParsedSection]) -> None:
    for child in element:
        if _local(child) not in ("section", "sec"):
            continue
        title = ""
        for grandchild in child:
            if _local(grandchild) in ("section-title", "title"):
                title = _flatten_text(grandchild)
                break
        text = _paragraph_text(child)
        if title or text:
            out.append(
                ParsedSection(title=title, text=text, level=level, canonical=canonicalise(title))
            )
        _walk_sections(child, level + 1, out)


def _scope(root: Any, name: str) -> Any | None:
    for element in root.iter():
        if _local(element) == name:
            return element
    return None


def _first_text(root: Any, names: set[str]) -> str:
    for element in root.iter():
        if _local(element) in names:
            text = _flatten_text(element)
            if text:
                return text
    return ""


def _front_matter(root: Any) -> tuple[str, str]:
    """Pull the article title and abstract, from either schema.

    The two differ in exactly the way that makes a naive lookup wrong. JATS names the title
    `<article-title>` and uses bare `<title>` for *section headings*, so searching the
    document for the first `<title>` would confidently return "1. Introduction". Elsevier-style
    markup
    puts both in `<coredata>`. So each is looked for in its own place rather than by a
    single hopeful pattern.
    """
    title = ""
    coredata = _scope(root, "coredata")
    if coredata is not None:
        title = _first_text(coredata, {"title"})
    if not title:
        title = _first_text(root, {"article-title"})

    abstract = ""
    if coredata is not None:
        abstract = _first_text(coredata, {"description"})
    if not abstract:
        element = _scope(root, "abstract")
        if element is not None:
            abstract = _flatten_text(element)
    return title, abstract


def parse_sections(xml: str | bytes) -> ParsedSections:
    """Parse JATS or Elsevier-style full-text XML into sections.

    Tolerant by design: a paper with no `<sections>` element at all still yields one
    catch-all section holding every paragraph, so it can be screened rather than dropped.
    """
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    if not xml.strip():
        return ParsedSections()

    # `recover=True` salvages truncated or malformed markup, but lxml still refuses a
    # document with no root element at all, so that case is caught rather than raised.
    parser = etree.XMLParser(recover=True, resolve_entities=False, no_network=True)
    try:
        root = etree.fromstring(xml, parser=parser)
    except etree.XMLSyntaxError:
        return ParsedSections()
    if root is None:
        return ParsedSections()

    title, abstract = _front_matter(root)

    sections: list[ParsedSection] = []
    for element in root.iter():
        if _local(element) in ("sections", "body"):
            _walk_sections(element, 0, sections)
            if sections:
                break

    if not sections:
        # No structure to work with. Better one unlabelled block than nothing at all.
        body = _first_text(root, {"originalText", "body"})
        if body:
            sections = [ParsedSection(title="", text=body, level=0, canonical=None)]

    return ParsedSections(title=title, abstract=abstract, sections=sections)
