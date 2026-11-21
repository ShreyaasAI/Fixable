"""`scan_sensitive` / `scan_secrets` (m7-contract §2.3, amended by §13 A2: no email detector)."""

from __future__ import annotations

import json

import pytest
from m7_fakes import (
    AWS_KEY,
    CARD,
    GITHUB_TOKEN,
    GOOGLE_KEY,
    JWT,
    PHONE,
    PRIVATE_KEY_HEADER,
    SK_KEY,
    SLACK_TOKEN,
    SSN,
)

from swf.guard import Finding, scan_secrets, scan_sensitive

POSITIVES = [
    ("phone", "pii", f"call {PHONE} today"),
    ("phone", "pii", "call (202) 555-0143 today"),
    ("ssn", "pii", f"ssn {SSN} on file"),
    ("credit_card", "pii", f"card {CARD} expires soon"),
    ("credit_card", "pii", "card 4111-1111-1111-1111 expires soon"),
    ("aws_access_key", "secret", f"key {AWS_KEY} here"),
    ("aws_access_key", "secret", "key ASIA" + "Z3EXAMPLE12345QR here"),
    ("github_token", "secret", f"token {GITHUB_TOKEN}"),
    ("github_token", "secret", "token github" + "_pat_" + "11ABCDEFG0abcdefghijkl_mnopqrstuv"),
    ("private_key", "secret", f"{PRIVATE_KEY_HEADER}\nMIIE"),
    ("slack_token", "secret", f"slack {SLACK_TOKEN}"),
    ("sk_api_key", "secret", f"key={SK_KEY}"),
    ("google_api_key", "secret", f"GOOGLE_API_KEY={GOOGLE_KEY}"),
    ("jwt", "secret", f"Authorization: Bearer {JWT}"),
]

NEGATIVES = [
    ("luhn-invalid card", "card 4111 1111 1111 1112 expires soon"),
    ("email", "write to alice@corp.test about it"),
    ("email with plus", "carol+tag@mail.example.net"),
    ("git sha", "commit 9fceb02d0ae598e95dc970b74767f19372d61af8"),
    ("uuid", "id 550e8400-e29b-41d4-a716-446655440000"),
    ("ip-like version", "server 1.2.3.4"),
    ("date", "released 2026-11-21"),
    ("iso timestamp", "created 2026-11-21T10:02:11Z"),
    ("ssn with area 000", "id 000-12-3456"),
    ("ssn with area 666", "id 666-12-3456"),
    ("ssn with area 9xx", "id 900-12-3456"),
    ("ssn with group 00", "id 123-00-6789"),
    ("ssn with serial 0000", "id 123-45-0000"),
    ("short sk prefix", "task sk-short and ask-me"),
    ("plain prose", "Page 2 repeats the last item of page 1."),
]


@pytest.mark.parametrize(("detector", "category", "text"), POSITIVES)
def test_each_detector_matches_its_positive(detector: str, category: str, text: str) -> None:
    findings = scan_sensitive(text)
    mine = [f for f in findings if f.detector == detector]
    assert len(mine) == 1, findings
    assert mine[0].category == category
    assert mine[0].detail == "1 match"


@pytest.mark.parametrize(("name", "text"), NEGATIVES)
def test_negatives_yield_no_finding(name: str, text: str) -> None:
    assert scan_sensitive(text) == [], name


def test_an_email_address_is_never_a_finding() -> None:
    assert scan_sensitive("alice@corp.test") == []
    assert scan_secrets("alice@corp.test") == []


def test_the_detail_counts_matches_and_never_holds_the_value() -> None:
    text = f"a {PHONE} and b +44 20 7946 0958 and c {GITHUB_TOKEN} and d {AWS_KEY} and {AWS_KEY}"
    findings = {f.detector: f for f in scan_sensitive(text)}
    assert findings["phone"].detail == "2 matches"
    assert findings["github_token"].detail == "1 match"
    assert findings["aws_access_key"].detail == "2 matches"
    for value in (PHONE, "7946", GITHUB_TOKEN, AWS_KEY, "IOSFODNN7"):
        for finding in findings.values():
            assert value not in finding.detail
            assert value not in json.dumps(finding.as_payload())


def test_findings_come_in_detector_order_one_per_detector() -> None:
    lines = [
        f"phone {PHONE}",
        f"ssn {SSN}",
        f"card {CARD}",
        f"aws {AWS_KEY}",
        f"gh {GITHUB_TOKEN}",
        f"pk {PRIVATE_KEY_HEADER}",
        f"slack {SLACK_TOKEN}",
        f"sk {SK_KEY}",
        f"google {GOOGLE_KEY}",
        f"jwt {JWT}",
    ]
    findings = scan_sensitive("\n".join(lines) + "\n" + lines[0])
    assert [f.detector for f in findings] == [
        "phone",
        "ssn",
        "credit_card",
        "aws_access_key",
        "github_token",
        "private_key",
        "slack_token",
        "sk_api_key",
        "google_api_key",
        "jwt",
    ]
    assert findings[0].detail == "2 matches"
    assert {f.category for f in findings[:3]} == {"pii"}
    assert {f.category for f in findings[3:]} == {"secret"}


def test_scan_secrets_returns_only_secrets() -> None:
    text = f"{PHONE} {SSN} {CARD} {AWS_KEY} {GITHUB_TOKEN}"
    findings = scan_secrets(text)
    assert [f.detector for f in findings] == ["aws_access_key", "github_token"]
    assert all(isinstance(f, Finding) and f.category == "secret" for f in findings)
    assert scan_secrets(f"{PHONE} {SSN} {CARD}") == []


def test_a_finding_serialises_to_category_detector_detail() -> None:
    (finding,) = scan_sensitive(f"ssn {SSN}")
    assert finding.as_payload() == {"category": "pii", "detector": "ssn", "detail": "1 match"}


def test_the_empty_string_has_no_findings() -> None:
    assert scan_sensitive("") == []
