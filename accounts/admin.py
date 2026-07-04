from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import UserCreationForm
from .models import User


class CustomUserCreationForm(UserCreationForm):
    email = forms.EmailField(required=True, label="Email")

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "email")


@admin.register(User)
class MyUserAdmin(UserAdmin):
    add_form = CustomUserCreationForm
    fieldsets = UserAdmin.fieldsets + (
        ("Datos adicionales", {"fields": ("phone",)}),
    )
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("username", "email", "password1", "password2"),
        }),
        ("Datos adicionales", {"fields": ("phone",)}),
    )
    list_display = ("username", "email", "phone", "is_staff", "is_active")