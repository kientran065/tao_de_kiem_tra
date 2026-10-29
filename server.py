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
        CREATE TABLE IF NOT EXISTS subjects(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL,
            created TEXT DEFAULT (datetime('now','localtime')));
        CREATE TABLE IF NOT EXISTS docs(id INTEGER PRIMARY KEY, subject_id INTEGER NOT NULL, kind TEXT NOT NULL, name TEXT,
            pages INTEGER, digest TEXT, created TEXT DEFAULT (datetime('now','localtime')));
        """)
        # cột thêm sau: docs_hash nhận ra các đề tạo từ cùng tài liệu; subject_id là môn học của đề
        for col in ("docs_hash TEXT", "subject_id INTEGER"):
            try:
                c.execute(f"ALTER TABLE exams ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass  # đã có cột
        # dọn tên lỗi cũ (ký tự rác từ bookmark PDF); hàm replace của SQLite không xử lý được ký tự NUL nên làm bằng Python
        for doc_id, name in c.execute("SELECT id, name FROM docs").fetchall():
            clean = re.sub(r"[\x00-\x1f]", "", name or "").strip()
            if clean != name:
                c.execute("UPDATE docs SET name=? WHERE id=?", (clean, doc_id))


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


SYSTEM_PROMPT = "Bạn là giáo viên soạn bài giảng và đề kiểm tra. Làm đúng yêu cầu và trả về đúng định dạng được yêu cầu."


def ask_claude(prompt, cwd, tools="Read"):
    """Gọi Claude Code ở chế độ không tương tác, trả về text kết quả.
    tools="" khi không cần đọc file. Lời dặn hệ thống ngắn và bỏ MCP/plugin/settings: phí cố định mỗi lần gọi
    giảm từ ~43.000 xuống ~900-2.000 token (đo ngày 29/10/2026)."""
    if not CLAUDE:
        raise RuntimeError("Không tìm thấy lệnh 'claude'. Hãy cài Claude Code và đăng nhập.")
    # prompt đi qua stdin vì dòng lệnh Windows giới hạn ~8000 ký tự
    # realpath: thư mục tạm có thể là tên rút gọn kiểu C:\Users\TRANMA~1\..., Claude Code từ chối đọc file ở đường dẫn đó
    p = subprocess.run([CLAUDE, "-p", "--output-format", "json", "--tools", tools, "--system-prompt", SYSTEM_PROMPT,
                        "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence",
                        *(["--allowedTools", tools] if tools else [])],
                       input=prompt, capture_output=True, text=True, encoding="utf-8", cwd=os.path.realpath(cwd), timeout=900)
    try:
        out = json.loads(p.stdout)
    except json.JSONDecodeError:
        raise RuntimeError("Claude lỗi: " + (p.stderr or p.stdout)[:500])
    if out.get("is_error"):
        raise RuntimeError("Claude lỗi: " + str(out.get("result"))[:500])
    if out.get("permission_denials"):  # không đọc được file thì câu trả lời chỉ là lời giải thích, không được lưu
        raise RuntimeError("Claude không được phép đọc file tài liệu, hãy thử lại")
    u = out.get("usage", {})
    used = sum(u.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    print(f"[Claude] vào {used:,} token, ra {u.get('output_tokens', 0):,} token", flush=True)
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


def task_prompt(n_questions, qtype, notes, mode, avoid):
    """Phần "Nhiệm vụ" dùng chung cho tạo đề từ file và từ bản ghi nhớ môn học."""
    lesson_task = ("Viết một bài giảng tiếng Việt (Markdown) dễ hiểu, có cấu trúc, tóm tắt và giải thích nội dung chính của tài liệu."
                   if mode != "exam" else 'KHÔNG viết bài giảng: để "lesson" là chuỗi rỗng "".')
    avoid_task = ("""
Các câu hỏi dưới đây ĐÃ CÓ trong những đề trước tạo từ cùng tài liệu này. KHÔNG được lặp lại chúng:
không hỏi lại nguyên văn hay diễn đạt lại cùng một câu, câu tính toán không dùng lại bộ số liệu cũ.
Ưu tiên những ý kiến thức trong tài liệu mà các câu này chưa hỏi tới.
""" + "\n".join(f"- {q}" for q in avoid)) if avoid and mode != "lesson" else ""
    types = {"mcq": 'chỉ trắc nghiệm ("mcq")', "essay": 'chỉ câu tự trả lời ("numeric" hoặc "essay")',
             "mixed": "kết hợp trắc nghiệm với câu tự trả lời (numeric/essay) theo đúng tỷ lệ của đề mẫu; không có đề mẫu thì khoảng 70% trắc nghiệm"}[qtype]
    return f"""
