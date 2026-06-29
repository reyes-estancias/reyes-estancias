from django.urls import path
from .views import PropertiesList, PropertyDetail, ExportCalendarView

urlpatterns = [
    path("property_list/", PropertiesList.as_view(), name="property_list"),
    path("<int:pk>/", PropertyDetail.as_view(), name="property_detail"),
    path("calendar/<str:ical_token>.ics", ExportCalendarView.as_view(), name="export_calendar"),
]