from django import forms
from django.contrib.auth.forms import UserCreationForm, AuthenticationForm
from django.contrib.auth import get_user_model
from accounts.models import phone_validator

User = get_user_model()

class TWMixin:
    base = (
        "w-full rounded-xl border border-neutral-200 bg-slate-50 px-4 py-2.5 text-sm "
        "outline-none placeholder:text-neutral-400 "
        "focus:border-slate-400 focus:bg-white focus:ring-2 focus:ring-slate-400/20 "
        "transition-colors"
    )
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for f in self.fields.values():
            f.widget.attrs["class"] = (f.widget.attrs.get("class","") + " " + self.base).strip()
            f.widget.attrs.setdefault("placeholder", f.label)


class UserRegistrationForm(TWMixin, UserCreationForm):
    first_name = forms.CharField(label="Nombre", max_length=150, required=True)
    last_name  = forms.CharField(label="Apellidos", max_length=150, required=True)
    email      = forms.EmailField(label="Correo electrónico", required=True)
    phone      = forms.CharField(
        label="Teléfono",
        max_length=20,
        required=False,
        validators=[phone_validator],
    )

    class Meta:
        model = User
        fields = ("first_name", "last_name", "email", "phone", "password1", "password2")
        widgets = {
            "email": forms.EmailInput(attrs={"autocomplete": "email"}),
            "phone": forms.TextInput(attrs={"autocomplete": "tel"}),
        }

    def clean_email(self):
        email = self.cleaned_data.get("email")
        if User.objects.filter(email=email).exists():
            raise forms.ValidationError("Ya existe una cuenta con este email.")
        return email


class EmailLoginForm(TWMixin, AuthenticationForm):
    username = forms.EmailField(
        label="Correo electrónico",
        widget=forms.EmailInput(attrs={"autocomplete": "email"}),
    )
