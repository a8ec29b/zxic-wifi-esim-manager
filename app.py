#!/usr/bin/env python3
import base64, datetime, hashlib, hmac, http.client, json, os, pathlib, re, socket, sqlite3, subprocess, threading, time, urllib.parse, urllib.request
import esim, led
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent
DATA = pathlib.Path(os.environ.get('ZXW_DATA', '/var/lib/zxw-panel'))
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / 'panel.sqlite3'
PASSWORD = (DATA / 'password').read_text().strip()
MODEM_PASSWORD = os.environ.get('ZXW_MODEM_PASSWORD', 'admin')
locks = {}
downloads = {}
notification_retry = {}

def db():
    con = sqlite3.connect(DB, timeout=10)
    con.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    con.execute('CREATE TABLE IF NOT EXISTS seen (device TEXT, fingerprint TEXT, delivered INTEGER, PRIMARY KEY(device, fingerprint))')
    con.execute('CREATE TABLE IF NOT EXISTS initialized (device TEXT PRIMARY KEY)')
    con.execute('CREATE TABLE IF NOT EXISTS notifications (eid TEXT NOT NULL, sequence INTEGER NOT NULL, delivered INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(eid,sequence))')
    con.execute('CREATE TABLE IF NOT EXISTS device_notes (imei TEXT PRIMARY KEY, note TEXT NOT NULL)')
    con.execute('CREATE TABLE IF NOT EXISTS connection_defaults (device TEXT PRIMARY KEY, instance TEXT NOT NULL, connected_at REAL NOT NULL, wifi_done INTEGER NOT NULL DEFAULT 0, data_done INTEGER NOT NULL DEFAULT 0, led_done INTEGER NOT NULL DEFAULT 0)')
    if 'led_done' not in {row[1] for row in con.execute('PRAGMA table_info(connection_defaults)')}:
        con.execute('ALTER TABLE connection_defaults ADD COLUMN led_done INTEGER NOT NULL DEFAULT 0')
    con.execute('CREATE TABLE IF NOT EXISTS led_settings (imei TEXT PRIMARY KEY, snapshot TEXT NOT NULL)')
    return con

def settings():
    with db() as con: return dict(con.execute('SELECT key,value FROM settings'))

def devices():
    found = []
    for net in pathlib.Path('/sys/class/net').iterdir():
        p = (net / 'device').resolve()
        usb = next((x for x in [p, *p.parents] if (x / 'idVendor').exists()), None)
        if not usb or (usb / 'idVendor').read_text().strip() != '19d2': continue
        try:
            lease = pathlib.Path('/run/systemd/netif/leases') / (net / 'ifindex').read_text().strip()
            values = dict(line.split('=', 1) for line in lease.read_text().splitlines() if '=' in line)
            gateway = values.get('ROUTER', '').split(' ')[0]
        except (FileNotFoundError, IndexError): gateway = ''
        found.append({'id': usb.name, 'interface': net.name, 'gateway': gateway, 'name': '中兴随身 Wi-Fi · ' + usb.name})
    return sorted(found, key=lambda d: d['id'])

def sms_datetime(value):
    match=re.fullmatch(r'(\d{2})[,;](\d{1,2})[,;](\d{1,2})[,;](\d{1,2})[,;](\d{1,2})[,;](\d{1,2})[,;]([+-]?\d{1,2})',str(value))
    if not match: return None
    year,month,day,hour,minute,second,zone=map(int,match.groups())
    try:
        return datetime.datetime(2000+year,month,day,hour,minute,second,tzinfo=datetime.timezone(datetime.timedelta(minutes=zone*15)))
    except ValueError:
        return None

class BoundConnection(http.client.HTTPConnection):
    def __init__(self, host, iface):
        super().__init__(host, timeout=8); self.iface = iface
    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, self.iface.encode() + b'\0')
        self.sock.connect((self.host, 80))

