"""
Steam Shame - A web app that calculates your Steam library shame score.
"""

from flask import Flask, redirect, request, session, url_for, render_template, jsonify
import requests
import os
import re
import random
import time
import threading
from urllib.parse import urlencode
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-this")

# Get these from environment variables
STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "")

# Simple in-memory cache for store API data (appid -> data, with TTL)
_store_cache = {}
_store_cache_lock = threading.Lock()
STORE_CACHE_TTL = 3600  # 1 hour


# ============== Steam API Functions ==============

def get_owned_games(steam_id: str) -> dict:
    """Fetch all owned games with playtime info."""
    url = "http://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
    params = {
        "key": STEAM_API_KEY,
        "steamid": steam_id,
        "include_appinfo": True,
        "include_played_free_games": True,
        "format": "json"
    }
    response = requests.get(url, params=params)
    response.raise_for_status()
    return response.json()


def get_player_summary(steam_id: str) -> dict:
    """Get player profile info."""
    url = "http://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
    params = {
        "key": STEAM_API_KEY,
        "steamids": steam_id,
        "format": "json"
    }
    response = requests.get(url, params=params)
    response.raise_for_status()
    return response.json()


def resolve_vanity_url(vanity_name: str) -> str:
    """Convert a vanity URL name to Steam ID."""
    url = "http://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/"
    params = {
        "key": STEAM_API_KEY,
        "vanityurl": vanity_name,
        "format": "json"
    }
    response = requests.get(url, params=params)
    response.raise_for_status()
    data = response.json()
    if data.get("response", {}).get("success") == 1:
        return data["response"]["steamid"]
    return None


def get_friends_list(steam_id: str) -> list:
    """Get a user's friends list."""
    url = "http://api.steampowered.com/ISteamUser/GetFriendList/v1/"
    params = {
        "key": STEAM_API_KEY,
        "steamid": steam_id,
        "relationship": "friend",
        "format": "json"
    }
    response = requests.get(url, params=params)
    if response.status_code == 401:
        return []  # Friends list is private
    response.raise_for_status()
    data = response.json()
    return data.get("friendslist", {}).get("friends", [])


def get_app_details(appid: int) -> dict:
    """Fetch store details for a single app. Uses cache."""
    now = time.time()

    with _store_cache_lock:
        cached = _store_cache.get(appid)
        if cached and (now - cached["ts"]) < STORE_CACHE_TTL:
            return cached["data"]

    try:
        url = f"https://store.steampowered.com/api/appdetails?appids={appid}"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            app_data = data.get(str(appid), {})
            if app_data.get("success"):
                result = app_data.get("data", {})
                with _store_cache_lock:
                    _store_cache[appid] = {"data": result, "ts": now}
                return result
    except Exception:
        pass

    return None


def get_app_details_batch(appids: list, max_workers: int = 5, delay: float = 0.3) -> dict:
    """Fetch store details for multiple apps with rate limiting."""
    results = {}

    def fetch_one(appid):
        time.sleep(random.uniform(0.1, delay))
        return appid, get_app_details(appid)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_one, aid): aid for aid in appids}
        for future in as_completed(futures):
            try:
                appid, data = future.result()
                if data:
                    results[appid] = data
            except Exception:
                continue

    return results


# ============== Analysis Functions ==============

def format_playtime(minutes: int) -> str:
    """Convert minutes to readable format."""
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes / 60
    if hours < 24:
        return f"{hours:.1f}h"
    days = hours / 24
    return f"{days:.1f} days"


