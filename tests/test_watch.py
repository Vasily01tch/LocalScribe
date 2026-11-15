import importlib.util, os, tempfile, time, unittest, wave
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parent.parent
class FolderImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        with patch.dict(os.environ, {'LOCALSCRIBE_DATA':str(self.root/'data'),'LOCALSCRIBE_TOKEN':'test'}):
            spec=importlib.util.spec_from_file_location('localscribe_test_server', ROOT/'backend/server.py')
            self.service=importlib.util.module_from_spec(spec); spec.loader.exec_module(self.service)
        self.service.init(); self.folder=self.root/'watch'; self.folder.mkdir()
    def tearDown(self): self.tmp.cleanup()
    def audio(self,name):
        path=self.folder/name
        with wave.open(str(path),'wb') as w: w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000); w.writeframes(b'\0\0'*8000)
        past=time.time()-20; os.utime(path,(past,past)); return path
    def test_only_one_import_per_unchanged_file(self):
        self.audio('meeting.wav'); self.service.scan_watch_folder(str(self.folder)); self.service.scan_watch_folder(str(self.folder))
        with self.service.db() as c:
            rows=c.execute('SELECT status,path FROM recordings').fetchall(); watched=c.execute('SELECT count(*) FROM watched').fetchone()[0]
        self.assertEqual(len(rows),1); self.assertEqual(watched,1); self.assertEqual(rows[0]['status'],'queued'); self.assertTrue(Path(rows[0]['path']).exists())
        self.assertEqual(self.service.jobs.qsize(),1)
    def test_changing_source_is_neither_published_nor_marked_seen(self):
        source=self.audio('changing.wav'); original=self.service.shutil.copy2
        def changing_copy(src,dst):
            original(src,dst)
            with open(src,'ab') as f: f.write(b'changed')
        with patch.object(self.service.shutil,'copy2',side_effect=changing_copy): self.service.scan_watch_folder(str(self.folder))
        with self.service.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM recordings').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM watched').fetchone()[0],0)
        self.assertEqual(list(self.service.MEDIA.iterdir()),[])
if __name__=='__main__': unittest.main()
