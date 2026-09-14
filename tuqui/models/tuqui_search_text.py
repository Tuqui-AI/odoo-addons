"""From text to search terms, index entries and fragments — plain Python, no database.

**Compatible with Tuqui's side, not identical to it.** Each side owns its own
normalization; what the contract shares is the *shape* of ``terms`` and
``matched_terms`` — folded, lowercase, one entry per term — and this module
reports the terms it actually searched, so Tuqui compares against those
(``CONTRATO-busqueda.md``, "después de la primera medición real", point 6). The
rule here:

- lowercase, accents folded;
- runs of letters and digits; everything else separates — hyphens, slashes,
  dots, underscores, ``@``;
- **codes with digits are a term whole, and their digit parts are the fallback**:
  ``PR-979``, ``SO-1234``, ``FURN_7800`` are searched as the whole code first and,
  if no record has it, by the parts with digits (``979``). Emails are one term.
  So the index holds the whole code *and* its parts: "979" finds "PR-979", and
  "FURN_7800" does not match "FURNX7800" (contract, adjustments agreed with the
  implementation of #975, point 3);
- Spanish function words out;
- terms shorter than 3 characters out, unless they carry a digit;
- at most ``MAX_TERMS``, chosen by codes first, then the longest, then
  alphabetical — never by the order the words were said in.


**Why the index is built here and not with ``to_tsvector``.** The PostgreSQL
parser has its own idea of a token: ``PR-979`` becomes ``pr`` and ``-979``,
``wiki.adhoc.inc`` stays one host name. Folding accents in SQL needs ``unaccent``,
and installing that extension has costs for the whole database. Building the
``tsvector`` literal from the same tokens the query uses makes both sides agree
by construction and leaves nothing in the database but a column: no extension,
no text search configuration, no function to dump or restore.
"""

import hashlib
import re
import unicodedata
from functools import lru_cache

# A term costs one more comparison per candidate here. On the proof-of-concept
# bench (60 real queries, 100k tasks) a cap of 6 dropped recall@10 from 0.87 to
# 0.83.
MAX_TERMS = 12
MIN_TERM_LENGTH = 3

# A run longer than this is not a word anyone types: base64, a hash, a URL
# with its separators already gone. Keeping it would only fill the index.
MAX_LEXEME_CHARS = 64

# tsvector limits: positions go up to 16383, and at most 256 per lexeme.
MAX_POSITION = 16383
MAX_POSITIONS_PER_LEXEME = 255

# One weight label per indexed field, in ``tuqui.search.config._indexed_fields``
# order. The label is what lets a search ignore the fields a user cannot read.
WEIGHTS = "ABCD"

SNIPPET_WORDS = 24
MAX_SNIPPET_CHARS = 320

_WORD = r"[^\W_]+"
_TOKEN_RE = re.compile(_WORD)
# A code or an email: runs joined by separators with nothing in between. Tried
# before the single word, so "PR-979" is one piece with two parts.
_PIECE_RE = re.compile(
    rf"(?P<email>{_WORD}(?:[.+-]{_WORD})*@{_WORD}(?:[.-]{_WORD})*\.{_WORD})"
    rf"|(?P<compound>{_WORD}(?:[-_/.]{_WORD})+)"
    rf"|{_WORD}"
)

# Spanish function words, plus the verbs that name the act of searching or
# talking instead of what is searched. This module's own list: it started from
# Tuqui's (#975) and may diverge; changing it changes NORMALIZATION_VERSION.
STOPWORDS = frozenset(
    """
    a al algun alguna algunas alguno algunos ante antes aqui asi aun cada casi como con contra
    cual cuales cualquier cuando cuanto de del desde donde e el ella ellas ellos en entre eran
    es esa esas ese eso esos esta estaba estaban estan estar estas este esto estos estoy fue fueron
    ha habia han has hasta hay la las le les lo los mas me mi mis mucho muchos muy nada ni no nos
    nosotros o otra otras otro otros para pero poco por porque que quien quienes se sea segun ser si
    sobre son su sus tambien tan tanto te tiene tienen todo todos tu tus u un una uno unos
    y ya yo acerca respecto
    hablamos hablaron hable hablo charlamos charlaron charle dijimos dije dijo pedi pedimos pidio
    discutimos busco buscar buscame encontrar encontra encontrame escribi escribio escribimos
    """.split()
)


@lru_cache(maxsize=100_000)
def fold(text):
    """Lowercase and strip accents."""
    decomposed = unicodedata.normalize("NFD", text or "")
    return "".join(char for char in decomposed if unicodedata.category(char) != "Mn").lower()


def _has_digit(token):
    return any(char.isdigit() for char in token)


def is_searchable(term):
    """Whether a folded token can ever be a query term (and so is worth indexing)."""
    if len(term) > MAX_LEXEME_CHARS or term in STOPWORDS:
        return False
    return len(term) >= MIN_TERM_LENGTH or _has_digit(term)


def _pieces(text):
    """``(whole, parts)`` for each piece of ``text`` (lowercased), in order.

    ``whole`` is the folded code or email when the piece is one that is searched
    whole, else ``None``; ``parts`` are its folded runs of letters and digits.
    """
    for match in _PIECE_RE.finditer(text.lower()):
        piece = match.group()
        if match.group("email") or (match.group("compound") and _has_digit(piece)):
            yield fold(piece), [fold(part) for part in _TOKEN_RE.findall(piece)]
        else:
            yield None, [fold(part) for part in _TOKEN_RE.findall(piece)]


