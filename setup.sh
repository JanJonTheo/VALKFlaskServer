#!/bin/bash

echo "🔍 Checking Docker setup..."

# Check if Docker Compose is available
if ! command -v docker-compose &> /dev/null; then
    echo "❌ docker-compose not found. Please install Docker Compose."
    exit 1
fi

echo "✅ Docker Compose found."

# Check if .env files exist
if [ ! -f "VALKFlaskServer/.env" ]; then
    echo "⚠️  VALKFlaskServer/.env not found. Please create it from the template."
fi

if [ ! -f "VALKStreamlitDashboard/.env" ]; then
    echo "⚠️  VALKStreamlitDashboard/.env not found. Please create it from the template."
fi

echo "🚀 Starting services..."
docker-compose up -d

echo "⏳ Waiting for services to start..."
sleep 10

echo "📊 Checking service status..."
docker-compose ps

echo "📱 Services should be available at:"
echo "   Flask API: http://localhost:5000"
echo "   Streamlit Dashboard: http://localhost:8501"

echo "🔍 To check logs:"
echo "   docker-compose logs flaskserver"
echo "   docker-compose logs streamlitdashboard"

echo "🛑 To stop services:"
echo "   docker-compose down"