class Modem:
    def __init__(self, device): self.d = device; self.cookie = ''; self.api = None
    def request(self, params, post=False):
        form = urllib.parse.urlencode(params)
        headers = {'Referer': 'http://' + self.d['gateway'] + '/index.html', 'Cookie': self.cookie}
        if post: headers['Content-Type'] = 'application/x-www-form-urlencoded'
        for api in ([self.api] if self.api else ['goform','reqproc']):
            conn = BoundConnection(self.d['gateway'], self.d['interface'])
            path = ('/goform/goform_' + ('set' if post else 'get') + '_cmd_process') if api=='goform' else ('/reqproc/proc_' + ('post' if post else 'get'))
            if not post: path += '?' + form
            conn.request('POST' if post else 'GET', path, form if post else None, headers)
            res = conn.getresponse(); content = res.read(); cookie = res.getheader('Set-Cookie')
            if res.status != 404: self.api=api; break
        if cookie: self.cookie = cookie.split(';', 1)[0]
        if res.status != 200: raise RuntimeError('设备返回 HTTP ' + str(res.status))
        return json.loads(content)
    def login(self):
        result = self.request({'goformId':'LOGIN','password':base64.b64encode(MODEM_PASSWORD.encode()).decode()}, True)
        if result.get('result') not in ('0', '4'): raise RuntimeError('设备登录失败：' + str(result.get('result')))
    def get(self, **kwargs): return self.request(kwargs)
    def set(self, **kwargs):
        result = self.request(kwargs, True)
        if result.get('result') != 'success': raise RuntimeError('设备拒绝操作：' + str(result.get('result')))
        return result
    def messages(self):
        result = self.get(cmd='sms_data_total', page=0, data_per_page=500, mem_store=1, tags=10, order_by='order by id desc')
        if 'messages' not in result: raise RuntimeError('设备未返回短信列表')
        for m in result['messages']:
            date=sms_datetime(m.get('date',''))
            if date: m['date_iso']=date.isoformat()
            raw = m.get('content', '')
            try: m['content'] = bytes.fromhex(raw).decode('utf-16-be').replace('\x00', '')
            except (ValueError, UnicodeError): m['content'] = raw
        return result['messages']

def on_device(device_id, action):
    d = next((x for x in devices() if x['id'] == device_id), None)
    if not d or not d['gateway']: raise RuntimeError('设备离线或尚未取得地址')
    with locks.setdefault(device_id, threading.RLock()):
        m = Modem(d); m.login(); return action(m)

def device_status(device):
    status=on_device(device,lambda m:m.get(cmd='wifi_cur_state,m_ssid_enable,network_provider,network_type,signalbar,modem_main_state,ppp_status,imei',multi_data=1))
    with db() as con: row=con.execute('SELECT note FROM device_notes WHERE imei=?',(status.get('imei',''),)).fetchone()
    status['note']=row[0] if row else ''
    try:
        if not status.get('imei'): raise RuntimeError('无法识别设备')
        led.snapshot(device)
        with db() as con: row=con.execute('SELECT 1 FROM led_settings WHERE imei=?',(status.get('imei',''),)).fetchone()
        status['led_enabled']=not bool(row)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        status['led_enabled']=None
    return status

def led_poll():
    while True:
        with db() as con: disabled={row[0] for row in con.execute('SELECT imei FROM led_settings')}
        if disabled:
            for d in devices():
                try:
                    with locks.setdefault(d['id'],threading.RLock()):
                        imei=on_device(d['id'],lambda m:m.get(cmd='imei')['imei'])
                        with db() as con: off=con.execute('SELECT 1 FROM led_settings WHERE imei=?',(imei,)).fetchone()
                        if off: led.off(d['id'])
                except Exception as exc: print('指示灯控制失败:',d['id'],str(exc),flush=True)
        time.sleep(3)

def usb_instance(device):
    usb=pathlib.Path('/sys/bus/usb/devices')/device
    boot=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    return ':'.join((boot,(usb/'busnum').read_text().strip(),(usb/'devnum').read_text().strip()))

