import streamlit as st
import streamlit.components.v1 as components
import json
import tempfile
import os
import requests
import pandas as pd  # Used for st.map and data visualization
from groq import Groq
from rapidfuzz import process, fuzz
from urllib.parse import quote
from datetime import datetime
import re
from gtts import gTTS
import io
import base64

# --- CONSTANTS ---
# Approximately 0.1 degrees latitude/longitude change is about 11 km
# This creates a bounding box roughly 22km x 22km, ensuring the 10km radius is covered.
RECT_OFFSET_DEGREE = 0.1
APIMARKET_BASE_URL = "https://prod.api.market/api/v1/the-api-guy/nobroker-api/api/nobroker/properties"
APIMARKET_KEY = st.secrets['APIMARKET_KEY']
# -----------------------------------------
# 1. Setup Groq client and Models
# -----------------------------------------
try:
    client = Groq(api_key=st.secrets["GROQ_API_KEY"])
    OWM_KEY = st.secrets["OPENWEATHERMAP_API_KEY"]  # Fetch OWM key here
except KeyError as e:
    st.error(f"Error: Missing API key in .streamlit/secrets.toml: {e}")
    st.stop()

# Groq Models (Confirmed as requested)
TRANSCRIBE_MODEL = 'whisper-large-v3-turbo'
LLM_MODEL = "llama-3.3-70b-versatile"

# -----------------------------------------
# 2. Known Indian places (expandable)
# -----------------------------------------
KNOWN_INDIAN_PLACES = [
    "New Delhi", "Delhi", "South Delhi", "Gurgaon", "Noida", "Faridabad", "Ghaziabad",
    "Mumbai", "Navi Mumbai", "Thane", "Pune", "Nagpur", "Nashik",
    "Bengaluru", "Mangalore", "Mysore",
    "Chennai", "Coimbatore", "Madurai", "Salem",
    "Hyderabad", "Secunderabad", "Warangal",
    "Kolkata", "Howrah", "Siliguri", "Durgapur",
    "Ahmedabad", "Gandhinagar", "Vadodara", "Surat", "Rajkot", "Bhavnagar",
    "Jamnagar", "Junagadh", "Vapi", "Valsad", "Bharuch",
    "Jaipur", "Jodhpur", "Udaipur", "Kota", "Ajmer",
    "Indore", "Bhopal", "Gwalior",
    "Lucknow", "Kanpur", "Agra", "Varanasi", "Prayagraj",
    "Patna", "Ranchi", "Jamshedpur",
    "Chandigarh", "Mohali", "Panchkula"
]


# -----------------------------------------
# 3. Fuzzy matching correction
# -----------------------------------------
def correct_place_names(text):
    words = text.split()
    corrected = []
    for w in words:
        match = process.extractOne(w, KNOWN_INDIAN_PLACES, scorer=fuzz.ratio)
        if match and match[1] > 80:
            corrected.append(match[0])
        else:
            corrected.append(w)
    return " ".join(corrected)


# -----------------------------------------
# 4. Whisper transcription
# -----------------------------------------
def transcribe_audio(audio_path):
    st.info("Transcribing audio...")
    with open(audio_path, "rb") as f:
        try:
            trans_resp = client.audio.transcriptions.create(
                file=f,
                model=TRANSCRIBE_MODEL,
                language="en"
            )
            return trans_resp.text
        except Exception as e:
            st.error("Transcription failed: " + str(e))
            raise


# -----------------------------------------
# 5. LLM JSON extraction
# -----------------------------------------
# -----------------------------------------
# 5. LLM JSON extraction using Groq (REVISED FOR LOCATION)
# -----------------------------------------
IGNORE_WORDS = ["AQI", "pollution", "traffic", "weather", "food", "news"]


