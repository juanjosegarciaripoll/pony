"""Unit tests for pony.tui.search_parser."""

from __future__ import annotations

from pony.tui.search_parser import parse_query


def test_bare_words_go_to_body() -> None:
    q = parse_query("hello world")
    assert q.body == "hello world"
    assert q.from_address == ""
    assert q.subject == ""


def test_from_prefix() -> None:
    q = parse_query("from:alice")
    assert q.from_address == "alice"
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
    q = parse_query("body:world")
    assert q.body == "world"


def test_mixed_tokens() -> None:
    q = parse_query("from:alice subject:re: hello world")
    assert q.from_address == "alice"
    assert q.subject == "re:"
    assert q.body == "hello world"


def test_multiple_same_field() -> None:
    q = parse_query("from:alice from:bob")
    assert q.from_address == "alice bob"


def test_quoted_value() -> None:
    q = parse_query('subject:"hello world"')
    assert q.subject == "hello world"


def test_quoted_bare_words() -> None:
    q = parse_query('"hello world"')
    assert q.body == "hello world"


def test_case_insensitive_by_default() -> None:
    q = parse_query("hello")
    assert q.case_sensitive is False


def test_case_flag_on() -> None:
    q = parse_query("case:yes hello")
    assert q.case_sensitive is True
    assert q.body == "hello"


def test_case_flag_off_explicit() -> None:
    q = parse_query("case:no hello")
    assert q.case_sensitive is False


def test_unknown_prefix_treated_as_body() -> None:
    q = parse_query("foo:bar baz")
    assert "foo:bar" in q.body
    assert "baz" in q.body


def test_empty_string() -> None:
    q = parse_query("")
    assert q.body == ""
    assert q.from_address == ""


def test_unclosed_quote_fallback() -> None:
    # shlex raises on unclosed quote; parser falls back to split()
    q = parse_query('from:alice "unclosed')
    assert q.from_address == "alice"
    assert '"unclosed' in q.body
