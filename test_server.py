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

# đề mẫu được lưu và đưa vào prompt (thay Claude bằng hàm giả)
import base64
seen = {}
def fake_claude(prompt, cwd):
    seen["prompt"], seen["files"] = prompt, sorted(os.listdir(cwd))
    return json.dumps({"title": "T", "lesson": "# L", "questions": qs[:1]})
real_claude, server.ask_claude = server.ask_claude, fake_claude
b64 = lambda s: base64.b64encode(s.encode()).decode()
for mode in ("both", "exam"):
    server.generate_exam([{"name": "a.txt", "data": b64("tài liệu")}], [{"name": "mau.pdf", "data": b64("%PDF")}], 5, "mcq", "", mode)
    assert "de_mau_0.pdf" in seen["files"] and "de_mau_0.pdf" in seen["prompt"] and "bắt chước phong cách" in seen["prompt"], mode
server.generate_exam([{"name": "l6.pdf", "data": b64("%PDF 6")}, {"name": "l7.pdf", "data": b64("%PDF 7")},
                      {"name": "note.txt", "data": b64("ghi chú")}], [], 5, "mcq", "", "exam")
assert {"tai_lieu_0.pdf", "tai_lieu_1.pdf", "tai_lieu_2.txt"} <= set(seen["files"])
assert "tai_lieu_0.pdf, tai_lieu_1.pdf, tai_lieu_2.txt" in seen["prompt"]
d = server.generate_exam([{"name": "a.txt", "data": b64("x")}], [], 5, "mcq", "", "lesson")
assert d["questions"] == [] and "KHÔNG tạo câu hỏi" in seen["prompt"]
server.ask_claude = real_claude

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

# tạo đề lần 2 từ cùng tài liệu: câu của đề trước được đưa vào prompt để tránh lặp
made = []
def fake_unique(prompt, cwd):
    made.append(prompt)
    return json.dumps({"title": "T", "lesson": "", "questions": [
        {"type": "mcq", "question": f"Câu số {len(made)}", "options": ["a", "b"], "answer": 0}]})
server.ask_claude = fake_unique
gen = lambda name, text, who: call("/api/exams", {"mode": "exam", "n_questions": 1, "duration_min": 5,
                                                   "docs": [{"name": name, "data": b64(text)}]}, who)
assert gen("slide.pdf", "tài liệu A", alice)[0] == 200 and "ĐÃ CÓ" not in made[-1]
gen("doi_ten.pdf", "tài liệu A", alice)  # cùng nội dung, khác tên file
assert "- Câu số 1" in made[-1]
gen("slide.pdf", "tài liệu A", alice)
assert "- Câu số 1" in made[-1] and "- Câu số 2" in made[-1]
gen("khac.pdf", "tài liệu B", alice)  # tài liệu khác: không tránh
assert "ĐÃ CÓ" not in made[-1]
gen("slide.pdf", "tài liệu A", bob)  # người khác: không thấy đề của alice
assert "ĐÃ CÓ" not in made[-1]
server.ask_claude = real_claude

# mục chỉ có bài giảng: không làm bài, không tải đề được
with server.db() as c:
    lid = c.execute("INSERT INTO exams(user_id,title,duration_min,lesson,questions) VALUES(?,?,?,?,?)",
                    (uid, "Chỉ bài giảng", 5, "# Bài", "[]")).lastrowid
assert call(f"/api/exams/{lid}/submit", {"answers": []}, alice)[0] == 400
assert call(f"/api/exams/{lid}/export", cookie=alice)[0] == 400
items = {e["id"]: e for e in call("/api/exams", cookie=alice)[1]}
assert items[lid]["n"] == 0 and items[lid]["has_lesson"] == 1 and items[eid]["n"] == 2
assert call("/api/exams", {"docs": [{"name": "a.txt", "data": ""}], "mode": "xyz"}, alice)[0] == 400

call("/api/logout", {}, alice)
assert call("/api/me", cookie=alice)[0] == 401
httpd.shutdown()
print("OK - tất cả kiểm tra đều qua")
