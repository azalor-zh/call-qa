"""Проверки collector без АТС и без боевого Call QA: поддельный адаптер + локальный HTTP-сервер вместо API."""
import json
import re
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import adapters  # noqa: E402
import collector  # noqa: E402
from adapters import AudioNotReady, Call  # noqa: E402
from adapters.freepbx import FreePBX, direction_of, pick_extension  # noqa: E402

RE = re.compile(r"^(?:PJSIP|SIP|IAX2)/(\d{2,6})-")


class FakeQA(BaseHTTPRequestHandler):
    uploads, mode = [], "ok"

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(json.dumps([{"name": "Иванов", "job_title": "", "phone": "+7 (701) 555-00-01"}]).encode())

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if FakeQA.mode == "429":
            self.send_response(429); self.end_headers(); return
        FakeQA.uploads.append({"auth": self.headers["Authorization"], "body": body})
        self.send_response(200); self.end_headers()
        self.wfile.write(json.dumps({"ids": [f"job{len(FakeQA.uploads)}"], "skipped": [], "rejected_by_limit": []}).encode())


class FakeAdapter(adapters.Adapter):
    calls, not_ready = [], set()

    def list_calls(self, days_back):
        return FakeAdapter.calls

    def fetch_audio(self, call, dst):
        if call.call_id in FakeAdapter.not_ready:
            raise AudioNotReady("дописывается")
        dst.write_bytes(b"RIFF0000data")


def call(cid, ext="152", d="out", billsec=30, answered=True, started="2026-11-05 13:40:18"):
    return Call(call_id=cid, started_at=started, direction=d, extension=ext, client_number="987779991206", duration_s=billsec,
                audio_ref=cid, answered=answered, source_call_id="1793868018.7742")


class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), FakeQA)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.tmp = tempfile.TemporaryDirectory()
        key = Path(cls.tmp.name) / "k"; key.write_text("secret-key\n")
        cls.qa = {"url": f"http://127.0.0.1:{cls.srv.server_port}", "api_key_file": str(key)}
        adapters.make_adapter = lambda kind, cfg: FakeAdapter(cfg)
        collector.make_adapter = adapters.make_adapter

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.tmp.cleanup()

    def src(self, **kw):
        s = {"name": "t", "adapter": "fake", "rubric": "sales", "state_db": str(Path(self.tmp.name) / f"{self._testMethodName}.db"),
             "filter": {"extensions": ["152"], "directions": ["in", "out"], "min_billsec": 10}, "employees": {"152": "Иванов Иван"}}
        s.update(kw); return s

    def setUp(self):
        FakeQA.uploads.clear(); FakeQA.mode = "ok"; FakeAdapter.not_ready = set()
        FakeAdapter.calls = [call("out-1.wav")]

    def test_filters(self):
        f = {"extensions": ["152"], "directions": ["out"], "min_billsec": 20, "not_before": "2026-11-05 00:00:00"}
        self.assertEqual(collector.accept(call("a"), f), "")
        self.assertTrue(collector.accept(call("a", ext="100"), f))
        self.assertTrue(collector.accept(call("a", d="in"), f))
        self.assertTrue(collector.accept(call("a", billsec=5), f))
        self.assertTrue(collector.accept(call("a", answered=False), f))
        self.assertTrue(collector.accept(call("a", started="2026-11-04 10:00:00"), f))
        self.assertTrue(collector.accept(call("a", ext=""), {}))

    def test_upload_and_dedup(self):
        s = self.src()
        r1 = collector.run_source(s, self.qa, False)
        r2 = collector.run_source(s, self.qa, False)
        self.assertEqual((r1["sent"], r2["sent"]), (1, 0))
        self.assertEqual(len(FakeQA.uploads), 1)
        up = FakeQA.uploads[0]
        self.assertEqual(up["auth"], "Bearer secret-key")
        self.assertIn("Иванов Иван".encode(), up["body"])
        self.assertIn(b"20261105134018_152_out_987779991206_1793868018.7742.wav", up["body"])

    def test_not_ready_is_retried(self):
        s = self.src(); FakeAdapter.not_ready = {"out-1.wav"}
        self.assertEqual(collector.run_source(s, self.qa, False)["wait"], 1)
        FakeAdapter.not_ready = set()
        self.assertEqual(collector.run_source(s, self.qa, False)["sent"], 1)

    def test_limit_429_retries_next_cycle(self):
        s = self.src(); FakeQA.mode = "429"
        self.assertEqual(collector.run_source(s, self.qa, False)["sent"], 0)
        FakeQA.mode = "ok"
        self.assertEqual(collector.run_source(s, self.qa, False)["sent"], 1)

    def test_employee_by_phone_and_max_per_cycle(self):
        FakeAdapter.calls = [call(f"out-{i}.wav", ext="701", started=f"2026-11-05 13:4{i}:00") for i in range(3)]
        s = self.src(max_per_cycle=2, employees={}); s["filter"]["extensions"] = ["701"]
        self.assertEqual(collector.run_source(s, self.qa, False)["sent"], 2)
        self.assertEqual(collector.run_source(s, self.qa, False)["sent"], 1)

    def test_dry_run_sends_nothing(self):
        self.assertEqual(collector.run_source(self.src(), self.qa, True)["sent"], 0)
        self.assertEqual(FakeQA.uploads, [])


class FreePBXParsing(unittest.TestCase):
    def test_direction_and_extension(self):
        self.assertEqual(direction_of("out-987-152-20261105-134018-1.7742.wav", "PJSIP/152-0001", "PJSIP/KKRes_2570-0002", RE), "out")
        self.assertEqual(direction_of("internal-152-118-2026.wav", "", "", RE), "internal")
        self.assertEqual(direction_of("x.wav", "PJSIP/Trunk-1", "PJSIP/152-2", RE), "in")
        self.assertEqual(pick_extension("PJSIP/152-0001", "PJSIP/KKRes_2570-0002", RE, {"152"}), "152")
        self.assertEqual(pick_extension("PJSIP/KK-1", "PJSIP/152-2", RE, {"152"}), "152")
        self.assertEqual(pick_extension("PJSIP/100-1", "PJSIP/118-2", RE, {"152"}), "")
        self.assertEqual(pick_extension("PJSIP/100-1", "PJSIP/118-2", RE, set()), "100")

    def test_remote_path_safe(self):
        with tempfile.TemporaryDirectory() as t:
            cred = Path(t) / "c"; cred.write_text("client_id=a\nclient_secret=b\n")
            fp = FreePBX({"api_base": "http://x", "api_credentials_file": str(cred)})
            c = call("out-987-152-20261105-134018-1.7742.wav")
            self.assertEqual(fp.remote_path(c), "/var/spool/asterisk/monitor/2026/11/05/out-987-152-20261105-134018-1.7742.wav")
            for bad in ("a b.wav", "x;rm -rf /.wav", "../../etc/passwd", "a'b.wav"):
                with self.assertRaises(ValueError):
                    fp.remote_path(call(bad))


if __name__ == "__main__":
    unittest.main(verbosity=2)
