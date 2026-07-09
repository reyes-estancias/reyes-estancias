from django.contrib import admin, messages
from django.utils.html import format_html
from django.urls import path, reverse
from django.shortcuts import render, redirect, get_object_or_404
from django import forms
from django.forms.widgets import ClearableFileInput
from django.db.models import Max
from concurrent.futures import ThreadPoolExecutor, as_completed

from .models import Property, PropertyImage

# --- 1) Widget múltiple que devuelve LISTA de ficheros ---
class MultipleFileInput(ClearableFileInput):
    allow_multiple_selected = True
    def value_from_datadict(self, data, files, name):
        # MUY IMPORTANTE: devolver lista, no un único archivo
        return files.getlist(name) if files else []

# --- 2) Campo que acepta listas de ficheros ---
class MultiFileField(forms.Field):
    widget = MultipleFileInput

    def to_python(self, data):
        # data ya es una lista (por el widget)
        return data or []

    def validate(self, value):
        super().validate(value)
        if self.required and not value:
            raise forms.ValidationError("Sube al menos una imagen.")

class BulkImageUploadForm(forms.Form):
    images = MultiFileField(required=True, label="Imágenes")

class PropertyImageInline(admin.TabularInline):
    model = PropertyImage
    extra = 1
    fields = ("preview", "image", "position", "cover")
    readonly_fields = ("preview",)
    ordering = ("position", "id")

    def preview(self, obj):
        if obj.image:
            return format_html('<img src="{}" style="height:60px;border-radius:6px;">', obj.image.url)
        return "—"

@admin.register(Property)
class PropertyAdmin(admin.ModelAdmin):
    list_display = ("name", "max_people", "nightly_price", "airbnb_ical_url")
    inlines = [PropertyImageInline]
    change_form_template = "admin/properties/property/change_form.html"
    search_fields = ("name",)
    fieldsets = (
        (None, {"fields": ("name", "beds", "max_people", "nightly_price", "address", "latitude", "longitude")}),
        ("Descripción", {"fields": ("description", "tu_propiedad", "servicios_zonas_comunes", "interaccion_viajeros", "otros_detalles")}),
        ("Integración iCal", {"fields": ("airbnb_ical_url",)}),
    )

    def get_urls(self):
        urls = super().get_urls()
        my = [
            path(
                "<int:pk>/bulk-upload/",
                self.admin_site.admin_view(self.bulk_upload_view),
                name="properties_property_bulk_upload",
            ),
        ]
        return my + urls

    def bulk_upload_view(self, request, pk):
        prop = get_object_or_404(Property, pk=pk)

        if request.method == "POST":
            form = BulkImageUploadForm(request.POST, request.FILES)
            if form.is_valid():
                files = form.cleaned_data["images"]  # ← lista de UploadedFile
                base = (PropertyImage.objects
                          .filter(property=prop)
                          .aggregate(m=Max("position"))["m"] or -1) + 1

                def upload_one(args):
                    i, f = args
                    return PropertyImage.objects.create(
                        property=prop, image=f, position=base + i
                    )

                with ThreadPoolExecutor(max_workers=8) as executor:
                    futures = {executor.submit(upload_one, (i, f)): i for i, f in enumerate(files)}
                    created = [None] * len(files)
                    for future in as_completed(futures):
                        i = futures[future]
                        created[i] = future.result()

                # Si no hay portada aún, marca la primera subida como cover
                if created and not PropertyImage.objects.filter(property=prop, cover=True).exists():
                    created[0].cover = True
                    created[0].save()

                messages.success(request, f"Subidas {len(created)} imágenes.")
                return redirect(reverse("admin:properties_property_change", args=[pk]))
        else:
            form = BulkImageUploadForm()

        ctx = dict(self.admin_site.each_context(request),
                   form=form, original=prop, opts=self.model._meta)
        return render(request, "admin/properties/property/bulk_upload.html", ctx)