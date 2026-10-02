import io
import json
import subprocess
import sys
from unittest.mock import Mock, patch

import requests
from django.conf import settings
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.test import Client, SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from .models import Mahalla, SyncLog
from .source import SourceError, fetch_mahallas, normalize_record
from .sync import SYNC_LOCK_ID, SyncFailed, SyncInProgress, sync_lock, synchronize


def source_row(tin="207140195", name="Bog'", **columns):
    values = {
        "tin": tin,
        "name_uz_latin": name,
        "region_soato": "1735",
        "district_soato": "1735204",
        **columns,
    }
    return {
        "id": "transient-source-row-id",
        "tableStructId": settings.MAHALLA_TABLE_ID,
        "dateTime": "2026-10-01T02:28:17.311Z",
        "tableColumn": {key: {"data": value} for key, value in values.items()},
    }


def response(rows, total=None, revision="2026-10-01T02:28:22.956Z"):
    return Mock(
        json=Mock(
            return_value={
                "statusCode": 200,
                "error": None,
                "result": {
                    "isSuccess": False,
                    "count": len(rows) if total is None else total,
                    "data": rows,
                    "tableStruct": {"id": settings.MAHALLA_TABLE_ID, "lastUpdate": revision},
                },
            }
        )
    )


class SourceTests(SimpleTestCase):
    def setUp(self):
        patcher = patch("mahallas.source.requests.Session")
        self.session = patcher.start().return_value.__enter__.return_value
        self.addCleanup(patcher.stop)

    def test_normalizes_real_shape_and_uses_tin_even_when_codes_are_missing(self):
        row = source_row(name="  Bog'  MFY \n", code="", uzcad_code="")
        record = normalize_record(row)
        self.assertEqual(record["external_id"], "207140195")
        self.assertEqual(record["name"], "Bog' MFY")
        self.assertEqual(
            record["source_updated_at"].isoformat(), "2026-10-01T02:28:17.311000+00:00"
        )

    def test_fetches_every_page_and_ignores_known_broken_success_flag(self):
        with self.settings(MAHALLA_PAGE_SIZE=2):
            self.session.post.side_effect = [
                response([source_row(), source_row("207140211")], 3),
                response([source_row("207140339")], 3),
            ]
            self.assertEqual(len(fetch_mahallas()), 3)
        calls = self.session.post.call_args_list
        self.assertEqual([call.kwargs["json"]["offset"] for call in calls], [0, 2])
        self.assertEqual(calls[0].kwargs["timeout"], (5, 20))

    def test_handles_timeout_connection_and_http_errors(self):
        for error in (requests.Timeout(), requests.ConnectionError(), requests.HTTPError("503")):
            with self.subTest(error=type(error).__name__):
                self.session.post.side_effect = error
                with self.assertRaises(SourceError):
                    fetch_mahallas()

    def test_rejects_http_error_response(self):
        self.session.post.return_value = Mock(
            raise_for_status=Mock(side_effect=requests.HTTPError("500"))
        )
        with self.assertRaises(SourceError):
            fetch_mahallas()

    def test_rejects_invalid_json(self):
        self.session.post.return_value = Mock(json=Mock(side_effect=ValueError("bad JSON")))
        with self.assertRaisesMessage(SourceError, "invalid JSON"):
            fetch_mahallas()

    def test_rejects_invalid_envelopes(self):
        for payload in (
            None,
            [],
            {},
            {"statusCode": 500},
            {"statusCode": 200, "result": [], "error": None},
        ):
            with self.subTest(payload=payload):
                self.session.post.return_value = Mock(json=Mock(return_value=payload))
                with self.assertRaises(SourceError):
                    fetch_mahallas()

    def test_rejects_empty_source_and_invalid_counts(self):
        for total in (0, -1, True, "1", 100_001):
            with self.subTest(total=total):
                self.session.post.return_value = response([], total)
                with self.assertRaises(SourceError):
                    fetch_mahallas()

    def test_rejects_truncated_pagination(self):
        self.session.post.side_effect = [response([source_row()], 2), response([], 2)]
        with self.assertRaisesMessage(SourceError, "before the advertised"):
            fetch_mahallas()

    def test_rejects_duplicate_ids_across_pages(self):
        self.session.post.side_effect = [response([source_row()], 2), response([source_row()], 2)]
        with self.assertRaisesMessage(SourceError, "duplicate"):
            fetch_mahallas()

    def test_rejects_changes_to_count_or_revision_during_fetch(self):
        for last_page in (
            response([source_row("207140211")], 3),
            response([source_row("207140211")], 2, "new-revision"),
        ):
            self.session.post.side_effect = [response([source_row()], 2), last_page]
            with self.assertRaisesMessage(SourceError, "changed during pagination"):
                fetch_mahallas()

    def test_rejects_invalid_rows(self):
        for row in (
            None,
            {},
            source_row(tin=""),
            source_row(tin="abc"),
            source_row(name=" "),
            source_row(name="a" * 256),
        ):
            with self.subTest(row=row):
                self.session.post.return_value = response([row])
                with self.assertRaises(SourceError):
                    fetch_mahallas()

    def test_rejects_wrong_classifier_and_invalid_timestamp(self):
        for field, value in (
            ("tableStructId", "wrong"),
            ("dateTime", "yesterday"),
            ("dateTime", {}),
        ):
            row = source_row()
            row[field] = value
            with self.assertRaises(SourceError):
                normalize_record(row)

    def test_missing_timestamp_is_nullable(self):
        row = source_row()
        row.pop("dateTime")
        self.assertIsNone(normalize_record(row)["source_updated_at"])

    def test_stops_at_overall_download_deadline(self):
        with self.settings(MAHALLA_SYNC_TIMEOUT=0):
            with self.assertRaisesMessage(SourceError, "time limit"):
                fetch_mahallas()
        self.session.post.assert_not_called()


