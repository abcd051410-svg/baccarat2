import os
import time
import psycopg2
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
DATABASE_URL = os.environ.get("DATABASE_URL")

# 공지 (메모리 저장 — 서버 재시작 시 초기화)
CURRENT_NOTICE = {"id": "", "message": "", "created_at": 0}


def get_db_connection():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL 환경 변수가 설정되지 않았습니다!")
    return psycopg2.connect(DATABASE_URL)


def init_db():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

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

        for col, typedef in [
            ("display_name", "VARCHAR(50) DEFAULT ''"),
            ("total_profit", "BIGINT DEFAULT 0"),
        ]:
            try:
                cursor.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {typedef}")
            except Exception:
                try:
                    cursor.execute(f"ALTER TABLE users ADD COLUMN {col} {typedef}")
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
        display_name = (data.get("display_name") or data.get("name") or "").strip()

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
                   COALESCE(display_name, ''), COALESCE(total_profit, 0)
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
            db_password, cash, game_cash, total_rolling, initial_withdraw,
            current_rolling, profit_rate, last_attendance, display_name, total_profit,
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
        total_profit = data.get("total_profit")
        display_name = data.get("display_name")

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE users
            SET cash = %s,
                game_cash = COALESCE(%s, game_cash),
                total_rolling = total_rolling + COALESCE(%s, 0),
                initial_withdraw = COALESCE(%s, initial_withdraw),
                current_rolling = COALESCE(%s, current_rolling),
                total_profit = COALESCE(%s, total_profit),
                display_name = COALESCE(%s, display_name)
            WHERE username = %s
            """,
            (
                cash, game_cash, rolling_add, initial_withdraw,
                current_rolling, total_profit, display_name, username,
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
        cursor.execute("""
            SELECT username, cash, game_cash, total_rolling, initial_withdraw,
                   current_rolling, COALESCE(display_name, ''), COALESCE(total_profit, 0)
            FROM users ORDER BY username
        """)
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        users = []
        for r in rows:
            users.append({
                "username": r[0],
                "cash": r[1],
                "game_cash": r[2],
                "total_rolling": r[3],
                "initial_withdraw": r[4],
                "current_rolling": r[5],
                "display_name": r[6] or r[0],
                "name": r[6] or r[0],
                "total_profit": r[7],
            })
        return jsonify(users)
    except Exception as e:
        print(f"Admin users error: {e}")
        return jsonify({"error": f"서버 통신 오류: {str(e)}"}), 500


@app.route("/api/admin/edit", methods=["POST"])
def admin_edit_user():
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
        if new_game_cash is not None:
            cursor.execute(
                "UPDATE users SET cash = %s, game_cash = %s WHERE username = %s",
                (max(0, int(new_cash or 0)), max(0, int(new_game_cash)), username),
            )
        else:
            cursor.execute(
                "UPDATE users SET cash = %s WHERE username = %s",
                (max(0, int(new_cash or 0)), username),
            )
        conn.commit()
        cursor.execute("SELECT cash, game_cash FROM users WHERE username = %s", (username,))
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
    global CURRENT_NOTICE
    try:
        data = request.json or {}
        if data.get("pw") != "abcd051410" and data.get("admin_user") != "abcd051410":
            return jsonify({"error": "Unauthorized"}), 401
        message = (data.get("message") or "").strip()
        if not message:
            return jsonify({"error": "공지 내용이 없습니다."}), 400
        CURRENT_NOTICE = {
            "id": str(int(time.time() * 1000)),
            "message": message,
            "created_at": time.time(),
        }
        return jsonify({"success": True, "id": CURRENT_NOTICE["id"]})
    except Exception as e:
        print(f"Notice error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/notice", methods=["GET"])
def get_notice():
    if CURRENT_NOTICE.get("id"):
        return jsonify(CURRENT_NOTICE)
    return jsonify({"id": "", "message": ""})


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
    """송금. 수수료 5%(최소 100원) → 관리자 계좌"""
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

        fee = max(100, int(amount * 0.05))
        total_need = amount + fee

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT password, cash FROM users WHERE username = %s", (from_user,))
        row = cursor.fetchone()
        if not row:
            cursor.close(); conn.close()
            return jsonify({"error": "보내는 계정이 없습니다."}), 400
        if row[0] != password:
            cursor.close(); conn.close()
            return jsonify({"error": "비밀번호가 일치하지 않습니다."}), 400
        if (row[1] or 0) < total_need:
            cursor.close(); conn.close()
            return jsonify({
                "error": f"잔고 부족 (송금 ₩{amount:,} + 수수료 ₩{fee:,} = ₩{total_need:,} 필요)"
            }), 400

        cursor.execute("SELECT username FROM users WHERE username = %s", (to_user,))
        if not cursor.fetchone():
            cursor.close(); conn.close()
            return jsonify({"error": "받는 아이디가 존재하지 않습니다."}), 400

        cursor.execute("UPDATE users SET cash = cash - %s WHERE username = %s", (total_need, from_user))
        cursor.execute("UPDATE users SET cash = cash + %s WHERE username = %s", (amount, to_user))
        cursor.execute("SELECT username FROM users WHERE username = %s", (ADMIN,))
        if cursor.fetchone():
            cursor.execute("UPDATE users SET cash = cash + %s WHERE username = %s", (fee, ADMIN))

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
    """유저 손실금·게임 수수료 → 관리자 은행 잔고"""
    try:
        data = request.json or {}
        try:
            amount = int(data.get("amount", 0))
        except (TypeError, ValueError):
            amount = 0
        if amount <= 0:
            return jsonify({"success": True, "skipped": True})
        ADMIN = "abcd051410"
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT username FROM users WHERE username = %s", (ADMIN,))
        if not cursor.fetchone():
            cursor.close(); conn.close()
            return jsonify({"error": "admin not found"}), 400
        cursor.execute("UPDATE users SET cash = cash + %s WHERE username = %s", (amount, ADMIN))
        conn.commit()
        cursor.close(); conn.close()
        return jsonify({"success": True, "amount": amount})
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
        if not cursor.fetchone():
            cursor.close(); conn.close()
            return jsonify({"error": "존재하지 않는 아이디입니다."}), 400
        cursor.execute(
            "INSERT INTO messages (from_user, to_user, body) VALUES (%s, %s, %s) RETURNING id, created_at",
            (from_user, to_user, body),
        )
        row = cursor.fetchone()
        conn.commit()
        cursor.close(); conn.close()
        return jsonify({"success": True, "id": row[0], "created_at": str(row[1]) if row else None})
    except Exception as e:
        print(f"Msg send error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/messages/inbox", methods=["GET"])
def messages_inbox():
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
            cursor.execute("SELECT COALESCE(display_name, '') FROM users WHERE username = %s", (peer,))
            nr = cursor.fetchone()
            peers.append({
                "username": peer,
                "display_name": (nr[0] if nr and nr[0] else peer),
                "last_at": str(last_at) if last_at else "",
                "unread": int(unread or 0),
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


@app.route("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
