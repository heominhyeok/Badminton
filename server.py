"""게스트 신청 · 관리자 서버 (http://127.0.0.1:8790)

- public/ 폴더의 화면을 내보내고, /api/* 로 신청·입금확인·통계·설정을 처리한다
- 저장소: data/badminton.db (SQLite)
- 외부 주소(터널)로 들어오면 접속 키 필요: data/access_key.txt (링크에 ?key=키)
- 관리자 로그인: data/admin.json (처음 실행 때 아이디 admin, 비밀번호를 새로 만들어 콘솔에 한 번 출력)
"""
import hashlib, json, os, re, secrets, sqlite3, threading
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(BASE, 'public')
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
}
SETTING_KEYS = list(DEFAULT_SETTINGS)
LOCK = threading.Lock()
SESSIONS = set()


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
        ''')
        for k, v in DEFAULT_SETTINGS.items():
            con.execute('INSERT OR IGNORE INTO setting(key, value) VALUES (?, ?)', (k, v))


def settings():
    with db() as con:
        s = {r['key']: r['value'] for r in con.execute('SELECT key, value FROM setting')}
    # 운동 날짜: 비어 있으면 매일 자동으로 오늘(한국시간 자정에 바뀜).
    # 지정한 날짜가 지나가면 다시 오늘로 돌아감
    fixed = s.get('meet_date') or ''
    s['meet_date_fixed'] = fixed if fixed >= today() else ''
    s['meet_date'] = s['meet_date_fixed'] or today()
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


def admin_conf():
    if not os.path.exists(ADMIN_FILE):
        # 테스트용 초기 계정 (설정 탭에서 비밀번호 변경 가능)
        uid, pw = 'test', 'test1234'
        salt = secrets.token_hex(16)
        conf = {'id': uid, 'salt': salt, 'hash': hash_pw(pw, salt)}
        with open(ADMIN_FILE, 'w', encoding='utf-8') as f:
            json.dump(conf, f)
        print(f'[처음 실행] 관리자 계정 생성: {uid}  (data/admin.json 에 암호화 저장)', flush=True)
    with open(ADMIN_FILE, encoding='utf-8') as f:
        return json.load(f)


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
        """접속 키 통과면 True. 아니면 응답을 보내고 False"""
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

    def is_admin(self):
        return self.cookie('gp_admin') in SESSIONS

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
            if p.startswith('/api/admin/') and p != '/api/admin/login' and not self.is_admin():
                raise ApiError('관리자 로그인이 필요해요', 401)
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
    h.json({'settings': {k: s[k] for k in SETTING_KEYS}, 'counts': counts(s['meet_date'])})


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
    if not s['account_no']: raise ApiError('아직 입금 계좌가 설정되지 않았어요. 총무에게 알려주세요')
    with LOCK, db() as con:
        # 같은 날 이름·나이대·성별·급수가 모두 같은 신청이 있으면 중복 신청으로 막음
        dup = con.execute('''SELECT status, pay_name FROM guest WHERE meet_date=? AND name=? AND age=? AND gender=? AND level=?''',
                          (s['meet_date'], name, age, gender, level)).fetchone()
        if dup:
            if dup['status'] == '신청완료':
                raise ApiError('오늘 이미 신청이 완료됐어요', 409)
            raise ApiError(f"이미 신청돼 있어요. '{dup['pay_name']}' 이름으로 게스트비를 입금해주세요", 409)
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
        con.execute('INSERT INTO deposit(received_at, raw, sender, amount, guest_id) VALUES (?,?,?,?,?)',
                    (now(), raw[:500], sender, amount, gid))
    h.json({'ok': True, 'sender': sender, 'amount': amount, 'matched_guest_id': gid,
            'result': '자동으로 입금 확인했어요' if gid else '맞는 신청이 없어서 기록만 했어요'})


def admin_deposits(h, q, d):
    """폰에서 넘어온 입금 알림 기록 (최신순, 페이지)"""
    size = max(1, min(int(q.get('size') or 8), 50))
    page = max(1, int(q.get('page') or 1))
    with db() as con:
        total = con.execute('SELECT COUNT(*) FROM deposit').fetchone()[0]
        matched = con.execute('SELECT COUNT(*) FROM deposit WHERE guest_id IS NOT NULL').fetchone()[0]
        pages = max(1, -(-total // size)); page = min(page, pages)
        rows = [dict(r) for r in con.execute(
            '''SELECT d.id, d.received_at, d.sender, d.amount, d.guest_id, g.name guest_name, g.status guest_status
               FROM deposit d LEFT JOIN guest g ON g.id=d.guest_id ORDER BY d.id DESC LIMIT ? OFFSET ?''',
            (size, (page - 1) * size))]
    h.json({'deposits': rows, 'page': page, 'pages': pages, 'total': total, 'matched': matched, 'today': today()})


# ── 관리자 API ─────────────────────────────────────
def admin_login(h, q, d):
    conf = admin_conf()
    ok = clean(d.get('id')) == conf['id'] and secrets.compare_digest(hash_pw(str(d.get('pw') or ''), conf['salt']), conf['hash'])
    if not ok: raise ApiError('아이디 또는 비밀번호가 맞지 않아요', 401)
    sid = secrets.token_urlsafe(24)
    SESSIONS.add(sid)
    secure = '' if h.is_local() else ' Secure;'
    h.json({'ok': True}, headers={'Set-Cookie': f'gp_admin={sid}; Path=/; HttpOnly;{secure} SameSite=Lax; Max-Age=43200'})


def admin_me(h, q, d):
    h.json({'ok': True})


def admin_logout(h, q, d):
    SESSIONS.discard(h.cookie('gp_admin'))
    h.json({'ok': True}, headers={'Set-Cookie': 'gp_admin=; Path=/; Max-Age=0'})


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
            f'''SELECT id,name,phone4,age,gender,level,pay_name,fee,status,created_at,paid_at FROM guest WHERE {where}
                ORDER BY CASE status WHEN '입금대기' THEN 0 ELSE 1 END, id LIMIT ? OFFSET ?''', args + [size, (page - 1) * size])]
        paid_sum = con.execute("SELECT COALESCE(SUM(fee),0) FROM guest WHERE meet_date=? AND status='신청완료'", (date,)).fetchone()[0]
    h.json({'date': date, 'guests': rows, 'counts': counts(date), 'paid_sum': paid_sum, 'page': page, 'pages': pages, 'total': total})


def admin_mark(h, q, d):
    paid = bool(d.get('paid'))
    with db() as con:
        con.execute('UPDATE guest SET status=?, paid_at=? WHERE id=?',
                    ('신청완료' if paid else '입금대기', now() if paid else None, d.get('id')))
    h.json({'ok': True})


def admin_delete(h, q, d):
    with db() as con:
        con.execute('DELETE FROM guest WHERE id=?', (d.get('id'),))
    h.json({'ok': True})


def admin_stats(h, q, d):
    mode = q.get('mode', 'day')
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
    s = settings()
    out = {k: s[k] for k in SETTING_KEYS}
    out['meet_date'] = s['meet_date_fixed']      # '' 이면 매일 자동
    out['today'] = today()
    h.json(out)


def admin_settings_save(h, q, d):
    vals = {k: clean(d.get(k), 300) for k in SETTING_KEYS if k in d}
    if 'account_no' in vals: vals['account_no'] = re.sub(r'\D', '', vals['account_no'])
    if 'fee' in vals and not vals['fee'].isdigit(): raise ApiError('게스트비는 숫자로 입력해주세요')
    if 'max_guests' in vals and not vals['max_guests'].isdigit(): raise ApiError('정원은 숫자로 입력해주세요')
    if vals.get('meet_date') and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', vals['meet_date']): raise ApiError('날짜 형식이 잘못됐어요')
    if vals.get('meet_date') and vals['meet_date'] < today(): raise ApiError('지난 날짜는 지정할 수 없어요')
    if vals.get('kakao_link') and not vals['kakao_link'].startswith('https://'): raise ApiError('카카오페이 링크는 https:// 로 시작해야 해요')
    with db() as con:
        for k, v in vals.items():
            con.execute('UPDATE setting SET value=? WHERE key=?', (v, k))
    h.json({'ok': True})


def admin_password(h, q, d):
    pw = str(d.get('pw') or '')
    if len(pw) < 6: raise ApiError('비밀번호는 6자 이상으로 해주세요')
    set_admin_pw(pw)
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
    ('POST', '/api/admin/delete'): admin_delete,
    ('GET', '/api/admin/stats'): admin_stats,
    ('GET', '/api/admin/settings'): admin_settings_get,
    ('POST', '/api/admin/settings'): admin_settings_save,
    ('POST', '/api/admin/password'): admin_password,
    ('GET', '/api/admin/deposits'): admin_deposits,
    ('POST', '/api/hook/deposit'): hook_deposit,
}


if __name__ == '__main__':
    init_db()
    admin_conf()
    hook_key()
    srv = ThreadingHTTPServer((HOST, PORT), H)
    print(f'게스트 신청 서버: http://{HOST}:{PORT}  데이터: {DATA}', flush=True)
    srv.serve_forever()
