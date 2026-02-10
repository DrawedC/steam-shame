"""
Steam Shame - A web app that calculates your Steam library shame score.
"""
from flask import Flask, redirect, request, url_for, render_template, jsonify, send_file
import requests, os, re, random, time, math, threading, io
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import logging

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-this")
STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "")

# Caches
_store_cache = {}
_store_cache_lock = threading.Lock()
STORE_CACHE_TTL = 86400  # 24 hours

_games_cache = {}
_games_cache_lock = threading.Lock()
GAMES_CACHE_TTL = 300  # 5 min

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("steam-shame")

# Genre grouping
GENRE_GROUPS = {
    'action': 'action',
    'shooter': 'action',
    'fps': 'action',
    'third person shooter': 'action',
    'hack and slash': 'action',
    'beat \'em up': 'action',
    'fighting': 'action',
    'platformer': 'action',
    'metroidvania': 'action',
    'roguelike': 'action',
    'adventure': 'adventure',
    'visual novel': 'adventure',
    'point & click': 'adventure',
    'walking simulator': 'adventure',
    'rpg': 'rpg',
    'jrpg': 'rpg',
    'role-playing': 'rpg',
    'strategy': 'strategy',
    'turn-based strategy': 'strategy',
    '4x': 'strategy',
    'tower defense': 'strategy',
    'real time strategy': 'strategy',
    'simulation': 'simulation',
    'management': 'simulation',
    'building': 'simulation',
    'farming sim': 'simulation',
    'indie': 'indie',
    'casual': 'casual',
    'racing': 'racing',
    'sports': 'sports',
    'puzzle': 'puzzle',
    'horror': 'horror',
    'survival': 'survival',
    'open world': 'open world',
}

# ============== Steam API ==============
def get_owned_games(steam_id):
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
    except Exception as e:
        log.warning(f"Owned games fetch failed: {e}")
        raise

def get_player_summary(steam_id):
    r = requests.get("http://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/",
        params={"key":STEAM_API_KEY,"steamids":steam_id,"format":"json"}, timeout=15)
    r.raise_for_status()
    return r.json()

def resolve_vanity_url(vanity_name):
    r = requests.get("http://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/",
        params={"key":STEAM_API_KEY,"vanityurl":vanity_name,"format":"json"}, timeout=15)
    r.raise_for_status()
    d = r.json()
    return d["response"]["steamid"] if d.get("response",{}).get("success")==1 else None

def get_friends_list(steam_id):
    try:
        r = requests.get("http://api.steampowered.com/ISteamUser/GetFriendList/v1/",
            params={"key":STEAM_API_KEY,"steamid":steam_id,"relationship":"friend","format":"json"}, timeout=15)
        if r.status_code == 401: return []
        r.raise_for_status()
        return r.json().get("friendslist",{}).get("friends",[])
    except:
        return []

def get_app_details(appid):
    now = time.time()
    with _store_cache_lock:
        c = _store_cache.get(appid)
        if c and (now - c["ts"]) < STORE_CACHE_TTL:
            return c["data"]
    try:
        url = f"https://store.steampowered.com/api/appdetails?appids={appid}&l=english"
        r = requests.get(url, timeout=10)
        if r.status_code == 429:
            log.warning(f"Rate limited on appid {appid}, waiting 10s")
            time.sleep(10)
            r = requests.get(url, timeout=10)  # retry once
        if r.status_code == 200:
            ad = r.json().get(str(appid), {})
            if ad.get("success"):
                result = ad.get("data", {})
                with _store_cache_lock:
                    _store_cache[appid] = {"data": result, "ts": now}
                return result
        return None
    except Exception as e:
        log.debug(f"Store API error for {appid}: {e}")
        return None

