import os
import time
import random
import secrets
import hashlib
from datetime import datetime, timezone, timedelta
import psycopg2
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Auth-Token, X-Admin-Token"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    return response


@app.route("/api/<path:_any>", methods=["OPTIONS"])
@app.route("/api/<path:_any>/", methods=["OPTIONS"])
def cors_preflight(_any=None):
    return ("", 204)

DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_USER = "abcd051410"

# token -> {username, exp}
AUTH_TOKENS = {}
TOKEN_TTL = 60 * 60 * 24 * 7  # 7 days

# ===== 공유 상태 (메모리) =====
CURRENT_NOTICE = {"id": "", "message": "", "created_at": 0}
CURRENT_PATCH = {"id": "", "title": "", "date": "", "body": "", "created_at": 0}
FORCE_RELOAD = {"id": 0, "message": ""}

DIFFICULTY_PRESETS = {
    # mult는 '이익분'에만 적용 (원금 환불 제외) — 밸런스 5/5용
    "easy": {"mode": "easy", "label": "쉬움", "rtp": 0.90, "mult": 0.90},
    "normal": {"mode": "normal", "label": "보통", "rtp": 0.96, "mult": 0.96},
    "hard": {"mode": "hard", "label": "어려움", "rtp": 0.99, "mult": 0.99},
}
DIFFICULTY_CONFIG = dict(DIFFICULTY_PRESETS["normal"])

# 난이도별 하우 수익 누적 (관리자 조회용, 메모리)
DIFFICULTY_STATS = {
    "easy": {"house_net": 0},
    "normal": {"house_net": 0},
    "hard": {"house_net": 0},
}

# ===== 시즌 / 미션 (싱글플레이·비실시간) =====
SEASON_CONFIG = {
    "id": 1,
    "name": "시즌 1",
    "label": "ROYAL S1",
    "started_at": time.time(),
}


def ensure_season_tables(cursor, conn):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS season_meta (
            id INT PRIMARY KEY DEFAULT 1,
            season_id INT NOT NULL DEFAULT 1,
            name VARCHAR(50) NOT NULL DEFAULT '시즌 1',
            label VARCHAR(50) NOT NULL DEFAULT 'ROYAL S1',
            started_at DOUBLE PRECISION DEFAULT 0,
            duration_days INT DEFAULT 14
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS season_archives (
            season_id INT PRIMARY KEY,
            name VARCHAR(50) NOT NULL,
            label VARCHAR(50) NOT NULL,
            started_at DOUBLE PRECISION DEFAULT 0,
            ended_at DOUBLE PRECISION DEFAULT 0,
            rankings TEXT DEFAULT '[]'
        )
        """
    )
    cursor.execute("SELECT season_id FROM season_meta WHERE id=1")
    row = cursor.fetchone()
    if not row:
        cursor.execute(
            """
            INSERT INTO season_meta (id, season_id, name, label, started_at)
            VALUES (1, %s, %s, %s, %s)
            """,
            (
                int(SEASON_CONFIG.get("id") or 1),
                SEASON_CONFIG.get("name") or "시즌 1",
                SEASON_CONFIG.get("label") or "ROYAL S1",
                float(SEASON_CONFIG.get("started_at") or time.time()),
            ),
        )
    conn.commit()


def load_season_config_from_db():
    """서버 시작/요청 시 DB의 현재 시즌을 메모리에 반영"""
    global SEASON_CONFIG
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            ensure_season_tables(cursor, conn)
            cursor.execute(
                "SELECT season_id, name, label, COALESCE(started_at,0) FROM season_meta WHERE id=1"
            )
            row = cursor.fetchone()
            if row:
                started = float(row[3] or 0)
                duration = 14
                SEASON_CONFIG = {
                    "id": int(row[0] or 1),
                    "name": row[1] or f"시즌 {int(row[0] or 1)}",
                    "label": row[2] or f"ROYAL S{int(row[0] or 1)}",
                    "started_at": started,
                    "duration_days": duration,
                    "ends_at": started + duration * 86400 if started else 0,
                }
        finally:
            cursor.close()
            release_db(conn)
    except Exception as e:
        print(f"load_season_config_from_db: {e}")


def save_season_meta():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            ensure_season_tables(cursor, conn)
            cursor.execute(
                """
                INSERT INTO season_meta (id, season_id, name, label, started_at)
                VALUES (1, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    season_id=EXCLUDED.season_id,
                    name=EXCLUDED.name,
                    label=EXCLUDED.label,
                    started_at=EXCLUDED.started_at
                """,
                (
                    int(SEASON_CONFIG.get("id") or 1),
                    SEASON_CONFIG.get("name") or "시즌 1",
                    SEASON_CONFIG.get("label") or "ROYAL S1",
                    float(SEASON_CONFIG.get("started_at") or time.time()),
                ),
            )
            conn.commit()
        finally:
            cursor.close()
            release_db(conn)
    except Exception as e:
        print(f"save_season_meta: {e}")

# 일일 미션 정의 (code, title, target, reward_cash)
DAILY_MISSIONS = [
    {"code": "attend", "title": "출석체크 1회", "target": 1, "reward": 15000},
    {"code": "play_any", "title": "아무 게임 5회 플레이", "target": 5, "reward": 25000},
    {"code": "play_bacc", "title": "바카라 3회", "target": 3, "reward": 20000},
    {"code": "win_any", "title": "게임 1회 승리", "target": 1, "reward": 20000},
    {"code": "transfer", "title": "송금 1회", "target": 1, "reward": 10000},
]



def calc_season_points(init_w, game_cash):
    """시즌 점수: 수익률만 (공정 10/10). 세션 상한 30000"""
    try:
        init_w = int(init_w or 0)
        game_cash = int(game_cash or 0)
    except (TypeError, ValueError):
        return 0, {"efficiency": 0, "profit_pts": 0, "profit": 0, "roi": 0.0}
    profit = game_cash - init_w
    if profit <= 0:
        return 0, {"efficiency": 0, "profit_pts": 0, "profit": int(profit), "roi": 0.0}
    base = max(init_w, 1)
    roi = profit / float(base)
    efficiency = int(min(30000, roi * 10000))
    return efficiency, {
        "efficiency": efficiency,
        "profit_pts": 0,
        "profit": int(profit),
        "roi": round(roi * 100, 2),
        "capped": efficiency,
        "mode": "roi_only",
    }


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dig = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120000)
    return f"pbkdf2:{salt}:{dig.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if not stored:
        return False
    if stored.startswith("pbkdf2:"):
        try:
            _, salt, hexdig = stored.split(":", 2)
            dig = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), salt.encode("utf-8"), 120000
            )
            return secrets.compare_digest(dig.hex(), hexdig)
        except Exception:
            return False
    # 구버전 평문 호환
    return secrets.compare_digest(str(stored), str(password))


def issue_token(username: str) -> str:
    # 유저당 토큰 정리(간단)
    dead = [k for k, v in AUTH_TOKENS.items() if v.get("exp", 0) < time.time() or v.get("username") == username]
    for k in dead:
        AUTH_TOKENS.pop(k, None)
    tok = secrets.token_urlsafe(32)
    AUTH_TOKENS[tok] = {"username": username, "exp": time.time() + TOKEN_TTL}
    return tok


def get_auth_username():
    tok = request.headers.get("X-Auth-Token") or ""
    if not tok and request.method in ("POST", "PUT", "PATCH"):
        data = request.get_json(silent=True)
        if not data:
            try:
                import json as _json
                data = _json.loads(request.data.decode("utf-8") or "{}")
            except Exception:
                data = {}
        tok = (data or {}).get("token") or ""
    if not tok and request.method == "GET":
        tok = request.args.get("token") or ""
    if not tok:
        return None
    info = AUTH_TOKENS.get(tok)
    if not info or info.get("exp", 0) < time.time():
        AUTH_TOKENS.pop(tok, None)
        return None
    return info.get("username")


def require_user():
    """친구 서버용: 토큰 없으면 본문/쿼리 username 허용"""
    u = get_auth_username()
    if u:
        return u, None
    data = request.get_json(silent=True) or {}
    if not data and request.data:
        try:
            import json as _json
            data = _json.loads(request.data.decode("utf-8") or "{}")
        except Exception:
            data = {}
    u = (data.get("username") or data.get("from_user") or request.args.get("user") or "").strip()
    if u:
        return u, None
    return None, (jsonify({"error": "username 필요"}), 400)


def require_admin():
    """관리자 인증 강화: 토큰(관리자) 또는 pw 확인"""
    u = get_auth_username()
    if u == ADMIN_USER:
        return ADMIN_USER, None

    pw = None
    if request.method in ("POST", "PUT", "PATCH"):
        data = request.get_json(silent=True) or {}
        if not data and request.data:
            try:
                import json as _json
                data = _json.loads(request.data.decode("utf-8") or "{}")
            except Exception:
                data = {}
        pw = (data.get("pw") or data.get("admin_user") or data.get("password") or "").strip()
    else:
        pw = (request.args.get("pw") or request.args.get("admin_user") or "").strip()

    if pw in ("abcd051410", ADMIN_USER):
        return ADMIN_USER, None
    return None, (jsonify({"error": "Unauthorized - 관리자만 접근 가능"}), 403)


# rank 높을수록 상위. 아이언4(최저) → ... → 챌린저(최고)
# Iron~Diamond: 4가 낮고 1이 높음

def kst_today():
    kst = timezone(timedelta(hours=9))
    return datetime.now(kst).strftime("%Y-%m-%d")


def _load_mission_data(raw):
    import json as _json
    try:
        d = _json.loads(raw or "{}")
        if not isinstance(d, dict):
            d = {}
    except Exception:
        d = {}
    return d


def _save_mission_data(cursor, username, data):
    import json as _json
    cursor.execute(
        "UPDATE users SET mission_data=%s WHERE username=%s",
        (_json.dumps(data, ensure_ascii=False), username),
    )


def title_for_tier(tier_name):
    mapping = {
        "챌린저": "전설의 도전자",
        "그랜드마스터": "그랜드 마스터",
        "마스터": "카지노 마스터",
        "다이아1": "다이아몬드 엘리트",
        "다이아2": "다이아 플레이어",
        "다이아3": "다이아 플레이어",
        "다이아4": "다이아 플레이어",
        "에메랄드1": "에메랄드 고수",
        "에메랄드2": "에메랄드",
        "에메랄드3": "에메랄드",
        "에메랄드4": "에메랄드",
        "플래티넘1": "플래티넘 스타",
        "플래티넘2": "플래티넘",
        "플래티넘3": "플래티넘",
        "플래티넘4": "플래티넘",
        "골드1": "골든 핸드",
        "골드2": "골드",
        "골드3": "골드",
        "골드4": "골드",
        "실버1": "실버 라이저",
        "실버2": "실버",
        "실버3": "실버",
        "실버4": "실버",
        "브론즈1": "브론즈",
        "브론즈2": "브론즈",
        "브론즈3": "브론즈",
        "브론즈4": "브론즈",
        "아이언1": "뉴비 아이언",
        "아이언2": "뉴비 아이언",
        "아이언3": "뉴비 아이언",
        "아이언4": "뉴비 아이언",
    }
    return mapping.get(tier_name or "", "로얄 멤버")


TIER_TABLE = [
    # min_rolling, name, badge, fee_rate, attend, max_bet_mult, roll_ratio, rank
    # 후반 (상징+도전, 기존 20억 → 8억으로 현실화)
    (800_000_000,   "챌린저",     "🏆", 0.005, 150000, 5.0, 0.35, 31),
    (500_000_000,   "그랜드마스터", "⚜️", 0.007, 135000, 4.5, 0.38, 30),
    (320_000_000,   "마스터",     "🔥", 0.009, 125000, 4.0, 0.42, 29),
    (200_000_000,   "다이아1",    "💎", 0.012, 115000, 3.6, 0.48, 28),
    (140_000_000,   "다이아2",    "💎", 0.014, 110000, 3.3, 0.50, 27),
    (95_000_000,    "다이아3",    "💎", 0.016, 105000, 3.0, 0.52, 26),
    (65_000_000,    "다이아4",    "💎", 0.018, 100000, 2.8, 0.55, 25),
    (45_000_000,    "에메랄드1",  "💚", 0.020, 95000, 2.5, 0.58, 24),
    (32_000_000,    "에메랄드2",  "💚", 0.022, 90000, 2.3, 0.60, 23),
    (22_000_000,    "에메랄드3",  "💚", 0.024, 88000, 2.1, 0.62, 22),
    (15_000_000,    "에메랄드4",  "💚", 0.026, 85000, 2.0, 0.65, 21),
    (10_000_000,    "플래티넘1",  "💠", 0.028, 80000, 1.9, 0.68, 20),
    (7_000_000,     "플래티넘2",  "💠", 0.030, 78000, 1.8, 0.70, 19),
    (5_000_000,     "플래티넘3",  "💠", 0.032, 75000, 1.7, 0.72, 18),
    (3_500_000,     "플래티넘4",  "💠", 0.034, 72000, 1.6, 0.75, 17),
    (2_400_000,     "골드1",     "🥇", 0.036, 70000, 1.5, 0.78, 16),
    (1_700_000,     "골드2",     "🥇", 0.038, 68000, 1.4, 0.80, 15),
    (1_200_000,     "골드3",     "🥇", 0.040, 65000, 1.35,0.82, 14),
    (850_000,       "골드4",     "🥇", 0.042, 62000, 1.3, 0.84, 13),
    (600_000,       "실버1",     "🥈", 0.045, 58000, 1.25,0.86, 12),
    (420_000,       "실버2",     "🥈", 0.048, 55000, 1.2, 0.88, 11),
    (300_000,       "실버3",     "🥈", 0.050, 52000, 1.15,0.90, 10),
    (200_000,       "실버4",     "🥈", 0.052, 50000, 1.1, 0.92,  9),
    (130_000,       "브론즈1",   "🥉", 0.054, 47000, 1.05,0.94,  8),
    (85_000,        "브론즈2",   "🥉", 0.056, 45000, 1.0, 0.95,  7),
    (55_000,        "브론즈3",   "🥉", 0.058, 43000, 1.0, 0.96,  6),
    (35_000,        "브론즈4",   "🥉", 0.060, 40000, 1.0, 0.97,  5),
    (20_000,        "아이언1",   "⚙️", 0.060, 36000, 1.0, 0.98,  4),
    (10_000,        "아이언2",   "⚙️", 0.060, 32000, 1.0, 0.99,  3),
    (4_000,         "아이언3",   "⚙️", 0.060, 28000, 1.0, 1.00,  2),
    (0,             "아이언4",   "⚙️", 0.060, 25000, 1.0, 1.00,  1),
]



def get_tier(rolling):
    rolling = int(rolling or 0)
    for i, row in enumerate(TIER_TABLE):
        mn, name, badge, fee, attend, mult, ratio, rank = row
        if rolling >= mn:
            if i == 0:
                next_name, next_need = None, 0
            else:
                nmin, nname = TIER_TABLE[i - 1][0], TIER_TABLE[i - 1][1]
                next_name, next_need = nname, max(0, nmin - rolling)
            return {
                "name": name,
                "badge": badge,
                "fee_rate": fee,
                "attend_reward": attend,
                "max_bet_mult": mult,
                "roll_ratio": ratio,
                "min_rolling": mn,
                "rolling": rolling,
                "next_name": next_name,
                "next_need": next_need,
                "rank": rank,
            }
    return get_tier(0)


def ensure_user_columns(cursor, conn=None):
    """누락 컬럼 보정 — 로그인/회원가입 실패 방지"""
    # 비밀번호 해시(pbkdf2)가 100자 초과 → 컬럼 확장
    try:
        cursor.execute("ALTER TABLE users ALTER COLUMN password TYPE VARCHAR(255)")
    except Exception:
        try:
            conn and conn.rollback()
        except Exception:
            pass
        try:
            cursor.execute("ALTER TABLE users ALTER COLUMN password TYPE TEXT")
        except Exception:
            try:
                conn and conn.rollback()
            except Exception:
                pass
    cols = [
        ("display_name", "VARCHAR(50) DEFAULT ''"),
        ("total_profit", "BIGINT DEFAULT 0"),
        ("last_seen", "DOUBLE PRECISION DEFAULT 0"),
        ("suspended", "BOOLEAN DEFAULT FALSE"),
        ("profile_title", "VARCHAR(40) DEFAULT ''"),
        ("daily_loss", "BIGINT DEFAULT 0"),
        ("daily_loss_date", "VARCHAR(20) DEFAULT ''"),
        ("self_excluded", "BOOLEAN DEFAULT FALSE"),
        ("season_points", "BIGINT DEFAULT 0"),
        ("mission_data", "TEXT DEFAULT '{}'"),
        ("last_recovery", "VARCHAR(20) DEFAULT ''"),
    ]
    for col, typedef in cols:
        try:
            cursor.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {typedef}")
        except Exception:
            try:
                cursor.execute(f"ALTER TABLE users ADD COLUMN {col} {typedef}")
            except Exception:
                pass
    if conn is not None:
        try:
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass


def get_db_connection():
    """매 요청마다 새 연결 (풀 고갈 없음). 반드시 release_db로 닫을 것."""
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL 환경 변수가 설정되지 않았습니다!")
    conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
    return conn


def release_db(conn):
    """연결 종료. 예외가 나도 무시."""
    if conn is None:
        return
    try:
        conn.close()
    except Exception:
        pass


def log_transaction(cursor, username, kind, amount, balance_after=0, memo=""):
    """입출금 기록. amount 양수=입금, 음수=출금."""
    try:
        cursor.execute(
            """
            INSERT INTO transactions (username, kind, amount, balance_after, memo)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (username, kind, int(amount), int(balance_after or 0), memo or ""),
        )
    except Exception as e:
        print(f"log_transaction error: {e}")


def resolve_username(identifier, conn=None):
    """아이디 또는 이름으로 username 찾기. 동명이인이면 None, multi 플래그."""
    identifier = (identifier or "").strip()
    if not identifier:
        return None, "empty"
    own_conn = conn is None
    if own_conn:
        conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT username FROM users WHERE username = %s", (identifier,))
    row = cursor.fetchone()
    if row:
        if own_conn:
            cursor.close(); release_db(conn)
        return row[0], None
    cursor.execute(
        "SELECT username FROM users WHERE TRIM(COALESCE(display_name,'')) = %s",
        (identifier,),
    )
    rows = cursor.fetchall()
    if own_conn:
        cursor.close(); release_db(conn)
    if len(rows) == 1:
        return rows[0][0], None
    if len(rows) > 1:
        return None, "ambiguous"
    return None, "not_found"


def init_db():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # 기본 테이블
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                username VARCHAR(50) PRIMARY KEY,
                password VARCHAR(255) NOT NULL,
                cash BIGINT DEFAULT 1000000,
                game_cash BIGINT DEFAULT 0,
                total_rolling BIGINT DEFAULT 0,
                initial_withdraw BIGINT DEFAULT 0,
                current_rolling BIGINT DEFAULT 0,
                profit_rate REAL DEFAULT 0.0,
                last_attendance VARCHAR(20) DEFAULT '',
                display_name VARCHAR(50) DEFAULT '',
                total_profit BIGINT DEFAULT 0
            )
        """)

        # 기존 DB에 컬럼이 없을 수 있으므로 안전하게 추가
        for col, typedef in [
            ("display_name", "VARCHAR(50) DEFAULT ''"),
            ("total_profit", "BIGINT DEFAULT 0"),
            ("last_seen", "DOUBLE PRECISION DEFAULT 0"),
            ("suspended", "BOOLEAN DEFAULT FALSE"),
            ("profile_title", "VARCHAR(40) DEFAULT ''"),
            ("daily_loss", "BIGINT DEFAULT 0"),
            ("daily_loss_date", "VARCHAR(20) DEFAULT ''"),
            ("self_excluded", "BOOLEAN DEFAULT FALSE"),
            ("season_points", "BIGINT DEFAULT 0"),
            ("mission_data", "TEXT DEFAULT '{}'"),
        ]:
            try:
                cursor.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {typedef}")
            except Exception:
                # IF NOT EXISTS 미지원 환경 대비
                try:
                    cursor.execute(f"ALTER TABLE users ADD COLUMN {col} {typedef}")
                except Exception:
                    pass

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id SERIAL PRIMARY KEY,
                username VARCHAR(50) NOT NULL,
                kind VARCHAR(30) NOT NULL,
                amount BIGINT NOT NULL,
                balance_after BIGINT DEFAULT 0,
                memo TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        try:
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(username, created_at DESC)")
        except Exception:
            pass

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                from_user VARCHAR(50) NOT NULL,
                to_user VARCHAR(50) NOT NULL,
                body TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_read BOOLEAN DEFAULT FALSE
            )
        """)
        try:
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_msg_to ON messages(to_user, is_read)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_msg_from ON messages(from_user)")
        except Exception:
            pass

        conn.commit()
        cursor.close()
        release_db(conn)
        print("Database initialized successfully!")
    except Exception as e:
        print(f"Database initialization error details: {e}")


