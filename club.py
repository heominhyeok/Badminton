"""클럽 회원·회비 — DB 와 상관없는 계산만 모아 둔 곳 (표준 라이브러리만 사용)

1) 엑셀(재무보고현황.xlsx)의 '회비내역(YYYY)' 탭을 읽어 회원·월별 회비 기록으로 바꾸기
2) 입금 알림(보낸 사람 표시 + 금액)을 보고 누구의 몇 월 회비인지 자동으로 판단하기
"""
import io, re, zipfile
from datetime import date, timedelta
from xml.etree import ElementTree as ET

M = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
KINDS = ('납부', '병가', '출장', '탈퇴')


# ── 년월 계산 ───────────────────────────────────────
def ym_add(ym, n):
    y, m = int(ym[:4]), int(ym[5:7])
    t = y * 12 + (m - 1) + n
    return f'{t // 12:04d}-{t % 12 + 1:02d}'


def ym_range(start, n):
    return [ym_add(start, i) for i in range(n)]


# ── 엑셀 읽기 (xlsx = xml 묶음) ──────────────────────
def _col_row(ref):
    m = re.match(r'([A-Z]+)(\d+)', ref)
    return m.group(1), int(m.group(2))


def read_xlsx(data):
    """{탭 이름: {(행, 열문자): 값}} — 값은 글자 또는 숫자(float). 날짜는 엑셀 일련번호(숫자)로 옴"""
    z = zipfile.ZipFile(io.BytesIO(data))
    names = set(z.namelist())
    ss = []
    if 'xl/sharedStrings.xml' in names:
        for si in ET.fromstring(z.read('xl/sharedStrings.xml')).iter(M + 'si'):
            ss.append(''.join(t.text or '' for t in si.iter(M + 't')))
    rels = {r.get('Id'): r.get('Target') for r in ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))}
    out = {}
    for sh in ET.fromstring(z.read('xl/workbook.xml')).iter(M + 'sheet'):
        target = rels.get(sh.get(R + 'id'), '')
        path = target.lstrip('/') if target.lstrip('/').startswith('xl/') else 'xl/' + target
        if path not in names:
            continue
        cells = {}
        for c in ET.fromstring(z.read(path)).iter(M + 'c'):
            t, v = c.get('t'), c.find(M + 'v')
            if t == 's' and v is not None:
                val = ss[int(v.text)]
            elif t == 'inlineStr':
                val = ''.join(x.text or '' for x in c.iter(M + 't'))
            elif v is None or v.text is None:
                continue
            elif t in ('str', 'e', 'b'):
                val = v.text
            else:
                try: val = float(v.text)
                except ValueError: val = v.text
            col, row = _col_row(c.get('r'))
            cells[(row, col)] = val
        out[sh.get('name')] = cells
    return out


def _excel_date(n):
    return date(1899, 12, 30) + timedelta(days=int(n))


def _cell(v, year, month):
    """한 칸 → ('납부', 'YYYY-MM-DD') / ('병가', None) / … / None"""
    if v is None or v == '':
        return None
    if isinstance(v, float):
        if v > 30000:                      # 엑셀 날짜
            return '납부', _excel_date(v).isoformat()
        return None
    s = re.sub(r'\s+', '', str(v))
    for k in ('병가', '출장', '탈퇴'):
        if k in s:
            return k, None
    if '미납' in s or '면제' in s:
        return None
    m = re.search(r'(\d{1,2})\D+(\d{1,2})', s)   # '7월 1일', '8워 27일' 같은 손으로 쓴 날짜
    if m:
        pm, pd = int(m.group(1)), int(m.group(2))
        # 1~3월 칸에 10~12월 날짜 = 전년도 말에 미리 낸 것
        py = year - 1 if (month <= 3 and pm >= 10) else year
        try:
            return '납부', date(py, pm, pd).isoformat()
        except ValueError:
            return None
    return None


