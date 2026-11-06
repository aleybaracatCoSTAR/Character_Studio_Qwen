"""Bounded catalogue-field lookup for names, titles and spoken years.

This is lexical retrieval, not semantic inference. Compact postings are built
once per store revision or frozen call snapshot. Candidate queries do not scan
catalogue rows, change saved spellings, or use a model/network service.
"""
from __future__ import annotations

from array import array
from collections import Counter, defaultdict
import json
import math
import re
import unicodedata


MAX_POSTING_VISITS = 8_192
MAX_CANDIDATES = 256
MAX_FUZZY_BUCKET = 512
MAX_FUZZY_TERMS = 12
MAX_WORD_LENGTH = 64
MAX_ALLOWED_SETS = 8
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_YEAR = re.compile(r'(?<![0-9])(?:1[0-9]{3}|20[0-9]{2})(?![0-9])')
_FRAMING = frozenset('''art artwork artworks painting paintings work works piece picture pictures
    image images photo photos called named titled catalogue catalog search searching looking
    look find show see please actual original exact artist artists painter creator painted made
    created produced date dated year years from in during before shortly after around about
    now actually use used using has have tell would like want can could help available record
    records information know explain describe description medium material materials technique
    dimensions size height width age old period century'''.split())
_CENTURIES = {'ten':10,'eleven':11,'twelve':12,'thirteen':13,'fourteen':14,'fifteen':15,
              'sixteen':16,'seventeen':17,'eighteen':18,'nineteen':19,'twenty':20}
_TENS = {'twenty':20,'thirty':30,'forty':40,'fifty':50,'sixty':60,'seventy':70,'eighty':80,'ninety':90}
_UNITS = {'one':1,'two':2,'three':3,'four':4,'five':5,'six':6,'seven':7,'eight':8,'nine':9}
_SPOKEN = re.compile(r'(?P<century>'+ '|'.join(_CENTURIES) +r')\s*(?:hundred\s*(?:and\s*)?)?'
                     r'(?P<tens>'+ '|'.join(_TENS) +r')\s*(?P<unit>'+ '|'.join(_UNITS) +r')?')
_DATE_CONTEXT = re.compile(r'\b(?:from|in|during|before|after|around|circa|dated|date|year|painted|made|created|produced)'
                           r'\s*(?:(?:the|year|of|was|is|about)\s+){0,3}$', re.I)


def fold(value):
    return ''.join(character for character in unicodedata.normalize('NFKD', value.casefold())
                   if not unicodedata.combining(character))


def words(value):
    return tuple(match.group() for match in _WORD.finditer(fold(value)))


def spoken_years(query):
    """Normalize conventional century+tens years only in an artwork-date query.

    A standalone 'thirteen eighty-seven' is itself a date query. Number words
    embedded in an unrelated sentence are not converted into invented years.
    """
    value = re.sub(r'[-–—]', ' ', fold(query)).strip(' ?.!,')
    compact = re.sub(r'\s+', '', value)
    compact = re.sub(r'^(?:from|paintedin|shortlybefore)', '', compact)
    whole = _SPOKEN.fullmatch(compact)
    matches = [whole] if whole else [match for match in _SPOKEN.finditer(value)
                                    if _DATE_CONTEXT.search(value[:match.start()])]
    result = set()
    for match in matches:
        if not match: continue
        year = _CENTURIES[match['century']] * 100 + _TENS[match['tens']] + _UNITS.get(match['unit'], 0)
        if 1000 <= year <= 2099: result.add(str(year))
    return result


def retrieval_terms(query, terms):
    years = spoken_years(query)
    compact_date = re.fullmatch(r'(?:paintedin|from|shortlybefore)(1[0-9]{3}|20[0-9]{2})', re.sub(r'\s+', '', fold(query)))
    if compact_date: years.add(compact_date[1])
    result = {fold(term) for term in terms if fold(term) not in _FRAMING}
    if years:
        number_words = set(_CENTURIES) | set(_TENS) | set(_UNITS) | {'hundred'}
        result = {term for term in result if term not in number_words and not _SPOKEN.fullmatch(term)
                  and not re.fullmatch(r'(?:from|paintedin|shortlybefore).+', term)}
        result |= years
    return result


