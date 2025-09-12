from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy import Column, String, DateTime, Text, Integer, Float, ForeignKey, JSON
from sqlalchemy.orm import relationship
from datetime import datetime
import uuid

Base = declarative_base()

class EDDNMessage(Base):
    __tablename__ = "eddn_message"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    schema_ref = Column(String(255), nullable=False)
    header_gateway_timestamp = Column(DateTime, nullable=True)
    message_type = Column(String(128), nullable=True)
    message_json = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)

    @staticmethod
    def from_eddn(data):
        # Extrahiere Felder aus EDDN-Message
        schema_ref = data.get("$schemaRef", "")
        header = data.get("header", {})
        header_gateway_timestamp = None
        if "gatewayTimestamp" in header:
            try:
                header_gateway_timestamp = datetime.fromisoformat(header["gatewayTimestamp"].replace("Z", "+00:00"))
            except Exception:
                header_gateway_timestamp = None
        message_type = data.get("message", {}).get("event", None)
        return EDDNMessage(
            schema_ref=schema_ref,
            header_gateway_timestamp=header_gateway_timestamp,
            message_type=message_type,
            message_json=str(data),
            timestamp=datetime.utcnow()
        )

class SystemInfo(Base):
    __tablename__ = "eddn_system_info"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    eddn_message_id = Column(String(36), index=True)  # Reference to eddn_message.id
    system_name = Column(String(255), index=True)
    controlling_faction = Column(String(255))
    controlling_power = Column(String(255))  # New field
    population = Column(Integer)
    security = Column(String(128))
    government = Column(String(128))
    allegiance = Column(String(128))
    updated_at = Column(DateTime, default=datetime.utcnow, index=True)

class Faction(Base):
    __tablename__ = "eddn_faction"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    eddn_message_id = Column(String(36), index=True)  # Reference to eddn_message.id
    system_name = Column(String(255), index=True)
    name = Column(String(255), index=True)
    influence = Column(Float)
    state = Column(String(128))
    recovering_states = Column(JSON)
    active_states = Column(JSON)
    pending_states = Column(JSON)  # <--- NEU
    updated_at = Column(DateTime, default=datetime.utcnow, index=True)

class Conflict(Base):
    __tablename__ = "eddn_conflict"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    eddn_message_id = Column(String(36), index=True)  # Reference to eddn_message.id
    system_name = Column(String(255), index=True)
    faction1 = Column(String(255))
    faction2 = Column(String(255))
    stake1 = Column(String(255))
    stake2 = Column(String(255))
    won_days1 = Column(Integer)
    won_days2 = Column(Integer)
    status = Column(String(128))
    war_type = Column(String(128))
    updated_at = Column(DateTime, default=datetime.utcnow, index=True)

class Powerplay(Base):
    __tablename__ = "eddn_powerplay"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    eddn_message_id = Column(String(36), index=True)  # Reference to eddn_message.id
    system_name = Column(String(255), index=True)
    power = Column(JSON)
    powerplay_state = Column(String(128))
    control_progress = Column(Integer)
    reinforcement = Column(Integer)
    undermining = Column(Integer)
    updated_at = Column(DateTime, default=datetime.utcnow, index=True)
