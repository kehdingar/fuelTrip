import os
import googlemaps
import folium
from geopy.distance import great_circle
from difflib import SequenceMatcher
from django.conf import settings
from django.http import JsonResponse
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from .serializers import TripInputSerializer
import pandas as pd
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

def similar(a, b):
    return SequenceMatcher(None, a.lower(), b.lower()).ratio() * 100

fuel_data_dict = defaultdict(dict)
fuel_data = pd.read_csv('fuel.csv')
for _, row in fuel_data.iterrows():
    city = row['City'].strip().lower()
    name = row['Truckstop Name'].strip().lower()
    price = row['Retail Price']
    if pd.notna(price):
        fuel_data_dict[city][name] = price

class FuelTripView(APIView):
    def post(self, request, *args, **kwargs):
        serializer = TripInputSerializer(data=request.data)
        if serializer.is_valid():
            start_location = serializer.validated_data['start_location']
            end_location = serializer.validated_data['end_location']

            # Initialize Google Maps Client
            gmaps = googlemaps.Client(key=settings.GOOGLE_MAPS_API_KEY)

            # Get directions with alternatives
            directions_result = gmaps.directions(start_location, end_location, alternatives=True)

            if not directions_result:
                return Response({"error": "No routes found."}, status=status.HTTP_404_NOT_FOUND)

            shortest_route = min(directions_result, key=lambda x: x['legs'][0]['distance']['value'])
            fuel_stops = []

            for step in shortest_route['legs'][0]['steps']:
                places_result = gmaps.places_nearby(
                    location=(step['end_location']['lat'], step['end_location']['lng']),
                    radius=5000,
                    type='gas_station'
                )
                fuel_stops.extend(places_result.get('results', []))

            matched_stops = []
            unique_matches = set()

            def find_best_match(stop_name, city):
                if city in fuel_data_dict:
                    best_match = None
                    best_ratio = 0
                    for name in fuel_data_dict[city].keys():
                        ratio = similar(name, stop_name)
                        if ratio > best_ratio:
                            best_ratio = ratio
                            best_match = name
                    if best_ratio >= 90:
                        return best_match, fuel_data_dict[city][best_match]
                return None

            # Use ThreadPoolExecutor for parallel processing of matches
            with ThreadPoolExecutor() as executor:
                futures = []
                for stop in fuel_stops:
                    stop_name = stop['name'].lower()
                    stop_city = stop['vicinity'].split(', ')[-1].lower()
                    futures.append(executor.submit(find_best_match, stop_name, stop_city))

                for future in futures:
                    result = future.result()
                    if result:
                        matched_stops.append((stop, result[1], result[0]))

            total_cost = 0
            max_range_miles = 500
            mpg = 10
            remaining_distance_to_destination_miles = shortest_route['legs'][0]['distance']['value'] * 0.000621371
            current_fuel_level_miles = max_range_miles
            necessary_stops = []

            for i, (stop, price_per_gallon, name) in enumerate(matched_stops):
                stop_location_coords = (stop['geometry']['location']['lat'], stop['geometry']['location']['lng'])
                origin_coords = (shortest_route['legs'][0]['start_location']['lat'],
                                 shortest_route['legs'][0]['start_location']['lng'])
                distance_to_stop_miles = great_circle(origin_coords, stop_location_coords).miles
                gallons_needed_to_reach_stop = distance_to_stop_miles / mpg

                if gallons_needed_to_reach_stop > current_fuel_level_miles:
                    continue

                total_cost += gallons_needed_to_reach_stop * price_per_gallon
                necessary_stops.append(stop)

            # Create the Folium map
            start_location_coords = (shortest_route['legs'][0]['start_location']['lat'],
                                     shortest_route['legs'][0]['start_location']['lng'])
            end_location_coords = (shortest_route['legs'][0]['end_location']['lat'],
                                   shortest_route['legs'][0]['end_location']['lng'])
            m = folium.Map(location=start_location_coords, zoom_start=6)
            folium.Marker(start_location_coords, tooltip='Start: ' + str(start_location), icon=folium.Icon(color='red')).add_to(m)
            folium.Marker(end_location_coords, tooltip='End: ' + str(end_location), icon=folium.Icon(color='green')).add_to(m)

            # Draw polyline from start to end
            route_coords = [(step['end_location']['lat'], step['end_location']['lng']) for step in shortest_route['legs'][0]['steps']]
            folium.PolyLine(locations=[(shortest_route['legs'][0]['start_location']['lat'], shortest_route['legs'][0]['start_location']['lng'])] + route_coords, color='blue').add_to(m)

            for stop in necessary_stops:
                stop_location = (stop['geometry']['location']['lat'], stop['geometry']['location']['lng'])
                folium.Marker(stop_location, tooltip=stop['name'], icon=folium.Icon(color='blue')).add_to(m)

            map_filename = "route_with_fuel_stops.html"
            map_filepath = os.path.join(settings.BASE_DIR, 'static', 'maps', map_filename)
            os.makedirs(os.path.dirname(map_filepath), exist_ok=True)
            m.save(map_filepath)

            # Construct the map URL
            map_url = request.build_absolute_uri(f'/static/maps/{map_filename}')

            return JsonResponse({"total_cost": f"${total_cost:.2f}", "map_url": map_url}, status=status.HTTP_200_OK)
        else:
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
