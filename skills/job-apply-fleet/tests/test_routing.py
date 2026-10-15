from lib.prefs import load, screen_labels
from lib.routing import route, warm_current_contact

P = load()


def test_screen_labels_read_the_new_key():
    assert screen_labels(P) == ["non_job", "event", "internship_or_undergrad"]
    assert screen_labels({"auto_skip": {"screen_labels": ["x"]}}) == ["x"]  # legacy shape still readable


def test_route_order_first_match_wins():
    same = {"airtable_match": {"relation": "same_role"}, "posting_status": "closed"}
    assert route(same, {"label": "event"}, P)[0] == "airtable"
    assert route({}, {"label": "event"}, P) == ("screened", "screened out: event")
    assert route({"posting_status": "closed"}, None, P) == ("closed", "posting closed (confirm on the Jobs tab)")
    assert route({"posting_status": "open"}, {"label": "job"}, P) == ("board", "scored on the Jobs tab")


def test_unpaid_and_equity_only_go_to_the_board_not_skip():
    for pt in ("unpaid", "equity_only"):
        assert route({"pay_type": pt, "posting_status": "open"}, None, P)[0] == "board"


def test_warm_current_contact_unchanged():
    net = {"contacts": [{"name": "J", "status": "Current", "warmth": "warm (former colleague)"}]}
    assert warm_current_contact(net)["name"] == "J"


def test_preferences_no_longer_carry_tier_keys():
    assert "enjoyed_categories" not in P and "categories" not in P and "auto_skip" not in P
    assert P["grad_eligibility"] == {"degree": "master's", "graduated_year": 2024}
