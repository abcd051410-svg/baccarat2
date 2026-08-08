import os
import time
from datetime import datetime, timezone, timedelta
import psycopg2
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
DATABASE_URL = os.environ.get("DATABASE_URL")

# 공지 (메모리 저장 — 서버 재시작 시 초기화)
# 영구 저장이 필요하면 notices 테이블로 분리 가능
CURRENT_NOTICE = {"id": "", "message": "", "created_at": 0}

# 롤링 티어 (누적 total_rolling 기준, 하락 없음)
TIER_TABLE = [
    # min_rolling, name, badge, fee_rate, attend, max_bet_mult, roll_ratio
    # roll_ratio: 마감에 필요한 롤링 = 출금액 * ratio (낮을수록 유리)
    (300_000_000, "마스터", "🔥", 0.01, 300000, 5.0, 0.50),
    (100_000_000, "다이아", "👑", 0.02, 280000, 3.0, 0.60),
    (50_000_000, "플래티넘", "💎", 0.03, 250000, 2.0, 0.70),
    (20_000_000, "골드", "🥇", 0.04, 230000, 1.5, 0.80),
    (5_000_000, "실버", "🥈", 0.045, 210000, 1.2, 0.90),
    (0, "브론즈", "🥉", 0.05, 200000, 1.0, 1.00),
]


def get_tier(rolling):
    rolling = int(rolling or 0)
    for i, row in enumerate(TIER_TABLE):
        mn, name, badge, fee, attend, mult, ratio = row
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
            }
    return get_tier(0)





def get_db_connection():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL 환경 변수가 설정되지 않았습니다!")
    return psycopg2.connect(DATABASE_URL)



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
            cursor.close(); conn.close()
        return row[0], None
    cursor.execute(
        "SELECT username FROM users WHERE display_name = %s OR display_name = %s",
        (identifier, identifier),
    )
    rows = cursor.fetchall()
    if own_conn:
        cursor.close(); conn.close()
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
                password VARCHAR(100) NOT NULL,
                cash BIGINT DEFAULT 200000,
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
        except Exception:
            pass

        conn.commit()
        cursor.close()
        conn.close()
        print("Database initialized successfully!")
    except Exception as e:
        print(f"Database initialization error details: {e}")


@app.route("/api/register", methods=["POST"])
def register():
    try:
        data = request.json or {}
        username = data.get("username", "").strip()
        password = data.get("password", "").strip()
        display_name = (
            data.get("display_name")
            or data.get("name")
            or ""
        ).strip()

        if not username or not password:
            return jsonify({"error": "아이디와 비밀번호를 모두 입력해주세요."}), 400
        if not display_name:
            return jsonify({"error": "이름을 입력해주세요."}), 400

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT username FROM users WHERE username = %s", (username,))
        if cursor.fetchone():
            cursor.close()
            conn.close()
            return jsonify({"error": "이미 존재하는 아이디입니다."}), 400

        cursor.execute(
            """
            INSERT INTO users (
                username, password, cash, game_cash, total_rolling,
                initial_withdraw, current_rolling, profit_rate, last_attendance,
                display_name, total_profit
            )
            VALUES (%s, %s, 200000, 0, 0, 0, 0, 0.0, '', %s, 0)
            """,
            (username, password, display_name),
        )
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({"success": True, "message": "회원가입이 완료되었습니다."})
    except Exception as e:
        print(f"Register error: {e}")
        return jsonify({"error": f"서버 통신 오류: {str(e)}"}), 500


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
        if not row:
            cursor.close()
            conn.close()
            return jsonify({"error": "존재하지 않는 아이디입니다."}), 400

        (
            db_password,
            cash,
            game_cash,
            total_rolling,
            initial_withdraw,
            current_rolling,
            profit_rate,
            last_attendance,
            display_name,
            total_profit,
            suspended,
        ) = row

        if db_password != password:
            cursor.close()
            conn.close()
            return jsonify({"error": "비밀번호가 일치하지 않습니다."}), 400

        cursor.close()
        conn.close()
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
        })
    except Exception as e:
        print(f"Login error: {e}")
        return jsonify({"error": f"서버 통신 오류: {str(e)}"}), 500