def apply_connection_defaults(device,instance,stop):
    with db() as con:
        row=con.execute('SELECT instance,connected_at FROM connection_defaults WHERE device=?',(device,)).fetchone()
        if not row or row[0]!=instance:
            con.execute('INSERT OR REPLACE INTO connection_defaults(device,instance,connected_at) VALUES (?,?,?)',(device,instance,time.time()))
    while not stop.is_set():
        with db() as con:
            row=con.execute('SELECT connected_at,wifi_done,data_done,led_done FROM connection_defaults WHERE device=? AND instance=?',(device,instance)).fetchone()
        if not row or (row[1] and row[2] and row[3]): return
        for kind,due in (('wifi',not row[1]),('led',not row[3]),('data',not row[2] and time.time()>=row[0]+120)):
            if not due or stop.is_set(): continue
            try:
                if kind=='led':
                    with locks.setdefault(device,threading.RLock()):
                        imei=on_device(device,lambda m:m.get(cmd='imei')['imei'])
                        if not imei: raise RuntimeError('无法识别设备')
                        with db() as con: saved=con.execute('SELECT 1 FROM led_settings WHERE imei=?',(imei,)).fetchone()
                        try: state=led.snapshot(device)
                        except led.NoIndicatorError: state=None
                        if state is not None:
                            if not saved:
                                with db() as con: con.execute('INSERT INTO led_settings VALUES (?,?)',(imei,json.dumps(state)))
                            led.off(device)
                        elif saved:
                            with db() as con: con.execute('DELETE FROM led_settings WHERE imei=?',(imei,))
                else:
                    def disable(m):
                        if kind=='wifi':
                            if m.get(cmd='wifi_cur_state').get('wifi_cur_state')!='0': m.set(goformId='SET_WIFI_INFO',wifiEnabled='0')
                        elif m.get(cmd='ppp_status').get('ppp_status')!='ppp_disconnected': m.set(goformId='DISCONNECT_NETWORK')
                    on_device(device,disable)
                with db() as con: con.execute(f'UPDATE connection_defaults SET {kind}_done=1 WHERE device=? AND instance=?',(device,instance))
            except Exception as exc: print('接入默认设置失败:',device,kind,str(exc),flush=True)
        stop.wait(3)

def watch_connections():
    running={}
    while True:
        try:
            current={}
            for d in devices():
                try: current[d['id']]=usb_instance(d['id'])
                except FileNotFoundError: continue
            for device,(instance,stop) in list(running.items()):
                if current.get(device)!=instance:
                    stop.set(); del running[device]
            for device,instance in current.items():
                if device not in running:
                    stop=threading.Event(); running[device]=(instance,stop)
                    threading.Thread(target=apply_connection_defaults,args=(device,instance,stop),daemon=True).start()
        except Exception as exc: print('设备接入检查失败:',str(exc),flush=True)
        time.sleep(3)

def fingerprint(m):
    return hashlib.sha256(json.dumps([m.get(k) for k in ('id','number','date','content')], ensure_ascii=False).encode()).hexdigest()

def lpa_call(device,action,**data):
    return esim.lpa_call(device,action,**data)

def queue_notifications(eid, sequences):
    with db() as con:
        for seq in sequences: con.execute('INSERT OR IGNORE INTO notifications(eid,sequence) VALUES (?,?)',(eid,seq))

def pending_notifications(device):
    with db() as con: queued=con.execute('SELECT eid,sequence FROM notifications WHERE delivered=0').fetchall()
    if not any(notification_retry.get((eid,seq),0)<=time.monotonic() for eid,seq in queued): return
    with locks.setdefault(device,threading.RLock()):
        info=esim.card_info(device)
        with db() as con: pending=con.execute('SELECT sequence FROM notifications WHERE eid=? AND delivered=0 ORDER BY sequence',(info['eid'],)).fetchall()
        for (seq,) in pending:
            key=(info['eid'],seq)
            if notification_retry.get(key,0)>time.monotonic(): continue
            notification_retry[key]=time.monotonic()+300
            try:
                lpa_call(device,'notify',sequence=seq)
                with db() as con: con.execute('UPDATE notifications SET delivered=1 WHERE eid=? AND sequence=?',(info['eid'],seq))
            except Exception as exc: print('运营商通知发送失败:',seq,str(exc),flush=True)