def analyze_library(games: list) -> dict:
    """Crunch the numbers on your shame."""

    if not games:
        return None

    total_games = len(games)

    # Categorize games by playtime
    never_played = [g for g in games if g.get("playtime_forever", 0) == 0]
    under_hour = [g for g in games if 0 < g.get("playtime_forever", 0) < 60]
    under_two_hours = [g for g in games if 60 <= g.get("playtime_forever", 0) < 120]
    actually_played = [g for g in games if g.get("playtime_forever", 0) >= 120]

    # Total playtime
    total_minutes = sum(g.get("playtime_forever", 0) for g in games)
    total_hours = total_minutes / 60

    # Top played games
    sorted_by_playtime = sorted(games, key=lambda x: x.get("playtime_forever", 0), reverse=True)
    top_10 = sorted_by_playtime[:10]

    # Games you "tried" but gave up on (10-60 min) — get ALL, show sample
    all_gave_up = sorted(
        [g for g in games if 10 <= g.get("playtime_forever", 0) < 60],
        key=lambda x: x.get("playtime_forever", 0),
        reverse=True
    )
    gave_up_sample = all_gave_up[:10]
    gave_up_total = len(all_gave_up)

    # Random sample of never played
    never_played_sample = random.sample(never_played, min(10, len(never_played))) if never_played else []

    # Calculate concentration
    top_5_minutes = sum(g.get("playtime_forever", 0) for g in sorted_by_playtime[:5])
    concentration = (top_5_minutes / total_minutes * 100) if total_minutes > 0 else 0

    # Shame score
    shame_score = (len(never_played) / total_games * 100) if total_games > 0 else 0

    # Verdict
    if shame_score > 70:
        verdict = "You have a problem. Stop buying games."
    elif shame_score > 50:
        verdict = "Steam sales have claimed another victim."
    elif shame_score > 30:
        verdict = "Not bad, but you know you'll never play those."
    else:
        verdict = "Impressive restraint. Or new account."

    return {
        "total_games": total_games,
        "never_played_count": len(never_played),
        "never_played_sample": [{"name": g.get("name", "Unknown"), "appid": g.get("appid")} for g in never_played_sample],
        "under_hour_count": len(under_hour),
        "under_two_hours_count": len(under_two_hours),
        "actually_played_count": len(actually_played),
        "gave_up": [{"name": g.get("name", "Unknown"), "playtime": g.get("playtime_forever", 0), "appid": g.get("appid")} for g in gave_up_sample],
        "gave_up_total": gave_up_total,
        "total_minutes": total_minutes,
        "total_hours": round(total_hours, 1),
        "top_10": [{"name": g.get("name", "Unknown"), "playtime": g.get("playtime_forever", 0), "playtime_formatted": format_playtime(g.get("playtime_forever", 0)), "appid": g.get("appid")} for g in top_10],
        "concentration": round(concentration, 1),
        "shame_score": round(shame_score, 1),
        "verdict": verdict,
    }


# ============== Indie & Genre Analysis ==============

