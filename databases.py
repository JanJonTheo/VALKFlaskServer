import os
import logging
import logging.handlers
import json

import sqlalchemy
from sqlalchemy import create_engine, inspect, text, Column, Integer, String, Boolean, MetaData, Table
from sqlalchemy.engine import make_url

from models import (
    db,
    System, Faction,
    MissionCompletedEvent, MissionCompletedInfluence, MissionFailedEvent,
    MarketBuyEvent, MarketSellEvent,
    RedeemVoucherEvent,
    MultiSellExplorationDataEvent, SellExplorationDataEvent,
    ManualActivitySubmission,
    ColonisationAssistStatus, ColonisationDelivery,
    BGSEvalRun, BGSEvalResult
)
from dashboard_users import ensure_dashboard_schema
from redeem_voucher import (
    encode_factions,
    normalize_redeem_voucher_payload,
    parse_event_payload,
    redeem_voucher_factions,
)

# -----------------------------------------------------------------------------
# Tenant-Konfiguration
# -----------------------------------------------------------------------------
TENANT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "tenant.json")
with open(TENANT_CONFIG_PATH, "r", encoding="utf-8") as f:
    TENANTS = json.load(f)

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("databases")
log_handler = logging.handlers.RotatingFileHandler(
    os.path.join(LOG_DIR, "databases.log"), maxBytes=4 * 1024 * 1024, backupCount=10
)
formatter = logging.Formatter('%(asctime)s %(levelname)s:%(name)s:%(message)s')
log_handler.setFormatter(formatter)
logger.addHandler(log_handler)
logger.setLevel(logging.INFO)
logger.propagate = False


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _get_existing_columns(engine, table_name: str) -> set[str]:
    insp = inspect(engine)
    return set([col['name'] for col in insp.get_columns(table_name)])


def _get_model_columns(model) -> dict:
    return {col.name: col for col in model.__table__.columns}


def ensure_protected_faction_table_conn(conn, db_uri: str):
    """
    Legt 'protected_faction' an, falls sie fehlt.
    """
    try:
        insp = inspect(conn)
        if "protected_faction" in insp.get_table_names():
            logger.info(f"Tabelle 'protected_faction' existiert bereits in {db_uri}.")
            return

        metadata = MetaData()
        Table(
            "protected_faction", metadata,
            Column("id", Integer, primary_key=True),
            Column("name", String(128), unique=True, nullable=False),
            Column("webhook_url", String(256)),
            Column("description", String(128)),
            Column("protected", Boolean, default=True)
        )
        metadata.create_all(conn)
        logger.info(f"Tabelle 'protected_faction' sichergestellt für Tenant: {db_uri}")
    except Exception as e:
        logger.warning(f"ensure_protected_faction_table_conn failed for {db_uri}: {e}")


def ensure_bgs_eval_tables_and_indexes(conn, db_uri: str):
    """
    Phase 2: Ensure eval tables exist and indexes/unique constraints are present.

    Important:
      - db.Model.metadata.create_all(bind=conn) creates missing tables,
        but indexes on existing DBs are not always applied retroactively.
      - Therefore we enforce indexes via CREATE INDEX IF NOT EXISTS.
    """
    try:
        # Tables (safe repeated)
        db.Model.metadata.create_all(bind=conn)

        # EvalRun index
        conn.execute(sqlalchemy.text(
            "CREATE INDEX IF NOT EXISTS idx_bgs_eval_run_ticktime ON bgs_eval_run(ticktime);"
        ))

        # EvalResult unique + indexes
        conn.execute(sqlalchemy.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_bgs_eval_result "
            "ON bgs_eval_result(ticktime, system_name, faction);"
        ))
        conn.execute(sqlalchemy.text(
            "CREATE INDEX IF NOT EXISTS idx_bgs_eval_result_ticktime ON bgs_eval_result(ticktime);"
        ))
        conn.execute(sqlalchemy.text(
            "CREATE INDEX IF NOT EXISTS idx_bgs_eval_result_system ON bgs_eval_result(system_name);"
        ))
        conn.execute(sqlalchemy.text(
            "CREATE INDEX IF NOT EXISTS idx_bgs_eval_result_faction ON bgs_eval_result(faction);"
        ))

        logger.info(f"Tabellen/Indizes 'bgs_eval_*' sichergestellt für Tenant: {db_uri}")
    except Exception as e:
        logger.warning(f"ensure_bgs_eval_tables_and_indexes failed for {db_uri}: {e}")