def parse_club(data, from_year=2026, today_ym=None):
    """엑셀 → {'members': [...], 'fees': [...], 'roles': [...]}
    members: name, role, exempt, status, sort, since / fees: name, ym, kind, paid_on"""
    book = read_xlsx(data)
    sheets = sorted((int(m.group(1)), n) for n in book if (m := re.search(r'회비내역\((\d{4})\)', n)))
    sheets = [(y, n) for y, n in sheets if y >= from_year]
    if not sheets:
        raise ValueError(f'{from_year}년 이후 "회비내역(연도)" 탭을 찾지 못했어요')
    members, fees, roles = {}, {}, []

    def add_fee(name, ym, kind, paid_on):
        if (name, ym) not in fees:
            fees[(name, ym)] = {'name': name, 'ym': ym, 'kind': kind, 'paid_on': paid_on}

    for year, sname in sheets:
        cells = book[sname]
        # 제목 줄: '1월'~'12월' 과 '비고' 가 있는 행
        hdr_row, months, note_col = None, {}, None
        for (r, c), v in sorted(cells.items()):
            if isinstance(v, str) and re.fullmatch(r'\s*(\d{1,2})\s*월\s*', v) and r < 10:
                hdr_row = r; months[c] = int(re.sub(r'\D', '', v))
            if isinstance(v, str) and v.replace(' ', '') == '비고' and r < 10:
                note_col = c
        if not months:
            continue
        r = hdr_row + 1
        while True:
            a, name = cells.get((r, 'A')), cells.get((r, 'B'))
            if isinstance(a, str) and '회원' in a:
                break
            if r > hdr_row + 300:
                break
            r += 1
            if not isinstance(name, str) or not name.strip():
                continue
            name = re.sub(r'\s+', '', name)
            role = cells.get((r - 1, 'C'))
            role = re.sub(r'\s+', '', role) if isinstance(role, str) and not re.search(r'\d', role) else ''
            note = re.sub(r'\s+', '', str(cells.get((r - 1, note_col)) or '')) if note_col else ''
            mem = members.get(name)
            if not mem:
                mem = members[name] = {'name': name, 'role': role, 'exempt': 0, 'left': False,
                                       'sort': len(members) + 1, 'last': None}
            if role:
                mem['role'] = role
                if role not in roles: roles.append(role)
            if '면제' in note: mem['exempt'] = 1
            if '탈퇴' in note: mem['left'] = True
            paid_dates = []
            for col, mo in months.items():
                got = _cell(cells.get((r - 1, col)), year, mo)
                if not got:
                    continue
                kind, paid_on = got
                ym = f'{year}-{mo:02d}'
                add_fee(name, ym, kind, paid_on)
                if kind == '탈퇴': mem['left'] = True
                if kind == '납부': paid_dates.append(paid_on)
                if today_ym is None or ym <= today_ym:
                    mem['last'] = kind if (mem['last'] is None or ym >= mem.get('last_ym', '')) else mem['last']
                    mem['last_ym'] = max(ym, mem.get('last_ym', ''))
            # 비고 '(2027년 1,2월 선납)' → 그 달들을 납부로 (날짜는 이 줄의 마지막 납부일)
            for py, mons in re.findall(r'(\d{4})년([\d,]+)월선납', note):
                if int(py) < from_year or '환불' in note:
                    continue
                for mo in mons.split(','):
                    if mo:
                        add_fee(name, f'{py}-{int(mo):02d}', '납부', max(paid_dates) if paid_dates else None)

    out_members = []
    for mem in members.values():
        status = '탈퇴' if mem['left'] else (mem['last'] if mem['last'] in ('병가', '출장') else '활동')
        yms = [f['ym'] for f in fees.values() if f['name'] == mem['name']]
        out_members.append({'name': mem['name'], 'role': mem['role'], 'exempt': mem['exempt'],
                            'status': status, 'sort': mem['sort'], 'since': min(yms) if yms else None})
    return {'members': out_members, 'fees': list(fees.values()), 'roles': roles}