def capture_notifications(device, info, before, related):
    current=lpa_call(device,'list-notifications')
    queue_notifications(info['eid'],[n['sequence'] for n in current if n['sequence']>before and n['iccid'] in related])


def poll():
    while True:
        config = settings()
        if config.get('tg_token') and config.get('tg_chat'):
            for d in devices():
                try:
                    msgs = on_device(d['id'], lambda m: m.messages())
                    incoming = [m for m in msgs if str(m.get('tag')) in ('0','1')]
                    with db() as con:
                        first = not con.execute('SELECT 1 FROM initialized WHERE device=?', (d['id'],)).fetchone()
                        for m in incoming:
                            con.execute('INSERT OR IGNORE INTO seen VALUES (?,?,?)', (d['id'], fingerprint(m), int(first)))
                        con.execute('INSERT OR IGNORE INTO initialized VALUES (?)', (d['id'],))
                    for m in incoming:
                        key = fingerprint(m)
                        with db() as con: pending = con.execute('SELECT delivered FROM seen WHERE device=? AND fingerprint=?', (d['id'],key)).fetchone()[0] == 0
                        if not pending: continue
                        date=sms_datetime(m.get('date',''))
                        display_date=date.astimezone().strftime('%Y-%m-%d %H:%M:%S %z') if date else m.get('date','')
                        payload = urllib.parse.urlencode({'chat_id':config['tg_chat'], 'text':f"{d['name']}\n{m.get('number','')}\n{display_date}\n\n{m.get('content','')}"}).encode()
                        with urllib.request.urlopen('https://api.telegram.org/bot'+config['tg_token']+'/sendMessage', payload, timeout=15) as resp:
                            if not json.load(resp).get('ok'): raise RuntimeError('Telegram 拒绝发送')
                        with db() as con: con.execute('UPDATE seen SET delivered=1 WHERE device=? AND fingerprint=?', (d['id'],key))
                except Exception as e: print('短信转发失败:', d['id'], str(e).replace(config['tg_token'],'[已隐藏]'), flush=True)
        time.sleep(20)

def notification_poll():
    while True:
        for d in devices():
            try: pending_notifications(d['id'])
            except Exception as e: print('运营商通知检查失败:',d['id'],str(e),flush=True)
        time.sleep(20)

