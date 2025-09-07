![VALT Logo](static/VALK_logo.jpg)

# Flask API for BGS-Tally Data

## Project Description

This project provides a RESTful API for receiving, storing, and processing Background Simulation (BGS) and Thargoid War activity data for the game Elite Dangerous. It is designed to integrate with BGS-Tally and other tools to support faction management and war tracking.

## Features

- Receive BGS and Thargoid War activity data via POST and PUT requests
- Store incoming data in a database
- Send notifications to Discord via webhooks
- Scheduled shoutouts and tick monitoring
- Conflict detection and reporting

## API Endpoints

The following API endpoints are available. Their specification is based on the description in `bgs_tally_openapi.json`, which forms the basis for development. All API functions are also included in `postman_collection.json` for use with the Postman software.

**Events**

- `POST /events` : Submit one or more Journal or Synthetic Events

**Activities**

- `PUT /activities` : Add or update activity for a given tick

**Summary APIs**

- `GET /api/summary/market-events`
- `GET /api/summary/missions-completed`
- `GET /api/summary/missions-failed`
- `GET /api/summary/bounty-vouchers`
- `GET /api/summary/combat-bonds`
- `GET /api/summary/influence-by-faction`
- `GET /api/summary/influence-eic`
- `GET /api/summary/exploration-sales`
- `GET /api/summary/bounty-fines`
- `GET /api/bounty-vouchers`
- `GET /api/syntheticcz-summary`
- `GET /api/syntheticgroundcz-summary`

**Top 5 APIs**

- `GET /api/summary/top5/market-events`
- `GET /api/summary/top5/missions-completed`
- `GET /api/summary/top5/bounty-vouchers`
- `GET /api/summary/top5/combat-bonds`
- `GET /api/summary/top5/influence-eic`
- `GET /api/summary/top5/exploration-sales`

**Discord Integration**

- `POST /api/summary/discord/top5all`
- `POST /api/summary/discord/tick`
- `POST /api/summary/discord/syntheticcz`
- `POST /api/summary/discord/syntheticgroundcz`

**Database Tables**

- `GET /api/table/event`
- `GET /api/table/market_buy_event`
- `GET /api/table/activity`
- `GET /api/table/cmdr`
- `GET /api/table/objective`
- `GET /api/table/objective_target`
- `GET /api/table/objective_target_settlement`

**Leaderboard & Recruits**

- `GET /api/summary/leaderboard`
- `GET /api/summary/recruits`

**Objectives**

- `POST /api/objectives`
- `GET /api/objectives`
- `GET /api/objectives?system=Sol`
- `GET /api/objectives?faction=Federal Navy`
- `GET /api/objectives?active=true`
- `DELETE /api/objectives/<id>`
- `GET /objectives`
- `POST /objectives`
- `DELETE /objectives/<id>`

**Authentication**

- `POST /api/login`

**Debug & Sync**

- `POST /api/debug/tick-change`
- `POST /api/sync/cmdrs`

**Discovery & Health**

- `GET /discovery`

## Installation

### Option 1: Docker Compose (Recommended)

The easiest way to run both the Flask API and Streamlit dashboard together is using Docker Compose.

**Prerequisites:**

- Docker and Docker Compose installed
- Both `VALKFlaskServer` and `VALKStreamlitDashboard` repositories cloned as sibling directories

**Directory structure should be:**

```
parent-folder/
├── VALKFlaskServer/          # This repository
│   ├── docker-compose.yml   # Main Docker Compose file
│   ├── Dockerfile
│   └── ...
└── VALKStreamlitDashboard/   # Sibling repository
    ├── Dockerfile
    └── ...
```

**Setup:**

1. **Create environment files**:

   ```bash
   # In VALKFlaskServer directory
   cp .env-template .env
   # In VALKStreamlitDashboard directory
   cp ../VALKStreamlitDashboard/.env-template ../VALKStreamlitDashboard/.env
   ```

   Edit both `.env` files with your configuration.

2. **Start the services**:

   ```bash
   # From VALKFlaskServer directory
   docker-compose up -d
   ```

3. **Access the services**:
   - Flask API: <http://localhost:5000>
   - Streamlit Dashboard: <http://localhost:8501>

**First Run Setup:**
On the first execution, the system automatically:

- Creates the SQLite database in a persistent Docker volume
- Sets up database tables (`setup_db.py`)
- Creates the admin user (`setup_users.py`) - username: `admin`, password: `passAdmin`

**Useful Commands:**

```bash
# View logs
docker-compose logs -f

# Stop services
docker-compose down

# Rebuild containers
docker-compose up --build

# Test setup
./test_setup.ps1  # Windows
./test_setup.sh   # Linux/macOS
```

### Option 2: Manual Installation

1. **Clone the repository**

   ```bash
   git clone https://github.com/yourusername/VALKFlaskServer.git
   cd VALKFlaskServer
   ```

2. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

3. **Set up the database**

   ```bash
   python setup_db.py
   python setup_users.py
   ```

4. **Create a `.env` file for production**
   You can use the provided `.env-template` file as a starting point. Copy it and rename to `.env`:

   ```bash
   cp .env-template .env
   ```

   Then edit the values in `.env` to match your production environment.

5. **Run the server**

   ```bash
   python app.py
   ```

## Discord

Further informations you'll find on the VALK Discord Server https://discord.gg/JdRBJnNS

## Screenshots

[Dashboard](screenshots/Dashboard.MD)

## Credits

This project was developed by Cmdr JanJonTheo.

Docker Compose setup by daniele-liprandi

## Disclaimer

This project is not affiliated with or endorsed by Frontier Developments Inc., the creators of Elite Dangerous.

## Special Thanks

Special thanks to Aussi and Cmdr NavlGazr from BGS-Tally for their support and assistance.
