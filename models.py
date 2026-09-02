import json

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

class Event(db.Model):
    __table_args__ = (
        db.Index("idx_event_timestamp", "timestamp"),
        db.Index("idx_event_tickid_timestamp", "tickid", "timestamp"),
    )

    id = db.Column(db.Integer, primary_key=True)
    event = db.Column(db.String(64), nullable=False)
    timestamp = db.Column(db.String(64), nullable=False)
    tickid = db.Column(db.String(24), nullable=False)
    ticktime = db.Column(db.String(64), nullable=False)
    cmdr = db.Column(db.String(64), nullable=True)
    starsystem = db.Column(db.String(128), nullable=True)
    systemaddress = db.Column(db.BigInteger, nullable=True)
    raw_json = db.Column(db.Text, nullable=True)

    @classmethod
    def from_dict(cls, data):
        return cls(
            event=data['event'],
            timestamp=data['timestamp'],
            tickid=data['tickid'],
            ticktime=data.get('ticktime', ''),
            cmdr=data.get('cmdr'),
            starsystem=data.get('StarSystem'),
            systemaddress=data.get('SystemAddress'),
            raw_json=json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        )

class MarketBuyEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('event.id'), nullable=False)
    stock = db.Column(db.Integer)
    stock_bracket = db.Column(db.Integer)
    value = db.Column(db.Integer)
    count = db.Column(db.Integer)
    # Neue/zusätzliche Felder aus MarketBuy-Event
    market_id = db.Column(db.BigInteger)            # MarketID
    commodity = db.Column(db.String(128))           # Type
    buy_price = db.Column(db.Integer)               # BuyPrice
    total_cost = db.Column(db.BigInteger)           # TotalCost
    station_faction = db.Column(db.String(128))     # StationFaction.Name
    starsystem = db.Column(db.String(128))          # StarSystem
    systemaddress = db.Column(db.BigInteger)        # SystemAddress

class MarketSellEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('event.id'), nullable=False)
    demand = db.Column(db.Integer)
    demand_bracket = db.Column(db.Integer)
    profit = db.Column(db.Integer)
    value = db.Column(db.Integer)
    count = db.Column(db.Integer)
    # Neue/zusätzliche Felder aus MarketSell-Event
    market_id = db.Column(db.BigInteger)            # MarketID
    commodity = db.Column(db.String(128))           # Type
    sell_price = db.Column(db.Integer)              # SellPrice
    total_sale = db.Column(db.BigInteger)           # TotalSale
    avg_price_paid = db.Column(db.Integer)          # AvgPricePaid
    station_faction = db.Column(db.String(128))     # StationFaction.Name
    starsystem = db.Column(db.String(128))          # StarSystem
    systemaddress = db.Column(db.BigInteger)        # SystemAddress

class MissionCompletedEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('event.id'), nullable=False)

    # NEU: Felder entsprechend app.py /events → MissionCompleted
    mission_id = db.Column(db.Integer)            # MissionID
    name = db.Column(db.String(128))             # Name
    faction = db.Column(db.String(128))          # Faction
    donor = db.Column(db.String(128))            # Donor
    target_faction = db.Column(db.String(128))   # TargetFaction
    target_type = db.Column(db.String(128))      # TargetType
    target = db.Column(db.String(128))           # Target
    kill_count = db.Column(db.Integer)           # KillCount

    # Alte Felder zur Abwärtskompatibilität beibehalten
    awarding_faction = db.Column(db.String(128))
    mission_name = db.Column(db.String(128))
    reward = db.Column(db.Integer)

