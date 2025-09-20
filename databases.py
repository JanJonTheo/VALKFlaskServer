import os
from models import db, System, Faction
from sqlalchemy.engine import make_url
from sqlalchemy import create_engine, inspect, text, Column, Integer, String, Boolean, MetaData, Table
import sqlalchemy
import logging
import json

# Lade Tenant-Konfiguration
TENANT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "tenant.json")
with open(TENANT_CONFIG_PATH, "r", encoding="utf-8") as f:
    TENANTS = json.load(f)

# Log-Verzeichnis sicherstellen
LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# Logger mit RotatingFileHandler, max. 128 MB, 10 Backups
logger = logging.getLogger("databases")
log_handler = logging.handlers.RotatingFileHandler(
    os.path.join(LOG_DIR, "databases.log"), maxBytes=128 * 1024 * 1024, backupCount=10
)
formatter = logging.Formatter('%(asctime)s %(levelname)s:%(name)s:%(message)s')
log_handler.setFormatter(formatter)
logger.addHandler(log_handler)
logger.setLevel(logging.INFO)
logger.propagate = False  # verhindert Weitergabe an Root-Logger


# Sicherstellen, dass die Tabelle protected_faction existiert
def ensure_protected_faction_table(db_uri):
    url = make_url(db_uri)
    engine = create_engine(db_uri)
    metadata = MetaData()
    inspector = inspect(engine)
    if "protected_faction" not in inspector.get_table_names():
        protected_faction = Table(
            "protected_faction", metadata,
            Column("id", Integer, primary_key=True),
            Column("name", String(128), unique=True, nullable=False),
            Column("webhook_url", String(256)),
            Column("description", String(128)),
            Column("protected", Boolean, default=True)
        )
        metadata.create_all(engine, tables=[protected_faction])
        logger.info(f"Tabelle 'protected_faction' wurde in {db_uri} angelegt.")
    else:
        logger.info(f"Tabelle 'protected_faction' existiert bereits in {db_uri}.")


# Initialisierung der Tenant-Datenbanken
def initialize_all_tenant_databases():
    """
    Prüft beim Start für alle Tenants, ob die SQLite-DB-Datei existiert.
    Falls nicht, wird sie samt Tabellenstruktur angelegt.
    Zusätzlich wird die Tabelle protected_faction angelegt, falls sie fehlt.
    """
    for tenant in TENANTS:
        db_uri = tenant.get("db_uri")
        if not db_uri:
            continue
        url = make_url(db_uri)
        if url.drivername == "sqlite" and url.database not in (None, "", ":memory:"):
            sqlite_file_path = url.database
            abs_path = os.path.abspath(sqlite_file_path)
            dir_name = os.path.dirname(abs_path)
            if dir_name and not os.path.exists(dir_name):
                os.makedirs(dir_name, exist_ok=True)
            if not os.path.exists(abs_path):
                engine = create_engine(db_uri, connect_args={"check_same_thread": False})
                with engine.begin() as conn:
                    db.Model.metadata.create_all(bind=conn)
                logger.info(f"Tenant-DB initialisiert: {abs_path}")
        # Update existing Tenant DB
        ensure_protected_faction_table(db_uri)


# Aktualisierung der Tenant-Datenbanken
def update_all_tenant_databases():
    """
    Aktualisiert für alle Tenants die Tabellenstruktur und Felder gemäß models.py.
    Fügt neue Felder/Tabellen hinzu, falls sie fehlen.
    Ergänzt fehlende Spalten in bestehenden Tabellen (nur für SQLite).
    """
    def get_existing_columns(engine, table_name):
        insp = inspect(engine)
        return set([col['name'] for col in insp.get_columns(table_name)])

    def get_model_columns(model):
        return {col.name: col for col in model.__table__.columns}

    for tenant in TENANTS:
        db_uri = tenant.get("db_uri")
        if not db_uri:
            continue
        url = make_url(db_uri)
        connect_args = {"check_same_thread": False} if url.drivername == "sqlite" else {}
        engine = create_engine(db_uri, connect_args=connect_args)
        with engine.begin() as conn:
            db.Model.metadata.create_all(bind=conn)
            if url.drivername == "sqlite":
                sys_existing = get_existing_columns(engine, "system")
                sys_model = get_model_columns(System)
                for col_name, col_obj in sys_model.items():
                    if col_name not in sys_existing:
                        col_type = str(col_obj.type)
                        alter_sql = f'ALTER TABLE system ADD COLUMN {col_name} {col_type}'
                        try:
                            conn.execute(sqlalchemy.text(alter_sql))
                            logger.info(f"Spalte '{col_name}' zu Tabelle 'system' ergänzt für Tenant: {db_uri}")
                        except Exception as e:
                            logger.warning(f"Fehler beim Ergänzen von Spalte '{col_name}' in 'system': {e}")

                fac_existing = get_existing_columns(engine, "faction")
                fac_model = get_model_columns(Faction)
                for col_name, col_obj in fac_model.items():
                    if col_name not in fac_existing:
                        col_type = str(col_obj.type)
                        alter_sql = f'ALTER TABLE faction ADD COLUMN {col_name} {col_type}'
                        try:
                            conn.execute(sqlalchemy.text(alter_sql))
                            logger.info(f"Spalte '{col_name}' zu Tabelle 'faction' ergänzt für Tenant: {db_uri}")
                        except Exception as e:
                            logger.warning(f"Fehler beim Ergänzen von Spalte '{col_name}' in 'faction': {e}")

        logger.info(f"Tenant-DB aktualisiert: {db_uri}")


# Löschen aller Aktivitätsdaten für alle Tenants
def delete_all_activity_data():
    """
    Löscht alle Datensätze aus den Tabellen activity, system und faction für alle Tenants.
    """
    from models import Activity, System, Faction
    for tenant in TENANTS:
        db_uri = tenant.get("db_uri")
        if not db_uri:
            continue
        url = make_url(db_uri)
        connect_args = {"check_same_thread": False} if url.drivername == "sqlite" else {}
        engine = create_engine(db_uri, connect_args=connect_args)
        from sqlalchemy.orm import sessionmaker
        Session = sessionmaker(bind=engine)
        session = Session()
        try:
            deleted_faction = session.query(Faction).delete()
            deleted_system = session.query(System).delete()
            deleted_activity = session.query(Activity).delete()
            session.commit()
            logger.info(f"Alle Datensätze aus activity, system und faction für Tenant {db_uri} gelöscht.")
        except Exception as e:
            session.rollback()
            logger.error(f"Fehler beim Löschen der Daten für Tenant {db_uri}: {e}")
        finally:
            session.close()
