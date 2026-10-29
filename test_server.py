"""Tự kiểm tra (không gọi Claude): python test_server.py"""
import json, os, tempfile, threading, urllib.request, urllib.error
import server

server.DB_PATH = os.path.join(tempfile.mkdtemp(), "test.db")
server.init_db()

# tên tài liệu lỗi (ký tự NUL từ bookmark PDF) được dọn khi khởi động
with server.db() as c:
    c.execute("INSERT INTO docs(subject_id,kind,name,digest) VALUES(1,'doc',?,'x')", (" 8: Intervals\x00\x00\x00",))
server.init_db()
with server.db() as c:
    assert c.execute("SELECT name FROM docs").fetchone()[0] == "8: Intervals"
    c.execute("DELETE FROM docs")

# chấm trắc nghiệm
qs = [{"type": "mcq", "question": "1+1", "options": ["1", "2"], "answer": 1},
      {"type": "mcq", "question": "2+2", "options": ["4", "5"], "answer": 0},
      {"type": "essay", "question": "Giải thích"}]
total, res = server.grade(qs, [1, 1, ""])
assert round(total, 2) == 3.33 and res[0]["correct"] and not res[1]["correct"] and res[2]["score"] == 0, (total, res)

assert server.extract_json('Đây:\n```json\n{"a": [1]}\n```') == {"a": [1]}

# câu điền đáp số: chấm trên máy, nhận dấu phẩy thập phân kiểu Moodle
assert [server.parse_number(s) for s in ("4,183", "4.183", "-34,66", "1 250,5", "1,250.5", "1.250,5")] == [4.183, 4.183, -34.66, 1250.5, 1250.5, 1250.5]
num = server.check_generated({"questions": [{"type": "numeric", "question": "Z?", "answer": "4.183", "tolerance": 0.01},
                                            {"type": "numeric", "question": "p?", "answer": 0.129}]}, "exam")["questions"]
assert num[0]["answer"] == 4.183 and num[1]["tolerance"] == 0.001  # thiếu sai số -> mặc định
total, res = server.grade(num, ["4,19", "0,2"])
assert res[0]["correct"] and not res[1]["correct"] and round(total, 2) == 5.0, res
assert server.grade(num, ["abc", None])[0] == 0
try:
    server.check_generated({"questions": [{"type": "numeric", "question": "?", "answer": "khoảng 4"}]}, "exam"); assert False
except ValueError:
    pass
assert "Answer:" in server.export_doc({"title": "T", "duration_min": 5, "questions": json.dumps(num)}, True).decode()
doc = server.export_doc({"title": "<T>", "duration_min": 5, "questions": json.dumps(qs)}, True).decode()
assert "&lt;T&gt;" in doc and "Đáp án" in doc and "B" in doc

# đề mẫu được lưu và đưa vào prompt (thay Claude bằng hàm giả)
import base64
seen = {}
def fake_claude(prompt, cwd, tools="Read"):
    seen["prompt"], seen["files"] = prompt, sorted(os.listdir(cwd))
    return json.dumps({"title": "T", "lesson": "# L", "questions": qs[:1]})
real_claude, server.ask_claude = server.ask_claude, fake_claude
b64 = lambda s: base64.b64encode(s.encode()).decode()
for mode in ("both", "exam"):
    server.generate_exam([{"name": "a.txt", "data": b64("tài liệu")}], [{"name": "mau.pdf", "data": b64("%PDF")}], 5, "mcq", "", mode)
    assert "de_mau_0.pdf" in seen["files"] and "de_mau_0.pdf" in seen["prompt"] and "Bám sát đề mẫu" in seen["prompt"] and "statistical value" in seen["prompt"], mode
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
def fake_unique(prompt, cwd, tools="Read"):
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

# môn học: quét tài liệu một lần thành bản ghi nhớ, ra đề từ bản ghi nhớ (không đọc lại file, không dùng công cụ)
calls = []
def fake_subject(prompt, cwd, tools="Read"):
    calls.append((prompt, tools, sorted(os.listdir(cwd))))
    if "BẢN GHI NHỚ" in prompt:
        return "## Ghi nhớ Lecture 6\nKiểm định: $Z = (\\bar x - \\mu_0)/(\\sigma/\\sqrt n)$ " + "x" * 50
    if "đề kiểm tra mẫu" in prompt:
        return "Đề mẫu gồm 2 câu. Câu 1: Tính $Z$ với n = 25. " + "y" * 50
    return json.dumps({"title": "Đề môn", "lesson": "", "questions": [
        {"type": "mcq", "question": f"Câu môn {len(calls)}", "options": ["a", "b"], "answer": 1}]})
server.ask_claude = fake_subject
s, r, _ = call("/api/subjects", {"name": "Thống kê toán"}, alice); sid = r["id"]
assert call("/api/subjects", {"name": " "}, alice)[0] == 400
s, d1, _ = call(f"/api/subjects/{sid}/docs", {"kind": "doc", "name": "Lec 6. HYPOTHESIS TESTING", "file": "Lec 6. HYPOTHESIS TESTING.pdf",
                                               "data": b64("%PDF lec6"), "pages": 28}, alice)
assert s == 200 and calls[-1][1] == "Read" and calls[-1][2] == ["tai_lieu_0.pdf"]
s, d2, _ = call(f"/api/subjects/{sid}/docs", {"kind": "sample", "name": "de.pdf", "data": b64("%PDF de")}, alice)
subj = call(f"/api/subjects/{sid}", cookie=alice)[1]
assert [d["kind"] for d in subj["docs"]] == ["doc", "sample"] and "Ghi nhớ Lecture 6" in subj["docs"][0]["digest"]
body = {"doc_ids": [d1["id"]], "sample_ids": [d2["id"]], "mode": "exam", "n_questions": 1, "duration_min": 5}
s, e1, _ = call(f"/api/subjects/{sid}/generate", body, alice)
prompt, tools, files = calls[-1]
assert s == 200 and tools == "" and files == [], (tools, files)  # không đọc file, không công cụ
assert "Ghi nhớ Lecture 6" in prompt and "Đề mẫu gồm 2 câu" in prompt and "ĐÃ CÓ" not in prompt
call(f"/api/subjects/{sid}/generate", body, alice)
assert "- Câu môn 3" in calls[-1][0]  # lần 2 tránh câu của lần 1
assert call(f"/api/subjects/{sid}/generate", {**body, "doc_ids": [999]}, alice)[0] == 400
assert call(f"/api/subjects/{sid}", cookie=bob)[0] == 400  # bob không thấy môn của alice
assert call(f"/api/subjects/{sid}/generate", body, bob)[0] == 400
assert call(f"/api/docs/{d1['id']}", cookie=bob, method="DELETE")[0] == 400
lst = call("/api/subjects", cookie=alice)[1][0]
assert (lst["n_docs"], lst["n_samples"], lst["n_exams"]) == (1, 1, 2)
assert call(f"/api/docs/{d2['id']}", cookie=alice, method="DELETE")[0] == 200
assert call(f"/api/subjects/{sid}", cookie=alice, method="DELETE")[0] == 200
assert call(f"/api/exams/{e1['id']}", cookie=alice)[0] == 200  # xoá môn vẫn giữ đề đã tạo
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
