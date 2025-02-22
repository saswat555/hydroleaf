#!/bin/bash
# Exit immediately if a command exits with a non-zero status.
set -e

# Ensure jq is installed for JSON parsing.
if ! command -v jq &> /dev/null
then
    echo "jq is required but not installed. Please install it (e.g., brew install jq) and re-run."
    exit 1
fi

# Base URL for your API endpoints
BASE_URL="http://localhost:8000/api/v1"

# Helper function to print test headers
function print_test() {
    echo ""
    echo "========== $1 =========="
}

# ------------------------------
# Health Endpoints (Positive Flows)
# ------------------------------
print_test "GET /health"
curl -s -X GET "$BASE_URL/health" -H "Content-Type: application/json" | jq .

print_test "GET /health/mqtt"
curl -s -X GET "$BASE_URL/health/mqtt" -H "Content-Type: application/json" | jq .

print_test "GET /health/database"
curl -s -X GET "$BASE_URL/health/database" -H "Content-Type: application/json" | jq .

print_test "GET /health/all"
curl -s -X GET "$BASE_URL/health/all" -H "Content-Type: application/json" | jq .

# ------------------------------
# Devices Endpoints
# ------------------------------
print_test "POST /devices/dosing (Create Dosing Device 1 - without dose_ml; expect negative dosing execution later)"
DEVICE1_RESPONSE=$(curl -s -X POST "$BASE_URL/devices/dosing" -H "Content-Type: application/json" -d '{
  "name": "Dosing Pump 1",
  "type": "dosing_unit",
  "mqtt_topic": "krishiverse/dosing/1",
  "location_description": "Greenhouse 1",
  "pump_configurations": [
    {
      "pump_number": 1,
      "chemical_name": "Nutrient A",
      "chemical_description": "Essential nutrients"
    }
  ]
}')
echo "$DEVICE1_RESPONSE" | jq .
DEVICE1=$(echo "$DEVICE1_RESPONSE" | jq '.id')

print_test "POST /devices/dosing (Create Dosing Device 2 - with dose_ml for positive dosing execution)"
DEVICE2_RESPONSE=$(curl -s -X POST "$BASE_URL/devices/dosing" -H "Content-Type: application/json" -d '{
  "name": "Dosing Pump 2",
  "type": "dosing_unit",
  "mqtt_topic": "krishiverse/dosing/2",
  "location_description": "Greenhouse 1",
  "pump_configurations": [
    {
      "pump_number": 1,
      "chemical_name": "Nutrient B",
      "chemical_description": "Advanced nutrients",
      "dose_ml": 50.0
    }
  ]
}')
echo "$DEVICE2_RESPONSE" | jq .
DEVICE2=$(echo "$DEVICE2_RESPONSE" | jq '.id')

print_test "POST /devices/sensor (Create Sensor Device)"
SENSOR_RESPONSE=$(curl -s -X POST "$BASE_URL/devices/sensor" -H "Content-Type: application/json" -d '{
  "name": "pH Sensor 1",
  "type": "ph_tds_sensor",
  "mqtt_topic": "krishiverse/sensor/1",
  "location_description": "Greenhouse 1",
  "sensor_parameters": {
    "unit": "pH",
    "range": "0-14"
  }
}')
echo "$SENSOR_RESPONSE" | jq .

print_test "GET /devices/discover"
curl -s -X GET "$BASE_URL/devices/discover" -H "Content-Type: application/json" | jq .

print_test "GET /devices/ (List All Devices)"
curl -s -X GET "$BASE_URL/devices/" -H "Content-Type: application/json" | jq .

print_test "GET /devices/{id} (Get details of Device 1)"
curl -s -X GET "$BASE_URL/devices/$DEVICE1" -H "Content-Type: application/json" | jq .

# ------------------------------
# Config Router Endpoints
# ------------------------------
print_test "GET /config/system-info"
curl -s -X GET "$BASE_URL/config/system-info" -H "Content-Type: application/json" | jq .

print_test "POST /config/dosing-profile (Negative: Non-existent device)"
curl -s -X POST "$BASE_URL/config/dosing-profile" -H "Content-Type: application/json" -d '{
  "device_id": 9999,
  "plant_name": "Tomato",
  "plant_type": "Vegetable",
  "growth_stage": "Seedling",
  "seeding_date": "2025-02-20T00:00:00Z",
  "target_ph_min": 5.5,
  "target_ph_max": 6.5,
  "target_tds_min": 600,
  "target_tds_max": 800,
  "dosing_schedule": {"morning": 50.0, "evening": 40.0}
}' | jq .

