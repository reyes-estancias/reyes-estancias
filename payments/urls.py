from django.urls import path
from .views import (
    StartCheckoutByTokenView,
    CheckoutSuccesView, CheckoutCancelView,
    StartBalanceByTokenView,
    RetryBalanceByTokenView,
    RetryDepositByTokenView,
    stripe_webhook,
)

urlpatterns = [
    # ── Camino por access_token (sin login) ──
    path("payment_start/<uuid:token>/",  StartCheckoutByTokenView.as_view(),  name="payment_start_token"),
    path("balance_start/<uuid:token>/",  StartBalanceByTokenView.as_view(),   name="start_balance_token"),
    path("retry-deposit/<uuid:token>/",  RetryDepositByTokenView.as_view(),   name="retry_deposit_token"),
    path("retry-balance/<uuid:token>/",  RetryBalanceByTokenView.as_view(),   name="retry_balance_token"),

    # ── Comunes ──
    path("payment_success/",  CheckoutSuccesView.as_view(),  name="payment_success"),
    path("payment_cancel/",   CheckoutCancelView.as_view(),  name="payment_cancel"),
    path("webhook/",          stripe_webhook,                name="webhook"),
]
