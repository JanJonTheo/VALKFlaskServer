#!/bin/bash

# Check if database exists, if not run setup scripts
if [ ! -f "/app/instance/bgs_data.db" ]; then
    echo "🔧 Database not found. Running initial setup..."
    
    # Create instance directory if it doesn't exist
    mkdir -p /app/instance
    
    # Run database setup
    echo "📊 Creating database tables..."
    python setup_db.py
    
    # Run user setup
    echo "👤 Setting up admin user..."
    python setup_users.py
    
    echo "✅ Initial setup completed!"
else
    echo "📊 Database found. Skipping setup."
fi

# Start the Flask application
echo "🚀 Starting Flask server..."
exec python app.py
