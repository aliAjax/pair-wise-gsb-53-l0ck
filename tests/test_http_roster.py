import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path

from app import build_service
from src.http_api import create_server


BASE = Path(__file__).resolve().parent.parent


class HttpRosterSmokeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service = build_service(str(Path(self.temp.name) / "http.db"))
        self.server = create_server("127.0.0.1", 0, service, BASE / "static")
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def request(self, method, path, body=None, role="admin", user="smoke"):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method)
        req.add_header("X-User-Id", user)
        req.add_header("X-Role", role)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_roster_change_and_person_documents_flow(self):
        create_body = {"reference": "IMM-HTTP-1", "data": {
            "applicant_id": "H-1", "case_type": "family", "received_day": 0, "deadline_days": 30,
            "response_day": 0, "representation_active": True,
            "required_documents": ["passport"],
            "dependents": [{"name": "娃娃", "relationship": "child", "birth_date": "2019-01-01"}],
            "as_of_date": "2030-01-01"}}
        status, record = self.request("POST", "/api/records", create_body, role="intake_officer")
        self.assertEqual(status, 201)
        self.assertIn("guardianship_declaration", record["payload"]["pending_documents"])
        rid, version = record["id"], record["version"]

        # 缺角色头应403
        req = urllib.request.Request(
            "http://127.0.0.1:%d/api/records/%d/documents" % (self.port, rid),
            data=json.dumps({"expected_version": version, "data": {}}).encode("utf-8"),
            method="POST")
        req.add_header("Content-Type", "application/json")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 403)

        # 给主申请人交材料，再给孩子补齐材料
        status, record = self.request("POST", "/api/records/%d/documents" % rid, {
            "expected_version": version,
            "data": {"person_id": "primary", "documents": ["passport"]}},
            role="legal_rep")
        self.assertEqual(status, 200)
        status, record = self.request("POST", "/api/records/%d/documents" % rid, {
            "expected_version": record["version"],
            "data": {"person_id": "P1", "documents": ["id_copy", "birth_certificate", "passport", "guardianship_declaration"]}},
            role="legal_rep")
        self.assertEqual(status, 200)
        self.assertEqual(record["payload"]["pending_documents"], [])

        # 决定前再无缺项，直接提交并决定
        status, record = self.request("POST", "/api/records/%d/actions/submit" % rid,
                                      {"expected_version": record["version"], "data": {"documents": ["passport"]}},
                                      role="legal_rep")
        self.assertEqual(status, 200)
        status, record = self.request("POST", "/api/records/%d/actions/decide" % rid,
                                      {"expected_version": record["version"],
                                       "data": {"decision": "granted", "decision_reason": "ok"}},
                                      role="case_officer")
        self.assertEqual(status, 200)

        # 审计时间线包含家属材料登记
        status, timeline = self.request("GET", "/api/records/%d/audit" % rid)
        actions = [event["action"] for event in timeline["items"]]
        self.assertIn("person_documents", actions)

    def test_roster_endpoint_validation_error(self):
        status, record = self.request("POST", "/api/records", {"reference": "IMM-HTTP-2", "data": {
            "applicant_id": "H-2", "case_type": "family", "received_day": 0, "deadline_days": 30,
            "response_day": 0, "representation_active": True, "required_documents": ["passport"]}},
            role="intake_officer")
        self.assertEqual(status, 201)
        # op非法
        status, error = self.request("POST", "/api/records/%d/roster" % record["id"], {
            "expected_version": record["version"], "data": {"op": "unknown"}}, role="case_officer")
        self.assertEqual(status, 422)
        self.assertEqual(error["error"], "validation_error")


if __name__ == "__main__":
    unittest.main()
