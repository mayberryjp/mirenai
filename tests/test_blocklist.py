from mirenai.domain.blocklist import is_blocked, parse_blocklist

_SAMPLE = """\
# Title: KADhosts
# Description: example header
#
0.0.0.0 4life.com
0.0.0.0 4shoes.com.pl
127.0.0.1 tracker.example.com  # inline comment
9print.pl
0.0.0.0 0.0.0.0
0.0.0.0 localhost

0.0.0.0 4life.com
"""


def test_parse_handles_hosts_and_domain_only_lines() -> None:
    domains = parse_blocklist(_SAMPLE)
    assert domains == [
        "4life.com",
        "4shoes.com.pl",
        "tracker.example.com",
        "9print.pl",
    ]


def test_parse_skips_comments_ips_and_invalid_names() -> None:
    domains = parse_blocklist(_SAMPLE)
    assert "0.0.0.0" not in domains  # bare IP is not a domain
    assert "localhost" not in domains  # no dot -> invalid


def test_parse_deduplicates_preserving_order() -> None:
    assert parse_blocklist("a.com\nb.com\na.com\n") == ["a.com", "b.com"]


def test_parse_normalizes_case_and_trailing_dot() -> None:
    assert parse_blocklist("0.0.0.0 Ads.Example.COM.\n") == ["ads.example.com"]


def test_is_blocked_matches_exact() -> None:
    assert is_blocked(frozenset({"ads.example"}), "ads.example")


def test_is_blocked_matches_subdomain() -> None:
    assert is_blocked(frozenset({"example.com"}), "tracker.ads.example.com")


def test_is_blocked_ignores_unrelated_and_parent_direction() -> None:
    blocked = frozenset({"ads.example.com"})
    assert not is_blocked(blocked, "example.com")  # parent is not blocked
    assert not is_blocked(blocked, "good.com")


def test_is_blocked_empty_set() -> None:
    assert not is_blocked(frozenset(), "anything.example")
