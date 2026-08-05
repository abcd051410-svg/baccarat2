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
            cash INTEGER DEFAULT 50000,
            total_rolling INTEGER DEFAULT 0
        )
    """)
  conn.commit()
  conn.close()


# 유저 로그인/조회
@app.route("/api/login", methods=["POST"])
def login():
  data = request.json
  username = data.get("username", "").strip()
  if not username:
    return jsonify({"error": "Invalid username"}), 400

  conn = sqlite3.connect(DB_NAME)
  cursor = conn.cursor()
  cursor.execute(
      "SELECT cash, total_rolling FROM users WHERE username = ?", (username,)
  )
  row = cursor.fetchone()

  if row:
    cash, total_rolling = row
  else:
    cash, total_rolling = 50000, 0
    cursor.execute(
        "INSERT INTO users (username, cash, total_rolling) VALUES (?, ?, ?)",
        (username, cash, total_rolling),
    )
    conn.commit()

  conn.close()
  return jsonify({"username": username, "cash": cash, "total_rolling": total_rolling})


# 유저 데이터 업데이트 (배팅 및 잔액 변동)
@app.route("/api/update", methods=["POST"])
def update_user():
  data = request.json
  username = data.get("username")
  cash = data.get("cash")
  rolling_add = data.get("rolling_add", 0)

  conn = sqlite3.connect(DB_NAME)
  cursor = conn.cursor()
  cursor.execute(
      """
        UPDATE users
        SET cash = ?, total_rolling = total_rolling + ?
        WHERE username = ?
    """,
      (cash, rolling_add, username),
  )
  conn.commit()
  conn.close()
  return jsonify({"success": True})


# 관리자: 모든 유저 목록 조회
@app.route("/api/admin/users", methods=["GET"])
def admin_get_users():
  pw = request.args.get("pw")
  if pw != "3195":
    return jsonify({"error": "Unauthorized"}), 403

  conn = sqlite3.connect(DB_NAME)
  cursor = conn.cursor()
  cursor.execute("SELECT username, cash, total_rolling FROM users")
  rows = cursor.fetchall()
  conn.close()

  users = [
      {"username": r[0], "cash": r[1], "total_rolling": r[2]} for r in rows
  ]
  return jsonify(users)


# 관리자: 유저 잔액 수정
@app.route('/api/admin/edit', methods=['POST'])

def admin_edit_user():

  data = request.json

  pw = data.get('pw')

  username = data.get('username')

  new_cash = data.get('cash')



  if pw != "3195":

    return jsonify({"error": "Unauthorized"}), 401



  conn = get_db_connection()

  cursor = conn.cursor()

  cursor.execute('UPDATE users SET cash = ? WHERE username = ?', (new_cash, username))

  conn.commit()

  conn.close()



  return jsonify({"success": True})


# 관리자: 유저 삭제
@app.route("/api/admin/delete", methods=["POST"])
def admin_delete_user():
  data.get("pw") != "6974"
  data = request.json
  if data.get("pw") != "6974":
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