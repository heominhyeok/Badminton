"""게스트 신청 · 관리자 서버 (http://127.0.0.1:8790)

- public/ 폴더의 화면을 내보내고, /api/* 로 신청·입금확인·통계·설정을 처리한다
- 저장소: data/badminton.db (SQLite)
- 게스트는 키 없이 바로 접속. REQUIRE_KEY=1 이면 외부 접속에 키 필요 (data/access_key.txt, 링크에 ?key=키)
- 관리자 로그인: data/admin.json (처음 실행 때 아이디 admin, 비밀번호를 새로 만들어 콘솔에 한 번 출력)
"""
import hashlib, json, os, re, secrets, sqlite3, threading
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import club

BASE = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(BASE, 'public')
# 화면 버전: 배포(서버 재시작) 때마다 index.html 내용으로 정함. 앱이 예전 화면이면 스스로 새로고침
with open(os.path.join(PUBLIC, 'index.html'), 'rb') as _f:
    INDEX_HTML = _f.read()
APP_VER = hashlib.sha1(INDEX_HTML).hexdigest()[:10]
INDEX_HTML = INDEX_HTML.replace(b'__APP_VER__', APP_VER.encode())
# 데이터 폴더: DATA_DIR > Railway 볼륨 경로 > ./data
DATA = os.environ.get('DATA_DIR') or os.environ.get('RAILWAY_VOLUME_MOUNT_PATH') or os.path.join(BASE, 'data')
# 서버 주소: HOST 지정 없으면 Railway에선 0.0.0.0, 로컬에선 127.0.0.1
HOST = os.environ.get('HOST') or ('0.0.0.0' if os.environ.get('RAILWAY_ENVIRONMENT') else '127.0.0.1')
DB_FILE = os.path.join(DATA, 'badminton.db')
KEY_FILE = os.path.join(DATA, 'access_key.txt')
ADMIN_FILE = os.path.join(DATA, 'admin.json')
PORT = int(os.environ.get('PORT', 8790))
KST = timezone(timedelta(hours=9))
os.makedirs(DATA, exist_ok=True)

DEFAULT_SETTINGS = {
    'meet_date': '', 'meet_title': '정기운동', 'meet_time': '19:30–21:30', 'meet_place': '상도초등학교',
    'fee': '5000', 'max_guests': '10',
    'bank_name': '', 'account_no': '', 'holder': '', 'kakao_link': '',
    'club_fee': '25000',          # 클럽 월 회비 (입금 금액 ÷ 이 금액 = 개월 수)
}
SETTING_KEYS = list(DEFAULT_SETTINGS)
PUBLIC_KEYS = [k for k in SETTING_KEYS if k != 'club_fee']   # 게스트 화면에 보내는 설정
LOCK = threading.Lock()
SESSIONS = {}   # sid → {'id', 'super', 'must_change'}


def today():
    return datetime.now(KST).strftime('%Y-%m-%d')


def now():
    return datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')