def extract_json(user_text):
    st.info("Extracting structured property information (LLM)...")

    # Ensure the list structure is explicit in the template
    json_template = {
        "deal_type": None,
        "property_type": None,
        "budget": None,
        "locations": ["List of relevant cities, districts, or areas mentioned"],  # CLARIFICATION HERE
        "bedrooms": None,
        "bathrooms": None,
        "amenities": [],
        "timeline": None,
        "contact_number": None,
        "extra_notes": None
    }

    prompt = f"""
You are a parser. Your task is to read the transcript and output STRICT JSON.
- The output MUST be valid JSON, conforming strictly to the JSON Template below.
- Crucially, extract the target location (e.g., "West Delhi") and place it into the "locations" list.

User said:
\"\"\"{user_text}\"\"\"

JSON Template:
{json.dumps(json_template, indent=2)}
"""

    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system",
             "content": "You must return ONLY valid JSON data. No prose. Use double quotes. Use null when unknown."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.0
    )

    raw = response.choices[0].message.content.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()

    try:
        parsed = json.loads(raw)
        return parsed, raw
    except Exception as e:
        st.error(f"Failed to parse JSON: {e}. Raw output below:")
        st.code(raw, language='json')
        return None, raw


# -----------------------------------------
# 6. Geoapify Geocoding and Places (FINAL CATEGORY FIX)
# -----------------------------------------
def fetch_location_insights(location_name):
    GEO_API_KEY = st.secrets["GEOAPIFY_API_KEY"]

    # --- Step 1: Geocoding ---
    geocode_url = "https://api.geoapify.com/v1/geocode/search"
    geocode_params = {"text": f"{location_name}, India", "apiKey": GEO_API_KEY, "limit": 1}

    lat, lon = None, None
    try:
        geocode_response = requests.get(geocode_url, params=geocode_params).json()
        if not geocode_response.get('features'):
            st.warning(f"Geocoding could not find precise coordinates for {location_name}.")
            return None, None, None

        coords = geocode_response['features'][0]['geometry']['coordinates']
        lon, lat = coords[0], coords[1]
        # Removed: Geocoded center display
        # st.write(f"Geocoded center: Lat **{lat:.4f}**, Lon **{lon:.4f}**")
    except Exception as e:
        st.error(f"Geocoding failed: {e}")
        return None, None, None

    # --- Step 2: Places API (Using rect Bounding Box and SUPPORTED Categories) ---
    places_url = "https://api.geoapify.com/v2/places"

    # *** CATEGORY FIX: Using only supported categories from the error message ***
    categories = (
        "commercial.shopping_mall,commercial.department_store,commercial.supermarket,"
        "education.school,education.college,education.university,"
        "healthcare.hospital,healthcare.clinic_or_praxis,healthcare.pharmacy,"
        "catering.restaurant,catering.cafe"
    )

    # Calculate bounding box corners (Lon1, Lat1, Lon2, Lat2)
    RECT_OFFSET_DEGREE = 0.1  # Using the constant defined earlier
    lon1 = lon - RECT_OFFSET_DEGREE
    lat1 = lat - RECT_OFFSET_DEGREE
    lon2 = lon + RECT_OFFSET_DEGREE
    lat2 = lat + RECT_OFFSET_DEGREE

    filter_rect = f"rect:{lon1:.4f},{lat1:.4f},{lon2:.4f},{lat2:.4f}"

    places_params = {
        "categories": categories,
        "filter": filter_rect,
        "limit": 50,
        "apiKey": GEO_API_KEY
    }

    nearby_results = []
    try:
        places_response = requests.get(places_url, params=places_params)

        if places_response.status_code != 200:
            st.error(f"Geoapify Places API Error: Status {places_response.status_code}. Request was rejected.")
            st.error("The API key is likely the issue if this error persists, as parameters are now correct.")
            return lat, lon, nearby_results

        places_data = places_response.json()

        for feature in places_data.get('features', []):
            props = feature['properties']
            nearby_results.append({
                "name": props.get('name', 'N/A'),
                "categories": [c.split('.')[-1] for c in props.get('categories', []) if c],
                "distance_m": props.get('distance'),
                "lat": feature['geometry']['coordinates'][1],
                "lon": feature['geometry']['coordinates'][0]
            })
    except Exception as e:
        st.error(f"Places lookup failed: {e}")

    return lat, lon, nearby_results


