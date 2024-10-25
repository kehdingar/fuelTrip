from rest_framework import serializers

class TripInputSerializer(serializers.Serializer):
    start_location = serializers.CharField()
    end_location = serializers.CharField()
