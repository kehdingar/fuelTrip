import csv
import os
import googlemaps
import folium
from rest_framework.views import APIView
from rest_framework.response import Response
from fuzzywuzzy import fuzz
from concurrent.futures import ThreadPoolExecutor
from django.conf import settings

from .serializers import TripInputSerializer

# load truck stop data from CSV
def load_truck_stops(file_path):
    truck_stops = []
    with open(file_path, 'r') as file:
        reader = csv.DictReader(file)
        for row in reader:
            truck_stops.append(row)
    return truck_stops

def find_matching_stops(fuel_stop, truck_stops):
    matches = []
    for truck_stop in truck_stops:
        name_match = fuzz.ratio(fuel_stop['name'], truck_stop['Truckstop Name']) >= 90
        city_match = fuzz.ratio(fuel_stop['city'], truck_stop['City']) >= 90
        if name_match and city_match:
            matches.append(truck_stop)
    return matches

def create_map(route, fuel_stops, request):
    start_location = route['start_location']
    end_location = route['end_location']
    m = folium.Map(location=[start_location['lat'], start_location['lng']], zoom_start=5)

    # draw the route on the map
    folium.PolyLine(
        locations=[(step['end_location']['lat'], step['end_location']['lng']) for step in route['steps']],
        color='blue',
        weight=5,
        opacity=0.8
    ).add_to(m)

    # Add markers for the start and end locations
    folium.Marker(
        location=[start_location['lat'], start_location['lng']],
        popup="Start",
        icon=folium.Icon(color="green")
    ).add_to(m)

    folium.Marker(
        location=[end_location['lat'], end_location['lng']],
        popup="End",
        icon=folium.Icon(color="red")
    ).add_to(m)

    # Add fuel stop markers
    for stop in fuel_stops:
        folium.Marker(
            location=[stop['location']['lat'], stop['location']['lng']],
            popup=f"{stop['name']}\n{stop['city']}\n{stop.get('distance', 'Unknown')} meters",
            icon=folium.Icon(color="blue" if stop.get('matched') else "purple")  # Different color for matched stops
        ).add_to(m)

    # Save the map as HTML
    map_filename = 'cheapest_route_map.html'
    map_filepath = os.path.join(settings.BASE_DIR, 'static', 'maps', map_filename)
    os.makedirs(os.path.dirname(map_filepath), exist_ok=True)
    m.save(map_filepath)

    # Construct the map URL
    map_url = request.build_absolute_uri(f'/static/maps/{map_filename}')
    return map_url

class FuelTripView(APIView):
    def post(self, request):
        serializer = TripInputSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=400)

        start_address = serializer.validated_data.get('start_location')
        end_address = serializer.validated_data.get('end_location')

        if not start_address or not end_address:
            return Response({"error": "Start and end addresses are required."}, status=400)

        api_key = settings.GOOGLE_MAPS_API_KEY
        gmaps = googlemaps.Client(key=api_key)

        truck_stops = load_truck_stops('fuel.csv')

        try:
            directions_result = gmaps.directions(
                origin=start_address,
                destination=end_address,
                mode="driving",
                alternatives=True
            )
        except googlemaps.exceptions.TransportError as e:
            return Response({"error": f"Error fetching directions: {e}"}, status=500)
        except Exception as e:
            return Response({"error": str(e)}, status=500)

        cheapest_route = None
        lowest_cost = float('inf')

        for route in directions_result:
            if 'legs' in route and route['legs']:
                route_distance = route['legs'][0]['distance']['value']
                fuel_stops = []
                car_range = 500 * 1609.34
                fuel_efficiency = 10

                total_fuel_needed = route_distance / (fuel_efficiency * 1609.34)

                for step in route['legs'][0]['steps']:
                    gas_stations = gmaps.places_nearby(
                        location=step['end_location'],
                        radius=150,
                        type='gas_station'
                    )
                    for gas_station in gas_stations['results']:
                        distance_matrix_result = gmaps.distance_matrix(
                            origins=[start_address],
                            destinations=[gas_station['geometry']['location']]
                        )
                        distance_to_gas_station = distance_matrix_result['rows'][0]['elements'][0]['distance']['value']

                        fuel_stop = {
                            'name': gas_station['name'],
                            'city': gas_station['vicinity'],
                            'distance': distance_to_gas_station,
                            'location': gas_station['geometry']['location']
                        }

                        if not fuel_stops or distance_to_gas_station + fuel_stops[-1]['distance'] <= car_range:
                            fuel_stops.append(fuel_stop)

                matched_stops = []
                with ThreadPoolExecutor() as executor:
                    # Check for matches on the CSV file
                    futures = [executor.submit(find_matching_stops, stop, truck_stops) for stop in fuel_stops]
                    for i, future in enumerate(futures):
                        matches = future.result()
                        if matches:
                            # Update stop with matched data
                            fuel_stops[i]['matched'] = True
                            matched_stops.extend(matches)
                            # Set fuel price from matched stop
                            fuel_stops[i]['price'] = min(float(match['Retail Price']) for match in matches)

                # Determine the fuel price to use
                if matched_stops:
                    fuel_price = min(float(stop['Retail Price']) for stop in matched_stops)
                else:
                    # Default approximate price if fuel stop match is not found on fuel.csv
                    fuel_price = 3.6

                total_cost = total_fuel_needed * fuel_price

                if total_cost < lowest_cost:
                    lowest_cost = total_cost
                    cheapest_route = {
                        'total_cost': f"${total_cost:.2f}",
                        'steps': route['legs'][0]['steps'],
                        'start_location': route['legs'][0]['start_location'],
                        'end_location': route['legs'][0]['end_location']
                    }

        if cheapest_route:
            map_link = create_map(cheapest_route, fuel_stops, request)
            return Response({
                'total_cost': cheapest_route['total_cost'],
                'map_link': map_link,
            })
        else:
            return Response({"error": "No routes found"}, status=404)
