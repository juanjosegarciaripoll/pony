"""Unit tests for pony.search_parser."""

from __future__ import annotations

from pony.search_parser import parse_query


def test_bare_words_search_subject_and_body() -> None:
    """A bare word is the broad default: "invoice" should find a subject too."""
    q = parse_query("hello world")
    assert q.text == "hello world"
    assert q.body == ""
    assert q.from_address == ""
    assert q.subject == ""


def test_from_prefix() -> None:
    q = parse_query("from:alice")
    assert q.from_address == "alice"
    assert q.text == ""
    assert q.body == ""


def test_to_prefix() -> None:
    q = parse_query("to:bob")
    assert q.to_address == "bob"


def test_cc_prefix() -> None:
    q = parse_query("cc:carol")
    assert q.cc_address == "carol"


def test_subject_prefix() -> None:
    q = parse_query("subject:hello")
    assert q.subject == "hello"


def test_subj_alias() -> None:
    q = parse_query("subj:hello")
    assert q.subject == "hello"


def test_body_explicit_prefix() -> None:
    """``body:`` narrows to the body, unlike a bare word."""
    q = parse_query("body:world")
    assert q.body == "world"
    assert q.text == ""


def test_mixed_tokens() -> None:
    q = parse_query("from:alice subject:re: hello world")
    assert q.from_address == "alice"
    assert q.subject == "re:"
    assert q.text == "hello world"


def test_multiple_same_field() -> None:
    q = parse_query("from:alice from:bob")
    assert q.from_address == "alice bob"


def test_quoted_value() -> None:
    q = parse_query('subject:"hello world"')
    assert q.subject == "hello world"


def test_quoted_bare_words() -> None:
    q = parse_query('"hello world"')
    assert q.text == "hello world"


def test_case_insensitive_by_default() -> None:
    q = parse_query("hello")
    assert q.case_sensitive is False


def test_case_flag_on() -> None:
    q = parse_query("case:yes hello")
    assert q.case_sensitive is True
    assert q.text == "hello"


def test_case_flag_off_explicit() -> None:
    q = parse_query("case:no hello")
    assert q.case_sensitive is False


def test_unknown_prefix_treated_as_a_bare_word() -> None:
    q = parse_query("foo:bar baz")
    assert "foo:bar" in q.text
    assert "baz" in q.text


def test_empty_string() -> None:
    q = parse_query("")
    assert q.body == ""
    assert q.from_address == ""


def test_unclosed_quote_fallback() -> None:
    # shlex raises on unclosed quote; parser falls back to split()
    q = parse_query('from:alice "unclosed')
    assert q.from_address == "alice"
    assert '"unclosed' in q.text