def get_app_details_batch(appids, max_workers=4, delay=0.8):
    results = {}
    def fetch(aid):
        time.sleep(random.uniform(0.3, delay))
        return aid, get_app_details(aid)
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

    any_playtime = any(g.get("playtime_forever",0) > 0 for g in games)
    played = sorted(raw_played, key=lambda x: x["playtime_forever"], reverse=True)
    abandoned = sorted([g for g in raw_abandoned if not is_recent(g)], key=lambda x: x["playtime_forever"])
    unplayed = [g for g in raw_unplayed if not is_recent(g)]

    shame = calculate_shame_score(len(raw_unplayed), len(raw_abandoned), total)
    verdict = "You have a problem. Stop buying games." if shame > 55 else \
              "Steam sales have claimed another victim." if shame > 40 else \
              "Not bad, but that backlog isn't clearing itself." if shame > 25 else \
              "Impressive restraint. Or new account."

    def gl(lst, limit=30):
        return [{"name":g.get("name","Unknown"),"appid":g.get("appid"),
                 "playtime":g.get("playtime_forever",0),
                 "playtime_fmt":format_playtime(g.get("playtime_forever",0))} for g in lst[:limit]]

    backlog_days = round(len(raw_unplayed) * 10)

    suggest = None
    if unplayed:
        pick = random.choice(unplayed)
        suggest = {"name": pick.get("name","Unknown"), "appid": pick.get("appid")}

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
    "rpg":{"names":["RPG","JRPG","Action RPG","Turn-Based RPG","CRPG","Role-Playing"],"label":"RPG","emoji":"⚔️"},
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

def classify_game_genres(details):
    if not details or 'genres' not in details:
        return []
    raw_genres = [g['description'].lower() for g in details.get('genres', [])]
    grouped = set()
    for genre in raw_genres:
        grouped_name = GENRE_GROUPS.get(genre, genre)
        grouped.add(grouped_name)
    if not grouped and 'action' in raw_genres:
        grouped.add('action')
    return sorted(list(grouped))

def detect_descriptor(stats):
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
    badges = detect_badges_instant(stats, games)
    ea = sum(1 for d in store_details.values()
             if "early access" in [g.get("description", "").lower() for g in d.get("genres", [])])
    if ea >= 5:
        badges.append({"name": "Early Access Addict", "emoji": "🚧",
                       "description": f"{ea} Early Access games. You love paying to beta test."})
    return badges[:6]

# ============== Routes ==============
@app.route("/")
def index():
    return render_template("index.html")

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
    else:
        steam_id = resolve_vanity_url(si)
    if not steam_id:
        return render_template("index.html", error="Could not find that Steam profile.")
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
        if not stats["any_playtime"] and stats["total_games"] > 2:
            return render_template("error.html", error="Game details appear private",
                message="We can see your games but not your playtime. Please set Game Details to Public in your Steam Privacy Settings.")
        instant_badges = detect_badges_instant(stats, games)
        return render_template("results.html", player_name=p.get("personaname","Unknown"),
            avatar_url=p.get("avatarfull",""), steam_id=steam_id, stats=stats, instant_badges=instant_badges)
    except Exception as e:
        return render_template("error.html", error="Something went wrong", message=str(e))

