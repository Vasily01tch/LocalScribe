import json, os, subprocess, tempfile, time, unittest, urllib.error, urllib.request, wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.dir = Path(self.tmp.name)
        env = dict(os.environ, LOCALSCRIBE_DATA=str(self.dir / 'data'), LOCALSCRIBE_TOKEN='test-token', LOCALSCRIBE_RECORDER='/tmp/localscribe-recorder', PYTHONPYCACHEPREFIX='/tmp/localscribe-test-cache')
        self.proc = subprocess.Popen(['python3', str(ROOT/'backend/server.py')], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
        self.port = json.loads(self.proc.stdout.readline())['port']
    def tearDown(self):
        self.proc.terminate(); self.proc.wait(timeout=5); self.proc.stdout.close(); self.proc.stderr.close(); self.tmp.cleanup()
    def request(self, path, data=None, token='test-token'):
        req=urllib.request.Request('http://127.0.0.1:%s%s'%(self.port,path), data=None if data is None else json.dumps(data).encode(),headers={'X-LocalScribe-Token':token, 'Content-Type':'application/json'})
        with urllib.request.urlopen(req, timeout=5) as r: return json.load(r)
    def test_import_and_failed_transcription_preserve_audio(self):
        file=self.dir/'meeting.wav'
        with wave.open(str(file),'wb') as w: w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000); w.writeframes(b'\0\0'*8000)
        id=self.request('/import',{'paths':[str(file)],'transcribe':False})['ids'][0]
        row=self.request('/library')[0]; self.assertEqual(row['id'],id); self.assertEqual(row['duration'],1)
        file.unlink(); self.assertTrue(Path(row['path']).exists())
        request=urllib.request.Request('http://127.0.0.1:%s/audio/%s?token=test-token'%(self.port,id),headers={'Range':'bytes=0-11'})
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status,206); self.assertEqual(response.headers['Content-Type'],'audio/x-wav'); self.assertEqual(response.read()[:4],b'RIFF')
        self.request('/transcribe',{'id':id})
        for _ in range(100):
            row=self.request('/library')[0]
            if row['status']=='error': break
            time.sleep(.05)
        self.assertEqual(row['status'],'error'); self.assertTrue(Path(row['path']).exists()); self.assertTrue(row['error'])
        self.request('/rename',{'id':id,'title':'Планирование'}); self.assertEqual(self.request('/library')[0]['title'],'Планирование')
        segments=[{'start':0,'end':1,'text':'Тест'}]; self.request('/segments',{'id':id,'segments':segments}); self.assertEqual(json.loads(self.request('/library')[0]['segments']),segments)
    def test_auth_and_settings(self):
        with self.assertRaises(urllib.error.HTTPError) as e: self.request('/library',token='bad')
        self.assertEqual(e.exception.code,403)
        self.request('/settings',{'prompt':'API, Kubernetes','auto_transcribe':True}); self.assertEqual(self.request('/settings')['prompt'],'API, Kubernetes')
if __name__ == '__main__': unittest.main()
