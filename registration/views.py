from django.shortcuts import redirect
from django.urls import reverse_lazy, reverse
from django.views.generic.edit import CreateView
from django.views import View
from django.contrib.auth.views import (
    LoginView,
    PasswordChangeView, PasswordChangeDoneView,
    PasswordResetView, PasswordResetDoneView,
    PasswordResetConfirmView, PasswordResetCompleteView,
)
from django.contrib import messages
from django.conf import settings
from django.core import signing
from django.core.mail import send_mail
from django.template.loader import render_to_string
import logging
from .forms import UserRegistrationForm, EmailLoginForm
from accounts.models import User
from bookings.models import Booking

logger = logging.getLogger(__name__)

_EMAIL_VERIFY_SALT    = "email-verify"
_EMAIL_VERIFY_MAX_AGE = 86400  # 24 horas


def _make_verification_token(user):
    return signing.dumps(user.pk, salt=_EMAIL_VERIFY_SALT)


def _send_verification_email(user, request):
    token      = _make_verification_token(user)
    verify_url = request.build_absolute_uri(
        reverse("verify_email", kwargs={"token": token})
    )
    html_body = render_to_string("emails/verify_email.html", {
        "user":       user,
        "verify_url": verify_url,
    })
    send_mail(
        subject="Verifica tu correo — Reyes Estancias",
        message=f"Verifica tu correo aquí: {verify_url}",
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        html_message=html_body,
        fail_silently=False,
    )


class SignUpView(CreateView):
    form_class     = UserRegistrationForm
    template_name  = "registration/signup.html"

    def form_valid(self, form):
        user = form.save()
        try:
            _send_verification_email(user, self.request)
        except Exception:
            logger.exception("Error enviando email de verificación a %s", user.email)
        messages.info(
            self.request,
            "Cuenta creada. Te hemos enviado un enlace de verificación a tu correo electrónico.",
        )
        return redirect(str(reverse_lazy("login")) + "?register")


class EmailLoginView(LoginView):
    form_class    = EmailLoginForm
    template_name = "registration/login.html"


class UserPasswordChangeView(PasswordChangeView):
    template_name = "registration/accounts_password_change_form.html"
    success_url = reverse_lazy("accounts_password_change_done")


class UserPasswordChangeDoneView(PasswordChangeDoneView):
    template_name = "registration/accounts_password_change_done.html"


class UserPasswordResetView(PasswordResetView):
    template_name = "registration/accounts_password_reset_form.html"


class UserPasswordResetDoneView(PasswordResetDoneView):
    template_name = "registration/accounts_password_reset_done.html"


class UserPasswordResetConfirmView(PasswordResetConfirmView):
    template_name = "registration/accounts_password_reset_confirm.html"


class UserPasswordResetCompleteView(PasswordResetCompleteView):
    template_name = "registration/accounts_password_reset_complete.html"


class VerifyEmailView(View):
    def get(self, request, token):
        try:
            user_pk = signing.loads(token, salt=_EMAIL_VERIFY_SALT, max_age=_EMAIL_VERIFY_MAX_AGE)
        except signing.SignatureExpired:
            messages.error(request, "El enlace de verificación ha caducado (válido 24 h). Solicita uno nuevo.")
            return redirect("login")
        except signing.BadSignature:
            messages.error(request, "Enlace de verificación no válido.")
            return redirect("login")

        try:
            user = User.objects.get(pk=user_pk)
        except User.DoesNotExist:
            messages.error(request, "Usuario no encontrado.")
            return redirect("login")

        if not user.email_verified:
            user.email_verified = True
            user.save(update_fields=["email_verified"])

            # Audit log: reservas guest que quedan vinculadas virtualmente al activar la cuenta
            guest_count = Booking.objects.filter(guest_email=user.email).count()
            if guest_count:
                logger.info(
                    "email_verified activado: user=%s (pk=%s) — %d reserva(s) guest vinculadas virtualmente",
                    user.email, user.pk, guest_count,
                )

        messages.success(request, "¡Correo verificado correctamente! Ya puedes iniciar sesión.")
        return redirect("login")
