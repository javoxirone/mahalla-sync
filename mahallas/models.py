from django.db import models


class Mahalla(models.Model):
    external_id = models.CharField(max_length=32, unique=True)  # Source TIN / STIR.
    name = models.CharField(max_length=255)
    region = models.CharField(max_length=16, db_index=True)  # SOATO code.
    district = models.CharField(max_length=16, db_index=True)  # SOATO code.
    is_active = models.BooleanField(default=True)
    source_updated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "external_id"]

    def __str__(self):
        return self.name


class SyncLog(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"

    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status, default=Status.RUNNING)
    created_count = models.PositiveIntegerField(default=0)
    updated_count = models.PositiveIntegerField(default=0)
    unchanged_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at", "-pk"]

    @property
    def duration(self):
        if self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return None

    @property
    def counts(self):
        return {
            "created": self.created_count,
            "updated": self.updated_count,
            "unchanged": self.unchanged_count,
            "failed": self.failed_count,
        }
