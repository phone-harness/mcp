"""The list-phones balance footer. Cloud Android is metered; a cloud iPhone is not."""
import re
from pathlib import Path

from phone_harness_mcp.server import _balance_line, _devices_text

# $34.15 at 5 cents a minute is 683 minutes: the figure Shawn saw on a subscription account.
SUBSCRIPTION = {"balance_cents": 3415, "price_cents_per_minute": 5, "billing_mode": "disabled"}
METERED = "Balance: $34.15 (about 683 min at $0.05/min)"


def test_minutes_appear_only_for_metered_usage():
    assert _balance_line(dict(SUBSCRIPTION, billing_mode="metered")) == METERED
    assert _balance_line(dict(SUBSCRIPTION, billing_mode="credits")) == METERED
    # Older /me responses have a rate and no billing_mode. Those stay metered.
    assert _balance_line({"balance_cents": 3415, "price_cents_per_minute": 5}) == METERED
    # The rate can still be present when billing is off. It is not a charge.
    assert _balance_line(SUBSCRIPTION) == "Balance: $34.15"
    assert _balance_line({"balance_cents": 425, "billing_mode": "metered"}) == "Balance: $4.25"
    assert _balance_line({"balance_cents": 425, "price_cents_per_minute": 0, "billing_mode": "metered"}) == "Balance: $4.25"
    assert _balance_line({}) is None
    assert _balance_line({"balance_cents": "3415", "price_cents_per_minute": 5}) is None


def test_subscription_footer_is_the_money_alone():
    text = _devices_text([], {
        **SUBSCRIPTION,
        "available": [
            {"platform": "ios", "kind": "device", "label": "My iPhone", "id": "iph-1", "state": "available"},
            {"platform": "android", "kind": "emulator", "label": "Android emulator", "temporary": True},
        ],
    })
    balance = next(line for line in text.splitlines() if line.startswith("Balance:"))
    assert balance == "Balance: $34.15"
    assert "My iPhone" in text
    # The temporary Android catalog row is still per minute. The balance line is not.
    assert "temporary, billed per minute" in text
    assert "about" not in balance and "/min" not in balance


def test_copy_does_not_call_a_cloud_iphone_rented():
    rented = re.compile(r"\brents?\b|\brented\b|\brental\b|\brenting\b", re.I)
    roots = [Path("src"), Path("README.md"), Path("docs"), Path("CHANGELOG.md")]
    hits = []
    for root in roots:
        paths = [root] if root.is_file() else root.rglob("*")
        for path in paths:
            if not path.is_file() or path.suffix not in {".py", ".md"}:
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if rented.search(line):
                    hits.append(f"{path}:{number}: {line.strip()}")
    assert hits == []