# ===== 공유 테이블 (폴링용, 메모리) =====
_TABLE_LOCK_NOTE = "single-process memory; fine for one Flask worker"


def _new_deck():
    suits = ["♠", "♥", "♦", "♣"]
    ranks = [
        ("A", 1), ("2", 2), ("3", 3), ("4", 4), ("5", 5), ("6", 6),
        ("7", 7), ("8", 8), ("9", 9), ("10", 0), ("J", 0), ("Q", 0), ("K", 0),
    ]
    d = [{"suit": s, "name": n, "val": v} for s in suits for n, v in ranks for _ in range(8)]
    random.shuffle(d)
    return d


def _score(hand):
    return sum(c["val"] for c in hand) % 10


def _gen_baccarat_round():
    deck = _new_deck()
    p = [deck.pop(), deck.pop()]
    b = [deck.pop(), deck.pop()]
    ps, bs = _score(p), _score(b)
    p3 = None
    # 내추럴이면 추가 카드 없음
    if ps < 8 and bs < 8:
        if ps <= 5:
            p3 = deck.pop()
            p.append(p3)
            ps = _score(p)
        def banker_draws():
            if p3 is None:
                return bs <= 5
            pv = p3["val"]
            if bs <= 2:
                return True
            if bs == 3:
                return pv != 8
            if bs == 4:
                return pv in (2, 3, 4, 5, 6, 7)
            if bs == 5:
                return pv in (4, 5, 6, 7)
            if bs == 6:
                return pv in (6, 7)
            return False
        if banker_draws():
            b.append(deck.pop())
            bs = _score(b)
    if ps > bs:
        result = "PLAYER"
    elif bs > ps:
        result = "BANKER"
    else:
        result = "TIE"
    return {
        "player": p,
        "banker": b,
        "pScore": ps,
        "bScore": bs,
        "result": result,
    }


def _gen_dt_round():
    # val for DT comparison: A=1..10=10,J=11,Q=12,K=13
    rank_map = {"A": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9, "10": 10, "J": 11, "Q": 12, "K": 13}
    suits = ["♠", "♥", "♦", "♣"]
    names = list(rank_map.keys())
    def card():
        n = random.choice(names)
        return {"suit": random.choice(suits), "name": n, "val": rank_map[n]}
    d, t = card(), card()
    if d["val"] > t["val"]:
        result = "DRAGON"
    elif t["val"] > d["val"]:
        result = "TIGER"
    else:
        result = "TIE"
    # multi 2/3/5/8 weighted
    r = random.random()
    if r < 0.45:
        multi = 2
    elif r < 0.75:
        multi = 3
    elif r < 0.92:
        multi = 5
    else:
        multi = 8
    suit_names = ["스페이드", "하트", "다이아", "클로버"]
    suit_syms = {"스페이드": "♠", "하트": "♥", "다이아": "♦", "클로버": "♣"}
    sn = random.choice(suit_names)
    return {
        "dragon": d,
        "tiger": t,
        "result": result,
        "multi": multi,
        "suit_name": sn,
        "suit_sym": suit_syms[sn],
    }


def _gen_history(game, n=None):
    """플레이어 없을 때 보여줄 초기 로드맵 기록"""
    if n is None:
        n = random.randint(28, 40)
    hist = []
    for _ in range(n):
        r = random.random()
        if game == "baccarat":
            if r > 0.88:
                res = "TIE"
            elif r > 0.48:
                res = "BANKER"
            else:
                res = "PLAYER"
        else:
            if r > 0.90:
                res = "TIE"
            elif r > 0.45:
                res = "TIGER"
            else:
                res = "DRAGON"
        hist.append(res)
    return hist


TABLES = {
    "baccarat": {
        "round_id": 1,
        "phase": "betting",
        "phase_end": 0.0,
        "payload": None,
        "history": [],
        "history_seeded": False,
    },
    "dragontiger": {
        "round_id": 1,
        "phase": "betting",
        "phase_end": 0.0,
        "payload": None,
        "history": [],
        "history_seeded": False,
    },
}

BETTING_SEC = 12
PLAYING_SEC_BACC = 22
PLAYING_SEC_DT = 12
RESULT_SEC = 5


def _ensure_table(game):
    now = time.time()
    t = TABLES[game]
    if not t.get("history_seeded"):
        t["history"] = _gen_history(game)
        t["history_seeded"] = True
    if t["phase_end"] <= 0:
        t["phase"] = "betting"
        t["phase_end"] = now + BETTING_SEC
        t["payload"] = None
        return t
    guard = 0
    while now >= t["phase_end"] and guard < 10:
        guard += 1
        if t["phase"] == "betting":
            t["phase"] = "playing"
            play_sec = PLAYING_SEC_BACC if game == "baccarat" else PLAYING_SEC_DT
            t["phase_end"] = now + play_sec
            # 배팅 중에 미리 만들어 둔 프리뷰가 있으면 카드만 확정
            if game == "baccarat":
                t["payload"] = _gen_baccarat_round()
            else:
                preview = t.get("payload") or {}
                full = _gen_dt_round()
                # 배팅 중 공개한 multi/suit 유지
                if preview.get("multi"):
                    full["multi"] = preview["multi"]
                    full["suit_name"] = preview.get("suit_name") or full["suit_name"]
                    full["suit_sym"] = preview.get("suit_sym") or full["suit_sym"]
                t["payload"] = full
        elif t["phase"] == "playing":
            t["phase"] = "result"
            t["phase_end"] = now + RESULT_SEC
            # 라운드 결과를 공유 기록에 추가
            payload = t.get("payload") or {}
            res = payload.get("result")
            if res:
                t.setdefault("history", []).append(res)
                if len(t["history"]) > 80:
                    t["history"] = t["history"][-80:]
        else:
            t["round_id"] += 1
            t["phase"] = "betting"
            t["phase_end"] = now + BETTING_SEC
            if game == "dragontiger":
                prev = _gen_dt_round()
                t["payload"] = {
                    "multi": prev["multi"],
                    "suit_name": prev["suit_name"],
                    "suit_sym": prev["suit_sym"],
                    "dragon": None,
                    "tiger": None,
                    "result": None,
                }
            else:
                t["payload"] = None
        now = time.time()
    return t


@app.route("/api/table/state", methods=["GET"])
def table_state():
    game = (request.args.get("game") or "baccarat").strip().lower()
    if game not in TABLES:
        return jsonify({"error": "unknown game"}), 400
    t = _ensure_table(game)
    now = time.time()
    remain = max(0, int(t["phase_end"] - now))
    return jsonify({
        "game": game,
        "round_id": t["round_id"],
        "phase": t["phase"],
        "remain": remain,
        "phase_end": t["phase_end"],
        "server_now": now,
        "payload": t["payload"],
        "history": t.get("history") or [],
        "history_len": len(t.get("history") or []),
    })


