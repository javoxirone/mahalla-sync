from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("api/mahallas/", views.mahalla_list, name="mahalla-list"),
    path("api/mahallas/sync/", views.sync_now, name="sync-now"),
    path("api/mahallas/sync/history/", views.sync_history, name="sync-history"),
]