GENRE_CATEGORIES = {
    "fps_shooter": {"names": ["FPS", "Shooter", "First-Person Shooter", "Third-Person Shooter"], "label": "Shooter Fanatic", "emoji": "🔫"},
    "rpg": {"names": ["RPG", "JRPG", "Action RPG", "Turn-Based RPG", "CRPG", "Role-Playing"], "label": "RPG Adventurer", "emoji": "⚔️"},
    "strategy": {"names": ["Strategy", "Real-Time Strategy", "Turn-Based Strategy", "Tower Defense", "RTS", "4X", "Grand Strategy"], "label": "Armchair General", "emoji": "🧠"},
    "survival": {"names": ["Survival", "Survival Horror", "Crafting", "Base Building", "Open World Survival Craft"], "label": "Survival Nut", "emoji": "🏕️"},
    "simulation": {"names": ["Simulation", "Life Sim", "Farming Sim", "Management", "City Builder", "Building"], "label": "Sim Enthusiast", "emoji": "🏗️"},
    "indie": {"names": ["Indie"], "label": "Indie Connoisseur", "emoji": "🎨"},
    "action": {"names": ["Action", "Hack and Slash", "Beat 'em up", "Action-Adventure"], "label": "Action Junkie", "emoji": "💥"},
    "puzzle": {"names": ["Puzzle", "Logic", "Hidden Object"], "label": "Puzzle Brain", "emoji": "🧩"},
    "platformer": {"names": ["Platformer", "2D Platformer", "3D Platformer", "Precision Platformer"], "label": "Platformer Pro", "emoji": "🍄"},
    "horror": {"names": ["Horror", "Psychological Horror", "Survival Horror"], "label": "Horror Addict", "emoji": "👻"},
    "racing": {"names": ["Racing", "Driving", "Automobile Sim"], "label": "Speed Demon", "emoji": "🏎️"},
    "sports": {"names": ["Sports", "Football", "Basketball", "Baseball", "Soccer", "Golf"], "label": "Sports Gamer", "emoji": "⚽"},
    "sandbox": {"names": ["Sandbox", "Open World", "Exploration"], "label": "Sandbox Explorer", "emoji": "🌍"},
    "roguelike": {"names": ["Roguelike", "Roguelite", "Roguevania", "Procedural Generation"], "label": "Roguelike Masochist", "emoji": "💀"},
    "multiplayer": {"names": ["Massively Multiplayer", "MMO", "MMORPG", "Co-op", "Multiplayer"], "label": "Social Gamer", "emoji": "👥"},
    "vr": {"names": ["VR", "Virtual Reality", "VR Only", "VR Supported"], "label": "VR Enthusiast", "emoji": "🥽"},
    "casual": {"names": ["Casual", "Clicker", "Idle", "Card Game", "Board Game"], "label": "Casual Gamer", "emoji": "🎲"},
    "visual_novel": {"names": ["Visual Novel", "Dating Sim", "Choose Your Own Adventure", "Interactive Fiction"], "label": "Story Lover", "emoji": "📖"},
    "fighting": {"names": ["Fighting", "Martial Arts"], "label": "Fighting Game Fan", "emoji": "🥊"},
}


def classify_game_genres(store_data: dict) -> list:
    """Extract genre category keys from store data."""
    categories = set()
    genres = [g.get("description", "") for g in store_data.get("genres", [])]
    tags = [c.get("description", "") for c in store_data.get("categories", [])]
    all_labels = genres + tags

    for cat_key, cat_info in GENRE_CATEGORIES.items():
        for name in cat_info["names"]:
            if name.lower() in [l.lower() for l in all_labels]:
                categories.add(cat_key)
                break

    return list(categories)


def classify_indie_tier(review_count: int) -> dict:
    """Classify a game's indie tier based on review count."""
    if review_count < 500:
        return {"tier": "hidden_gem", "label": "Hidden Gem", "emoji": "💎", "color": "#a855f7"}
    elif review_count < 1000:
        return {"tier": "indie", "label": "Indie", "emoji": "🎨", "color": "#22c55e"}
    elif review_count < 10000:
        return {"tier": "mid_tier", "label": "Mid-Tier", "emoji": "📊", "color": "#3b82f6"}
    else:
        return {"tier": "mainstream", "label": "Mainstream", "emoji": "🏢", "color": "#f59e0b"}