# ── 입금 알림 → 회비 자동 판단 ─────────────────────────
def next_due(rows, today_ym):
    """아직 안 낸 가장 이른 달. rows = {ym: kind}
    마지막으로 낸 달 다음 달부터 (지난 병가·출장 달은 건너뜀). 낸 적 없으면 이번 달"""
    paid = [ym for ym, k in rows.items() if k == '납부']
    if not paid:
        ym = today_ym
    else:
        ym = ym_add(max(paid), 1)
        while ym < today_ym and rows.get(ym) in ('병가', '출장'):
            ym = ym_add(ym, 1)
    return ym


def mentioned_month(text, today_ym):
    """'11월회비홍길동' → '2026-11'. 가까운 해로 맞춤 (12월에 '1월' 이면 다음 해)"""
    m = re.search(r'(\d{1,2})월', text)
    if not m or not 1 <= int(m.group(1)) <= 12:
        return None
    mo, y, tm = int(m.group(1)), int(today_ym[:4]), int(today_ym[5:7])
    if mo - tm <= -6: y += 1
    elif mo - tm >= 6: y -= 1
    return f'{y}-{mo:02d}'


def plan_fee(sender, amount, fee, members, rows_by_member, today_ym):
    """자동 처리 계획 [(member_id, [ym…])] 또는 None(판단 불가 → 관리자가 직접: '미정')
    members: [{id, name, status}] (탈퇴 제외), rows_by_member: {id: {ym: kind}}
    - 보낸 사람 표시 안에 들어 있는 회원 이름을 모두 찾음 (두 사람 이름이 같이 올 수 있음)
    - 금액 ÷ 회비 = 전체 개월 수, 사람 수로 똑같이 나눔 (안 나누어떨어지면 미정)
    - '11월' 같은 달이 적혀 있으면 그 달부터, 없으면 각자 아직 안 낸 가장 이른 달부터"""
    if not amount or not fee or amount % fee:
        return None
    s = re.sub(r'\s+', '', sender or '')
    found = [m for m in members if m['name'] and m['name'] in s]
    found = [m for m in found if not any(o is not m and m['name'] in o['name'] and m['name'] != o['name'] for o in found)]
    names = [m['name'] for m in found]
    if not found or len(set(names)) != len(names):      # 못 찾았거나 동명이인 → 미정
        return None
    total = amount // fee
    if total % len(found) or total // len(found) > 12:
        return None
    per = total // len(found)
    want = mentioned_month(re.sub('|'.join(map(re.escape, names)), '', s), today_ym)
    plan = []
    for m in found:
        rows = rows_by_member.get(m['id'], {})
        start = want or next_due(rows, today_ym)
        yms = ym_range(start, per)
        if any(rows.get(ym) == '납부' for ym in yms):   # 이미 낸 달과 겹치면 미정
            return None
        plan.append((m['id'], yms))
    return plan


def plan_text(plan, names, today_ym=None):
    """[(id, yms)] → '홍길동 11월 · 김철수 11~12월' (올해가 아니면 '27년 1월' 처럼 연도도)"""
    def lab(yms):
        a, b = yms[0], yms[-1]
        yr = '' if today_ym and a[:4] == today_ym[:4] else f'{a[2:4]}년 '
        if a == b: return f'{yr}{int(a[5:])}월'
        if a[:4] == b[:4]: return f'{yr}{int(a[5:])}~{int(b[5:])}월'
        return f"{a[2:4]}년 {int(a[5:])}월~{b[2:4]}년 {int(b[5:])}월"
    return ' · '.join(f'{names[i]} {lab(yms)}' for i, yms in plan)


# ── 엑셀 만들기 (회비 현황 다운로드, 지금 쓰는 엑셀 양식처럼) ─────────
def _esc(s):
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _colname(i):          # 0 → A
    s, i = '', i + 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