@app.route("/api/user_balance", methods=["GET"])
@app.route("/api/balance", methods=["GET"])
def get_balance():
    try:
        username = (request.args.get("user") or request.args.get("username") or "").strip()
        if not username:
            return jsonify({"error": "user required"}), 400
        conn = get_db_connection()
        cursor = conn.cursor()
        # last_seen 갱신
        try:
            cursor.execute(
                "UPDATE users SET last_seen = %s WHERE username = %s",
                (time.time(), username),
            )
            conn.commit()
        except Exception:
            pass
        cursor.execute(
            """
            SELECT COALESCE(cash,0), COALESCE(game_cash,0), COALESCE(total_rolling,0),
                   COALESCE(initial_withdraw,0), COALESCE(current_rolling,0),
                   COALESCE(display_name,''), COALESCE(total_profit,0),
                   COALESCE(last_attendance,''), COALESCE(suspended, FALSE)
            FROM users WHERE username=%s
            """,
            (username,),
        )
        row = cursor.fetchone()
        cursor.close(); release_db(conn)
        if not row:
            return jsonify({"error": "없음"}), 404
        return jsonify({
            "username": username,
            "cash": int(row[0] or 0),
            "game_cash": int(row[1] or 0),
            "total_rolling": int(row[2] or 0),
            "initial_withdraw": int(row[3] or 0),
            "current_rolling": int(row[4] or 0),
            "display_name": row[5] or "",
            "name": row[5] or username,
            "total_profit": int(row[6] or 0),
            "last_attendance": row[7] or "",
            "suspended": bool(row[8]),
            "tier": get_tier(row[2] or 0),
            "is_admin": username == ADMIN_USER,
        })
    except Exception as e:
        print(f"balance error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/ranking", methods=["GET"])
def get_ranking():
    """랭킹용 유저 목록 — 티어/시즌/총자산 공통 데이터"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            ensure_user_columns(cursor, conn)
        except Exception:
            pass
        try:
            cursor.execute(
                """
                SELECT username, COALESCE(cash,0), COALESCE(game_cash,0), COALESCE(total_rolling,0),
                       COALESCE(display_name,''), COALESCE(total_profit,0), COALESCE(last_seen,0),
                       COALESCE(season_points,0)
                FROM users
                """
            )
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            cursor.execute(
                """
                SELECT username, COALESCE(cash,0), COALESCE(game_cash,0), COALESCE(total_rolling,0),
                       COALESCE(display_name,''), COALESCE(total_profit,0), COALESCE(last_seen,0), 0
                FROM users
                """
            )
        rows = cursor.fetchall()
        cursor.close(); release_db(conn)
        now = time.time()
        users = []
        for r in rows:
            ls = float(r[6] or 0)
            cash = int(r[1] or 0)
            game = int(r[2] or 0)
            users.append({
                "username": r[0],
                "cash": cash,
                "game_cash": game,
                "total_assets": cash + game,
                "total_rolling": int(r[3] or 0),
                "display_name": r[4] or r[0],
                "name": r[4] or r[0],
                "total_profit": int(r[5] or 0),
                "last_seen": ls,
                "online": (now - ls) < 90 if ls else False,
                "season_points": int(r[7] or 0),
                "tier": get_tier(r[3] or 0),
            })
        return jsonify(users)
    except Exception as e:
        print(f"ranking error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/register", methods=["POST"])
def register():
    conn = None
    cursor = None
    try:
        data = request.get_json(silent=True) or {}
        if not data and request.data:
            try:
                import json as _json
                data = _json.loads(request.data.decode("utf-8") or "{}")
            except Exception:
                data = {}
        username = (data.get("username") or "").strip()
        password = (data.get("password") or "").strip()
        display_name = (
            data.get("display_name")
            or data.get("name")
            or ""
        ).strip()

        if not username or not password:
            return jsonify({"error": "아이디와 비밀번호를 모두 입력해주세요."}), 400
        if not display_name:
            return jsonify({"error": "이름을 입력해주세요."}), 400
        if len(username) < 3 or len(username) > 20:
            return jsonify({"error": "아이디는 3~20자로 입력해주세요."}), 400
        if len(password) < 4:
            return jsonify({"error": "비밀번호는 4자 이상 입력해주세요."}), 400
        if len(display_name) > 20:
            return jsonify({"error": "이름은 20자 이내로 입력해주세요."}), 400

        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            ensure_user_columns(cursor, conn)
        except Exception as ee:
            print(f"register ensure cols: {ee}")
            try:
                conn.rollback()
            except Exception:
                pass

        cursor.execute("SELECT username FROM users WHERE username = %s", (username,))
        if cursor.fetchone():
            cursor.close()
            release_db(conn)
            return jsonify({"error": "이미 존재하는 아이디입니다."}), 400

        pw_hash = hash_password(password)
        try:
            cursor.execute(
                """
                INSERT INTO users (
                    username, password, cash, game_cash, total_rolling,
                    initial_withdraw, current_rolling, profit_rate, last_attendance,
                    display_name, total_profit, last_seen
                )
                VALUES (%s, %s, 1000000, 0, 0, 0, 0, 0.0, '', %s, 0, %s)
                """,
                (username, pw_hash, display_name, time.time()),
            )
        except Exception as ins_err:
            # last_seen 없는 구형 스키마 호환
            print(f"register insert fallback: {ins_err}")
            try:
                conn.rollback()
            except Exception:
                pass
            cursor.execute(
                """
                INSERT INTO users (
                    username, password, cash, game_cash, total_rolling,
                    initial_withdraw, current_rolling, profit_rate, last_attendance,
                    display_name, total_profit
                )
                VALUES (%s, %s, 1000000, 0, 0, 0, 0, 0.0, '', %s, 0)
                """,
                (username, pw_hash, display_name),
            )
        conn.commit()
        cursor.close()
        release_db(conn)
        conn = None
        return jsonify({"success": True, "message": "회원가입이 완료되었습니다.", "cash": 1000000})
    except Exception as e:
        print(f"Register error: {e}")
        try:
            if cursor: cursor.close()
        except Exception:
            pass
        try:
            if conn: release_db(conn)
        except Exception:
            pass
        return jsonify({"error": "회원가입 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."}), 500


@app.route("/api/login", methods=["POST"])
def login():
    try:
        data = request.json or {}
        username = data.get("username", "").strip()
        password = data.get("password", "").strip()

        if not username or not password:
            return jsonify({"error": "아이디와 비밀번호를 모두 입력해주세요."}), 400

        conn = get_db_connection()
        cursor = conn.cursor()
        ensure_user_columns(cursor, conn)
        row = None
        try:
            cursor.execute(
                """
                SELECT password, cash, game_cash, total_rolling, initial_withdraw,
                       current_rolling, profit_rate, last_attendance,
                       COALESCE(display_name, ''), COALESCE(total_profit, 0),
                       COALESCE(suspended, FALSE)
                FROM users WHERE username = %s
                """,
                (username,),
            )
            row = cursor.fetchone()
        except Exception as sel_e:
            print(f"Login select fallback: {sel_e}")
            try:
                conn.rollback()
            except Exception:
                pass
            cursor.execute(
                """
                SELECT password, cash, game_cash, total_rolling, initial_withdraw,
                       current_rolling, profit_rate, last_attendance
                FROM users WHERE username = %s
                """,
                (username,),
            )
            row = cursor.fetchone()
            if row:
                row = tuple(row) + ("", 0, False)

        if not row:
            cursor.close()
            release_db(conn)
            return jsonify({"error": "존재하지 않는 아이디입니다."}), 400

        vals = list(row) + [None] * 11
        db_password = vals[0]
        cash = vals[1]
        game_cash = vals[2]
        total_rolling = vals[3]
        initial_withdraw = vals[4]
        current_rolling = vals[5]
        profit_rate = vals[6]
        last_attendance = vals[7]
        display_name = vals[8] if vals[8] is not None else ""
        total_profit = vals[9] if vals[9] is not None else 0
        suspended = bool(vals[10]) if vals[10] is not None else False

        if not verify_password(password, db_password or ""):
            cursor.close()
            release_db(conn)
            return jsonify({"error": "비밀번호가 일치하지 않습니다."}), 400

        # 평문이면 해시로 승격
        if not str(db_password or "").startswith("pbkdf2:"):
            try:
                cursor.execute(
                    "UPDATE users SET password = %s WHERE username = %s",
                    (hash_password(password), username),
                )
                conn.commit()
            except Exception as e:
                print(f"pw migrate: {e}")

        # last_seen 갱신
        try:
            cursor.execute(
                "UPDATE users SET last_seen = %s WHERE username = %s",
                (time.time(), username),
            )
            conn.commit()
        except Exception:
            pass

        cursor.close()
        release_db(conn)
        return jsonify({
            "username": username,
            "display_name": display_name or username,
            "name": display_name or username,
            "cash": cash,
            "game_cash": game_cash,
            "total_rolling": total_rolling,
            "initial_withdraw": initial_withdraw,
            "current_rolling": current_rolling,
            "profit_rate": profit_rate,
            "last_attendance": last_attendance,
            "total_profit": total_profit,
            "tier": get_tier(total_rolling),
            "suspended": bool(suspended),
            "token": issue_token(username),
            "is_admin": username == ADMIN_USER,
        })
    except Exception as e:
        print(f"Login error: {e}")
        return jsonify({"error": f"서버 통신 오류: {str(e)}"}), 500


@app.route("/api/update", methods=["POST"])
def update_user():
    """보유금/롤링 갱신. 은행은 출금·마감 패턴 또는 증가만 허용(송금 입금 보호)."""
    try:
        data = request.get_json(silent=True) or {}
        if not data and request.data:
            try:
                import json as _json
                data = _json.loads(request.data.decode("utf-8") or "{}")
            except Exception:
                data = {}
        username = (data.get("username") or "").strip()
        if not username:
            return jsonify({"error": "username 필요"}), 400

        cash = data.get("cash")
        game_cash = data.get("game_cash")
        rolling_add = data.get("rolling_add", 0)
        initial_withdraw = data.get("initial_withdraw")
        current_rolling = data.get("current_rolling")
        profit_rate = data.get("profit_rate")
        last_attendance = data.get("last_attendance")
        total_profit = data.get("total_profit")
        display_name = data.get("display_name")

        try:
            rolling_add = int(rolling_add or 0)
            if rolling_add < 0:
                rolling_add = 0
            if rolling_add > 100_000_000:
                rolling_add = 100_000_000
        except (TypeError, ValueError):
            rolling_add = 0
        try:
            cash = int(cash) if cash is not None else None
        except (TypeError, ValueError):
            cash = None
        try:
            game_cash = int(game_cash) if game_cash is not None else None
        except (TypeError, ValueError):
            game_cash = None
        if game_cash is not None and game_cash < 0:
            game_cash = 0
        if cash is not None and cash < 0:
            cash = 0

        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            ensure_user_columns(cursor, conn)
        except Exception:
            pass
        cursor.execute(
            "SELECT COALESCE(cash, 0), COALESCE(game_cash, 0) FROM users WHERE username = %s FOR UPDATE",
            (username,),
        )
        row = cursor.fetchone()
        if not row:
            cursor.close(); release_db(conn)
            return jsonify({"error": "유저 없음"}), 400
        db_cash = int(row[0] or 0)
        db_game = int(row[1] or 0)
        final_cash = db_cash
        # 게임머니: 클라이언트 값을 기본 사용하되, 비정상 음수 방지
        if game_cash is None:
            final_game = db_game
        else:
            final_game = max(0, int(game_cash))

        if cash is not None:
            c = int(cash)
            g = final_game
            bank_delta = c - db_cash
            game_delta = g - db_game
            if bank_delta == 0:
                final_cash = db_cash
            elif bank_delta < 0 and game_delta >= (-bank_delta) - 1:
                final_cash = c  # 출금
            elif bank_delta > 0 and game_delta <= (-bank_delta) + 1:
                final_cash = c  # 마감
            elif bank_delta > 0:
                final_cash = c  # 관리자 입금 등 증가 허용
            else:
                final_cash = db_cash  # 송금 입금 덮어쓰기 방지

        cursor.execute(
            """
            UPDATE users
            SET cash = %s,
                game_cash = %s,
                total_rolling = total_rolling + %s,
                initial_withdraw = COALESCE(%s, initial_withdraw),
                current_rolling = COALESCE(%s, current_rolling),
                profit_rate = COALESCE(%s, profit_rate),
                last_attendance = COALESCE(%s, last_attendance),
                total_profit = COALESCE(%s, total_profit),
                display_name = COALESCE(NULLIF(%s, ''), display_name),
                last_seen = %s
            WHERE username = %s
            """,
            (
                final_cash,
                final_game,
                rolling_add,
                initial_withdraw,
                current_rolling,
                profit_rate,
                last_attendance,
                total_profit,
                display_name,
                time.time(),
                username,
            ),
        )
        conn.commit()
        cursor.close(); release_db(conn)
        return jsonify({"success": True, "cash": final_cash, "game_cash": final_game})
    except Exception as e:
        print(f"Update error: {e}")
        return jsonify({"error": f"서버 통신 오류: {str(e)}"}), 500


@app.route("/api/me", methods=["GET"])
def api_me():
    try:
        auth_user, err = require_user()
        if err:
            return err
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT COALESCE(cash,0), COALESCE(game_cash,0), COALESCE(total_rolling,0),
                   COALESCE(initial_withdraw,0), COALESCE(current_rolling,0),
                   COALESCE(display_name,''), COALESCE(total_profit,0),
                   COALESCE(last_attendance,''), COALESCE(suspended, FALSE)
            FROM users WHERE username = %s
            """,
            (auth_user,),
        )
        row = cursor.fetchone()
        cursor.close(); release_db(conn)
        if not row:
            return jsonify({"error": "유저 없음"}), 400
        return jsonify({
            "username": auth_user,
            "cash": int(row[0] or 0),
            "game_cash": int(row[1] or 0),
            "total_rolling": int(row[2] or 0),
            "initial_withdraw": int(row[3] or 0),
            "current_rolling": int(row[4] or 0),
            "display_name": row[5] or "",
            "total_profit": int(row[6] or 0),
            "last_attendance": row[7] or "",
            "suspended": bool(row[8]),
            "tier": get_tier(row[2] or 0),
            "is_admin": auth_user == ADMIN_USER,
        })
    except Exception as e:
        print(f"me error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/withdraw", methods=["POST"])