def ensure_manual_activity_submission_table_and_indexes(conn, db_uri: str):
    """
    Ensure manual activity audit/idempotency table and key indexes exist.
    """
    try:
        db.Model.metadata.create_all(bind=conn)
        conn.execute(sqlalchemy.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_manual_activity_submission_submission_id "
            "ON manual_activity_submission(submission_id);"
        ))
        conn.execute(sqlalchemy.text(
            "CREATE INDEX IF NOT EXISTS idx_manual_activity_submission_cmdr "
            "ON manual_activity_submission(cmdr);"
        ))
        conn.execute(sqlalchemy.text(
            "CREATE INDEX IF NOT EXISTS idx_manual_activity_submission_tickid "
            "ON manual_activity_submission(tickid);"
        ))
        conn.execute(sqlalchemy.text(
            "CREATE INDEX IF NOT EXISTS idx_manual_activity_submission_system_name "
            "ON manual_activity_submission(system_name);"
        ))
        conn.execute(sqlalchemy.text(
            "CREATE INDEX IF NOT EXISTS idx_manual_activity_submission_faction_name "
            "ON manual_activity_submission(faction_name);"
        ))
        conn.execute(sqlalchemy.text(
            "CREATE INDEX IF NOT EXISTS idx_manual_activity_submission_activity_type "
            "ON manual_activity_submission(activity_type);"
        ))
        logger.info(f"Tabelle/Indizes 'manual_activity_submission' sichergestellt fuer Tenant: {db_uri}")
    except Exception as e:
        logger.warning(f"ensure_manual_activity_submission_table_and_indexes failed for {db_uri}: {e}")


def ensure_colonisation_tables_and_indexes(conn, db_uri: str):
    """
    Ensure central Colonisation delivery/status logging tables and key indexes exist.
    """
    try:
        db.Model.metadata.create_all(bind=conn)
        conn.execute(sqlalchemy.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_colonisation_delivery_delivery_id "
            "ON colonisation_delivery(delivery_id);"
        ))
        conn.execute(sqlalchemy.text(
            "CREATE INDEX IF NOT EXISTS idx_colonisation_delivery_cmdr "
            "ON colonisation_delivery(cmdr);"
        ))
        conn.execute(sqlalchemy.text(
            "CREATE INDEX IF NOT EXISTS idx_colonisation_delivery_market_id "
            "ON colonisation_delivery(market_id);"
        ))
        conn.execute(sqlalchemy.text(
            "CREATE INDEX IF NOT EXISTS idx_colonisation_delivery_market_cmdr "
            "ON colonisation_delivery(market_id, cmdr);"
        ))
        conn.execute(sqlalchemy.text(
            "CREATE INDEX IF NOT EXISTS idx_colonisation_delivery_market_session "
            "ON colonisation_delivery(market_id, session_id);"
        ))
        conn.execute(sqlalchemy.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_colonisation_status_status_id "
            "ON colonisation_assist_status(status_id);"
        ))
        conn.execute(sqlalchemy.text(
            "CREATE INDEX IF NOT EXISTS idx_colonisation_status_market_cmdr "
            "ON colonisation_assist_status(market_id, cmdr);"
        ))
        conn.execute(sqlalchemy.text(
            "CREATE INDEX IF NOT EXISTS idx_colonisation_status_market_updated "
            "ON colonisation_assist_status(market_id, updated_at);"
        ))
        logger.info(f"Tabellen/Indizes 'colonisation_*' sichergestellt fuer Tenant: {db_uri}")
    except Exception as e:
        logger.warning(f"ensure_colonisation_tables_and_indexes failed for {db_uri}: {e}")


def ensure_activity_query_indexes(conn, db_uri: str):
    """Ensure indexes used by tick-scoped Dashboard V2/V3 evaluations."""
    statements = (
        "CREATE INDEX IF NOT EXISTS idx_event_timestamp ON event(timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_event_tickid_timestamp ON event(tickid, timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_redeem_voucher_type_event_id ON redeem_voucher_event(type, event_id)",
        "CREATE INDEX IF NOT EXISTS idx_sell_exploration_event_id ON sell_exploration_data_event(event_id)",
        "CREATE INDEX IF NOT EXISTS idx_multi_sell_exploration_event_id ON multi_sell_exploration_data_event(event_id)",
    )
    try:
        for statement in statements:
            conn.execute(sqlalchemy.text(statement))
        logger.info(f"Activity query indexes ensured for tenant: {db_uri}")
    except Exception as e:
        logger.warning(f"ensure_activity_query_indexes failed for {db_uri}: {e}")


