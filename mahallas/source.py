"""Read the same paginated JSON service used by cs.egov.uz's Vue frontend."""

import re
import time
import unicodedata
from datetime import UTC

import requests
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime


class SourceError(Exception):
    """The source did not provide a complete, valid snapshot."""


def _field(columns, name, max_length, *, numeric=False):
    cell = columns.get(name)
    if not isinstance(cell, dict) or not isinstance(cell.get("data"), str):
        raise SourceError(f"Invalid or missing field: {name}.")
    value = unicodedata.normalize("NFC", " ".join(cell["data"].split()))
    if not value or len(value) > max_length:
        raise SourceError(f"Empty or oversized field: {name}.")
    if numeric and not re.fullmatch(r"[0-9]+", value):
        raise SourceError(f"Invalid numeric code: {name}.")
    return value


def normalize_record(row):
    if not isinstance(row, dict) or not isinstance(row.get("tableColumn"), dict):
        raise SourceError("Invalid mahalla record.")
    if row.get("tableStructId") != settings.MAHALLA_TABLE_ID:
        raise SourceError("A record belongs to a different classifier.")
    columns = row["tableColumn"]
    updated_at = row.get("dateTime")
    if updated_at is not None:
        try:
            updated_at = parse_datetime(updated_at)
        except (TypeError, ValueError):
            updated_at = None
        if updated_at is None:
            raise SourceError("Invalid source timestamp.")
        if timezone.is_naive(updated_at):
            updated_at = timezone.make_aware(updated_at, UTC)
    return {
        "external_id": _field(columns, "tin", 32, numeric=True),
        "name": _field(columns, "name_uz_latin", 255),
        "region": _field(columns, "region_soato", 16, numeric=True),
        "district": _field(columns, "district_soato", 16, numeric=True),
        "source_updated_at": updated_at,
        "is_active": True,
    }


def fetch_mahallas():
    """Validate every page before returning anything that can change the database."""
    records = []
    seen = set()
    total = None
    revision = None
    deadline = time.monotonic() + settings.MAHALLA_SYNC_TIMEOUT
    with requests.Session() as session:
        session.headers.update({"Accept": "application/json", "User-Agent": "MahallaSync/1.0"})
        while total is None or len(records) < total:
            if time.monotonic() >= deadline:
                raise SourceError("Source download exceeded the synchronization time limit.")
            try:
                response = session.post(
                    settings.MAHALLA_SOURCE_URL,
                    json={
                        "id": settings.MAHALLA_TABLE_ID,
                        "offset": len(records),
                        "limit": settings.MAHALLA_PAGE_SIZE,
                        "query": {},
                    },
                    timeout=(5, 20),
                )
                response.raise_for_status()
                payload = response.json()
            except requests.Timeout as exc:
                raise SourceError("Source request timed out.") from exc
            except requests.RequestException as exc:
                raise SourceError("Source request failed (connection or HTTP error).") from exc
            except ValueError as exc:
                raise SourceError("Source returned invalid JSON.") from exc

            if (
                not isinstance(payload, dict)
                or payload.get("statusCode") != 200
                or payload.get("error")
                or not isinstance(payload.get("result"), dict)
            ):
                raise SourceError("Source returned an invalid response or reported an error.")
            result = payload["result"]
            # isSuccess is false even in valid live responses; the website ignores it too.
            count, rows, structure = (
                result.get("count"),
                result.get("data"),
                result.get("tableStruct"),
            )
            if type(count) is not int or not 0 < count <= settings.MAHALLA_MAX_RECORDS:
                raise SourceError(
                    "Source record count is empty or invalid; existing data was preserved."
                )
            if not isinstance(rows, list) or not rows:
                raise SourceError("Source pagination ended before the advertised record count.")
            if (
                not isinstance(structure, dict)
                or structure.get("id") != settings.MAHALLA_TABLE_ID
                or not isinstance(structure.get("lastUpdate"), str)
                or not structure["lastUpdate"]
            ):
                raise SourceError("Source classifier metadata is invalid.")
            if total is None:
                total, revision = count, structure["lastUpdate"]
            elif count != total or structure["lastUpdate"] != revision:
                raise SourceError("Source changed during pagination; retry on the next run.")
            if len(rows) > settings.MAHALLA_PAGE_SIZE or len(records) + len(rows) > total:
                raise SourceError("Source returned more records than requested or advertised.")
            for row in rows:
                record = normalize_record(row)
                if record["external_id"] in seen:
                    raise SourceError("Source returned a duplicate mahalla TIN; snapshot rejected.")
                seen.add(record["external_id"])
                records.append(record)
    return records
