from django.urls import path
from django.contrib.auth import views as auth_views
from .views import (
    SignUpView, EmailLoginView, VerifyEmailView,
    UserPasswordChangeView, UserPasswordChangeDoneView,
    UserPasswordResetView, UserPasswordResetDoneView,
    UserPasswordResetConfirmView, UserPasswordResetCompleteView,
)

urlpatterns = [
    path("login/",  EmailLoginView.as_view(),          name="login"),
    path("logout/", auth_views.LogoutView.as_view(),   name="logout"),
    path("signup/", SignUpView.as_view(),               name="signup"),
    path("verify-email/<str:token>/", VerifyEmailView.as_view(), name="verify_email"),

    # Recuperación de contraseña (vistas propias para evitar que Jazzmin intercepte los templates)
    path("password_reset/",         UserPasswordResetView.as_view(),         name="password_reset"),
    path("password_reset/done/",    UserPasswordResetDoneView.as_view(),     name="password_reset_done"),
    path("reset/<uidb64>/<token>/", UserPasswordResetConfirmView.as_view(),  name="password_reset_confirm"),
    path("reset/done/",             UserPasswordResetCompleteView.as_view(), name="password_reset_complete"),

    # Cambio de contraseña (vistas propias para evitar que Jazzmin intercepte los templates)
    path("password_change/",       UserPasswordChangeView.as_view(),      name="password_change"),
    path("password_change/done/",  UserPasswordChangeDoneView.as_view(),  name="accounts_password_change_done"),
]
