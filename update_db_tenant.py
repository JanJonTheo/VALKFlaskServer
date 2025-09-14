import os
import json
from sqlalchemy import create_engine, inspect, Column, Integer, String, Boolean, MetaData, Table
from sqlalchemy.engine import make_url

TENANT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "tenant.json")
with open(TENANT_CONFIG_PATH, "r", encoding="utf-8") as f:
    tenant_data = json.load(f)
    # Ensure TENANTS is always a list for consistent iteration
    TENANTS = [tenant_data] if isinstance(tenant_data, dict) else tenant_data
    
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
        print(f"Tabelle 'protected_faction' wurde in {db_uri} angelegt.")
    else:
        print(f"Tabelle 'protected_faction' existiert bereits in {db_uri}.")

if __name__ == "__main__":
    for tenant in TENANTS:
        db_uri = tenant.get("db_uri")
        if db_uri:
            ensure_protected_faction_table(db_uri)

