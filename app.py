"""
Steam Shame - A web app that calculates your Steam library shame score.
"""

from flask import Flask, redirect, request, session, url_for, render_template, jsonify
import requests
import os
import re
import random
import time
import math
import threading
from urllib.parse import urlencode
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-this")

STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "")

_store_cache = {}
_store_cache_lock = threading.Lock()
STORE_CACHE_TTL = 3600


# ============== Steam API ==============

def get_owned_games(steam_id: str) -> dict:
    url = "http://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
    params = {"key": STEAM_API_KEY, "steamid": steam_id,
              "include_appinfo": True, "include_played_free_games": True, "format": "json"}
    r = requests.get(url, params=params); r.raise_for_status(); return r.json()

def get_player_summary(steam_id: str) -> dict:
    url = "http://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
    params = {"key": STEAM_API_KEY, "steamids": steam_id, "format": "json"}
    r = requests.get(url, params=params); r.raise_for_status(); return r.json()

def resolve_vanity_url(vanity_name: str) -> str:
    url = "http://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/"
    params = {"key": STEAM_API_KEY, "vanityurl": vanity_name, "format": "json"}
    r = requests.get(url, params=params); r.raise_for_status()
    data = r.json()
    if data.get("response", {}).get("success") == 1:
        return data["response"]["steamid"]
    return None

def get_friends_list(steam_id: str) -> list:
    url = "http://api.steampowered.com/ISteamUser/GetFriendList/v1/"
    params = {"key": STEAM_API_KEY, "steamid": steam_id, "relationship": "friend", "format": "json"}
    r = requests.get(url, params=params)
    if r.status_code == 401: return []
    r.raise_for_status()
    return r.json().get("friendslist", {}).get("friends", [])

def get_app_details(appid: int) -> dict:
    now = time.time()
    with _store_cache_lock:
        cached = _store_cache.get(appid)
        if cached and (now - cached["ts"]) < STORE_CACHE_TTL:
            return cached["data"]
    try:
        r = requests.get(f"https://store.steampowered.com/api/appdetails?appids={appid}", timeout=10)
        if r.status_code == 200:
            app_data = r.json().get(str(appid), {})
            if app_data.get("success"):
                result = app_data.get("data", {})
                with _store_cache_lock:
                    _store_cache[appid] = {"data": result, "ts": now}
                return result
    except Exception: pass
    return None

def get_app_details_batch(appids: list, max_workers=5, delay=0.35) -> dict:
    results = {}
    def fetch_one(appid):
        time.sleep(random.uniform(0.1, delay))
        return appid, get_app_details(appid)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_one, aid): aid for aid in appids}
        for f in as_completed(futures):
            try:
                appid, data = f.result()
                if data: results[appid] = data
            except: continue
    return results

def extract_usd_price(details: dict) -> float:
    price_data = details.get("price_overview")
    if not price_data: return None
    currency = price_data.get("currency", "")
    if currency and currency != "USD": return None
    price_cents = price_data.get("final", price_data.get("initial", 0))
    price_dollars = price_cents / 100
    if price_dollars > 80: return None
    return price_dollars


# ============== Analysis ==============

def format_playtime(minutes: int) -> str:
    if minutes < 60: return f"{minutes}m"
    hours = minutes / 60
    if hours < 24: return f"{hours:.1f}h"
    return f"{hours / 24:.1f} days"


def calculate_shame_score(never_played_count: int, abandoned_count: int, total_games: int) -> float:
    """Shame = percentage-driven with a gentle volume nudge.
    base = (never_played + 0.5*abandoned) / total * 100
    volume_mult approaches 1.0 from 0.65. Everyone gets some shame.
    """
    if total_games == 0: return 0.0
    shame_units = never_played_count + (abandoned_count * 0.5)
    base_pct = (shame_units / total_games) * 100
    volume_mult = min(1.0, 0.65 + 0.35 * (math.log2(max(total_games, 2)) / math.log2(500)))
    return round(min(base_pct * volume_mult, 99.9), 1)