def fetch_nearby_places_for_coordinates(lat, lon, radius_m=500):
    """
    Fetch nearby places for given coordinates using Geoapify Places API.

    Args:
        lat: Latitude
        lon: Longitude
        radius_m: Search radius in meters (default 500m)

    Returns:
        List of nearby places with name, categories, distance, and coordinates
    """
    GEO_API_KEY = st.secrets.get("GEOAPIFY_API_KEY")
    if not GEO_API_KEY:
        return []

    places_url = "https://api.geoapify.com/v2/places"

    # Categories for nearby amenities
    categories = (
        "commercial.shopping_mall,commercial.department_store,commercial.supermarket,"
        "education.school,education.college,education.university,"
        "healthcare.hospital,healthcare.clinic_or_praxis,healthcare.pharmacy,"
        "catering.restaurant,catering.cafe"
    )

    # Use circular filter with radius
    filter_circle = f"circle:{lon:.6f},{lat:.6f},{radius_m}"

    places_params = {
        "categories": categories,
        "filter": filter_circle,
        "limit": 20,  # Limit to 20 places per property to avoid too many API calls
        "apiKey": GEO_API_KEY
    }

    nearby_results = []
    try:
        places_response = requests.get(places_url, params=places_params, timeout=10)

        if places_response.status_code != 200:
            return nearby_results  # Return empty list on error

        places_data = places_response.json()

        for feature in places_data.get('features', []):
            props = feature['properties']
            nearby_results.append({
                "name": props.get('name', 'N/A'),
                "categories": [c.split('.')[-1] for c in props.get('categories', []) if c],
                "distance_m": props.get('distance'),
                "lat": feature['geometry']['coordinates'][1],
                "lon": feature['geometry']['coordinates'][0]
            })
    except Exception as e:
        # Silently fail for individual properties to not clutter the UI
        pass

    return nearby_results