Nhiệm vụ:
1. {lesson_task}
2. {f'''Tạo đúng {n_questions} câu hỏi kiểm tra, {types}, bám sát nội dung tài liệu.{STYLE_RULES}''' if mode != "lesson" else 'KHÔNG tạo câu hỏi: để "questions" là mảng rỗng [].'}
{("Yêu cầu thêm của người dùng (ưu tiên cao nhất): " + notes) if notes else ""}
{avoid_task}

CHỈ trả về một đối tượng JSON hợp lệ, không kèm chữ nào khác, theo đúng cấu trúc:
{{"title": "tên đề", "lesson": "bài giảng markdown",
 "questions": [
  {{"type": "mcq", "question": "...", "options": ["...", "...", "...", "..."], "answer": 0, "explanation": "..."}},
  {{"type": "numeric", "question": "...", "answer": 4.183, "tolerance": 0.01, "explanation": "cách tính"}},
  {{"type": "essay", "question": "...", "sample_answer": "...", "rubric": "tiêu chí chấm"}}
 ]}}
Với mcq, "answer" là chỉ số (bắt đầu từ 0) của đáp án đúng trong "options".
Câu mà đáp án là MỘT con số (điền đáp số) PHẢI dùng "numeric": "answer" là số (dấu chấm thập phân), "tolerance" là sai số cho phép
(tính tới việc làm tròn và tra bảng). Chỉ dùng "essay" khi cần trình bày lời giải hay lập luận."""


STYLE_RULES = """
   Bám sát đề mẫu và cách viết của giảng viên trong tài liệu (ưu tiên hơn cách viết quen thuộc của bạn):
   - Viết đề bằng ngôn ngữ của đề mẫu. Dùng LẠI ĐÚNG thuật ngữ, cụm từ, cách hỏi mà đề mẫu và tài liệu dùng, kể cả khi chúng
     khác cách dùng phổ biến (ví dụ tài liệu viết "statistical value", "significant level" thì không đổi thành
     "test statistic", "significance level").
   - Nếu đề mẫu cùng chủ đề với tài liệu: làm đề SONG SONG, câu thứ i cùng kiểu với câu thứ i của đề mẫu (cùng kỹ năng,
     dạng câu, cách trình bày) nhưng đổi bối cảnh và số liệu. Nếu khác chủ đề: giữ số câu từng dạng, cách hỏi, văn phong;
     nội dung lấy từ tài liệu.
   - Đặt câu hỏi qua tình huống thực tế và nhận định cần kiểm tra như đề mẫu và bài tập trong tài liệu, không viết trừu tượng
     kiểu "X ~ N(μ, σ²)" nếu đề mẫu không làm vậy. Dữ liệu nhiều nhóm thì trình bày bằng bảng Markdown; có thể cho vài câu
     dùng chung một bảng dữ liệu.
   - Phương án trắc nghiệm viết theo đúng khuôn mẫu phương án của đề mẫu.
   - Đề mẫu không cho sẵn giá trị tra bảng (giá trị tới hạn, xác suất) thì KHÔNG cho; người làm bài tự tra.
   - Giữ tỷ lệ câu khái niệm / tính toán / kết luận và độ khó như đề mẫu; không thêm lý thuyết nâng cao mà đề mẫu không hỏi.
   - Đáp án đúng của các câu trắc nghiệm phân bố đều giữa các vị trí A, B, C, D."""


def generate_exam(docs, samples, n_questions, qtype, notes, mode="both", avoid=()):
    """Tạo từ file gửi lên (Claude đọc lại file mỗi lần).
    mode: "both" (bài giảng + đề), "exam" (chỉ đề), "lesson" (chỉ bài giảng).
    avoid: câu hỏi của các đề trước tạo từ cùng tài liệu, không được lặp lại."""
    with tempfile.TemporaryDirectory() as tmp:
        doc_names = save_uploads(docs, tmp, "tai_lieu_")
        sample_names = save_uploads(samples, tmp, "de_mau_")
        prompt = f"""Dùng công cụ Read để đọc các file trong thư mục hiện tại:
- Tài liệu học: {", ".join(doc_names)}
- Đề kiểm tra mẫu: {", ".join(sample_names) or "(không có)"}
""" + task_prompt(n_questions, qtype, notes, mode, avoid)
        return check_generated(extract_json(ask_claude(prompt, tmp)), mode)


def generate_from_notes(docs, samples, n_questions, qtype, notes, mode="both", avoid=()):
    """Tạo từ bản ghi nhớ đã quét của môn học: không đọc lại file, không cần công cụ nên rẻ hơn nhiều.
    docs, samples: danh sách (tên, bản ghi nhớ)."""
    block = lambda tag, items: "\n".join(f'<{tag} ten="{n}">\n{d}\n</{tag}>' for n, d in items)
    prompt = f"""Tài liệu học dưới đây là bản ghi nhớ kiến thức đã được tóm tắt đầy đủ từ tài liệu gốc.
{block("tai_lieu", docs)}
{("Đề kiểm tra mẫu (chép lại từ đề gốc):" + chr(10) + block("de_mau", samples)) if samples else "Không có đề mẫu."}
""" + task_prompt(n_questions, qtype, notes, mode, avoid)
    with tempfile.TemporaryDirectory() as tmp:
        return check_generated(extract_json(ask_claude(prompt, tmp, tools="")), mode)


DIGEST_PROMPT = {
    "doc": """Dùng công cụ Read đọc file {name} trong thư mục hiện tại. Viết BẢN GHI NHỚ KIẾN THỨC của file để sau này
ra đề kiểm tra và viết bài giảng mà KHÔNG cần đọc lại file:
- Giữ ĐỦ mọi định nghĩa, công thức (viết bằng LaTeX gọn trong $...$), định lý và điều kiện áp dụng, bảng giá trị quan trọng.
- Giữ các ví dụ có số liệu kèm lời giải tóm tắt, và liệt kê các dạng bài tập có trong file.
- CHÉP NGUYÊN VĂN, đúng ngôn ngữ gốc, đề bài của các ví dụ và bài tập (đề thi thường viết theo đúng giọng này).
- Có một mục "Thuật ngữ và cách diễn đạt của giảng viên": liệt kê NGUYÊN VĂN theo ngôn ngữ gốc các thuật ngữ, cụm từ, cách đặt
  câu hỏi, cách viết bước giải và kết luận mà tài liệu dùng, kể cả khi khác cách dùng phổ biến. Không dịch các cụm này.
- Bỏ lời dẫn, trang trí, nội dung lặp lại. Phần giải thích viết cô đọng bằng tiếng Việt.
CHỈ trả về nội dung Markdown của bản ghi nhớ.""",
    "sample": """Dùng công cụ Read đọc file {name} trong thư mục hiện tại. Đây là một đề kiểm tra mẫu.