@app.route("/api/update", methods=["POST"])
def update_user():
    try:
        data = request.json or {}
        username = data.get("username")
        cash = data.get("cash")
        game_cash = data.get("game_cash")
        rolling_add = data.get("rolling_add", 0)
        initial_withdraw = data.get("initial_withdraw")
        current_rolling = data.get("current_rolling")
        profit_rate = data.get("profit_rate")
        last_attendance = data.get("last_attendance")
        total_profit = data.get("total_profit")
        display_name = data.get("display_name")

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE users
            SET cash = %s,
                game_cash = COALESCE(%s, game_cash),
                total_rolling = total_rolling + %s,
                initial_withdraw = COALESCE(%s, initial_withdraw),
                current_rolling = COALESCE(%s, current_rolling),
                profit_rate = COALESCE(%s, profit_rate),
                last_attendance = COALESCE(%s, last_attendance),
                total_profit = COALESCE(%s, total_profit),
                display_name = COALESCE(%s, display_name)
            WHERE username = %s
            """,
            (
                cash,
                game_cash,
                rolling_add,
                initial_withdraw,
                current_rolling,
                profit_rate,
                last_attendance,
                total_profit,
                display_name,
                username,
            ),
        )
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        print(f"Update error: {e}")
        return jsonify({"error": f"서버 통신 오류: {str(e)}"}), 500


@app.route("/api/admin/users", methods=["GET"])
def admin_get_users():
    try:
        pw = request.args.get("pw") or request.args.get("admin_user") or ""
        if pw != "abcd051410":
            return jsonify({"error": "Unauthorized"}), 403

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
        conn.close()
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
        data = request.json or {}
        pw = data.get("pw")
        username = data.get("username")
        new_cash = data.get("cash")
        new_game_cash = data.get("game_cash", None)

        if (pw != "abcd051410") and (data.get("admin_user") != "abcd051410"):
            return jsonify({"error": "Unauthorized"}), 401
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
                "UPDATE users SET cash = %s, game_cash = %s WHERE username = %s",
                (final_cash, final_game, username),
            )
        else:
            final_game = prev_game
            cursor.execute(
                "UPDATE users SET cash = %s WHERE username = %s",
                (final_cash, username),
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
        conn.close()
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
        data = request.json or {}
        if data.get("pw") != "abcd051410" and data.get("admin_user") != "abcd051410":
            return jsonify({"error": "Unauthorized"}), 403

        username = data.get("username")
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM users WHERE username = %s", (username,))
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        print(f"Admin delete error: {e}")
        return jsonify({"error": f"서버 통신 오류: {str(e)}"}), 500


@app.route("/api/admin/notice", methods=["POST"])
def admin_send_notice():
    """관리자 공지 발송 → 전체 유저가 /api/notice 로 수신"""
    global CURRENT_NOTICE
    try:
        data = request.json or {}
        if data.get("pw") != "abcd051410" and data.get("admin_user") != "abcd051410":
            return jsonify({"error": "Unauthorized"}), 403

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
def get_notice():
    """최신 공지 조회 (프론트 15초 폴링)"""
    if not CURRENT_NOTICE.get("id"):
        return jsonify({"id": "", "message": ""})
    return jsonify({
        "id": CURRENT_NOTICE["id"],
        "message": CURRENT_NOTICE["message"],
        "created_at": CURRENT_NOTICE.get("created_at", 0),
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
            cursor.close(); conn.close()
            return jsonify({"error": "존재하지 않는 계정입니다."}), 400
        if row[0] != password:
            cursor.close(); conn.close()
            return jsonify({"error": "비밀번호가 일치하지 않습니다."}), 400
        cursor.execute("DELETE FROM users WHERE username = %s", (username,))
        conn.commit()
        cursor.close(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        print(f"Delete account error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/reset_ranking", methods=["POST"])
def admin_reset_ranking():
    try:
        data = request.json or {}
        admin_user = data.get("admin_user") or data.get("pw") or ""
        if admin_user != "abcd051410":
            return jsonify({"error": "Unauthorized"}), 401
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET total_profit = 0, total_rolling = 0")
        conn.commit()
        cursor.close(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        print(f"Reset ranking error: {e}")
        return jsonify({"error": str(e)}), 500




@app.route("/api/transfer", methods=["POST"])
def transfer_money():
    """유저 간 은행 잔고 송금. 수수료 5%(최소 100원)는 관리자 계좌로."""
    try:
        data = request.json or {}
        from_user = (data.get("from_user") or "").strip()
        to_user = (data.get("to_user") or "").strip()
        amount = data.get("amount")
        password = data.get("password") or ""
        ADMIN = "abcd051410"

        if not from_user or not to_user:
            return jsonify({"error": "송금 대상 아이디를 입력해주세요."}), 400
        if from_user == to_user:
            return jsonify({"error": "본인에게는 송금할 수 없습니다."}), 400
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            return jsonify({"error": "금액을 확인해주세요."}), 400
        if amount <= 0:
            return jsonify({"error": "1원 이상 송금해주세요."}), 400

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT password, cash, COALESCE(total_rolling, 0) FROM users WHERE username = %s",
            (from_user,),
        )
        row = cursor.fetchone()
        if not row:
            cursor.close(); conn.close()
            return jsonify({"error": "보내는 계정이 없습니다."}), 400
        if row[0] != password:
            cursor.close(); conn.close()
            return jsonify({"error": "비밀번호가 일치하지 않습니다."}), 400
        from_cash = row[1] or 0
        tier = get_tier(row[2] if len(row) > 2 else 0)
        fee = max(100, int(amount * float(tier["fee_rate"])))
        total_need = amount + fee
        if from_cash < total_need:
            cursor.close(); conn.close()
            return jsonify({
                "error": f"잔고 부족 (송금 ₩{amount:,} + 수수료 ₩{fee:,} = ₩{total_need:,} 필요)"
            }), 400

        cursor.execute("SELECT username FROM users WHERE username = %s", (to_user,))
        row_to = cursor.fetchone()
        if row_to:
            to_user = row_to[0]
        else:
            cursor.execute("SELECT username FROM users WHERE display_name = %s", (to_user,))
            matches = cursor.fetchall()
            if len(matches) == 1:
                to_user = matches[0][0]
            elif len(matches) > 1:
                cursor.close(); conn.close()
                return jsonify({"error": "같은 이름이 여러 명입니다. 아이디로 송금해주세요."}), 400
            else:
                cursor.close(); conn.close()
                return jsonify({"error": "받는 사람(아이디/이름)을 찾을 수 없습니다."}), 400

        cursor.execute(
            "UPDATE users SET cash = cash - %s WHERE username = %s",
            (total_need, from_user),
        )
        cursor.execute(
            "UPDATE users SET cash = cash + %s WHERE username = %s",
            (amount, to_user),
        )
        # 수수료 → 관리자
        cursor.execute("SELECT username FROM users WHERE username = %s", (ADMIN,))
        if cursor.fetchone():
            cursor.execute(
                "UPDATE users SET cash = cash + %s WHERE username = %s",
                (fee, ADMIN),
            )
        else:
            # 관리자 계정 없으면 무시하지 않고 로그만
            print(f"[WARN] Admin {ADMIN} missing, fee ₩{fee} not credited")

        # 기록
        cursor.execute("SELECT cash FROM users WHERE username = %s", (from_user,))
        from_bal = (cursor.fetchone() or [0])[0] or 0
        cursor.execute("SELECT cash FROM users WHERE username = %s", (to_user,))
        to_bal = (cursor.fetchone() or [0])[0] or 0
        log_transaction(cursor, from_user, "송금출금", -amount, from_bal, f"{to_user}에게 송금")
        if fee > 0:
            log_transaction(cursor, from_user, "송금수수료", -fee, from_bal, "송금 수수료")
        log_transaction(cursor, to_user, "송금입금", amount, to_bal, f"{from_user}에게서 받음")
        conn.commit()
        cursor.execute("SELECT cash, game_cash FROM users WHERE username = %s", (from_user,))
        me = cursor.fetchone()
        cursor.close(); conn.close()
        return jsonify({
            "success": True,
            "cash": me[0] if me else 0,
            "game_cash": me[1] if me else 0,
            "fee": fee,
            "sent": amount,
            "message": f"{to_user}님에게 ₩{amount:,} 송금 (수수료 ₩{fee:,})",
        })
    except Exception as e:
        print(f"Transfer error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/house_collect", methods=["POST"])
def house_collect():
    """하우 손익 반영. amount>0 유저 손실(관리자 입금), amount<0 유저 당첨(관리자 출금)"""
    try:
        data = request.json or {}
        try:
            amount = int(data.get("amount", 0))
        except (TypeError, ValueError):
            amount = 0
        if amount == 0:
            return jsonify({"success": True, "skipped": True})
        memo = (data.get("memo") or "").strip()
        ADMIN = "abcd051410"
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT cash FROM users WHERE username = %s", (ADMIN,))
        row = cursor.fetchone()
        if not row:
            cursor.close(); conn.close()
            return jsonify({"error": "admin not found"}), 400
        prev = int(row[0] or 0)
        new_cash = prev + amount
        if new_cash < 0:
            new_cash = 0
        cursor.execute(
            "UPDATE users SET cash = %s WHERE username = %s",
            (new_cash, ADMIN),
        )
        if amount > 0:
            kind = "하우수익"
            note = (memo + " · 유저 손실→하우 이득") if memo else "유저 손실·수수료 회수"
        else:
            kind = "하우지급"
            note = (memo + " · 유저 당첨→하우 지출") if memo else "유저 당첨 지급"
        log_transaction(cursor, ADMIN, kind, amount, new_cash, note)
        conn.commit()
        cursor.close(); conn.close()
        return jsonify({"success": True, "amount": amount, "admin_cash": new_cash})
    except Exception as e:
        print(f"House collect error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/messages/send", methods=["POST"])
def messages_send():
    try:
        data = request.json or {}
        from_user = (data.get("from_user") or "").strip()
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
            cursor.execute("SELECT username FROM users WHERE display_name = %s", (to_user,))
            matches = cursor.fetchall()
            if len(matches) == 1:
                to_user = matches[0][0]
            elif len(matches) > 1:
                cursor.close(); conn.close()
                return jsonify({"error": "같은 이름이 여러 명입니다. 아이디로 보내주세요."}), 400
            else:
                cursor.close(); conn.close()
                return jsonify({"error": "받는 사람(아이디/이름)을 찾을 수 없습니다."}), 400
        cursor.execute(
            "INSERT INTO messages (from_user, to_user, body) VALUES (%s, %s, %s) RETURNING id, created_at",
            (from_user, to_user, body),
        )
        row = cursor.fetchone()
        conn.commit()
        cursor.close(); conn.close()
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
        cursor.close(); conn.close()
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
        cursor.close(); conn.close()
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
        cursor.close(); conn.close()
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
        return jsonify({"error": str(e)}), 500



@app.route("/api/attendance", methods=["POST"])
def attendance_check():
    """매일 1회 출석 시 은행 잔고 +200000"""
    try:
        data = request.json or {}
        username = (data.get("username") or "").strip()
        if not username:
            return jsonify({"error": "로그인이 필요합니다."}), 400

        # KST 날짜
        kst = timezone(timedelta(hours=9))
        today = datetime.now(kst).strftime("%Y-%m-%d")

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT cash, COALESCE(last_attendance, ''), COALESCE(total_rolling, 0) FROM users WHERE username = %s",
            (username,),
        )
        row = cursor.fetchone()
        if not row:
            cursor.close(); conn.close()
            return jsonify({"error": "계정을 찾을 수 없습니다."}), 400

        cash, last, rolling = int(row[0] or 0), (row[1] or ""), int(row[2] or 0)
        tier = get_tier(rolling)
        reward = int(tier["attend_reward"])
        if last == today:
            cursor.close(); conn.close()
            return jsonify({"error": "오늘은 이미 출석했습니다.", "already": True, "last_attendance": last, "tier": tier}), 400

        new_cash = cash + reward
        cursor.execute(
            "UPDATE users SET cash = %s, last_attendance = %s WHERE username = %s",
            (new_cash, today, username),
        )
        log_transaction(cursor, username, "출석보상", reward, new_cash, f"{today} 출석체크")
        conn.commit()
        cursor.close(); conn.close()
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
        data = request.json or {}
        if data.get("pw") != "abcd051410" and data.get("admin_user") != "abcd051410":
            return jsonify({"error": "Unauthorized"}), 401
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
        cursor.close(); conn.close()
        return jsonify({"success": True, "count": count, "amount": amount})
    except Exception as e:
        print(f"Bulk pay error: {e}")
        return jsonify({"error": str(e)}), 500



@app.route("/api/admin/suspend", methods=["POST"])
def admin_suspend():
    """계정 일시정지 / 해제"""
    try:
        data = request.json or {}
        if data.get("pw") != "abcd051410" and data.get("admin_user") != "abcd051410":
            return jsonify({"error": "Unauthorized"}), 401
        username = (data.get("username") or "").strip()
        suspend = bool(data.get("suspend", True))
        if not username:
            return jsonify({"error": "username required"}), 400
        if username == "abcd051410":
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
            cursor.close(); conn.close()
            return jsonify({"error": "유저 없음"}), 400
        conn.commit()
        cursor.close(); conn.close()
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
            cursor.close(); conn.close()
            return jsonify({"error": "계정을 찾을 수 없습니다."}), 400
        if row[0] != password:
            cursor.close(); conn.close()
            return jsonify({"error": "비밀번호가 일치하지 않습니다."}), 400
        old_name = row[1] or ""
        if new_name == old_name:
            cursor.close(); conn.close()
            return jsonify({"error": "기존과 다른 이름을 입력해주세요."}), 400
        cursor.execute(
            "UPDATE users SET display_name = %s, suspended = FALSE WHERE username = %s",
            (new_name, username),
        )
        conn.commit()
        cursor.close(); conn.close()
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
        cursor.close(); conn.close()
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
        cursor.close(); conn.close()
        return jsonify({"count": int(count)})
    except Exception as e:
        return jsonify({"count": 0})


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
