#!/bin/bash

echo "🧪 Testing VALK Docker Setup..."
echo ""

# Test Flask server root endpoint
echo "1. Testing Flask server root endpoint..."
FLASK_RESPONSE=$(curl -s http://localhost:5000/)
if echo "$FLASK_RESPONSE" | grep -q "VALK Flask Server is running"; then
    echo "   ✅ Flask server root endpoint working"
else
    echo "   ❌ Flask server root endpoint failed"
    echo "   Response: $FLASK_RESPONSE"
fi

# Test Flask discovery endpoint
echo ""
echo "2. Testing Flask discovery endpoint..."
DISCOVERY_RESPONSE=$(curl -s http://localhost:5000/discovery)
if echo "$DISCOVERY_RESPONSE" | grep -q "VALK FlaskServer BGS Data API"; then
    echo "   ✅ Flask discovery endpoint working"
else
    echo "   ❌ Flask discovery endpoint failed"
fi

# Test network connectivity from Streamlit to Flask
echo ""
echo "3. Testing network connectivity from Streamlit to Flask..."
NETWORK_TEST=$(docker exec ed-streamlitdashboard-1 python -c "import requests; print(requests.get('http://flaskserver:5000/').status_code)" 2>/dev/null)
if [ "$NETWORK_TEST" = "200" ]; then
    echo "   ✅ Network connectivity working"
else
    echo "   ❌ Network connectivity failed"
    echo "   Status code: $NETWORK_TEST"
fi

# Test API endpoint with authentication requirement
echo ""
echo "4. Testing API endpoint (expecting 401 - unauthorized)..."
API_TEST=$(docker exec ed-streamlitdashboard-1 python -c "import requests; import os; print(requests.get(os.getenv('API_BASE') + 'summary/leaderboard').status_code)" 2>/dev/null)
if [ "$API_TEST" = "401" ]; then
    echo "   ✅ API endpoint responding correctly (401 - needs auth)"
else
    echo "   ❌ API endpoint unexpected response"
    echo "   Status code: $API_TEST"
fi

echo ""
echo "🎯 Summary:"
echo "   Flask server: http://localhost:5000"
echo "   Streamlit dashboard: http://localhost:8501"
echo "   Default admin login: admin / passAdmin"
echo ""
echo "📝 Next steps:"
echo "   1. Open http://localhost:8501 in your browser"
echo "   2. Login with admin / passAdmin"
echo "   3. Test the dashboard functionality"
