"""The id of a local application record (Airtable module off): a UUID5, so a re-run for the same application
always produces the same id and scripts/append_once.py can skip the duplicate line.

    python scripts/record_id.py --lead 0123456789ab
    python scripts/record_id.py --company "Acme" --role "Product Manager"

Name: the lead id when one is known, else "<company>|<role>" lowercased. Namespace: the fixed project UUID below
(never change it, or every existing local record id changes).
"""
import argparse
import sys
import uuid

NAMESPACE = uuid.UUID("32cb86d8-d889-4b61-b129-ff7f448af516")


def local_record_id(lead_id: str | None = None, company: str | None = None, role: str | None = None) -> str:
    if lead_id:
        name = lead_id.strip()
    elif company and role:
        name = f"{company.strip()}|{role.strip()}".lower()
    else:
        raise ValueError("need a lead id, or a company and a role")
    return str(uuid.uuid5(NAMESPACE, name))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lead")
    ap.add_argument("--company")
    ap.add_argument("--role")
    a = ap.parse_args(argv)
    try:
        print(local_record_id(a.lead, a.company, a.role))
    except ValueError as e:
        ap.error(str(e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