def api_withdraw():
    """은행 → 게임 보유금"""
    try:
        data = request.get_json(silent=True) or {}
        auth_user, err = require_user()
        auth_user = auth_user or (data.get("username") or "").strip()
        if not auth_user:
            return jsonify({"error": "username 필요"}), 400
        try:
            amount = int(data.get("amount", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "금액 오류"}), 400
        if amount <= 0:
            return jsonify({"error": "1원 이상 출금"}), 400
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COALESCE(cash,0), COALESCE(game_cash,0), COALESCE(initial_withdraw,0) FROM users WHERE username=%s FOR UPDATE",
            (auth_user,),
        )
        row = cursor.fetchone()
        if not row:
            cursor.close(); release_db(conn)
            return jsonify({"error": "유저 없음"}), 400
        bank, game, init_w = int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)
        if bank < amount:
            cursor.close(); release_db(conn)
            return jsonify({"error": "은행 잔고 부족"}), 400
        bank2, game2 = bank - amount, game + amount
        init2 = init_w + amount
        cursor.execute(
            "UPDATE users SET cash=%s, game_cash=%s, initial_withdraw=%s, last_seen=%s WHERE username=%s",
            (bank2, game2, init2, time.time(), auth_user),
        )
        log_transaction(cursor, auth_user, "출금", -amount, bank2, "은행→보유")
        conn.commit()
        cursor.close(); release_db(conn)
        return jsonify({"success": True, "cash": bank2, "game_cash": game2, "initial_withdraw": init2})
    except Exception as e:
        print(f"withdraw error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/close_session", methods=["POST"])
def api_close_session():
    """게임 보유금 → 은행 마감"""
    try:
        data = request.get_json(silent=True) or {}
        auth_user, err = require_user()
        auth_user = auth_user or (data.get("username") or "").strip()
        if not auth_user:
            return jsonify({"error": "username 필요"}), 400
        # 클라이언트가 보낸 롤링 체크는 참고용, 서버 잔액 기준
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT COALESCE(cash,0), COALESCE(game_cash,0), COALESCE(initial_withdraw,0),
                   COALESCE(current_rolling,0), COALESCE(total_profit,0), COALESCE(total_rolling,0)
            FROM users WHERE username=%s FOR UPDATE
            """,
            (auth_user,),
        )
        row = cursor.fetchone()
        if not row:
            cursor.close(); release_db(conn)
            return jsonify({"error": "유저 없음"}), 400
        bank, game, init_w, cur_roll, tot_profit, tot_roll = [int(x or 0) for x in row]
        if game <= 0:
            cursor.close(); release_db(conn)
            return jsonify({"error": "마감할 보유금이 없습니다."}), 400
        # 티어 롤링 비율
        tier = get_tier(tot_roll)
        ratio = float(tier.get("roll_ratio") or 1.0)
        need = int((init_w or 0) * ratio + 0.999999) if init_w else 0
        # 클라이언트가 current_rolling을 보내면 더 큰 쪽 사용(조작 완화: 서버 값 우선하되 서버가 낮을 수 있음)
        client_roll = data.get("current_rolling")
        try:
            client_roll = int(client_roll) if client_roll is not None else cur_roll
        except (TypeError, ValueError):
            client_roll = cur_roll
        use_roll = max(cur_roll, min(client_roll, cur_roll + 50_000_000))
        if need > 0 and use_roll < need:
            cursor.close(); release_db(conn)
            return jsonify({"error": f"롤링 미달 (₩{use_roll:,} / ₩{need:,})"}), 400
        session_profit = game - init_w
        bank2 = bank + game
        tot_profit2 = tot_profit + session_profit
        # 시즌 포인트: 효율(수익률) + 수익(상한) — 대자본 절대액 편향 완화
        season_add, season_detail = calc_season_points(init_w, game)
        try:
            cursor.execute(
                """
                UPDATE users SET cash=%s, game_cash=0, initial_withdraw=0, current_rolling=0, total_profit=%s,
                    season_points = COALESCE(season_points,0) + %s, last_seen=%s
                WHERE username=%s
                """,
                (bank2, tot_profit2, season_add, time.time(), auth_user),
            )
        except Exception:
            cursor.execute(
                """
                UPDATE users SET cash=%s, game_cash=0, initial_withdraw=0, current_rolling=0, total_profit=%s, last_seen=%s
                WHERE username=%s
                """,
                (bank2, tot_profit2, time.time(), auth_user),
            )
            season_add = 0
            season_detail = {"efficiency": 0, "profit_pts": 0, "profit": int(session_profit), "roi": 0}
        log_transaction(
            cursor, auth_user, "마감", game, bank2,
            f"세션수익 {session_profit} · 시즌+{season_add} (효율{season_detail.get('efficiency',0)}/수익{season_detail.get('profit_pts',0)})"
        )
        conn.commit()
        # 갱신된 시즌 포인트 조회
        try:
            cursor.execute("SELECT COALESCE(season_points,0) FROM users WHERE username=%s", (auth_user,))
            sp_row = cursor.fetchone()
            season_total = int(sp_row[0] or 0) if sp_row else season_add
        except Exception:
            season_total = season_add
        cursor.close(); release_db(conn)
        return jsonify({
            "success": True,
            "cash": bank2,
            "game_cash": 0,
            "initial_withdraw": 0,
            "current_rolling": 0,
            "total_profit": tot_profit2,
            "session_profit": session_profit,
            "season_points_gained": season_add,
            "season_points": season_total,
            "season_detail": season_detail,
        })
    except Exception as e:
        print(f"close error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/users", methods=["GET"])
def admin_get_users():
    try:
        auth_admin, err = require_admin()
        if err:
            return err

        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS suspended BOOLEAN DEFAULT FALSE"
            )
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
        try:
            cursor.execute("""
                SELECT username, cash, game_cash, total_rolling, initial_withdraw,
                       current_rolling, profit_rate,
                       COALESCE(display_name, ''), COALESCE(total_profit, 0),
                       COALESCE(last_seen, 0), COALESCE(suspended, FALSE)
                FROM users
            """)
        except Exception:
            cursor.execute("""
                SELECT username, cash, game_cash, total_rolling, initial_withdraw,
                       current_rolling, profit_rate,
                       COALESCE(display_name, ''), COALESCE(total_profit, 0),
                       COALESCE(last_seen, 0), FALSE
                FROM users
            """)
        rows = cursor.fetchall()
        cursor.close()
        release_db(conn)
        now = time.time()
        users = []
        for r in rows:
            ls = float(r[9] or 0)
            users.append({
                "username": r[0],
                "cash": r[1],
                "game_cash": r[2],
                "total_rolling": r[3],
                "initial_withdraw": r[4],
                "current_rolling": r[5],
                "profit_rate": r[6],
                "display_name": r[7] or r[0],
                "name": r[7] or r[0],
                "total_profit": r[8],
                "last_seen": ls,
                "online": (now - ls) < 90 if ls else False,
                "tier": get_tier(r[3] if len(r) > 3 else 0),
                "suspended": bool(r[10]) if len(r) > 10 else False,
            })
        return jsonify(users)
    except Exception as e:
        print(f"Admin users error: {e}")
        return jsonify({"error": f"서버 통신 오류: {str(e)}"}), 500


@app.route("/api/admin/edit", methods=["POST"])
def admin_edit_user():
    """cash / game_cash 모두 설정 가능. 프론트에서 계산된 최종값을 보냄."""
    try:
        auth_admin, err = require_admin()
        if err:
            return err
        data = request.json or {}
        username = data.get("username")
        new_cash = data.get("cash")
        new_game_cash = data.get("game_cash", None)
        if not username:
            return jsonify({"error": "username required"}), 400

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT cash, COALESCE(game_cash, 0) FROM users WHERE username = %s",
            (username,),
        )
        prev = cursor.fetchone()
        prev_cash = int(prev[0] or 0) if prev else 0
        prev_game = int(prev[1] or 0) if prev else 0
        final_cash = max(0, int(new_cash or 0))
        if new_game_cash is not None:
            final_game = max(0, int(new_game_cash))
            cursor.execute(
                "UPDATE users SET cash = %s, game_cash = %s, last_seen = %s WHERE username = %s",
                (final_cash, final_game, time.time(), username),
            )
        else:
            final_game = prev_game
            cursor.execute(
                "UPDATE users SET cash = %s, last_seen = %s WHERE username = %s",
                (final_cash, time.time(), username),
            )
        d_cash = final_cash - prev_cash
        d_game = final_game - prev_game
        if d_cash != 0:
            kind = "관리자입금" if d_cash > 0 else "관리자출금"
            log_transaction(cursor, username, kind, d_cash, final_cash, "관리자 은행 잔고 조정")
        if d_game != 0:
            kind = "게임입금" if d_game > 0 else "게임출금"
            log_transaction(cursor, username, kind, d_game, final_game, "관리자 게임머니 조정")
        conn.commit()
        cursor.execute(
            "SELECT cash, game_cash FROM users WHERE username = %s", (username,)
        )
        row = cursor.fetchone()
        cursor.close()
        release_db(conn)
        return jsonify({
            "success": True,
            "cash": row[0] if row else 0,
            "game_cash": row[1] if row else 0,
        })
    except Exception as e:
        print(f"Admin edit error: {e}")
        return jsonify({"error": f"서버 통신 오류: {str(e)}"}), 500


@app.route("/api/admin/delete", methods=["POST"])
def admin_delete_user():
    try:
        auth_admin, err = require_admin()
        if err:
            return err
        data = request.json or {}
        username = data.get("username")
        if not username:
            return jsonify({"error": "username required"}), 400
        if username == ADMIN_USER:
            return jsonify({"error": "관리자 계정은 삭제할 수 없습니다."}), 400

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM users WHERE username = %s", (username,))
        conn.commit()
        cursor.close()
        release_db(conn)
        return jsonify({"success": True})
    except Exception as e:
        print(f"Admin delete error: {e}")
        return jsonify({"error": f"서버 통신 오류: {str(e)}"}), 500


@app.route("/api/admin/notice", methods=["POST"])
def admin_send_notice():
    """관리자 공지 발송 → 전체 유저가 /api/notice 로 수신"""
    global CURRENT_NOTICE
    try:
        auth_admin, err = require_admin()
        if err:
            return err
        data = request.json or {}

        message = (data.get("message") or "").strip()
        if not message:
            return jsonify({"error": "공지 내용이 비어 있습니다."}), 400

        CURRENT_NOTICE = {
            "id": f"n_{int(time.time() * 1000)}",
            "message": message,
            "created_at": int(time.time()),
        }
        return jsonify({"success": True, "id": CURRENT_NOTICE["id"]})
    except Exception as e:
        print(f"Admin notice error: {e}")
        return jsonify({"error": f"서버 통신 오류: {str(e)}"}), 500


@app.route("/api/notice", methods=["GET"])


@app.route("/api/patch", methods=["GET"])
def get_patch_notes():
    """현재 패치 노트 (전체 유저)"""
    if not CURRENT_PATCH.get("id"):
        return jsonify({"id": "", "title": "", "date": "", "body": ""})
    return jsonify({
        "id": CURRENT_PATCH.get("id") or "",
        "title": CURRENT_PATCH.get("title") or "패치 노트",
        "date": CURRENT_PATCH.get("date") or "",
        "body": CURRENT_PATCH.get("body") or "",
        "created_at": CURRENT_PATCH.get("created_at") or 0,
    })


@app.route("/api/admin/patch", methods=["POST"])
def admin_set_patch():
    """관리자 패치 노트 작성/배포 → 전체 유저 팝업"""
    global CURRENT_PATCH, CURRENT_NOTICE
    try:
        auth_admin, err = require_admin()
        if err:
            return err
        data = request.get_json(silent=True) or {}
        title = (data.get("title") or "패치 노트").strip()[:80]
        body = (data.get("body") or "").strip()[:4000]
        date = (data.get("date") or time.strftime("%Y.%m.%d")).strip()[:20]
        if not body:
            return jsonify({"error": "패치 내용을 입력하세요."}), 400
        pid = f"patch_{int(time.time() * 1000)}"
        CURRENT_PATCH = {
            "id": pid,
            "title": title,
            "date": date,
            "body": body,
            "created_at": time.time(),
        }
        # 중요 공지로도 한 줄 안내
        CURRENT_NOTICE = {
            "id": f"notice_{pid}",
            "message": f"📢 [패치] {title}\n\n새 패치 노트가 등록되었습니다.\n메뉴에서 패치 노트를 확인하세요.",
            "created_at": time.time(),
            "important": True,
        }
        return jsonify({"success": True, "id": pid, "patch": CURRENT_PATCH})
    except Exception as e:
        print(f"admin patch: {e}")
        return jsonify({"error": str(e)}), 500


def get_notice():
    """최신 공지 조회 (프론트 15초 폴링)"""
    if not CURRENT_NOTICE.get("id"):
        return jsonify({"id": "", "message": ""})
    return jsonify({
        "id": CURRENT_NOTICE["id"],
        "message": CURRENT_NOTICE["message"],
        "created_at": CURRENT_NOTICE.get("created_at", 0),
        "important": bool(CURRENT_NOTICE.get("important")),
    })


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/delete_account", methods=["POST"])
def delete_account():
    try:
        data = request.json or {}
        username = (data.get("username") or "").strip()
        password = (data.get("password") or "").strip()
        if not username or not password:
            return jsonify({"error": "아이디와 비밀번호를 입력해주세요."}), 400
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT password FROM users WHERE username = %s", (username,))
        row = cursor.fetchone()
        if not row:
            cursor.close(); release_db(conn)
            return jsonify({"error": "존재하지 않는 계정입니다."}), 400
        if not verify_password(password, row[0]):
            cursor.close(); release_db(conn)
            return jsonify({"error": "비밀번호가 일치하지 않습니다."}), 400
        cursor.execute("DELETE FROM users WHERE username = %s", (username,))
        conn.commit()
        cursor.close(); release_db(conn)
        return jsonify({"success": True})
    except Exception as e:
        print(f"Delete account error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/reset_ranking", methods=["POST"])
def admin_reset_ranking():
    try:
        auth_admin, err = require_admin()
        if err:
            return err
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET total_profit = 0, total_rolling = 0")
        conn.commit()
        cursor.close(); release_db(conn)
        return jsonify({"success": True})
    except Exception as e:
        print(f"Reset ranking error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/transfer", methods=["POST"])
def transfer_money():
    """유저 간 송금 (은행 우선, 부족분 보유금). 수수료 → 관리자."""
    try:
        data = request.get_json(silent=True)
        if not data:
            try:
                import json as _json
                data = _json.loads(request.data.decode("utf-8") or "{}")
            except Exception:
                data = {}
        from_user = (data.get("from_user") or data.get("username") or "").strip()
        to_user = (data.get("to_user") or "").strip()
        password = data.get("password") or ""
        try:
            amount = int(data.get("amount") or 0)
        except (TypeError, ValueError):
            return jsonify({"error": "금액을 확인해주세요."}), 400

        if not from_user or not to_user:
            return jsonify({"error": "보내는/받는 사람을 입력해주세요."}), 400
        if from_user == to_user:
            return jsonify({"error": "본인에게는 송금할 수 없습니다."}), 400
        if amount <= 0:
            return jsonify({"error": "1원 이상 송금해주세요."}), 400
        if not password:
            return jsonify({"error": "비밀번호를 입력해주세요."}), 400

        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT password, COALESCE(cash, 0), COALESCE(game_cash, 0), COALESCE(total_rolling, 0)
                FROM users WHERE username = %s
                """,
                (from_user,),
            )
            row = cursor.fetchone()
            if not row:
                return jsonify({"error": "보내는 계정이 없습니다."}), 400
            if not verify_password(password, row[0] or ""):
                return jsonify({"error": "비밀번호가 일치하지 않습니다."}), 400

            from_bank = int(row[1] or 0)
            from_game = int(row[2] or 0)
            tier = get_tier(row[3] if len(row) > 3 else 0) or get_tier(0)
            fee_rate = float(tier.get("fee_rate") or 0.05)
            fee = max(100, int(amount * fee_rate))
            total_need = amount + fee
            if from_bank + from_game < total_need:
                return jsonify({
                    "error": f"잔고 부족 (필요 ₩{total_need:,} / 은행 ₩{from_bank:,} + 보유 ₩{from_game:,})"
                }), 400

            # 받는 사람: 아이디 → 이름
            cursor.execute("SELECT username FROM users WHERE username = %s", (to_user,))
            rt = cursor.fetchone()
            if rt:
                to_user = rt[0]
            else:
                cursor.execute(
                    "SELECT username FROM users WHERE TRIM(COALESCE(display_name,'')) = %s",
                    (to_user,),
                )
                matches = cursor.fetchall()
                if not matches:
                    cursor.execute(
                        "SELECT username FROM users WHERE LOWER(TRIM(COALESCE(display_name,''))) = LOWER(%s)",
                        (to_user,),
                    )
                    matches = cursor.fetchall()
                if len(matches) == 1:
                    to_user = matches[0][0]
                elif len(matches) > 1:
                    return jsonify({"error": "같은 이름이 여러 명입니다. 아이디로 송금하세요."}), 400
                else:
                    return jsonify({"error": "받는 사람(아이디/이름)을 찾을 수 없습니다."}), 400

            take_bank = min(from_bank, total_need)
            take_game = total_need - take_bank

            cursor.execute(
                "UPDATE users SET cash = COALESCE(cash,0) - %s, game_cash = COALESCE(game_cash,0) - %s, last_seen = %s WHERE username = %s",
                (take_bank, take_game, time.time(), from_user),
            )
            cursor.execute(
                "UPDATE users SET cash = COALESCE(cash,0) + %s, last_seen = %s WHERE username = %s",
                (amount, time.time(), to_user),
            )
            # 수수료 → 관리자
            cursor.execute("SELECT username FROM users WHERE username = %s", (ADMIN_USER,))
            if cursor.fetchone():
                cursor.execute(
                    "UPDATE users SET cash = COALESCE(cash,0) + %s WHERE username = %s",
                    (fee, ADMIN_USER),
                )

            cursor.execute("SELECT COALESCE(cash,0) FROM users WHERE username = %s", (from_user,))
            from_bal = int((cursor.fetchone() or [0])[0] or 0)
            cursor.execute("SELECT COALESCE(cash,0) FROM users WHERE username = %s", (to_user,))
            to_bal = int((cursor.fetchone() or [0])[0] or 0)

            try:
                log_transaction(cursor, from_user, "송금출금", -amount, from_bal, f"{to_user}에게 송금")
                if fee:
                    log_transaction(cursor, from_user, "송금수수료", -fee, from_bal, "송금 수수료")
                log_transaction(cursor, to_user, "송금입금", amount, to_bal, f"{from_user}에게서 받음")
            except Exception as le:
                print(f"transfer log skip: {le}")

            conn.commit()
            cursor.execute(
                "SELECT COALESCE(cash,0), COALESCE(game_cash,0) FROM users WHERE username = %s",
                (from_user,),
            )
            me = cursor.fetchone()
            return jsonify({
                "success": True,
                "cash": int(me[0] or 0) if me else 0,
                "game_cash": int(me[1] or 0) if me else 0,
                "fee": fee,
                "sent": amount,
                "to_user": to_user,
                "message": f"{to_user}님에게 ₩{amount:,} 송금 완료 (수수료 ₩{fee:,})",
            })
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"Transfer inner error: {e}")
            return jsonify({"error": f"송금 처리 오류: {str(e)}"}), 500
        finally:
            try:
                cursor.close()
            except Exception:
                pass
            release_db(conn)
    except Exception as e:
        print(f"Transfer error: {e}")
        return jsonify({"error": f"서버 오류: {str(e)}"}), 500


