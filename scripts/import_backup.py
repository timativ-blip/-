"""Upload surveys from a phone's "backup" file (exit-poll-backup-YYYY-MM-DD.json) to the server.

The interviewer app can save every survey stored on the phone into that file (button "Скачать резервную копию").
Use this when the phone cannot deliver its queue by itself. Surveys are sent through /api/surveys/batch in packs of 50;
every survey has its own id, so sending one twice never creates a duplicate.

    pip install requests
    ACCESS_CODE=<interviewer access code> python scripts/import_backup.py exit-poll-backup-2026-09-20.json [--base URL] [--all] [--dry-run]

Only surveys that the phone had not delivered are sent, unless --all is given. The code is read from the environment, never from arguments.
"""
import argparse
import json
import os
import sys
import time

BASE = "https://exit-poll-pw6y.onrender.com"
PACK = 50


def pending_surveys(data, send_all=False):
    """Surveys from a backup that still need to reach the server, without the phone's bookkeeping fields."""
    items = data["surveys"] if isinstance(data, dict) else data
    result = []
    for item in items:
        if not send_all and (item.get("received") or item.get("rejected")):
            continue
        result.append({key: value for key, value in item.items() if key not in ("received", "rejected", "problem")})
    return result


def packs(items, size=PACK):
    return [items[i:i + size] for i in range(0, len(items), size)]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file")
    parser.add_argument("--base", default=BASE)
    parser.add_argument("--all", action="store_true", help="also resend surveys the phone marked as delivered")
    parser.add_argument("--dry-run", action="store_true", help="only count what would be sent")
    args = parser.parse_args()
    with open(args.file, encoding="utf-8") as handle:
        items = pending_surveys(json.load(handle), args.all)
    print(f"{len(items)} surveys to send in {len(packs(items))} packs")
    if args.dry_run or not items:
        return 0
    import requests  # build/operations time only
    session = requests.Session()
    code = os.environ.get("ACCESS_CODE", "")
    if code:
        response = session.post(args.base + "/api/login", json={"code": code}, timeout=60)
        if response.status_code != 200:
            print("Login failed:", response.status_code, response.text[:200])
            return 1
    saved, failed = 0, []
    for number, pack in enumerate(packs(items), 1):
        response = None
        for attempt in range(4):
            try:
                response = session.post(args.base + "/api/surveys/batch", json={"surveys": pack}, timeout=90)
                if response.status_code == 200:
                    break
            except requests.RequestException:
                response = None
            time.sleep(3 * (attempt + 1))
        if response is None or response.status_code != 200:
            print(f"Pack {number}: giving up after 4 attempts")
            failed.extend({"id": item.get("id"), "reason": "pack not delivered"} for item in pack)
            continue
        results = response.json()["results"]
        ok = sum(1 for r in results if r["saved"])
        saved += ok
        failed.extend({"id": r.get("id"), "reason": r.get("error")} for r in results if not r["saved"])
        print(f"Pack {number}: {ok}/{len(pack)} saved")
    print(f"Done: {saved} saved, {len(failed)} not saved")
    for item in failed[:20]:
        print("  not saved:", item["id"], "-", item["reason"])
    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