class Handler(BaseHTTPRequestHandler):
    def authenticated(self):
        expected = 'Basic ' + base64.b64encode(('admin:'+PASSWORD).encode()).decode()
        return hmac.compare_digest(self.headers.get('Authorization',''), expected)
    def respond(self, status, body, content='application/json; charset=utf-8'):
        body = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status); self.send_header('Content-Type',content); self.send_header('Content-Length',str(len(body))); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(body)
    def auth(self):
        if self.authenticated(): return True
        self.send_response(401); self.send_header('WWW-Authenticate','Basic realm="ZXW Panel"'); self.end_headers(); return False
    def do_GET(self):
        if not self.auth(): return
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == '/api/devices': return self.respond(200,devices())
            if path == '/api/settings':
                s = settings(); return self.respond(200, {'tg_chat':s.get('tg_chat',''), 'tg_configured': bool(s.get('tg_token'))})
            if path.startswith('/api/device/'):
                _,_,_,device,what = path.split('/')
                if what == 'messages': return self.respond(200,on_device(device,lambda m:m.messages()))
                if what == 'esim':
                    with locks.setdefault(device, threading.RLock()): return self.respond(200,esim.card_info(device))
                if what == 'status': return self.respond(200,device_status(device))
                if what == 'download': return self.respond(200,downloads.get(device,{'state':'idle'}))
            assets = {'/':('index.html','text/html; charset=utf-8'), '/app.js':('app.js','text/javascript; charset=utf-8'), '/style.css':('style.css','text/css; charset=utf-8')}
            if path in assets:
                name, content = assets[path]; return self.respond(200,(ROOT/'web'/name).read_bytes(),content)
            self.respond(404,{'error':'未找到'})
        except Exception as e: self.respond(502,{'error':str(e)})
    def do_POST(self):
        if not self.auth(): return
        try:
            if self.headers.get('Content-Type','').split(';')[0] != 'application/json': return self.respond(415,{'error':'请求格式错误'})
            origin = self.headers.get('Origin')
            if origin and urllib.parse.urlsplit(origin).netloc != self.headers.get('Host'): return self.respond(403,{'error':'请求来源不匹配'})
            data = json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))) or b'{}')
            path = urllib.parse.urlsplit(self.path).path
            if path == '/api/settings':
                with db() as con:
                    for key in ('tg_token','tg_chat'):
                        if key in data: con.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(key,data[key].strip()))
                return self.respond(200,{'ok':True})
            if path.startswith('/api/device/'):
                _,_,_,device,action=path.split('/')
                if action == 'led':
                    with locks.setdefault(device,threading.RLock()):
                        imei=on_device(device,lambda m:m.get(cmd='imei')['imei'])
                        if not imei: raise RuntimeError('无法识别设备')
                        with db() as con:
                            row=con.execute('SELECT snapshot FROM led_settings WHERE imei=?',(imei,)).fetchone()
                        if data['enabled']:
                            if row:
                                led.restore(device,json.loads(row[0]))
                                with db() as con: con.execute('DELETE FROM led_settings WHERE imei=?',(imei,))
                        elif not row:
                            state=led.snapshot(device)
                            led.off(device)
                            with db() as con: con.execute('INSERT INTO led_settings VALUES (?,?)',(imei,json.dumps(state)))
                    return self.respond(200,{'result':'success'})
                if action == 'note':
                    imei=on_device(device,lambda m:m.get(cmd='imei')['imei'])
                    note=data['note'].strip()
                    with db() as con: con.execute('INSERT OR REPLACE INTO device_notes(imei,note) VALUES (?,?)',(imei,note))
                    return self.respond(200,{'note':note})
                if action in ('profile','profile-delete','profile-rename'):
                    if device not in [d['id'] for d in devices()]: raise RuntimeError('设备离线')
                    with locks.setdefault(device, threading.RLock()):
                        info=esim.card_info(device)
                        profiles=info['profiles']
                        profile=next((p for p in profiles if p['iccid']==data['iccid']),None)
                        if not profile: raise RuntimeError('配置不存在')
                        before=max((n['sequence'] for n in lpa_call(device,'list-notifications')),default=0) if action != 'profile-rename' else 0
                        related={data['iccid']} | {p['iccid'] for p in profiles if p['state']==1}
                        if action == 'profile-delete':
                            if profile['state'] == 1: raise RuntimeError('请先切换到其他配置再删除')
                            result=esim.delete_profile(device,data['iccid'])
                            if any(p['iccid']==data['iccid'] for p in esim.card_info(device)['profiles']): raise RuntimeError('卡仍显示此配置，请刷新确认')
                            try: capture_notifications(device,info,before,related)
                            except Exception as exc: result['warning']='配置已删除，但无法排队运营商通知：'+str(exc)
                            return self.respond(200,result)
                        if action == 'profile-rename': return self.respond(200,esim.rename_profile(device,data['iccid'],data['nickname']))
                        error=None
                        try: esim.set_profile(device,data['iccid'],bool(data['enabled']))
                        except Exception as exc: error=exc
                        for _ in range(8):
                            time.sleep(2)
                            try:
                                updated=esim.card_info(device)['profiles']
                                actual=next((p['state'] for p in updated if p['iccid']==data['iccid']),None)
                                if actual==int(bool(data['enabled'])):
                                    result={'result':'success'}
                                    try: capture_notifications(device,info,before,related)
                                    except Exception as exc: result['warning']='配置状态已改变，但无法排队运营商通知：'+str(exc)
                                    return self.respond(200,result)
                            except Exception: pass
                        raise RuntimeError('无法确认配置状态：'+str(error or '请稍后刷新'))
                if action == 'data':
                    return self.respond(200,on_device(device,lambda m:m.set(goformId='CONNECT_NETWORK' if data['enabled'] else 'DISCONNECT_NETWORK')))
                if action == 'download':
                    if device not in [d['id'] for d in devices()]: raise RuntimeError('设备离线')
                    if downloads.get(device,{}).get('state')=='running': raise RuntimeError('已有下载正在进行')
                    activation=data['activation'].strip()
                    if not activation.startswith('LPA:1$'): raise ValueError('请输入有效的激活码')
                    downloads[device]={'state':'running'}
                    def run_download():
                        try:
                            with locks.setdefault(device,threading.RLock()):
                                info=esim.card_info(device)
                                imei=on_device(device,lambda m:m.get(cmd='imei')['imei'])
                                answer=esim.lpa_call(device,'download',timeout=600,imei=imei,activation=activation,confirmation=data.get('confirmation',''))
                                if answer.get('notification_failures'):
                                    queue_notifications(info['eid'],answer['notification_failures'])
                                downloads[device]={'state':'done','warning':answer.get('warning','')}
                        except Exception as exc: downloads[device]={'state':'error','error':str(exc)}
                    threading.Thread(target=run_download,daemon=True).start()
                    return self.respond(202,{'state':'running'})
                if action == 'wifi':
                    def wifi(m):
                        state=m.get(cmd='m_ssid_enable',multi_data=1)
                        params={'goformId':'SET_WIFI_INFO','wifiEnabled':'1' if data['enabled'] else '0'}
                        if data['enabled']: params['m_ssid_enable']=state.get('m_ssid_enable','0')
                        return m.set(**params)
                    return self.respond(200,on_device(device,wifi))
                if action == 'send':
                    def send(m):
                        now=datetime.datetime.now().astimezone(); zone=now.utcoffset().total_seconds()/3600
                        stamp=now.strftime('%y;%m;%d;%H;%M;%S;')+f'{zone:+g}'
                        number=data['number'].strip(); message=data['message'].strip()
                        if not number or not message: raise ValueError('请填写号码和短信内容')
                        m.set(goformId='SEND_SMS',Number=number,sms_time=stamp,MessageBody=message.encode('utf-16-be').hex().upper(),ID='-1',encode_type='UNICODE')
                        for _ in range(30):
                            time.sleep(1); result=m.get(cmd='sms_cmd_status_info',sms_cmd=4).get('sms_cmd_status_result')
                            if result == '3': return {'result':'success'}
                            if result == '2': raise RuntimeError('设备报告短信发送失败')
                        raise RuntimeError('设备尚未确认发送结果，请检查已发送短信')
                    return self.respond(200,on_device(device,send))
                if action == 'delete': return self.respond(200,on_device(device,lambda m:m.set(goformId='DELETE_SMS',msg_id=str(data['id'])+';')))
                if action == 'read': return self.respond(200,on_device(device,lambda m:m.set(goformId='SET_MSG_READ',msg_id=str(data['id'])+';',tag=0)))
            self.respond(404,{'error':'未找到'})
        except Exception as e: self.respond(502,{'error':str(e)})

if __name__ == '__main__':
    with db(): pass
    threading.Thread(target=watch_connections,daemon=True).start()
    threading.Thread(target=led_poll,daemon=True).start()
    threading.Thread(target=poll,daemon=True).start()
    threading.Thread(target=notification_poll,daemon=True).start()
    ThreadingHTTPServer((os.environ.get('ZXW_HOST','127.0.0.1'), int(os.environ.get('ZXW_PORT','9528'))),Handler).serve_forever()