# -----------------------------------------
# 7. Property Listings (API MARKET Integration - FINAL ROBUST PARSING)
# -----------------------------------------
def fetch_property_listings_market(location_name, parsed_reqs, center_lat, center_lon):
    st.info(f"Fetching **real** listings for {location_name} via API Market...")

    # Build request URL using the simplified, proven parameters
    lat_long_param = f"{center_lat:.4f}%2C{center_lon:.4f}"

    url = (
        f"{APIMARKET_BASE_URL}?"
        f"lat_long={lat_long_param}&"
        f"distance_km=4"
    )

    headers = {
        "accept": "application/json",
        "x-api-market-key": APIMARKET_KEY,
    }

    listings = []

    try:
        #st.code(f"API Market Request URL:\n{url}", language='http')

        response = requests.get(url, headers=headers, timeout=30)

        if response.status_code != 200:
            st.error(f"API Market Request Failed! Status: {response.status_code}. Response: {response.text}")
            return []

        data = response.json()

        # --- FIX: DIRECTLY EXTRACT PROPERTIES AND IGNORE 'success' FLAG ---
        # The structure is: { "success": true, "data": { "propertiesByLocality": {...} } }
        # There is no 'root' key - access 'data' directly at the top level

        # Try both known paths to get the properties list
        properties = data.get('data', {}).get('allProperties', [])
        if not properties:
            # Flatten propertiesByLocality
            properties = [item for sublist in
                          data.get('data', {}).get('propertiesByLocality', {}).values() for item in
                          sublist]

        if not properties:
            st.warning("No properties found after checking both listing arrays.")
            return []

        # 3. Map the API's fields to your application's schema (Using confirmed keys)
        # Limit to first 5 properties to avoid too many API calls
        properties_to_process = properties[:5]

        progress_bar = st.progress(0)
        status_text = st.empty()

        for idx, item in enumerate(properties_to_process):
            if not isinstance(item, dict): continue

            # Update progress
            progress = (idx + 1) / len(properties_to_process)
            progress_bar.progress(progress)
            status_text.text(f"Processing property {idx + 1}/{len(properties_to_process)}...")

            # Extract price and format as monthly rent
            price_in_rupees = item.get('price') or 0
            price_per_month = f"Rs. {price_in_rupees:,}/month" if price_in_rupees > 0 else "Price on request"

            # Extract BHK number from strings like "BHK2", "RK1", "BHK1"
            bhk_str = item.get('bhk', '')
            bedrooms = 0
            if bhk_str:
                # Extract number from strings like "BHK2", "RK1", "BHK1"
                match = re.search(r'(\d+)', str(bhk_str))
                if match:
                    bedrooms = int(match.group(1))
                # Handle RK (Room Kitchen) as 1 bedroom
                elif 'RK' in str(bhk_str).upper():
                    bedrooms = 1

            # Parse coordinates
            item_lat = item.get('googleMapsPin', '0,0').split(',')[0] if item.get('googleMapsPin') else center_lat
            item_lon = item.get('googleMapsPin', '0,0').split(',')[1] if item.get('googleMapsPin') else center_lon

            # Convert to float safely
            try:
                item_lat = float(item_lat)
            except:
                item_lat = center_lat
            try:
                item_lon = float(item_lon)
            except:
                item_lon = center_lon

            # Convert timestamp to readable date
            available_from = "Available now"
            available_from_ts = item.get('availableFrom')
            if available_from_ts:
                try:
                    # Handle both string and numeric timestamps
                    ts = int(str(available_from_ts).strip('"'))
                    # Convert from milliseconds to seconds if needed
                    if ts > 1e12:
                        ts = ts / 1000
                    dt = datetime.fromtimestamp(ts)
                    available_from = dt.strftime("%d %b %Y")
                except (ValueError, OSError):
                    available_from = "Available soon"

            # Generate property ID from URL or use index
            property_id = item.get('fullUrl', '').split('/')[-1] if item.get('fullUrl') else f"prop_{idx}"

            # Fetch nearby places for this property
            status_text.text(f"Fetching nearby places for property {idx + 1}...")
            nearby_places = fetch_nearby_places_for_coordinates(item_lat, item_lon, radius_m=500)

            listings.append({
                "id": property_id,
                "price_per_month": price_per_month,
                "price_rupees": price_in_rupees,  # Keep for sorting/filtering
                "area_sqft": item.get('propertySize', 0),
                "bedrooms": bedrooms,
                "bathrooms": item.get('bath', 0),
                "builder": item.get('siteName') or item.get('ownerName', 'Owner'),
                "status": available_from,
                "description": item.get('title', 'Property in ' + item.get('location', 'Unknown')),
                "address": item.get('address', ''),
                "location": item.get('location', ''),
                "parking": item.get('parking', 'NONE'),
                "floor": f"{item.get('floor', 'N/A')}/{item.get('totalFloor', 'N/A')}",
                "url": item.get('fullUrl', ''),
                "googleMapsPin": item.get('googleMapsPin', ''),
                "lat": item_lat,
                "lon": item_lon,
                "nearby_places": nearby_places
            })

        # Clear progress indicators
        progress_bar.empty()
        status_text.empty()

        st.success(f"Successfully retrieved and mapped {len(listings)} live properties.")
        return listings

    except Exception as e:
        st.error(f"Error processing API Market data: {e}")
        return []