def normalize_query(query):
    """``(terms, ignored, fallbacks)`` for a natural-language query.

    ``terms`` in query order; ``ignored`` the words left out; ``fallbacks`` maps a
    whole code to the parts with digits to search when no record has the code.
    """
    terms, ignored, fallbacks = [], [], {}

    def add(term):
        if term in terms or term in ignored:
            return
        (terms if is_searchable(term) else ignored).append(term)

    for whole, parts in _pieces(query or ""):
        if whole is None:
            for part in parts:
                add(part)
        else:
            add(whole)
            if _has_digit(whole):
                fallbacks.setdefault(whole, [part for part in parts if _has_digit(part)])
    if len(terms) > MAX_TERMS:
        kept = set(sorted(terms, key=lambda t: (not _has_digit(t), -len(t), t))[:MAX_TERMS])
        ignored.extend(t for t in terms if t not in kept)
        terms = [t for t in terms if t in kept]
    return terms, ignored, {code: parts for code, parts in fallbacks.items() if code in terms and parts}


def lexemes(text):
    """``(lexeme, position)`` of what the index holds for ``text``.

    Positions count every run, stopwords included, so the distance between two
    terms is the real one; only searchable lexemes come out. A whole code or
    email takes the position of its first part.
    """
    position = 0
    for whole, parts in _pieces(text):
        first = position + 1
        for part in parts:
            position += 1
            if is_searchable(part):
                yield part, min(position, MAX_POSITION)
        if whole is not None and is_searchable(whole):
            yield whole, min(first, MAX_POSITION)


def quote_lexeme(lexeme):
    """A lexeme as tsvector/tsquery input: quoted, nothing parsed."""
    return "'" + lexeme.replace("\\", "\\\\").replace("'", "''") + "'"


def build_tsvector(parts, extra_lexemes=()):
    """A tsvector literal for ``parts`` — ``[(weight, text), ...]`` — or ``None`` if empty.

    Each field starts a little after the previous one ends, so a phrase never
    spans two.
    """
    positions = {}
    offset = 0
    for weight, text in parts:
        last = 0
        for term, position in lexemes(text):
            last = position
            entries = positions.setdefault(term, [])
            if len(entries) < MAX_POSITIONS_PER_LEXEME:
                entries.append(f"{min(offset + position, MAX_POSITION)}{weight}")
        offset = min(offset + last + 8, MAX_POSITION)
    if not positions:
        return None
    body = " ".join(f"{quote_lexeme(term)}:{','.join(entries)}" for term, entries in positions.items())
    extra = " ".join(quote_lexeme(lexeme) for lexeme in extra_lexemes)
    return f"{body} {extra}".strip()


def prefix_for(term):
    """The prefix a term with no match is retried with, or ``None``.

    Only words of 5 letters or more, cut to leave the last three off (never under
    5): "facturas" → "factu", "conciliaciones" → "conciliacio". It catches plural
    and inflection; it does not translate ("analysis" is not "analisis").
    """
    if len(term) < 5 or not term.isalpha():
        return None
    return term[: max(5, len(term) - 3)]


def matched_terms(text, terms, prefixes=None):
    """The ``terms`` present in ``text``, in ``terms`` order.

    ``prefixes`` maps a term to the prefix it is searched with, if any.
    """
    prefixes = prefixes or {}
    present = {term for term, _position in lexemes(text)}
    found = []
    for term in terms:
        prefix = prefixes.get(term)
        if term in present or (prefix and any(lexeme.startswith(prefix) for lexeme in present)):
            found.append(term)
    return found


def snippet(text, terms, prefixes=None):
    """A short excerpt of ``text``: the window of words with the most distinct terms."""
    words = list(_TOKEN_RE.finditer(text))
    if not words:
        return ""
    prefixes = tuple((prefixes or {}).values())
    # A whole code shows up in the text as its parts.
    wanted = set(terms) | {part for term in terms for part in _TOKEN_RE.findall(term)}
    folded = [fold(word.group()) for word in words]
    hits = [index for index, term in enumerate(folded) if term in wanted or (prefixes and term.startswith(prefixes))]
    start = 0
    if hits:
        best = -1
        for hit in hits:
            candidate = max(hit - 3, 0)
            found = len(wanted.intersection(folded[candidate : candidate + SNIPPET_WORDS]))
            if found > best:
                best, start = found, candidate
    end = min(start + SNIPPET_WORDS, len(words)) - 1
    excerpt = " ".join(text[words[start].start() : words[end].end()].split())
    if len(excerpt) > MAX_SNIPPET_CHARS:
        excerpt = excerpt[:MAX_SNIPPET_CHARS].rsplit(" ", 1)[0]
    prefix = "…" if words[start].start() > 0 else ""
    suffix = "…" if words[end].end() < len(text.rstrip()) else ""
    return f"{prefix}{excerpt}{suffix}"


# Bump when the *code* above changes what comes out (``fold``, ``_pieces``,
# ``lexemes``, ``normalize_query``). Word lists, patterns and limits need no bump:
# they are part of the fingerprint below. ``test_the_rule_version_follows_the_rule``
# fails when the output changes and neither did.
RULE_REVISION = 1


def _rule_fingerprint():
    digest = hashlib.sha256()
    for part in (
        RULE_REVISION,
        sorted(STOPWORDS),
        _WORD,
        _PIECE_RE.pattern,
        MIN_TERM_LENGTH,
        MAX_TERMS,
        MAX_LEXEME_CHARS,
        MAX_POSITION,
        MAX_POSITIONS_PER_LEXEME,
        WEIGHTS,
    ):
        digest.update(repr(part).encode())
    return digest.hexdigest()[:16]


# What an index row was built with. Derived, never typed: a configuration whose
# rows were built with another value is re-indexed by the cron, and searches
# answer ``partial`` until it is done.
NORMALIZATION_VERSION = _rule_fingerprint()
