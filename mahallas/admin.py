from django.contrib import admin

from .models import Mahalla, SyncLog


@admin.register(Mahalla)
class MahallaAdmin(admin.ModelAdmin):
    list_display = ("name", "external_id", "region", "district", "is_active")
    search_fields = ("name", "external_id")
    list_filter = ("region", "is_active")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SyncLog)
class SyncLogAdmin(admin.ModelAdmin):
    list_display = ("started_at", "status", "created_count", "updated_count", "failed_count")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