# -----------------------------------------
# 8. Live Weather and Air Quality Index (OpenWeatherMap)
# -----------------------------------------
def fetch_weather_and_aqi(lat, lon):
    BASE_URL = "https://api.openweathermap.org/data/2.5"
    weather_data, aqi_data = {}, {}

    try:
        # --- Fetch Weather ---
        weather_params = {"lat": lat, "lon": lon, "appid": OWM_KEY, "units": "metric"}
        weather_resp = requests.get(f"{BASE_URL}/weather", params=weather_params)
        if weather_resp.status_code == 200:
            data = weather_resp.json()
            weather_data = {
                "temp_c": data['main']['temp'],
                "conditions": data['weather'][0]['description'].capitalize(),
                "humidity": data['main']['humidity']
            }

        # --- Fetch Air Pollution Index (AQI) ---
        aqi_params = {"lat": lat, "lon": lon, "appid": OWM_KEY}
        aqi_resp = requests.get(f"{BASE_URL}/air_pollution", params=aqi_params)

        if aqi_resp.status_code == 200:
            data = aqi_resp.json()
            aqi_val = data['list'][0]['main']['aqi']
            aqi_map = {1: "Good", 2: "Fair", 3: "Moderate", 4: "Poor", 5: "Very Poor"}
            aqi_data = {
                "aqi_index": aqi_val,
                "aqi_status": aqi_map.get(aqi_val, "Unknown"),
                "pm2_5": data['list'][0]['components']['pm2_5'],
                "pm10": data['list'][0]['components']['pm10']
            }
    except Exception as e:
        st.error(f"Weather/AQI API call failed: {e}")

    return weather_data, aqi_data


# -----------------------------------------
# 8.5. Text-to-Speech Functions
# -----------------------------------------
def generate_speech_text(location_name, num_properties, weather_data, aqi_data):
    """
    Generate energetic and dynamic speech text summarizing property search results and weather information.
    Optimized for Indian location names pronunciation with more energy and faster pace.
    """
    speech_parts = []

    # Introduction with location and property count - more energetic
    if num_properties > 0:
        speech_parts.append(
            f"For {location_name}, we found {num_properties} "
            f"propert{'y' if num_properties == 1 else 'ies'} available! "
            f"Check your screen to view and compare them now."
        )
    else:
        speech_parts.append(
            f"Unfortunately, for {location_name}, no properties were found. "
            f"Please try searching a different location."
        )

    # Weather information - more dynamic and energetic
    if weather_data or aqi_data:
        speech_parts.append("Here's the current weather and air quality update!")

        if weather_data:
            temp = weather_data.get('temp_c', 'N/A')
            conditions = weather_data.get('conditions', 'N/A')
            humidity = weather_data.get('humidity', 'N/A')

            # Format temperature for natural speech
            if isinstance(temp, (int, float)):
                temp_str = f"{int(temp)}" if temp == int(temp) else f"{temp:.1f}"
            else:
                temp_str = str(temp)

            speech_parts.append(
                f"Temperature: {temp_str} degrees Celsius. "
                f"Conditions: {conditions.lower()}. "
                f"Humidity: {humidity} percent."
            )

        if aqi_data:
            aqi_status = aqi_data.get('aqi_status', 'N/A')
            pm2_5 = aqi_data.get('pm2_5', 'N/A')
            pm10 = aqi_data.get('pm10', 'N/A')

            # Format PM values for natural speech
            if isinstance(pm2_5, (int, float)):
                pm2_5_str = f"{int(pm2_5)}" if pm2_5 == int(pm2_5) else f"{pm2_5:.1f}"
            else:
                pm2_5_str = str(pm2_5)

            if isinstance(pm10, (int, float)):
                pm10_str = f"{int(pm10)}" if pm10 == int(pm10) else f"{pm10:.1f}"
            else:
                pm10_str = str(pm10)

            speech_parts.append(
                f"Air Quality Index: {aqi_status.lower()}. "
                f"PM 2.5: {pm2_5_str} micrograms per cubic meter. "
                f"PM 10: {pm10_str} micrograms per cubic meter."
            )

    return " ".join(speech_parts)


