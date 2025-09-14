#!/bin/bash

# Check if tenant database exists and if users table is missing
echo "🔧 Checking tenant database setup..."

# Create database directory if it doesn't exist
mkdir -p /app/db

# Always check if users table exists in tenant database
if [ -f "/app/db/bgs_data_CIU.db" ]; then
    # Check if users table exists
    USERS_TABLE_EXISTS=$(python -c "import sqlite3; conn = sqlite3.connect('/app/db/bgs_data_CIU.db'); cursor = conn.cursor(); cursor.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name='users'\"); result = cursor.fetchone(); conn.close(); print('1' if result else '0')")
    
    if [ "$USERS_TABLE_EXISTS" = "0" ]; then
        echo "� Users table missing. Creating users table and admin user..."
        python setup_db.py
        python setup_users.py
        echo "✅ User setup completed!"
    else
        echo "👤 Users table found. Checking if admin user exists..."
        # Check if admin user exists, if not create it
        python setup_users.py
    fi
else
    echo "📊 Tenant database not found. This should be created automatically by the app."
fi

# Start the EDDN client in the background
echo "📡 Starting EDDN client in background..."
python eddn_client.py &

# Start the Flask application
echo "🚀 Starting Flask server..."
exec python app.py