class SyncTests(TestCase):
    def setUp(self):
        patcher = patch("mahallas.sync.fetch_mahallas")
        self.fetch = patcher.start()
        self.addCleanup(patcher.stop)
        self.record = normalize_record(source_row())
        self.fetch.return_value = [self.record]

    def test_creates_and_logs_new_record(self):
        log = synchronize()
        self.assertEqual(log.counts, {"created": 1, "updated": 0, "unchanged": 0, "failed": 0})
        self.assertEqual(Mahalla.objects.get().name, "Bog'")
        self.assertEqual(log.status, "success")
        self.assertIsNotNone(log.finished_at)

    def test_repeated_sync_is_idempotent_and_preserves_unchanged_timestamp(self):
        synchronize()
        before = Mahalla.objects.get()
        self.assertEqual(synchronize().unchanged_count, 1)
        after = Mahalla.objects.get()
        self.assertEqual(after.pk, before.pk)
        self.assertEqual(after.updated_at, before.updated_at)
        self.assertEqual(Mahalla.objects.count(), 1)

    def test_updates_changed_fields(self):
        synchronize()
        self.fetch.return_value = [
            {**self.record, "name": "New name", "region": "1726", "district": "1726266"}
        ]
        log = synchronize()
        record = Mahalla.objects.get()
        self.assertEqual(
            (record.name, record.region, record.district), ("New name", "1726", "1726266")
        )
        self.assertEqual((log.created_count, log.updated_count), (0, 1))

    def test_source_row_id_change_does_not_change_identity(self):
        synchronize()
        row = source_row()
        row["id"] = "a-new-export-row-id"
        self.fetch.return_value = [normalize_record(row)]
        self.assertEqual(synchronize().unchanged_count, 1)
        self.assertEqual(Mahalla.objects.count(), 1)

    def test_source_failure_preserves_every_existing_field(self):
        synchronize()
        before = list(Mahalla.objects.values())
        self.fetch.side_effect = SourceError("Source timed out.")
        with self.assertRaises(SyncFailed) as error:
            synchronize()
        self.assertEqual(list(Mahalla.objects.values()), before)
        self.assertEqual(error.exception.log.failed_count, 1)
        self.assertEqual(error.exception.log.status, "failed")

    def test_later_page_failure_does_not_partially_apply_snapshot(self):
        synchronize()
        before = list(Mahalla.objects.values())
        with patch("mahallas.source.requests.Session") as session:
            session.return_value.__enter__.return_value.post.side_effect = [
                response([source_row(name="Changed")], 2),
                requests.Timeout(),
            ]
            self.fetch.side_effect = fetch_mahallas
            with self.assertRaises(SyncFailed):
                synchronize()
        self.assertEqual(list(Mahalla.objects.values()), before)

    def test_database_failure_rolls_back_creates_and_updates(self):
        synchronize()
        self.fetch.return_value = [
            {**self.record, "name": "Changed"},
            normalize_record(source_row("207140211")),
        ]
        with patch(
            "mahallas.sync.Mahalla.objects.bulk_update", side_effect=IntegrityError("failed write")
        ):
            with (
                self.assertLogs("mahallas.sync", level="ERROR"),
                self.assertRaises(SyncFailed) as error,
            ):
                synchronize()
        self.assertEqual(Mahalla.objects.count(), 1)
        self.assertEqual(Mahalla.objects.get().name, "Bog'")
        self.assertEqual(
            error.exception.log.counts, {"created": 0, "updated": 0, "unchanged": 0, "failed": 1}
        )

    def test_missing_records_become_inactive_and_can_return(self):
        synchronize()
        self.fetch.return_value = [normalize_record(source_row("207140211"))]
        self.assertEqual(synchronize().updated_count, 1)
        self.assertFalse(Mahalla.objects.get(external_id=self.record["external_id"]).is_active)
        self.assertEqual(Mahalla.objects.count(), 2)
        self.fetch.return_value = [self.record, normalize_record(source_row("207140211"))]
        self.assertEqual(synchronize().updated_count, 1)
        self.assertTrue(Mahalla.objects.get(external_id=self.record["external_id"]).is_active)

    def test_database_enforces_unique_identifier(self):
        synchronize()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Mahalla.objects.create(**self.record)

    def test_overlapping_sync_is_rejected_without_a_second_fetch(self):
        other_session = connection.copy()
        try:
            with other_session.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(%s)", [SYNC_LOCK_ID])
            with self.assertRaises(SyncInProgress):
                synchronize()
        finally:
            other_session.close()
        self.fetch.assert_not_called()
        self.assertFalse(SyncLog.objects.exists())
        synchronize()  # Lock was released normally.

    def test_lock_excludes_another_process(self):
        code = (
            "import json,sys,psycopg; "
            "db=psycopg.connect(**json.load(sys.stdin)); "
            "locked=db.execute('SELECT pg_try_advisory_lock(%s)', "
            f"({SYNC_LOCK_ID},)).fetchone()[0]; "
            "db.close(); sys.exit(0 if locked else 1)"
        )
        config = connection.settings_dict
        database = json.dumps(
            {
                "dbname": config["NAME"],
                "user": config["USER"],
                "password": config["PASSWORD"],
                "host": config["HOST"],
                "port": config["PORT"],
                "connect_timeout": 5,
            }
        )
        with sync_lock():
            child = subprocess.run(
                [sys.executable, "-c", code],
                input=database,
                text=True,
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(child.returncode, 1, child.stderr)
        child = subprocess.run(
            [sys.executable, "-c", code],
            input=database,
            text=True,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(child.returncode, 0, child.stderr)

    def test_database_releases_lock_when_owning_session_closes(self):
        other_session = connection.copy()
        with other_session.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", [SYNC_LOCK_ID])
        other_session.close()
        self.assertEqual(synchronize().status, "success")

    def test_failed_run_releases_lock_and_next_run_can_succeed(self):
        self.fetch.side_effect = SourceError("Unavailable")
        with self.assertRaises(SyncFailed):
            synchronize()
        self.fetch.side_effect = None
        self.assertEqual(synchronize().status, "success")

    def test_recovers_log_from_interrupted_process(self):
        abandoned = SyncLog.objects.create()
        synchronize()
        abandoned.refresh_from_db()
        self.assertEqual(abandoned.status, "failed")
        self.assertIn("interrupted", abandoned.error_message)

    def test_large_snapshot_uses_batched_writes_and_unchanged_run_is_cheap(self):
        self.fetch.return_value = [
            {**self.record, "external_id": str(200_000_000 + i)} for i in range(10_001)
        ]
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(synchronize().created_count, 10_001)
        self.assertLess(len(queries), 150)
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(synchronize().unchanged_count, 10_001)
        self.assertLess(len(queries), 15)


class ApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Mahalla.objects.create(**normalize_record(source_row(name="Bunyodkor")))
        Mahalla.objects.create(
            **normalize_record(
                source_row("207140211", "Navbahor", region_soato="1726", district_soato="1726266")
            )
        )

    def test_search_filters_and_pagination(self):
        data = self.client.get("/api/mahallas/?page_size=1").json()
        self.assertEqual(data["count"], 2)
        self.assertEqual(len(data["results"]), 1)
        self.assertIn("page=2", data["next"])
        data = self.client.get("/api/mahallas/?search=NAV&region=1726&district=1726266").json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["name"], "Navbahor")
        self.assertEqual(self.client.get("/api/mahallas/?search=none").json()["results"], [])

    def test_pagination_parameters_are_validated(self):
        for query in (
            "page=0",
            "page=-1",
            "page=abc",
            "page_size=0",
            "page_size=101",
            "page_size=bad",
        ):
            with self.subTest(query=query):
                self.assertEqual(self.client.get("/api/mahallas/?" + query).status_code, 400)
        self.assertEqual(self.client.get("/api/mahallas/?page=100").status_code, 404)

    def test_manual_sync_returns_counts(self):
        log = SyncLog(created_count=3, updated_count=7, unchanged_count=9450)
        with patch("mahallas.views.synchronize", return_value=log):
            result = self.client.post("/api/mahallas/sync/")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(
            result.json(), {"created": 3, "updated": 7, "unchanged": 9450, "failed": 0}
        )

    def test_manual_sync_requires_post_and_csrf(self):
        self.assertEqual(self.client.get("/api/mahallas/sync/").status_code, 405)
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.post("/api/mahallas/sync/").status_code, 403)
        client.get("/")
        token = client.cookies["csrftoken"].value
        with patch("mahallas.views.synchronize", return_value=SyncLog()):
            self.assertEqual(
                client.post("/api/mahallas/sync/", HTTP_X_CSRFTOKEN=token).status_code, 200
            )

    def test_overlap_returns_conflict(self):
        with patch("mahallas.views.synchronize", side_effect=SyncInProgress("Already running")):
            self.assertEqual(self.client.post("/api/mahallas/sync/").status_code, 409)

    def test_source_failure_returns_bad_gateway_and_data_remains_readable(self):
        log = SyncLog(status="failed", failed_count=1, error_message="Source unavailable")
        with patch("mahallas.views.synchronize", side_effect=SyncFailed(log)):
            result = self.client.post("/api/mahallas/sync/")
        self.assertEqual(result.status_code, 502)
        self.assertEqual(result.json()["failed"], 1)
        self.assertEqual(self.client.get("/api/mahallas/").json()["count"], 2)

    def test_home_shows_data_status_and_escapes_source_content(self):
        Mahalla.objects.filter(name="Navbahor").update(name="<script>alert(1)</script>")
        result = self.client.get("/")
        self.assertContains(result, "Uzbekistan Mahallas")
        self.assertContains(result, "Synchronize Now")
        self.assertContains(result, "Bunyodkor")
        self.assertContains(result, "&lt;script&gt;")
        self.assertNotContains(result, "<script>alert(1)</script>")

    def test_home_handles_empty_database(self):
        Mahalla.objects.all().delete()
        self.assertContains(self.client.get("/"), "No mahallas yet")

    def test_history_returns_latest_runs(self):
        first = SyncLog.objects.create(status="failed", error_message="Timeout")
        second = SyncLog.objects.create(status="success")
        data = self.client.get("/api/mahallas/sync/history/").json()["results"]
        self.assertEqual([row["id"] for row in data], [second.pk, first.pk])


class SchedulerTests(SimpleTestCase):
    def test_watch_runs_immediately_then_waits_until_next_minute(self):
        log = SyncLog(created_count=1)
        with patch(
            "mahallas.management.commands.sync_mahallas.synchronize", return_value=log
        ) as sync:
            with patch("mahallas.management.commands.sync_mahallas.time") as clock:
                clock.monotonic.side_effect = [0, 2]
                clock.sleep.side_effect = KeyboardInterrupt
                call_command("sync_mahallas", watch=True, stdout=io.StringIO())
        sync.assert_called_once()
        clock.sleep.assert_called_once_with(58)

    def test_watch_continues_after_failure(self):
        failed = SyncFailed(SyncLog(error_message="Unavailable"))
        with patch(
            "mahallas.management.commands.sync_mahallas.synchronize",
            side_effect=[failed, SyncLog()],
        ) as sync:
            with patch(
                "mahallas.management.commands.sync_mahallas.time.sleep",
                side_effect=[None, KeyboardInterrupt],
            ):
                call_command(
                    "sync_mahallas", watch=True, stdout=io.StringIO(), stderr=io.StringIO()
                )
        self.assertEqual(sync.call_count, 2)
