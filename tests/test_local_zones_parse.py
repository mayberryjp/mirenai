from ipaddress import ip_address

from mirenai.domain.localzones import (
    DEFAULT_TTL,
    RTYPE_A,
    RTYPE_AAAA,
    RTYPE_CNAME,
    RTYPE_PTR,
    LocalRecord,
    LocalRecords,
    parse_zone,
)


def test_ipv4_line_expands_to_forward_and_reverse() -> None:
    records = parse_zone("192.0.2.10,host.example.lan")
    assert LocalRecord("host.example.lan", RTYPE_A, "192.0.2.10", DEFAULT_TTL) in records
    ptr_name = ip_address("192.0.2.10").reverse_pointer
    assert LocalRecord(ptr_name, RTYPE_PTR, "host.example.lan", DEFAULT_TTL) in records
    assert len(records) == 2


def test_ipv6_line_expands_to_aaaa_and_reverse() -> None:
    records = parse_zone("2001:db8::10,v6.example.lan")
    assert LocalRecord("v6.example.lan", RTYPE_AAAA, "2001:db8::10", DEFAULT_TTL) in records
    ptr_name = ip_address("2001:db8::10").reverse_pointer
    assert any(r.rtype == RTYPE_PTR and r.name == ptr_name for r in records)


def test_hostname_value_becomes_cname() -> None:
    records = parse_zone("host.example.lan,www.example.lan")
    assert records == [LocalRecord("www.example.lan", RTYPE_CNAME, "host.example.lan", DEFAULT_TTL)]


def test_custom_ttl_third_field() -> None:
    records = parse_zone("192.0.2.20,ttl.example.lan,60")
    assert all(r.ttl == 60 for r in records)


def test_invalid_ttl_falls_back_to_default() -> None:
    records = parse_zone("192.0.2.20,ttl.example.lan,notanumber")
    assert all(r.ttl == DEFAULT_TTL for r in records)


def test_comments_and_blank_lines_ignored() -> None:
    text = """
    # a full-line comment
    192.0.2.10,host.example.lan   # inline comment

    """
    records = parse_zone(text)
    assert {r.name for r in records} == {"host.example.lan", "10.2.0.192.in-addr.arpa"}


def test_name_is_normalized_lowercased_and_dot_stripped() -> None:
    records = parse_zone("192.0.2.10,Host.Example.LAN.")
    assert any(r.name == "host.example.lan" and r.rtype == RTYPE_A for r in records)


def test_invalid_rows_are_skipped() -> None:
    text = "notenoughfields\n,emptyvalue\n192.0.2.10,\n192.0.2.10,ok.example.lan"
    records = parse_zone(text)
    assert {r.name for r in records if r.rtype == RTYPE_A} == {"ok.example.lan"}


def test_duplicate_rows_are_deduped() -> None:
    records = parse_zone("192.0.2.10,host.example.lan\n192.0.2.10,host.example.lan")
    assert len(records) == 2


def test_local_records_index_lookup() -> None:
    records = parse_zone("192.0.2.10,host.example.lan\nhost.example.lan,www.example.lan")
    index = LocalRecords(records)
    assert index.owns("host.example.lan") is True
    assert index.owns("missing.example.lan") is False
    assert index.get("host.example.lan", RTYPE_A)[0].value == "192.0.2.10"
    assert index.cname("www.example.lan") is not None
    assert index.cname("host.example.lan") is None
    assert len(index) == 3


def test_empty_local_records() -> None:
    index = LocalRecords([])
    assert len(index) == 0
    assert index.owns("anything") is False
    assert index.get("anything", RTYPE_A) == []
