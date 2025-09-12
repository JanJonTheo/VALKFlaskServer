from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

class Event(db.Model):
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
            raw_json=str(data)
        )

class MarketBuyEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('event.id'), nullable=False)
    stock = db.Column(db.Integer)
    stock_bracket = db.Column(db.Integer)
    value = db.Column(db.Integer)
    count = db.Column(db.Integer)

class MarketSellEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('event.id'), nullable=False)
    demand = db.Column(db.Integer)
    demand_bracket = db.Column(db.Integer)
    profit = db.Column(db.Integer)
    value = db.Column(db.Integer)
    count = db.Column(db.Integer)

class MissionCompletedEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('event.id'), nullable=False)
    awarding_faction = db.Column(db.String(128))
    mission_name = db.Column(db.String(128))
    reward = db.Column(db.Integer)

class MissionCompletedInfluence(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    mission_id = db.Column(db.Integer, db.ForeignKey('mission_completed_event.id'), nullable=False)
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
    mission_name = db.Column(db.String(128))
    awarding_faction = db.Column(db.String(128))
    fine = db.Column(db.Integer)

class MultiSellExplorationDataEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('event.id'), nullable=False)
    total_earnings = db.Column(db.Integer)

class RedeemVoucherEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('event.id'), nullable=False)
    amount = db.Column(db.Integer)
    faction = db.Column(db.String(128))
    type = db.Column(db.String(128))

class SellExplorationDataEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('event.id'), nullable=False)
    earnings = db.Column(db.Integer)

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

class ProtectedFaction(db.Model):
    __tablename__ = "protected_faction"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), unique=True, nullable=False)
    webhook_url = db.Column(db.String(256))
    description = db.Column(db.String(128))
    protected = db.Column(db.Boolean, default=True)
