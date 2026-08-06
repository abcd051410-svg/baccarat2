import os
import psycopg2
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")


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
                cash BIGINT DEFAULT 50000,
                game_cash BIGINT DEFAULT 0,
                total_rolling BIGINT DEFAULT 0,
                initial_withdraw BIGINT DEFAULT 0,
                current_rolling BIGINT DEFAULT 0,
                profit_rate REAL DEFAULT 0.0,
                last_attendance VARCHAR(20) DEFAULT ''
            )
        """)
        conn.commit()
        cursor.close()
        conn.close()
        print("Database initialized successfully!")
    except Exception as e:
        print(f"Database initialization error details: {e}")


@app.route("/api/register", methods=["POST"])
def register():
    try:
        data = request.json
        username = data.get("username", "").strip()
        password = data.get("password", "").strip()

        if not username or not password:
            return jsonify({"error": "아이디와 비밀번호를 모두 입력해주세요."}), 400

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT username FROM users WHERE username = %s", (username,)
        )
        if cursor.fetchone():
            cursor.close()
            conn.close()
            return jsonify({"error": "이미 존재하는 아이디입니다."}), 400

        cursor.execute(
            """
            INSERT INTO users (username, password, cash, game_cash, total_rolling, initial_withdraw, current_rolling, profit_rate, last_attendance)
            VALUES (%s, %s, 50000, 0, 0, 0, 0, 0.0, '')
        """,
            (username, password),
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
        data = request.json
        username = data.get("username", "").strip()
        password = data.get("password", "").strip()

        if not username or not password:
            return jsonify({"error": "아이디와 비밀번호를 모두 입력해주세요."}), 400

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT password, cash, game_cash, total_rolling, initial_withdraw, current_rolling, profit_rate, last_attendance 
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
        ) = row
        if db_password != password:
            cursor.close()
            conn.close()
            return jsonify({"error": "비밀번호가 일치하지 않습니다."}), 400

        cursor.close()
        conn.close()
        return jsonify({
            "username": username,
            "cash": cash,
            "game_cash": game_cash,
            "total_rolling": total_rolling,
            "initial_withdraw": initial_withdraw,
            "current_rolling": current_rolling,
            "profit_rate": profit_rate,
            "last_attendance": last_attendance,
        })
    except Exception as e:
        print(f"Login error: {e}")
        return jsonify({"error": f"서버 통신 오류: {str(e)}"}), 500


@app.route("/api/update", methods=["POST"])
def update_user():
    try:
        data = request.json
        username = data.get("username")
        cash = data.get("cash")
        game_cash = data.get("game_cash")
        rolling_add = data.get("rolling_add", 0)
        initial_withdraw = data.get("initial_withdraw")
        current_rolling = data.get("current_rolling")
        profit_rate = data.get("profit_rate")
        last_attendance = data.get("last_attendance")

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
                last_attendance = COALESCE(%s, last_attendance)
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
            SELECT username, cash, game_cash, total_rolling, initial_withdraw, current_rolling, profit_rate 
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
            })
        return jsonify(users)
    except Exception as e:
        print(f"Admin users error: {e}")
        return jsonify({"error": f"서버 통신 오류: {str(e)}"}), 500


@app.route("/api/admin/edit", methods=["POST"])
def admin_edit_user():
    try:
        data = request.json
        pw = data.get("pw")
        username = data.get("username")
        new_cash = data.get("cash")

        if pw != "3195":
            return jsonify({"error": "Unauthorized"}), 401

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET cash = %s WHERE username = %s", (new_cash, username)
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
        data = request.json
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


@app.route("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