def one_edit(left, right):
    """At most one insertion, deletion, substitution or adjacent transposition."""
    if left == right: return True
    if abs(len(left)-len(right)) > 1: return False
    if len(left) == len(right):
        different = [index for index, pair in enumerate(zip(left, right)) if pair[0] != pair[1]]
        return len(different) == 1 or (len(different) == 2 and different[1] == different[0]+1
            and left[different[0]] == right[different[1]] and left[different[1]] == right[different[0]])
    shorter, longer = (left, right) if len(left) < len(right) else (right, left)
    index = next((index for index, pair in enumerate(zip(shorter, longer)) if pair[0] != pair[1]), len(shorter))
    return shorter[index:] == longer[index+1:]


class ArtworkIndex:
    """Compact title/artist/date postings; no source bodies or audio are cached."""
    def __init__(self, rows):
        self.title = {}; self.artist = {}; self.dates = {}; self.names = {}; self.collections = defaultdict(set); self._allowed_cache = {}
        for row in rows:
            rowid = row['rowid']; self.collections[row['collection_id']].add(rowid)
            metadata = json.loads(row['metadata'])
            for term in set(words(row['title'])): self._append(self.title, term, rowid)
            artist = words(metadata.get('artist', ''))
            for term in set(artist): self._append(self.artist, term, rowid)
            if artist:
                for name in {''.join(artist), ''.join(sorted(artist))}:
                    if 5 <= len(name) <= 160: self._append(self.names, name, rowid)
            for year in set(_YEAR.findall(metadata.get('date', ''))): self._append(self.dates, year, rowid)
        self.vocabulary = set(self.title) | set(self.artist)
        self.buckets = self._buckets(self.vocabulary, MAX_WORD_LENGTH)
        self.name_buckets = self._buckets(self.names, 160)

    @staticmethod
    def _append(index, term, rowid):
        if term not in index: index[term] = array('Q')
        index[term].append(rowid)

    @staticmethod
    def _buckets(values, maximum):
        buckets = defaultdict(list)
        for value in sorted(values):
            if value.isalpha() and 5 <= len(value) <= maximum:
                buckets[(value[:2], len(value))].append(value)
        return buckets

    @staticmethod
    def _near(value, buckets):
        if not value.isalpha() or not 5 <= len(value) <= 160: return ()
        candidates = []
        for length in (len(value), len(value)-1, len(value)+1):
            candidates.extend(buckets.get((value[:2], length), ()))
        # A bounded bucket is conservative: an unusually broad prefix produces
        # no spelling guess, rather than a partial arbitrary nearest match.
        if len(candidates) > MAX_FUZZY_BUCKET: return ()
        return tuple(candidate for candidate in candidates if candidate != value and one_edit(value, candidate))[:4]

    def _allowed(self, ids):
        key = tuple(ids)
        if len(key) == 1: return self.collections.get(key[0], set())
        if key not in self._allowed_cache:
            if len(self._allowed_cache) >= MAX_ALLOWED_SETS: self._allowed_cache.pop(next(iter(self._allowed_cache)))
            self._allowed_cache[key] = set().union(*(self.collections.get(id, ()) for id in key))
        return self._allowed_cache[key]

    @staticmethod
    def _intersect(postings, allowed):
        ordered = sorted(postings, key=len)
        if not ordered: return set()
        result = set(ordered[0]) & allowed
        for posting in ordered[1:]:
            result.intersection_update(posting)
            if not result: break
        return result

    def ranked(self, connection, ids, query, terms):
        """Return confident catalogue-field row IDs, or None for ordinary FTS.

        Name correction requires one edit across a complete joined name with
        at least eight characters. Title matching needs >=75% token coverage
        (at least three words), or both words of a two-word title. A >=90%
        title with a >=15-point margin is kept without unrelated runners-up.
        """
        content = sorted(terms)
        if not content: return None
        allowed = self._allowed(ids)
        requested_years = {term for term in content if _YEAR.fullmatch(term)}
        years = requested_years & set(self.dates)
        alpha = [term for term in content if not _YEAR.fullmatch(term)]
        dated = set().union(*(self.dates[year] for year in years)) & allowed if years else set()
        if requested_years and not dated: return []
        if dated and not alpha: return sorted(dated)
        if alpha:
            joined = ''.join(alpha)
            names = [joined] if joined in self.names else []
            if not names and len(joined) >= 8:
                names = [name for name in self._near(joined, self.name_buckets)
                         if 1 - 1/max(len(name),len(joined)) >= .90]
            named = set().union(*(self.names[name] for name in names)) & allowed if names else set()
            if named:
                return sorted(named & dated if requested_years else named)
            if all(term in self.artist for term in alpha):
                named = self._intersect([self.artist[term] for term in alpha], allowed)
                if named and (len(alpha) > 1 or len(self.artist[alpha[0]]) >= 2*len(self.title.get(alpha[0], ()))):
                    return sorted(named & dated if requested_years else named)

        # Rare title/name terms seed candidates. No query walks the entire row
        # set; common-only queries retain the existing indexed FTS path.
        alternatives = {}
        for term in alpha[:MAX_FUZZY_TERMS]:
            alternatives[term] = (term,) + (self._near(term, self.buckets) if len(term) >= 5 else ())
        seeds = []
        for term, variants in alternatives.items():
            for variant in variants:
                for field in (self.title, self.artist):
                    posting = field.get(variant, ())
                    if posting: seeds.append((len(posting), term, posting))
        votes = Counter(); visits = 0
        for length, term, posting in sorted(seeds, key=lambda seed: seed[0]):
            if visits + length > MAX_POSTING_VISITS: continue
            visits += length
            for rowid in posting:
                if rowid in allowed: votes[rowid] += 1
            if visits >= MAX_POSTING_VISITS: break
        candidates = [rowid for rowid, _ in sorted(votes.items(), key=lambda value:(-value[1],value[0]))[:MAX_CANDIDATES]]
        if dated:
            candidates = list(dict.fromkeys([*sorted(dated)[:MAX_CANDIDATES], *candidates]))[:MAX_CANDIDATES]
        if not candidates: return None
        placeholders = ','.join('?' for _ in candidates)
        rows = connection.execute('SELECT rowid,title,metadata FROM collection_entries WHERE rowid IN ('+placeholders+')', candidates).fetchall()
        ranked = []
        for row in rows:
            if requested_years and row['rowid'] not in dated: continue
            title = set(words(row['title'])); artist = set(words(json.loads(row['metadata']).get('artist', '')))
            hits = sum(any(variant in title for variant in variants) for variants in alternatives.values())
            exact = sum(term in title for term in alternatives)
            combined = sum(any(variant in title or variant in artist for variant in variants) for variants in alternatives.values())
            n = len(alpha)
            title_ok = n >= 3 and hits >= max(3,math.ceil(.75*n)) or n == 2 and hits == 2
            combined_ok = n >= 3 and hits >= 2 and combined == n
            if title_ok or combined_ok:
                coverage = (hits + .65*(combined-hits))/n
                score = coverage + .08*exact/max(n,1) + (.2 if row['rowid'] in dated else 0)
                ranked.append((score, coverage, row['rowid']))
        if not ranked:
            return sorted(dated) if dated and not alpha else None
        ranked.sort(key=lambda value:(-value[0],value[2]))
        best = ranked[0]
        if best[1] >= .90 and (len(ranked) == 1 or best[0]-ranked[1][0] >= .15): return [best[2]]
        return [rowid for score, _, rowid in ranked if score >= best[0]-.12]