def analyze_library(games: list) -> dict:
    if not games: return None
    thirty_days_ago = time.time() - (30 * 24 * 60 * 60)

    def is_recent(game):
        lp = game.get("rtime_last_played", 0)
        if lp and lp > thirty_days_ago: return True
        if game.get("playtime_2weeks", 0) > 0: return True
        return False

    total_games = len(games)

    # Three clean categories (raw — for pie chart)
    raw_played = [g for g in games if g.get("playtime_forever", 0) > 60]
    raw_abandoned = [g for g in games if 1 <= g.get("playtime_forever", 0) <= 60]
    raw_unplayed = [g for g in games if g.get("playtime_forever", 0) == 0]

    # Shame-filtered (exclude recent from shame lists)
    played = sorted(raw_played, key=lambda x: x["playtime_forever"], reverse=True)
    abandoned = sorted([g for g in raw_abandoned if not is_recent(g)], key=lambda x: x["playtime_forever"])
    unplayed = [g for g in raw_unplayed if not is_recent(g)]

    shame_score = calculate_shame_score(len(raw_unplayed), len(raw_abandoned), total_games)

    if shame_score > 55: verdict = "You have a problem. Stop buying games."
    elif shame_score > 40: verdict = "Steam sales have claimed another victim."
    elif shame_score > 25: verdict = "Not bad, but that backlog isn't clearing itself."
    else: verdict = "Impressive restraint. Or new account."

    def game_list(glist, limit=30):
        return [{"name": g.get("name", "Unknown"), "appid": g.get("appid"),
                 "playtime": g.get("playtime_forever", 0)} for g in glist[:limit]]

    backlog_days = round((len(raw_unplayed) * 10) / 1)  # 10 hrs avg at 1 hr/day

    return {
        "total_games": total_games,
        "played_count": len(raw_played),
        "abandoned_count": len(raw_abandoned),
        "never_played_count": len(raw_unplayed),
        "played_games": game_list(played, 30),
        "abandoned_games": game_list(abandoned, 30),
        "unplayed_games": game_list(random.sample(unplayed, min(30, len(unplayed))) if unplayed else [], 30),
        "played_total": len(played),
        "abandoned_total": len(abandoned),
        "unplayed_total": len(unplayed),
        "shame_score": shame_score,
        "verdict": verdict,
        "backlog_days": backlog_days,
    }


# ============== Genre Analysis ==============

GENRE_CATEGORIES = {
    "fps_shooter": {"names": ["FPS", "Shooter", "First-Person Shooter", "Third-Person Shooter"], "label": "Shooter", "emoji": "🔫"},
    "rpg": {"names": ["RPG", "JRPG", "Action RPG", "Turn-Based RPG", "CRPG", "Role-Playing"], "label": "RPG", "emoji": "⚔️"},
    "strategy": {"names": ["Strategy", "Real-Time Strategy", "Turn-Based Strategy", "Tower Defense", "RTS", "4X", "Grand Strategy"], "label": "Strategy", "emoji": "🧠"},
    "survival": {"names": ["Survival", "Survival Horror", "Crafting", "Base Building", "Open World Survival Craft"], "label": "Survival", "emoji": "🏕️"},
    "simulation": {"names": ["Simulation", "Life Sim", "Farming Sim", "Management", "City Builder", "Building"], "label": "Simulation", "emoji": "🏗️"},
    "action": {"names": ["Action", "Hack and Slash", "Beat 'em up", "Action-Adventure"], "label": "Action", "emoji": "💥"},
    "puzzle": {"names": ["Puzzle", "Logic", "Hidden Object"], "label": "Puzzle", "emoji": "🧩"},
    "platformer": {"names": ["Platformer", "2D Platformer", "3D Platformer", "Precision Platformer"], "label": "Platformer", "emoji": "🍄"},
    "horror": {"names": ["Horror", "Psychological Horror", "Survival Horror"], "label": "Horror", "emoji": "👻"},
    "racing": {"names": ["Racing", "Driving", "Automobile Sim"], "label": "Racing", "emoji": "🏎️"},
    "sports": {"names": ["Sports", "Football", "Basketball", "Baseball", "Soccer", "Golf"], "label": "Sports", "emoji": "⚽"},
    "sandbox": {"names": ["Sandbox", "Open World", "Exploration"], "label": "Open World", "emoji": "🌍"},
    "roguelike": {"names": ["Roguelike", "Roguelite", "Roguevania", "Procedural Generation"], "label": "Roguelike", "emoji": "💀"},
    "multiplayer": {"names": ["Massively Multiplayer", "MMO", "MMORPG", "Co-op", "Multiplayer"], "label": "Multiplayer", "emoji": "👥"},
    "casual": {"names": ["Casual", "Clicker", "Idle", "Card Game", "Board Game"], "label": "Casual", "emoji": "🎲"},
    "visual_novel": {"names": ["Visual Novel", "Dating Sim", "Choose Your Own Adventure", "Interactive Fiction"], "label": "Visual Novel", "emoji": "📖"},
    "fighting": {"names": ["Fighting", "Martial Arts"], "label": "Fighting", "emoji": "🥊"},
}

