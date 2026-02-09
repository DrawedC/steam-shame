"""
Steam Shame - A web app that calculates your Steam library shame score.
"""
from flask import Flask, redirect, request, url_for, render_template, jsonify, send_file
import requests, os, re, random, time, math, threading, io
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image, ImageDraw, ImageFont
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import io
import os

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-this")
STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "")

# Caches
_store_cache = {}
_store_cache_lock = threading.Lock()
STORE_CACHE_TTL = 86400  # 24 hours — genres rarely change

_games_cache = {}
_games_cache_lock = threading.Lock()
GAMES_CACHE_TTL = 300  # 5 min — avoids re-fetching for async endpoints

import logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("steam-shame")

# ============== Steam API ==============
def get_owned_games(steam_id):
    """Fetch owned games with short-term cache to avoid redundant calls."""
    now = time.time()
    with _games_cache_lock:
        c = _games_cache.get(steam_id)
        if c and (now - c["ts"]) < GAMES_CACHE_TTL:
            return c["data"]
    try:
        r = requests.get("http://api.steampowered.com/IPlayerService/GetOwnedGames/v1/",
            params={"key":STEAM_API_KEY,"steamid":steam_id,"include_appinfo":True,
                    "include_played_free_games":True,"format":"json"}, timeout=15)
        r.raise_for_status()
        data = r.json()
        with _games_cache_lock:
            _games_cache[steam_id] = {"data":data,"ts":now}
        return data
    except requests.exceptions.HTTPError as e:
        log.warning(f"Steam API error for {steam_id}: {e}")
        raise
    except requests.exceptions.Timeout:
        log.warning(f"Steam API timeout for {steam_id}")
        raise

def get_player_summary(steam_id):
    r = requests.get("http://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/",
        params={"key":STEAM_API_KEY,"steamids":steam_id,"format":"json"}, timeout=15)
    r.raise_for_status(); return r.json()

def resolve_vanity_url(vanity_name):
    r = requests.get("http://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/",
        params={"key":STEAM_API_KEY,"vanityurl":vanity_name,"format":"json"}, timeout=15)
    r.raise_for_status(); d = r.json()
    return d["response"]["steamid"] if d.get("response",{}).get("success")==1 else None

def get_friends_list(steam_id):
    try:
        r = requests.get("http://api.steampowered.com/ISteamUser/GetFriendList/v1/",
            params={"key":STEAM_API_KEY,"steamid":steam_id,"relationship":"friend","format":"json"}, timeout=15)
        if r.status_code == 401: return []
        r.raise_for_status(); return r.json().get("friendslist",{}).get("friends",[])
    except: return []

def get_app_details(appid):
    """Fetch store details with cache. Returns None on failure (rate limit, timeout, etc)."""
    now = time.time()
    with _store_cache_lock:
        c = _store_cache.get(appid)
        if c and (now - c["ts"]) < STORE_CACHE_TTL: return c["data"]
    try:
        r = requests.get(f"https://store.steampowered.com/api/appdetails?appids={appid}", timeout=10)
        if r.status_code == 429:
            log.warning(f"Store API rate limited on appid {appid}")
            return None
        if r.status_code == 200:
            ad = r.json().get(str(appid),{})
            if ad.get("success"):
                result = ad.get("data",{})
                with _store_cache_lock: _store_cache[appid] = {"data":result,"ts":now}
                return result
    except Exception as e:
        log.debug(f"Store API error for {appid}: {e}")
    return None

def get_app_details_batch(appids, max_workers=4, delay=0.5):
    """Batch fetch store details. Slower but more reliable to avoid rate limits."""
    results = {}
    def fetch(aid):
        time.sleep(random.uniform(0.2, delay)); return aid, get_app_details(aid)
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for f in as_completed({ex.submit(fetch, a): a for a in appids}):
            try:
                aid, d = f.result()
                if d: results[aid] = d
            except: pass
    log.info(f"Store batch: {len(results)}/{len(appids)} fetched")
    return results

def extract_usd_price(details):
    if not details: return None
    pd = details.get("price_overview")
    if not pd: return None
    if pd.get("currency","") not in ("","USD"): return None
    p = pd.get("final", pd.get("initial",0)) / 100
    return p if 0 < p <= 80 else None

