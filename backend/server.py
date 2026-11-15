#!/usr/bin/env python3
"""LocalScribe local service. No audio or transcript leaves this machine."""
import importlib, json, mimetypes, multiprocessing, os, queue, re, shutil, signal, sqlite3, subprocess, sys, threading, time, uuid, wave

# PyInstaller must divert multiprocessing helpers before app initialization.
if __name__ == "__main__":
    multiprocessing.freeze_support()
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(os.environ.get('LOCALSCRIBE_DATA', Path.home() / 'Library/Application Support/LocalScribe'))
ROOT.mkdir(parents=True, exist_ok=True)
MEDIA = ROOT / 'media'; MEDIA.mkdir(exist_ok=True)
PREVIEWS = ROOT / 'previews'; PREVIEWS.mkdir(exist_ok=True)
preview_lock = threading.Lock()
DB = ROOT / 'library.sqlite3'
TOKEN = os.environ['LOCALSCRIBE_TOKEN']
RECORDER = os.environ.get('LOCALSCRIBE_RECORDER', str(Path(__file__).resolve().parent.parent / 'native/recorder'))
MODEL = os.environ.get('LOCALSCRIBE_MODEL', 'mlx-community/whisper-large-v2-mlx')
os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'
lock = threading.RLock(); jobs = queue.Queue(); recorder = None; inference = None; record_id = None; record_state = 'idle'; level = 0.0

def db():
    con = sqlite3.connect(DB, timeout=30); con.row_factory = sqlite3.Row; return con