# ============== Async API ==============
@app.route("/api/value/<steam_id>")
def api_value(steam_id):
    try:
        games = get_owned_games(steam_id).get("response",{}).get("games",[])
        if not games: return jsonify({"error":"No games"}), 404

        played = [g for g in games if g.get("playtime_forever",0) > 0]
        unplayed = [g for g in games if g.get("playtime_forever",0) == 0]

        # Always use sampled estimate (no full scan)
        sp = random.sample(played, min(40, len(played))) if played else []
        su = random.sample(unplayed, min(40, len(unplayed))) if unplayed else []

        sd = get_app_details_batch([g["appid"] for g in sp+su], max_workers=5, delay=0.8)

        pp, up = [], []
        for g in sp:
            d = sd.get(g["appid"])
            pr = extract_usd_price(d)
            if pr: pp.append(pr)
        for g in su:
            d = sd.get(g["appid"])
            pr = extract_usd_price(d)
            if pr: up.append(pr)

        ap = (sum(pp)/len(pp)) if pp else 0
        au = (sum(up)/len(up)) if up else 0

        total_value = round(ap * len(played))
        unplayed_value = round(au * len(unplayed))

        # Rate limit / incomplete data check
        fetched_ratio = len(sd) / max(1, len(sp) + len(su))
        if fetched_ratio < 0.6:
            return jsonify({
                "library_value": total_value,
                "unplayed_value": unplayed_value,
                "is_estimate": True,
                "warning": "Experiencing heavy traffic, please try again later for more accurate values."
            })

        return jsonify({
            "library_value": total_value,
            "unplayed_value": unplayed_value,
            "is_estimate": True
        })

    except Exception as e:
        log.error(f"Value error: {e}")
        return jsonify({"error": "Experiencing heavy traffic, please try again later"}), 503

@app.route("/api/suggest/<steam_id>")
def api_suggest(steam_id):
    try:
        games = get_owned_games(steam_id).get("response",{}).get("games",[])
        if not games: return jsonify({"error":"No games"}), 404
        unplayed = [g for g in games if g.get("playtime_forever",0) == 0]
        if not unplayed: return jsonify({"error":"No unplayed games! Congrats."}), 200
        pick = random.choice(unplayed)
        appid = pick["appid"]
        name = pick.get("name","Unknown")
        img = f"https://cdn.akamai.steamstatic.com/steam/apps/{appid}/capsule_616x353.jpg"
        store_url = f"https://store.steampowered.com/app/{appid}"
        return jsonify({"name":name,"appid":appid,"image":img,"store_url":store_url})
    except Exception as e: return jsonify({"error":str(e)}), 500