@app.route("/api/house_collect", methods=["POST"])
def house_collect():
    """하우 손익 — 유저 손실(amount>0)은 관리자 은행으로, 당첨(amount<0)은 관리자에서 차감"""
    try:
        data = request.get_json(silent=True) or {}
        if not data and request.data:
            try:
                import json as _json
                data = _json.loads(request.data.decode("utf-8") or "{}")
            except Exception:
                data = {}
        auth_user, _err = require_user()
        auth_user = auth_user or (data.get("username") or data.get("from_user") or "system")
        try:
            amount = int(data.get("amount", 0))
        except (TypeError, ValueError):
            amount = 0
        if amount == 0:
            return jsonify({"success": True, "skipped": True})
        # 1회 최대 500억 (시뮬용)
        if abs(amount) > 50_000_000_000:
            return jsonify({"error": "한도 초과"}), 400
        memo = (data.get("memo") or "").strip()[:200]
        if auth_user and auth_user not in memo and auth_user != ADMIN_USER:
            memo = f"{memo} · {auth_user}".strip(" ·")

        conn = get_db_connection()
        cursor = conn.cursor()

        # 관리자 계정이 없으면 생성 시도 (안전장치)
        cursor.execute("SELECT COALESCE(cash,0) FROM users WHERE username=%s FOR UPDATE", (ADMIN_USER,))
        row = cursor.fetchone()
        if not row:
            try:
                cursor.execute(
                    """
                    INSERT INTO users (username, password, cash, game_cash, total_rolling,
                        initial_withdraw, current_rolling, profit_rate, last_attendance,
                        display_name, total_profit, last_seen)
                    VALUES (%s, %s, 0, 0, 0, 0, 0, 0.0, '', %s, 0, %s)
                    ON CONFLICT (username) DO NOTHING
                    """,
                    (ADMIN_USER, hash_password("abcd051410"), "관리자", time.time()),
                )
                conn.commit()
                cursor.execute("SELECT COALESCE(cash,0) FROM users WHERE username=%s FOR UPDATE", (ADMIN_USER,))
                row = cursor.fetchone()
            except Exception as ce:
                print(f"admin account ensure: {ce}")
                try:
                    conn.rollback()
                except Exception:
                    pass
                cursor.close(); release_db(conn)
                return jsonify({"error": "관리자 계좌 없음"}), 400

        admin_cash = int((row[0] if row else 0) or 0)
        # amount>0 유저 손실 → 관리자 +
        # amount<0 유저 당첨 → 관리자 계좌에서 차감 (마이너스 허용)
        new_admin = admin_cash + amount
        cursor.execute(
            "UPDATE users SET cash=%s WHERE username=%s",
            (int(new_admin), ADMIN_USER),
        )
        kind = "하우수입" if amount > 0 else "하우지급"
        log_transaction(cursor, ADMIN_USER, kind, amount, new_admin, memo or auth_user)
        conn.commit()
        cursor.close(); release_db(conn)

        # 난이도별 하우 수익 누적
        try:
            mode = DIFFICULTY_CONFIG.get("mode") or "normal"
            if mode in DIFFICULTY_STATS:
                DIFFICULTY_STATS[mode]["house_net"] = int(DIFFICULTY_STATS[mode].get("house_net", 0)) + int(amount)
        except Exception as se:
            print(f"difficulty stats skip: {se}")

        return jsonify({"success": True, "admin_cash": new_admin, "amount": amount})
    except Exception as e:
        print(f"House collect error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/messages/send", methods=["POST"])
def messages_send():
    try:
        auth_user, err = require_user()
        if err:
            return err
        data = request.json or {}
        from_user = (data.get("from_user") or auth_user or "").strip()
        to_user = (data.get("to_user") or "").strip()
        body = (data.get("body") or "").strip()
        if not from_user or not to_user or not body:
            return jsonify({"error": "받는 사람과 내용을 입력해주세요."}), 400
        if from_user == to_user:
            return jsonify({"error": "본인에게는 보낼 수 없습니다."}), 400
        if len(body) > 1000:
            return jsonify({"error": "메시지는 1000자 이내로 작성해주세요."}), 400
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT username FROM users WHERE username = %s", (to_user,))
        row_to = cursor.fetchone()
        if row_to:
            to_user = row_to[0]
        else:
            cursor.execute("SELECT username FROM users WHERE TRIM(COALESCE(display_name,'')) = %s", (to_user,))
            matches = cursor.fetchall()
            if len(matches) == 1:
                to_user = matches[0][0]
            elif len(matches) > 1:
                cursor.close(); release_db(conn)
                return jsonify({"error": "같은 이름이 여러 명입니다. 아이디로 보내주세요."}), 400
            else:
                cursor.close(); release_db(conn)
                return jsonify({"error": "받는 사람(아이디/이름)을 찾을 수 없습니다."}), 400
        cursor.execute(
            "INSERT INTO messages (from_user, to_user, body) VALUES (%s, %s, %s) RETURNING id, created_at",
            (from_user, to_user, body),
        )
        row = cursor.fetchone()
        conn.commit()
        cursor.close(); release_db(conn)
        return jsonify({
            "success": True,
            "id": row[0],
            "created_at": str(row[1]) if row else None,
        })
    except Exception as e:
        print(f"Msg send error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/messages/inbox", methods=["GET"])
def messages_inbox():
    """대화 상대 목록 + 미읽음 수"""
    try:
        username = (request.args.get("user") or "").strip()
        if not username:
            return jsonify({"error": "user required"}), 400
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                CASE WHEN from_user = %s THEN to_user ELSE from_user END AS peer,
                MAX(created_at) AS last_at,
                SUM(CASE WHEN to_user = %s AND is_read = FALSE THEN 1 ELSE 0 END) AS unread
            FROM messages
            WHERE from_user = %s OR to_user = %s
            GROUP BY peer
            ORDER BY last_at DESC
            LIMIT 50
            """,
            (username, username, username, username),
        )
        rows = cursor.fetchall()
        peers = []
        for peer, last_at, unread in rows:
            cursor.execute(
                "SELECT COALESCE(display_name, '') FROM users WHERE username = %s",
                (peer,),
            )
            nr = cursor.fetchone()
            cursor.execute("SELECT COALESCE(last_seen, 0) FROM users WHERE username = %s", (peer,))
            ls = cursor.fetchone()
            last_seen = float(ls[0]) if ls else 0
            peers.append({
                "username": peer,
                "display_name": (nr[0] if nr and nr[0] else peer),
                "last_at": str(last_at) if last_at else "",
                "unread": int(unread or 0),
                "last_seen": last_seen,
                "online": (time.time() - last_seen) < 90,
            })
        cursor.execute(
            "SELECT COUNT(*) FROM messages WHERE to_user = %s AND is_read = FALSE",
            (username,),
        )
        total_unread = cursor.fetchone()[0] or 0
        cursor.close(); release_db(conn)
        return jsonify({"peers": peers, "total_unread": int(total_unread)})
    except Exception as e:
        print(f"Inbox error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/messages/thread", methods=["GET"])
def messages_thread():
    try:
        me = (request.args.get("user") or "").strip()
        peer = (request.args.get("peer") or "").strip()
        if not me or not peer:
            return jsonify({"error": "user, peer required"}), 400
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, from_user, to_user, body, created_at, is_read
            FROM messages
            WHERE (from_user = %s AND to_user = %s) OR (from_user = %s AND to_user = %s)
            ORDER BY created_at ASC
            LIMIT 200
            """,
            (me, peer, peer, me),
        )
        rows = cursor.fetchall()
        # mark as read
        cursor.execute(
            """
            UPDATE messages SET is_read = TRUE
            WHERE to_user = %s AND from_user = %s AND is_read = FALSE
            """,
            (me, peer),
        )
        conn.commit()
        msgs = [
            {
                "id": r[0],
                "from_user": r[1],
                "to_user": r[2],
                "body": r[3],
                "created_at": str(r[4]) if r[4] else "",
                "is_read": bool(r[5]),
            }
            for r in rows
        ]
        cursor.close(); release_db(conn)
        return jsonify({"messages": msgs})
    except Exception as e:
        print(f"Thread error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/transactions", methods=["GET"])
