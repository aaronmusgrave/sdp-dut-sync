#!/usr/bin/env python3
"""Upload local ServiceDesk exports to a SharePoint document library via Microsoft Graph."""

import argparse
import csv
import json
import os
from pathlib import Path
from urllib.parse import quote

import requests


def clean(value: str | None) -> str:
    if not value:
        return ""
    return value.strip().strip('"').strip("'").replace('\\', '').strip()


TENANT_ID = clean(os.getenv("AZURE_TENANT_ID"))
CLIENT_ID = clean(os.getenv("AZURE_CLIENT_ID"))
CLIENT_SECRET = clean(os.getenv("AZURE_CLIENT_SECRET"))
DRIVE_ID = clean(os.getenv("SHAREPOINT_DRIVE_ID"))
ROOT_FOLDER_ID = clean(os.getenv("SHAREPOINT_ROOT_FOLDER_ID"))
TOKEN = None


def graph_token() -> str:
    global TOKEN
    if TOKEN:
        return TOKEN
    response = requests.post(
        f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token",
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        },
        timeout=30,
    )
    response.raise_for_status()
    TOKEN = response.json()["access_token"]
    return TOKEN


def request(method: str, url: str, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {graph_token()}"
    response = requests.request(method, url, headers=headers, timeout=90, **kwargs)
    response.raise_for_status()
    return response


def list_children(folder_id: str) -> list[dict]:
    url = f"https://graph.microsoft.com/v1.0/drives/{DRIVE_ID}/items/{folder_id}/children"
    items = []
    while url:
        data = request("GET", url, params={"$top": 200}).json()
        items.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
    return items


def ensure_folder(parent_id: str, name: str) -> str:
    for item in list_children(parent_id):
        if item.get("name") == name and item.get("folder") is not None:
            return item["id"]
    url = f"https://graph.microsoft.com/v1.0/drives/{DRIVE_ID}/items/{parent_id}/children"
    body = {
        "name": name,
        "folder": {},
        "@microsoft.graph.conflictBehavior": "fail",
    }
    return request("POST", url, json=body).json()["id"]


def upload_text(folder_id: str, filename: str, content: str, content_type: str) -> None:
    url = (
        f"https://graph.microsoft.com/v1.0/drives/{DRIVE_ID}/items/"
        f"{folder_id}:/{quote(filename, safe='')}:/content"
    )
    request("PUT", url, data=content.encode("utf-8"), headers={"Content-Type": content_type})


def delete_item(item_id: str) -> None:
    request("DELETE", f"https://graph.microsoft.com/v1.0/drives/{DRIVE_ID}/items/{item_id}")


def flatten_performance_json(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for technician, record in data.items():
        profile = record.get("profile", {})
        meetings = record.get("meetings", {})
        performance = record.get("performance", {})
        sla = record.get("sla_summary", {})
        rows.append({
            "date": record.get("date", ""),
            "technician": technician,
            "email": profile.get("email", ""),
            "department": profile.get("department", ""),
            "manager": profile.get("manager_name", ""),
            "office_location": profile.get("office_location", ""),
            "meetings": meetings.get("count", 0),
            "meeting_hours": meetings.get("hours", 0),
            "closed_tickets": len(record.get("tickets", [])),
            "sla_total": sla.get("total", 0),
            "sla_met_pct": sla.get("sla_met_pct", ""),
            "avg_resolution_minutes": sla.get("avg_resolution_minutes", ""),
            "weighted_score": performance.get("weighted_score", ""),
            "performance_rating": performance.get("performance_rating", ""),
        })
    columns = list(rows[0].keys()) if rows else ["date", "technician"]
    output = []
    from io import StringIO
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def validate_config() -> None:
    missing = [
        name for name, value in {
            "AZURE_TENANT_ID": TENANT_ID,
            "AZURE_CLIENT_ID": CLIENT_ID,
            "AZURE_CLIENT_SECRET": CLIENT_SECRET,
            "SHAREPOINT_DRIVE_ID": DRIVE_ID,
            "SHAREPOINT_ROOT_FOLDER_ID": ROOT_FOLDER_ID,
        }.items() if not value
    ]
    if missing:
        raise RuntimeError(f"Missing required SharePoint configuration: {', '.join(missing)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--target-folder", choices=["performance"], default=None)
    parser.add_argument("--glob", default=None)
    parser.add_argument("--reconcile-active", action="store_true")
    parser.add_argument("--make-csv", action="store_true")
    args = parser.parse_args()

    validate_config()
    source = Path(args.source_dir)
    root_children = {item.get("name"): item for item in list_children(ROOT_FOLDER_ID)}

    if args.target_folder:
        folder_id = ensure_folder(ROOT_FOLDER_ID, args.target_folder)
        files = sorted(source.glob(args.glob or "*"))
        for path in files:
            if not path.is_file():
                continue
            upload_text(folder_id, path.name, path.read_text(encoding="utf-8"), "application/json")
            if args.make_csv and path.suffix.lower() == ".json":
                upload_text(folder_id, path.with_suffix(".csv").name, flatten_performance_json(path), "text/csv")
            print(f"Uploaded {path.name} → {args.target_folder}")
        return

    folder_ids = {
        name: ensure_folder(ROOT_FOLDER_ID, name)
        for name in ("active", "closed", "archived")
    }

    for folder_name, folder_id in folder_ids.items():
        local_folder = source / folder_name
        if not local_folder.exists():
            continue
        for path in sorted(local_folder.glob("*.md")):
            upload_text(folder_id, path.name, path.read_text(encoding="utf-8"), "text/markdown")
            print(f"Uploaded {path.name} → {folder_name}")

    if args.reconcile_active:
        local_names = {p.name for p in (source / "active").glob("*.md")}
        for item in list_children(folder_ids["active"]):
            if item.get("file") is not None and item.get("name") not in local_names:
                delete_item(item["id"])
                print(f"Removed stale active file {item['name']}")


if __name__ == "__main__":
    main()
