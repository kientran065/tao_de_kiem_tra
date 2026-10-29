"""Trang tạo đề & bài giảng chạy local, dùng Claude Code (`claude -p`) làm AI.
Chạy: python server.py  rồi mở http://localhost:8000
Chỉ dùng thư viện chuẩn của Python."""
import base64, hashlib, html, json, os, re, secrets, shutil, sqlite3, subprocess, tempfile, zipfile
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from http.cookies import SimpleCookie
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "data.db")
PORT = int(os.environ.get("PORT", 8000))
MAX_UPLOAD = 100 * 1024 * 1024  # tổng request; base64 làm file to thêm ~1/3 nên ứng với ~70MB file (MAX_MB ở index.html)


# ---------- Database ----------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL, salt TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS exams(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, title TEXT, duration_min INTEGER,
            lesson TEXT, questions TEXT, created TEXT DEFAULT (datetime('now','localtime')));
        CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY, exam_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            answers TEXT, result TEXT, score REAL, created TEXT DEFAULT (datetime('now','localtime')));
        """)
        try:  # cột thêm sau: dấu vân tay tài liệu, để nhận ra các đề tạo từ cùng tài liệu
            c.execute("ALTER TABLE exams ADD COLUMN docs_hash TEXT")
        except sqlite3.OperationalError:
            pass  # đã có cột


def hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()


def docs_fingerprint(docs):
    """Cùng bộ file tài liệu (không phụ thuộc tên file hay thứ tự chọn) cho cùng một mã."""
    parts = sorted(hashlib.sha256(base64.b64decode(f["data"])).hexdigest() for f in docs)
    return hashlib.sha256("".join(parts).encode()).hexdigest()


# ---------- Claude ----------
if os.name == "nt":
    # cửa sổ mở từ Explorer giữ PATH cũ lúc đăng nhập; đọc lại PATH mới nhất để thấy claude và node
    import winreg
    def _reg_path(root, key):
        try:
            with winreg.OpenKey(root, key) as k:
                return os.path.expandvars(winreg.QueryValueEx(k, "Path")[0])
        except OSError:
            return ""
    os.environ["PATH"] = ";".join(filter(None, (os.environ.get("PATH"),
        _reg_path(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        _reg_path(winreg.HKEY_CURRENT_USER, "Environment"))))
CLAUDE = shutil.which("claude")
if not CLAUDE and os.name == "nt":
    # Claude Code cài từ trong app Claude desktop (gói MSIX) bị Windows chuyển AppData\Roaming vào thư mục riêng của app,
    # nên cửa sổ mở từ Explorer không thấy. shortcut: phụ thuộc thư mục của app, cài lại bằng npm ngoài app thì bỏ được
    import glob
    CLAUDE = next(iter(glob.glob(os.path.expandvars(r"%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\npm\claude.cmd"))), None)


def ask_claude(prompt, cwd):
    """Gọi Claude Code ở chế độ không tương tác, trả về text kết quả."""
    if not CLAUDE:
        raise RuntimeError("Không tìm thấy lệnh 'claude'. Hãy cài Claude Code và đăng nhập.")
    # prompt đi qua stdin vì dòng lệnh Windows giới hạn ~8000 ký tự
    p = subprocess.run([CLAUDE, "-p", "--output-format", "json", "--allowedTools", "Read"],
                       input=prompt, capture_output=True, text=True, encoding="utf-8", cwd=cwd, timeout=900)
    try:
        out = json.loads(p.stdout)
    except json.JSONDecodeError:
        raise RuntimeError("Claude lỗi: " + (p.stderr or p.stdout)[:500])
    if out.get("is_error"):
        raise RuntimeError("Claude lỗi: " + str(out.get("result"))[:500])
    return out["result"]


def extract_json(text):
    """Lấy khối JSON từ câu trả lời (Claude đôi khi bọc trong ```json)."""
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        raise ValueError("Claude không trả về JSON")
    start = min(starts)
    end = max(text.rfind("}"), text.rfind("]"))
    return json.loads(text[start:end + 1])


def docx_text(path):
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    xml = re.sub(r"</w:p>", "\n", xml)
    return html.unescape(re.sub(r"<[^>]+>", "", xml))


def save_uploads(files, folder, prefix):
    """Lưu file người dùng gửi; .docx đổi sang .txt để Claude đọc được. Trả về tên file."""
    names = []
    for i, f in enumerate(files):
        ext = os.path.splitext(f["name"])[1].lower()
        if ext not in (".pdf", ".txt", ".md", ".docx"):
            raise ValueError(f"Không hỗ trợ file {f['name']} (chỉ PDF, DOCX, TXT, MD)")
        path = os.path.join(folder, f"{prefix}{i}{ext}")
        with open(path, "wb") as out:
            out.write(base64.b64decode(f["data"]))
        if ext == ".docx":
            txt = path[:-5] + ".txt"
            with open(txt, "w", encoding="utf-8") as out:
                out.write(docx_text(path))
            path = txt
        names.append(os.path.basename(path))
    return names


def generate_exam(docs, samples, n_questions, qtype, notes, mode="both", avoid=()):
    """mode: "both" (bài giảng + đề), "exam" (chỉ đề), "lesson" (chỉ bài giảng).
    avoid: câu hỏi của các đề trước tạo từ cùng tài liệu, không được lặp lại."""
    lesson_task = ("Viết một bài giảng tiếng Việt (Markdown) dễ hiểu, có cấu trúc, tóm tắt và giải thích nội dung chính của tài liệu."
                   if mode != "exam" else 'KHÔNG viết bài giảng: để "lesson" là chuỗi rỗng "".')
    avoid_task = ("""
Các câu hỏi dưới đây ĐÃ CÓ trong những đề trước tạo từ cùng tài liệu này. KHÔNG được lặp lại chúng:
không hỏi lại nguyên văn hay diễn đạt lại cùng một câu, câu tính toán không dùng lại bộ số liệu cũ.
Ưu tiên những ý kiến thức trong tài liệu mà các câu này chưa hỏi tới.
""" + "\n".join(f"- {q}" for q in avoid)) if avoid and mode != "lesson" else ""
    with tempfile.TemporaryDirectory() as tmp:
        doc_names = save_uploads(docs, tmp, "tai_lieu_")
        sample_names = save_uploads(samples, tmp, "de_mau_")
        types = {"mcq": 'chỉ trắc nghiệm ("mcq")', "essay": 'chỉ tự luận ("essay")',
                 "mixed": "kết hợp trắc nghiệm và tự luận (khoảng 70% trắc nghiệm)"}[qtype]
        prompt = f"""Bạn là giáo viên. Dùng công cụ Read để đọc các file trong thư mục hiện tại:
- Tài liệu học: {", ".join(doc_names)}
- Đề kiểm tra mẫu: {", ".join(sample_names) or "(không có)"}

Nhiệm vụ:
1. {lesson_task}
2. {f'''Tạo đúng {n_questions} câu hỏi kiểm tra, {types}, bám sát nội dung tài liệu.
   Nếu có đề mẫu, bắt chước phong cách, độ khó và cách hỏi của đề mẫu.''' if mode != "lesson" else 'KHÔNG tạo câu hỏi: để "questions" là mảng rỗng [].'}
{("Yêu cầu thêm của người dùng: " + notes) if notes else ""}
{avoid_task}

CHỈ trả về một đối tượng JSON hợp lệ, không kèm chữ nào khác, theo đúng cấu trúc:
{{"title": "tên đề", "lesson": "bài giảng markdown",
 "questions": [
  {{"type": "mcq", "question": "...", "options": ["...", "...", "...", "..."], "answer": 0, "explanation": "..."}},
  {{"type": "essay", "question": "...", "sample_answer": "...", "rubric": "tiêu chí chấm"}}
 ]}}
Với mcq, "answer" là chỉ số (bắt đầu từ 0) của đáp án đúng trong "options"."""
        data = extract_json(ask_claude(prompt, tmp))
    if mode == "lesson":
        data["questions"] = []
        if not data.get("lesson"):
            raise ValueError("Claude không tạo được bài giảng, hãy thử lại")
        return data
    if mode == "exam":
        data["lesson"] = ""
    qs = data.get("questions")
    if not isinstance(qs, list) or not qs:
        raise ValueError("Claude không tạo được câu hỏi")
    for q in qs:
        if q.get("type") == "mcq" and not (isinstance(q.get("answer"), int) and 0 <= q["answer"] < len(q.get("options", []))):
            raise ValueError("Claude trả về câu trắc nghiệm không hợp lệ, hãy thử lại")
    return data


def grade(questions, answers):
    """Chấm thang 10, mỗi câu bằng điểm. Trắc nghiệm chấm ngay, tự luận nhờ Claude."""
    per_q = 10 / len(questions)
    results = [None] * len(questions)
    essays = []
    for i, q in enumerate(questions):
        a = answers[i] if i < len(answers) else None
        if q["type"] == "mcq":
            ok = a == q["answer"]
            results[i] = {"score": per_q if ok else 0, "correct": ok, "feedback": q.get("explanation", "")}
        elif not (isinstance(a, str) and a.strip()):
            results[i] = {"score": 0, "feedback": "Không trả lời."}
        else:
            essays.append(i)
    if essays:
        items = [{"id": i, "question": questions[i]["question"], "sample_answer": questions[i].get("sample_answer", ""),
                  "rubric": questions[i].get("rubric", ""), "student_answer": answers[i]} for i in essays]
        prompt = ("Chấm các câu tự luận sau, mỗi câu thang điểm 0 đến 10 (cho phép số lẻ như 7.5), "
                  "nhận xét ngắn bằng tiếng Việt, chỉ ra chỗ đúng/sai.\n"
                  + json.dumps(items, ensure_ascii=False) +
                  '\nCHỈ trả về JSON dạng: [{"id": 0, "score": 7.5, "feedback": "..."}]')
        with tempfile.TemporaryDirectory() as tmp:
            graded = {g["id"]: g for g in extract_json(ask_claude(prompt, tmp))}
        for i in essays:
            g = graded.get(i, {"score": 0, "feedback": "Không chấm được câu này."})
            s = min(max(float(g.get("score", 0)), 0), 10)
            results[i] = {"score": s / 10 * per_q,"feedback": g.get("feedback", "")}
    total = round(sum(r["score"] for r in results), 2)
    return total, results


def export_doc(exam, with_key):
    # shortcut: HTML lưu đuôi .doc (Word mở được); cần .docx thật thì dùng thư viện python-docx
    qs = json.loads(exam["questions"])
    e = html.escape
    body = [f"<h1>{e(exam['title'])}</h1><p>Thời gian: {exam['duration_min']} phút &nbsp;|&nbsp; Họ tên: ........................</p>"]
    for i, q in enumerate(qs, 1):
        body.append(f"<p><b>Câu {i}.</b> {e(q['question'])}</p>")
        if q["type"] == "mcq":
            body += [f"<p style='margin-left:24px'>{'ABCDEFGH'[j]}. {e(o)}</p>" for j, o in enumerate(q["options"])]
        else:
            body.append("<p>" + "." * 400 + "</p>")
    if with_key:
        body.append("<br style='page-break-before:always'><h2>Đáp án</h2>")
        for i, q in enumerate(qs, 1):
            ans = "ABCDEFGH"[q["answer"]] if q["type"] == "mcq" else q.get("sample_answer", "")
            body.append(f"<p><b>Câu {i}:</b> {e(ans)}<br><i>{e(q.get('explanation') or q.get('rubric') or '')}</i></p>")
    return ("<html><head><meta charset='utf-8'></head><body style='font-family:Times New Roman;font-size:13pt'>"
            + "".join(body) + "</body></html>").encode("utf-8")


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    def send(self, code, data=None, ctype="application/json", headers=None):
        body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith(("text", "application/json")) else ""))
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_UPLOAD:
            raise ValueError("Tổng dung lượng file quá lớn (tối đa khoảng 70MB)")
        return json.loads(self.rfile.read(n) or b"{}")

    def user(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        if "token" not in c:
            return None
        with db() as conn:
            r = conn.execute("SELECT u.id, u.username FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?",
                             (c["token"].value,)).fetchone()
        return dict(r) if r else None

    def do_GET(self): self.route("GET")
    def do_POST(self): self.route("POST")
    def do_DELETE(self): self.route("DELETE")

    def route(self, method):
        url = urlparse(self.path)
        path = url.path
        try:
            if method == "GET" and path in ("/", "/index.html"):
                with open(os.path.join(ROOT, "index.html"), "rb") as f:
                    return self.send(200, f.read(), "text/html")
            if path == "/api/register" and method == "POST":
                return self.register()
            if path == "/api/login" and method == "POST":
                return self.login()
            u = self.user()
            if not u:
                return self.send(401, {"error": "Chưa đăng nhập"})
            m = re.fullmatch(r"/api/(exams|attempts)(?:/(\d+))?(?:/(\w+))?", path)
            if path == "/api/me":
                return self.send(200, u)
            if path == "/api/logout" and method == "POST":
                with db() as c:
                    c.execute("DELETE FROM sessions WHERE user_id=?", (u["id"],))
                return self.send(200, {}, headers={"Set-Cookie": "token=; Path=/; Max-Age=0"})
            if m:
                kind, id_, action = m.group(1), m.group(2) and int(m.group(2)), m.group(3)
                return getattr(self, f"{kind}_{method.lower()}")(u, id_, action, parse_qs(url.query))
            self.send(404, {"error": "Không tìm thấy"})
        except (ValueError, RuntimeError, KeyError, subprocess.TimeoutExpired) as e:
            self.send(400, {"error": str(e)})

    def register(self):
        b = self.body()
        name, pw = b.get("username", "").strip(), b.get("password", "")
        if not re.fullmatch(r"[\w.]{3,30}", name) or len(pw) < 6:
            raise ValueError("Tên đăng nhập 3-30 ký tự (chữ, số, _ .), mật khẩu ít nhất 6 ký tự")
        salt = secrets.token_hex(16)
        try:
            with db() as c:
                c.execute("INSERT INTO users(username,pw_hash,salt) VALUES(?,?,?)", (name, hash_pw(pw, salt), salt))
        except sqlite3.IntegrityError:
            raise ValueError("Tên đăng nhập đã tồn tại")
        self.login(b)

    def login(self, b=None):
        b = b or self.body()
        with db() as c:
            r = c.execute("SELECT * FROM users WHERE username=?", (b.get("username", "").strip(),)).fetchone()
            if not r or not secrets.compare_digest(hash_pw(b.get("password", ""), r["salt"]), r["pw_hash"]):
                return self.send(401, {"error": "Sai tên đăng nhập hoặc mật khẩu"})
            token = secrets.token_urlsafe(32)
            c.execute("INSERT INTO sessions VALUES(?,?)", (token, r["id"]))
        self.send(200, {"username": r["username"]},
                  headers={"Set-Cookie": f"token={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000"})

    def get_exam(self, u, id_):
        with db() as c:
            r = c.execute("SELECT * FROM exams WHERE id=? AND user_id=?", (id_, u["id"])).fetchone()
        if not r:
            raise ValueError("Không tìm thấy đề")
        return r

    def exams_get(self, u, id_, action, q):
        if id_ is None:
            with db() as c:
                rows = c.execute("""SELECT e.id, e.title, e.duration_min, e.created, json_array_length(e.questions) n, length(e.lesson) > 0 has_lesson,
                    (SELECT COUNT(*) FROM attempts a WHERE a.exam_id=e.id) attempts,
                    (SELECT MAX(score) FROM attempts a WHERE a.exam_id=e.id) best
                    FROM exams e WHERE user_id=? ORDER BY id DESC""", (u["id"],)).fetchall()
            return self.send(200, [dict(r) for r in rows])
        e = self.get_exam(u, id_)
        if action == "export" and e["questions"] == "[]":
            raise ValueError("Mục này chỉ có bài giảng, không có đề để tải")
        if action == "export":
            key = q.get("key") == ["1"]
            fname = f"de_{id_}{'_dap_an' if key else ''}.doc"
            return self.send(200, export_doc(e, key), "application/msword",
                             {"Content-Disposition": f'attachment; filename="{fname}"'})
        qs = json.loads(e["questions"])
        # không gửi đáp án xuống trình duyệt khi làm bài
        public = [{k: v for k, v in x.items() if k in ("type", "question", "options")} for x in qs]
        self.send(200, {"id": e["id"], "title": e["title"], "duration_min": e["duration_min"],
                        "lesson": e["lesson"], "questions": public})

    def exams_post(self, u, id_, action, q):
        b = self.body()
        if id_ is None:
            if not b.get("docs"):
                raise ValueError("Cần tải lên ít nhất một tài liệu")
            n = int(b.get("n_questions", 10))
            minutes = int(b.get("duration_min", 15))
            mode = b.get("mode", "both")
            if mode not in ("both", "exam", "lesson") or b.get("qtype", "mcq") not in ("mcq", "essay", "mixed"):
                raise ValueError("Lựa chọn không hợp lệ")
            if not (1 <= n <= 100 and 1 <= minutes <= 300):
                raise ValueError("Số câu 1-100, thời gian 1-300 phút")
            fp = docs_fingerprint(b["docs"])
            with db() as c:
                rows = c.execute("SELECT questions FROM exams WHERE user_id=? AND docs_hash=? ORDER BY id DESC",
                                 (u["id"], fp)).fetchall()
            # shortcut: chỉ gửi 150 câu gần nhất cho prompt khỏi quá dài; cần hơn thì tóm tắt theo chủ đề
            avoid = [q["question"] for r in rows for q in json.loads(r["questions"])][:150]
            data = generate_exam(b["docs"], b.get("samples", []), n, b.get("qtype", "mcq"), b.get("notes", "").strip(), mode, avoid)
            with db() as c:
                cur = c.execute("INSERT INTO exams(user_id,title,duration_min,lesson,questions,docs_hash) VALUES(?,?,?,?,?,?)",
                                (u["id"], data.get("title", "Đề kiểm tra"), minutes, data.get("lesson", ""),
                                 json.dumps(data["questions"], ensure_ascii=False), fp))
            return self.send(200, {"id": cur.lastrowid})
        if action == "submit":
            e = self.get_exam(u, id_)
            qs = json.loads(e["questions"])
            if not qs:
                raise ValueError("Mục này chỉ có bài giảng, không có đề để làm")
            answers = b.get("answers", [])
            score, results = grade(qs, answers)
            with db() as c:
                cur = c.execute("INSERT INTO attempts(exam_id,user_id,answers,result,score) VALUES(?,?,?,?,?)",
                                (id_, u["id"], json.dumps(answers, ensure_ascii=False),
                                 json.dumps(results, ensure_ascii=False), score))
            return self.send(200, {"id": cur.lastrowid})
        self.send(404, {"error": "Không tìm thấy"})

    def exams_delete(self, u, id_, action, q):
        self.get_exam(u, id_)
        with db() as c:
            c.execute("DELETE FROM attempts WHERE exam_id=?", (id_,))
            c.execute("DELETE FROM exams WHERE id=?", (id_,))
        self.send(200, {})

    def attempts_get(self, u, id_, action, q):
        with db() as c:
            if id_ is None:
                rows = c.execute("""SELECT a.id, a.exam_id, a.score, a.created, e.title FROM attempts a
                    JOIN exams e ON e.id=a.exam_id WHERE a.user_id=? ORDER BY a.id DESC""", (u["id"],)).fetchall()
                return self.send(200, [dict(r) for r in rows])
            a = c.execute("""SELECT a.*, e.title, e.questions FROM attempts a JOIN exams e ON e.id=a.exam_id
                WHERE a.id=? AND a.user_id=?""", (id_, u["id"])).fetchone()
        if not a:
            raise ValueError("Không tìm thấy bài làm")
        self.send(200, {"id": a["id"], "exam_id": a["exam_id"], "title": a["title"], "score": a["score"],
                        "created": a["created"], "questions": json.loads(a["questions"]),
                        "answers": json.loads(a["answers"]), "results": json.loads(a["result"])})


if __name__ == "__main__":
    init_db()
    # trên Windows, reuse_address cho phép 2 server cùng chiếm cổng, bản cũ sẽ "nuốt" request
    ThreadingHTTPServer.allow_reuse_address = os.name != "nt"
    try:
        # chỉ nghe trên máy này: Claude chạy bằng tài khoản của bạn, không nên mở ra mạng
        httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        raise SystemExit(f"Cổng {PORT} đang bận: trang web có thể đã chạy ở cửa sổ khác. Hãy đóng cửa sổ đó rồi chạy lại.")
    print(f"Mở http://localhost:{PORT}  (Claude: {CLAUDE or 'KHÔNG TÌM THẤY'})")
    httpd.serve_forever()
