# -*- coding: utf-8 -*-
"""Pure matching rules for the "Мощность ОБ" pyRevit button."""

import codecs
import json
import re


try:
    text_type = unicode
except NameError:
    text_type = str


def normalize_text(value):
    if value is None:
        return u""
    if not isinstance(value, text_type):
        value = text_type(value)
    value = value.lower().replace(u"ё", u"е")
    value = re.sub(u"(?<=[0-9])(?=[a-zа-я])", u" ", value, flags=re.UNICODE)
    value = re.sub(u"(?<=[a-zа-я])(?=[0-9])", u" ", value, flags=re.UNICODE)
    value = re.sub(u"[^0-9a-zа-я]+", u" ", value, flags=re.UNICODE)
    return u" ".join(value.split())


def _contains_words(mark_words, alias):
    return set(alias.split()).issubset(mark_words)


def _is_unspecified_electric_boiler(mark):
    """An electric boiler must explicitly contain 9 or 12 kW."""
    if not (u"котел" in mark or u"электрокотел" in mark):
        return False
    tokens = set(mark.split())
    electric = (
        u"электрокотел" in mark
        or u"эл" in tokens
        or u"электрический" in tokens
    )
    if not electric:
        return False
    stated_powers = tokens.intersection(set((u"9", u"12")))
    return len(stated_powers) != 1


def load_catalog(path):
    with codecs.open(path, "r", "utf-8-sig") as stream:
        catalog = json.load(stream)

    if not isinstance(catalog, list) or not catalog:
        raise ValueError(u"Справочник потребителей пуст или имеет неверный формат")

    aliases_seen = {}
    for row_number, item in enumerate(catalog, 1):
        for key in ("name", "voltage_v", "power_w", "aliases"):
            if key not in item:
                raise ValueError(
                    u"Строка {0}: отсутствует поле {1}".format(row_number, key)
                )
        if not item["aliases"]:
            raise ValueError(u"Строка {0}: не заданы варианты марки".format(row_number))

        normalized_aliases = []
        for alias in item["aliases"]:
            normalized = normalize_text(alias)
            if not normalized:
                continue
            previous = aliases_seen.get(normalized)
            if previous and previous != item["name"]:
                raise ValueError(
                    u'Вариант "{0}" одновременно задан для "{1}" и "{2}"'.format(
                        alias, previous, item["name"]
                    )
                )
            aliases_seen[normalized] = item["name"]
            if normalized not in normalized_aliases:
                normalized_aliases.append(normalized)
        item["_aliases"] = normalized_aliases

    return catalog


def match_consumer(mark, catalog):
    """Return (status, consumer, matched aliases).

    status is one of: matched, unmatched, ambiguous.
    Any matches belonging to different consumers are treated as ambiguous
    instead of guessing which appliance the designer meant.
    """
    normalized_mark = normalize_text(mark)
    if not normalized_mark:
        return "unmatched", None, []
    if _is_unspecified_electric_boiler(normalized_mark):
        return "ambiguous", None, []

    mark_words = set(normalized_mark.split())
    matches = []
    for item in catalog:
        best_alias = None
        for alias in item.get("_aliases", item.get("aliases", [])):
            normalized_alias = normalize_text(alias)
            if _contains_words(mark_words, normalized_alias):
                if best_alias is None or len(normalized_alias.split()) > len(best_alias.split()):
                    best_alias = normalized_alias
        if best_alias is not None:
            matches.append((len(best_alias), best_alias, item))

    if not matches:
        return "unmatched", None, []

    winner_names = set(match[2]["name"] for match in matches)
    if len(winner_names) != 1:
        return "ambiguous", None, [match[1] for match in matches]

    return "matched", matches[0][2], [match[1] for match in matches]