def text_to_speech(text, lang='en', slow=False, tld='com'):
    """
    Convert text to speech using gTTS and return audio bytes.
    Uses Indian English domain for better pronunciation of Indian location names.
    Configured for faster, more energetic speech.

    Args:
        text: Text to convert to speech
        lang: Language code (default 'en' for English)
        slow: Whether to speak slowly (default False for faster speech)
        tld: Top-level domain for TTS service ('com' for US English, 'co.in' for Indian English)

    Returns:
        Audio bytes (MP3 format)
    """
    try:
        # Use faster speech (slow=False) and try Indian English domain first
        # for better Indian location pronunciation
        try:
            tts = gTTS(text=text, lang=lang, slow=slow, tld='co.in')
        except:
            # Fallback to default TTS
            tts = gTTS(text=text, lang=lang, slow=slow, tld=tld)

        # Save to bytes buffer
        audio_buffer = io.BytesIO()
        tts.write_to_fp(audio_buffer)
        audio_buffer.seek(0)

        return audio_buffer.read()
    except Exception as e:
        st.error(f"TTS generation failed: {e}")
        return None


# -----------------------------------------
# 9. Map Display (Using st.map)
# -----------------------------------------
def display_st_map(lat, lon, nearby_places):
    center_df = pd.DataFrame({'lat': [lat], 'lon': [lon], 'name': ['Target Location']})
    if nearby_places:
        nearby_df = pd.DataFrame([
            {'lat': p['lat'], 'lon': p['lon'], 'name': p['name']}
            for p in nearby_places
        ])
        map_data = pd.concat([center_df, nearby_df])
    else:
        map_data = center_df
    st.map(map_data, zoom=11, use_container_width=True)


# -----------------------------------------
# 10. Streamlit UI (Final Integration)
# -----------------------------------------
st.title("🏡 AI Property Assistant — Groq Pipeline (India Optimized)")
st.write("Record a short voice message describing the property you want.")

audio_file = st.audio_input("Record your requirement (Browser/Device)")

