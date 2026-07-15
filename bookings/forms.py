from django import forms
from .models import *
from django.core.exceptions import ValidationError
from datetime import date
from core.forms import TWMixin
from registration.forms import TWMixin as TWMixinReg
from accounts.models import phone_validator

class ChangeDatesForm(TWMixin, forms.Form):
    checkin = forms.DateField(
        label="Llegada",
        widget=forms.DateInput(attrs={"type": "text"}),  # necesario para Flatpickr + placeholder
        input_formats=["%Y-%m-%d"],
    )
    checkout = forms.DateField(
        label="Salida",
        widget=forms.DateInput(attrs={"type": "text"}),
        input_formats=["%Y-%m-%d"],
    )

    def clean(self):
        data = super().clean()
        ci, co = data.get("checkin"), data.get("checkout")

        if not ci or not co:
            raise ValidationError("Fechas inválidas")

        days = (co - ci).days
        if days < 2:
            raise ValidationError("La reserva debe tener una duración mínima de 2 noches")
        if co < ci:
            raise ValidationError("La fecha de salida no puede ser anterior a la de llegada")
        if ci < date.today():
            raise ValidationError("La fecha de llegada debe ser posterior al día de hoy")

        return data


class GuestInfoForm(TWMixinReg, forms.Form):
    first_name = forms.CharField(
        label="Nombre",
        max_length=150,
        widget=forms.TextInput(attrs={"autocomplete": "given-name"}),
    )
    last_name = forms.CharField(
        label="Apellidos",
        max_length=150,
        widget=forms.TextInput(attrs={"autocomplete": "family-name"}),
    )
    email = forms.EmailField(
        label="Correo electrónico",
        widget=forms.EmailInput(attrs={"autocomplete": "email"}),
    )
    phone = forms.CharField(
        label="Teléfono",
        max_length=20,
        validators=[phone_validator],
        widget=forms.TextInput(attrs={"autocomplete": "tel"}),
    )

    def guest_name(self):
        return f"{self.cleaned_data['first_name']} {self.cleaned_data['last_name']}".strip()


class RequestMagicLinkForm(TWMixinReg, forms.Form):
    email = forms.EmailField(
        label="Correo electrónico",
        widget=forms.EmailInput(attrs={"autocomplete": "email"}),
    )