def init():
    with db() as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS recordings(id TEXT PRIMARY KEY, title TEXT NOT NULL, path TEXT NOT NULL, created TEXT NOT NULL, duration REAL DEFAULT 0, status TEXT NOT NULL, error TEXT DEFAULT '', segments TEXT DEFAULT '[]', source TEXT DEFAULT 'import');
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS watched(path TEXT PRIMARY KEY, mtime REAL NOT NULL, size INTEGER NOT NULL);''')
        columns = {row[1] for row in c.execute('PRAGMA table_info(recordings)')}
        if 'progress' not in columns: c.execute('ALTER TABLE recordings ADD COLUMN progress REAL DEFAULT 0')
        c.execute("UPDATE recordings SET status='error', error='Обработка прервана. Запустите её повторно.' WHERE status IN ('queued','processing','recording')")

def settings():
    with db() as c: rows = c.execute('SELECT key,value FROM settings').fetchall()
    result = {'prompt':'', 'auto_transcribe':False, 'watch_folder':''}
    for row in rows:
        try: result[row['key']] = json.loads(row['value'])
        except ValueError: pass
    return result

def update(id, **values):
    with db() as c: c.execute('UPDATE recordings SET ' + ','.join(k+'=?' for k in values) + ' WHERE id=?', (*values.values(), id))

def get(id):
    with db() as c: row = c.execute('SELECT * FROM recordings WHERE id=?', (id,)).fetchone()
    if not row: raise ValueError('Запись не найдена')
    return dict(row)

def duration(path):
    try:
        with wave.open(str(path)) as f: return f.getnframes() / f.getframerate()
    except Exception:
        try:
            # The bundled static FFmpeg can inspect containers without an external ffprobe.
            result = subprocess.run(['ffmpeg','-hide_banner','-i',str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=20)
            match = re.search(r'Duration: (\d+):(\d+):(\d+(?:\.\d+)?)', result.stderr)
            if match: return int(match[1])*3600 + int(match[2])*60 + float(match[3])
        except Exception: pass
        return 0

def playback_path(row):
    source = Path(row['path'])
    # WebKit does not support every container accepted by FFmpeg. Keep the
    # original untouched and create an audio-only playback copy on demand.
    if source.suffix.lower() in {'.wav', '.mp3', '.m4a', '.mp4', '.aac'}:
        return source
    target = PREVIEWS / (row['id'] + '.m4a')
    with preview_lock:
        if not target.exists():
            temporary = PREVIEWS / (row['id'] + '.partial.m4a')
            try:
                result = subprocess.run(['ffmpeg','-v','error','-nostdin','-y','-i',str(source),'-map','0:a:0','-vn','-c:a','aac','-b:a','128k',str(temporary)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                if result.returncode: raise ValueError('Не удалось подготовить аудио для прослушивания: '+result.stderr[-500:])
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
    return target

def file_signature(path):
    stat = Path(path).stat()
    return (stat.st_mtime_ns, stat.st_size, stat.st_ino, stat.st_mtime)

def new_record(path, title, source, watched_signature=None):
    id = uuid.uuid4().hex
    ext = Path(path).suffix.lower() or '.wav'
    dest = MEDIA / (id + ext)
    temporary = MEDIA / (id + '.partial')
    try:
        # Never publish a partly copied recording, including when the source is
        # still being written by another application.
        before = file_signature(path)
        if watched_signature is not None and before != watched_signature:
            raise ValueError('Файл изменился до импорта')
        shutil.copy2(path, temporary)
        if file_signature(path) != before or temporary.stat().st_size != before[1]:
            raise ValueError('Файл изменился во время импорта')
        temporary.replace(dest)
        with db() as c:
            c.execute('INSERT INTO recordings(id,title,path,created,duration,status,source) VALUES(?,?,?,?,?,?,?)',
                (id,title,str(dest),datetime.now(timezone.utc).isoformat(),duration(dest),'queued' if watched_signature else 'ready',source))
            if watched_signature:
                c.execute('INSERT OR REPLACE INTO watched VALUES(?,?,?)', (str(path), before[3], before[1]))
        if watched_signature: jobs.put(id)
        return id
    except Exception:
        temporary.unlink(missing_ok=True)
        dest.unlink(missing_ok=True)
        raise

def enqueue(id):
    with lock:
        row = get(id)
        if row['status'] in ('queued','processing','recording'): raise ValueError('Запись уже обрабатывается')
        update(id, status='queued', error='', progress=0); jobs.put(id)

def run_inference():
    """Run MLX in a separate process: a native crash must not kill the library."""
    try:
        request = json.loads(sys.stdin.readline())
        import mlx_whisper
        module = importlib.import_module('mlx_whisper.transcribe')
        original = module.tqdm.tqdm
        class Progress(original):
            def __init__(self, *args, **kwargs):
                kwargs['disable'] = True
                super().__init__(*args, **kwargs)
                self._completed = 0
            def update(self, n=1):
                self._completed += n
                if self.total:
                    print(json.dumps({'progress': min(0.99, self._completed / self.total)}), flush=True)
        module.tqdm.tqdm = Progress
        result = mlx_whisper.transcribe(request['path'], path_or_hf_repo=request['model'], language='ru', initial_prompt=request['prompt'] or None, verbose=False)
        segments = [{'start':float(s['start']), 'end':float(s['end']), 'text':s['text'].strip()} for s in result.get('segments', [])]
        print(json.dumps({'segments':segments}, ensure_ascii=False), flush=True)
    except Exception as e:
        print(json.dumps({'error':str(e)}, ensure_ascii=False), flush=True)
        sys.exit(1)

def worker():
    global inference
    while True:
        id = jobs.get()
        try:
            row = get(id); update(id,status='processing',error='',progress=0)
            cmd = [sys.executable, '--transcribe'] if getattr(sys, 'frozen', False) else [sys.executable, str(Path(__file__).resolve()), '--transcribe']
            inference = subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,bufsize=1)
            inference.stdin.write(json.dumps({'path':row['path'],'model':MODEL,'prompt':settings()['prompt']},ensure_ascii=False)+'\n'); inference.stdin.close()
            result = None; error = None; last_update = 0.0
            for line in inference.stdout:
                try: message = json.loads(line)
                except ValueError: continue
                if 'progress' in message and time.monotonic() - last_update > 0.5:
                    update(id,progress=message['progress']); last_update = time.monotonic()
                if 'segments' in message: result = message['segments']
                if 'error' in message: error = message['error']
            code = inference.wait(); inference = None
            if error: raise RuntimeError(error)
            if code or result is None: raise RuntimeError('Движок распознавания неожиданно завершился. Проверьте модель и поддержку Metal; исходное аудио сохранено.')
            update(id,status='done',segments=json.dumps(result,ensure_ascii=False),duration=duration(row['path']) or (result[-1]['end'] if result else 0),progress=1)
        except Exception as e: update(id,status='error',error=str(e),progress=0)
        finally: jobs.task_done()

def monitor(proc, id):
    global recorder, record_id, record_state, level
    error = ''
    for line in proc.stdout:
        try:
            data = json.loads(line)
            if 'level' in data: level = data['level']
            if 'state' in data: record_state = data['state']
            if 'error' in data: error = data['error']
        except ValueError: pass
    proc.wait()
    row = get(id); d = duration(row['path'])
    update(id, duration=d, status='error' if error or proc.returncode else 'ready', error=error or ('Запись неожиданно прервана.' if proc.returncode else ''))
    with lock: recorder = None; record_id = None; record_state = 'idle'; level = 0.0
    if not error and proc.returncode == 0 and settings()['auto_transcribe']:
        try: enqueue(id)
        except ValueError: pass

def start_record(device):
    global recorder, record_id, record_state
    with lock:
        if recorder: raise ValueError('Запись уже идёт')
        id = uuid.uuid4().hex; path = MEDIA / (id + '.wav')
        with db() as c: c.execute('INSERT INTO recordings(id,title,path,created,status,source) VALUES(?,?,?,?,?,?)', (id,'Запись ' + datetime.now().strftime('%d.%m.%Y %H:%M'),str(path),datetime.now(timezone.utc).isoformat(),'recording','microphone'))
        try: recorder = subprocess.Popen([RECORDER,str(path),str(device or 0)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1)
        except Exception as e: update(id,status='error',error=str(e)); raise
        record_id = id; record_state = 'starting'; threading.Thread(target=monitor,args=(recorder,id),daemon=True).start(); return id

def scan_watch_folder(folder):
    extensions = {'.wav','.mp3','.m4a','.mp4','.mov','.aac','.flac','.ogg','.webm','.aiff','.aif','.mkv'}
    if not folder or not Path(folder).is_dir(): return
    for path in Path(folder).iterdir():
        try:
            if not path.is_file() or path.suffix.lower() not in extensions: continue
            stat = path.stat()
            if time.time() - stat.st_mtime < 10: continue
            with db() as c: old = c.execute('SELECT mtime,size FROM watched WHERE path=?',(str(path),)).fetchone()
            if old and old['mtime']==stat.st_mtime and old['size']==stat.st_size: continue
            new_record(path,path.stem,'folder',(stat.st_mtime_ns,stat.st_size,stat.st_ino,stat.st_mtime))
        except (OSError, ValueError) as e:
            # A file being written or removed must not stall the rest.
            print('watch:',path,e,file=sys.stderr,flush=True)

def watch():
    while True:
        try: scan_watch_folder(settings()['watch_folder'])
        except Exception as e: print('watch:',e,file=sys.stderr,flush=True)
        time.sleep(10)

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*'); self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-LocalScribe-Token'); self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS'); super().end_headers()
    def do_OPTIONS(self): self.send_response(204); self.end_headers()
    def send_json(self, value, status=200):
        data=json.dumps(value,ensure_ascii=False).encode(); self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
    def authorized(self):
        p=urlparse(self.path); return self.headers.get('X-LocalScribe-Token') == TOKEN or (p.path.startswith('/audio/') and parse_qs(p.query).get('token',[''])[0]==TOKEN)
    def do_GET(self):
        if not self.authorized(): return self.send_json({'error':'Unauthorized'},403)
        p=urlparse(self.path).path
        try:
            if p=='/library':
                with db() as c: rows=[dict(x) for x in c.execute('SELECT * FROM recordings ORDER BY created DESC')]
                return self.send_json(rows)
            if p=='/settings': return self.send_json(settings())
            if p=='/recording': return self.send_json({'id':record_id,'state':record_state,'level':level})
            if p=='/devices':
                result=subprocess.check_output([RECORDER,'--devices'],text=True,timeout=10); return self.send_json(json.loads(result))
            if p.startswith('/audio/'):
                path=playback_path(get(p.split('/')[-1])); size=path.stat().st_size; start=0; end=size-1
                range_header=self.headers.get('Range','')
                if range_header.startswith('bytes='):
                    parts=range_header[6:].split('-',1); start=int(parts[0] or 0); end=min(int(parts[1]) if parts[1] else end,end)
                if start > end: self.send_response(416); self.end_headers(); return
                self.send_response(206 if range_header else 200); self.send_header('Content-Type', mimetypes.guess_type(str(path))[0] or 'application/octet-stream'); self.send_header('Accept-Ranges','bytes'); self.send_header('Content-Length',str(end-start+1))
                if range_header: self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
                self.end_headers()
                with path.open('rb') as f:
                    f.seek(start); remaining=end-start+1
                    while remaining:
                        chunk=f.read(min(1024*1024,remaining))
                        if not chunk: break
                        self.wfile.write(chunk); remaining-=len(chunk)
                return
            self.send_json({'error':'Not found'},404)
        except Exception as e: self.send_json({'error':str(e)},400)
    def do_POST(self):
        global record_state
        if not self.authorized(): return self.send_json({'error':'Unauthorized'},403)
        try:
            data=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))) or '{}'); p=urlparse(self.path).path
            if p=='/record/start': return self.send_json({'id':start_record(data.get('device'))})
            if p=='/record/control':
                command=data['command']
                if command not in ('pause','resume','stop') or not recorder: raise ValueError('Запись не активна')
                if record_state == 'starting':
                    if command != 'stop': raise ValueError('Дождитесь ответа на запрос доступа к микрофону')
                    recorder.terminate()
                else:
                    recorder.stdin.write(command+'\n'); recorder.stdin.flush()
                return self.send_json({'ok':True})
            if p=='/import':
                ids=[]
                for path in data['paths']:
                    f=Path(path)
                    if not f.is_file(): raise ValueError('Файл не найден: '+path)
                    id=new_record(f,f.stem,'import'); ids.append(id)
                    if data.get('transcribe',True): enqueue(id)
                return self.send_json({'ids':ids})
            if p=='/transcribe': enqueue(data['id']); return self.send_json({'ok':True})
            if p=='/rename':
                title=data['title'].strip()
                if not title: raise ValueError('Название не может быть пустым')
                update(data['id'],title=title); return self.send_json({'ok':True})
            if p=='/segments':
                segments=data['segments'];
                if not isinstance(segments,list): raise ValueError('Неверный формат')
                update(data['id'],segments=json.dumps(segments,ensure_ascii=False)); return self.send_json({'ok':True})
            if p=='/export':
                Path(data['path']).write_text(data['text'], encoding='utf-8'); return self.send_json({'ok':True})
            if p=='/settings':
                with db() as c:
                    for key in ('prompt','auto_transcribe','watch_folder'):
                        if key in data: c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(key,json.dumps(data[key],ensure_ascii=False)))
                return self.send_json({'ok':True})
            return self.send_json({'error':'Not found'},404)
        except Exception as e: return self.send_json({'error':str(e)},400)

if __name__=='__main__' and '--transcribe' in sys.argv:
    run_inference(); sys.exit(0)

if __name__=='__main__':
    init(); threading.Thread(target=worker,daemon=True).start(); threading.Thread(target=watch,daemon=True).start()
    server=ThreadingHTTPServer(('127.0.0.1',int(os.environ.get('LOCALSCRIBE_PORT','0'))),Handler)
    def shutdown(_signum, _frame):
        global recorder, inference
        if inference and inference.poll() is None: inference.terminate()
        if recorder and recorder.poll() is None:
            try:
                recorder.stdin.write('stop\n'); recorder.stdin.flush(); recorder.wait(timeout=5)
            except Exception: recorder.terminate()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    print(json.dumps({'port':server.server_port}),flush=True); server.serve_forever(); server.server_close()
