import zmq
import json
import logging
import logging.handlers
import zlib
from datetime import datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models_eddn import Base, EDDNMessage, Faction, Conflict, SystemInfo, Powerplay
import os

# Log-Verzeichnis sicherstellen
LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# Logger mit RotatingFileHandler, max. 128 MB, 10 Backups
logger = logging.getLogger("eddn_client")
log_handler = logging.handlers.RotatingFileHandler(
    os.path.join(LOG_DIR, "eddn_client.log"), maxBytes=128 * 1024 * 1024, backupCount=10
)
formatter = logging.Formatter('%(asctime)s %(levelname)s:%(name)s:%(message)s')
log_handler.setFormatter(formatter)
logger.addHandler(log_handler)
logger.setLevel(logging.INFO)

EDDN_URL = "tcp://eddn.edcd.io:9500"

# DB_URI from environment variable
DB_URI = os.getenv("EDDN_DATABASE", "sqlite:///db/bgs_data_eddn.db")

def cleanup_old_entries(session):
    """Löscht alle EDDNMessage-Einträge älter als 24h."""
    cutoff = datetime.utcnow() - timedelta(hours=24)
    deleted = session.query(EDDNMessage).filter(EDDNMessage.timestamp < cutoff).delete()
    if deleted:
        logger.info(f"{deleted} alte eddn_message-Einträge gelöscht.")



def save_system_related_data(session, data, eddn_message_id):
    msg = data.get("message", {})
    system_name = msg.get("StarSystem")
    now = datetime.utcnow()

    if not system_name:
        return

    # SystemInfo: Remove old, insert new
    session.query(SystemInfo).filter_by(system_name=system_name).delete()
    sysinfo = SystemInfo(
        eddn_message_id=eddn_message_id,
        system_name=system_name,
        controlling_faction=msg.get("SystemFaction", {}).get("Name"),
        controlling_power=msg.get("ControllingPower"),
        population=msg.get("Population"),
        security=msg.get("SystemSecurity"),
        government=msg.get("SystemGovernment"),
        allegiance=msg.get("SystemAllegiance"),
        updated_at=now
    )
    session.add(sysinfo)

    # Factions: Remove old, insert new only if data present
    session.query(Faction).filter_by(system_name=system_name).delete()
    factions = msg.get("Factions", [])
    if factions:
        for faction in factions:
            f = Faction(
                eddn_message_id=eddn_message_id,
                system_name=system_name,
                name=faction.get("Name"),
                influence=faction.get("Influence"),
                state=faction.get("FactionState"),
                recovering_states=faction.get("RecoveringStates"),
                active_states=faction.get("ActiveStates"),
                pending_states=faction.get("PendingStates"),
                updated_at=now
            )
            session.add(f)

    # Conflicts: Remove old, insert new only if data present
    session.query(Conflict).filter_by(system_name=system_name).delete()
    conflicts = msg.get("Conflicts", [])
    if conflicts:
        for conflict in conflicts:
            stake1 = conflict.get("Faction1", {}).get("Stake")
            stake2 = conflict.get("Faction2", {}).get("Stake")
            won_days1 = conflict.get("Faction1", {}).get("WonDays")
            won_days2 = conflict.get("Faction2", {}).get("WonDays")

            c = Conflict(
                eddn_message_id=eddn_message_id,
                system_name=system_name,
                faction1=conflict.get("Faction1", {}).get("Name"),
                faction2=conflict.get("Faction2", {}).get("Name"),
                stake1=stake1,
                stake2=stake2,
                won_days1=won_days1,
                won_days2=won_days2,
                status=conflict.get("Status"),
                war_type=conflict.get("WarType"),
                updated_at=now
            )
            session.add(c)

    # Powerplay: Remove old, insert new only if data present
    session.query(Powerplay).filter_by(system_name=system_name).delete()
    has_powerplay = "Powers" in msg or "PowerplayState" in msg
    if has_powerplay and (msg.get("Powers") or msg.get("PowerplayState")):
        p = Powerplay(
            eddn_message_id=eddn_message_id,
            system_name=system_name,
            power=msg.get("Powers") if isinstance(msg.get("Powers"), list) else [msg.get("Powers")] if msg.get("Powers") else [],
            powerplay_state=msg.get("PowerplayState"),
            control_progress=msg.get("PowerplayStateControlProgress"),
            reinforcement=msg.get("PowerplayStateReinforcement"),
            undermining=msg.get("PowerplayStateUndermining"),
            updated_at=now
        )
        session.add(p)

def defragment_database(engine):
    """Defragmentiert die SQLite-Datenbank für optimale Performance."""
    if "sqlite" in str(engine.url):
        logger.info("SQLite-Datenbank starte Defragmentierung (VACUUM)...")
        with engine.connect() as conn:
            conn.exec_driver_sql("VACUUM")
        logger.info("SQLite-Datenbank wurde defragmentiert (VACUUM ausgeführt).")

def main():
    # Ensure db directory exists
    os.makedirs("db", exist_ok=True)
    
    # DB-Session with basic SQLite settings
    engine = create_engine(
        DB_URI, 
        connect_args={"check_same_thread": False}
    )
    
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    # Defragmentierung beim Start
    defragment_database(engine)

    # ZMQ-Subscriber initialisieren
    context = zmq.Context()
    socket = context.socket(zmq.SUB)
    socket.connect(EDDN_URL)
    socket.setsockopt_string(zmq.SUBSCRIBE, "")

    logger.info("EDDN Client gestartet, wartet auf Nachrichten...")

    last_cleanup = datetime.utcnow()
    last_vacuum = datetime.utcnow()

    try:
        while True:
            msg = socket.recv()
            try:
                # EDDN-Nachrichten sind zlib-komprimiert
                decompressed = zlib.decompress(msg)
                data = json.loads(decompressed.decode("utf-8"))

                # Nur Location und FSDJump speichern
                message_type = data.get("message", {}).get("event", None)
                if message_type not in ("Location", "FSDJump"):
                    continue

                # EDDNMessage-Objekt erzeugen und speichern
                eddn_msg = EDDNMessage.from_eddn(data)
                session.add(eddn_msg)
                session.flush()  # Damit eddn_msg.id verfügbar ist

                # Systemdaten extrahieren und speichern, eddn_message_id übergeben
                save_system_related_data(session, data, eddn_msg.id)

                session.commit()
                #logger.info(f"EDDN-Nachricht gespeichert: {eddn_msg.schema_ref} @ {eddn_msg.timestamp}")

                # Bereinige alte Einträge alle 100 Nachrichten oder alle 10 Minuten
                if (datetime.utcnow() - last_cleanup).total_seconds() > 600 or session.query(EDDNMessage).count() % 100 == 0:
                    cleanup_old_entries(session)
                    last_cleanup = datetime.utcnow()

                # Defragmentiere die Datenbank alle 12 Stunden
                if (datetime.utcnow() - last_vacuum).total_seconds() > 43200:
                    defragment_database(engine)
                    last_vacuum = datetime.utcnow()
            except Exception as ex:
                logger.error(f"Fehler beim Verarbeiten/Speichern einer Nachricht: {ex}")
                session.rollback()
    except KeyboardInterrupt:
        logger.info("EDDN Client beendet.")
    finally:
        session.close()
        socket.close()
        context.term()

if __name__ == "__main__":
    main()