# 스타일 번호: 0 기본, 1 제목, 2 머리글, 3 칸, 4 파란 글자(병가·출장), 5 빨간 글자(탈퇴), 6 선납(연초록), 7 회색 칸, 8 이름 칸
_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<fonts count="5"><font><sz val="10"/><name val="맑은 고딕"/></font>'
    '<font><b/><sz val="16"/><name val="맑은 고딕"/></font>'
    '<font><b/><sz val="10"/><name val="맑은 고딕"/></font>'
    '<font><b/><sz val="10"/><color rgb="FF1D4ED8"/><name val="맑은 고딕"/></font>'
    '<font><b/><sz val="10"/><color rgb="FFDC2626"/><name val="맑은 고딕"/></font></fonts>'
    '<fills count="5"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FFF2F2F2"/></patternFill></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FFE4F4EA"/></patternFill></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FFD9D9D9"/></patternFill></fill></fills>'
    '<borders count="2"><border/><border><left style="thin"/><right style="thin"/><top style="thin"/><bottom style="thin"/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    # 9번 = 금액 (천 단위 쉼표 #,##0 — 엑셀 기본 서식 3번)
    '<cellXfs count="10">' + ''.join(
        f'<xf numFmtId="{nf}" fontId="{f}" fillId="{fl}" borderId="{b}" xfId="0" applyNumberFormat="1" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
        f'<alignment horizontal="{al}" vertical="center"/></xf>'
        for nf, f, fl, b, al in ((0, 0, 0, 0, 'center'), (0, 1, 0, 0, 'center'), (0, 2, 2, 1, 'center'), (0, 0, 0, 1, 'center'),
                                 (0, 3, 0, 1, 'center'), (0, 4, 0, 1, 'center'), (0, 0, 3, 1, 'center'), (0, 0, 4, 1, 'center'),
                                 (0, 2, 0, 1, 'center'), (3, 2, 0, 1, 'right'))) +
    '</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')

_CT = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
       '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
       '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
       '<Default Extension="xml" ContentType="application/xml"/>'
       '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
       '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
       '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>')
_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
         '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
_WB_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')


def write_xlsx(sheet_name, grid, widths, merges=()):
    """grid: 행마다 [(값, 스타일번호)…] → xlsx 바이트"""
    rows = []
    for ri, row in enumerate(grid, 1):
        cs = []
        for ci, (v, s) in enumerate(row):
            ref = f'{_colname(ci)}{ri}'
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                cs.append(f'<c r="{ref}" s="{s}"><v>{v}</v></c>')
            elif v in ('', None):
                cs.append(f'<c r="{ref}" s="{s}"/>')
            else:
                cs.append(f'<c r="{ref}" s="{s}" t="inlineStr"><is><t>{_esc(v)}</t></is></c>')
        rows.append(f'<row r="{ri}" ht="{30 if ri == 1 else 20}" customHeight="1">{"".join(cs)}</row>')
    cols = ''.join(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>' for i, w in enumerate(widths, 1))
    mg = (f'<mergeCells count="{len(merges)}">' + ''.join(f'<mergeCell ref="{m}"/>' for m in merges) + '</mergeCells>') if merges else ''
    sheet = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             '<sheetViews><sheetView workbookViewId="0"><pane xSplit="3" ySplit="3" topLeftCell="D4" activePane="bottomRight" state="frozen"/></sheetView></sheetViews>'
             f'<cols>{cols}</cols><sheetData>{"".join(rows)}</sheetData>{mg}</worksheet>')
    wb = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
          f'<sheets><sheet name="{_esc(sheet_name)}" sheetId="1" r:id="rId1"/></sheets></workbook>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, body in (('[Content_Types].xml', _CT), ('_rels/.rels', _RELS), ('xl/workbook.xml', wb),
                           ('xl/_rels/workbook.xml.rels', _WB_RELS), ('xl/styles.xml', _STYLES), ('xl/worksheets/sheet1.xml', sheet)):
            z.writestr(name, body)
    return buf.getvalue()


