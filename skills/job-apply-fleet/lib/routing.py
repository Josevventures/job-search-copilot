"""Lead routing (ranking-engine spec §3.4). Replaces the tier rules: rank comes from the Jobs tab scorer,
not from here. Routes decide only which section a lead appears in. First match wins."""
from lib.prefs import screen_labels


def route(triage: dict, screen: dict | None, prefs: dict) -> tuple[str, str]:
    if (triage.get("airtable_match") or {}).get("relation") == "same_role":
        return "airtable", "already in Airtable"
    label = (screen or {}).get("label")
    if label in screen_labels(prefs):
        return "screened", f"screened out: {label}"
    if triage.get("posting_status") == "closed":
        return "closed", "posting closed (confirm on the Jobs tab)"
    return "board", "scored on the Jobs tab"


def warm_current_contact(network) -> dict | None:
    """First contact who works there now AND is warm. Prefix match on warmth so legacy
    free text ("warm (former colleague...)") from older triage versions still counts."""
    contacts = network.get("contacts") if isinstance(network, dict) else None
    if not isinstance(contacts, list):
        return None
    for c in contacts:
        if not isinstance(c, dict):
            continue
        status = str(c.get("status") or "").strip().lower()
        warmth = str(c.get("warmth") or "").strip().lower()
        if status == "current" and warmth.startswith("warm"):
            return c
    return None
