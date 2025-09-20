from flask import Blueprint, request, jsonify, g
from models import db
from models import Activity, System, Faction
from sqlalchemy import text
import json

activities_bp = Blueprint("activities", __name__)


# Funktion zum Abrufen von Aktivitäten mit Filtern
def get_activities(tick_filter=None, cmdr=None, system_name=None, faction_name=None):

    query = db.session.query(Activity)

    # Filter anwenden
    if tick_filter == "current":
        tickid_row = db.session.execute(text("SELECT tickid FROM activity ORDER BY timestamp DESC LIMIT 1")).fetchone()
        if tickid_row and tickid_row[0]:
            query = query.filter(Activity.tickid == tickid_row[0])
    elif tick_filter == "last":
        tickids = db.session.execute(text("SELECT DISTINCT tickid FROM activity ORDER BY timestamp DESC LIMIT 2")).fetchall()
        if len(tickids) == 2:
            query = query.filter(Activity.tickid == tickids[1][0])
    elif tick_filter:
        query = query.filter(Activity.tickid == tick_filter)
    if cmdr:
        query = query.filter(Activity.cmdr == cmdr)

    activities = query.all()
    result = []
    for activity in activities:
        activity_dict = {
            "tickid": activity.tickid,
            "ticktime": activity.ticktime,
            "timestamp": activity.timestamp,
            "cmdr": activity.cmdr,
            "systems": []
        }
        for system in activity.systems:
            if system_name and system.name != system_name:
                continue
            system_dict = {
                "name": system.name,
                "address": system.address,
                "twkills": json.loads(system.twkills) if system.twkills else None,
                "twsandr": json.loads(system.twsandr) if system.twsandr else None,
                "twreactivate": system.twreactivate,
                "factions": []
            }
            for faction in system.factions:
                if faction_name and faction.name != faction_name:
                    continue
                faction_dict = {
                    "name": faction.name,
                    "state": faction.state,
                    "bvs": faction.bvs,
                    "cbs": faction.cbs,
                    "exobiology": faction.exobiology,
                    "exploration": faction.exploration,
                    "scenarios": faction.scenarios,
                    "infprimary": faction.infprimary,
                    "infsecondary": faction.infsecondary,
                    "missionfails": faction.missionfails,
                    "murdersground": faction.murdersground,
                    "murdersspace": faction.murdersspace,
                    "tradebm": faction.tradebm,
                    "stations": json.loads(faction.stations) if faction.stations else None,
                    "czground": json.loads(faction.czground) if faction.czground else None,
                    "czspace": json.loads(faction.czspace) if faction.czspace else None,
                    "tradebuy": json.loads(faction.tradebuy) if faction.tradebuy else None,
                    "tradesell": json.loads(faction.tradesell) if faction.tradesell else None,
                    "sandr": json.loads(faction.sandr) if faction.sandr else None
                }
                system_dict["factions"].append(faction_dict)
            activity_dict["systems"].append(system_dict)
        result.append(activity_dict)
    return result


