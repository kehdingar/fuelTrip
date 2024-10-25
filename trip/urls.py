from django.urls import path
from .views import FuelTripView




urlpatterns = [
    path('calculate-fuel-trip/', FuelTripView.as_view(), name='calculate-fuel-trip'),
]

