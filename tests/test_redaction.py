from incident_triage.redaction import _luhn_ok, redact


def test_redacts_email_and_ip():
    r = redact("Contact alice@example.com about 10.0.0.5")
    assert "[REDACTED_EMAIL]" in r.redacted_text
    assert "[REDACTED_IPV4]" in r.redacted_text
    assert r.any_hit


def test_ipv4_is_not_mistagged_as_phone():
    r = redact("Firewall logs show 192.168.100.123 timing out")
    assert r.counts.get("IPV4") == 1
    assert r.counts.get("PHONE", 0) == 0


def test_leaves_clean_text_alone():
    r = redact("Login page is broken for external users")
    assert not r.any_hit
    assert r.redacted_text == "Login page is broken for external users"


def test_does_not_flag_random_digits_as_card():
    # Order number, not a credit card.
    r = redact("Order 1234567890123 failed to ship")
    assert r.counts.get("CARD", 0) == 0


def test_flags_valid_luhn_card():
    # 4111 1111 1111 1111 is a well-known Luhn-valid test PAN.
    r = redact("Card used: 4111 1111 1111 1111 for the order.")
    assert r.counts.get("CARD", 0) == 1
    assert "[REDACTED_CARD]" in r.redacted_text


def test_redacts_generic_secret():
    r = redact("api_key=abcd1234efgh5678 was leaked in logs")
    assert r.counts.get("GENERIC_SECRET", 0) >= 1


def test_luhn_helper_direct():
    assert _luhn_ok("4111111111111111")
    assert not _luhn_ok("4111111111111112")
