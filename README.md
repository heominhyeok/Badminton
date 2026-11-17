# Badminton 게스트 신청

배드민턴 모임 게스트 신청 → 게스트비 송금(토스·카카오페이) → 입금 자동 확인 → 관리자 명단·통계.

## 실행

```
python -X utf8 server.py
```

- 주소: http://127.0.0.1:8790 (포트는 환경변수 `PORT`로 변경)
- 파이썬 표준 라이브러리만 사용 (설치할 패키지 없음)
- 처음 실행하면 `data/` 폴더에 DB와 키 파일이 생김 (git에 올리지 않음)
  - `data/badminton.db` 신청·설정·입금 알림 기록
  - `data/access_key.txt` 외부 접속 키 (링크에 `?key=` 로 붙임)
  - `data/admin.json` 관리자 계정 (처음 계정 test / test1234 — 설정 탭에서 비밀번호 변경)
  - `data/hook_key.txt` 입금 알림 전달용 비밀키

## Railway 배포

- GitHub `main`에 올리면 자동 배포 (시작 명령은 `railway.json`)
- 볼륨을 `/data`에 붙임 → `RAILWAY_VOLUME_MOUNT_PATH`로 DB·키 파일이 볼륨에 저장됨
- 환경변수 (Variables 탭)
  - `ACCESS_KEY` 외부 접속 키 (없으면 볼륨의 access_key.txt 자동 생성)
  - `HOOK_KEY` 입금 알림 비밀키 (없으면 볼륨의 hook_key.txt 자동 생성)
  - `DATA_DIR` 데이터 폴더 직접 지정 (선택), `HOST` 서버 주소 (Railway에선 자동 0.0.0.0)

## 구성

- `server.py` — API 서버 (신청, 상태 확인, 관리자, 입금 알림 수신)
- `public/index.html` — 게스트 화면 + 관리자 화면

## 입금 자동 확인

안드로이드 폰의 MacroDroid가 카카오뱅크 입금 알림을 서버로 보냄.

- 발동 조건: 알림을 받으면 · 카카오뱅크 · 텍스트 조건 없음 · 연속 발동 방지 해제
- 동작: HTTP 요청 POST `/api/hook/deposit?k=<data/hook_key.txt>` · text/plain
- 본문: `{not_title} {notification}`

알림의 금액이 게스트비와 같고, 보낸 사람 표시에 신청 이름이 들어 있을 때만 자동으로 신청 완료.

## 송금 링크

- 토스: `supertoss://send?amount=&bank=&accountNo=&origin=qr`
- 카카오페이: `kakaotalk://kakaopay/money/to/bank?bank_code=&bank_account_number=&amount=` (은행 코드는 카카오페이 계좌송금 앱링크 가이드 기준)