# ============== Analysis ==============
def format_playtime(minutes):
    if minutes == 0: return "0m"
    if minutes < 60: return f"{minutes}m"
    hours = minutes / 60
    if hours < 24: return f"{hours:.1f}h"
    return f"{hours/24:.1f} days"

def calculate_shame_score(never_played, abandoned, total):
    if total == 0: return 0.0
    base = ((never_played + abandoned * 0.5) / total) * 100
    vol = min(1.0, 0.65 + 0.35 * (math.log2(max(total, 2)) / math.log2(500)))
    return round(min(base * vol, 99.9), 1)

def analyze_library(games):
    if not games: return None
    tda = time.time() - 30*86400
    def is_recent(g):
        lp = g.get("rtime_last_played",0)
        if lp and lp > tda: return True
        return g.get("playtime_2weeks",0) > 0

    total = len(games)
    raw_played = [g for g in games if g.get("playtime_forever",0) > 60]
    raw_abandoned = [g for g in games if 1 <= g.get("playtime_forever",0) <= 60]
    raw_unplayed = [g for g in games if g.get("playtime_forever",0) == 0]

    # Check for likely private game details: has games but none played at all
    any_playtime = any(g.get("playtime_forever",0) > 0 for g in games)

    played = sorted(raw_played, key=lambda x: x["playtime_forever"], reverse=True)
    abandoned = sorted([g for g in raw_abandoned if not is_recent(g)], key=lambda x: x["playtime_forever"])
    unplayed = [g for g in raw_unplayed if not is_recent(g)]
    shame = calculate_shame_score(len(raw_unplayed), len(raw_abandoned), total)

    if shame > 55: verdict = "You have a problem. Stop buying games."
    elif shame > 40: verdict = "Steam sales have claimed another victim."
    elif shame > 25: verdict = "Not bad, but that backlog isn't clearing itself."
    else: verdict = "Impressive restraint. Or new account."

    def gl(lst, limit=30):
        return [{"name":g.get("name","Unknown"),"appid":g.get("appid"),
                 "playtime":g.get("playtime_forever",0),
                 "playtime_fmt":format_playtime(g.get("playtime_forever",0))} for g in lst[:limit]]

    backlog_days = round(len(raw_unplayed) * 10)  # 10h avg at 1h/day

    # Pick a random unplayed game to suggest
    suggest = None
    if unplayed:
        pick = random.choice(unplayed)
        suggest = {"name": pick.get("name","Unknown"), "appid": pick.get("appid")}

    # Most played game
    most_played = None
    if played:
        top = played[0]
        most_played = {"name": top.get("name","Unknown"), "appid": top.get("appid"),
                       "playtime_fmt": format_playtime(top.get("playtime_forever",0))}

    result = {
        "total_games": total,
        "played_count": len(raw_played), "abandoned_count": len(raw_abandoned),
        "never_played_count": len(raw_unplayed),
        "any_playtime": any_playtime,
        "played_games": gl(played, 30),
        "abandoned_games": gl(abandoned, 30),
        "unplayed_games": gl(random.sample(unplayed, min(30, len(unplayed))) if unplayed else [], 30),
        "all_played": gl(played, 9999),
        "all_abandoned": gl(abandoned, 9999),
        "all_unplayed": gl(sorted(unplayed, key=lambda x: x.get("name","").lower()), 9999),
        "played_total": len(played), "abandoned_total": len(abandoned), "unplayed_total": len(unplayed),
        "shame_score": shame, "verdict": verdict, "backlog_days": backlog_days,
        "suggest": suggest, "most_played": most_played,
    }
    result["descriptor"] = detect_descriptor(result)
    return result

