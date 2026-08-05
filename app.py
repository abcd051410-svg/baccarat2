import sqlite3
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
DB_NAME = "baccarat.db"


def init_db():
  conn = sqlite3.connect(DB_NAME)
  cursor = conn.cursor()
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password TEXT NOT NULL,
            cash INTEGER DEFAULT 50000,
            game_cash INTEGER DEFAULT 0,
            total_rolling INTEGER DEFAULT 0,
            initial_withdraw INTEGER DEFAULT 0,
            current_rolling INTEGER DEFAULT 0,
            profit_rate REAL DEFAULT 0.0
        )
    """)
  conn.commit()
  conn.close()


@app.route("/api/register", methods=["POST"])
def register():
  data = request.json
  username = data.get("username", "").strip()
  password = data.get("password", "").strip()

  if not username or not password:
    return jsonify({"error": "아이디와 비밀번호를 모두 입력해주세요."}), 400

  conn = sqlite3.connect(DB_NAME)
  cursor = conn.cursor()

  cursor.execute("SELECT username FROM users WHERE username = ?", (username,))
  if cursor.fetchone():
    conn.close()
    return jsonify({"error": "이미 존재하는 아이디입니다."}), 400

  cursor.execute(
      """
        INSERT INTO users (username, password, cash, game_cash, total_rolling, initial_withdraw, current_rolling, profit_rate)
        VALUES (?, ?, 50000, 0, 0, 0, 0, 0.0)
    """,
      (username, password),
  )
  conn.commit()
  conn.close()
  return jsonify({"success": True, "message": "회원가입이 완료되었습니다."})


@app.route("/api/login", methods=["POST"])
def login():
  data = request.json
  username = data.get("username", "").strip()
  password = data.get("password", "").strip()

  if not username or not password:
    return jsonify({"error": "아이디와 비밀번호를 모두 입력해주세요."}), 400

  conn = sqlite3.connect(DB_NAME)
  cursor = conn.cursor()
  cursor.execute(
      """
        SELECT password, cash, game_cash, total_rolling, initial_withdraw, current_rolling, profit_rate 
        FROM users WHERE username = ?
    """,
      (username,),
  )
  row = cursor.fetchone()

  if not row:
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
  ) = row
  if db_password != password:
    conn.close()
    return jsonify({"error": "비밀번호가 일치하지 않습니다."}), 400

  conn.close()
  return jsonify({
      "username": username,
      "cash": cash,
      "game_cash": game_cash,
      "total_rolling": total_rolling,
      "initial_withdraw": initial_withdraw,
      "current_rolling": current_rolling,
      "profit_rate": profit_rate,
  })


@app.route("/api/update", methods=["POST"])
def update_user():
  data = request.json
  username = data.get("username")
  cash = data.get("cash")
  game_cash = data.get("game_cash")
  rolling_add = data.get("rolling_add", 0)
  initial_withdraw = data.get("initial_withdraw")
  current_rolling = data.get("current_rolling")
  profit_rate = data.get("profit_rate")

  conn = sqlite3.connect(DB_NAME)
  cursor = conn.cursor()
  cursor.execute(
      """
        UPDATE users
        SET cash = ?, 
            game_cash = COALESCE(?, game_cash),
            total_rolling = total_rolling + ?,
            initial_withdraw = COALESCE(?, initial_withdraw),
            current_rolling = COALESCE(?, current_rolling),
            profit_rate = COALESCE(?, profit_rate)
        WHERE username = ?
    """,
      (
          cash,
          game_cash,
          rolling_add,
          initial_withdraw,
          current_rolling,
          profit_rate,
          username,
      ),
  )
  conn.commit()
  conn.close()
  return jsonify({"success": True})


@app.route("/api/admin/users", methods=["GET"])
def admin_get_users():
  pw = request.args.get("pw")
  if pw != "3195":
    return jsonify({"error": "Unauthorized"}), 403

  conn = sqlite3.connect(DB_NAME)
  cursor = conn.cursor()
  cursor.execute("""
        SELECT username, cash, game_cash, total_rolling, initial_withdraw, current_rolling, profit_rate 
        FROM users
    """)
  rows = cursor.fetchall()
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


@app.route("/api/admin/edit", methods=["POST"])
def admin_edit_user():
  data = request.json
  pw = data.get("pw")
  username = data.get("username")
  new_cash = data.get("cash")

  if pw != "3195":
    return jsonify({"error": "Unauthorized"}), 401

  conn = sqlite3.connect(DB_NAME)
  cursor = conn.cursor()
  cursor.execute(
      "UPDATE users SET cash = ? WHERE username = ?", (new_cash, username)
  )
  conn.commit()
  conn.close()

  return jsonify({"success": True})


@app.route("/api/admin/delete", methods=["POST"])
def admin_delete_user():
  data = request.json
  if data.get("pw") != "3195":
    return jsonify({"error": "Unauthorized"}), 403

  username = data.get("username")

  conn = sqlite3.connect(DB_NAME)
  cursor = conn.cursor()
  cursor.execute("DELETE FROM users WHERE username = ?", (username,))
  conn.commit()
  conn.close()
  return jsonify({"success": True})


@app.route("/")
def index():
  return render_template("index.html")


if __name__ == "__main__":
  init_db()
  app.run(host="0.0.0.0", port=5000, debug=True)