def backfill_redeem_voucher_details(conn, db_uri: str = "") -> dict[str, int]:
    """Repair only voucher details that can be derived losslessly from stored data."""
    stats = {"scanned": 0, "factions": 0, "faction": 0, "raw_json": 0}
    try:
        rows = conn.execute(sqlalchemy.text(
            """
            SELECT
                rv.id AS redeem_id,
                rv.amount,
                rv.type,
                rv.faction,
                rv.factions,
                e.id AS event_id,
                e.raw_json
            FROM redeem_voucher_event rv
            JOIN event e ON e.id = rv.event_id
            WHERE (rv.factions IS NULL OR TRIM(rv.factions) = '')
               OR (rv.faction IS NULL OR TRIM(rv.faction) = '')
               OR (
                    rv.factions IS NOT NULL
                    AND TRIM(rv.factions) != ''
                    AND (e.raw_json IS NULL OR e.raw_json NOT LIKE '%Factions%')
               )
            """
        )).mappings().all()

        for row in rows:
            stats["scanned"] += 1
            payload = parse_event_payload(row.get("raw_json")) or {}
            raw_had_factions = bool(redeem_voucher_factions({
                "Factions": payload.get("Factions"),
            }))
            if not raw_had_factions and str(row.get("factions") or "").strip():
                try:
                    stored_factions = json.loads(row["factions"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    stored_factions = None
                if isinstance(stored_factions, list) and stored_factions:
                    payload["Factions"] = stored_factions
            payload.setdefault("event", "RedeemVoucher")
            payload.setdefault("Type", row.get("type"))
            payload.setdefault("Amount", row.get("amount"))
            if row.get("faction"):
                payload.setdefault("Faction", row.get("faction"))

            normalized, factions, primary_faction = normalize_redeem_voucher_payload(payload)
            updates = {}
            if not str(row.get("factions") or "").strip() and factions:
                updates["factions"] = encode_factions(factions)
                stats["factions"] += 1
            if not str(row.get("faction") or "").strip() and primary_faction:
                updates["faction"] = primary_faction
                stats["faction"] += 1

            if updates:
                updates["redeem_id"] = row["redeem_id"]
                assignments = ", ".join(f"{name} = :{name}" for name in updates if name != "redeem_id")
                conn.execute(
                    sqlalchemy.text(f"UPDATE redeem_voucher_event SET {assignments} WHERE id = :redeem_id"),
                    updates,
                )

            if payload and factions and not raw_had_factions:
                conn.execute(
                    sqlalchemy.text("UPDATE event SET raw_json = :raw_json WHERE id = :event_id"),
                    {
                        "event_id": row["event_id"],
                        "raw_json": json.dumps(normalized, ensure_ascii=False, separators=(",", ":")),
                    },
                )
                stats["raw_json"] += 1

        logger.info(f"RedeemVoucher backfill for {db_uri}: {stats}")
    except Exception as e:
        logger.warning(f"RedeemVoucher backfill skipped for {db_uri}: {e}")
    return stats


# -----------------------------------------------------------------------------
# Initialisierung (nur DB-Datei + minimal create_all für neue SQLite DBs)
# -----------------------------------------------------------------------------
def initialize_all_tenant_databases():
    """
    Stellt beim Start sicher, dass die SQLite-DB-Dateien existieren.
    Keine Schema-Updates hier – dafür ist update_all_tenant_databases() zuständig.
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
                logger.info(f"Tenant-DB initialisiert (neu): {abs_path}")


# -----------------------------------------------------------------------------
# Updates / "Migrationen" (Single place for schema ensures + column adds)
# -----------------------------------------------------------------------------
def update_all_tenant_databases():
    """
    Aktualisiert für alle Tenants die Tabellenstruktur und Felder gemäß models.py.
    - Erst ensure/create missing tables via create_all
    - Danach ENSURE: protected_faction + bgs_eval tables/indexes
    - Danach SQLite-spezifische ALTER TABLE ADD COLUMN für bestehende Tabellen/Spalten
    """
    for tenant in TENANTS:
        db_uri = tenant.get("db_uri")
        if not db_uri:
            continue

        url = make_url(db_uri)
        connect_args = {"check_same_thread": False} if url.drivername == "sqlite" else {}
        engine = create_engine(db_uri, connect_args=connect_args)

        with engine.begin() as conn:
            # 1) Missing tables
            db.Model.metadata.create_all(bind=conn)

            # 2) Ensure protected factions
            ensure_protected_faction_table_conn(conn, db_uri)

            # 3) Ensure bgs eval tables + indexes (ticktime-only)
            ensure_bgs_eval_tables_and_indexes(conn, db_uri)

            # 4) Ensure manual activity submissions
            ensure_manual_activity_submission_table_and_indexes(conn, db_uri)

            # 5) Ensure central Colonisation delivery/status logging
            ensure_colonisation_tables_and_indexes(conn, db_uri)

            # 6) SQLite: missing columns via ALTER TABLE
            if url.drivername == "sqlite":
                # --- system ---
                sys_existing = _get_existing_columns(engine, "system")
                sys_model = _get_model_columns(System)
                for col_name, col_obj in sys_model.items():
                    if col_name not in sys_existing:
                        col_type = str(col_obj.type)
                        alter_sql = f'ALTER TABLE system ADD COLUMN {col_name} {col_type}'
                        try:
                            conn.execute(sqlalchemy.text(alter_sql))
                            logger.info(f"Spalte '{col_name}' zu Tabelle 'system' ergänzt für Tenant: {db_uri}")
                        except Exception as e:
                            logger.warning(f"Fehler beim Ergänzen von Spalte '{col_name}' in 'system': {e}")

                # --- faction ---
                fac_existing = _get_existing_columns(engine, "faction")
                fac_model = _get_model_columns(Faction)
                for col_name, col_obj in fac_model.items():
                    if col_name not in fac_existing:
                        col_type = str(col_obj.type)
                        alter_sql = f'ALTER TABLE faction ADD COLUMN {col_name} {col_type}'
                        try:
                            conn.execute(sqlalchemy.text(alter_sql))
                            logger.info(f"Spalte '{col_name}' zu Tabelle 'faction' ergänzt für Tenant: {db_uri}")
                        except Exception as e:
                            logger.warning(f"Fehler beim Ergänzen von Spalte '{col_name}' in 'faction': {e}")

                # --- mission_completed_event ---
                try:
                    mce_existing = _get_existing_columns(engine, "mission_completed_event")
                    mce_model = _get_model_columns(MissionCompletedEvent)
                    for col_name, col_obj in mce_model.items():
                        if col_name not in mce_existing:
                            col_type = str(col_obj.type)
                            alter_sql = f'ALTER TABLE mission_completed_event ADD COLUMN {col_name} {col_type}'
                            try:
                                conn.execute(sqlalchemy.text(alter_sql))
                                logger.info(
                                    f"Spalte '{col_name}' zu Tabelle 'mission_completed_event' ergänzt für Tenant: {db_uri}"
                                )
                            except Exception as e:
                                logger.warning(
                                    f"Fehler beim Ergänzen von Spalte '{col_name}' in 'mission_completed_event': {e}"
                                )
                except Exception as e:
                    logger.warning(f"mission_completed_event column ensure skipped for {db_uri}: {e}")

                # --- mission_completed_influence ---
                try:
                    mci_existing = _get_existing_columns(engine, "mission_completed_influence")
                    mci_model = _get_model_columns(MissionCompletedInfluence)
                    for col_name, col_obj in mci_model.items():
                        if col_name not in mci_existing:
                            col_type = str(col_obj.type)
                            alter_sql = f'ALTER TABLE mission_completed_influence ADD COLUMN {col_name} {col_type}'
                            try:
                                conn.execute(sqlalchemy.text(alter_sql))
                                logger.info(
                                    f"Spalte '{col_name}' zu Tabelle 'mission_completed_influence' ergänzt für Tenant: {db_uri}"
                                )
                            except Exception as e:
                                logger.warning(
                                    f"Fehler beim Ergänzen von Spalte '{col_name}' in 'mission_completed_influence': {e}"
                                )
                    conn.execute(sqlalchemy.text(
                        "CREATE INDEX IF NOT EXISTS idx_mission_completed_influence_mission_id "
                        "ON mission_completed_influence(mission_id);"
                    ))
                    conn.execute(sqlalchemy.text(
                        "CREATE INDEX IF NOT EXISTS idx_mission_completed_influence_event_id "
                        "ON mission_completed_influence(event_id);"
                    ))
                except Exception as e:
                    logger.warning(f"mission_completed_influence column ensure skipped for {db_uri}: {e}")

                # --- mission_failed_event ---
                try:
                    mfe_existing = _get_existing_columns(engine, "mission_failed_event")
                    mfe_model = _get_model_columns(MissionFailedEvent)
                    for col_name, col_obj in mfe_model.items():
                        if col_name not in mfe_existing:
                            col_type = str(col_obj.type)
                            alter_sql = f'ALTER TABLE mission_failed_event ADD COLUMN {col_name} {col_type}'
                            try:
                                conn.execute(sqlalchemy.text(alter_sql))
                                logger.info(
                                    f"Spalte '{col_name}' zu Tabelle 'mission_failed_event' ergänzt für Tenant: {db_uri}"
                                )
                            except Exception as e:
                                logger.warning(
                                    f"Fehler beim Ergänzen von Spalte '{col_name}' in 'mission_failed_event': {e}"
                                )
                except Exception as e:
                    logger.warning(f"mission_failed_event column ensure skipped for {db_uri}: {e}")

                # --- market_buy_event ---
                try:
                    mb_existing = _get_existing_columns(engine, "market_buy_event")
                    mb_model = _get_model_columns(MarketBuyEvent)
                    for col_name, col_obj in mb_model.items():
                        if col_name not in mb_existing:
                            col_type = str(col_obj.type)
                            alter_sql = f'ALTER TABLE market_buy_event ADD COLUMN {col_name} {col_type}'
                            try:
                                conn.execute(sqlalchemy.text(alter_sql))
                                logger.info(f"Spalte '{col_name}' zu Tabelle 'market_buy_event' ergänzt für Tenant: {db_uri}")
                            except Exception as e:
                                logger.warning(f"Fehler beim Ergänzen von Spalte '{col_name}' in 'market_buy_event': {e}")
                except Exception as e:
                    logger.warning(f"market_buy_event column ensure skipped for {db_uri}: {e}")

                # --- market_sell_event ---
                try:
                    ms_existing = _get_existing_columns(engine, "market_sell_event")
                    ms_model = _get_model_columns(MarketSellEvent)
                    for col_name, col_obj in ms_model.items():
                        if col_name not in ms_existing:
                            col_type = str(col_obj.type)
                            alter_sql = f'ALTER TABLE market_sell_event ADD COLUMN {col_name} {col_type}'
                            try:
                                conn.execute(sqlalchemy.text(alter_sql))
                                logger.info(f"Spalte '{col_name}' zu Tabelle 'market_sell_event' ergänzt für Tenant: {db_uri}")
                            except Exception as e:
                                logger.warning(f"Fehler beim Ergänzen von Spalte '{col_name}' in 'market_sell_event': {e}")
                except Exception as e:
                    logger.warning(f"market_sell_event column ensure skipped for {db_uri}: {e}")

                # --- redeem_voucher_event ---
                try:
                    rv_existing = _get_existing_columns(engine, "redeem_voucher_event")
                    rv_model = _get_model_columns(RedeemVoucherEvent)
                    for col_name, col_obj in rv_model.items():
                        if col_name not in rv_existing:
                            col_type = str(col_obj.type)
                            alter_sql = f'ALTER TABLE redeem_voucher_event ADD COLUMN {col_name} {col_type}'
                            try:
                                conn.execute(sqlalchemy.text(alter_sql))
                                logger.info(f"Spalte '{col_name}' zu Tabelle 'redeem_voucher_event' ergänzt für Tenant: {db_uri}")
                            except Exception as e:
                                logger.warning(f"Fehler beim Ergänzen von Spalte '{col_name}' in 'redeem_voucher_event': {e}")
                except Exception as e:
                    logger.warning(f"redeem_voucher_event column ensure skipped for {db_uri}: {e}")

                # --- multi_sell_exploration_data_event ---
                try:
                    msed_existing = _get_existing_columns(engine, "multi_sell_exploration_data_event")
                    msed_model = _get_model_columns(MultiSellExplorationDataEvent)
                    for col_name, col_obj in msed_model.items():
                        if col_name not in msed_existing:
                            col_type = str(col_obj.type)
                            alter_sql = f'ALTER TABLE multi_sell_exploration_data_event ADD COLUMN {col_name} {col_type}'
                            try:
                                conn.execute(sqlalchemy.text(alter_sql))
                                logger.info(
                                    f"Spalte '{col_name}' zu Tabelle 'multi_sell_exploration_data_event' ergänzt für Tenant: {db_uri}"
                                )
                            except Exception as e:
                                logger.warning(
                                    f"Fehler beim Ergänzen von Spalte '{col_name}' in 'multi_sell_exploration_data_event': {e}"
                                )
                except Exception as e:
                    logger.warning(f"multi_sell_exploration_data_event column ensure skipped for {db_uri}: {e}")

                # --- sell_exploration_data_event ---
                try:
                    sed_existing = _get_existing_columns(engine, "sell_exploration_data_event")
                    sed_model = _get_model_columns(SellExplorationDataEvent)
                    for col_name, col_obj in sed_model.items():
                        if col_name not in sed_existing:
                            col_type = str(col_obj.type)
                            alter_sql = f'ALTER TABLE sell_exploration_data_event ADD COLUMN {col_name} {col_type}'
                            try:
                                conn.execute(sqlalchemy.text(alter_sql))
                                logger.info(
                                    f"Spalte '{col_name}' zu Tabelle 'sell_exploration_data_event' ergänzt für Tenant: {db_uri}"
                                )
                            except Exception as e:
                                logger.warning(
                                    f"Fehler beim Ergänzen von Spalte '{col_name}' in 'sell_exploration_data_event': {e}"
                                )
                except Exception as e:
                    logger.warning(f"sell_exploration_data_event column ensure skipped for {db_uri}: {e}")

                # --- manual_activity_submission ---
                try:
                    mas_existing = _get_existing_columns(engine, "manual_activity_submission")
                    mas_model = _get_model_columns(ManualActivitySubmission)
                    for col_name, col_obj in mas_model.items():
                        if col_name not in mas_existing:
                            col_type = str(col_obj.type)
                            alter_sql = f'ALTER TABLE manual_activity_submission ADD COLUMN {col_name} {col_type}'
                            try:
                                conn.execute(sqlalchemy.text(alter_sql))
                                logger.info(
                                    f"Spalte '{col_name}' zu Tabelle 'manual_activity_submission' ergaenzt fuer Tenant: {db_uri}"
                                )
                            except Exception as e:
                                logger.warning(
                                    f"Fehler beim Ergaenzen von Spalte '{col_name}' in 'manual_activity_submission': {e}"
                                )
                except Exception as e:
                    logger.warning(f"manual_activity_submission column ensure skipped for {db_uri}: {e}")

                ensure_activity_query_indexes(conn, db_uri)
                backfill_redeem_voucher_details(conn, db_uri)

        # Dashboard identity/preference tables deliberately live in each
        # tenant database and are maintained idempotently.
        ensure_dashboard_schema(engine)

        logger.info(f"Tenant-DB aktualisiert: {db_uri}")


# -----------------------------------------------------------------------------
# Utility: Activity-Daten löschen
# -----------------------------------------------------------------------------
def delete_all_activity_data():
    """
    Löscht alle Datensätze aus den Tabellen activity, system und faction für alle Tenants.
    """
    from sqlalchemy.orm import sessionmaker
    from models import Activity, System as SysModel, Faction as FacModel

    for tenant in TENANTS:
        db_uri = tenant.get("db_uri")
        if not db_uri:
            continue

        url = make_url(db_uri)
        connect_args = {"check_same_thread": False} if url.drivername == "sqlite" else {}
        engine = create_engine(db_uri, connect_args=connect_args)

        Session = sessionmaker(bind=engine)
        session = Session()
        try:
            deleted_faction = session.query(FacModel).delete()
            deleted_system = session.query(SysModel).delete()
            deleted_activity = session.query(Activity).delete()
            session.commit()
            logger.info(
                f"Alle Datensätze aus activity/system/faction für Tenant {db_uri} gelöscht "
                f"(activity={deleted_activity}, system={deleted_system}, faction={deleted_faction})."
            )
        except Exception as e:
            session.rollback()
            logger.error(f"Fehler beim Löschen der Daten für Tenant {db_uri}: {e}")
        finally:
            session.close()


# -----------------------------------------------------------------------------
# Optional: Ensure EDDN Indizes
# -----------------------------------------------------------------------------
def ensure_eddn_indexes():
    """
    Erzeugt sinnvolle Indizes im EDDN-DB-Kontext, falls nicht vorhanden.
    """
    try:
        eddn_db_uri = os.getenv("EDDN_DATABASE")
        if not eddn_db_uri:
            logger.warning("EDDN_DATABASE not configured; skip index creation")
            return

        engine = create_engine(eddn_db_uri)
        with engine.begin() as conn:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_esi_system_name ON eddn_system_info(system_name);"))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_esi_system_name_nocase "
                "ON eddn_system_info(system_name COLLATE NOCASE);"
            ))
            logger.info("EDDN indexes ensured (exact and NOCASE system_name).")
    except Exception as e:
        logger.error(f"ensure_eddn_indexes failed: {e}")