# ============== Genre ==============
GENRE_CATEGORIES = {
    "fps_shooter":{"names":["FPS","Shooter","First-Person Shooter","Third-Person Shooter"],"label":"Shooter","emoji":"🔫"},
    "rpg":{"names":["RPG","JRPG","Action RPG","Turn-Based RPG","CRPG","Role-Playing"],"label":"RPG","emoji":"⚔ï¸"},
    "strategy":{"names":["Strategy","Real-Time Strategy","Turn-Based Strategy","Tower Defense","RTS","4X","Grand Strategy"],"label":"Strategy","emoji":"🧠"},
    "survival":{"names":["Survival","Survival Horror","Crafting","Base Building","Open World Survival Craft"],"label":"Survival","emoji":"🏕️"},
    "simulation":{"names":["Simulation","Life Sim","Farming Sim","Management","City Builder","Building"],"label":"Simulation","emoji":"🏗️"},
    "action":{"names":["Action","Hack and Slash","Beat 'em up","Action-Adventure"],"label":"Action","emoji":"💥"},
    "puzzle":{"names":["Puzzle","Logic","Hidden Object"],"label":"Puzzle","emoji":"🧩"},
    "platformer":{"names":["Platformer","2D Platformer","3D Platformer","Precision Platformer"],"label":"Platformer","emoji":"🏄"},
    "horror":{"names":["Horror","Psychological Horror","Survival Horror"],"label":"Horror","emoji":"👻"},
    "racing":{"names":["Racing","Driving","Automobile Sim"],"label":"Racing","emoji":"🏎️"},
    "sports":{"names":["Sports","Football","Basketball","Baseball","Soccer","Golf"],"label":"Sports","emoji":"⚽"},
    "sandbox":{"names":["Sandbox","Open World","Exploration"],"label":"Open World","emoji":"🌍"},
    "roguelike":{"names":["Roguelike","Roguelite","Roguevania","Procedural Generation"],"label":"Roguelike","emoji":"💀"},
    "multiplayer":{"names":["Massively Multiplayer","MMO","MMORPG","Co-op","Multiplayer"],"label":"Multiplayer","emoji":"👥"},
    "casual":{"names":["Casual","Clicker","Idle","Card Game","Board Game"],"label":"Casual","emoji":"🎲"},
    "visual_novel":{"names":["Visual Novel","Dating Sim","Choose Your Own Adventure","Interactive Fiction"],"label":"Visual Novel","emoji":"📖"},
    "fighting":{"names":["Fighting","Martial Arts"],"label":"Fighting","emoji":"🥊"},
}

def classify_game_genres(store_data):
    cats = set()
    all_labels = [g.get("description","") for g in store_data.get("genres",[])] + \
                 [c.get("description","") for c in store_data.get("categories",[])]
    lower_labels = [l.lower() for l in all_labels]
    for k, v in GENRE_CATEGORIES.items():
        for n in v["names"]:
            if n.lower() in lower_labels: cats.add(k); break
    return list(cats)

def detect_descriptor(stats):
    """Primary identity based on play habits."""
    played_pct = (stats["played_count"] / stats["total_games"] * 100) if stats["total_games"] else 0
    abandoned_pct = (stats["abandoned_count"] / stats["total_games"] * 100) if stats["total_games"] else 0
    unplayed_pct = (stats["never_played_count"] / stats["total_games"] * 100) if stats["total_games"] else 0

    if played_pct > 50:
        return {"type": "player", "emoji": "🎮", "title": "The Player",
                "description": "You actually play your games. A rare breed."}
    elif abandoned_pct > played_pct and abandoned_pct > unplayed_pct:
        return {"type": "sampler", "emoji": "🧪", "title": "The Sampler",
                "description": "You try everything but commit to nothing."}
    else:
        return {"type": "collector", "emoji": "🏛️", "title": "The Collector",
                "description": "You buy games like they're going out of style. They're not."}


def detect_badges_instant(stats, games):
    """Badges computable instantly without store API calls."""
    badges = []

    if stats["never_played_count"] == 0:
        badges.append({"name": "Pristine Library", "emoji": "✨",
                       "description": "Zero unplayed games. You're either disciplined or just got here."})

    if stats["never_played_count"] >= 100:
        badges.append({"name": "Humble Bundle Victim", "emoji": "📦",
                       "description": f"{stats['never_played_count']} unplayed games. Those bundles got you good."})

    if stats["abandoned_count"] >= 30:
        badges.append({"name": "Acquired Tastes", "emoji": "🍷",
                       "description": f"{stats['abandoned_count']} games abandoned under an hour. Very particular."})

    tm = sum(g.get("playtime_forever", 0) for g in games)
    if tm > 0:
        tg = max(games, key=lambda g: g.get("playtime_forever", 0))
        tp = (tg["playtime_forever"] / tm) * 100
        if tp > 50:
            badges.append({"name": "One-Trick Pony", "emoji": "🐴",
                           "description": f"{tp:.0f}% of your time in {tg.get('name', 'one game')}."})

    qa = len([g for g in games if 0 < g.get("playtime_forever", 0) < 10])
    if qa >= 15:
        badges.append({"name": "10-Minute Rule", "emoji": "⏱️",
                       "description": f"{qa} games with under 10 minutes. Harsh critic."})

    return badges[:6]


