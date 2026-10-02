"""One synchronization path shared by HTTP, the scheduler, and the CLI."""

import logging
from contextlib import contextmanager

from django.db import connection, transaction
from django.utils import timezone

from .models import Mahalla, SyncLog
from .source import SourceError, fetch_mahallas

logger = logging.getLogger(__name__)
SYNC_FIELDS = ("name", "region", "district", "is_active", "source_updated_at")
SYNC_LOCK_ID = 6_503_205  # Stable, application-specific PostgreSQL advisory lock key.


class SyncInProgress(Exception):
    pass


class SyncFailed(Exception):
    def __init__(self, log):
        self.log = log
        super().__init__(log.error_message)


@contextmanager
def sync_lock():
    """Coordinate all containers using the same database, without waiting in a queue."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [SYNC_LOCK_ID])
        if not cursor.fetchone()[0]:
            raise SyncInProgress("A synchronization is already running.")
    try:
        # Keep the same database session for the lock and the subsequent writes.
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [SYNC_LOCK_ID])


def synchronize():
    with sync_lock():
        # Owning the lock proves any previously 'running' log was interrupted.
        SyncLog.objects.filter(status=SyncLog.Status.RUNNING).update(
            status=SyncLog.Status.FAILED,
            finished_at=timezone.now(),
            failed_count=1,
            error_message="Previous synchronization was interrupted before completion.",
        )
        log = SyncLog.objects.create()
        try:
            records = fetch_mahallas()
            with transaction.atomic():
                existing = Mahalla.objects.in_bulk(field_name="external_id")
                creates, updates = [], []
                now = timezone.now()
                for record in records:
                    mahalla = existing.pop(record["external_id"], None)
                    if mahalla is None:
                        creates.append(Mahalla(**record))
                    elif any(getattr(mahalla, field) != record[field] for field in SYNC_FIELDS):
                        for field in SYNC_FIELDS:
                            setattr(mahalla, field, record[field])
                        mahalla.updated_at = now
                        updates.append(mahalla)
                    else:
                        log.unchanged_count += 1
                # Missing records are retained and only marked inactive after a complete fetch.
                for mahalla in existing.values():
                    if mahalla.is_active:
                        mahalla.is_active = False
                        mahalla.updated_at = now
                        updates.append(mahalla)
                Mahalla.objects.bulk_create(creates, batch_size=500)
                Mahalla.objects.bulk_update(updates, [*SYNC_FIELDS, "updated_at"], batch_size=500)
                log.created_count = len(creates)
                log.updated_count = len(updates)
                log.status = SyncLog.Status.SUCCESS
                log.finished_at = timezone.now()
                log.save()
        except Exception as exc:
            if not isinstance(exc, SourceError):
                logger.exception("Synchronization failed")
            log.status = SyncLog.Status.FAILED
            log.created_count = log.updated_count = log.unchanged_count = 0
            log.failed_count = 1
            log.error_message = (
                str(exc)
                if isinstance(exc, SourceError)
                else "Synchronization failed; database changes were rolled back."
            )
            log.finished_at = timezone.now()
            log.save()
            raise SyncFailed(log) from exc
        return log
