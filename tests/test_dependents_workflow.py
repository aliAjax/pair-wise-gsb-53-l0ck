import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


PRIMARY_REQUIRED = ["passport", "sponsor_letter"]
CHILD_DOCS = ["id_copy", "birth_certificate", "passport", "guardianship_declaration"]
SPOUSE_DOCS = ["id_copy", "marriage_certificate", "passport"]


def create_data(**overrides):
    data = {"applicant_id": "A-100", "case_type": "family", "received_day": 0,
            "deadline_days": 30, "response_day": 0, "representation_active": True,
            "required_documents": list(PRIMARY_REQUIRED)}
    data.update(overrides)
    return data


class DependentWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.intake = Actor("u1", "intake_officer")
        self.legal = Actor("u2", "legal_rep")
        self.officer = Actor("u3", "case_officer")
        self.supervisor = Actor("u4", "supervisor")

    def tearDown(self):
        self.temp.cleanup()

    def _create(self, reference, dependents=None, as_of=None):
        data = create_data(dependents=dependents) if dependents is not None else create_data()
        if as_of:
            data["as_of_date"] = as_of
        return self.service.create(self.intake, reference, data)

    def test_intake_with_minor_child_blocks_submit_until_guardianship(self):
        record = self._create("IMM-30001", [{"name": "钱小宝", "relationship": "child", "birth_date": "2018-05-01"}], "2030-01-01")
        payload = record["payload"]
        self.assertEqual(len(payload["roster"]), 1)
        self.assertTrue(payload["roster"][0]["minor"])
        # 待补区含监护声明
        self.assertIn("guardianship_declaration", payload["pending_documents"])
        # 只交主申请人材料不能提交，错误里逐人点名
        record = self.service.submit_person_documents(
            self.legal, record["id"], record["version"],
            {"person_id": "primary", "documents": PRIMARY_REQUIRED})
        with self.assertRaises(ValidationError) as ctx:
            self.service.act(self.legal, record["id"], record["version"], "submit", {"documents": []})
        self.assertIn("钱小宝", str(ctx.exception))
        self.assertIn("监护声明", str(ctx.exception))
        # 补齐子女全部材料后可提交
        record = self.service.submit_person_documents(
            self.legal, record["id"], record["version"],
            {"person_id": "P1", "documents": CHILD_DOCS})
        self.assertEqual(record["payload"]["pending_documents"], [])
        record = self.service.act(self.legal, record["id"], record["version"], "submit", {"documents": []})
        self.assertEqual(record["state"], "submitted")

    def test_roster_added_after_intake_recomputes_missing(self):
        record = self._create("IMM-30002")
        self.assertEqual(record["payload"]["roster"], [])
        # 主申请人材料先齐
        record = self.service.submit_person_documents(
            self.legal, record["id"], record["version"],
            {"person_id": "primary", "documents": PRIMARY_REQUIRED})
        self.assertEqual(record["payload"]["pending_documents"], [])
        # 之后新增配偶，缺项重新出现并留在待补区
        record = self.service.change_roster(self.intake, record["id"], record["version"], {
            "op": "add",
            "person": {"name": "周敏", "relationship": "spouse", "birth_date": "1992-03-03"},
            "as_of_date": "2030-01-05",
        })
        missing = record["payload"]["missing_by_person"]["P1"]
        self.assertEqual(sorted(missing), sorted(SPOUSE_DOCS))
        self.assertEqual(record["payload"]["roster_changes"][-1]["type"], "added")
        # 决定前核不过
        record = self.service.act(self.legal, record["id"], record["version"], "submit", {"documents": [], "supervisor_waiver": True})
        with self.assertRaises(ValidationError):
            self.service.act(self.officer, record["id"], record["version"], "decide",
                             {"decision": "granted", "decision_reason": "通过"})

    def test_remove_person_drops_missing_keeps_history(self):
        record = self._create("IMM-30003", [{"name": "孙二", "relationship": "spouse", "birth_date": "1990-01-01"}], "2030-01-01")
        record = self.service.submit_person_documents(
            self.legal, record["id"], record["version"],
            {"person_id": "primary", "documents": PRIMARY_REQUIRED})
        record = self.service.submit_person_documents(
            self.legal, record["id"], record["version"],
            {"person_id": "P1", "documents": ["id_copy"]})  # 配偶只交了部分
        record = self.service.change_roster(self.intake, record["id"], record["version"], {
            "op": "remove", "person_id": "P1", "reason": "不随迁", "as_of_date": "2030-01-08"})
        payload = record["payload"]
        self.assertEqual(payload["roster"][0]["status"], "removed")
        self.assertEqual(payload["roster"][0]["remove_reason"], "不随迁")
        self.assertNotIn("P1", payload["missing_by_person"])
        self.assertEqual(payload["pending_documents"], [])
        # 历史材料仍挂在退出者名下
        self.assertEqual(payload["person_documents"]["P1"], ["id_copy"])
        self.assertEqual(payload["roster_changes"][-1]["type"], "removed")
        # 退出者不能再补交
        with self.assertRaises(ValidationError):
            self.service.submit_person_documents(
                self.legal, record["id"], record["version"],
                {"person_id": "P1", "documents": SPOUSE_DOCS})
        # 材料齐全可提交并决定
        record = self.service.act(self.legal, record["id"], record["version"], "submit", {"documents": []})
        record = self.service.act(self.officer, record["id"], record["version"], "decide",
                                  {"decision": "granted", "decision_reason": "合格"})
        self.assertEqual(record["state"], "decided")

    def test_child_becomes_adult_loses_guardianship_requirement(self):
        record = self._create("IMM-30004", [{"name": "李成年", "relationship": "child", "birth_date": "2012-03-10"}], "2030-03-09")
        self.assertIn("guardianship_declaration", record["payload"]["required_by_person"]["P1"])
        # 成年前缺监护声明不能决定
        record = self.service.submit_person_documents(
            self.legal, record["id"], record["version"],
            {"person_id": "primary", "documents": PRIMARY_REQUIRED, "as_of_date": "2030-03-09"})
        record = self.service.submit_person_documents(
            self.legal, record["id"], record["version"],
            {"person_id": "P1", "documents": ["id_copy", "birth_certificate", "passport"], "as_of_date": "2030-03-09"})
        record = self.service.act(self.legal, record["id"], record["version"], "submit",
                                  {"documents": [], "supervisor_waiver": True, "as_of_date": "2030-03-09"})
        with self.assertRaises(ValidationError):
            self.service.act(self.officer, record["id"], record["version"], "decide",
                             {"decision": "granted", "decision_reason": "通过", "as_of_date": "2030-03-09"})
        # 到成年日重新核对名单，监护声明不再要求
        record = self.service.change_roster(self.officer, record["id"], record["version"], {
            "op": "refresh", "as_of_date": "2030-03-10"})
        self.assertNotIn("guardianship_declaration", record["payload"]["required_by_person"]["P1"])
        self.assertEqual(record["payload"]["roster_changes"][-1]["type"], "turned_adult")
        self.assertEqual(record["payload"]["pending_documents"], [])

    def test_reopen_decided_case_to_recheck_roster(self):
        record = self._create("IMM-30005")
        record = self.service.submit_person_documents(
            self.legal, record["id"], record["version"],
            {"person_id": "primary", "documents": PRIMARY_REQUIRED})
        record = self.service.act(self.legal, record["id"], record["version"], "submit", {"documents": []})
        record = self.service.act(self.officer, record["id"], record["version"], "decide",
                                  {"decision": "granted", "decision_reason": "合格"})
        # 决定后名单冻结
        with self.assertRaises(Conflict):
            self.service.change_roster(self.intake, record["id"], record["version"], {
                "op": "add", "person": {"name": "新生", "relationship": "child", "birth_date": "2025-01-01"},
                "as_of_date": "2030-02-01"})
        # 非主管不能重开
        with self.assertRaises(PermissionDenied):
            self.service.act(self.legal, record["id"], record["version"], "reopen", {"reopen_reason": "补核家属"})
        record = self.service.act(self.supervisor, record["id"], record["version"], "reopen", {"reopen_reason": "补核家属"})
        self.assertEqual(record["state"], "submitted")
        self.assertEqual(record["payload"]["reopen_reason"], "补核家属")
        # 重开后可调整名单并重算缺项
        record = self.service.change_roster(self.intake, record["id"], record["version"], {
            "op": "add", "person": {"name": "新生", "relationship": "child", "birth_date": "2025-01-01"},
            "as_of_date": "2030-02-01"})
        self.assertIn("guardianship_declaration", record["payload"]["pending_documents"])

    def test_roster_and_document_permissions(self):
        record = self._create("IMM-30006")
        # 无角色身份
        with self.assertRaises(PermissionDenied):
            self.service.change_roster(Actor("x", "outsider"), record["id"], record["version"], {
                "op": "add", "person": {"name": "甲", "relationship": "spouse", "birth_date": "1990-01-01"}})
        # 收案员可改名单但不能登记材料
        with self.assertRaises(PermissionDenied):
            self.service.submit_person_documents(
                self.intake, record["id"], record["version"],
                {"person_id": "primary", "documents": PRIMARY_REQUIRED})
        # 版本冲突保护名单操作
        record2 = self.service.change_roster(self.intake, record["id"], record["version"], {
            "op": "add", "person": {"name": "甲", "relationship": "spouse", "birth_date": "1990-01-01"},
            "as_of_date": "2030-01-01"})
        with self.assertRaises(Conflict):
            self.service.change_roster(self.intake, record["id"], record["version"], {
                "op": "add", "person": {"name": "乙", "relationship": "parent", "birth_date": "1970-01-01"},
                "as_of_date": "2030-01-01"})
        self.assertTrue(record2["version"] >= 2)


if __name__ == "__main__":
    unittest.main()