print_test "POST /config/dosing-profile (Positive for Device 1)"
PROFILE1_RESPONSE=$(curl -s -X POST "$BASE_URL/config/dosing-profile" -H "Content-Type: application/json" -d "{
  \"device_id\": $DEVICE1,
  \"plant_name\": \"Tomato\",
  \"plant_type\": \"Vegetable\",
  \"growth_stage\": \"Seedling\",
  \"seeding_date\": \"2025-02-20T00:00:00Z\",
  \"target_ph_min\": 5.5,
  \"target_ph_max\": 6.5,
  \"target_tds_min\": 600,
  \"target_tds_max\": 800,
  \"dosing_schedule\": {\"morning\": 50.0, \"evening\": 40.0}
}")
echo "$PROFILE1_RESPONSE" | jq .
PROFILE1=$(echo "$PROFILE1_RESPONSE" | jq '.id')

print_test "GET /config/dosing-profiles/{device_id} (For Device 1)"
curl -s -X GET "$BASE_URL/config/dosing-profiles/$DEVICE1" -H "Content-Type: application/json" | jq .

print_test "DELETE /config/dosing-profiles/9999 (Negative: Non-existent profile)"
curl -s -X DELETE "$BASE_URL/config/dosing-profiles/9999" -H "Content-Type: application/json" | jq .

# ------------------------------
# Dosing Router Endpoints (Pre-LLM execution)
# ------------------------------
print_test "POST /dosing/execute/{id} (Negative: Execute dosing on Device 1 missing dose_ml)"
curl -s -X POST "$BASE_URL/dosing/execute/$DEVICE1" -H "Content-Type: application/json" | jq .

print_test "POST /dosing/execute/{id} (Positive: Execute dosing on Device 2 with valid dose_ml)"
curl -s -X POST "$BASE_URL/dosing/execute/$DEVICE2" -H "Content-Type: application/json" | jq .

print_test "POST /dosing/cancel/{id} (Cancel dosing on Device 1)"
curl -s -X POST "$BASE_URL/dosing/cancel/$DEVICE1" -H "Content-Type: application/json" | jq .

print_test "POST /dosing/cancel/{id} (Cancel dosing on Device 2)"
curl -s -X POST "$BASE_URL/dosing/cancel/$DEVICE2" -H "Content-Type: application/json" | jq .

print_test "GET /dosing/history/{id} (Expected error due to prior import issue)"
curl -s -X GET "$BASE_URL/dosing/history/$DEVICE1" -H "Content-Type: application/json" | jq .

print_test "POST /dosing/profile (Alternate dosing profile creation for Device 1)"
PROFILE2_RESPONSE=$(curl -s -X POST "$BASE_URL/dosing/profile" -H "Content-Type: application/json" -d "{
  \"device_id\": $DEVICE1,
  \"plant_name\": \"Lettuce\",
  \"plant_type\": \"Leafy Green\",
  \"growth_stage\": \"Vegetative\",
  \"seeding_date\": \"2025-02-18T00:00:00Z\",
  \"target_ph_min\": 6.0,
  \"target_ph_max\": 7.0,
  \"target_tds_min\": 500,
  \"target_tds_max\": 700,
  \"dosing_schedule\": {\"morning\": 30.0, \"afternoon\": 20.0}
}")
echo "$PROFILE2_RESPONSE" | jq .

# ------------------------------
# LLM-Based Dosing Request Flow
# ------------------------------
print_test "POST /dosing/llm-request (Full LLM dosing flow)"
# Prepare sample sensor data and plant profile.
SENSOR_DATA='{"ph": 6.8, "tds": 450}'
PLANT_PROFILE='{"plant_name": "Cucumber", "plant_type": "Vegetable", "growth_stage": "Seedling", "seeding_date": "2025-02-20T00:00:00Z", "weather_locale": "Local"}'
curl -s -X POST "$BASE_URL/dosing/llm-request?device_id=$DEVICE2" \
     -H "Content-Type: application/json" \
     -d "{\"sensor_data\": $SENSOR_DATA, \"plant_profile\": $PLANT_PROFILE}" | jq .

# ------------------------------
# End of Test Suite
# ------------------------------
echo ""
echo "========================"
echo "Test suite execution complete."