def detect_badges(stats, store_details, games):
    """Full badge detection including store-dependent badges."""
    badges = detect_badges_instant(stats, games)

    # Early access addict (needs store data)
    ea = sum(1 for d in store_details.values()
             if "early access" in [g.get("description", "").lower() for g in d.get("genres", [])])
    if ea >= 5:
        badges.append({"name": "Early Access Addict", "emoji": "🚧",
                       "description": f"{ea} Early Access games. You love paying to beta test."})

    return badges[:6]


# ============== Routes ==============
@app.route("/")
def index(): return render_template("index.html")

@app.route("/lookup", methods=["POST"])
def lookup():
    si = request.form.get("steam_input","").strip()
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
            return render_template("error.html", error="This profile is private",
                message="Game details need to be public for Steam Shame to work.")
        gd = get_owned_games(steam_id)
        games = gd.get("response",{}).get("games",[])
        if not games:
            return render_template("error.html", error="No games found",
                message="Either this account has no games, or game details are set to private.")
        stats = analyze_library(games)
        # Detect likely private game details
        if not stats["any_playtime"] and stats["total_games"] > 2:
            return render_template("error.html", error="Game details appear private",
                message="We can see your games but not your playtime. Please set Game Details to Public in your Steam Privacy Settings.")
        # Compute instant badges (no store API needed)
        instant_badges = detect_badges_instant(stats, games)
        return render_template("results.html", player_name=p.get("personaname","Unknown"),
            avatar_url=p.get("avatarfull",""), steam_id=steam_id, stats=stats, instant_badges=instant_badges)
    except Exception as e:
        return render_template("error.html", error="Something went wrong", message=str(e))

# ============== Async API ==============
@app.route("/api/value/<steam_id>")
def api_value(steam_id):
    full = request.args.get("full","0") == "1"
    try:
        games = get_owned_games(steam_id).get("response",{}).get("games",[])
        if not games: return jsonify({"error":"No games"}), 404
        played = [g for g in games if g.get("playtime_forever",0) > 0]
        unplayed = [g for g in games if g.get("playtime_forever",0) == 0]
        sp = played if full else (random.sample(played, min(25, len(played))) if played else [])
        su = unplayed if full else (random.sample(unplayed, min(25, len(unplayed))) if unplayed else [])
        sd = get_app_details_batch([g["appid"] for g in sp+su], max_workers=5, delay=0.35)
        pp, up = [], []
        for g in sp:
            d = sd.get(g["appid"]); pr = extract_usd_price(d)
            if pr: pp.append(pr)
        for g in su:
            d = sd.get(g["appid"]); pr = extract_usd_price(d)
            if pr: up.append(pr)
        if full:
            tpv, tuv, ie = sum(pp), sum(up), False
        else:
            ap = (sum(pp)/len(pp)) if pp else 0
            au = (sum(up)/len(up)) if up else 0
            tpv, tuv, ie = ap*len(played), au*len(unplayed), True
        return jsonify({"library_value":round(tpv+tuv),"unplayed_value":round(tuv),"is_estimate":ie})
    except Exception as e: return jsonify({"error":str(e)}), 500

