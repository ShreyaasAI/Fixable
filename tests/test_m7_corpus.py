"""The guard corpus (m7-contract §6): shape, split secrets, and the regex detectors against it."""

from __future__ import annotations

import re
from collections import Counter

import pytest
from m7_fakes import CORPUS, MARKER, CorpusItem, load_corpus

from swf.guard import scan_secrets, scan_sensitive

CATEGORIES = {"injection", "toxicity", "malicious", "pii", "secret", "clean"}
SOURCES = {"issue", "agents_md", "tool_observation"}
MINIMUMS = {
    "injection": 20,
    "toxicity": 10,
    "malicious": 12,
    "pii": 10,
    "secret": 8,
    "clean": 30,
}
ITEMS = load_corpus()


def test_the_file_is_lf_only() -> None:
    assert b"\r" not in CORPUS.read_bytes()


def test_there_are_at_least_ninety_items_and_each_category_meets_its_minimum() -> None:
    counts = Counter(i.category for i in ITEMS)
    assert len(ITEMS) >= 90
    for category, minimum in MINIMUMS.items():
        assert counts[category] >= minimum, (category, counts[category])


def test_ids_are_unique_and_well_formed() -> None:
    ids = [i.id for i in ITEMS]
    assert len(ids) == len(set(ids))
    assert all(re.fullmatch(r"[a-z]+-\d{3}", i) for i in ids)


def test_categories_and_sources_are_valid() -> None:
    for item in ITEMS:
        assert item.category in CATEGORIES, item.id
        assert item.source in SOURCES, item.id


def test_expect_is_a_subset_of_the_categories_and_clean_expects_nothing() -> None:
    for item in ITEMS:
        assert set(item.expect) <= CATEGORIES - {"clean"}, item.id
        if item.category == "clean":
            assert item.expect == (), item.id
        else:
            assert item.expect, item.id
            assert item.category in item.expect, item.id


@pytest.mark.parametrize("category", sorted(CATEGORIES))
def test_each_category_uses_all_three_sources(category: str) -> None:
    sources = {i.source for i in ITEMS if i.category == category}
    assert sources == SOURCES


def test_every_item_has_text() -> None:
    assert all(i.text.strip() for i in ITEMS)


def test_the_loader_removes_the_marker_and_nothing_else() -> None:
    raw = CORPUS.read_text(encoding="utf-8")
    assert MARKER in raw
    assert all(MARKER not in i.text for i in ITEMS)


def test_the_raw_file_holds_no_contiguous_secret() -> None:
    raw = CORPUS.read_text(encoding="utf-8")
    assert scan_secrets(raw) == []


def test_every_secret_item_carries_the_marker_in_the_raw_file() -> None:
    import yaml

    data = yaml.safe_load(CORPUS.read_text(encoding="utf-8"))
    for raw in data["items"]:
        if raw["category"] == "secret":
            assert MARKER in raw["text"], raw["id"]


def test_the_hard_negative_mix_is_present() -> None:
    clean = " ".join(i.text for i in ITEMS if i.category == "clean")
    assert "@" in clean  # email addresses are hard negatives (§13 A2)
    assert "test_ignore_previous" in clean
    assert re.search(r"\b[0-9a-f]{40}\b", clean)  # a git sha
    assert re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", clean)
    assert re.search(r"\b\d+\.\d+\.\d+\.\d+\b", clean)  # IP-like version
    assert re.search(r"\b\d{4}-\d{2}-\d{2}\b", clean)  # a date


def _scan_hits(items: list[CorpusItem], category: str) -> list[tuple[str, set[str]]]:
    return [
        (i.id, {f.category for f in scan_sensitive(i.text)})
        for i in items
        if i.category == category
    ]


@pytest.mark.parametrize("category", ["pii", "secret"])
def test_every_regex_item_is_flagged_by_scan_sensitive_with_its_category(category: str) -> None:
    for item_id, found in _scan_hits(ITEMS, category):
        assert category in found, item_id


def test_no_clean_item_has_a_regex_hit() -> None:
    for item in ITEMS:
        if item.category == "clean":
            assert scan_sensitive(item.text) == [], item.id
