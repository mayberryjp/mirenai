from mirenai.domain.blocklist import detect_format, is_blocked, parse_blocklist

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


_ADBLOCK_SAMPLE = """\
[Adblock Plus]
! Title: HaGeZi's Multi PRO++
! Version: 2026.0928.0855.30
!
||telemetry.001-studio.com^
||ad.001zb.com^
||analytics.004gmbh.de^
"""


def test_detect_format_hosts() -> None:
    assert detect_format(_SAMPLE) == "hosts"


def test_detect_format_domain_only() -> None:
    assert detect_format("a.com\nb.com\nc.com\n") == "domain"


def test_detect_format_adblock() -> None:
    assert detect_format(_ADBLOCK_SAMPLE) == "adblock"


def test_detect_format_unknown_when_no_entries() -> None:
    assert detect_format("# only comments\n! adblock comment\n\n") == "unknown"


def test_parse_adblock_extracts_hosts() -> None:
    assert parse_blocklist(_ADBLOCK_SAMPLE) == [
        "telemetry.001-studio.com",
        "ad.001zb.com",
        "analytics.004gmbh.de",
    ]


def test_parse_adblock_strips_modifiers_skips_exceptions_and_cosmetics() -> None:
    text = (
        "||ads.example.com^$third-party\n"
        "@@||allow.example.com^\n"
        "example.com##.banner\n"
        "sub.example.org#?#.ad\n"
        "||*.wildcard.com^\n"
        "||track.example.net^\n"
    )
    assert parse_blocklist(text) == ["ads.example.com", "track.example.net"]


def test_parse_adblock_dedupes_against_hosts_format() -> None:
    assert parse_blocklist("0.0.0.0 a.com\n||a.com^\n") == ["a.com"]