@app.route("/api/suggest/<steam_id>")
def api_suggest(steam_id):
    """Return a random unplayed game with its store image."""
    try:
        games = get_owned_games(steam_id).get("response",{}).get("games",[])
        if not games: return jsonify({"error":"No games"}), 404
        unplayed = [g for g in games if g.get("playtime_forever",0) == 0]
        if not unplayed: return jsonify({"error":"No unplayed games! Congrats."}), 200
        pick = random.choice(unplayed)
        appid = pick["appid"]
        name = pick.get("name","Unknown")
        # Steam capsule image — better aspect ratio than header.jpg
        img = f"https://cdn.akamai.steamstatic.com/steam/apps/{appid}/capsule_616x353.jpg"
        store_url = f"https://store.steampowered.com/app/{appid}"
        return jsonify({"name":name,"appid":appid,"image":img,"store_url":store_url})
    except Exception as e: return jsonify({"error":str(e)}), 500

@app.route("/api/personality/<steam_id>")
def api_personality(steam_id):
    try:
        games = get_owned_games(steam_id).get("response",{}).get("games",[])
        if not games: return jsonify({"error":"No games"}), 404

        # CORRECT buckets:
        # owned = ALL games (sample)
        # played = any playtime > 0 (sample)
        # unplayed = playtime == 0 (sample)
        all_played = [g for g in games if g.get("playtime_forever",0) > 0]
        all_unplayed = [g for g in games if g.get("playtime_forever",0) == 0]

        # NEW: Seed random with steam_id for consistent sampling across loads
        random.seed(int(steam_id))

        # Slightly increased sample sizes for better accuracy
        owned_sample = random.sample(games, min(120, len(games)))
        played_sample = random.sample(all_played, min(80, len(all_played))) if all_played else []
        unplayed_sample = random.sample(all_unplayed, min(80, len(all_unplayed))) if all_unplayed else []

        all_appids = list(set(g["appid"] for g in owned_sample + played_sample + unplayed_sample))
        sd = get_app_details_batch(all_appids, max_workers=5, delay=0.35)

        def count_genres(game_list):
            counts, names = {}, {}
            for g in game_list:
                d = sd.get(g["appid"])
                if not d: continue
                cats = classify_game_genres(d)
                nm = g.get("name","Unknown")
                for c in cats:
                    counts[c] = counts.get(c,0)+1
                    names.setdefault(c,[]).append(nm)
            return counts, names

        oc, og = count_genres(owned_sample)
        pc, pg = count_genres(played_sample)
        uc, ug = count_genres(unplayed_sample)

        all_genres = sorted(set(list(oc)+list(pc)+list(uc)))

        def norm(counts):
            t = sum(counts.values()) or 1
            return {k: round((counts.get(k,0)/t)*100,1) for k in all_genres}

        labels = [{"key":k,"label":GENRE_CATEGORIES.get(k,{}).get("label",k),
                   "emoji":GENRE_CATEGORIES.get(k,{}).get("emoji","🎮")} for k in all_genres]
        on, pn, un = norm(oc), norm(pc), norm(uc)
        radar = {"labels":labels,
                 "owned":[on.get(k,0) for k in all_genres],
                 "played":[pn.get(k,0) for k in all_genres],
                 "unplayed":[un.get(k,0) for k in all_genres]}
        genre_games = {k:{"owned":og.get(k,[]),"played":pg.get(k,[]),"unplayed":ug.get(k,[])} for k in all_genres}

        def maj(counts):
            if not counts: return None
            top = max(counts.items(), key=lambda x: x[1])
            t = sum(counts.values()) or 1
            i = GENRE_CATEGORIES.get(top[0],{})
            return {"key":top[0],"label":i.get("label",top[0]),"emoji":i.get("emoji","🎮"),"pct":round((top[1]/t)*100,1)}

        om, pm, um = maj(oc), maj(pc), maj(uc)
        mismatch = pm and um and pm["key"] != um["key"]

        stats = analyze_library(games)
        badges = detect_badges(stats, sd, games)

        # Mismatch badge for Gamer DNA
        mismatch_badge = None
        if mismatch and um:
            mismatch_badge = {"emoji": "🤔", "title": f"Thinks They Like {um['label']}",
                              "description": f"Your unplayed library is full of {um['emoji']} {um['label']} games, but that's not what you actually play."}

        return jsonify({"radar":radar,"genre_games":genre_games,"overall_majority":om,
            "played_majority":pm,"unplayed_majority":um,"show_unplayed_mismatch":mismatch,
            "mismatch_badge":mismatch_badge,"badges":badges})
    except Exception as e: return jsonify({"error":str(e)}), 500

