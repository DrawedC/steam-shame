# Genre grouping to make DNA radar more meaningful and less noisy
GENRE_GROUPS = {
    # Action family → group into one main bucket
    'action': 'action',
    'shooter': 'action',
    'fps': 'action',
@@ -34,29 +33,24 @@
    'beat \'em up': 'action',
    'fighting': 'action',
    'platformer': 'action',
    'metroidvania': 'action',  # many consider these action-adventure, but group here
    'roguelike': 'action',     # often action-heavy
    # Adventure family
    'metroidvania': 'action',
    'roguelike': 'action',
    'adventure': 'adventure',
    'visual novel': 'adventure',
    'point & click': 'adventure',
    'walking simulator': 'adventure',
    # RPG family
    'rpg': 'rpg',
    'jrpg': 'rpg',
    'role-playing': 'rpg',
    # Strategy / turn-based
    'strategy': 'strategy',
    'turn-based strategy': 'strategy',
    '4x': 'strategy',
    'tower defense': 'strategy',
    'real time strategy': 'strategy',
    # Simulation / management
    'simulation': 'simulation',
    'management': 'simulation',
    'building': 'simulation',
    'farming sim': 'simulation',
    # Other common groups
    'indie': 'indie',
    'casual': 'casual',
    'racing': 'racing',
@@ -69,7 +63,6 @@

# ============== Steam API ==============
def get_owned_games(steam_id):
    """Fetch owned games with short-term cache to avoid redundant calls."""
    now = time.time()
    with _games_cache_lock:
        c = _games_cache.get(steam_id)
@@ -121,6 +114,7 @@
        if c and (now - c["ts"]) < STORE_CACHE_TTL:
            return c["data"]
    try:
        # Force English to avoid localized genre names
        url = f"https://store.steampowered.com/api/appdetails?appids={appid}&l=english"
        r = requests.get(url, timeout=10)
        if r.status_code == 429:
@@ -138,7 +132,6 @@
    return None

def get_app_details_batch(appids, max_workers=4, delay=0.5):
    """Batch fetch store details. Slower but more reliable to avoid rate limits."""
    results = {}
    def fetch(aid):
        time.sleep(random.uniform(0.2, delay))
@@ -203,7 +196,7 @@
                 "playtime":g.get("playtime_forever",0),
                 "playtime_fmt":format_playtime(g.get("playtime_forever",0))} for g in lst[:limit]]

    backlog_days = round(len(raw_unplayed) * 10)  # 10h avg at 1h/day
    backlog_days = round(len(raw_unplayed) * 10)

    suggest = None
    if unplayed:
@@ -256,14 +249,10 @@
}

def classify_game_genres(details):
    """
    Extract and group Steam genres into broader categories
    Returns a list of grouped genre names (lowercase)
    """
    if not details or 'genres' not in details:
        return []
    raw_genres = [g['description'].lower() for g in details.get('genres', [])]
    grouped = set()  # use set to avoid duplicates
    grouped = set()
    for genre in raw_genres:
        grouped_name = GENRE_GROUPS.get(genre, genre)
        grouped.add(grouped_name)
@@ -272,7 +261,6 @@
    return sorted(list(grouped))

def detect_descriptor(stats):
    """Primary identity based on play habits."""
    played_pct = (stats["played_count"] / stats["total_games"] * 100) if stats["total_games"] else 0
    abandoned_pct = (stats["abandoned_count"] / stats["total_games"] * 100) if stats["total_games"] else 0
    unplayed_pct = (stats["never_played_count"] / stats["total_games"] * 100) if stats["total_games"] else 0
@@ -287,7 +275,6 @@
                "description": "You buy games like they're going out of style. They're not."}

