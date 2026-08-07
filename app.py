import os
import time
import psycopg2
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
DATABASE_URL = os.environ.get("DATABASE_URL")

# 공지 (메모리 저장 — 서버 재시작 시 초기화)
# 영구 저장이 필요하면 notices 테이블로 분리 가능
CURRENT_NOTICE = {"id": "", "message": "", "created_at": 0}


def get_db_connection():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL 환경 변수가 설정되지 않았습니다!")
    return psycopg2.connect(DATABASE_URL)


def init_db():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # 기본 테이블
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                username VARCHAR(50) PRIMARY KEY,
                password VARCHAR(100) NOT NULL,
                cash BIGINT DEFAULT 50000,
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
        ]:
            try:
                cursor.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {typedef}")
            except Exception:
                # IF NOT EXISTS 미지원 환경 대비
                try:
                    cursor.execute(f"ALTER TABLE users ADD COLUMN {col} {typedef}")
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
            VALUES (%s, %s, 50000, 0, 0, 0, 0, 0.0, '', %s, 0)
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
        pw = request.args.get("pw")
        if pw != "3195":
            return jsonify({"error": "Unauthorized"}), 403

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT username, cash, game_cash, total_rolling, initial_withdraw,
                   current_rolling, profit_rate,
                   COALESCE(display_name, ''), COALESCE(total_profit, 0)
            FROM users
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
                "profit_rate": r[6],
                "display_name": r[7] or r[0],
                "name": r[7] or r[0],
                "total_profit": r[8],
            })
        return jsonify(users)
    except Exception as e:
        print(f"Admin users error: {e}")
        return jsonify({"error": f"서버 통신 오류: {str(e)}"}), 500


@app.route("/api/admin/edit", methods=["POST"])
def admin_edit_user():
    """금액 설정(덮어쓰기). 프론트 '금액 추가'는 현재 잔액+추가분을 cash로 보냄."""
    try:
        data = request.json or {}
        pw = data.get("pw")
        username = data.get("username")
        new_cash = data.get("cash")

        if pw != "3195":
            return jsonify({"error": "Unauthorized"}), 401

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET cash = %s WHERE username = %s",
            (new_cash, username),
        )
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        print(f"Admin edit error: {e}")
        return jsonify({"error": f"서버 통신 오류: {str(e)}"}), 500


@app.route("/api/admin/delete", methods=["POST"])
def admin_delete_user():
    try:
        data = request.json or {}
        if data.get("pw") != "3195":
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
        if data.get("pw") != "3195":
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


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
