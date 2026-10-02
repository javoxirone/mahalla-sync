import json
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from mahallas.sync import SyncFailed, SyncInProgress, synchronize


class Command(BaseCommand):
    help = "Synchronize once, or use --watch to synchronize automatically every 60 seconds."

    def add_arguments(self, parser):
        parser.add_argument(
            "--watch", action="store_true", help="Run immediately, then every minute."
        )

    def handle(self, *args, **options):
        try:
            while True:
                started = time.monotonic()
                close_old_connections()
                try:
                    log = synchronize()
                    self.stdout.write(json.dumps(log.counts))
                except SyncInProgress as exc:
                    self.stdout.write(str(exc))
                except SyncFailed as exc:
                    if not options["watch"]:
                        raise CommandError(str(exc)) from exc
                    self.stderr.write(str(exc))
                finally:
                    close_old_connections()
                if not options["watch"]:
                    return
                # Keep a one-minute cadence; skip missed ticks for runs longer than a minute.
                elapsed = time.monotonic() - started
                time.sleep(60 - elapsed % 60)
        except KeyboardInterrupt:
            self.stdout.write("Scheduler stopped.")
