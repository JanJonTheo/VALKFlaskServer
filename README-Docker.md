# VALK Elite Dangerous BGS Management System

This project consists of two main components:
- **VALKFlaskServer**: REST API backend for BGS data management
- **VALKStreamlitDashboard**: Web dashboard for data visualization

## Quick Start with Docker

1. **Create environment files**:
   ```bash
   cp VALKFlaskServer/.env-template VALKFlaskServer/.env
   cp VALKStreamlitDashboard/.env-template VALKStreamlitDashboard/.env
   ```
   Edit both `.env` files with your configuration.

2. **Start the services**:
   ```bash
   docker-compose up -d
   ```

3. **Access the applications**:
   - Flask API: http://localhost:5000
   - Streamlit Dashboard: http://localhost:8501

## First Run Setup

On the first execution, the system automatically:
- Creates the SQLite database in a persistent Docker volume
- Sets up all database tables
- Creates an admin user (username: `admin`, password: `passAdmin`)

## Database Persistence

The SQLite database is stored in a Docker volume named `flask_db_data`, ensuring data persists between container restarts and updates.

## Useful Commands

- View logs: `docker-compose logs -f`
- Stop services: `docker-compose down`
- Rebuild containers: `docker-compose up --build`
- Access database volume: The database is available at `/var/lib/docker/volumes/ed_flask_db_data/_data/` on Linux systems

## Manual Setup

For manual installation instructions, see the README files in each service directory.

## Default Admin Credentials

- Username: `admin`
- Password: `passAdmin`

**⚠️ Remember to change the default admin password after first login!**