class MissionCompletedInfluence(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    mission_id = db.Column(db.Integer, db.ForeignKey('mission_completed_event.id'), nullable=False, index=True)
    # New rows use the normalized mission_completed_event.id relation above.
    # event_id also marks the corrected link format so legacy rows, which stored
    # Event.id in mission_id, can still be read without rewriting history.
    event_id = db.Column(db.Integer, db.ForeignKey('event.id'), nullable=True, index=True)
    system = db.Column(db.String(128))
    influence = db.Column(db.String(8))
    trend = db.Column(db.String(32))
    faction_name = db.Column(db.String(128))
    reputation = db.Column(db.String(8))
    reputation_trend = db.Column(db.String(32))
    effect = db.Column(db.String(128))
    effect_trend = db.Column(db.String(32))

class Activity(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tickid = db.Column(db.String(24), nullable=False)
    ticktime = db.Column(db.String(64), nullable=False)
    timestamp = db.Column(db.String(64), nullable=False)
    cmdr = db.Column(db.String(64), nullable=True)
    systems = db.relationship('System', backref='activity', cascade="all, delete-orphan")

class System(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    address = db.Column(db.BigInteger, nullable=False)
    activity_id = db.Column(db.Integer, db.ForeignKey('activity.id'), nullable=False)
    factions = db.relationship('Faction', backref='system', cascade="all, delete-orphan")
    # Neue Felder für Thargoid War
    twkills = db.Column(db.Text)      # JSON-String für dict {"cyclops": 1, ...}
    twsandr = db.Column(db.Text)      # JSON-String für dict {"damagedpods": 1, ...}
    twreactivate = db.Column(db.Integer)

class Faction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    state = db.Column(db.String(64), nullable=False)
    bvs = db.Column(db.Integer)
    cbs = db.Column(db.Integer)
    exobiology = db.Column(db.Integer)
    exploration = db.Column(db.Integer)
    scenarios = db.Column(db.Integer)
    infprimary = db.Column(db.Integer)
    infsecondary = db.Column(db.Integer)
    missionfails = db.Column(db.Integer)
    murdersground = db.Column(db.Integer)
    murdersspace = db.Column(db.Integer)
    tradebm = db.Column(db.Integer)
    system_id = db.Column(db.Integer, db.ForeignKey('system.id'), nullable=False)
    # Neue Felder aus bgs_tally_openapi.json
    stations = db.Column(db.Text)     # JSON-String für Liste von Station-Objekten
    czground = db.Column(db.Text)     # JSON-String für dict
    czspace = db.Column(db.Text)      # JSON-String für dict
    tradebuy = db.Column(db.Text)     # JSON-String für dict
    tradesell = db.Column(db.Text)    # JSON-String für dict
    sandr = db.Column(db.Text)        # JSON-String für dict

class FactionKillBondEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('event.id'), nullable=False)
    killer_ship = db.Column(db.String(64))
    awarding_faction = db.Column(db.String(128))
    victim_faction = db.Column(db.String(128))
    reward = db.Column(db.Integer)

class MissionFailedEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('event.id'), nullable=False)
    mission_id = db.Column(db.Integer)
    name = db.Column(db.String(128))
    faction = db.Column(db.String(128))
    mission_name = db.Column(db.String(128))
    awarding_faction = db.Column(db.String(128))
    fine = db.Column(db.Integer)

class MultiSellExplorationDataEvent(db.Model):
    __table_args__ = (
        db.Index("idx_multi_sell_exploration_event_id", "event_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('event.id'), nullable=False)
    total_earnings = db.Column(db.Integer)
    # Neue/zusätzliche Felder aus MultiSellExplorationData-Event
    discovered = db.Column(db.Text)       # JSON-String der Liste 'Discovered'
    base_value = db.Column(db.Integer)    # BaseValue
    bonus = db.Column(db.Integer)         # Bonus
    station_faction = db.Column(db.String(128)) # StationFaction.Name
    starsystem = db.Column(db.String(128))      # StarSystem
    systemaddress = db.Column(db.BigInteger)   # SystemAddress

class RedeemVoucherEvent(db.Model):
    __table_args__ = (
        db.Index("idx_redeem_voucher_type_event_id", "type", "event_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('event.id'), nullable=False)
    amount = db.Column(db.Integer)
    faction = db.Column(db.String(128))
    type = db.Column(db.String(128))
    # Neue/zusätzliche Felder aus RedeemVoucher-Event
    factions = db.Column(db.Text)                # JSON-String für Liste von {"Faction":"...","Amount":...}
    station_faction = db.Column(db.String(128)) # StationFaction.Name
    starsystem = db.Column(db.String(128))      # StarSystem
    systemaddress = db.Column(db.BigInteger)    # SystemAddress

class SellExplorationDataEvent(db.Model):
    __table_args__ = (
        db.Index("idx_sell_exploration_event_id", "event_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('event.id'), nullable=False)
    earnings = db.Column(db.Integer)
    # Neue/zusätzliche Felder aus SellExplorationData-Event
    systems = db.Column(db.Text)          # JSON-String der Liste 'Systems'
    discovered = db.Column(db.Text)       # JSON-String der Liste 'Discovered'
    base_value = db.Column(db.Integer)    # BaseValue
    bonus = db.Column(db.Integer)         # Bonus
    station_faction = db.Column(db.String(128)) # StationFaction.Name
    starsystem = db.Column(db.String(128))      # StarSystem
    systemaddress = db.Column(db.BigInteger)   # SystemAddress

class Cmdr(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), unique=True, nullable=False)
    rank_combat = db.Column(db.String(64))
    rank_trade = db.Column(db.String(64))
    rank_explore = db.Column(db.String(64))
    rank_cqc = db.Column(db.String(64))
    rank_empire = db.Column(db.String(64))
    rank_federation = db.Column(db.String(64))
    rank_power = db.Column(db.String(64))
    credits = db.Column(db.BigInteger)
    assets = db.Column(db.BigInteger)
    inara_url = db.Column(db.String(256))
    squadron_name = db.Column(db.String(128))
    squadron_rank = db.Column(db.String(64))

class CommitCrimeEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey("event.id"), nullable=False)
    crime_type = db.Column(db.String(128))
    faction = db.Column(db.String(128))
    victim = db.Column(db.String(128))
    bounty = db.Column(db.Integer)

class Objective(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String)
    priority = db.Column(db.Integer)
    type = db.Column(db.String)
    system = db.Column(db.String)
    faction = db.Column(db.String)
    description = db.Column(db.Text)
    startdate = db.Column(db.DateTime)
    enddate = db.Column(db.DateTime)
    targets = db.relationship('ObjectiveTarget', backref='objective', cascade="all, delete-orphan")

class ObjectiveTarget(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    objective_id = db.Column(db.Integer, db.ForeignKey("objective.id"), nullable=False)
    type = db.Column(db.String)
    station = db.Column(db.String)
    system = db.Column(db.String)
    faction = db.Column(db.String)
    progress = db.Column(db.Integer)
    targetindividual = db.Column(db.Integer)
    targetoverall = db.Column(db.Integer)
    settlements = db.relationship('ObjectiveTargetSettlement', backref='target', cascade="all, delete-orphan")

class ObjectiveTargetSettlement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    target_id = db.Column(db.Integer, db.ForeignKey("objective_target.id"), nullable=False)
    name = db.Column(db.String)
    targetindividual = db.Column(db.Integer)
    targetoverall = db.Column(db.Integer)
    progress = db.Column(db.Integer)

class SyntheticGroundCZ(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('event.id'), nullable=False)
    cz_type = db.Column(db.String(64))
    settlement = db.Column(db.String(128))
    faction = db.Column(db.String(128))
    cmdr = db.Column(db.String(64))
    station_faction_name = db.Column(db.String(128))

class SyntheticCZ(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('event.id'), nullable=False)
    cz_type = db.Column(db.String(64))
    faction = db.Column(db.String(128))
    cmdr = db.Column(db.String(64))
    station_faction_name = db.Column(db.String(128))

class ManualActivitySubmission(db.Model):
    __tablename__ = "manual_activity_submission"

    id = db.Column(db.Integer, primary_key=True)
    submission_id = db.Column(db.String(128), unique=True, nullable=False, index=True)
    source = db.Column(db.String(64), nullable=False, default="discord_modal")

    discord_guild_id = db.Column(db.String(64))
    discord_channel_id = db.Column(db.String(64))
    discord_user_id = db.Column(db.String(64))
    discord_message_id = db.Column(db.String(64))
    discord_interaction_id = db.Column(db.String(64))

    cmdr = db.Column(db.String(64), nullable=False, index=True)

    tickid = db.Column(db.String(24), nullable=False, index=True)
    ticktime = db.Column(db.String(64), nullable=False)

    captured_at = db.Column(db.String(64), nullable=False)
    client_timestamp = db.Column(db.String(64))

    system_name = db.Column(db.String(128), nullable=False, index=True)
    system_address = db.Column(db.BigInteger, nullable=False)
    faction_name = db.Column(db.String(128), nullable=False, index=True)
    faction_state = db.Column(db.String(64), nullable=False, default="None")

    activity_type = db.Column(db.String(64), nullable=False, index=True)
    amount = db.Column(db.BigInteger)
    count = db.Column(db.Integer)
    influence = db.Column(db.Integer)
    cz_type = db.Column(db.String(16))
    settlement = db.Column(db.String(128))

    activity_id = db.Column(db.Integer)
    event_ids_json = db.Column(db.Text)

    payload_hash = db.Column(db.String(128), nullable=False)
    payload_json = db.Column(db.Text, nullable=False)
    note = db.Column(db.Text)
    status = db.Column(db.String(32), nullable=False, default="saved")
    error_message = db.Column(db.Text)
    created_at = db.Column(db.String(64), nullable=False)

    webhook_status = db.Column(db.String(32))
    webhook_message_id = db.Column(db.String(64))
    webhook_error = db.Column(db.Text)
    webhook_posted_at = db.Column(db.String(64))

class ColonisationDelivery(db.Model):
    __tablename__ = "colonisation_delivery"

    id = db.Column(db.Integer, primary_key=True)
    delivery_id = db.Column(db.String(128), unique=True, nullable=False, index=True)
    batch_id = db.Column(db.String(128))
    session_id = db.Column(db.String(128), index=True)

    client_id = db.Column(db.String(128), index=True)
    client_name = db.Column(db.String(128))
    cmdr = db.Column(db.String(64), nullable=False, index=True)

    target_name = db.Column(db.String(128), index=True)
    target_system = db.Column(db.String(128), index=True)
    target_station = db.Column(db.String(128))
    market_id = db.Column(db.BigInteger, nullable=False, index=True)

    commodity_key = db.Column(db.String(128), nullable=False, index=True)
    commodity = db.Column(db.String(128), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)

    source = db.Column(db.String(64))
    verification_source = db.Column(db.String(64))
    event_id = db.Column(db.Integer)
    note = db.Column(db.Text)

    created_at = db.Column(db.String(64), nullable=False, index=True)
    received_at = db.Column(db.String(64), nullable=False)
    payload_json = db.Column(db.Text)

    __table_args__ = (
        db.Index("idx_colonisation_delivery_market_cmdr", "market_id", "cmdr"),
        db.Index("idx_colonisation_delivery_market_session", "market_id", "session_id"),
    )

class ColonisationAssistStatus(db.Model):
    __tablename__ = "colonisation_assist_status"

    id = db.Column(db.Integer, primary_key=True)
    status_id = db.Column(db.String(128), unique=True, nullable=False, index=True)
    session_id = db.Column(db.String(128), index=True)

    client_id = db.Column(db.String(128), index=True)
    client_name = db.Column(db.String(128))
    cmdr = db.Column(db.String(64), nullable=False, index=True)

    target_name = db.Column(db.String(128), index=True)
    target_system = db.Column(db.String(128), index=True)
    target_station = db.Column(db.String(128))
    market_id = db.Column(db.BigInteger, nullable=False, index=True)

    phase = db.Column(db.String(64))
    reason = db.Column(db.Text)
    cargo_count = db.Column(db.Integer)
    updated_at = db.Column(db.String(64), nullable=False, index=True)
    received_at = db.Column(db.String(64), nullable=False)
    payload_json = db.Column(db.Text)

    __table_args__ = (
        db.Index("idx_colonisation_status_market_cmdr", "market_id", "cmdr"),
        db.Index("idx_colonisation_status_market_updated", "market_id", "updated_at"),
    )

class ProtectedFaction(db.Model):
    __tablename__ = "protected_faction"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), unique=True, nullable=False)
    webhook_url = db.Column(db.String(256))
    description = db.Column(db.String(128))
    protected = db.Column(db.Boolean, default=True)

class BGSEvalRun(db.Model):
    """
    One row per evaluation run.
    IMPORTANT: ticktime is the ONLY tick key (string).
    """
    __tablename__ = "bgs_eval_run"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    ticktime = db.Column(db.String(64), nullable=False, index=True)  # ticktime-only
    created_at = db.Column(db.String(64), nullable=False)            # ISO string
    eval_type = db.Column(db.String(64), nullable=False)
    version = db.Column(db.String(32), nullable=False)
    meta_json = db.Column(db.Text, nullable=True)
    __table_args__ = (
        db.Index("idx_bgs_eval_run_ticktime", "ticktime"),
    )

class BGSEvalResult(db.Model):
    """
    One row per (ticktime, system_name, faction).
    IMPORTANT: ticktime is the ONLY tick key (string).
    """
    __tablename__ = "bgs_eval_result"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    ticktime = db.Column(db.String(64), nullable=False, index=True)     # ticktime-only
    system_name = db.Column(db.String(128), nullable=False, index=True)
    faction = db.Column(db.String(128), nullable=False, index=True)
    total_effect = db.Column(db.Float, nullable=False)
    breakdown_json = db.Column(db.Text, nullable=True)
    cmdr_count = db.Column(db.Integer, nullable=True)
    cmdr_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.String(64), nullable=False)               # ISO string
    __table_args__ = (
        db.UniqueConstraint("ticktime", "system_name", "faction", name="uq_bgs_eval_result"),
        db.Index("idx_bgs_eval_result_ticktime", "ticktime"),
        db.Index("idx_bgs_eval_result_system", "system_name"),
        db.Index("idx_bgs_eval_result_faction", "faction"),
    )