def classify_game_genres(store_data: dict) -> list:
    categories = set()
    genres = [g.get("description", "") for g in store_data.get("genres", [])]
    tags = [c.get("description", "") for c in store_data.get("categories", [])]
    all_labels = genres + tags
    for cat_key, cat_info in GENRE_CATEGORIES.items():
        for name in cat_info["names"]:
            if name.lower() in [l.lower() for l in all_labels]:
                categories.add(cat_key); break
    return list(categories)

def detect_badges(stats, store_details, games):
    badges = []
    if stats["total_games"] > 200 and stats["shame_score"] > 35:
        badges.append({"name": "Humble Bundle Victim", "emoji": "📦", "description": "200+ games, most untouched."})
    ea = sum(1 for d in store_details.values() if "early access" in [g.get("description","").lower() for g in d.get("genres",[])])
    if ea >= 5: badges.append({"name": "Early Access Addict", "emoji": "🚧", "description": f"{ea} Early Access games."})
    tm = sum(g.get("playtime_forever",0) for g in games)
    if tm > 0:
        tg = max(games, key=lambda g: g.get("playtime_forever",0))
        tp = (tg["playtime_forever"]/tm)*100
        if tp > 50: badges.append({"name": "One-Trick Pony", "emoji": "🐴", "description": f"{tp:.0f}% of time in {tg.get('name','one game')}."})
    if stats["total_games"] >= 500: badges.append({"name": "Game Collector", "emoji": "🏛️", "description": f"{stats['total_games']} games."})
    qa = len([g for g in games if 0 < g.get("playtime_forever",0) < 30])
    if qa >= 20: badges.append({"name": "Speedrun Abandoner", "emoji": "⏱️", "description": f"{qa} games under 30 min."})
    if stats["total_games"] < 50 and stats["shame_score"] < 20: badges.append({"name": "Disciplined Buyer", "emoji": "🎯", "description": "Small library, actually played."})
    f2p = sum(1 for d in store_details.values() if d.get("is_free", False))
    if f2p >= 10: badges.append({"name": "F2P Warrior", "emoji": "🆓", "description": f"{f2p} free games."})
    return badges[:6]


# ============== Routes ==============

@app.route("/")
def index(): return render_template("index.html")

@app.route("/lookup", methods=["POST"])
def lookup():
    si = request.form.get("steam_input", "").strip()
    if not si: return redirect(url_for("index"))
    steam_id = None
    if re.match(r"^\d{17}$", si): steam_id = si
    elif "steamcommunity.com" in si:
        m = re.search(r"steamcommunity\.com/(?:profiles|id)/([^/\?]+)", si)
        if m:
            v = m.group(1)
            steam_id = v if re.match(r"^\d{17}$", v) else resolve_vanity_url(v)
    else: steam_id = resolve_vanity_url(si)
    if not steam_id: return render_template("index.html", error="Could not find that Steam profile.")
    return redirect(url_for("results", steam_id=steam_id))

@app.route("/results/<steam_id>")
def results(steam_id):
    try:
        pd = get_player_summary(steam_id)
        players = pd.get("response",{}).get("players",[])
        if not players: return render_template("index.html", error="Could not find that Steam profile.")
        p = players[0]
        if p.get("communityvisibilitystate") != 3:
            return render_template("error.html", error="This profile is private", message="Game details need to be public.")
        gd = get_owned_games(steam_id)
        games = gd.get("response",{}).get("games",[])
        if not games: return render_template("error.html", error="No games found", message="No games or game details are private.")
        stats = analyze_library(games)
        return render_template("results.html", player_name=p.get("personaname","Unknown"),
            avatar_url=p.get("avatarfull",""), steam_id=steam_id, stats=stats)
    except Exception as e:
        return render_template("error.html", error="Something went wrong", message=str(e))