def detect_badges(stats: dict, store_details: dict, games: list) -> list:
    """Detect funny badges based on library patterns."""
    badges = []

    # Humble Bundle Victim
    if stats["total_games"] > 200 and stats["shame_score"] > 50:
        badges.append({
            "name": "Humble Bundle Victim",
            "emoji": "📦",
            "description": "200+ games, most untouched. Those bundles got you good."
        })

    # Early Access Addict
    early_access_count = 0
    for appid, data in store_details.items():
        genres = [g.get("description", "").lower() for g in data.get("genres", [])]
        if "early access" in genres:
            early_access_count += 1
    if early_access_count >= 5:
        badges.append({
            "name": "Early Access Addict",
            "emoji": "🚧",
            "description": f"{early_access_count} Early Access games. You love paying to beta test."
        })

    # One-Trick Pony
    if stats["top_10"] and stats["total_minutes"] > 0:
        top_pct = (stats["top_10"][0]["playtime"] / stats["total_minutes"]) * 100
        if top_pct > 50:
            badges.append({
                "name": "One-Trick Pony",
                "emoji": "🐴",
                "description": f"{top_pct:.0f}% of your time in {stats['top_10'][0]['name']}."
            })

    # Collector
    if stats["total_games"] >= 500:
        badges.append({
            "name": "Game Collector",
            "emoji": "🏛️",
            "description": f"{stats['total_games']} games. You don't play games, you collect them."
        })

    # Speedrunner (Abandon Edition)
    quick_abandon = len([g for g in games if 0 < g.get("playtime_forever", 0) < 30])
    if quick_abandon >= 20:
        badges.append({
            "name": "Speedrun Abandoner",
            "emoji": "⏱️",
            "description": f"Opened {quick_abandon} games for under 30 minutes."
        })

    # No-Lifer
    if stats["total_hours"] > 5000:
        badges.append({
            "name": "No-Lifer",
            "emoji": "🦉",
            "description": f"{stats['total_hours']:.0f} hours total. That's {stats['total_hours']/24:.0f} full days."
        })

    # Disciplined Buyer
    if stats["total_games"] < 50 and stats["shame_score"] < 30:
        badges.append({
            "name": "Disciplined Buyer",
            "emoji": "🎯",
            "description": "Small library, actually played. Impressive self-control."
        })

    # F2P Warrior
    f2p_count = 0
    for appid, data in store_details.items():
        if data.get("is_free", False):
            f2p_count += 1
    if f2p_count >= 10:
        badges.append({
            "name": "F2P Warrior",
            "emoji": "🆓",
            "description": f"{f2p_count} free-to-play games. At least those didn't cost anything."
        })

    # Refund Zone Lurker
    refund_zone = len([g for g in games if 100 <= g.get("playtime_forever", 0) <= 130])
    if refund_zone >= 5:
        badges.append({
            "name": "Refund Zone Lurker",
            "emoji": "🔄",
            "description": f"{refund_zone} games at exactly ~2 hours. Totally not testing refund limits."
        })

    return badges[:6]


# ============== Routes ==============

@app.route("/")
def index():
    """Home page with lookup form."""
    return render_template("index.html")


@app.route("/lookup", methods=["POST"])
def lookup():
    """Look up a Steam profile by URL or ID."""
    steam_input = request.form.get("steam_input", "").strip()

    if not steam_input:
        return redirect(url_for("index"))

    steam_id = None

    if re.match(r"^\d{17}$", steam_input):
        steam_id = steam_input
    elif "steamcommunity.com" in steam_input:
        match = re.search(r"steamcommunity\.com/(?:profiles|id)/([^/\?]+)", steam_input)
        if match:
            id_or_vanity = match.group(1)
            if re.match(r"^\d{17}$", id_or_vanity):
                steam_id = id_or_vanity
            else:
                steam_id = resolve_vanity_url(id_or_vanity)
    else:
        steam_id = resolve_vanity_url(steam_input)

    if not steam_id:
        return render_template("index.html", error="Could not find that Steam profile. Try pasting your full Steam profile URL.")

    return redirect(url_for("results", steam_id=steam_id))


@app.route("/results/<steam_id>")
def results(steam_id: str):
    """Show shame results for a Steam ID."""

    try:
        player_data = get_player_summary(steam_id)
        players = player_data.get("response", {}).get("players", [])

        if not players:
            return render_template("index.html", error="Could not find that Steam profile.")

        player = players[0]
        player_name = player.get("personaname", "Unknown")
        avatar_url = player.get("avatarfull", "")
        profile_url = player.get("profileurl", "")

        if player.get("communityvisibilitystate") != 3:
            return render_template("error.html",
                error="This profile is private",
                message="Game details need to be public for Steam Shame to work. Update your privacy settings in Steam and try again."
            )

        games_data = get_owned_games(steam_id)
        games = games_data.get("response", {}).get("games", [])

        if not games:
            return render_template("error.html",
                error="No games found",
                message="Either this account has no games, or game details are set to private. Check your Steam privacy settings."
            )

        stats = analyze_library(games)

        return render_template("results.html",
            player_name=player_name,
            avatar_url=avatar_url,
            profile_url=profile_url,
            steam_id=steam_id,
            stats=stats
        )

    except requests.exceptions.HTTPError as e:
        return render_template("error.html", error="Steam API Error", message=str(e))
    except Exception as e:
        return render_template("error.html", error="Something went wrong", message=str(e))