# Multi-Tenant API-Endpunkt
@activities_bp.route("/api/activities", methods=["GET"])
def activities_api():
    # API-Key aus Header holen und Tenant setzen
    from app import get_tenant_by_apikey, set_tenant_db_config
    apikey = request.headers.get("apikey")
    tenant = get_tenant_by_apikey(apikey)
    if not tenant:
        return jsonify({"error": "Unauthorized: Invalid API key"}), 401
    g.tenant = tenant
    set_tenant_db_config(tenant)
    if hasattr(g, "tenant_db_error"):
        return jsonify({"error": f"Tenant-Datenbank nicht erreichbar: {g.tenant_db_error}"}), 500

    # Filter aus Query-Parametern
    tick_filter = request.args.get("tick")
    cmdr = request.args.get("cmdr")
    system_name = request.args.get("system")
    faction_name = request.args.get("faction")

    try:
        result = get_activities(
            tick_filter=tick_filter,
            cmdr=cmdr,
            system_name=system_name,
            faction_name=faction_name
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@activities_bp.route("/activities", methods=["PUT"])
def put_activities():
    from models import Activity, System, Faction, db
    try:
        activity_data = request.get_json()
        validated = activity_data

        # Suche nach vorhandenem Activity-Datensatz
        activity = db.session.query(Activity).filter_by(
            tickid=validated['tickid'],
            cmdr=validated.get('cmdr')
        ).first()
        if not activity:
            activity = Activity(
                tickid=validated['tickid'],
                ticktime=validated['ticktime'],
                timestamp=validated['timestamp'],
                cmdr=validated.get('cmdr')
            )
            db.session.add(activity)
        else:
            activity.ticktime = validated['ticktime']
            activity.timestamp = validated['timestamp']

        # Systeme aktualisieren/hinzufügen
        for sys in validated['systems']:
            system = db.session.query(System).filter_by(
                activity_id=activity.id,
                name=sys['name'],
                address=sys['address']
            ).first()
            if not system:
                system = System(
                    name=sys['name'],
                    address=sys['address']
                )
                activity.systems.append(system)
            # Felder aktualisieren
            if 'twkills' in sys:
                system.twkills = json.dumps(sys['twkills'])
            if 'twsandr' in sys:
                system.twsandr = json.dumps(sys['twsandr'])
            if 'twreactivate' in sys:
                system.twreactivate = sys['twreactivate']

            # Fraktionen aktualisieren/hinzufügen
            for fac in sys['factions']:
                faction = db.session.query(Faction).filter_by(
                    system_id=system.id,
                    name=fac['name']
                ).first()
                if not faction:
                    faction = Faction(
                        name=fac['name'],
                        state=fac['state'],
                        bvs=fac.get('bvs', 0),
                        cbs=fac.get('cbs', 0),
                        exobiology=fac.get('exobiology', 0),
                        exploration=fac.get('exploration', 0),
                        scenarios=fac.get('scenarios', 0),
                        infprimary=fac.get('infprimary', 0),
                        infsecondary=fac.get('infsecondary', 0),
                        missionfails=fac.get('missionfails', 0),
                        murdersground=fac.get('murdersground', 0),
                        murdersspace=fac.get('murdersspace', 0),
                        tradebm=fac.get('tradebm', 0)
                    )
                    system.factions.append(faction)
                else:
                    faction.state = fac['state']
                    faction.bvs = fac.get('bvs', 0)
                    faction.cbs = fac.get('cbs', 0)
                    faction.exobiology = fac.get('exobiology', 0)
                    faction.exploration = fac.get('exploration', 0)
                    faction.scenarios = fac.get('scenarios', 0)
                    faction.infprimary = fac.get('infprimary', 0)
                    faction.infsecondary = fac.get('infsecondary', 0)
                    faction.missionfails = fac.get('missionfails', 0)
                    faction.murdersground = fac.get('murdersground', 0)
                    faction.murdersspace = fac.get('murdersspace', 0)
                    faction.tradebm = fac.get('tradebm', 0)
                # Neue Felder für Faction
                if 'stations' in fac:
                    faction.stations = json.dumps(fac['stations'])
                if 'czground' in fac:
                    faction.czground = json.dumps(fac['czground'])
                if 'czspace' in fac:
                    faction.czspace = json.dumps(fac['czspace'])
                if 'tradebuy' in fac:
                    faction.tradebuy = json.dumps(fac['tradebuy'])
                if 'tradesell' in fac:
                    faction.tradesell = json.dumps(fac['tradesell'])
                if 'sandr' in fac:
                    faction.sandr = json.dumps(fac['sandr'])

        db.session.commit()

        return jsonify({"status": "activity saved"}), 200
    except Exception as e:
        db.session.rollback()
        import logging
        logging.getLogger(__name__).error(f"Activity processing error: {str(e)}")
        return jsonify({"error": str(e)}), 400
