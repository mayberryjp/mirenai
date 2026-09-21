from mirenai.domain.policy import PolicyRule, select_policy


def _rule(client: str, domain: str, action: str = "forward") -> PolicyRule:
    return PolicyRule(client=client, domain=domain, action=action)


def test_allow_everything_wildcard() -> None:
    rules = [_rule("1.2.3.4", "*", "forward")]
    match = select_policy(rules, "1.2.3.4", "anything.example.net")
    assert match is not None
    assert match.action == "forward"


def test_no_match_returns_none() -> None:
    rules = [_rule("1.2.3.4", "example.com")]
    assert select_policy(rules, "9.9.9.9", "example.com") is None
    assert select_policy(rules, "1.2.3.4", "other.com") is None


def test_exact_domain_beats_wildcard() -> None:
    rules = [_rule("1.2.3.4", "*", "deny"), _rule("1.2.3.4", "example.com", "forward")]
    match = select_policy(rules, "1.2.3.4", "example.com")
    assert match is not None
    assert match.action == "forward"


def test_exact_client_breaks_tie_over_wildcard_client() -> None:
    rules = [_rule("*", "example.com", "deny"), _rule("1.2.3.4", "example.com", "forward")]
    match = select_policy(rules, "1.2.3.4", "example.com")
    assert match is not None
    assert match.action == "forward"


def test_suffix_wildcard_matches_domain_and_subdomains() -> None:
    rules = [_rule("1.2.3.4", "*.example.com", "forward")]
    assert select_policy(rules, "1.2.3.4", "example.com") is not None
    assert select_policy(rules, "1.2.3.4", "www.example.com") is not None
    assert select_policy(rules, "1.2.3.4", "a.b.example.com") is not None
    assert select_policy(rules, "1.2.3.4", "notexample.com") is None


def test_longer_suffix_wins() -> None:
    rules = [_rule("*", "*.com", "deny"), _rule("*", "*.example.com", "forward")]
    match = select_policy(rules, "1.2.3.4", "a.example.com")
    assert match is not None
    assert match.action == "forward"


def test_case_insensitive_and_trailing_dot() -> None:
    rules = [_rule("1.2.3.4", "Example.COM", "forward")]
    match = select_policy(rules, "1.2.3.4", "example.com.")
    assert match is not None
    assert match.action == "forward"