def get_transactions():
    """유저 입출금 기록 (최신순, 최대 50건)"""
    try:
        username = (request.args.get("user") or "").strip()
        if not username:
            return jsonify({"error": "user required"}), 400
        limit = 50
        try:
            limit = min(50, max(1, int(request.args.get("limit", 50))))
        except (TypeError, ValueError):
            limit = 50
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT id, kind, amount, balance_after, memo, created_at
                FROM transactions
                WHERE username = %s
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
                (username, limit),
            )
            rows = cursor.fetchall()
        except Exception as qe:
            # 테이블 없으면 빈 목록 반환 (초기화 안 된 경우 대비)
            print(f"Transactions query fallback: {qe}")
            try:
                conn.rollback()
            except Exception:
                pass
            rows = []
        cursor.close(); release_db(conn)
        items = [
            {
                "id": r[0],
                "kind": r[1],
                "amount": r[2],
                "balance_after": r[3],
                "memo": r[4] or "",
                "created_at": str(r[5]) if r[5] else "",
            }
            for r in rows
        ]
        return jsonify({"items": items, "limit": limit})
    except Exception as e:
        print(f"Transactions error: {e}")
        return jsonify({"error": "입출금 기록을 불러올 수 없습니다. 잠시 후 다시 시도해주세요."}), 500


@app.route("/api/attendance", methods=["POST"])
def attendance_check():
    """매일 1회 출석 시 은행 잔고 +티어별 보상"""
    try:
        auth_user, err = require_user()
        if err:
            return err
        data = request.json or {}
        username = (data.get("username") or auth_user or "").strip()
        if not username:
            return jsonify({"error": "로그인이 필요합니다."}), 400

        # KST 날짜
        kst = timezone(timedelta(hours=9))
        today = datetime.now(kst).strftime("%Y-%m-%d")

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT cash, COALESCE(last_attendance, ''), COALESCE(total_rolling, 0) FROM users WHERE username = %s FOR UPDATE",
            (username,),
        )
        row = cursor.fetchone()
        if not row:
            cursor.close(); release_db(conn)
            return jsonify({"error": "계정을 찾을 수 없습니다."}), 400

        cash, last, rolling = int(row[0] or 0), (row[1] or ""), int(row[2] or 0)
        tier = get_tier(rolling)
        reward = int(tier["attend_reward"])
        if last == today:
            cursor.close(); release_db(conn)
            return jsonify({"error": "오늘은 이미 출석했습니다.", "already": True, "last_attendance": last, "tier": tier}), 400

        new_cash = cash + reward
        cursor.execute(
            "UPDATE users SET cash = %s, last_attendance = %s, last_seen = %s WHERE username = %s",
            (new_cash, today, time.time(), username),
        )
        log_transaction(cursor, username, "출석보상", reward, new_cash, f"{today} 출석체크")
        conn.commit()
        cursor.close(); release_db(conn)
        return jsonify({
            "success": True,
            "reward": reward,
            "cash": new_cash,
            "last_attendance": today,
            "tier": tier,
            "message": f"출석 완료! [{tier['badge']}{tier['name']}] ₩{reward:,} 지급",
        })
    except Exception as e:
        print(f"Attendance error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/bulk_pay", methods=["POST"])
def admin_bulk_pay():
    """모든 유저 은행 잔고에 동일 금액 지급"""
    try:
        auth_admin, err = require_admin()
        if err:
            return err
        data = request.json or {}
        try:
            amount = int(data.get("amount", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "금액을 확인해주세요."}), 400
        if amount == 0:
            return jsonify({"error": "0원은 지급할 수 없습니다."}), 400

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT username, cash FROM users")
        rows = cursor.fetchall()
        count = 0
        for username, cash in rows:
            new_cash = max(0, int(cash or 0) + amount)
            cursor.execute(
                "UPDATE users SET cash = %s WHERE username = %s",
                (new_cash, username),
            )
            kind = "전체지급" if amount > 0 else "전체차감"
            log_transaction(
                cursor, username, kind, amount, new_cash,
                f"관리자 전체 {'지급' if amount > 0 else '차감'}",
            )
            count += 1
        conn.commit()
        cursor.close(); release_db(conn)
        return jsonify({"success": True, "count": count, "amount": amount})
    except Exception as e:
        print(f"Bulk pay error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/force_reload", methods=["GET"])
def get_force_reload():
    return jsonify({"id": FORCE_RELOAD.get("id", 0), "message": FORCE_RELOAD.get("message") or ""})


@app.route("/api/admin/force_reload", methods=["POST"])
def admin_force_reload():
    global FORCE_RELOAD
    try:
        auth_admin, err = require_admin()
        if err:
            return err
        data = request.json or {}
        msg = (data.get("message") or "서버가 갱신되었습니다. 잠시 후 새로고침됩니다.").strip()
        FORCE_RELOAD = {"id": int(FORCE_RELOAD.get("id", 0)) + 1, "message": msg}
        return jsonify({"success": True, **FORCE_RELOAD})
    except Exception as e:
        print(f"Force reload error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/difficulty", methods=["GET"])
def get_difficulty():
    d = dict(DIFFICULTY_CONFIG)
    d["force_reload_id"] = FORCE_RELOAD.get("id", 0)
    d["force_reload_message"] = FORCE_RELOAD.get("message") or ""
    d["stats"] = {
        k: {"house_net": int(v.get("house_net", 0))}
        for k, v in DIFFICULTY_STATS.items()
    }
    return jsonify(d)


@app.route("/api/admin/difficulty", methods=["POST"])
def admin_set_difficulty():
    global DIFFICULTY_CONFIG
    try:
        auth_admin, err = require_admin()
        if err:
            return err
        data = request.json or {}
        mode = (data.get("mode") or "").strip().lower()
        if mode not in DIFFICULTY_PRESETS:
            return jsonify({"error": "mode must be easy|normal|hard"}), 400
        DIFFICULTY_CONFIG = dict(DIFFICULTY_PRESETS[mode])
        return jsonify({
            "success": True,
            **DIFFICULTY_CONFIG,
            "stats": {
                k: {"house_net": int(v.get("house_net", 0))}
                for k, v in DIFFICULTY_STATS.items()
            },
        })
    except Exception as e:
        print(f"Difficulty error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/rename", methods=["POST"])
def admin_rename():
    try:
        auth_admin, err = require_admin()
        if err:
            return err
        data = request.json or {}
        username = (data.get("username") or "").strip()
        new_name = (data.get("display_name") or data.get("name") or "").strip()
        if not username:
            return jsonify({"error": "username required"}), 400
        if not new_name:
            return jsonify({"error": "이름을 입력해주세요."}), 400
        if len(new_name) > 20:
            return jsonify({"error": "이름은 20자 이내"}), 400
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET display_name = %s, last_seen = %s WHERE username = %s",
            (new_name, time.time(), username),
        )
        if cursor.rowcount == 0:
            cursor.close(); release_db(conn)
            return jsonify({"error": "유저 없음"}), 400
        conn.commit()
        cursor.close(); release_db(conn)
        return jsonify({"success": True, "username": username, "display_name": new_name})
    except Exception as e:
        print(f"Admin rename error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/suspend", methods=["POST"])
def admin_suspend():
    """계정 일시정지 / 해제"""
    try:
        auth_admin, err = require_admin()
        if err:
            return err
        data = request.json or {}
        username = (data.get("username") or "").strip()
        suspend = bool(data.get("suspend", True))
        if not username:
            return jsonify({"error": "username required"}), 400
        if username == ADMIN_USER:
            return jsonify({"error": "관리자 계정은 정지할 수 없습니다."}), 400
        conn = get_db_connection()
        cursor = conn.cursor()
        # suspended 컬럼 없으면 생성 (기존 DB 호환)
        try:
            cursor.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS suspended BOOLEAN DEFAULT FALSE"
            )
            conn.commit()
        except Exception:
            try:
                cursor.execute(
                    "ALTER TABLE users ADD COLUMN suspended BOOLEAN DEFAULT FALSE"
                )
                conn.commit()
            except Exception as col_e:
                # 이미 있으면 무시
                conn.rollback()
                print(f"suspend col ensure: {col_e}")
        cursor.execute(
            "UPDATE users SET suspended = %s WHERE username = %s",
            (suspend, username),
        )
        if cursor.rowcount == 0:
            cursor.close(); release_db(conn)
            return jsonify({"error": "유저 없음"}), 400
        conn.commit()
        cursor.close(); release_db(conn)
        return jsonify({"success": True, "username": username, "suspended": suspend})
    except Exception as e:
        print(f"Suspend error: {e}")
        return jsonify({"error": f"일시정지 처리 실패: {str(e)}"}), 500


@app.route("/api/change_name", methods=["POST"])
def change_name():
    """이름 변경 — 일시정지 계정도 해제됨"""
    try:
        data = request.json or {}
        username = (data.get("username") or "").strip()
        password = (data.get("password") or "").strip()
        new_name = (data.get("display_name") or data.get("name") or "").strip()
        if not username or not password:
            return jsonify({"error": "아이디와 비밀번호를 입력해주세요."}), 400
        if not new_name or len(new_name) < 1:
            return jsonify({"error": "이름을 입력해주세요."}), 400
        if len(new_name) > 20:
            return jsonify({"error": "이름은 20자 이내로 입력해주세요."}), 400
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT password, COALESCE(display_name, '') FROM users WHERE username = %s",
            (username,),
        )
        row = cursor.fetchone()
        if not row:
            cursor.close(); release_db(conn)
            return jsonify({"error": "계정을 찾을 수 없습니다."}), 400
        if not verify_password(password, row[0] or ""):
            cursor.close(); release_db(conn)
            return jsonify({"error": "비밀번호가 일치하지 않습니다."}), 400
        old_name = row[1] or ""
        if new_name == old_name:
            cursor.close(); release_db(conn)
            return jsonify({"error": "기존과 다른 이름을 입력해주세요."}), 400
        cursor.execute(
            "UPDATE users SET display_name = %s, suspended = FALSE, last_seen = %s WHERE username = %s",
            (new_name, time.time(), username),
        )
        conn.commit()
        cursor.close(); release_db(conn)
        return jsonify({
            "success": True,
            "display_name": new_name,
            "suspended": False,
            "message": "이름이 변경되었습니다. 이용이 다시 가능합니다.",
        })
    except Exception as e:
        print(f"Change name error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/heartbeat", methods=["POST"])
def heartbeat():
    try:
        data = request.json or {}
        username = (data.get("username") or "").strip()
        if not username:
            return jsonify({"error": "username required"}), 400
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET last_seen = %s WHERE username = %s",
            (time.time(), username),
        )
        conn.commit()
        cursor.close(); release_db(conn)
        return jsonify({"success": True})
    except Exception as e:
        print(f"Heartbeat error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/messages/unread", methods=["GET"])
def messages_unread():
    try:
        username = (request.args.get("user") or "").strip()
        if not username:
            return jsonify({"count": 0})
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM messages WHERE to_user = %s AND is_read = FALSE",
            (username,),
        )
        count = cursor.fetchone()[0] or 0
        cursor.close(); release_db(conn)
        return jsonify({"count": int(count)})
    except Exception as e:
        return jsonify({"count": 0})


