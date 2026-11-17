// 앱 설치용 서비스 워커. 신청·입금 상태는 항상 서버에서 새로 받아야 하므로 캐시하지 않고,
// 인터넷이 끊겼을 때 첫 화면 요청에만 안내 문구를 보여준다.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => {
  if (e.request.mode !== 'navigate') return;
  e.respondWith(fetch(e.request).catch(() => new Response(
    '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' +
    '<body style="font-family:sans-serif;display:grid;place-items:center;height:100vh;margin:0;color:#1E1E1E">' +
    '<div style="text-align:center"><p style="font-size:17px;font-weight:700">인터넷에 연결되지 않았어요</p>' +
    '<p style="color:#8E8E93;font-size:14px">연결을 확인한 뒤 다시 열어주세요</p></div>',
    {headers: {'Content-Type': 'text/html; charset=utf-8'}})));
});