@app.route("/api/friends/<steam_id>")
def api_friends(steam_id):
    try:
        pd = get_player_summary(steam_id)
        ps = pd.get("response",{}).get("players",[])
        if not ps or ps[0].get("communityvisibilitystate")!=3: return jsonify({"error":"Profile not accessible"}),403
        friends = get_friends_list(steam_id)
        if not friends: return jsonify({"leaderboard":[],"user_rank":None,"error":"No friends found"})
        aids = [steam_id]+[f["steamid"] for f in friends[:15]]
        aps = []
        for i in range(0,len(aids),100):
            bd = get_player_summary(",".join(aids[i:i+100]))
            aps.extend(bd.get("response",{}).get("players",[]))

        # Parallelize game fetching for each friend
        def fetch_friend(p):
            pid = p.get("steamid")
            if p.get("communityvisibilitystate")!=3: return None
            try:
                gl = get_owned_games(pid).get("response",{}).get("games",[])
                if not gl: return None
                s = analyze_library(gl)
                if not s["any_playtime"]: return None
                if s["shame_score"] >= 99.9: return None
                return {"steam_id":pid,"name":p.get("personaname","Unknown"),"avatar":p.get("avatar",""),
                    "shame_score":s["shame_score"],"total_games":s["total_games"],
                    "played_count":s["played_count"],"never_played":s["never_played_count"],"is_user":pid==steam_id}
            except: return None

        lb = []
        with ThreadPoolExecutor(max_workers=4) as ex:
            for result in ex.map(fetch_friend, aps):
                if result: lb.append(result)

        lb.sort(key=lambda x: x["shame_score"], reverse=True)
        ur = None
        for i,e in enumerate(lb): e["rank"]=i+1; ur = i+1 if e["is_user"] else ur
        return jsonify({"leaderboard":lb[:10],"total_friends":len(lb)-1,"user_rank":ur})
    except Exception as e: return jsonify({"error":str(e)}),500

@app.route("/friends/<steam_id>")
def friends_leaderboard(steam_id):
    try:
        pd = get_player_summary(steam_id); ps = pd.get("response",{}).get("players",[])
        if not ps: return render_template("error.html",error="Not found",message="Profile not found.")
        p = ps[0]
        if p.get("communityvisibilitystate")!=3: return render_template("error.html",error="Private",message="Profile needs to be public.")
        friends = get_friends_list(steam_id)
        if not friends: return render_template("error.html",error="No friends",message="Friends list is private or empty.")
        aids = [steam_id]+[f["steamid"] for f in friends]
        aps = []
        for i in range(0,len(aids),100):
            bd = get_player_summary(",".join(aids[i:i+100]))
            aps.extend(bd.get("response",{}).get("players",[]))
        lb = []
        for pl in aps:
            pid = pl.get("steamid")
            if pl.get("communityvisibilitystate")!=3: continue
            try:
                gl = get_owned_games(pid).get("response",{}).get("games",[])
                if not gl: continue
                s = analyze_library(gl)
                if not s["any_playtime"]: continue
                lb.append({"steam_id":pid,"name":pl.get("personaname","Unknown"),"avatar":pl.get("avatar",""),
                    "shame_score":s["shame_score"],"total_games":s["total_games"],
                    "played_count":s["played_count"],"never_played":s["never_played_count"],"is_user":pid==steam_id})
            except: continue
        lb.sort(key=lambda x: x["shame_score"], reverse=True)
        for i,e in enumerate(lb): e["rank"]=i+1
        ur = next((e["rank"] for e in lb if e["is_user"]),None)
        return render_template("friends.html",player_name=p.get("personaname","Unknown"),
            avatar_url=p.get("avatarfull",""),steam_id=steam_id,leaderboard=lb,user_rank=ur,total_friends=len(lb)-1)
    except Exception as e: return render_template("error.html",error="Error",message=str(e))