# ============== Async API Endpoints ==============

@app.route("/api/stats/<steam_id>")
def api_stats(steam_id: str):
    """JSON API for stats."""
    try:
        player_data = get_player_summary(steam_id)
        players = player_data.get("response", {}).get("players", [])

        if not players:
            return jsonify({"error": "Profile not found"}), 404

        player = players[0]

        if player.get("communityvisibilitystate") != 3:
            return jsonify({"error": "Profile is private"}), 403

        games_data = get_owned_games(steam_id)
        games = games_data.get("response", {}).get("games", [])

        if not games:
            return jsonify({"error": "No games found"}), 404

        stats = analyze_library(games)
        stats["player_name"] = player.get("personaname")
        stats["avatar_url"] = player.get("avatarfull")

        return jsonify(stats)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/value/<steam_id>")
def api_value(steam_id: str):
    """Async endpoint: fetch price/value data for a user's library."""
    try:
        games_data = get_owned_games(steam_id)
        games = games_data.get("response", {}).get("games", [])

        if not games:
            return jsonify({"error": "No games found"}), 404

        sorted_games = sorted(games, key=lambda x: x.get("playtime_forever", 0), reverse=True)

        # Sample: top 10 played + up to 40 unplayed
        played_games = [g for g in sorted_games if g.get("playtime_forever", 0) > 0][:10]
        unplayed_games = [g for g in sorted_games if g.get("playtime_forever", 0) == 0]
        unplayed_sample = random.sample(unplayed_games, min(40, len(unplayed_games))) if unplayed_games else []

        all_sample = played_games + unplayed_sample
        appids = [g["appid"] for g in all_sample]

        store_data = get_app_details_batch(appids, max_workers=4, delay=0.4)

        # $/hour for top played
        value_list = []
        for g in played_games:
            appid = g["appid"]
            details = store_data.get(appid)
            if not details:
                continue
            price_data = details.get("price_overview")
            if not price_data:
                continue
            price_cents = price_data.get("initial", 0)
            price_dollars = price_cents / 100
            hours = g.get("playtime_forever", 0) / 60
            if hours > 0 and price_dollars > 0:
                per_hour = price_dollars / hours
                value_list.append({
                    "name": g.get("name", "Unknown"),
                    "appid": appid,
                    "price": price_dollars,
                    "hours": round(hours, 1),
                    "per_hour": round(per_hour, 2),
                    "per_hour_formatted": f"${per_hour:.2f}/hr" if per_hour >= 0.01 else "<$0.01/hr"
                })

        value_list.sort(key=lambda x: x["per_hour"])

        # Money wasted estimate
        unplayed_prices = []
        for g in unplayed_sample:
            appid = g["appid"]
            details = store_data.get(appid)
            if not details:
                continue
            price_data = details.get("price_overview")
            if price_data:
                price_cents = price_data.get("initial", 0)
                unplayed_prices.append(price_cents / 100)

        total_unplayed = len(unplayed_games)
        sampled_unplayed = len([g for g in unplayed_sample if store_data.get(g["appid"])])
        avg_unplayed_price = (sum(unplayed_prices) / len(unplayed_prices)) if unplayed_prices else 0
        estimated_wasted = avg_unplayed_price * total_unplayed

        return jsonify({
            "best_value": value_list[:5],
            "worst_value": list(reversed(value_list[-5:])) if len(value_list) > 5 else [],
            "estimated_wasted": round(estimated_wasted, 2),
            "sample_size": sampled_unplayed,
            "total_unplayed": total_unplayed,
            "avg_unplayed_price": round(avg_unplayed_price, 2),
            "is_estimate": sampled_unplayed < total_unplayed
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/indie/<steam_id>")
def api_indie(steam_id: str):
    """Async endpoint: indie analysis using review count as proxy."""
    try:
        games_data = get_owned_games(steam_id)
        games = games_data.get("response", {}).get("games", [])

        if not games:
            return jsonify({"error": "No games found"}), 404

        sample = random.sample(games, min(80, len(games)))
        appids = [g["appid"] for g in sample]

        store_data = get_app_details_batch(appids, max_workers=4, delay=0.4)

        tiers = {"hidden_gem": 0, "indie": 0, "mid_tier": 0, "mainstream": 0}
        indie_examples = {"hidden_gem": [], "indie": [], "mid_tier": [], "mainstream": []}

        for g in sample:
            appid = g["appid"]
            details = store_data.get(appid)
            if not details:
                continue
            recs = details.get("recommendations", {})
            review_count = recs.get("total", 0)

            tier = classify_indie_tier(review_count)
            tiers[tier["tier"]] += 1
            if len(indie_examples[tier["tier"]]) < 3:
                indie_examples[tier["tier"]].append({
                    "name": g.get("name", "Unknown"),
                    "appid": appid,
                    "reviews": review_count
                })

        total_classified = sum(tiers.values())
        indie_pct = 0
        if total_classified > 0:
            indie_pct = round(((tiers["hidden_gem"] + tiers["indie"]) / total_classified) * 100, 1)

        if indie_pct > 60:
            indie_roast = "You're basically running an indie game film festival. Mainstream? Never heard of it."
        elif indie_pct > 40:
            indie_roast = "A healthy mix of indie gems and AAA titles. You have taste AND a marketing budget."
        elif indie_pct > 20:
            indie_roast = "You dabble in indie, but let's be real — you're here for the blockbusters."
        else:
            indie_roast = "All AAA, all the time. You're basically a walking GameStop ad."

        return jsonify({
            "tiers": tiers,
            "examples": indie_examples,
            "indie_percentage": indie_pct,
            "total_classified": total_classified,
            "total_games": len(games),
            "roast": indie_roast
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/personality/<steam_id>")
def api_personality(steam_id: str):
    """Async endpoint: gamer personality type + badges."""
    try:
        games_data = get_owned_games(steam_id)
        games = games_data.get("response", {}).get("games", [])

        if not games:
            return jsonify({"error": "No games found"}), 404

        played_games = sorted(
            [g for g in games if g.get("playtime_forever", 0) > 0],
            key=lambda x: x.get("playtime_forever", 0),
            reverse=True
        )
        unplayed_games = [g for g in games if g.get("playtime_forever", 0) == 0]

        played_sample = played_games[:30]
        unplayed_sample = random.sample(unplayed_games, min(30, len(unplayed_games)))
        all_sample = played_sample + unplayed_sample

        appids = list(set(g["appid"] for g in all_sample))
        store_data = get_app_details_batch(appids, max_workers=4, delay=0.4)

        played_genre_counts = {}
        purchased_genre_counts = {}
        unplayed_genre_counts = {}

        for g in played_sample:
            details = store_data.get(g["appid"])
            if not details:
                continue
            cats = classify_game_genres(details)
            for c in cats:
                played_genre_counts[c] = played_genre_counts.get(c, 0) + 1
                purchased_genre_counts[c] = purchased_genre_counts.get(c, 0) + 1

        for g in unplayed_sample:
            details = store_data.get(g["appid"])
            if not details:
                continue
            cats = classify_game_genres(details)
            for c in cats:
                purchased_genre_counts[c] = purchased_genre_counts.get(c, 0) + 1
                unplayed_genre_counts[c] = unplayed_genre_counts.get(c, 0) + 1

        played_sorted = sorted(played_genre_counts.items(), key=lambda x: x[1], reverse=True)
        purchased_sorted = sorted(purchased_genre_counts.items(), key=lambda x: x[1], reverse=True)
        unplayed_sorted = sorted(unplayed_genre_counts.items(), key=lambda x: x[1], reverse=True)

        primary_type = None
        if played_sorted:
            cat_key = played_sorted[0][0]
            cat_info = GENRE_CATEGORIES.get(cat_key, {})
            primary_type = {
                "key": cat_key,
                "label": cat_info.get("label", "Gamer"),
                "emoji": cat_info.get("emoji", "🎮"),
                "count": played_sorted[0][1]
            }

        purchase_type = None
        if purchased_sorted:
            cat_key = purchased_sorted[0][0]
            cat_info = GENRE_CATEGORIES.get(cat_key, {})
            purchase_type = {
                "key": cat_key,
                "label": cat_info.get("label", "Gamer"),
                "emoji": cat_info.get("emoji", "🎮"),
                "count": purchased_sorted[0][1]
            }

        # "You think you like" — genres bought but not played, 5+ threshold
        think_you_like = None
        primary_played_key = played_sorted[0][0] if played_sorted else None
        for cat_key, count in unplayed_sorted:
            if count >= 5 and cat_key != primary_played_key:
                cat_info = GENRE_CATEGORIES.get(cat_key, {})
                think_you_like = {
                    "key": cat_key,
                    "label": cat_info.get("label", "Gamer"),
                    "emoji": cat_info.get("emoji", "🎮"),
                    "count": count
                }
                break

        genre_breakdown_played = []
        for cat_key, count in played_sorted[:6]:
            cat_info = GENRE_CATEGORIES.get(cat_key, {})
            genre_breakdown_played.append({
                "key": cat_key,
                "label": cat_info.get("label", cat_key),
                "emoji": cat_info.get("emoji", "🎮"),
                "count": count
            })

        genre_breakdown_purchased = []
        for cat_key, count in purchased_sorted[:6]:
            cat_info = GENRE_CATEGORIES.get(cat_key, {})
            genre_breakdown_purchased.append({
                "key": cat_key,
                "label": cat_info.get("label", cat_key),
                "emoji": cat_info.get("emoji", "🎮"),
                "count": count
            })

        stats = analyze_library(games)
        badges = detect_badges(stats, store_data, games)

        return jsonify({
            "primary_type": primary_type,
            "purchase_type": purchase_type,
            "think_you_like": think_you_like,
            "genre_breakdown_played": genre_breakdown_played,
            "genre_breakdown_purchased": genre_breakdown_purchased,
            "badges": badges,
            "sample_size": len(store_data)
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/friends/<steam_id>")
def api_friends(steam_id: str):
    """JSON API for friends leaderboard."""
    try:
        player_data = get_player_summary(steam_id)
        players = player_data.get("response", {}).get("players", [])

        if not players or players[0].get("communityvisibilitystate") != 3:
            return jsonify({"error": "Profile not accessible"}), 403

        user_games_data = get_owned_games(steam_id)
        user_games = user_games_data.get("response", {}).get("games", [])

        if not user_games:
            return jsonify({"error": "No games"}), 404

        user_stats = analyze_library(user_games)

        friends = get_friends_list(steam_id)

        if not friends:
            return jsonify({"leaderboard": [], "user_rank": None, "error": "No friends found or friends list is private"})

        friend_ids = [f["steamid"] for f in friends[:50]]
        all_ids = [steam_id] + friend_ids

        all_players = []
        for i in range(0, len(all_ids), 100):
            batch = all_ids[i:i+100]
            batch_data = get_player_summary(",".join(batch))
            all_players.extend(batch_data.get("response", {}).get("players", []))

        leaderboard = []

        for p in all_players:
            pid = p.get("steamid")
            pname = p.get("personaname", "Unknown")
            pavatar = p.get("avatar", "")

            if p.get("communityvisibilitystate") != 3:
                continue

            try:
                p_games_data = get_owned_games(pid)
                p_games = p_games_data.get("response", {}).get("games", [])

                if not p_games:
                    continue

                p_stats = analyze_library(p_games)

                if p_stats["shame_score"] == 100:
                    continue

                leaderboard.append({
                    "steam_id": pid,
                    "name": pname,
                    "avatar": pavatar,
                    "shame_score": p_stats["shame_score"],
                    "total_games": p_stats["total_games"],
                    "never_played": p_stats["never_played_count"],
                    "total_hours": p_stats["total_hours"],
                    "is_user": pid == steam_id
                })
            except:
                continue

        leaderboard.sort(key=lambda x: x["shame_score"], reverse=True)

        user_rank = None
        for i, entry in enumerate(leaderboard):
            entry["rank"] = i + 1
            if entry["is_user"]:
                user_rank = i + 1

        return jsonify({
            "leaderboard": leaderboard[:10],
            "total_friends": len(leaderboard) - 1,
            "user_rank": user_rank
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/friends/<steam_id>")
def friends_leaderboard(steam_id: str):
    """Show shame leaderboard for user and their friends."""

    try:
        player_data = get_player_summary(steam_id)
        players = player_data.get("response", {}).get("players", [])

        if not players:
            return render_template("error.html", error="Profile not found", message="Could not find that Steam profile.")

        player = players[0]
        player_name = player.get("personaname", "Unknown")
        avatar_url = player.get("avatarfull", "")

        if player.get("communityvisibilitystate") != 3:
            return render_template("error.html",
                error="Profile is private",
                message="Your profile needs to be public to compare with friends."
            )

        friends = get_friends_list(steam_id)

        if not friends:
            return render_template("error.html",
                error="No friends found",
                message="Either your friends list is private, or you have no friends on Steam. 😢"
            )

        all_ids = [steam_id] + [f["steamid"] for f in friends]

        all_players = []
        for i in range(0, len(all_ids), 100):
            batch = all_ids[i:i+100]
            batch_data = get_player_summary(",".join(batch))
            all_players.extend(batch_data.get("response", {}).get("players", []))

        leaderboard = []

        for p in all_players:
            pid = p.get("steamid")
            pname = p.get("personaname", "Unknown")
            pavatar = p.get("avatar", "")

            if p.get("communityvisibilitystate") != 3:
                continue

            try:
                games_data = get_owned_games(pid)
                games = games_data.get("response", {}).get("games", [])

                if not games:
                    continue

                stats = analyze_library(games)

                leaderboard.append({
                    "steam_id": pid,
                    "name": pname,
                    "avatar": pavatar,
                    "shame_score": stats["shame_score"],
                    "total_games": stats["total_games"],
                    "never_played": stats["never_played_count"],
                    "total_hours": stats["total_hours"],
                    "is_user": pid == steam_id
                })
            except:
                continue

        leaderboard.sort(key=lambda x: x["shame_score"], reverse=True)

        for i, entry in enumerate(leaderboard):
            entry["rank"] = i + 1

        user_rank = next((e["rank"] for e in leaderboard if e["is_user"]), None)

        return render_template("friends.html",
            player_name=player_name,
            avatar_url=avatar_url,
            steam_id=steam_id,
            leaderboard=leaderboard,
            user_rank=user_rank,
            total_friends=len(leaderboard) - 1
        )

    except Exception as e:
        return render_template("error.html", error="Something went wrong", message=str(e))


if __name__ == "__main__":
    if not STEAM_API_KEY:
        print("⚠️  Warning: STEAM_API_KEY not set!")
        print("   Set it with: export STEAM_API_KEY=your_key_here")

    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV") == "development"

    app.run(debug=debug, host="0.0.0.0", port=port)
