"""
URL configuration for reyes_estancias project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings

urlpatterns = [
    path('admin/', admin.site.urls),
    path("", include("core.urls")),
    path("properties/", include("properties.urls")),
    path("bookings/", include("bookings.urls")),
    path("payments/", include("payments.urls")),
    #Auth
    path("accounts/", include("django.contrib.auth.urls")),
    path("accounts/", include("registration.urls")),

]
#1-IMPORTANTE=> Si paso las urls de la manera en la que lo he hecho con pages, a la hora de acceder a esos paths en los templates tengo que ponerlo así=> {% url 'pages:nombre_de_url' %}
#2-IMPORTANTE=> Si paso las urls de la manera en la que lo he hecho con profiles, a la hora de acceder a esos paths en los templates tengo que poner el nombre de la url directamente. 
    #2.1 Ejemplo=>Si dentro de profiles hay dos path(profiles_list, profiles_detail), para acceder a uno de ellos sería así=> {% url 'profiles_list' %}. No puedo poner {% url 'profiles:profiles_list' %}. 
from django.conf.urls.static import static
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

if settings.DEBUG:
    urlpatterns += [path("__reload__/", include("django_browser_reload.urls"))]
    
