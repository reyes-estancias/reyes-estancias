from django.urls import path
from .views import *

urlpatterns = [
    path("bookings_list/", BookingsList.as_view(), name="bookings_list"),
    path("create_booking/<int:property_id>/", CreateBookingView.as_view(), name="create_booking"),

    path("remake_booking/<int:pk>/", RemakeBookingView.as_view(), name="remake_booking"),

    # ── Magic link — Mis Reservas sin cuenta ──
    path("mis-reservas/", RequestMagicLinkView.as_view(), name="request_magic_link"),
    path("mis-reservas/verificar/<uuid:token>/", ValidateMagicLinkView.as_view(), name="validate_magic_link"),
    path("mis-reservas/verificar-email/", SendVerificationLinkView.as_view(), name="send_verification_link"),
    path("mis-reservas/salir/", ClearGuestSessionView.as_view(), name="clear_guest_session"),

    # ── Camino por access_token (sin login) ──
    path("cancel/<uuid:token>/", CancelBookingByTokenView.as_view(), name="cancel_booking_token"),
    path("cancel/<uuid:token>/sure/", CancelBookingSureByTokenView.as_view(), name="cancel_booking_sure_token"),
    path("change_dates/<uuid:token>/", BookingChangeDatesStartByTokenView.as_view(), name="booking_change_dates_start_token"),
    path("change_dates/<uuid:token>/preview/", BookingChangeDatesPreviewByTokenView.as_view(), name="booking_change_dates_preview_token"),
    path("change_dates/<uuid:token>/apply/", BookingChangeDatesApplyByTokenView.as_view(), name="booking_change_dates_apply_token"),
]