@app.route("/share/<steam_id>.png")
def share_image(steam_id):
    try:
        pd = get_player_summary(steam_id)
        players = pd.get("response", {}).get("players", [])
        if not players:
            return "Not found", 404
        p = players[0]
        games = get_owned_games(steam_id).get("response", {}).get("games", [])
        if not games:
            return "No games", 404
        stats = analyze_library(games)

        W, H = 1200, 630
        img = Image.new('RGB', (W, H), (10, 10, 18))  # dark base
        draw = ImageDraw.Draw(img)

        # Subtle radial gradient background
        center_x, center_y = W//2, H//3
        for r in range(400, 0, -2):
            alpha = int(40 * (1 - r/400))
            draw.ellipse(
                (center_x - r, center_y - r*0.7, center_x + r, center_y + r*0.7),
                fill=(30 + alpha//3, 20 + alpha//4, 80 + alpha//2)
            )

        # Load fonts (fallback to default if missing)
        font_path_inter = "static/fonts/Inter-Bold.ttf"
        font_path_orbitron = "static/fonts/Orbitron-Bold.ttf"

        try:
            font_huge   = ImageFont.truetype(font_path_orbitron, 220)
            font_large  = ImageFont.truetype(font_path_inter, 80)
            font_med    = ImageFont.truetype(font_path_inter, 48)
            font_sm     = ImageFont.truetype(font_path_inter, 36)
        except Exception:
            font_huge = font_large = font_med = font_sm = ImageFont.load_default()

        name = p.get("personaname", "Player")
        score_str = f"{stats['shame_score']:.1f}"

        # Big score with glow + gradient
        # First draw glow (blur)
        glow = Image.new('RGBA', (W, H), (0,0,0,0))
        glow_draw = ImageDraw.Draw(glow)
        for offset, color, size in [
            (18, (255, 80, 180, 60), 240),
            (12, (255, 120, 100, 100), 230),
            (6,  (255, 160, 80,  140), 225)
        ]:
            glow_draw.text((W//2 + offset, 100 + offset), score_str, fill=color, font=font_huge, anchor="mm")
        glow = glow.filter(ImageFilter.GaussianBlur(12))
        img.paste(glow, (0,0), glow)

        # Main score text with simple gradient
        for dx, dy, color in [(-3,-3,(255,140,60)), (3,3,(255,60,140)), (0,0,(255,100,100))]:
            draw.text((W//2 + dx, 100 + dy), score_str, fill=color, font=font_huge, anchor="mm")

        # % symbol
        pct_x = W//2 + font_huge.getlength(score_str) // 2 + 20
        draw.text((pct_x, 100 + 60), "%", fill=(255, 180, 120), font=font_med, anchor="lm")

        # Labels
        draw.text((W//2, 260), "SHAME SCORE", fill=(160, 160, 200), font=font_med, anchor="mm")
        
        # Stats
        stats_line = f"{stats['total_games']} GAMES OWNED • {stats['never_played_count']} NEVER PLAYED"
        draw.text((W//2, 340), stats_line, fill=(200, 200, 220), font=font_sm, anchor="mm")

        # Player name
        draw.text((W//2, 420), name.upper(), fill=(220, 220, 255), font=font_large, anchor="mm")

        # Small branding
        draw.text((W//2, H - 40), "SteamShame • steam-shame.up.railway.app", fill=(100, 100, 140), font=font_sm, anchor="mm")

        # Optional: add avatar in corner (small circle)
        try:
            av_url = p.get("avatarfull", "")
            if av_url:
                av_resp = requests.get(av_url, timeout=5)
                av_img = Image.open(io.BytesIO(av_resp.content)).convert("RGBA")
                av_img = av_img.resize((120, 120), Image.LANCZOS)
                mask = Image.new("L", (120, 120), 0)
                ImageDraw.Draw(mask).ellipse((0,0,120,120), fill=255)
                img.paste(av_img, (W - 160, 40), mask)
        except:
            pass

        buf = io.BytesIO()
        img.save(buf, format='PNG', optimize=True, quality=95)
        buf.seek(0)
        return send_file(buf, mimetype='image/png', download_name=f"steam-shame-{steam_id}.png")

    except Exception as e:
        log.error(f"Share image error: {e}")
        return "Error generating image", 500


if __name__ == "__main__":
    if not STEAM_API_KEY: print("Warning: STEAM_API_KEY not set!")
    app.run(debug=os.environ.get("FLASK_ENV")=="development",host="0.0.0.0",port=int(os.environ.get("PORT",5000)))