# ============== Async API ==============

@app.route("/api/value/<steam_id>")
def api_value(steam_id):
    full_scan = request.args.get("full","0") == "1"
    try:
        games = get_owned_games(steam_id).get("response",{}).get("games",[])
        if not games: return jsonify({"error": "No games"}), 404
        played = [g for g in games if g.get("playtime_forever",0) > 0]
        unplayed = [g for g in games if g.get("playtime_forever",0) == 0]
        sp = played if full_scan else (random.sample(played, min(40, len(played))) if played else [])
        su = unplayed if full_scan else (random.sample(unplayed, min(40, len(unplayed))) if unplayed else [])
        sd = get_app_details_batch([g["appid"] for g in sp + su], max_workers=5, delay=0.35)

        pp, uwp = [], []
        for g in sp:
            d = sd.get(g["appid"]); pr = extract_usd_price(d) if d else None
            if pr and pr > 0: pp.append(pr)
        for g in su:
            d = sd.get(g["appid"]); pr = extract_usd_price(d) if d else None
            if pr and pr > 0: uwp.append({"name": g.get("name","Unknown"), "price": pr})

        up = [x["price"] for x in uwp]
        if full_scan:
            tpv, tuv, ie = sum(pp), sum(up), False
        else:
            ap = (sum(pp)/len(pp)) if pp else 0
            au = (sum(up)/len(up)) if up else 0
            tpv, tuv, ie = ap*len(played), au*len(unplayed), True

        regrets = sorted(uwp, key=lambda x: x["price"], reverse=True)[:5]
        return jsonify({"library_value": round(tpv+tuv), "unplayed_value": round(tuv),
            "is_estimate": ie, "regrets": [{"name":r["name"],"price":r["price"]} for r in regrets]})
    except Exception as e: return jsonify({"error": str(e)}), 500


@app.route("/api/personality/<steam_id>")
def api_personality(steam_id):
    try:
        games = get_owned_games(steam_id).get("response",{}).get("games",[])
        if not games: return jsonify({"error": "No games"}), 404
        tda = time.time() - (30*24*60*60)
        pg = sorted([g for g in games if g.get("playtime_forever",0) > 60], key=lambda x: x["playtime_forever"], reverse=True)
        ug = [g for g in games if g.get("playtime_forever",0)==0 and not (g.get("rtime_last_played",0)>tda) and not (g.get("playtime_2weeks",0)>0)]
        ps, us = pg[:40], random.sample(ug, min(40, len(ug)))
        os_ = random.sample(games, min(60, len(games)))
        aids = list(set(g["appid"] for g in ps+us+os_))
        sd = get_app_details_batch(aids, max_workers=5, delay=0.35)

        def cg(gl):
            c, n = {}, {}
            for g in gl:
                d = sd.get(g["appid"])
                if not d: continue
                cats = classify_game_genres(d); nm = g.get("name","Unknown")
                for cat in cats:
                    c[cat] = c.get(cat,0)+1; n.setdefault(cat,[]).append(nm)
            return c, n

        oc, og = cg(os_); pc, pgn = cg(ps); uc, ugn = cg(us)
        all_g = sorted(set(list(oc)+list(pc)+list(uc)))

        def norm(counts):
            t = sum(counts.values()) or 1
            return {k: round((counts.get(k,0)/t)*100,1) for k in all_g}

        labels = [{"key":gk,"label":GENRE_CATEGORIES.get(gk,{}).get("label",gk),"emoji":GENRE_CATEGORIES.get(gk,{}).get("emoji","🎮")} for gk in all_g]
        radar = {"labels":labels, "owned":[norm(oc).get(k,0) for k in all_g],
                 "played":[norm(pc).get(k,0) for k in all_g], "unplayed":[norm(uc).get(k,0) for k in all_g]}
        gg = {gk: {"owned":og.get(gk,[]),"played":pgn.get(gk,[]),"unplayed":ugn.get(gk,[])} for gk in all_g}

        def maj(counts):
            if not counts: return None
            top = max(counts.items(), key=lambda x: x[1]); t = sum(counts.values()) or 1
            i = GENRE_CATEGORIES.get(top[0],{})
            return {"key":top[0],"label":i.get("label",top[0]),"emoji":i.get("emoji","🎮"),"pct":round((top[1]/t)*100,1)}

        om, pm, um = maj(oc), maj(pc), maj(uc)
        stats = analyze_library(games)
        badges = detect_badges(stats, sd, games)
        return jsonify({"radar":radar, "genre_games":gg, "overall_majority":om, "played_majority":pm,
            "unplayed_majority":um, "show_unplayed_mismatch": pm and um and pm["key"]!=um["key"], "badges":badges})
    except Exception as e: return jsonify({"error": str(e)}), 500


