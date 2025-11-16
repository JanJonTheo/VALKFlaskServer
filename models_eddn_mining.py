import os
import uuid
from datetime import datetime

from sqlalchemy import (
    create_engine, Column, String, Integer, Float, DateTime, Boolean,
    ForeignKey, JSON, UniqueConstraint, Index
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

# -------------------------------------------------------------------
# DB Setup
# -------------------------------------------------------------------

MINING_DB_URL = os.getenv("MINING_DB_URL", "sqlite:///db/bgs_data_eddn_mining.db")

engine = create_engine(
    MINING_DB_URL,
    echo=False,
    future=True
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

BaseMining = declarative_base()

def _uuid() -> str:
    return str(uuid.uuid4())

# -------------------------------------------------------------------
# Ringe & Hotspots
# -------------------------------------------------------------------

class MiningRing(BaseMining):
    """
    Ring-Metadaten, extrahiert z. B. aus Journal 'Scan' (Rings[]).
    """
    __tablename__ = "eddn_mining_ring"

    id = Column(String(36), primary_key=True, default=_uuid)

    system_name = Column(String(255), index=True, nullable=False)
    body_name   = Column(String(255), index=True, nullable=False, default="")
    ring_name   = Column(String(255), index=True, nullable=False)

    ring_type       = Column(String(64), index=True)     # Icy, Rocky, Metal Rich, Metallic
    inner_radius_km = Column(Float)                      # aus InnerRad (m) -> km
    outer_radius_km = Column(Float)                      # aus OuterRad (m) -> km
    reserve_level   = Column(String(32), index=True)     # Pristine, Major, Common, Low, Depleted

    updated_at = Column(DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        UniqueConstraint("system_name", "ring_name", name="uq_ring_system_ringname"),
        Index("ix_ring_system_body_ring", "system_name", "body_name", "ring_name"),
    )


class MiningHotspot(BaseMining):
    """
    Hotspots, z. B. aus SAASignalsFound (Type '* Hotspot') oder anderen Quellen.
    """
    __tablename__ = "eddn_mining_hotspot"

    id = Column(String(36), primary_key=True, default=_uuid)

    ring_id   = Column(String(36), ForeignKey("eddn_mining_ring.id"), index=True, nullable=False)
    commodity = Column(String(128), index=True, nullable=False)     # Painite, LTD, Tritium, Monazite, ...

    # Zähl-/Statusfelder
    count        = Column(Integer, default=1)                        # Überlappungs- oder Fundanzahl
    sightings    = Column(Integer, default=1, index=True)            # Anzahl Meldungen (optional genutzt)
    first_seen_at = Column(DateTime, default=datetime.utcnow, index=True)
    last_seen_at  = Column(DateTime, default=datetime.utcnow, index=True)

    # Optionale Felder (werden im Client defensiv verwendet)
    source     = Column(String(64))                                  # z. B. 'eddn_journal'
    confidence = Column(Integer)                                     # grobe Einschätzung (0..100)
    active     = Column(Boolean, default=True, index=True)

    eddn_message_id = Column(String(64), index=True)
    updated_at      = Column(DateTime, default=datetime.utcnow, index=True)

    ring = relationship("MiningRing", backref="hotspots", lazy="joined")

    __table_args__ = (
        UniqueConstraint("ring_id", "commodity", name="uq_hotspot_ring_commodity"),
        Index("ix_hotspot_recent", "commodity", "last_seen_at"),
    )

# -------------------------------------------------------------------
# Mining Sessions & Events (für Prospector/Collected/Refined etc.)
# -------------------------------------------------------------------

class MiningSession(BaseMining):
    """
    Session pro Cmdr/Ort. Dient als Kontext für Events (optional).
    """
    __tablename__ = "eddn_mining_session"

    id = Column(String(36), primary_key=True, default=_uuid)

    cmdr        = Column(String(128), index=True, nullable=False, default="Unknown")
    system_name = Column(String(255), index=True, nullable=False, default="Unknown")
    body_name   = Column(String(255), index=True, default="")
    ring_name   = Column(String(255), index=True, default="")
    ring_id     = Column(String(36), ForeignKey("eddn_mining_ring.id"), index=True, nullable=True)

    ring_type     = Column(String(64), index=True)       # optionaler Kontext
    reserve_level = Column(String(32), index=True)       # optionaler Kontext

    started_at = Column(DateTime, default=datetime.utcnow, index=True)
    ended_at   = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, index=True)

    ring = relationship("MiningRing", lazy="joined")

    __table_args__ = (
        Index("ix_msession_scope", "cmdr", "system_name", "body_name", "ring_name"),
    )


class ProspectedAsteroid(BaseMining):
    """
    Optionales Event-Log für ProspectedAsteroid.
    (Schema minimal gehalten; erweitere nach Bedarf.)
    """
    __tablename__ = "eddn_mining_prospected_asteroid"

    id = Column(String(36), primary_key=True, default=_uuid)

    session_id  = Column(String(36), ForeignKey("eddn_mining_session.id"), index=True, nullable=True)
    system_name = Column(String(255), index=True)
    body_name   = Column(String(255), index=True)
    ring_name   = Column(String(255), index=True)

    content_raw = Column(JSON)                          # kompletter Journal-Event
    timestamp   = Column(DateTime, default=datetime.utcnow, index=True)

    session = relationship("MiningSession", lazy="joined")


class MiningRefinedEvent(BaseMining):
    """
    Journal 'MiningRefined' – was wurde raffiniert?
    """
    __tablename__ = "eddn_mining_refined"

    id = Column(String(36), primary_key=True, default=_uuid)

    session_id  = Column(String(36), ForeignKey("eddn_mining_session.id"), index=True, nullable=True)
    system_name = Column(String(255), index=True)
    body_name   = Column(String(255), index=True)
    ring_name   = Column(String(255), index=True)

    commodity   = Column(String(128), index=True)
    amount      = Column(Float)                         # falls verfügbar
    timestamp   = Column(DateTime, default=datetime.utcnow, index=True)

    raw_json    = Column(JSON)
    session     = relationship("MiningSession", lazy="joined")


class MaterialCollectedEvent(BaseMining):
    """
    Journal 'MaterialCollected' – eingesammelte Materialien (z. B. LTD fragments etc.).
    """
    __tablename__ = "eddn_mining_material_collected"

    id = Column(String(36), primary_key=True, default=_uuid)

    session_id  = Column(String(36), ForeignKey("eddn_mining_session.id"), index=True, nullable=True)
    system_name = Column(String(255), index=True)
    body_name   = Column(String(255), index=True)
    ring_name   = Column(String(255), index=True)

    category    = Column(String(64), index=True)        # 'Encoded','Manufactured','Raw' etc., sofern vorhanden
    name        = Column(String(128), index=True)
    count       = Column(Integer, default=1)

    timestamp   = Column(DateTime, default=datetime.utcnow, index=True)
    raw_json    = Column(JSON)

    session     = relationship("MiningSession", lazy="joined")

# -------------------------------------------------------------------
# SAA Signals: Roh (Fundstellen) – aus SAASignalsFound (Hotspot-Parser)
# -------------------------------------------------------------------

class SAASignalFound(BaseMining):
    """
    Roh-Signale pro Ort/Typ/Zählung.
    Dient u. a. zur Erkennung von "* Hotspot" (für MiningHotspot).
    """
    __tablename__ = "eddn_mining_saa_signal"

    id = Column(String(36), primary_key=True, default=_uuid)

    system_name = Column(String(255), index=True, nullable=False)
    body_name   = Column(String(255), index=True)
    body_id     = Column(Integer, index=True)
    ring_name   = Column(String(255), index=True)

    signal_type            = Column(String(255), index=True)  # z. B. "$Some_Hotspot;"
    signal_type_localised  = Column(String(255), index=True)  # lesbarer Name
    count                  = Column(Integer, default=1)

    timestamp   = Column(DateTime, default=datetime.utcnow, index=True)

    # Deduplizierung & Sichtungen
    fingerprint   = Column(String(64), index=True)            # stabiler Hash über Kernfelder
    first_seen_at = Column(DateTime, default=datetime.utcnow, index=True)
    last_seen_at  = Column(DateTime, default=datetime.utcnow, index=True)
    sightings     = Column(Integer, default=1, index=True)

    uploader_id = Column(String(128), index=True)
    raw_json    = Column(JSON)

    __table_args__ = (
        # „Natürlicher“ Schlüssel für konservatives Deduplizieren
        UniqueConstraint(
            "system_name", "body_id", "ring_name",
            "signal_type", "signal_type_localised", "count",
            name="uq_saa_found_natural"
        ),
        Index("ix_saa_found_recent", "signal_type_localised", "last_seen_at"),
    )

# -------------------------------------------------------------------
# SAA Signals: Strukturierte Ablage (klassifiziert + Biological-Details)
# -------------------------------------------------------------------

class SAASignal(BaseMining):
    """
    Generische, strukturierte SAA-Signale (klassifiziert nach Group/Type, mit Koordinaten).
    Diese Tabelle wird über den strukturierten Parser befüllt.
    """
    __tablename__ = "eddn_saa_signal"

    id = Column(String(36), primary_key=True, default=_uuid)
    eddn_message_id = Column(String(64), index=True)

    system_name  = Column(String(255), index=True, nullable=False)
    body_name    = Column(String(255), index=True, nullable=False)
    latitude     = Column(Float)
    longitude    = Column(Float)

    signal_group = Column(String(32), index=True, nullable=False)    # Geological|Biological|Human|Mining
    signal_type  = Column(String(128), index=True, nullable=False)   # z. B. Tritium, Brain Tree, Fumarole...
    signal_code  = Column(String(255))                                # roher Codex-/Typ-Schlüssel

    count        = Column(Integer, default=0)
    sightings    = Column(Integer, default=1, index=True)

    first_seen_at = Column(DateTime, default=datetime.utcnow, index=True)
    last_seen_at  = Column(DateTime, default=datetime.utcnow, index=True)

    raw_json   = Column(JSON)
    updated_at = Column(DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        UniqueConstraint(
            "system_name", "body_name", "latitude", "longitude",
            "signal_group", "signal_type",
            name="uq_saa_loc_group_type"
        ),
        Index("ix_saa_recent", "signal_group", "signal_type", "last_seen_at"),
    )


class BiologicalSAASignal(BaseMining):
    """
    Zusatzinformationen für Biological-SAA-Signale (1:1 zu SAASignal).
    """
    __tablename__ = "eddn_biological_saa_signal"

    id = Column(String(36), primary_key=True, default=_uuid)
    saa_signal_id = Column(String(36), ForeignKey("eddn_saa_signal.id"), unique=True, nullable=False, index=True)

    # Rohschlüssel aus Journal (z. B. "$Codex_Ent_Bacterial_Genus_Name;")
    genus_raw   = Column(String(255), index=True)
    species_raw = Column(String(255), index=True)
    variant_raw = Column(String(255), index=True)

    # Normalisierte Labels (optional via Mapping beautified)
    genus   = Column(String(255), index=True)
    species = Column(String(255), index=True)
    variant = Column(String(255), index=True)

    updated_at = Column(DateTime, default=datetime.utcnow, index=True)


class BiologicalTaxonomyMap(BaseMining):
    """
    Mapping-Tabelle, um Codex-Schlüssel auf sprechende Namen zu mappen (genus/species/variant).
    """
    __tablename__ = "eddn_biological_taxonomy_map"

    id = Column(String(36), primary_key=True, default=_uuid)

    key_raw = Column(String(255), unique=True, index=True)    # z. B. "$Codex_Ent_Bacterial_Genus_Name;"
    pretty  = Column(String(255), index=True)                 # z. B. "Bacterial"
    kind    = Column(String(32), index=True)                  # "genus" | "species" | "variant"

    updated_at = Column(DateTime, default=datetime.utcnow, index=True)

# -------------------------------------------------------------------
# Schema initialisieren (wird von eddn_client.py aufgerufen)
# -------------------------------------------------------------------

def init_mining_models() -> None:
    """
    Erstellt alle Tabellen, falls nicht vorhanden.
    (Beibehaltener Name mit Schreibfehler für Abwärtskompatibilität zum Aufruf in eddn_client.py)
    """
    BaseMining.metadata.create_all(bind=engine)