# ── 저장소 ─────────────────────────────────────────
def db():
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS setting (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS guest (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meet_date TEXT NOT NULL,          -- 운동 날짜 YYYY-MM-DD
            name TEXT NOT NULL, phone4 TEXT NOT NULL,
            age TEXT NOT NULL, gender TEXT NOT NULL, level TEXT NOT NULL,
            pay_name TEXT NOT NULL, fee INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT '입금대기',   -- 입금대기 | 신청완료
            token TEXT NOT NULL,              -- 신청자 본인 확인용
            created_at TEXT NOT NULL, paid_at TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_guest_date ON guest(meet_date);
        CREATE TABLE IF NOT EXISTS deposit (    -- 폰 알림으로 들어온 입금 기록
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at TEXT NOT NULL, raw TEXT NOT NULL,
            sender TEXT, amount INTEGER,
            guest_id INTEGER                     -- 자동으로 맞춘 신청 (없으면 NULL)
        );
        CREATE TABLE IF NOT EXISTS admin_user (  -- 슈퍼 관리자가 추가한 관리자
            id TEXT PRIMARY KEY,
            salt TEXT NOT NULL, hash TEXT NOT NULL,
            must_change INTEGER NOT NULL DEFAULT 1,  -- 1이면 처음 비밀번호(0000) 상태 → 로그인 후 바로 바꿔야 함
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS meet_day (   -- 캘린더에서 따로 정한 날: 연 날(주말 등)·정보 바꾼 날·쉬는 날(off=1)
            date TEXT PRIMARY KEY,
            title TEXT NOT NULL, time TEXT NOT NULL, place TEXT NOT NULL,
            fee TEXT NOT NULL, max_guests TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS member (     -- 클럽 회원
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, role TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '활동',      -- 활동 | 병가 | 출장 | 탈퇴
            exempt INTEGER NOT NULL DEFAULT 0,        -- 1 = 회비 면제 (총무·재무 등)
            sort INTEGER NOT NULL DEFAULT 0, since TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fee (        -- 회원별 월 회비 기록 (한 달에 한 줄)
            member_id INTEGER NOT NULL, ym TEXT NOT NULL,   -- YYYY-MM
            kind TEXT NOT NULL,                             -- 납부 | 병가 | 출장 | 탈퇴
            paid_on TEXT, amount INTEGER, deposit_id INTEGER,
            PRIMARY KEY (member_id, ym)
        );
        CREATE TABLE IF NOT EXISTS push_sub (   -- 관리자 폰 알림 주소 (로그아웃해도 유지)
            endpoint TEXT PRIMARY KEY,
            p256dh TEXT NOT NULL, auth TEXT NOT NULL,
            admin_id TEXT NOT NULL, created_at TEXT NOT NULL
        );
        ''')
        for k, v in DEFAULT_SETTINGS.items():
            con.execute('INSERT OR IGNORE INTO setting(key, value) VALUES (?, ?)', (k, v))
        if 'fee_note' not in [r[1] for r in con.execute('PRAGMA table_info(deposit)')]:
            con.execute('ALTER TABLE deposit ADD COLUMN fee_note TEXT')     # 회비로 처리된 내용 ('홍길동 11월')
        if 'linked' not in [r[1] for r in con.execute('PRAGMA table_info(deposit)')]:
            con.execute('ALTER TABLE deposit ADD COLUMN linked INTEGER NOT NULL DEFAULT 0')   # 1 = 관리자가 게스트 명단과 직접 연결
        if 'viewer' not in [r[1] for r in con.execute('PRAGMA table_info(admin_user)')]:
            con.execute('ALTER TABLE admin_user ADD COLUMN viewer INTEGER NOT NULL DEFAULT 0')   # 1 = 보기만
        if 'off' not in [r[1] for r in con.execute('PRAGMA table_info(meet_day)')]:
            con.execute('ALTER TABLE meet_day ADD COLUMN off INTEGER NOT NULL DEFAULT 0')   # 1 = 그날 쉼
        # 예전 '날짜 지정'(하루만 지정) → 캘린더에서 연 날로 옮기고 지정 값은 비움
        old = con.execute("SELECT value FROM setting WHERE key='meet_date'").fetchone()
        if old and old['value']:
            if old['value'] >= today():
                d = {r['key']: r['value'] for r in con.execute('SELECT key, value FROM setting')}
                con.execute('INSERT OR IGNORE INTO meet_day(date,title,time,place,fee,max_guests,created_at) VALUES (?,?,?,?,?,?,?)',
                            (old['value'], d['meet_title'], d['meet_time'], d['meet_place'], d['fee'], d['max_guests'], now()))
            con.execute("UPDATE setting SET value='' WHERE key='meet_date'")
        # 한 번만: 주말 휴무 전(2026-11-23 이전)에 주말 날짜로 들어온 신청 → 다음 월요일 명단으로 옮김
        if not con.execute("SELECT 1 FROM setting WHERE key='weekend_moved'").fetchone():
            for r in con.execute("SELECT id, meet_date, pay_name FROM guest WHERE meet_date < '2026-11-23'"
                                 " AND strftime('%w', meet_date) IN ('0','6')").fetchall():
                d = datetime.strptime(r['meet_date'], '%Y-%m-%d')
                mon = (d + timedelta(days=7 - d.weekday())).strftime('%Y-%m-%d')
                pay, i = r['pay_name'], 2
                while con.execute('SELECT 1 FROM guest WHERE meet_date=? AND pay_name=? AND id<>?', (mon, pay, r['id'])).fetchone():
                    pay = f"{r['pay_name']}{i}"; i += 1
                con.execute('UPDATE guest SET meet_date=?, pay_name=? WHERE id=?', (mon, pay, r['id']))
            con.execute("INSERT INTO setting(key, value) VALUES ('weekend_moved', ?)", (now(),))


def settings(defaults=False):
    """운동 정보. 운동일은 항상 오늘(한국시간 자정에 바뀜).
    평일(월~금)은 기본으로 열림(캘린더에서 쉬는 날로 닫을 수 있음), 주말 등은 캘린더에서 연 날만 열림 (사용자 결정 2026-11-24).
    연 날은 그날 정보(이름·시간·장소·게스트비·정원)로 덮어씀. defaults=True 면 기본값 그대로."""
    with db() as con:
        s = {r['key']: r['value'] for r in con.execute('SELECT key, value FROM setting')}
        day = con.execute('SELECT * FROM meet_day WHERE date=?', (today(),)).fetchone()
    s['meet_date'] = today()
    if defaults: return s
    if day and day['off']:
        s['closed'] = True
    elif day:
        s.update(meet_title=day['title'], meet_time=day['time'], meet_place=day['place'],
                 fee=day['fee'], max_guests=day['max_guests'])
        s['closed'] = False
    else:
        s['closed'] = datetime.now(KST).weekday() >= 5
    return s


def counts(meet_date):
    with db() as con:
        rows = con.execute('SELECT status, gender, COUNT(*) n FROM guest WHERE meet_date=? GROUP BY status, gender', (meet_date,)).fetchall()
    c = {'confirmed': 0, 'pending': 0, 'male': 0, 'female': 0}
    for r in rows:
        if r['status'] == '신청완료':
            c['confirmed'] += r['n']
            c['male' if r['gender'] == '남' else 'female'] += r['n']
        else:
            c['pending'] += r['n']
    return c


# ── 접속 키 · 관리자 ───────────────────────────────
def access_key():
    if os.environ.get('ACCESS_KEY'):
        return os.environ['ACCESS_KEY']
    if not os.path.exists(KEY_FILE):
        with open(KEY_FILE, 'w', encoding='utf-8') as f:
            f.write(secrets.token_urlsafe(6))
    with open(KEY_FILE, encoding='utf-8') as f:
        return f.read().strip()


HOOK_KEY_FILE = os.path.join(DATA, 'hook_key.txt')


def hook_key():
    """폰 알림 전달 앱이 쓰는 비밀키 (환경변수 HOOK_KEY 또는 data/hook_key.txt)"""
    if os.environ.get('HOOK_KEY'):
        return os.environ['HOOK_KEY']
    if not os.path.exists(HOOK_KEY_FILE):
        with open(HOOK_KEY_FILE, 'w', encoding='utf-8') as f:
            f.write(secrets.token_urlsafe(16))
    with open(HOOK_KEY_FILE, encoding='utf-8') as f:
        return f.read().strip()


def hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac('sha256', pw.encode(), bytes.fromhex(salt), 200_000).hex()


DEFAULT_ADMIN = ('admin', 'admin1234')   # 기본 관리자 계정 (설정 탭에서 비밀번호 변경 가능)


def write_admin(uid, pw):
    salt = secrets.token_hex(16)
    conf = {'id': uid, 'salt': salt, 'hash': hash_pw(pw, salt)}
    with open(ADMIN_FILE, 'w', encoding='utf-8') as f:
        json.dump(conf, f)
    return conf


_ENV_ADMIN = {}


def env_admin():
    """Railway 환경변수 ADMIN_ID / ADMIN_PW 가 있으면 그 계정만 사용 (비밀번호 변경도 Railway에서)"""
    # 값 앞뒤 공백·따옴표는 무시 (Railway에 붙여넣을 때 섞여 들어가는 경우)
    uid = (os.environ.get('ADMIN_ID') or '').strip().strip('"\'').strip()
    pw = (os.environ.get('ADMIN_PW') or '').strip().strip('"\'').strip()
    if not (uid and pw):
        return None
    if _ENV_ADMIN.get('key') != (uid, pw):
        salt = secrets.token_hex(16)
        _ENV_ADMIN.update(key=(uid, pw), conf={'id': uid, 'salt': salt, 'hash': hash_pw(pw, salt), 'env': True})
    return _ENV_ADMIN['conf']


def admin_conf():
    if env_admin():
        return env_admin()
    if not os.path.exists(ADMIN_FILE):
        write_admin(*DEFAULT_ADMIN)
        print(f'[처음 실행] 관리자 계정 생성: {DEFAULT_ADMIN[0]}  (data/admin.json 에 암호화 저장)', flush=True)
    with open(ADMIN_FILE, encoding='utf-8') as f:
        conf = json.load(f)
    # 예전 시험용 계정(test / test1234)을 그대로 쓰고 있으면 기본 계정으로 바꿈. 비밀번호를 바꿨으면 손대지 않음
    if conf.get('id') == 'test' and secrets.compare_digest(hash_pw('test1234', conf['salt']), conf['hash']):
        conf = write_admin(*DEFAULT_ADMIN)
        print(f'관리자 계정을 기본 계정({DEFAULT_ADMIN[0]})으로 바꿨어요', flush=True)
    return conf


def set_admin_pw(new_pw):
    conf = admin_conf()
    conf['salt'] = secrets.token_hex(16)
    conf['hash'] = hash_pw(new_pw, conf['salt'])
    with open(ADMIN_FILE, 'w', encoding='utf-8') as f:
        json.dump(conf, f)


KEY_PAGE = '''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>게스트 신청</title><style>body{font-family:-apple-system,"Apple SD Gothic Neo","Malgun Gothic",sans-serif;background:#fff;display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
form{width:min(340px,90vw)}h1{font-size:20px;margin:0 0 6px}p{color:#8E8E93;font-size:14px;margin:0 0 18px}
input{width:100%;box-sizing:border-box;border:0;background:#F5F5F7;border-radius:12px;padding:15px;font-size:16px}
button{margin-top:10px;width:100%;border:0;border-radius:12px;padding:15px;background:#FFE300;color:#1E1E1E;font-size:16px;font-weight:700;cursor:pointer}
.e{color:#E8836C}</style></head><body><form method="get" action="/"><h1>게스트 신청</h1><p>MSG</p>
<input name="key" placeholder="접속 키" autofocus><button>들어가기</button></form></body></html>'''


class ApiError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


def clean(v, maxlen=40):
    return str(v or '').strip()[:maxlen]


# ── 요청 처리 ──────────────────────────────────────
class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=PUBLIC, **kw)

    def log_message(self, fmt, *args):
        pass

    def list_directory(self, path):
        self.send_error(404)

    def end_headers(self):
        # 화면 파일은 매번 새로 확인 → 배포 후 앱을 다시 열면 바로 새 화면 (아이콘 등 이미지는 그대로 캐시)
        p = self.path.split('?')[0]
        if p == '/' or p.endswith(('.html', '.js', '.json', '.webmanifest')):
            if not any(h.startswith(b'Cache-Control') for h in getattr(self, '_headers_buffer', [])):
                self.send_header('Cache-Control', 'no-cache')
        super().end_headers()

    # 접속 키
    def cookie(self, name):
        for part in (self.headers.get('Cookie') or '').split(';'):
            k, _, v = part.strip().partition('=')
            if k == name:
                return v
        return None

    def is_local(self):
        return self.client_address[0] == '127.0.0.1' and 'Cf-Connecting-Ip' not in self.headers

    def gate(self):
        """접속 키 통과면 True. 아니면 응답을 보내고 False
        게스트는 바로 들어와야 하므로 기본은 열어둠. 환경변수 REQUIRE_KEY=1 일 때만 키를 받음"""
        if not os.environ.get('REQUIRE_KEY'):
            return True
        if self.is_local() or self.cookie('gp_key') == access_key():
            return True
        if self.path.startswith('/api/hook/'):      # 폰 알림 전달은 자체 비밀키로 확인
            return True
        if self.path.startswith('/api/'):
            self.json({'error': '접속 키가 필요해요'}, 401)
            return False
        qk = parse_qs(urlparse(self.path).query).get('key', [None])[0]
        if qk == access_key():
            self.send_response(302)
            self.send_header('Set-Cookie', f'gp_key={access_key()}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=2592000')
            self.send_header('Location', '/')
            self.end_headers()
            return False
        msg = '<span class="e">접속 키가 맞지 않아요. 다시 입력해주세요.</span>' if qk else '총무에게 받은 접속 키를 입력해주세요.'
        body = KEY_PAGE.replace('MSG', msg).encode('utf-8')
        self.send_response(401)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)
        return False

    def session(self):
        return SESSIONS.get(self.cookie('gp_admin') or '')

    # 응답
    def json(self, obj, code=200, headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if self.path.startswith('/api/admin/club/import'):     # 엑셀 파일 그대로
            if n > 10_000_000: raise ApiError('파일이 너무 커요')
            return {'_raw': self.rfile.read(n)}
        if n > 20000:
            raise ApiError('요청이 너무 커요')
        raw = self.rfile.read(n) or b'{}'
        if 'application/x-www-form-urlencoded' in (self.headers.get('Content-Type') or ''):
            return {k: v[0] for k, v in parse_qs(raw.decode('utf-8', 'replace')).items()}
        try:
            return json.loads(raw)
        except ValueError:
            if self.path.startswith('/api/hook/'):   # 알림 앱이 본문을 그냥 글자로 보낸 경우
                return {'text': raw.decode('utf-8', 'replace')}
            raise ApiError('요청 형식이 잘못됐어요')

    def do_GET(self):
        if not self.gate():
            return
        if self.path.startswith('/api/'):
            return self.api('GET')
        if self.path.split('?')[0] in ('/', '/index.html'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(INDEX_HTML)))
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            return self.wfile.write(INDEX_HTML)
        return super().do_GET()

    def do_POST(self):
        if not self.gate():
            return
        if self.path.startswith('/api/'):
            return self.api('POST')
        self.send_error(404)

    def api(self, method):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path
        try:
            data = self.body() if method == 'POST' else {}
            if p.startswith('/api/admin/') and p != '/api/admin/login':
                sess = self.session()
                if not sess:
                    raise ApiError('관리자 로그인이 필요해요', 401)
                if sess['must_change'] and p not in ('/api/admin/me', '/api/admin/logout', '/api/admin/password'):
                    raise ApiError('처음 비밀번호를 먼저 바꿔주세요', 403)
                if (p.startswith('/api/admin/users') or p in ('/api/admin/deposits/delete', '/api/admin/club/import')) and not sess['super']:
                    raise ApiError('슈퍼 관리자만 할 수 있어요', 403)
                if sess.get('viewer') and method == 'POST' and p not in VIEWER_OK:
                    raise ApiError('보기 전용 관리자예요. 수정은 할 수 없어요', 403)
            handler = ROUTES.get((method, p))
            if not handler:
                raise ApiError('없는 주소예요', 404)
            return handler(self, q, data)
        except ApiError as e:
            return self.json({'error': str(e)}, e.code)
        except Exception as e:  # noqa
            return self.json({'error': f'서버 오류: {e}'}, 500)


# ── 게스트 API ─────────────────────────────────────
def public_info(h, q, d):
    s = settings()
    h.json({'settings': {k: s[k] for k in PUBLIC_KEYS}, 'counts': counts(s['meet_date']), 'closed': s['closed'], 'ver': APP_VER})


def mask_name(n):
    """김민수 → 김**  (성만 보이게)"""
    n = str(n or '').strip()
    return (n[:1] + '*' * max(len(n) - 1, 2)) if n else '***'


def public_guests(h, q, d):
    """오늘 입금 완료된 게스트 목록 (이름은 성만, 성별·나이대·급수만 공개)"""
    s = settings()
    with db() as con:
        rows = con.execute("""SELECT name, gender, age, level FROM guest
                              WHERE meet_date=? AND status='신청완료' ORDER BY paid_at, id""", (s['meet_date'],)).fetchall()
    h.json({'date': s['meet_date'], 'guests': [
        {'name': mask_name(r['name']), 'gender': r['gender'], 'age': r['age'], 'level': r['level']} for r in rows]})


def apply(h, q, d):
    name = clean(d.get('name'), 10)
    age, gender, level = clean(d.get('age')), clean(d.get('gender')), clean(d.get('level'))
    if not name: raise ApiError('이름을 입력해주세요')
    if age not in ('10대', '20대', '30대', '40대', '50대', '60대'): raise ApiError('나이대를 골라주세요')
    if gender not in ('남', '여'): raise ApiError('성별을 골라주세요')
    if level not in ('A', 'B', 'C', 'D', '초심'): raise ApiError('급수를 골라주세요')
    s = settings()
    if s['closed']: raise ApiError('오늘은 운동이 없어요. 운동하는 날에 신청해주세요')
    if not s['account_no']: raise ApiError('아직 입금 계좌가 설정되지 않았어요. 총무에게 알려주세요')
    with LOCK, db() as con:
        # 같은 날 이름만 같아도 막음 (다른 브라우저로 다시 신청하는 것 방지, 사용자 결정 2026-11-18)
        dup = con.execute("SELECT 1 FROM guest WHERE meet_date=? AND REPLACE(name,' ','')=?",
                          (s['meet_date'], name.replace(' ', ''))).fetchone()
        if dup:
            raise ApiError('이미 같은 이름으로 신청한 사람이 있어요. 게스트 본인이 신청해주세요', 409)
        n = con.execute('SELECT COUNT(*) FROM guest WHERE meet_date=?', (s['meet_date'],)).fetchone()[0]
        if n >= int(s['max_guests'] or 0): raise ApiError('오늘 게스트 자리가 다 찼어요')
        token = secrets.token_urlsafe(12)
        # 입금자명 = 신청한 이름. 같은 날 같은 이름이 이미 있으면 홍길동2, 홍길동3 처럼 번호를 붙임
        pay_name, n = name, 1
        while con.execute('SELECT 1 FROM guest WHERE meet_date=? AND pay_name=?', (s['meet_date'], pay_name)).fetchone():
            n += 1
            pay_name = f'{name}{n}'
        cur = con.execute('''INSERT INTO guest(meet_date,name,phone4,age,gender,level,pay_name,fee,token,created_at)
                             VALUES (?,?,'',?,?,?,?,?,?,?)''',
                          (s['meet_date'], name, age, gender, level, pay_name, int(s['fee']), token, now()))
    h.json({'id': cur.lastrowid, 'token': token, 'pay_name': pay_name, 'meet_date': s['meet_date']})


def my_status(h, q, d):
    with db() as con:
        r = con.execute('SELECT status, meet_date FROM guest WHERE id=? AND token=?', (q.get('id'), q.get('token'))).fetchone()
    if not r: raise ApiError('신청 내역을 찾을 수 없어요', 404)
    h.json({'status': r['status'], 'meet_date': r['meet_date'], 'counts': counts(r['meet_date'])})


def cancel(h, q, d):
    with db() as con:
        con.execute("DELETE FROM guest WHERE id=? AND token=? AND status='입금대기'", (d.get('id'), d.get('token')))
    h.json({'ok': True})


# ── 입금 알림 자동 확인 ─────────────────────────────
def parse_deposit(text):
    """카카오뱅크 입금 알림 해석 → (보낸 사람 표시, 금액 또는 None, 잔액 또는 None, 통장 끝번호)
    예) 제목+본문 '입금 3,000원 김운지 게스트1 → 입출금통장(5128) 잔액 7,059,788원'
        본문만   '조한균 → 입출금통장(8322) 잔액 50,936원'  (알림 앱이 제목을 못 넘긴 경우)
    입금은 '보낸사람 → 내 통장(끝번호)' 모양. 출금(내 통장 → 받는사람)·광고는 None"""
    t = ' '.join(str(text).split())
    m_amt = re.search(r'입금\s*([\d,]+)\s*원', t)
    body = t[m_amt.end():] if m_amt else t
    m = re.search(r'(.+?)\s*→\s*([^→]*?\((\d{3,4})\))', body)
    if not m:
        return None, None, None, None
    sender, acct = m.group(1).strip(), m.group(3)
    if re.search(r'\(\d{3,4}\)', sender):          # 왼쪽이 내 통장이면 출금
        return None, None, None, None
    bal = re.search(r'잔액\s*([\d,]+)\s*원', body)
    amount = int(m_amt.group(1).replace(',', '')) if m_amt else None
    balance = int(bal.group(1).replace(',', '')) if bal else None
    return sender, amount, balance, acct


def match_deposit(con, sender, amount):
    """입금대기 중인 신청 가운데 금액이 같고 보낸 사람 표시에 이름이 들어 있는 것을 찾음.
    금액을 모르면(None) 이름만으로 찾음. 최근 3일 신청만 보고,
    입금자명(동명이인 구분 숫자 포함)이 맞는 쪽을 먼저, 그다음 먼저 신청한 순"""
    if amount is None:          # 금액을 확인할 수 없으면 자동 확인하지 않음 (관리자가 직접 확인)
        return None
    since = (datetime.now(KST) - timedelta(days=3)).strftime('%Y-%m-%d')
    rows = con.execute("""SELECT id, name, pay_name FROM guest
                          WHERE status='입금대기' AND fee=? AND meet_date>=? ORDER BY id""", (amount, since)).fetchall()
    s = sender.replace(' ', '')
    exact = [r for r in rows if r['pay_name'].replace(' ', '') in s and r['pay_name'] != r['name']]
    by_name = [r for r in rows if r['name'].replace(' ', '') in s]
    pick = (exact or by_name or [None])[0]
    return pick['id'] if pick else None


def hook_log(msg):
    """알림 전달이 왜 안 맞는지 볼 수 있게 모든 요청을 data/hook.log 에 남김"""
    with open(os.path.join(DATA, 'hook.log'), 'a', encoding='utf-8') as f:
        f.write(f'{now()} {msg}\n')


def hook_deposit(h, q, d):
    raw = ' '.join(str(d.get(k) or '') for k in ('title', 'text', 'big_text')).strip() or str(d)
    if not secrets.compare_digest(str(q.get('k') or d.get('k') or ''), hook_key()):
        hook_log(f'키 틀림 | {raw[:200]}')
        raise ApiError('키가 맞지 않아요', 403)
    sender, amount, balance, acct = parse_deposit(raw)
    if sender is None:
        hook_log(f'무시(입금 아님) | {raw[:200]}')
        return h.json({'ok': True, 'result': '입금 알림이 아니라서 무시했어요'})
    with LOCK, db() as con:
        # 금액은 알림 제목('입금 3,000원')에서만 읽음. 제목이 없으면 기록만 하고 자동 확인하지 않음
        hook_log(f'수신 | 보낸사람={sender} 금액={amount} 잔액={balance} | {raw[:200]}')
        gid = match_deposit(con, sender, amount)
        if gid:
            con.execute("UPDATE guest SET status='신청완료', paid_at=? WHERE id=?", (now(), gid))
        cur = con.execute('INSERT INTO deposit(received_at, raw, sender, amount, guest_id) VALUES (?,?,?,?,?)',
                          (now(), raw[:500], sender, amount, gid))
        # 게스트비로 안 맞고 회비 이상이면: 이름·금액으로 누구의 몇 월 회비인지 판단 (못 하면 '미정'으로 남김)
        note = None if gid else auto_fee(con, cur.lastrowid, sender, amount)
    if gid: notify_paid(gid)
    if note: push_all('💰 회비 입금', note)
    hook_log(f"결과 | 게스트={gid} 회비={note or '-'}")
    h.json({'ok': True, 'sender': sender, 'amount': amount, 'matched_guest_id': gid, 'fee': note,
            'result': '자동으로 입금 확인했어요' if gid else f'회비로 처리했어요: {note}' if note else '맞는 신청이 없어서 기록만 했어요'})


def admin_deposits(h, q, d):
    """폰에서 넘어온 입금 알림 기록 — 받은 날짜별 (최신순, 페이지). date 없으면 오늘"""
    size = max(1, min(int(q.get('size') or 8), 50))
    page = max(1, int(q.get('page') or 1))
    date = q.get('date') or today()
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date): raise ApiError('날짜 형식이 맞지 않아요')
    with db() as con:
        total = con.execute('SELECT COUNT(*) FROM deposit WHERE substr(received_at,1,10)=?', (date,)).fetchone()[0]
        matched = con.execute('SELECT COUNT(*) FROM deposit WHERE substr(received_at,1,10)=? AND (guest_id IS NOT NULL OR fee_note IS NOT NULL)', (date,)).fetchone()[0]
        pages = max(1, -(-total // size)); page = min(page, pages)
        rows = [dict(r) for r in con.execute(
            '''SELECT d.id, d.received_at, d.sender, d.amount, d.guest_id, d.fee_note, d.linked, g.name guest_name, g.status guest_status, g.token LIKE 'manual-%' guest_manual
               FROM deposit d LEFT JOIN guest g ON g.id=d.guest_id WHERE substr(d.received_at,1,10)=?
               ORDER BY d.id DESC LIMIT ? OFFSET ?''',
            (date, size, (page - 1) * size))]
    h.json({'deposits': rows, 'date': date, 'page': page, 'pages': pages, 'total': total, 'matched': matched, 'today': today(), 'club_fee': club_fee()})


def admin_link_guests(h, q, d):
    """입금 ↔ 게스트 명단 연결용: 그날 게스트 전부 (상태 상관없이) + 이미 다른 입금과 연결됐는지"""
    date = q.get('date') or today()
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date): raise ApiError('날짜 형식이 맞지 않아요')
    with db() as con:
        rows = [dict(r) for r in con.execute(
            '''SELECT g.id, g.name, g.age, g.gender, g.level, g.pay_name, g.status, g.fee,
                      EXISTS(SELECT 1 FROM deposit d WHERE d.guest_id=g.id) linked
               FROM guest g WHERE g.meet_date=? ORDER BY g.id''', (date,))]
    h.json({'date': date, 'guests': rows})


def admin_deposit_link(h, q, d):
    """입금 알림을 게스트 명단의 한 사람과 직접 연결 (관리자가 고름).
    입금대기였으면 입금완료로 바꾸고(입금 시각 그대로) 관리자 폰 알림, 이미 입금완료면 연결만"""
    dep_id, gid = d.get('deposit_id'), d.get('guest_id')
    with LOCK, db() as con:
        dep = con.execute('SELECT id, received_at, guest_id, fee_note FROM deposit WHERE id=?', (dep_id,)).fetchone()
        g = con.execute('SELECT id, name, status FROM guest WHERE id=?', (gid,)).fetchone()
        if not dep: raise ApiError('입금 알림을 찾을 수 없어요', 404)
        if not g: raise ApiError('게스트를 찾을 수 없어요', 404)
        if dep['guest_id'] or dep['fee_note']: raise ApiError('이미 처리된 입금이에요', 409)
        if con.execute('SELECT 1 FROM deposit WHERE guest_id=?', (gid,)).fetchone():
            raise ApiError(f"{g['name']}님은 이미 다른 입금과 연결돼 있어요", 409)
        was_waiting = g['status'] == '입금대기'
        if was_waiting:
            con.execute("UPDATE guest SET status='신청완료', paid_at=? WHERE id=?", (dep['received_at'], gid))
        con.execute('UPDATE deposit SET guest_id=?, linked=1 WHERE id=?', (gid, dep_id))
    if was_waiting: notify_paid(gid)
    h.json({'ok': True, 'name': g['name'], 'paid_now': was_waiting})


def admin_deposit_search(h, q, d):
    """입금 알림 검색: 날짜 상관없이 입금자 이름(띄어쓰기 무시, 일부만 맞아도)으로, 최근 것부터 200건"""
    word = re.sub(r'\s+', '', clean(q.get('q'), 20))
    if not word: raise ApiError('찾을 이름을 입력해주세요')
    like = '%' + word.replace('\\', '').replace('%', '').replace('_', '') + '%'
    with db() as con:
        rows = [dict(r) for r in con.execute(
            '''SELECT d.id, d.received_at, d.sender, d.amount, d.guest_id, d.fee_note, d.linked, g.name guest_name, g.status guest_status,
                      g.token LIKE 'manual-%' guest_manual
               FROM deposit d LEFT JOIN guest g ON g.id=d.guest_id
               WHERE REPLACE(d.sender,' ','') LIKE ? OR REPLACE(COALESCE(d.fee_note,''),' ','') LIKE ? OR REPLACE(COALESCE(g.name,''),' ','') LIKE ?
               ORDER BY d.id DESC LIMIT 201''', (like, like, like))]
    h.json({'deposits': rows[:200], 'more': len(rows) > 200, 'club_fee': club_fee()})


def admin_deposit_delete(h, q, d):
    """입금 알림 기록 한 줄 삭제 (슈퍼 관리자만). 기록만 지우고 신청 상태는 그대로"""
    with LOCK, db() as con:
        con.execute('DELETE FROM deposit WHERE id=?', (d.get('id'),))
    h.json({'ok': True})


# ── 관리자 폰 알림 (웹 푸시) ─────────────────────────
# 알림을 켠 관리자 폰에 '입금 완료된 게스트'만 알린다. 로그아웃해도 알림 주소는 남아서 계속 받음
try:
    from pywebpush import webpush, WebPushException
    from py_vapid import Vapid02
    from cryptography.hazmat.primitives import serialization
    import base64
    PUSH_OK = True
except ImportError:          # 라이브러리가 없으면 알림 기능만 꺼짐 (나머지는 그대로 동작)
    PUSH_OK = False
VAPID_FILE = os.path.join(DATA, 'vapid_private.pem')


def vapid():
    """푸시 서명 키 (처음에 만들어 볼륨에 보관 — 바뀌면 기존 알림 등록이 모두 무효가 되므로 유지)"""
    if not os.path.exists(VAPID_FILE):
        v = Vapid02(); v.generate_keys(); v.save_key(VAPID_FILE)
    return Vapid02.from_file(VAPID_FILE)


def vapid_public():
    raw = vapid().public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def push_log(msg):
    with open(os.path.join(DATA, 'push.log'), 'a', encoding='utf-8') as f:
        f.write(f'{now()} {msg}\n')


def push_all(title, body):
    """등록된 관리자 폰 전부에 알림 (응답을 늦추지 않게 따로 실행). 없어진 등록은 지움"""
    if not PUSH_OK: return
    with db() as con:
        subs = [dict(r) for r in con.execute('SELECT * FROM push_sub')]
        origin = (con.execute("SELECT value FROM setting WHERE key='push_origin'").fetchone() or {'value': ''})['value']
    if not subs: return
    payload = json.dumps({'title': title, 'body': body})
    claims_sub = origin if origin.startswith('https://') else 'mailto:admin@example.com'

    def run():
        key = vapid()
        for s in subs:
            info = {'endpoint': s['endpoint'], 'keys': {'p256dh': s['p256dh'], 'auth': s['auth']}}
            try:
                webpush(info, payload, vapid_private_key=key, vapid_claims={'sub': claims_sub}, ttl=86400)
                push_log(f"보냄 {s['admin_id']} | {body}")
            except WebPushException as e:
                code = getattr(e.response, 'status_code', None)
                push_log(f"실패 {s['admin_id']} {code} | {e}")
                if code in (404, 410):      # 폰에서 알림을 끊었거나 앱을 지운 경우
                    with db() as con: con.execute('DELETE FROM push_sub WHERE endpoint=?', (s['endpoint'],))
            except Exception as e:
                push_log(f"실패 {s['admin_id']} | {e}")
    threading.Thread(target=run, daemon=True).start()


def notify_paid(gid):
    """입금 완료된 게스트 알림: 이름 · 나이대 · 성별 · 급수"""
    with db() as con:
        g = con.execute('SELECT name, age, gender, level FROM guest WHERE id=?', (gid,)).fetchone()
    if g:
        push_all('🏸 게스트 신청 완료', f"{g['name']} · {g['age']} · {g['gender']} · {g['level']}")


def push_key(h, q, d):
    if not PUSH_OK: raise ApiError('서버에 알림 기능이 아직 설치되지 않았어요', 503)
    h.json({'key': vapid_public()})


def push_subscribe(h, q, d):
    if not PUSH_OK: raise ApiError('서버에 알림 기능이 아직 설치되지 않았어요', 503)
    sub = d.get('sub') or {}
    ep, keys = str(sub.get('endpoint') or ''), sub.get('keys') or {}
    if not ep.startswith(('https://', 'http://127.0.0.1')) or not keys.get('p256dh') or not keys.get('auth'):
        raise ApiError('알림 등록 정보가 맞지 않아요')
    host = h.headers.get('X-Forwarded-Host') or h.headers.get('Host') or ''
    with db() as con:
        con.execute('INSERT OR REPLACE INTO push_sub(endpoint,p256dh,auth,admin_id,created_at) VALUES (?,?,?,?,?)',
                    (ep, keys['p256dh'], keys['auth'], h.session()['id'], now()))
        if host and not host.startswith(('127.', 'localhost')):
            con.execute("INSERT OR REPLACE INTO setting(key,value) VALUES ('push_origin', ?)", ('https://' + host,))
    h.json({'ok': True})


def push_unsubscribe(h, q, d):
    with db() as con:
        con.execute('DELETE FROM push_sub WHERE endpoint=?', (str(d.get('endpoint') or ''),))
    h.json({'ok': True})


# ── 관리자 API ─────────────────────────────────────
USER_INIT_PW = '0000'   # 추가·초기화한 관리자의 처음 비밀번호


def pw_ok(pw, row):
    return secrets.compare_digest(hash_pw(pw, row['salt']), row['hash'])


# 보기 전용 관리자도 할 수 있는 것: 로그아웃·내 비밀번호·내 폰 알림
VIEWER_OK = ('/api/admin/logout', '/api/admin/password', '/api/admin/push/subscribe', '/api/admin/push/unsubscribe')


def me_info(sess):
    return {'ok': True, 'id': sess['id'], 'super': sess['super'], 'must_change': sess['must_change'],
            'viewer': bool(sess.get('viewer')),
            'env': bool(sess['super'] and env_admin())}


def admin_login(h, q, d):
    uid, pw = clean(d.get('id')), str(d.get('pw') or '')
    conf = admin_conf()
    sess = None
    # 아이디는 대소문자 구분 없이 (휴대폰 키보드가 첫 글자를 대문자로 바꾸는 경우)
    if uid.lower() == conf['id'].lower():
        if pw_ok(pw, conf):
            sess = {'id': conf['id'], 'super': True, 'must_change': False}
    else:
        with db() as con:
            row = con.execute('SELECT * FROM admin_user WHERE lower(id)=lower(?)', (uid,)).fetchone()
        if row and pw_ok(pw, row):
            sess = {'id': row['id'], 'super': False, 'must_change': bool(row['must_change']), 'viewer': bool(row['viewer'])}
    if not sess: raise ApiError('아이디 또는 비밀번호가 맞지 않아요', 401)
    sid = secrets.token_urlsafe(24)
    SESSIONS[sid] = sess
    secure = '' if h.is_local() else ' Secure;'
    h.json(me_info(sess), headers={'Set-Cookie': f'gp_admin={sid}; Path=/; HttpOnly;{secure} SameSite=Lax; Max-Age=43200'})


def admin_me(h, q, d):
    h.json(me_info(h.session()))


def admin_logout(h, q, d):
    SESSIONS.pop(h.cookie('gp_admin') or '', None)
    h.json({'ok': True}, headers={'Set-Cookie': 'gp_admin=; Path=/; Max-Age=0'})


def drop_sessions(uid):
    """그 관리자의 로그인을 모두 끊음 (비밀번호 초기화·삭제 때)"""
    for sid in [s for s, v in SESSIONS.items() if v['id'] == uid and not v['super']]:
        SESSIONS.pop(sid, None)


# ── 관리자 관리 (슈퍼 관리자만) ─────────────────────
def users_list(h, q, d):
    with db() as con:
        rows = [dict(r) for r in con.execute('SELECT id, must_change, viewer, created_at FROM admin_user ORDER BY created_at')]
    h.json({'users': rows, 'super_id': admin_conf()['id']})


def users_add(h, q, d):
    uid = clean(d.get('id'), 20)
    if not re.fullmatch(r'[0-9A-Za-z가-힣_.-]{2,20}', uid):
        raise ApiError('아이디는 2~20자 한글·영문·숫자로 해주세요')
    if uid.lower() == admin_conf()['id'].lower():
        raise ApiError('슈퍼 관리자 아이디와 같아요')
    salt = secrets.token_hex(16)
    try:
        with db() as con:
            if con.execute('SELECT 1 FROM admin_user WHERE lower(id)=lower(?)', (uid,)).fetchone():
                raise sqlite3.IntegrityError
            con.execute('INSERT INTO admin_user(id, salt, hash, must_change, created_at) VALUES (?,?,?,1,?)',
                        (uid, salt, hash_pw(USER_INIT_PW, salt), now()))
    except sqlite3.IntegrityError:
        raise ApiError('이미 있는 아이디예요')
    h.json({'ok': True})


def users_reset(h, q, d):
    uid = clean(d.get('id'), 20)
    salt = secrets.token_hex(16)
    with db() as con:
        n = con.execute('UPDATE admin_user SET salt=?, hash=?, must_change=1 WHERE id=?',
                        (salt, hash_pw(USER_INIT_PW, salt), uid)).rowcount
    if not n: raise ApiError('없는 아이디예요', 404)
    drop_sessions(uid)
    h.json({'ok': True})


def users_perm(h, q, d):
    """슈퍼 관리자가 관리자 권한을 바꿈: viewer=1 이면 보기만, 0 이면 수정도 가능"""
    uid, viewer = clean(d.get('id'), 20), 1 if d.get('viewer') else 0
    with db() as con:
        n = con.execute('UPDATE admin_user SET viewer=? WHERE id=?', (viewer, uid)).rowcount
    if not n: raise ApiError('없는 아이디예요', 404)
    for s in SESSIONS.values():                      # 로그인 중이면 바로 적용
        if s['id'].lower() == uid.lower(): s['viewer'] = bool(viewer)
    h.json({'ok': True})


def users_delete(h, q, d):
    uid = clean(d.get('id'), 20)
    with db() as con:
        n = con.execute('DELETE FROM admin_user WHERE id=?', (uid,)).rowcount
        if n: con.execute('DELETE FROM push_sub WHERE lower(admin_id)=lower(?)', (uid,))   # 삭제된 관리자 폰 알림도 끊음
    if not n: raise ApiError('없는 아이디예요', 404)
    drop_sessions(uid)
    h.json({'ok': True})


def admin_guests(h, q, d):
    date = q.get('date') or settings()['meet_date']
    size = max(1, min(int(q.get('size') or 8), 50))
    page = max(1, int(q.get('page') or 1))
    status = q.get('status') or ''        # '' | 입금대기 | 신청완료
    where, args = 'meet_date=?', [date]
    if status in ('입금대기', '신청완료'):
        where += ' AND status=?'; args.append(status)
    with db() as con:
        total = con.execute(f'SELECT COUNT(*) FROM guest WHERE {where}', args).fetchone()[0]
        pages = max(1, -(-total // size)); page = min(page, pages)
        # 입금대기를 먼저, 그다음 신청 순서
        rows = [dict(r) for r in con.execute(
            f'''SELECT id,name,phone4,age,gender,level,pay_name,fee,status,created_at,paid_at,token LIKE 'manual-%' manual FROM guest WHERE {where}
                ORDER BY CASE status WHEN '입금대기' THEN 0 ELSE 1 END, id LIMIT ? OFFSET ?''', args + [size, (page - 1) * size])]
        paid_sum = con.execute("SELECT COALESCE(SUM(fee),0) FROM guest WHERE meet_date=? AND status='신청완료'", (date,)).fetchone()[0]
    h.json({'date': date, 'guests': rows, 'counts': counts(date), 'paid_sum': paid_sum, 'page': page, 'pages': pages, 'total': total})


def admin_add(h, q, d):
    """관리자 수기 입력: 바로 입금완료로 명단에 추가 (자리 수 제한·주말 휴무와 상관없이)"""
    date = clean(d.get('date'), 10)
    name = clean(d.get('name'), 10)
    age, gender, level = clean(d.get('age')), clean(d.get('gender')), clean(d.get('level'))
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date): raise ApiError('날짜가 잘못됐어요')
    if not name: raise ApiError('이름을 입력해주세요')
    if age not in ('10대', '20대', '30대', '40대', '50대', '60대'): raise ApiError('나이대를 골라주세요')
    if gender not in ('남', '여'): raise ApiError('성별을 골라주세요')
    if level not in ('A', 'B', 'C', 'D', '초심'): raise ApiError('급수를 골라주세요')
    try: fee = int(str(d.get('fee', '')).replace(',', ''))
    except ValueError: raise ApiError('금액을 숫자로 입력해주세요')
    if not 0 <= fee <= 1000000: raise ApiError('금액이 너무 커요')
    dep_id = d.get('deposit_id')
    with LOCK, db() as con:
        dep = None
        if dep_id:   # 입금 알림에서 바로 추가: 그 알림과 연결하고 입금 시각을 그대로 씀
            dep = con.execute('SELECT id, received_at, guest_id, fee_note FROM deposit WHERE id=?', (dep_id,)).fetchone()
            if not dep: raise ApiError('입금 알림을 찾을 수 없어요', 404)
            if dep['guest_id'] or dep['fee_note']: raise ApiError('이미 처리된 입금이에요', 409)
        if con.execute("SELECT 1 FROM guest WHERE meet_date=? AND REPLACE(name,' ','')=?",
                       (date, name.replace(' ', ''))).fetchone():
            raise ApiError('이미 같은 이름이 이 날 명단에 있어요', 409)
        pay_name, n = name, 1
        while con.execute('SELECT 1 FROM guest WHERE meet_date=? AND pay_name=?', (date, pay_name)).fetchone():
            n += 1; pay_name = f'{name}{n}'
        cur = con.execute("INSERT INTO guest(meet_date,name,phone4,age,gender,level,pay_name,fee,status,token,created_at,paid_at)"
                          " VALUES (?,?,'',?,?,?,?,?,'신청완료',?,?,?)",
                          (date, name, age, gender, level, pay_name, fee, 'manual-' + secrets.token_urlsafe(9), now(),
                           dep['received_at'] if dep else now()))
        if dep: con.execute('UPDATE deposit SET guest_id=? WHERE id=?', (cur.lastrowid, dep['id']))
    h.json({'ok': True, 'id': cur.lastrowid})


def admin_mark(h, q, d):
    paid = bool(d.get('paid'))
    with db() as con:
        was = con.execute('SELECT status FROM guest WHERE id=?', (d.get('id'),)).fetchone()
        con.execute('UPDATE guest SET status=?, paid_at=? WHERE id=?',
                    ('신청완료' if paid else '입금대기', now() if paid else None, d.get('id')))
    if paid and was and was['status'] == '입금대기':
        notify_paid(d.get('id'))
    h.json({'ok': True})


def admin_delete(h, q, d):
    with db() as con:
        con.execute('DELETE FROM guest WHERE id=?', (d.get('id'),))
    h.json({'ok': True})


PAID_SQL = "COALESCE(paid_on, ym || '-01')"   # 회비를 낸 날 (날짜 없는 예전 기록은 그 회비 달 1일)


def admin_stats_people(h, q, d):
    """통계에서 사람 수를 눌렀을 때: 누가 냈는지 이름 목록 (게스트비·회비 둘 다)
    key 가 있으면 그 줄(일별=날짜, 월별=YYYY-MM), 없으면 그 기간 전체(일별=month, 월별=year)"""
    kind, mode, key = q.get('kind') or '', q.get('mode') or 'day', q.get('key') or ''
    month, year = q.get('month') or today()[:7], q.get('year') or today()[:4]
    if not re.fullmatch(r'(\d{4}(-\d{2}(-\d{2})?)?)?', key) or not re.fullmatch(r'\d{4}-\d{2}', month) or not re.fullmatch(r'\d{4}', year):
        raise ApiError('조건이 잘못됐어요')
    out = []
    with db() as con:
        if kind == 'club':
            # 입금한 달 기준: 일별 줄 = 그 날 낸 사람, 월별 줄 = 그 달 낸 사람, 전체 = 그 기간 (선납분 포함)
            P = PAID_SQL.replace('paid_on', 'f.paid_on').replace('ym ||', 'f.ym ||')
            if mode == 'day':
                where, args = (f'{P}=?', [key]) if key else (f'substr({P},1,7)=?', [month])
            else:
                where, args = (f'substr({P},1,7)=?', [key]) if key else (f'substr({P},1,4)=?', [year])
            rows = con.execute(f'''SELECT m.name, m.role, f.ym, f.paid_on, f.amount FROM fee f JOIN member m ON m.id=f.member_id
                                   WHERE f.kind='납부' AND {where} ORDER BY m.sort, f.ym''', args).fetchall()
            per = {}
            for r in rows:
                p = per.setdefault(r['name'], {'name': r['name'], 'role': r['role'], 'yms': [], 'paid': set(), 'amount': 0})
                p['yms'].append(r['ym']); p['paid'].add(r['paid_on'] or r['ym'] + '-01'); p['amount'] += r['amount'] or 0
            for p in per.values():
                yms = p['yms']
                mon = lambda ym: f"{'' if ym[:4] == yms[0][:4] else ym[2:4] + '년 '}{int(ym[5:])}"
                months = f"{int(yms[0][5:])}월분" if len(yms) == 1 else f"{len(yms)}개월분 ({int(yms[0][5:])}~{mon(yms[-1])}월)"
                paid = sorted(p['paid'])
                pre = any(ym > paid[0][:7] for ym in yms)
                when = f" · {int(paid[0][5:7])}/{int(paid[0][8:10])} 납부" if len(paid) == 1 else f" · {len(paid)}번 납부"
                out.append({'name': p['name'], 'tag': p['role'], 'detail': months + when + (' (선납)' if pre else ''),
                            'amount': p['amount'], 'units': len(yms)})
        else:
            if mode == 'day':
                where, args = ('meet_date=?', [key]) if key else ('substr(meet_date,1,7)=?', [month])
            else:
                where, args = ('substr(meet_date,1,7)=?', [key]) if key else ('substr(meet_date,1,4)=?', [year])
            for r in con.execute(f'''SELECT name, age, gender, level, fee, meet_date FROM guest
                                     WHERE status='신청완료' AND {where} ORDER BY meet_date, paid_at, id''', args):
                lv = r['level'] + ('' if r['level'] == '초심' else '급')
                day = '' if (mode == 'day' and key) else f"{int(r['meet_date'][5:7])}/{int(r['meet_date'][8:10])} · "
                out.append({'name': r['name'], 'tag': '', 'detail': f"{day}{r['age']} · {r['gender']} · {lv}", 'amount': r['fee']})
    h.json({'people': out, 'total': sum(p['amount'] or 0 for p in out), 'units': sum(p.get('units', 1) for p in out)})


def admin_stats(h, q, d):
    mode = q.get('mode', 'day')
    if q.get('kind') == 'club':
        # 회비는 '입금한 달' 기준 (사용자 결정 2026-11-24, 보고서 수입): 선납도 낸 달에 전부 잡음
        #  count = 낸 사람 수, units = 명분 (회비 1달 = 1명분, 3개월 선납이면 3명분)
        P = PAID_SQL
        if mode == 'month':
            key, rng = f'substr({P},1,7)', (f'substr({P},1,4)', q.get('year') or today()[:4])
        else:
            key, rng = P, (f'substr({P},1,7)', q.get('month') or today()[:7])
        with db() as con:
            rows = [{'key': r['k'], 'count': r['n'], 'units': r['u'], 'sum': r['s'] or 0} for r in con.execute(
                f"SELECT {key} k, COUNT(DISTINCT member_id) n, COUNT(*) u, SUM(amount) s FROM fee WHERE kind='납부' AND {rng[0]}=? GROUP BY k ORDER BY k", (rng[1],))]
            people = con.execute(f"SELECT COUNT(DISTINCT member_id) FROM fee WHERE kind='납부' AND {rng[0]}=?", (rng[1],)).fetchone()[0]
        return h.json({'mode': mode, 'kind': 'club', 'rows': rows, 'total_count': people,
                       'total_units': sum(r['units'] for r in rows), 'total_sum': sum(r['sum'] for r in rows)})
    if mode == 'month':
        year = q.get('year') or today()[:4]
        sql = '''SELECT substr(meet_date,1,7) k, COUNT(*) n, SUM(fee) s FROM guest
                 WHERE status='신청완료' AND substr(meet_date,1,4)=? GROUP BY k ORDER BY k'''
        arg = year
    else:
        month = q.get('month') or today()[:7]
        sql = '''SELECT meet_date k, COUNT(*) n, SUM(fee) s FROM guest
                 WHERE status='신청완료' AND substr(meet_date,1,7)=? GROUP BY k ORDER BY k'''
        arg = month
    with db() as con:
        rows = [{'key': r['k'], 'count': r['n'], 'sum': r['s'] or 0} for r in con.execute(sql, (arg,))]
    h.json({'mode': mode, 'rows': rows, 'total_count': sum(r['count'] for r in rows), 'total_sum': sum(r['sum'] for r in rows)})


def admin_settings_get(h, q, d):
    s = settings(defaults=True)
    out = {k: s[k] for k in SETTING_KEYS}
    out['today'] = today()
    h.json(out)


def admin_settings_save(h, q, d):
    vals = {k: clean(d.get(k), 300) for k in SETTING_KEYS if k in d and k != 'meet_date'}
    if 'account_no' in vals: vals['account_no'] = re.sub(r'\D', '', vals['account_no'])
    if 'fee' in vals and not vals['fee'].isdigit(): raise ApiError('게스트비는 숫자로 입력해주세요')
    if 'max_guests' in vals and not vals['max_guests'].isdigit(): raise ApiError('정원은 숫자로 입력해주세요')
    if 'club_fee' in vals and (not vals['club_fee'].isdigit() or int(vals['club_fee']) <= 0): raise ApiError('회비는 0보다 큰 숫자로 입력해주세요')
    if vals.get('kakao_link') and not vals['kakao_link'].startswith('https://'): raise ApiError('카카오페이 링크는 https:// 로 시작해야 해요')
    with db() as con:
        for k, v in vals.items():
            con.execute('UPDATE setting SET value=? WHERE key=?', (v, k))
    h.json({'ok': True})


def admin_days(h, q, d):
    """캘린더: 그 달에 따로 연 날 목록"""
    month = q.get('month') or today()[:7]
    if not re.fullmatch(r'\d{4}-\d{2}', month): raise ApiError('달 형식이 잘못됐어요')
    with db() as con:
        rows = [dict(r) for r in con.execute('SELECT date,title,time,place,fee,max_guests,off FROM meet_day WHERE substr(date,1,7)=? ORDER BY date', (month,))]
    h.json({'month': month, 'today': today(), 'days': rows})


def admin_day_save(h, q, d):
    date = clean(d.get('date'), 10)
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date): raise ApiError('날짜가 잘못됐어요')
    if date < today(): raise ApiError('지난 날짜는 열 수 없어요')
    title, time_, place = clean(d.get('title'), 30), clean(d.get('time'), 30), clean(d.get('place'), 60)
    fee, mx = clean(d.get('fee')).replace(',', ''), clean(d.get('max_guests'))
    if not title: raise ApiError('모임 이름을 입력해주세요')
    if not fee.isdigit(): raise ApiError('게스트비는 숫자로 입력해주세요')
    if not mx.isdigit(): raise ApiError('정원은 숫자로 입력해주세요')
    with db() as con:
        con.execute('INSERT OR REPLACE INTO meet_day(date,title,time,place,fee,max_guests,created_at,off) VALUES (?,?,?,?,?,?,?,0)',
                    (date, title, time_, place, fee, mx, now()))
    h.json({'ok': True})


def admin_day_off(h, q, d):
    """그날 쉬는 날로 닫기. 평일은 '쉼' 표시를 남기고, 주말은 연 기록만 지움"""
    date = clean(d.get('date'), 10)
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date): raise ApiError('날짜가 잘못됐어요')
    if date < today(): raise ApiError('지난 날짜는 바꿀 수 없어요')
    with db() as con:
        if datetime.strptime(date, '%Y-%m-%d').weekday() < 5:
            con.execute("INSERT OR REPLACE INTO meet_day(date,title,time,place,fee,max_guests,created_at,off) VALUES (?,'','','','0','0',?,1)", (date, now()))
        else:
            con.execute('DELETE FROM meet_day WHERE date=?', (date,))
        n = con.execute('SELECT COUNT(*) FROM guest WHERE meet_date=?', (date,)).fetchone()[0]
    h.json({'ok': True, 'guests': n})


def admin_day_delete(h, q, d):
    with db() as con:
        con.execute('DELETE FROM meet_day WHERE date=?', (clean(d.get('date'), 10),))
    h.json({'ok': True})


# ── 클럽 회원·회비 ─────────────────────────────────
DEFAULT_ROLES = ['고문', '회장', '부회장', '총무', '부총무', '재무', '부재무']


def this_ym():
    return today()[:7]


def club_fee():
    try: return int(settings(defaults=True).get('club_fee') or 25000)
    except ValueError: return 25000


def get_roles(con):
    r = con.execute("SELECT value FROM setting WHERE key='roles'").fetchone()
    try: return json.loads(r['value']) if r else list(DEFAULT_ROLES)
    except ValueError: return list(DEFAULT_ROLES)


def set_roles(con, roles):
    con.execute("INSERT OR REPLACE INTO setting(key, value) VALUES ('roles', ?)", (json.dumps(roles, ensure_ascii=False),))


def fill_status_months(con):
    """병가·출장 중인 회원은 그 상태가 이어지는 달을 기록으로 채움 (지난 달이 '미납'으로 보이지 않게)"""
    ym_now = this_ym()
    for m in con.execute("SELECT id, status FROM member WHERE status IN ('병가','출장')").fetchall():
        last = con.execute('SELECT MAX(ym) FROM fee WHERE member_id=?', (m['id'],)).fetchone()[0]
        ym = club.ym_add(last, 1) if last else ym_now
        while ym <= ym_now:
            con.execute('INSERT OR IGNORE INTO fee(member_id, ym, kind) VALUES (?,?,?)', (m['id'], ym, m['status']))
            ym = club.ym_add(ym, 1)


def member_rows(con):
    out = {}
    for r in con.execute('SELECT member_id, ym, kind FROM fee'):
        out.setdefault(r['member_id'], {})[r['ym']] = r['kind']
    return out


def auto_fee(con, dep_id, sender, amount):
    """입금 → 회비 자동 처리. 처리했으면 '홍길동 11월' 같은 설명, 판단이 안 서면 None (미정)"""
    fee = club_fee()
    if not amount or amount < fee:
        return None
    mems = [dict(r) for r in con.execute("SELECT id, name, status FROM member WHERE status<>'탈퇴'")]
    plan = club.plan_fee(sender, amount, fee, mems, member_rows(con), this_ym())
    if not plan:
        return None
    for mid, yms in plan:
        for ym in yms:
            con.execute('INSERT OR REPLACE INTO fee(member_id, ym, kind, paid_on, amount, deposit_id) VALUES (?,?,?,?,?,?)',
                        (mid, ym, '납부', today(), fee, dep_id))
    note = club.plan_text(plan, {m['id']: m['name'] for m in mems}, this_ym())
    con.execute('UPDATE deposit SET fee_note=? WHERE id=?', (note, dep_id))
    return note


def admin_club(h, q, d):
    """회원·회비 화면에 필요한 것 한 번에: 회원, 모든 회비 기록, 역할 목록, 회비 금액"""
    with LOCK, db() as con:
        fill_status_months(con)
        members = [dict(r) for r in con.execute('SELECT id, name, role, status, exempt, sort, since FROM member ORDER BY sort, id')]
        fees = [dict(r) for r in con.execute('SELECT member_id m, ym, kind, paid_on, amount, deposit_id FROM fee')]
        roles = get_roles(con)
    h.json({'members': members, 'fees': fees, 'roles': roles, 'fee': club_fee(), 'ym': this_ym(), 'today': today()})


def admin_member_save(h, q, d):
    name = re.sub(r'\s+', '', clean(d.get('name'), 20))
    role, status = clean(d.get('role'), 20), clean(d.get('status')) or '활동'
    if not name: raise ApiError('이름을 입력해주세요')
    if status not in ('활동', '병가', '출장', '탈퇴'): raise ApiError('상태를 골라주세요')
    exempt, mid, ym = 1 if d.get('exempt') else 0, d.get('id'), this_ym()
    with LOCK, db() as con:
        if role and role not in get_roles(con): raise ApiError('없는 역할이에요')
        dup = con.execute('SELECT 1 FROM member WHERE name=? AND id<>?', (name, mid or 0)).fetchone()
        if dup: raise ApiError('같은 이름의 회원이 있어요. 구분되게 적어주세요 (예: 김민수B)', 409)
        if mid:
            if not con.execute('UPDATE member SET name=?, role=?, status=?, exempt=? WHERE id=?',
                               (name, role, status, exempt, mid)).rowcount:
                raise ApiError('없는 회원이에요', 404)
        else:
            srt = con.execute('SELECT COALESCE(MAX(sort),0)+1 FROM member').fetchone()[0]
            mid = con.execute('INSERT INTO member(name, role, status, exempt, sort, since, created_at) VALUES (?,?,?,?,?,?,?)',
                              (name, role, status, exempt, srt, ym, now())).lastrowid
        # 이번 달 기록: 병가·출장·탈퇴면 그 상태로 (낸 기록이 있으면 그대로), 활동으로 돌아오면 이번 달 상태 기록을 지움
        cur = con.execute('SELECT kind FROM fee WHERE member_id=? AND ym=?', (mid, ym)).fetchone()
        if status != '활동' and (not cur or cur['kind'] != '납부'):
            con.execute('INSERT OR REPLACE INTO fee(member_id, ym, kind) VALUES (?,?,?)', (mid, ym, status))
        if status == '활동' and cur and cur['kind'] in ('병가', '출장', '탈퇴'):
            con.execute('DELETE FROM fee WHERE member_id=? AND ym=?', (mid, ym))
        if status != '탈퇴':     # 탈퇴를 되돌리면 이후 달의 탈퇴 표시도 지움
            con.execute("DELETE FROM fee WHERE member_id=? AND kind='탈퇴' AND ym>=?", (mid, ym))
    h.json({'ok': True, 'id': mid})


def admin_member_delete(h, q, d):
    with LOCK, db() as con:
        con.execute('DELETE FROM fee WHERE member_id=?', (d.get('id'),))
        con.execute('DELETE FROM member WHERE id=?', (d.get('id'),))
    h.json({'ok': True})


def admin_role_add(h, q, d):
    name = re.sub(r'\s+', '', clean(d.get('name'), 10))
    if not name: raise ApiError('역할 이름을 입력해주세요')
    with db() as con:
        roles = get_roles(con)
        if name in roles: raise ApiError('이미 있는 역할이에요')
        set_roles(con, roles + [name])
    h.json({'ok': True})


def admin_role_delete(h, q, d):
    name = clean(d.get('name'), 10)
    with db() as con:
        if con.execute('SELECT 1 FROM member WHERE role=?', (name,)).fetchone():
            raise ApiError('이 역할을 맡은 회원이 있어서 지울 수 없어요')
        set_roles(con, [r for r in get_roles(con) if r != name])
    h.json({'ok': True})


def admin_fee_set(h, q, d):
    """회비 현황 칸 하나 직접 바꾸기: kind '' 이면 기록 지움(미납)"""
    mid, ym, kind = d.get('member_id'), clean(d.get('ym'), 7), clean(d.get('kind'))
    if not re.fullmatch(r'\d{4}-\d{2}', ym): raise ApiError('달이 잘못됐어요')
    with LOCK, db() as con:
        if not kind:
            con.execute('DELETE FROM fee WHERE member_id=? AND ym=?', (mid, ym))
        elif kind == '납부':
            paid_on = clean(d.get('paid_on'), 10) or today()
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', paid_on): raise ApiError('납부일이 잘못됐어요')
            con.execute('INSERT OR REPLACE INTO fee(member_id, ym, kind, paid_on, amount) VALUES (?,?,?,?,?)',
                        (mid, ym, '납부', paid_on, club_fee()))
        elif kind in ('병가', '출장', '탈퇴'):
            con.execute('INSERT OR REPLACE INTO fee(member_id, ym, kind) VALUES (?,?,?)', (mid, ym, kind))
        else:
            raise ApiError('상태가 잘못됐어요')
    h.json({'ok': True})


def admin_fee_pay(h, q, d):
    """입금 알림(또는 직접)으로 회비 처리: items = [{member_id, start, months}]"""
    items, dep_id = d.get('items') or [], d.get('deposit_id')
    if not items: raise ApiError('회원을 골라주세요')
    fee = club_fee()
    with LOCK, db() as con:
        paid_on = today()
        if dep_id:
            dep = con.execute('SELECT received_at, guest_id, fee_note FROM deposit WHERE id=?', (dep_id,)).fetchone()
            if not dep: raise ApiError('입금 알림을 찾을 수 없어요', 404)
            if dep['guest_id'] or dep['fee_note']: raise ApiError('이미 처리된 입금이에요', 409)
            paid_on = dep['received_at'][:10]
        names, plan = {}, []
        for it in items:
            m = con.execute('SELECT id, name FROM member WHERE id=?', (it.get('member_id'),)).fetchone()
            start, n = clean(it.get('start'), 7), int(it.get('months') or 0)
            if not m: raise ApiError('없는 회원이에요', 404)
            if not re.fullmatch(r'\d{4}-\d{2}', start) or not 1 <= n <= 24: raise ApiError('달을 다시 골라주세요')
            yms = club.ym_range(start, n)
            got = con.execute(f"SELECT ym FROM fee WHERE member_id=? AND kind='납부' AND ym IN ({','.join('?' * n)})",
                              (m['id'], *yms)).fetchall()
            if got: raise ApiError(f"{m['name']} {int(got[0]['ym'][5:])}월은 이미 냈어요")
            names[m['id']] = m['name']; plan.append((m['id'], yms))
        for mid, yms in plan:
            for ym in yms:
                con.execute('INSERT OR REPLACE INTO fee(member_id, ym, kind, paid_on, amount, deposit_id) VALUES (?,?,?,?,?,?)',
                            (mid, ym, '납부', paid_on, fee, dep_id))
        note = club.plan_text(plan, names, this_ym())
        if dep_id: con.execute('UPDATE deposit SET fee_note=? WHERE id=?', (note, dep_id))
    h.json({'ok': True, 'note': note})


def admin_club_report(h, q, d):
    """월 회비 결산서 (PDF 는 화면에서 그림) — 고른 달 1개월
    수입 = 그 달에 들어온 돈 (입금한 달 기준): 그 달 회비(인원 × 회비) / 이전 달 미납분 / 선납금(다음 달 이후분) / 게스트비
    기타사항 = 그 달 신입·탈퇴·병가·출장 명단, 회원 표 = 전월·가입·탈퇴·회원 (병가·총무·재무 포함)"""
    ym = q.get('month') or this_ym()
    if not re.fullmatch(r'\d{4}-\d{2}', ym): raise ApiError('달 형식이 잘못됐어요')
    fee = club_fee()
    prev = club.ym_add(ym, -1)
    with db() as con:
        fill_status_months(con)
        members = [dict(r) for r in con.execute('SELECT id, name, since, sort FROM member ORDER BY sort, id')]
        left = {r['member_id']: r['m'] for r in con.execute("SELECT member_id, MIN(ym) m FROM fee WHERE kind='탈퇴' GROUP BY member_id")}
        status = {}
        for r in con.execute("SELECT member_id, kind FROM fee WHERE ym=? AND kind IN ('병가','출장')", (ym,)):
            status[r['member_id']] = r['kind']
        paid = con.execute(f"""SELECT f.member_id, f.ym, f.amount, m.name, m.since FROM fee f JOIN member m ON m.id=f.member_id
                               WHERE f.kind='납부' AND substr({PAID_SQL.replace('paid_on', 'f.paid_on').replace('ym ||', 'f.ym ||')},1,7)=?
                               ORDER BY m.sort, f.ym""", (ym,)).fetchall()
        g = con.execute("SELECT COUNT(*) n, COALESCE(SUM(fee),0) s FROM guest WHERE status='신청완료' AND substr(meet_date,1,7)=?", (ym,)).fetchone()
    first = min((m['since'] or ym for m in members), default=ym)
    def is_member(m, at):
        return (m['since'] or first) <= at and not (left.get(m['id']) and left[m['id']] <= at)
    new = [m for m in members if m['since'] == ym and ym != first]
    quit_ = [m for m in members if left.get(m['id']) == ym]
    names = lambda ms: [m['name'] for m in ms]
    cur = [r for r in paid if r['ym'] == ym]
    back = [r for r in paid if r['ym'] < ym]
    pre = {}
    for r in paid:
        if r['ym'] > ym:
            p = pre.setdefault(r['member_id'], {'name': r['name'], 'new': r['since'] == ym, 'months': 0, 'amount': 0})
            p['months'] += 1; p['amount'] += r['amount'] or 0
    pre = list(pre.values())
    lines = [{'kind': 'fee', 'title': f'{int(ym[5:])}월 회비', 'count': len({r['member_id'] for r in cur}),
              'unit': fee, 'amount': sum(r['amount'] or 0 for r in cur)}]
    if back:
        lines.append({'kind': 'back', 'title': '이전 달 회비 (미납분)', 'count': len(back), 'unit': fee,
                      'amount': sum(r['amount'] or 0 for r in back)})
    if pre:
        lines.append({'kind': 'pre', 'title': ('신입회원 선납금' if all(p['new'] for p in pre) else '선납금'),
                      'detail': ', '.join(f"{p['name']}{'(신입)' if p['new'] and not all(x['new'] for x in pre) else ''} {p['months']}개월" for p in pre),
                      'count': sum(p['months'] for p in pre), 'unit': fee, 'amount': sum(p['amount'] for p in pre)})
    lines.append({'kind': 'guest', 'title': f"게스트비 ({g['n']}명)", 'count': g['n'], 'amount': g['s']})
    h.json({
        'month': ym, 'fee': fee, 'lines': lines, 'income': sum(l['amount'] for l in lines),
        'new': names(new), 'quit': names(quit_),
        'sick': [m['name'] for m in members if status.get(m['id']) == '병가'],
        'trip': [m['name'] for m in members if status.get(m['id']) == '출장'],
        'count': {'prev': sum(is_member(m, prev) for m in members), 'join': len(new), 'quit': len(quit_),
                  'now': sum(is_member(m, ym) for m in members)},
    })


def admin_club_export(h, q, d):
    """회비 현황 엑셀 다운로드 — 고른 연도 1년치 (지금 쓰는 '회비내역(연도)' 양식)"""
    year = q.get('year') or today()[:4]
    if not re.fullmatch(r'\d{4}', year): raise ApiError('연도가 잘못됐어요')
    with db() as con:
        fill_status_months(con)
        members = [dict(r) for r in con.execute('SELECT id, name, role, status, exempt, since FROM member ORDER BY sort, id')]
        fees = {}
        for r in con.execute('SELECT member_id, ym, kind, paid_on, amount FROM fee'):
            fees.setdefault(r['member_id'], {})[r['ym']] = dict(r)
    data = club.fee_sheet(int(year), members, fees)
    from urllib.parse import quote
    fname = quote(f'상도초클럽_회비현황_{year}.xlsx')
    h.send_response(200)
    h.send_header('Content-Type', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    h.send_header('Content-Disposition', f"attachment; filename=\"club-fee-{year}.xlsx\"; filename*=UTF-8''{fname}")
    h.send_header('Content-Length', str(len(data)))
    h.send_header('Cache-Control', 'no-store')
    h.end_headers()
    h.wfile.write(data)


def admin_club_import(h, q, d):
    """엑셀(재무보고현황.xlsx) 의 '회비내역(2026)' 부터 읽어 회원·회비를 새로 채움 (기존 회원·회비는 지움)"""
    try:
        r = club.parse_club(d.get('_raw') or b'', 2026, this_ym())
    except Exception as e:  # noqa
        raise ApiError(f'엑셀을 읽지 못했어요: {e}')
    with LOCK, db() as con:
        con.execute('DELETE FROM fee'); con.execute('DELETE FROM member')
        ids = {}
        for m in r['members']:
            ids[m['name']] = con.execute('INSERT INTO member(name, role, status, exempt, sort, since, created_at) VALUES (?,?,?,?,?,?,?)',
                                         (m['name'], m['role'], m['status'], m['exempt'], m['sort'], m['since'] or '2026-01', now())).lastrowid
        for f in r['fees']:
            con.execute('INSERT OR REPLACE INTO fee(member_id, ym, kind, paid_on, amount) VALUES (?,?,?,?,?)',
                        (ids[f['name']], f['ym'], f['kind'], f['paid_on'], club_fee() if f['kind'] == '납부' else None))
        roles = get_roles(con)
        set_roles(con, roles + [x for x in r['roles'] if x not in roles])
    h.json({'ok': True, 'members': len(r['members']), 'fees': len(r['fees']),
            'active': sum(m['status'] != '탈퇴' for m in r['members'])})


def admin_password(h, q, d):
    pw = str(d.get('pw') or '')
    sess = h.session()
    if len(pw) < 6: raise ApiError('비밀번호는 6자 이상으로 해주세요')
    if sess['super']:
        if env_admin(): raise ApiError('슈퍼 관리자 비밀번호는 Railway 환경변수(ADMIN_PW)에서 바꿔주세요')
        set_admin_pw(pw)
    else:
        salt = secrets.token_hex(16)
        with db() as con:
            con.execute('UPDATE admin_user SET salt=?, hash=?, must_change=0 WHERE id=?', (salt, hash_pw(pw, salt), sess['id']))
        for v in SESSIONS.values():
            if v['id'] == sess['id'] and not v['super']:
                v['must_change'] = False
    h.json({'ok': True})


ROUTES = {
    ('GET', '/api/public'): public_info,
    ('GET', '/api/public/guests'): public_guests,
    ('POST', '/api/apply'): apply,
    ('GET', '/api/status'): my_status,
    ('POST', '/api/cancel'): cancel,
    ('POST', '/api/admin/login'): admin_login,
    ('GET', '/api/admin/me'): admin_me,
    ('POST', '/api/admin/logout'): admin_logout,
    ('GET', '/api/admin/guests'): admin_guests,
    ('POST', '/api/admin/mark'): admin_mark,
    ('POST', '/api/admin/add'): admin_add,
    ('POST', '/api/admin/days/save'): admin_day_save,
    ('POST', '/api/admin/days/delete'): admin_day_delete,
    ('POST', '/api/admin/days/off'): admin_day_off,
    ('POST', '/api/admin/delete'): admin_delete,
    ('GET', '/api/admin/stats'): admin_stats,
    ('GET', '/api/admin/stats/people'): admin_stats_people,
    ('GET', '/api/admin/settings'): admin_settings_get,
    ('GET', '/api/admin/days'): admin_days,
    ('POST', '/api/admin/settings'): admin_settings_save,
    ('POST', '/api/admin/password'): admin_password,
    ('GET', '/api/admin/deposits'): admin_deposits,
    ('GET', '/api/admin/users'): users_list,
    ('POST', '/api/admin/users/add'): users_add,
    ('POST', '/api/admin/deposits/delete'): admin_deposit_delete,
    ('GET', '/api/admin/deposits/search'): admin_deposit_search,
    ('GET', '/api/admin/deposits/link_guests'): admin_link_guests,
    ('POST', '/api/admin/deposits/link'): admin_deposit_link,
    ('GET', '/api/admin/push/key'): push_key,
    ('POST', '/api/admin/push/subscribe'): push_subscribe,
    ('POST', '/api/admin/push/unsubscribe'): push_unsubscribe,
    ('POST', '/api/admin/users/reset'): users_reset,
    ('POST', '/api/admin/users/delete'): users_delete,
    ('POST', '/api/admin/users/perm'): users_perm,
    ('GET', '/api/admin/club'): admin_club,
    ('POST', '/api/admin/members/save'): admin_member_save,
    ('POST', '/api/admin/members/delete'): admin_member_delete,
    ('POST', '/api/admin/roles/add'): admin_role_add,
    ('POST', '/api/admin/roles/delete'): admin_role_delete,
    ('POST', '/api/admin/fees/set'): admin_fee_set,
    ('POST', '/api/admin/fees/pay'): admin_fee_pay,
    ('POST', '/api/admin/club/import'): admin_club_import,
    ('GET', '/api/admin/club/export'): admin_club_export,
    ('GET', '/api/admin/club/report'): admin_club_report,
    ('POST', '/api/hook/deposit'): hook_deposit,
}


if __name__ == '__main__':
    init_db()
    admin_conf()
    hook_key()
    srv = ThreadingHTTPServer((HOST, PORT), H)
    print(f'게스트 신청 서버: http://{HOST}:{PORT}  데이터: {DATA}', flush=True)
    srv.serve_forever()