Mở đầu bằng một đoạn ngắn mô tả: cấu trúc đề, số câu từng dạng, độ khó, cách hỏi, thang điểm.
Sau đó chép lại NGUYÊN VĂN toàn bộ câu hỏi (kể cả phương án và số liệu), công thức viết bằng LaTeX gọn trong $...$.
Cuối cùng có mục "Văn phong đề": thuật ngữ đặc trưng (nguyên văn), cách đặt câu hỏi, khuôn mẫu phương án, có cho sẵn giá trị
tra bảng hay không, dữ liệu dạng bảng hay không, bối cảnh thực tế, dấu thập phân của đáp số.
CHỈ trả về nội dung Markdown.""",
}


def digest_doc(f, kind):
    """Quét một file một lần, trả về bản ghi nhớ để lưu vào môn học."""
    with tempfile.TemporaryDirectory() as tmp:
        name = save_uploads([f], tmp, "tai_lieu_")[0]
        text = ask_claude(DIGEST_PROMPT[kind].format(name=name), tmp).strip()
    if len(text) < 50:
        raise ValueError(f"Claude không đọc được nội dung file {f['name']}, hãy thử lại")
    return text


def check_generated(data, mode):
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
        if q.get("type") == "numeric":
            try:
                q["answer"] = float(q["answer"])
                q["tolerance"] = abs(float(q.get("tolerance") or 0)) or max(0.001, abs(q["answer"]) * 0.005)
            except (KeyError, TypeError, ValueError):
                raise ValueError("Claude trả về câu điền đáp số không hợp lệ, hãy thử lại")
    return data


def parse_number(s):
    """Đáp số người làm bài gõ: chấp nhận "4,183" (dấu phẩy thập phân như Moodle), "4.183", "-34.66", "1 250,5"."""
    s = str(s).strip().replace(" ", "")
    if "," in s and "." in s:  # 1.250,5 hoặc 1,250.5: dấu đứng sau là dấu thập phân
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    return float(s.replace(",", "."))


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
        elif q["type"] == "numeric":  # chấm ngay trên máy, không tốn lượt Claude
            try:
                ok = abs(parse_number(a) - q["answer"]) <= q["tolerance"]
            except (TypeError, ValueError):
                ok = False
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
            graded = {g["id"]: g for g in extract_json(ask_claude(prompt, tmp, tools=""))}
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
        elif q["type"] == "numeric":
            body.append("<p>Answer: ....................</p>")
        else:
            body.append("<p>" + "." * 400 + "</p>")
    if with_key:
        body.append("<br style='page-break-before:always'><h2>Đáp án</h2>")
        for i, q in enumerate(qs, 1):
            ans = ("ABCDEFGH"[q["answer"]] if q["type"] == "mcq" else f"{q['answer']:g} (sai số ±{q['tolerance']:g})"
                   if q["type"] == "numeric" else q.get("sample_answer", ""))
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
            m = re.fullmatch(r"/api/(exams|attempts|subjects|docs)(?:/(\d+))?(?:/(\w+))?", path)
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

    @staticmethod
    def gen_params(b):
        """Đọc và kiểm tra các lựa chọn tạo đề: (số câu, phút, kiểu câu hỏi, yêu cầu thêm, chế độ)."""
        n, minutes, mode, qtype = int(b.get("n_questions", 10)), int(b.get("duration_min", 15)), b.get("mode", "both"), b.get("qtype", "mcq")
        if mode not in ("both", "exam", "lesson") or qtype not in ("mcq", "essay", "mixed"):
            raise ValueError("Lựa chọn không hợp lệ")
        if not (1 <= n <= 100 and 1 <= minutes <= 300):
            raise ValueError("Số câu 1-100, thời gian 1-300 phút")
        return n, minutes, qtype, b.get("notes", "").strip(), mode

    @staticmethod
    def avoid_list(u, fp):
        with db() as c:
            rows = c.execute("SELECT questions FROM exams WHERE user_id=? AND docs_hash=? ORDER BY id DESC", (u["id"], fp)).fetchall()
        # shortcut: chỉ gửi 150 câu gần nhất cho prompt khỏi quá dài; cần hơn thì tóm tắt theo chủ đề
        return [q["question"] for r in rows for q in json.loads(r["questions"])][:150]

    @staticmethod
    def save_exam(u, data, minutes, fp, subject_id=None):
        with db() as c:
            return c.execute("INSERT INTO exams(user_id,title,duration_min,lesson,questions,docs_hash,subject_id) VALUES(?,?,?,?,?,?,?)",
                             (u["id"], data.get("title", "Đề kiểm tra"), minutes, data.get("lesson", ""),
                              json.dumps(data["questions"], ensure_ascii=False), fp, subject_id)).lastrowid

    def exams_post(self, u, id_, action, q):
        b = self.body()
        if id_ is None:
            if not b.get("docs"):
                raise ValueError("Cần tải lên ít nhất một tài liệu")
            n, minutes, qtype, notes, mode = self.gen_params(b)
            fp = docs_fingerprint(b["docs"])
            data = generate_exam(b["docs"], b.get("samples", []), n, qtype, notes, mode, self.avoid_list(u, fp))
            return self.send(200, {"id": self.save_exam(u, data, minutes, fp)})
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

    # ---------- Môn học: quét tài liệu một lần, ra đề nhiều lần từ bản ghi nhớ ----------
    def get_subject(self, u, id_):
        with db() as c:
            s = c.execute("SELECT * FROM subjects WHERE id=? AND user_id=?", (id_, u["id"])).fetchone()
        if not s:
            raise ValueError("Không tìm thấy môn học")
        return s

    def subjects_get(self, u, id_, action, q):
        with db() as c:
            if id_ is None:
                rows = c.execute("""SELECT s.id, s.name, s.created,
                    (SELECT COUNT(*) FROM docs d WHERE d.subject_id=s.id AND d.kind='doc') n_docs,
                    (SELECT COUNT(*) FROM docs d WHERE d.subject_id=s.id AND d.kind='sample') n_samples,
                    (SELECT COUNT(*) FROM exams e WHERE e.subject_id=s.id) n_exams
                    FROM subjects s WHERE user_id=? ORDER BY id DESC""", (u["id"],)).fetchall()
                return self.send(200, [dict(r) for r in rows])
            s = self.get_subject(u, id_)
            docs = c.execute("SELECT id, kind, name, pages, digest, created FROM docs WHERE subject_id=? ORDER BY id",
                             (id_,)).fetchall()
        self.send(200, {"id": s["id"], "name": s["name"], "docs": [dict(d) for d in docs]})

    def subjects_post(self, u, id_, action, q):
        b = self.body()
        if id_ is None:
            name = b.get("name", "").strip()
            if not 1 <= len(name) <= 100:
                raise ValueError("Tên môn học 1-100 ký tự")
            with db() as c:
                sid = c.execute("INSERT INTO subjects(user_id,name) VALUES(?,?)", (u["id"], name)).lastrowid
            return self.send(200, {"id": sid})
        self.get_subject(u, id_)
        if action == "docs":
            kind = b.get("kind")
            if kind not in DIGEST_PROMPT or not b.get("data"):
                raise ValueError("Thiếu file hoặc loại tài liệu không hợp lệ")
            # "file" là tên file thật (có đuôi để biết loại), "name" là tên hiển thị như "Lec 6. HYPOTHESIS TESTING"
            digest = digest_doc({"name": b.get("file") or b.get("name", ""), "data": b["data"]}, kind)
            name = re.sub(r"[\x00-\x1f]", "", b.get("name", "")).strip()[:200]  # tên lấy từ bookmark PDF có thể chứa ký tự rác
            with db() as c:
                doc_id = c.execute("INSERT INTO docs(subject_id,kind,name,pages,digest) VALUES(?,?,?,?,?)",
                                   (id_, kind, name, b.get("pages"), digest)).lastrowid
            return self.send(200, {"id": doc_id})
        if action == "generate":
            n, minutes, qtype, notes, mode = self.gen_params(b)
            ids = sorted({int(i) for i in b.get("doc_ids", [])}), sorted({int(i) for i in b.get("sample_ids", [])})
            with db() as c:
                rows = {r["id"]: r for r in c.execute("SELECT id, kind, name, digest FROM docs WHERE subject_id=?", (id_,))}
            if not ids[0] or any(i not in rows for i in ids[0] + ids[1]):
                raise ValueError("Chọn ít nhất một tài liệu của môn này")
            fp = "mon:" + ",".join(map(str, ids[0]))  # cùng bộ tài liệu đã chọn -> tránh lặp câu của các đề trước
            data = generate_from_notes([(rows[i]["name"], rows[i]["digest"]) for i in ids[0]],
                                       [(rows[i]["name"], rows[i]["digest"]) for i in ids[1]],
                                       n, qtype, notes, mode, self.avoid_list(u, fp))
            return self.send(200, {"id": self.save_exam(u, data, minutes, fp, id_)})
        self.send(404, {"error": "Không tìm thấy"})

    def subjects_delete(self, u, id_, action, q):
        self.get_subject(u, id_)
        with db() as c:  # giữ lại các đề đã tạo, chỉ bỏ liên kết với môn
            c.execute("UPDATE exams SET subject_id=NULL WHERE subject_id=?", (id_,))
            c.execute("DELETE FROM docs WHERE subject_id=?", (id_,))
            c.execute("DELETE FROM subjects WHERE id=?", (id_,))
        self.send(200, {})

    def docs_delete(self, u, id_, action, q):
        with db() as c:
            if not c.execute("SELECT 1 FROM docs d JOIN subjects s ON s.id=d.subject_id WHERE d.id=? AND s.user_id=?",
                             (id_, u["id"])).fetchone():
                raise ValueError("Không tìm thấy tài liệu")
            c.execute("DELETE FROM docs WHERE id=?", (id_,))
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