@app.route("/api/personality/<steam_id>")
def api_personality(steam_id):
    try:
        games = get_owned_games(steam_id).get("response",{}).get("games",[])
        if not games: return jsonify({"error":"No games"}), 404

        all_played = [g for g in games if g.get("playtime_forever",0) > 0]
        all_unplayed = [g for g in games if g.get("playtime_forever",0) == 0]

        random.seed(int(steam_id))

        # Lower sample sizes to reduce API load
        owned_sample = random.sample(games, min(60, len(games)))
        played_sample = random.sample(all_played, min(40, len(all_played))) if all_played else []
        unplayed_sample = random.sample(all_unplayed, min(40, len(all_unplayed))) if all_unplayed else []

        all_appids = list(set(g["appid"] for g in owned_sample + played_sample + unplayed_sample))
        sd = get_app_details_batch(all_appids, max_workers=5, delay=0.8)

        def count_genres(game_list, weight_by_playtime=False):
            counts = {}
            names = {}
            for g in game_list:
                d = sd.get(g["appid"])
                if not d:
                    continue
                genres = classify_game_genres(d)
                playtime = g.get("playtime_forever", 0) if weight_by_playtime else 1
                weight = max(1, playtime / 60)
                nm = g.get("name", "Unknown")
                for gen in genres:
                    counts[gen] = counts.get(gen, 0) + weight
                    names.setdefault(gen, []).append(nm)
            return counts, names

        oc, og = count_genres(owned_sample, weight_by_playtime=False)
        pc, pg = count_genres(played_sample, weight_by_playtime=True)
        uc, ug = count_genres(unplayed_sample, weight_by_playtime=False)

        def norm(counts):
            total = sum(counts.values()) or 1
            return {k: round((v / total) * 100, 1) for k, v in counts.items()}

        on = norm(oc)
        pn = norm(pc)
        un = norm(uc)

        # 5% threshold + Misc grouping
        MIN_THRESHOLD = 5.0

        genre_max_pct = {}
        all_possible = set(on) | set(pn) | set(un)
        for k in all_possible:
            max_pct = max(on.get(k, 0), pn.get(k, 0), un.get(k, 0))
            genre_max_pct[k] = max_pct

        major_genres = [k for k, pct in genre_max_pct.items() if pct >= MIN_THRESHOLD]
        minor_genres = [k for k, pct in genre_max_pct.items() if pct < MIN_THRESHOLD]

        misc_owned    = sum(on.get(k, 0) for k in minor_genres)
        misc_played   = sum(pn.get(k, 0) for k in minor_genres)
        misc_unplayed = sum(un.get(k, 0) for k in minor_genres)

        display_genres = sorted(major_genres)
        has_misc = bool(minor_genres)
        if has_misc:
            display_genres.append("misc")

        labels = []
        for k in display_genres:
            if k == "misc":
                labels.append({"key": "misc", "label": "Misc", "emoji": "⋯"})
            else:
                info = GENRE_CATEGORIES.get(k, {})
                labels.append({
                    "key": k,
                    "label": info.get("label", k.capitalize()),
                    "emoji": info.get("emoji", "🎮")
                })

        radar = {
            "labels": labels,
            "owned":   [on.get(k, 0) if k != "misc" else misc_owned    for k in display_genres],
            "played":  [pn.get(k, 0) if k != "misc" else misc_played   for k in display_genres],
            "unplayed": [un.get(k, 0) if k != "misc" else misc_unplayed for k in display_genres]
        }

        genre_games = {}
        for k in major_genres:
            genre_games[k] = {
                "owned": og.get(k, []),
                "played": pg.get(k, []),
                "unplayed": ug.get(k, [])
            }
        if has_misc:
            misc_games = {"owned": [], "played": [], "unplayed": []}
            for k in minor_genres:
                misc_games["owned"].extend(og.get(k, []))
                misc_games["played"].extend(pg.get(k, []))
                misc_games["unplayed"].extend(ug.get(k, []))
            genre_games["misc"] = misc_games

        def maj(counts, misc_sum=0):
            if not counts: return None
            effective = counts.copy()
            if has_misc:
                effective["misc"] = misc_sum
            if not effective: return None
            top_key = max(effective, key=effective.get)
            total = sum(effective.values()) or 1
            i = GENRE_CATEGORIES.get(top_key, {}) if top_key != "misc" else {"label": "Misc", "emoji": "⋯"}
            return {
                "key": top_key,
                "label": i.get("label", top_key.capitalize()),
                "emoji": i.get("emoji", "🎮"),
                "pct": round((effective[top_key] / total) * 100, 1)
            }

        om = maj(oc, misc_owned)
        pm = maj(pc, misc_played)
        um = maj(uc, misc_unplayed)

        mismatch = pm and um and pm["key"] != um["key"]

        stats = analyze_library(games)
        badges = detect_badges(stats, sd, games)

        mismatch_badge = None
        if mismatch and um:
            mismatch_badge = {"emoji": "🤔", "title": f"Thinks They Like {um['label']}",
                              "description": f"Your unplayed library is full of {um['emoji']} {um['label']} games, but that's not what you actually play."}

        # Rate limit warning if store data is incomplete
        fetched_ratio = len(sd) / max(1, len(all_appids))
        warning = None
        if fetched_ratio < 0.6:
            warning = "Experiencing heavy traffic, please try again later for more accurate DNA."

        response = {
            "radar": radar,
            "genre_games": genre_games,
            "overall_majority": om,
            "played_majority": pm,
            "unplayed_majority": um,
            "show_unplayed_mismatch": mismatch,
            "mismatch_badge": mismatch_badge,
            "badges": badges
        }
        if warning:
            response["warning"] = warning

        return jsonify(response)

    except Exception as e:
        log.error(f"Personality error: {e}", exc_info=True)
        return jsonify({"error": "Experiencing heavy traffic, please try again later"}), 503

# ... (the rest of your file remains unchanged: api_friends, friends_leaderboard, share_image, if __name__ == "__main__")