if audio_file:
    st.write("---")
    st.subheader("Processing Steps")

    audio_path = None
    tmp_audio_path = None  # For TTS audio file
    try:
        # Create temporary WAV file for transcription
        # Works on Streamlit Cloud - temp files persist during session
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav", dir=tempfile.gettempdir()) as tmp:
            tmp.write(audio_file.read())
            audio_path = tmp.name

        # 1. Transcribe & 2. Correct
        transcript = transcribe_audio(audio_path)
        st.subheader("1. Raw Transcript")
        st.write(f"*{transcript}*")
        corrected = correct_place_names(transcript)
        st.subheader("2. Location-Corrected Text")
        st.code(corrected)

        # 3. Extract JSON
        parsed, raw_output = extract_json(corrected)

        # Removed: Structured Property Requirement display
        if parsed is None:
            st.error("JSON parsing failed.")
        else:
            # st.success("Extraction Successful")  # Hidden
            # st.json(parsed)  # Hidden

            locations_list = parsed.get('locations') or ['an unspecified area']
            deal_type = parsed.get('deal_type') or 'find'
            summary = f"You are looking to **{deal_type}** a property in **{', '.join(locations_list)}**."
            st.subheader("3. Human-friendly Summary")
            st.write(summary)

            # --- API INTEGRATION START ---

            lat, lon = None, None
            location_to_search = locations_list[0] if locations_list and locations_list[0] not in [None,
                                                                                                   'an unspecified area'] else None

            if location_to_search:

                # 5. Locality Insights (hidden from display but still fetching data)
                # Removed: Locality Insights display section
                with st.spinner(f'Fetching coordinates for {location_to_search}...'):
                    lat, lon, nearby_places = fetch_location_insights(location_to_search)

                # Removed: Geocoding success message, interactive map, and nearby amenities display
                # if lat and lon:
                #     st.success(f"Geocoding Successful: Latitude {lat:.4f}, Longitude {lon:.4f}")
                #     st.subheader("Interactive Map (Location & Nearby Points)")
                #     display_st_map(lat, lon, nearby_places)
                #     ... (nearby amenities display removed)

                # 4. Weather & Pollution
                st.subheader("4. Live Weather & Pollution Index (OpenWeatherMap)")
                weather, aqi = {}, {}  # Initialize to empty dicts
                if lat and lon:
                    with st.spinner('Fetching live weather and AQI data...'):
                        weather, aqi = fetch_weather_and_aqi(lat, lon)

                    if weather or aqi:
                        col1, col2 = st.columns(2)
                        if weather:
                            with col1:
                                st.markdown(f"**🌡️ Temp:** {weather.get('temp_c', 'N/A'):.1f}°C")
                                st.markdown(f"**💧 Humidity:** {weather.get('humidity', 'N/A')}%")
                                st.markdown(f"**☀️ Conditions:** {weather.get('conditions', 'N/A')}")
                        if aqi:
                            with col2:
                                st.markdown(
                                    f"**💨 AQI:** {aqi.get('aqi_index', 'N/A')} ({aqi.get('aqi_status', 'N/A')})")
                                st.markdown(f"**PM2.5:** {aqi.get('pm2_5', 'N/A'):.1f} μg/m³")
                                st.markdown(f"**PM10:** {aqi.get('pm10', 'N/A'):.1f} μg/m³")
                    else:
                        st.info("Could not fetch live weather or AQI data.")

                        # 5. Property Listings (API Market Integration)
                st.subheader("5. Property Listings Found (Real Data via API Market)")

                listings = fetch_property_listings_market(
                    location_to_search, parsed, lat, lon
                )

                if listings:
                    st.success(f"Found {len(listings)} matching properties in the area.")

                    df_properties = pd.DataFrame(listings)

                    st.markdown("**Map of All Found Properties:**")
                    # Use st.map directly with the properties DataFrame
                    st.map(df_properties[['lat', 'lon']], zoom=12, use_container_width=True)

                    # Display individual property cards with amenities drill-down
                    for prop in listings:
                        with st.expander(
                                f"🏡 {prop.get('description', 'Property')} - {prop.get('price_per_month', 'Price on request')}"):
                            # Main details in columns
                            col1, col2, col3 = st.columns(3)
                            with col1:
                                st.metric("BHK", prop.get('bedrooms', 0))
                            with col2:
                                st.metric("Area", f"{prop.get('area_sqft', 0)} sqft")
                            with col3:
                                st.metric("Bathrooms", prop.get('bathrooms', 0))

                            # Additional details
                            st.markdown("---")
                            col4, col5, col6 = st.columns(3)
                            with col4:
                                st.markdown(f"**📍 Location:** {prop.get('location', 'N/A')}")
                            with col5:
                                st.markdown(f"**🏢 Floor:** {prop.get('floor', 'N/A')}")
                            with col6:
                                st.markdown(f"**🚗 Parking:** {prop.get('parking', 'N/A').replace('_', ' ').title()}")

                            if prop.get('address'):
                                st.markdown(f"**📍 Address:** {prop.get('address')}")

                            if prop.get('googleMapsPin'):
                                st.markdown(f"**🗺️ Coordinates:** {prop.get('googleMapsPin')}")

                            st.markdown(f"**📅 Available From:** {prop.get('status', 'N/A')}")
                            st.markdown(f"**👤 Listed By:** {prop.get('builder', 'N/A')}")

                            # Display nearby places for this property
                            nearby_places = prop.get('nearby_places', [])
                            if nearby_places:
                                st.markdown("---")
                                st.markdown("**📍 Nearby Amenities (within 500m):**")
                                category_map = {}
                                for place in nearby_places:
                                    for cat in place.get('categories', []):
                                        if cat not in category_map:
                                            category_map[cat] = []
                                        distance_str = f"{place['distance_m']}m away" if place.get(
                                            'distance_m') else "nearby"
                                        category_map[cat].append(f"{place['name']} ({distance_str})")

                                if category_map:
                                    for cat, items in category_map.items():
                                        with st.expander(f"📍 {cat.capitalize()} ({len(items)} found)"):
                                            st.write("* " + "\n* ".join(items))
                                else:
                                    st.info("No categorized amenities found nearby.")
                            else:
                                st.markdown("---")
                                st.info("📍 Nearby amenities information not available for this property.")

                            if prop.get('url'):
                                st.markdown(f"**🔗 [View Details]({prop.get('url')})**")

                    # 8. Text-to-Speech Summary
                    st.subheader("🔊 Audio Summary")
                    with st.spinner('Generating audio summary...'):
                        # Generate speech text
                        speech_text = generate_speech_text(
                            location_name=location_to_search,
                            num_properties=len(listings),
                            weather_data=weather if lat and lon else {},
                            aqi_data=aqi if lat and lon else {}
                        )

                        # Convert to speech (using Indian English, faster and more energetic)
                        audio_bytes = text_to_speech(speech_text, lang='en', slow=False)

                        if audio_bytes:
                            # Save to temporary file for playback
                            # Works on Streamlit Cloud - temp files persist during session
                            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3",
                                                             dir=tempfile.gettempdir()) as tmp_audio:
                                tmp_audio.write(audio_bytes)
                                tmp_audio_path = tmp_audio.name

                            st.success("Audio summary generated successfully!")

                            # Create HTML audio player with autoplay and 1.25x speed
                            import base64

                            audio_base64 = base64.b64encode(audio_bytes).decode('utf-8')
                            audio_html = f"""
                                    <audio id="tts_audio" controls autoplay style="width: 100%;">
                                        <source src="data:audio/mp3;base64,{audio_base64}" type="audio/mpeg">
                                        Your browser does not support the audio element.
                                    </audio>
                                    <script>
                                        document.addEventListener('DOMContentLoaded', function() {{
                                            const audio = document.getElementById('tts_audio');
                                            if (audio) {{
                                                audio.playbackRate = 1.25;
                                                audio.play();
                                            }}
                                        }});
                                        // Also set playback rate when audio is loaded
                                        window.addEventListener('load', function() {{
                                            const audio = document.getElementById('tts_audio');
                                            if (audio) {{
                                                audio.playbackRate = 1.25;
                                            }}
                                        }});
                                    </script>
                                    """
                            st.components.v1.html(audio_html, height=80)

                            # Also provide standard audio player as fallback
                            st.markdown("**Listen to the summary (playing at 1.25x speed):**")

                            # Display the text that was spoken
                            with st.expander("📝 View spoken text"):
                                st.write(speech_text)

                            # Clean up temp file after a delay (Streamlit will handle this)
                            # Note: In production, you might want to use a more sophisticated cleanup
                        else:
                            st.warning("Could not generate audio summary. Please try again.")

                else:
                    st.warning("Could not generate property listings.")
            else:
                st.info("Please specify a location in your recording to get all features.")

    except Exception as e:
        st.error(f"An unexpected error occurred during pipeline execution: {e}")
    finally:
        # Cleanup temporary files (works on Streamlit Cloud)
        # Files are automatically cleaned when session ends, but we clean up explicitly
        try:
            if audio_path and os.path.exists(audio_path):
                os.remove(audio_path)
                # Don't show cleanup message in production to reduce clutter
                # st.write(f"Cleaned up temporary file: {audio_path}")
        except Exception as e:
        # Silently handle cleanup errors (file might already be removed)
            pass

    # Note: TTS audio file (tmp_audio_path) is kept for playback during session
    # Streamlit Cloud will clean it up automatically when session ends