@app.route("/api/health", methods=["GET"])
def health():
    """서버 상태 체크"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchone()
        cursor.close(); release_db(conn)
        return jsonify({
            "status": "ok",
            "time": time.time(),
            "difficulty": DIFFICULTY_CONFIG.get("mode"),
            "force_reload_id": FORCE_RELOAD.get("id", 0),
            "notice_id": CURRENT_NOTICE.get("id") or "",
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500



@app.route("/favicon.ico")
def favicon():
    return "", 204



@app.route("/api/admin/set_user_assets", methods=["POST"])
def admin_set_user_assets():
    """특정 유저 총자산(은행+게임) 설정"""
    try:
        auth_admin, err = require_admin()
        if err:
            return err
        data = request.json or {}
        username = (data.get("username") or "").strip()
        if not username:
            return jsonify({"error": "username required"}), 400
        try:
            cash = int(data.get("cash")) if data.get("cash") is not None else None
            game_cash = int(data.get("game_cash")) if data.get("game_cash") is not None else None
        except (TypeError, ValueError):
            return jsonify({"error": "금액 오류"}), 400
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COALESCE(cash,0), COALESCE(game_cash,0) FROM users WHERE username=%s", (username,))
        row = cursor.fetchone()
        if not row:
            cursor.close(); release_db(conn)
            return jsonify({"error": "유저 없음"}), 400
        final_cash = max(0, cash) if cash is not None else int(row[0] or 0)
        final_game = max(0, game_cash) if game_cash is not None else int(row[1] or 0)
        cursor.execute(
            "UPDATE users SET cash=%s, game_cash=%s, last_seen=%s WHERE username=%s",
            (final_cash, final_game, time.time(), username),
        )
        d_cash = final_cash - int(row[0] or 0)
        d_game = final_game - int(row[1] or 0)
        if d_cash:
            log_transaction(cursor, username, "관리자입금" if d_cash > 0 else "관리자출금", d_cash, final_cash, "총자산 설정")
        if d_game:
            log_transaction(cursor, username, "게임입금" if d_game > 0 else "게임출금", d_game, final_game, "게임머니 설정")
        conn.commit()
        cursor.close(); release_db(conn)
        return jsonify({"success": True, "cash": final_cash, "game_cash": final_game, "total": final_cash + final_game})
    except Exception as e:
        print(f"set_user_assets error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/set_user_rolling", methods=["POST"])
def admin_set_user_rolling():
    """특정 유저 마감롤링/총롤링/초기출금 설정"""
    try:
        auth_admin, err = require_admin()
        if err:
            return err
        data = request.json or {}
        username = (data.get("username") or "").strip()
        if not username:
            return jsonify({"error": "username required"}), 400
        fields = {}
        for key in ("current_rolling", "total_rolling", "initial_withdraw"):
            if key in data and data[key] is not None:
                try:
                    fields[key] = max(0, int(data[key]))
                except (TypeError, ValueError):
                    return jsonify({"error": f"{key} 금액 오류"}), 400
        if not fields:
            return jsonify({"error": "변경할 롤링 값이 없습니다"}), 400
        conn = get_db_connection()
        cursor = conn.cursor()
        sets = ", ".join(f"{k}=%s" for k in fields)
        vals = list(fields.values()) + [time.time(), username]
        cursor.execute(
            f"UPDATE users SET {sets}, last_seen=%s WHERE username=%s",
            vals,
        )
        if cursor.rowcount == 0:
            cursor.close(); release_db(conn)
            return jsonify({"error": "유저 없음"}), 400
        conn.commit()
        cursor.execute(
            "SELECT COALESCE(current_rolling,0), COALESCE(total_rolling,0), COALESCE(initial_withdraw,0) FROM users WHERE username=%s",
            (username,),
        )
        row = cursor.fetchone()
        cursor.close(); release_db(conn)
        return jsonify({
            "success": True,
            "current_rolling": int(row[0] or 0),
            "total_rolling": int(row[1] or 0),
            "initial_withdraw": int(row[2] or 0),
        })
    except Exception as e:
        print(f"set_user_rolling error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/bulk_set_assets", methods=["POST"])
def admin_bulk_set_assets():
    """모든 유저 은행 잔고를 동일 금액으로 설정 (총자산 일괄 설정)"""
    try:
        auth_admin, err = require_admin()
        if err:
            return err
        data = request.json or {}
        try:
            cash = int(data.get("cash", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "금액 오류"}), 400
        if cash < 0:
            cash = 0
        reset_game = bool(data.get("reset_game_cash", False))
        conn = get_db_connection()
        cursor = conn.cursor()
        if reset_game:
            cursor.execute("UPDATE users SET cash=%s, game_cash=0", (cash,))
        else:
            cursor.execute("UPDATE users SET cash=%s", (cash,))
        count = cursor.rowcount
        conn.commit()
        cursor.close(); release_db(conn)
        return jsonify({"success": True, "count": count, "cash": cash, "reset_game_cash": reset_game})
    except Exception as e:
        print(f"bulk_set_assets error: {e}")
        return jsonify({"error": str(e)}), 500



@app.route("/api/missions", methods=["GET"])
def get_missions():
    """일일 미션 목록 + 진행도"""
    try:
        username = (request.args.get("user") or "").strip()
        if not username:
            return jsonify({"error": "user required"}), 400
        today = kst_today()
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT COALESCE(mission_data,''), COALESCE(last_attendance,''), COALESCE(cash,0) FROM users WHERE username=%s",
                (username,),
            )
        except Exception:
            conn.rollback()
            ensure_user_columns(cursor, conn)
            cursor.execute(
                "SELECT COALESCE(mission_data,''), COALESCE(last_attendance,''), COALESCE(cash,0) FROM users WHERE username=%s",
                (username,),
            )
        row = cursor.fetchone()
        if not row:
            cursor.close(); release_db(conn)
            return jsonify({"error": "유저 없음"}), 400
        data = _load_mission_data(row[0])
        if data.get("date") != today:
            data = {"date": today, "progress": {}, "claimed": []}
            _save_mission_data(cursor, username, data)
            conn.commit()
        # auto progress attend
        prog = data.setdefault("progress", {})
        if (row[1] or "") == today:
            prog["attend"] = max(int(prog.get("attend") or 0), 1)
            data["progress"] = prog
            _save_mission_data(cursor, username, data)
            conn.commit()
        claimed = set(data.get("claimed") or [])
        items = []
        for m in DAILY_MISSIONS:
            cur = int(prog.get(m["code"]) or 0)
            items.append({
                **m,
                "progress": cur,
                "done": cur >= m["target"],
                "claimed": m["code"] in claimed,
            })
        cursor.close(); release_db(conn)
        return jsonify({"date": today, "missions": items, "season": SEASON_CONFIG})
    except Exception as e:
        print(f"missions error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/missions/event", methods=["POST"])
def mission_event():
    """게임/행동 이벤트 → 미션 진행도 증가"""
    try:
        data = request.get_json(silent=True) or {}
        username = (data.get("username") or "").strip()
        event = (data.get("event") or "").strip()
        if not username or not event:
            return jsonify({"error": "username/event 필요"}), 400
        today = kst_today()
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT COALESCE(mission_data,'') FROM users WHERE username=%s FOR UPDATE", (username,))
        except Exception:
            conn.rollback()
            ensure_user_columns(cursor, conn)
            cursor.execute("SELECT COALESCE(mission_data,'') FROM users WHERE username=%s FOR UPDATE", (username,))
        row = cursor.fetchone()
        if not row:
            cursor.close(); release_db(conn)
            return jsonify({"error": "유저 없음"}), 400
        md = _load_mission_data(row[0])
        if md.get("date") != today:
            md = {"date": today, "progress": {}, "claimed": []}
        prog = md.setdefault("progress", {})
        # map events
        inc = {}
        if event in ("play_any", "play_baccarat", "play_dragontiger", "play_highlow", "play_roulette",
                     "play_blackjack", "play_slot", "play_dice", "play_crash", "play_plinko",
                     "play_stock", "play_poker"):
            inc["play_any"] = 1
            if event == "play_baccarat":
                inc["play_bacc"] = 1
            elif event.startswith("play_"):
                pass
        if event == "win_any":
            inc["win_any"] = 1
        if event == "transfer":
            inc["transfer"] = 1
        if event == "attend":
            inc["attend"] = 1
        for k, v in inc.items():
            prog[k] = int(prog.get(k) or 0) + int(v)
        md["progress"] = prog
        _save_mission_data(cursor, username, md)
        conn.commit()
        cursor.close(); release_db(conn)
        return jsonify({"success": True, "progress": prog})
    except Exception as e:
        print(f"mission event error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/missions/claim", methods=["POST"])
def mission_claim():
    try:
        data = request.get_json(silent=True) or {}
        username = (data.get("username") or "").strip()
        code = (data.get("code") or "").strip()
        if not username or not code:
            return jsonify({"error": "username/code 필요"}), 400
        mission = next((m for m in DAILY_MISSIONS if m["code"] == code), None)
        if not mission:
            return jsonify({"error": "없는 미션"}), 400
        today = kst_today()
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COALESCE(mission_data,''), COALESCE(cash,0) FROM users WHERE username=%s FOR UPDATE",
            (username,),
        )
        row = cursor.fetchone()
        if not row:
            cursor.close(); release_db(conn)
            return jsonify({"error": "유저 없음"}), 400
        md = _load_mission_data(row[0])
        if md.get("date") != today:
            md = {"date": today, "progress": {}, "claimed": []}
        prog = md.get("progress") or {}
        claimed = list(md.get("claimed") or [])
        if code in claimed:
            cursor.close(); release_db(conn)
            return jsonify({"error": "이미 수령함"}), 400
        if int(prog.get(code) or 0) < mission["target"]:
            cursor.close(); release_db(conn)
            return jsonify({"error": "미션 미완료"}), 400
        reward = int(mission["reward"])
        new_cash = int(row[1] or 0) + reward
        claimed.append(code)
        md["claimed"] = claimed
        cursor.execute("UPDATE users SET cash=%s WHERE username=%s", (new_cash, username))
        _save_mission_data(cursor, username, md)
        log_transaction(cursor, username, "미션보상", reward, new_cash, mission["title"])
        conn.commit()
        cursor.close(); release_db(conn)
        return jsonify({"success": True, "reward": reward, "cash": new_cash, "code": code})
    except Exception as e:
        print(f"mission claim error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/safety", methods=["GET"])
def get_safety():
    try:
        username = (request.args.get("user") or "").strip()
        if not username:
            return jsonify({"error": "user required"}), 400
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT COALESCE(daily_loss,0), COALESCE(daily_loss_date,''), COALESCE(self_excluded,FALSE),
                       COALESCE(profile_title,''), COALESCE(season_points,0), COALESCE(total_rolling,0)
                FROM users WHERE username=%s
                """,
                (username,),
            )
        except Exception:
            conn.rollback()
            ensure_user_columns(cursor, conn)
            cursor.execute(
                """
                SELECT COALESCE(daily_loss,0), COALESCE(daily_loss_date,''), COALESCE(self_excluded,FALSE),
                       COALESCE(profile_title,''), COALESCE(season_points,0), COALESCE(total_rolling,0)
                FROM users WHERE username=%s
                """,
                (username,),
            )
        row = cursor.fetchone()
        cursor.close(); release_db(conn)
        if not row:
            return jsonify({"error": "유저 없음"}), 400
        today = kst_today()
        daily_loss = int(row[0] or 0) if (row[1] or "") == today else 0
        tier = get_tier(row[5] or 0)
        title = (row[3] or "").strip() or title_for_tier(tier.get("name"))
        return jsonify({
            "daily_loss": daily_loss,
            "daily_loss_date": row[1] or "",
            "self_excluded": bool(row[2]),
            "profile_title": title,
            "season_points": int(row[4] or 0),
            "season": SEASON_CONFIG,
            "daily_loss_limit": 5_000_000,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/safety/loss", methods=["POST"])
def safety_add_loss():
    """세션 손실 누적 (일일 한도용)"""
    try:
        data = request.get_json(silent=True) or {}
        username = (data.get("username") or "").strip()
        try:
            amount = int(data.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0
        if not username or amount <= 0:
            return jsonify({"success": True, "skipped": True})
        today = kst_today()
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT COALESCE(daily_loss,0), COALESCE(daily_loss_date,''), COALESCE(self_excluded,FALSE) FROM users WHERE username=%s FOR UPDATE",
                (username,),
            )
        except Exception:
            conn.rollback()
            ensure_user_columns(cursor, conn)
            cursor.execute(
                "SELECT COALESCE(daily_loss,0), COALESCE(daily_loss_date,''), COALESCE(self_excluded,FALSE) FROM users WHERE username=%s FOR UPDATE",
                (username,),
            )
        row = cursor.fetchone()
        if not row:
            cursor.close(); release_db(conn)
            return jsonify({"error": "유저 없음"}), 400
        if bool(row[2]):
            cursor.close(); release_db(conn)
            return jsonify({"error": "자기보호 모드입니다. 잠시 휴식이 필요합니다."}), 403
        loss = int(row[0] or 0) if (row[1] or "") == today else 0
        loss += amount
        cursor.execute(
            "UPDATE users SET daily_loss=%s, daily_loss_date=%s WHERE username=%s",
            (loss, today, username),
        )
        conn.commit()
        cursor.close(); release_db(conn)
        limit = 5_000_000
        return jsonify({"success": True, "daily_loss": loss, "limit": limit, "blocked": loss >= limit})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/safety/exclude", methods=["POST"])
def safety_self_exclude():
    try:
        data = request.get_json(silent=True) or {}
        username = (data.get("username") or "").strip()
        exclude = bool(data.get("exclude", True))
        if not username:
            return jsonify({"error": "username 필요"}), 400
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("UPDATE users SET self_excluded=%s WHERE username=%s", (exclude, username))
        except Exception:
            conn.rollback()
            ensure_user_columns(cursor, conn)
            cursor.execute("UPDATE users SET self_excluded=%s WHERE username=%s", (exclude, username))
        conn.commit()
        cursor.close(); release_db(conn)
        return jsonify({"success": True, "self_excluded": exclude})
    except Exception as e:
        return jsonify({"error": str(e)}), 500






# ===== 파산 재기 지원 (하루 1회) =====
RECOVERY_AMOUNT = 70_000
RECOVERY_BANK_MAX = 5_000  # 은행 잔고 이 이하 + 게임머니 0


@app.route("/api/recovery/status", methods=["GET", "POST"])
def recovery_status():
    try:
        data = request.get_json(silent=True) or {}
        auth_user, err = require_user()
        auth_user = auth_user or (data.get("username") or request.args.get("user") or "").strip()
        if not auth_user:
            return jsonify({"error": "로그인이 필요합니다"}), 401
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            ensure_user_columns(cursor, conn)
            try:
                conn.commit()
            except Exception:
                pass
        except Exception as ee:
            print(f"recovery status ensure: {ee}")
            try:
                conn.rollback()
            except Exception:
                pass
        try:
            cursor.execute(
                "SELECT COALESCE(cash,0), COALESCE(game_cash,0), COALESCE(last_recovery,'') FROM users WHERE username=%s",
                (auth_user,),
            )
        except Exception as se:
            # last_recovery 컬럼 없는 경우
            print(f"recovery status select fallback: {se}")
            try:
                conn.rollback()
            except Exception:
                pass
            cursor.execute(
                "SELECT COALESCE(cash,0), COALESCE(game_cash,0), '' FROM users WHERE username=%s",
                (auth_user,),
            )
        row = cursor.fetchone()
        cursor.close()
        release_db(conn)
        if not row:
            return jsonify({"error": "유저 없음"}), 404
        bank, game, last = int(row[0] or 0), int(row[1] or 0), (row[2] or "")
        today = time.strftime("%Y-%m-%d")
        broke = (game <= 0 and bank <= RECOVERY_BANK_MAX)
        claimed_today = (str(last) == today)
        can_claim = bool(broke and (not claimed_today))
        return jsonify({
            "success": True,
            "broke": broke,
            "can_claim": can_claim,
            "claimed_today": claimed_today,
            "amount": RECOVERY_AMOUNT,
            "bank": bank,
            "game_cash": game,
            "last_recovery": last,
            "bank_max": RECOVERY_BANK_MAX,
        })
    except Exception as e:
        print(f"recovery status: {e}")
        return jsonify({"error": f"status error: {e}"}), 500


@app.route("/api/recovery/claim", methods=["POST"])
def recovery_claim():
    """파산 유저 하루 1회 재기 지원금 — 서버 권위 지급"""
    try:
        data = request.get_json(silent=True) or {}
        auth_user, err = require_user()
        auth_user = auth_user or (data.get("username") or "").strip()
        if not auth_user:
            return jsonify({"error": "로그인이 필요합니다"}), 401
        today = time.strftime("%Y-%m-%d")
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            ensure_user_columns(cursor, conn)
            try:
                conn.commit()
            except Exception:
                pass
        except Exception as ee:
            print(f"recovery claim ensure: {ee}")
            try:
                conn.rollback()
            except Exception:
                pass
        try:
            cursor.execute(
                "SELECT COALESCE(cash,0), COALESCE(game_cash,0), COALESCE(last_recovery,'') FROM users WHERE username=%s FOR UPDATE",
                (auth_user,),
            )
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            cursor.execute(
                "SELECT COALESCE(cash,0), COALESCE(game_cash,0), '' FROM users WHERE username=%s FOR UPDATE",
                (auth_user,),
            )
        row = cursor.fetchone()
        if not row:
            cursor.close(); release_db(conn)
            return jsonify({"error": "유저 없음"}), 404
        bank, game, last = int(row[0] or 0), int(row[1] or 0), (row[2] or "")
        if str(last) == today:
            cursor.close(); release_db(conn)
            return jsonify({"error": "오늘은 이미 재기 지원을 받았습니다."}), 400
        if game > 0 or bank > RECOVERY_BANK_MAX:
            cursor.close(); release_db(conn)
            return jsonify({
                "error": f"파산 상태가 아닙니다. (게임머니 0원 · 은행 ₩{RECOVERY_BANK_MAX:,} 이하만 가능 / 현재 은행 ₩{bank:,} · 게임 ₩{game:,})"
            }), 400

        new_game = RECOVERY_AMOUNT
        try:
            cursor.execute(
                "UPDATE users SET game_cash=%s, last_recovery=%s WHERE username=%s",
                (new_game, today, auth_user),
            )
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            cursor.execute(
                "UPDATE users SET game_cash=%s WHERE username=%s",
                (new_game, auth_user),
            )
        try:
            log_transaction(cursor, auth_user, "재기지원", RECOVERY_AMOUNT, bank, "일일 파산 재기 지원")
        except Exception:
            pass
        conn.commit()
        cursor.close()
        release_db(conn)
        return jsonify({
            "success": True,
            "amount": RECOVERY_AMOUNT,
            "game_cash": new_game,
            "cash": bank,
            "message": f"재기 지원금 ₩{RECOVERY_AMOUNT:,} 지급 완료",
        })
    except Exception as e:
        print(f"recovery claim: {e}")
        return jsonify({"error": f"재기 지원 처리 실패: {e}"}), 500



@app.route("/api/season/ranking", methods=["GET"])
def season_ranking():
    """현재 또는 특정 시즌 랭킹. ?season_id= 없으면 현재 시즌 실시간."""
    try:
        load_season_config_from_db()
        season_id = request.args.get("season_id")
        # 과거 시즌 아카이브
        if season_id is not None and str(season_id).strip() != "":
            try:
                sid = int(season_id)
            except (TypeError, ValueError):
                return jsonify({"error": "invalid season_id"}), 400
            if sid == int(SEASON_CONFIG.get("id") or 0):
                pass  # fall through to live
            else:
                conn = get_db_connection()
                cursor = conn.cursor()
                try:
                    ensure_season_tables(cursor, conn)
                    cursor.execute(
                        """
                        SELECT season_id, name, label, COALESCE(started_at,0), COALESCE(ended_at,0), COALESCE(rankings,'[]')
                        FROM season_archives WHERE season_id=%s
                        """,
                        (sid,),
                    )
                    row = cursor.fetchone()
                finally:
                    cursor.close()
                    release_db(conn)
                if not row:
                    return jsonify({"error": "해당 시즌 기록 없음"}), 404
                import json as _json
                try:
                    users = _json.loads(row[5] or "[]")
                except Exception:
                    users = []
                return jsonify({
                    "season": {
                        "id": int(row[0]),
                        "name": row[1],
                        "label": row[2],
                        "started_at": float(row[3] or 0),
                        "ended_at": float(row[4] or 0),
                        "archived": True,
                    },
                    "users": users,
                })

        # 현재 시즌 라이브
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT username, COALESCE(display_name,''), COALESCE(season_points,0),
                       COALESCE(total_rolling,0), COALESCE(cash,0), COALESCE(game_cash,0), COALESCE(last_seen,0)
                FROM users ORDER BY season_points DESC, total_rolling DESC LIMIT 100
                """
            )
        except Exception:
            conn.rollback()
            ensure_user_columns(cursor, conn)
            cursor.execute(
                """
                SELECT username, COALESCE(display_name,''), COALESCE(season_points,0),
                       COALESCE(total_rolling,0), COALESCE(cash,0), COALESCE(game_cash,0), COALESCE(last_seen,0)
                FROM users ORDER BY season_points DESC, total_rolling DESC LIMIT 100
                """
            )
        rows = cursor.fetchall()
        cursor.close(); release_db(conn)
        now = time.time()
        users = []
        for r in rows:
            users.append({
                "username": r[0],
                "display_name": r[1] or r[0],
                "season_points": int(r[2] or 0),
                "total_rolling": int(r[3] or 0),
                "cash": int(r[4] or 0),
                "game_cash": int(r[5] or 0),
                "last_seen": float(r[6] or 0),
                "online": (now - float(r[6] or 0)) < 90 if r[6] else False,
                "tier": get_tier(r[3] or 0),
            })
        return jsonify({"season": dict(SEASON_CONFIG), "users": users})
    except Exception as e:
        print(f"season ranking error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/season/list", methods=["GET"])
def season_list():
    """현재 + 종료된 시즌 목록"""
    try:
        load_season_config_from_db()
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            ensure_season_tables(cursor, conn)
            cursor.execute(
                """
                SELECT season_id, name, label, COALESCE(started_at,0), COALESCE(ended_at,0)
                FROM season_archives ORDER BY season_id DESC
                """
            )
            rows = cursor.fetchall() or []
        finally:
            cursor.close()
            release_db(conn)
        seasons = []
        # current first
        seasons.append({
            **dict(SEASON_CONFIG),
            "archived": False,
            "ended_at": None,
        })
        for r in rows:
            if int(r[0]) == int(SEASON_CONFIG.get("id") or 0):
                continue
            seasons.append({
                "id": int(r[0]),
                "name": r[1],
                "label": r[2],
                "started_at": float(r[3] or 0),
                "ended_at": float(r[4] or 0),
                "archived": True,
            })
        return jsonify({"current": dict(SEASON_CONFIG), "seasons": seasons})
    except Exception as e:
        print(f"season list error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/season/end", methods=["POST"])
def admin_season_end():
    """시즌 종료 → 스냅샷 저장 → 포인트 초기화 → 다음 시즌 시작"""
    global SEASON_CONFIG, CURRENT_NOTICE
    try:
        auth_admin, err = require_admin()
        if err:
            return err
        load_season_config_from_db()
        import json as _json

        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            ensure_season_tables(cursor, conn)
            ensure_user_columns(cursor, conn)

            # snapshot rankings
            try:
                cursor.execute(
                    """
                    SELECT username, COALESCE(display_name,''), COALESCE(season_points,0),
                           COALESCE(total_rolling,0), COALESCE(cash,0), COALESCE(game_cash,0)
                    FROM users ORDER BY season_points DESC, total_rolling DESC LIMIT 200
                    """
                )
            except Exception:
                conn.rollback()
                ensure_user_columns(cursor, conn)
                cursor.execute(
                    """
                    SELECT username, COALESCE(display_name,''), COALESCE(season_points,0),
                           COALESCE(total_rolling,0), COALESCE(cash,0), COALESCE(game_cash,0)
                    FROM users ORDER BY season_points DESC, total_rolling DESC LIMIT 200
                    """
                )
            rows = cursor.fetchall() or []
            users = []
            for idx, r in enumerate(rows):
                users.append({
                    "rank": idx + 1,
                    "username": r[0],
                    "display_name": r[1] or r[0],
                    "season_points": int(r[2] or 0),
                    "total_rolling": int(r[3] or 0),
                    "cash": int(r[4] or 0),
                    "game_cash": int(r[5] or 0),
                    "tier": get_tier(r[3] or 0),
                })

            old_id = int(SEASON_CONFIG.get("id") or 1)
            old_name = SEASON_CONFIG.get("name") or f"시즌 {old_id}"
            old_label = SEASON_CONFIG.get("label") or f"ROYAL S{old_id}"
            old_started = float(SEASON_CONFIG.get("started_at") or time.time())
            ended_at = time.time()

            cursor.execute(
                """
                INSERT INTO season_archives (season_id, name, label, started_at, ended_at, rankings)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (season_id) DO UPDATE SET
                    name=EXCLUDED.name,
                    label=EXCLUDED.label,
                    started_at=EXCLUDED.started_at,
                    ended_at=EXCLUDED.ended_at,
                    rankings=EXCLUDED.rankings
                """,
                (old_id, old_name, old_label, old_started, ended_at, _json.dumps(users, ensure_ascii=False)),
            )

            # reset points
            try:
                cursor.execute("UPDATE users SET season_points=0")
            except Exception:
                pass

            new_id = old_id + 1
            SEASON_CONFIG = {
                "id": new_id,
                "name": f"시즌 {new_id}",
                "label": f"ROYAL S{new_id}",
                "started_at": ended_at,
                "duration_days": 14,
                "ends_at": ended_at + 14 * 86400,
            }
            # 전체 유저 중요 공지 (팝업)
            CURRENT_NOTICE = {
                "id": f"season_end_{old_id}_{int(ended_at)}",
                "message": (
                    "📢 [중요] 시즌 종료 알림\n\n"
                    + f"{old_name} ({old_label})이(가) 종료되었습니다.\n"
                    + f"시즌 점수가 초기화되고 {SEASON_CONFIG['name']}이(가) 시작됩니다.\n\n"
                    + "• 은행/게임 머니: 유지\n"
                    + "• 티어/롤링: 유지\n"
                    + "• 시즌 포인트: 0으로 리셋\n\n"
                    + "새 시즌에서 다시 도전해 보세요!"
                ),
                "created_at": ended_at,
                "important": True,
            }
            cursor.execute(
                """
                INSERT INTO season_meta (id, season_id, name, label, started_at)
                VALUES (1, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    season_id=EXCLUDED.season_id,
                    name=EXCLUDED.name,
                    label=EXCLUDED.label,
                    started_at=EXCLUDED.started_at
                """,
                (new_id, SEASON_CONFIG["name"], SEASON_CONFIG["label"], ended_at),
            )
            conn.commit()
        finally:
            cursor.close()
            release_db(conn)

        return jsonify({
            "success": True,
            "ended": {"id": old_id, "name": old_name, "label": old_label, "ended_at": ended_at},
            "current": dict(SEASON_CONFIG),
            "archived_count": len(users),
        })
    except Exception as e:
        print(f"admin season end error: {e}")
        return jsonify({"error": str(e)}), 500



@app.route("/api/admin/dashboard", methods=["GET"])
def admin_dashboard():
    try:
        auth_admin, err = require_admin()
        if err:
            return err
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = int((cursor.fetchone() or [0])[0] or 0)
        cursor.execute("SELECT COALESCE(SUM(cash),0), COALESCE(SUM(game_cash),0), COALESCE(SUM(total_rolling),0), COALESCE(SUM(total_profit),0) FROM users")
        sums = cursor.fetchone() or (0, 0, 0, 0)
        now = time.time()
        try:
            cursor.execute("SELECT COUNT(*) FROM users WHERE last_seen > %s", (now - 90,))
            online = int((cursor.fetchone() or [0])[0] or 0)
        except Exception:
            online = 0
        cursor.execute("SELECT COALESCE(cash,0) FROM users WHERE username=%s", (ADMIN_USER,))
        admin_cash = int((cursor.fetchone() or [0])[0] or 0)
        cursor.close(); release_db(conn)
        return jsonify({
            "total_users": total_users,
            "online": online,
            "sum_bank": int(sums[0] or 0),
            "sum_game": int(sums[1] or 0),
            "sum_rolling": int(sums[2] or 0),
            "sum_profit": int(sums[3] or 0),
            "admin_cash": admin_cash,
            "difficulty": DIFFICULTY_CONFIG,
            "stats": {k: {"house_net": int(v.get("house_net", 0))} for k, v in DIFFICULTY_STATS.items()},
            "season": SEASON_CONFIG,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500



if __name__ == "__main__":
    init_db()
    try:
        load_season_config_from_db()
    except Exception as _se:
        print(f"season load: {_se}")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