def fee_sheet(year, members, fees):
    """회비 현황 엑셀: 번호·이름·임원·1~12월·비고 + 아래에 납부 인원·합계
    members: [{id,name,role,status,exempt,since}], fees: {id: {ym: {kind, paid_on, amount}}}
    미리 낸 달은 '1월 3일 (선납)' 처럼 낸 날짜 + 선납"""
    y = str(year)
    months = [f'{y}-{m:02d}' for m in range(1, 13)]
    grid = [[(f'상도초클럽 회비현황({y}년)', 1)] + [('', 1)] * 15, [],
            [(h, 2) for h in ['번호', '이름', '임 원'] + [f'{m}월' for m in range(1, 13)] + ['비고']]]
    paid_n, total = [0] * 12, [0] * 12
    joined, quit_n, members_n = [0] * 12, [0] * 12, [0] * 12
    # 처음 기록이 시작된 달에 있던 회원은 '원래 회원' (가입자로 세지 않음)
    first_since = min((m.get('since') or months[0] for m in members), default=months[0])
    no = 0
    for m in members:
        rows = fees.get(m['id'], {})
        left = min((ym for ym, r in rows.items() if r['kind'] == '탈퇴'), default=None)
        since = m.get('since') or months[0]
        for i, ym in enumerate(months):
            if since == ym and since != first_since: joined[i] += 1      # 그 달 가입
            if left == ym: quit_n[i] += 1                                 # 그 달 탈퇴
            if since <= ym and not (left and left <= ym): members_n[i] += 1   # 그 달 회원 (병가·출장·면제 포함)
        if (left and left < months[0]) or since > months[-1]:
            continue                                   # 그 해에 회원이 아니었음
        no += 1
        line = [(no, 3), (m['name'], 8), (m['role'] or '', 3)]
        for i, ym in enumerate(months):
            r = rows.get(ym)
            if r and r['kind'] == '납부':
                p = r.get('paid_on') or ''
                pre = bool(p) and p[:7] < ym
                txt = (f'{int(p[5:7])}월 {int(p[8:10])}일' + (' (선납)' if pre else '')) if p else '납부'
                line.append((txt, 6 if pre else 3))
                total[i] += r.get('amount') or 0
                if not m['exempt']: paid_n[i] += 1          # 납부 인원은 재무·총무(회비 면제) 제외
            elif r:
                line.append((r['kind'], 5 if r['kind'] == '탈퇴' else 4))
            elif (left and ym > left) or ym < since:
                line.append(('', 7))
            elif m['exempt']:
                line.append(('면제', 7))
            else:
                line.append(('', 3))
        note = []
        if m['exempt']: note.append('면 제')
        if m['status'] == '탈퇴': note.append('탈 퇴')
        nxt = sorted(int(ym[5:]) for ym, r in rows.items() if ym.startswith(str(int(y) + 1)) and r['kind'] == '납부')
        if nxt: note.append(f"({int(y) + 1}년 {','.join(map(str, nxt))}월 선납)")
        line.append((' '.join(note), 5 if '탈 퇴' in note else 3))
        grid.append(line)
    grid.append([])
    foot = (('납부 인원 (재무·총무 제외)', paid_n, ''), ('가입자', joined, sum(joined)), ('탈퇴자', quit_n, sum(quit_n)),
            ('총회원 (병가·출장 포함)', members_n, ''), ('회비 합계', total, sum(total)))
    for label, vals, tail in foot:
        st = 9 if label == '회비 합계' else 3          # 금액은 천 단위 쉼표
        grid.append([(label, 2), ('', 2), ('', 2)] + [(v, st) for v in vals] + [(tail, st)])
    n = len(grid)
    merges = ['A1:P1'] + [f'A{r}:C{r}' for r in range(n - len(foot) + 1, n + 1)]
    return write_xlsx(f'회비내역({y})', grid, [6, 10, 10] + [12] * 12 + [24], merges)