@app.route("/api/friends/<steam_id>")
def api_friends(steam_id):
    try:
        pd = get_player_summary(steam_id)
        ps = pd.get("response",{}).get("players",[])
        if not ps or ps[0].get("communityvisibilitystate") != 3: return jsonify({"error":"Profile not accessible"}), 403
        friends = get_friends_list(steam_id)
        if not friends: return jsonify({"leaderboard":[],"user_rank":None,"error":"No friends found"})
        aids = [steam_id]+[f["steamid"] for f in friends[:50]]
        aps = []
        for i in range(0,len(aids),100):
            bd = get_player_summary(",".join(aids[i:i+100]))
            aps.extend(bd.get("response",{}).get("players",[]))
        lb = []
        for p in aps:
            pid = p.get("steamid")
            if p.get("communityvisibilitystate") != 3: continue
            try:
                gl = get_owned_games(pid).get("response",{}).get("games",[])
                if not gl: continue
                s = analyze_library(gl)
                if s["shame_score"] >= 99.9: continue
                lb.append({"steam_id":pid,"name":p.get("personaname","Unknown"),"avatar":p.get("avatar",""),
                    "shame_score":s["shame_score"],"total_games":s["total_games"],"played_count":s["played_count"],
                    "never_played":s["never_played_count"],"is_user":pid==steam_id})
            except: continue
        lb.sort(key=lambda x: x["shame_score"], reverse=True)
        ur = None
        for i,e in enumerate(lb): e["rank"]=i+1; ur = i+1 if e["is_user"] else ur
        return jsonify({"leaderboard":lb[:10],"total_friends":len(lb)-1,"user_rank":ur})
    except Exception as e: return jsonify({"error":str(e)}), 500


@app.route("/friends/<steam_id>")
def friends_leaderboard(steam_id):
    try:
        pd = get_player_summary(steam_id); ps = pd.get("response",{}).get("players",[])
        if not ps: return render_template("error.html", error="Not found", message="Profile not found.")
        p = ps[0]
        if p.get("communityvisibilitystate") != 3: return render_template("error.html", error="Private", message="Profile needs to be public.")
        friends = get_friends_list(steam_id)
        if not friends: return render_template("error.html", error="No friends", message="Friends list is private or empty.")
        aids = [steam_id]+[f["steamid"] for f in friends]
        aps = []
        for i in range(0,len(aids),100):
            bd = get_player_summary(",".join(aids[i:i+100]))
            aps.extend(bd.get("response",{}).get("players",[]))
        lb = []
        for pl in aps:
            pid = pl.get("steamid")
            if pl.get("communityvisibilitystate") != 3: continue
            try:
                gl = get_owned_games(pid).get("response",{}).get("games",[])
                if not gl: continue
                s = analyze_library(gl)
                lb.append({"steam_id":pid,"name":pl.get("personaname","Unknown"),"avatar":pl.get("avatar",""),
                    "shame_score":s["shame_score"],"total_games":s["total_games"],"played_count":s["played_count"],
                    "never_played":s["never_played_count"],"is_user":pid==steam_id})
            except: continue
        lb.sort(key=lambda x: x["shame_score"], reverse=True)
        for i,e in enumerate(lb): e["rank"]=i+1
        ur = next((e["rank"] for e in lb if e["is_user"]),None)
        return render_template("friends.html", player_name=p.get("personaname","Unknown"),
            avatar_url=p.get("avatarfull",""), steam_id=steam_id, leaderboard=lb, user_rank=ur, total_friends=len(lb)-1)
    except Exception as e: return render_template("error.html", error="Error", message=str(e))

if __name__ == "__main__":
    if not STEAM_API_KEY: print("Warning: STEAM_API_KEY not set!")
    app.run(debug=os.environ.get("FLASK_ENV")=="development", host="0.0.0.0", port=int(os.environ.get("PORT",5000)))