def detect_badges_instant(stats, games):
    """Badges computable instantly without store API calls."""
    badges = []
    if stats["never_played_count"] == 0:
        badges.append({"name": "Pristine Library", "emoji": "✨",
@@ -312,7 +299,6 @@
    return badges[:6]

def detect_badges(stats, store_details, games):
    """Full badge detection including store-dependent badges."""
    badges = detect_badges_instant(stats, games)
    ea = sum(1 for d in store_details.values()
             if "early access" in [g.get("description", "").lower() for g in d.get("genres", [])])
@@ -400,7 +386,6 @@

@app.route("/api/suggest/<steam_id>")
def api_suggest(steam_id):
    """Return a random unplayed game with its store image."""
    try:
        games = get_owned_games(steam_id).get("response",{}).get("games",[])
        if not games: return jsonify({"error":"No games"}), 404
@@ -452,58 +437,107 @@
        pc, pg = count_genres(played_sample, weight_by_playtime=True)
        uc, ug = count_genres(unplayed_sample, weight_by_playtime=False)

        # Normalize
        def norm(counts):
            total = sum(counts.values()) or 1
            return {k: round((v / total) * 100, 1) for k, v in counts.items()}

        all_genres = sorted(set(list(oc) + list(pc) + list(uc)))

        on, pn, un = norm(oc), norm(pc), norm(uc)

        labels = [
            {"key": k,
             "label": GENRE_CATEGORIES.get(k, {}).get("label", k.capitalize()),
             "emoji": GENRE_CATEGORIES.get(k, {}).get("emoji", "🎮")}
            for k in all_genres
        ]

        on = norm(oc)
        pn = norm(pc)
        un = norm(uc)

        # === 5% threshold + Misc grouping ===
        MIN_THRESHOLD = 5.0

        # Find max % for each genre across all three
        genre_max_pct = {}
        all_possible = set(on) | set(pn) | set(un)
        for k in all_possible:
            max_pct = max(on.get(k, 0), pn.get(k, 0), un.get(k, 0))
            genre_max_pct[k] = max_pct

        # Split major / minor
        major_genres = [k for k, pct in genre_max_pct.items() if pct >= MIN_THRESHOLD]
        minor_genres = [k for k, pct in genre_max_pct.items() if pct < MIN_THRESHOLD]

        # Misc sums
        misc_owned    = sum(on.get(k, 0) for k in minor_genres)
        misc_played   = sum(pn.get(k, 0) for k in minor_genres)
        misc_unplayed = sum(un.get(k, 0) for k in minor_genres)

        # Display order
        display_genres = sorted(major_genres)
        has_misc = bool(minor_genres)
        if has_misc:
            display_genres.append("misc")

        # Build labels
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

        # Radar data
        radar = {
            "labels": labels,
            "owned": [on.get(k, 0) for k in all_genres],
            "played": [pn.get(k, 0) for k in all_genres],
            "unplayed": [un.get(k, 0) for k in all_genres]
        }

        genre_games = {
            k: {"owned": og.get(k, []), "played": pg.get(k, []), "unplayed": ug.get(k, [])}
            for k in all_genres
            "owned":   [on.get(k, 0) if k != "misc" else misc_owned    for k in display_genres],
            "played":  [pn.get(k, 0) if k != "misc" else misc_played   for k in display_genres],
            "unplayed": [un.get(k, 0) if k != "misc" else misc_unplayed for k in display_genres]
        }

        def maj(counts):
        # genre_games with Misc combined
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

        # Majority with Misc support
        def maj(counts, misc_sum=0):
            if not counts: return None
            top_key = max(counts, key=counts.get)
            total = sum(counts.values()) or 1
            i = GENRE_CATEGORIES.get(top_key, {})
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
                "pct": round((counts[top_key] / total) * 100, 1)
                "pct": round((effective[top_key] / total) * 100, 1)
            }

        om, pm, um = maj(oc), maj(pc), maj(uc)
        om = maj(oc, misc_owned)
        pm = maj(pc, misc_played)
        um = maj(uc, misc_unplayed)

        mismatch = pm and um and pm["key"] != um["key"]

        stats = analyze_library(games)
        badges = detect_badges(stats, sd, games)

        mismatch_badge = None
        if mismatch and um:
            mismatch_badge = {
                "emoji": "🤔",
                "title": f"Thinks They Like {um['label']}",
                "description": f"Your unplayed library is full of {um['emoji']} {um['label']} games, but that's not what you actually play."
            }
            mismatch_badge = {"emoji": "🤔", "title": f"Thinks They Like {um['label']}",
                              "description": f"Your unplayed library is full of {um['emoji']} {um['label']} games, but that's not what you actually play."}

        return jsonify({
            "radar": radar,
@@ -605,10 +639,9 @@
        stats = analyze_library(games)

        W, H = 1200, 630
        img = Image.new('RGB', (W, H), (10, 10, 18))  # dark base
        img = Image.new('RGB', (W, H), (10, 10, 18))
        draw = ImageDraw.Draw(img)

        # Subtle radial gradient background
        center_x, center_y = W//2, H//3
        for r in range(400, 0, -2):
            alpha = int(40 * (1 - r/400))
@@ -617,7 +650,6 @@
                fill=(30 + alpha//3, 20 + alpha//4, 80 + alpha//2)
            )

        # Load fonts
        font_path_inter = "static/fonts/Inter-Bold.ttf"
        font_path_orbitron = "static/fonts/Orbitron-Bold.ttf"
        try:
@@ -631,7 +663,6 @@
        name = p.get("personaname", "Player")
        score_str = f"{stats['shame_score']:.1f}"

        # Glow effect
        glow = Image.new('RGBA', (W, H), (0,0,0,0))
        glow_draw = ImageDraw.Draw(glow)
        for offset, color, size in [
@@ -643,44 +674,42 @@
        glow = glow.filter(ImageFilter.GaussianBlur(12))
        img.paste(glow, (0,0), glow)

        # Main score text
        for dx, dy, color in [(-3,-3,(255,140,60)), (3,3,(255,60,140)), (0,0,(255,100,100))]:
            draw.text((W//2 + dx, 100 + dy), score_str, fill=color, font=font_huge, anchor="mm")

        # % symbol
        pct_x = W//2 + font_huge.getlength(score_str) // 2 + 20
        draw.text((pct_x, 100 + 60), "%", fill=(255, 180, 120), font=font_med, anchor="lm")

        draw.text((W//2, 260), "SHAME SCORE", fill=(160, 160, 200), font=font_med, anchor="mm")

        stats_line = f"{stats['total_games']} GAMES OWNED • {stats['never_played_count']} NEVER PLAYED"
        draw.text((W//2, 340), stats_line, fill=(200, 200, 220), font=font_sm, anchor="mm")

        draw.text((W//2, 420), name.upper(), fill=(220, 220, 255), font=font_large, anchor="mm")

        draw.text((W//2, H - 40), "SteamShame • steam-shame.up.railway.app", fill=(100, 100, 140), font=font_sm, anchor="mm")

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
