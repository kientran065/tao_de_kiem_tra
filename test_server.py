"""Tự kiểm tra (không gọi Claude): python test_server.py"""
import json, os, tempfile, threading, urllib.request, urllib.error
import server

server.DB_PATH = os.path.join(tempfile.mkdtemp(), "test.db")
server.init_db()

# chấm trắc nghiệm
qs = [{"type": "mcq", "question": "1+1", "options": ["1", "2"], "answer": 1},
      {"type": "mcq", "question": "2+2", "options": ["4", "5"], "answer": 0},
      {"type": "essay", "question": "Giải thích"}]
total, res = server.grade(qs, [1, 1, ""])
assert round(total, 2) == 3.33 and res[0]["correct"] and not res[1]["correct"] and res[2]["score"] == 0, (total, res)

assert server.extract_json('Đây:\n```json\n{"a": [1]}\n```') == {"a": [1]}
doc = server.export_doc({"title": "<T>", "duration_min": 5, "questions": json.dumps(qs)}, True).decode()
assert "&lt;T&gt;" in doc and "Đáp án" in doc and "B" in doc

# tài khoản + phân quyền qua HTTP
httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{httpd.server_port}"


def call(path, body=None, cookie="", method=None):
    req = urllib.request.Request(base + path, data=body and json.dumps(body).encode(), method=method,
                                 headers={"Cookie": cookie, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read()), r.headers.get("Set-Cookie", "").split(";")[0]
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()), ""


assert call("/api/exams")[0] == 401
assert call("/api/register", {"username": "an", "password": "123"})[0] == 400  # mật khẩu ngắn
s, _, alice = call("/api/register", {"username": "alice", "password": "secret1"})
assert s == 200 and alice.startswith("token=")
assert call("/api/register", {"username": "alice", "password": "secret1"})[0] == 400  # trùng tên
assert call("/api/login", {"username": "alice", "password": "wrong!!"})[0] == 401
_, _, bob = call("/api/register", {"username": "bob", "password": "secret2"})

with server.db() as c:
    uid = c.execute("SELECT id FROM users WHERE username='alice'").fetchone()[0]
    eid = c.execute("INSERT INTO exams(user_id,title,duration_min,lesson,questions) VALUES(?,?,?,?,?)",
                    (uid, "Đề", 5, "# Bài", json.dumps(qs[:2]))).lastrowid

s, e, _ = call(f"/api/exams/{eid}", cookie=alice)
assert s == 200 and "answer" not in e["questions"][0]  # không lộ đáp án
assert call(f"/api/exams/{eid}", cookie=bob)[0] == 400  # bob không xem được đề của alice
s, r, _ = call(f"/api/exams/{eid}/submit", {"answers": [1, 0]}, alice)
s, a, _ = call(f"/api/attempts/{r['id']}", cookie=alice)
assert a["score"] == 10
assert call(f"/api/attempts/{r['id']}", cookie=bob)[0] == 400
assert len(call("/api/attempts", cookie=alice)[1]) == 1 and call("/api/attempts", cookie=bob)[1] == []
call("/api/logout", {}, alice)
assert call("/api/me", cookie=alice)[0] == 401
httpd.shutdown()
print("OK - tất cả kiểm tra đều qua")
