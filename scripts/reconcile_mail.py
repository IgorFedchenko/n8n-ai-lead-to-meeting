"""Local operator procedure: mark an uncertain delivery sent after checking the mailbox.

No outbound email is sent. Run with the application's DATABASE_URL, after investigation.
"""

import argparse

from app.db import alert, get_run, save, transaction


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("kind", choices=("offer", "confirmation"))
    parser.add_argument("--verified-delivered", action="store_true", required=True)
    args = parser.parse_args()
    with transaction() as session:
        run = get_run(session, args.run_id)
        data = dict(run.data)
        if data.get(args.kind + "_mail") != "uncertain":
            raise SystemExit("Only an uncertain, expired delivery can be reconciled")
        data[args.kind + "_mail"] = "sent"
        data[args.kind + "_reservation"] = {"id": "operator-reconciled"}
        if data.get("manual_reason") == "mail_delivery_uncertain":
            data.pop("manual_reason", None)
            data["status"] = (
                "completed"
                if args.kind == "confirmation"
                else "booked"
                if "book" in data["done"]
                else "offered"
            )
        save(session, run, data, args.kind + "_mail_reconciled_delivered")
        alert(session, run.id, "mail_reconciled")
    print("Reconciled; no send performed")


if __name__ == "__main__":
    main()
