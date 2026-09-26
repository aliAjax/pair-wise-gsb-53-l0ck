import unittest
from datetime import date

from src.domain import ValidationError
from src.materials import PRIMARY_PERSON_ID, MaterialCalculator
from src.roster import ACTIVE, REMOVED, RosterRules, is_minor


class RosterRulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = RosterRules()
        self.as_of = date(2030, 1, 1)

    def test_initial_registration_records_minor_and_changes(self):
        persons = [
            {"name": "王芳", "relationship": "spouse", "birth_date": "1995-05-02"},
            {"name": "王小", "relationship": "child", "birth_date": "2015-06-01"},
        ]
        roster, changes = self.rules.build_initial(persons, self.as_of)
        self.assertEqual([e["person_id"] for e in roster], ["P1", "P2"])
        self.assertFalse(roster[0]["minor"])
        self.assertTrue(roster[1]["minor"])
        self.assertEqual([c["type"] for c in changes], ["registered", "registered"])

    def test_invalid_relationship_or_date_rejected(self):
        with self.assertRaises(ValidationError):
            self.rules.validate_person({"name": "甲", "relationship": "brother", "birth_date": "2010-01-01"})
        with self.assertRaises(ValidationError):
            self.rules.validate_person({"name": "甲", "relationship": "child", "birth_date": "2010/01/01"})

    def test_duplicate_person_rejected(self):
        with self.assertRaises(ValidationError):
            self.rules.validate_persons([
                {"name": "王小", "relationship": "child", "birth_date": "2015-01-01"},
                {"name": "王小", "relationship": "child", "birth_date": "2015-01-01"},
            ])

    def test_birth_after_registration_rejected(self):
        with self.assertRaises(ValidationError):
            self.rules.build_initial(
                [{"name": "未出生", "relationship": "child", "birth_date": "2030-06-01"}], self.as_of
            )

    def test_add_remove_keeps_history_and_ids_are_not_reused(self):
        roster, changes = self.rules.build_initial(
            [{"name": "甲", "relationship": "child", "birth_date": "2020-01-01"}], self.as_of
        )
        self.rules.add(roster, changes, {"name": "乙", "relationship": "spouse", "birth_date": "2000-01-01"}, self.as_of)
        self.assertEqual(roster[-1]["person_id"], "P2")
        self.rules.remove(roster, changes, "P1", "不随行", self.as_of)
        self.assertEqual(roster[0]["status"], REMOVED)
        # 再次新增的人员不能复用P1
        self.rules.add(roster, changes, {"name": "丙", "relationship": "parent", "birth_date": "1970-01-01"}, self.as_of)
        self.assertEqual(roster[-1]["person_id"], "P3")
        self.assertEqual([c["type"] for c in changes],
                         ["registered", "added", "removed", "added"])

    def test_reactivate_same_person_restores_entry(self):
        roster, changes = self.rules.build_initial(
            [{"name": "乙", "relationship": "spouse", "birth_date": "2000-01-01"}], self.as_of
        )
        self.rules.remove(roster, changes, "P1", "暂缓", self.as_of)
        self.rules.add(roster, changes, {"name": "乙", "relationship": "spouse", "birth_date": "2000-01-01"}, self.as_of)
        self.assertEqual(len(roster), 1)
        self.assertEqual(roster[0]["status"], ACTIVE)
        self.assertEqual(changes[-1]["type"], "reactivated")

    def test_add_active_duplicate_rejected(self):
        roster, changes = self.rules.build_initial(
            [{"name": "乙", "relationship": "spouse", "birth_date": "2000-01-01"}], self.as_of
        )
        with self.assertRaises(ValidationError):
            self.rules.add(roster, changes, {"name": "乙", "relationship": "spouse", "birth_date": "2000-01-01"}, self.as_of)

    def test_remove_unknown_rejected(self):
        roster, changes = [], []
        with self.assertRaises(ValidationError):
            self.rules.remove(roster, changes, "P9", "原因", self.as_of)

    def test_age_boundary_and_turned_adult_event(self):
        roster, changes = self.rules.build_initial(
            [{"name": "孩子", "relationship": "child", "birth_date": "2012-03-10"}], date(2030, 3, 9)
        )
        self.assertTrue(roster[0]["minor"])
        # 生日当天成年
        adults = self.rules.refresh_age(roster, changes, date(2030, 3, 10))
        self.assertEqual(adults, ["P1"])
        self.assertFalse(roster[0]["minor"])
        self.assertEqual(changes[-1]["type"], "turned_adult")
        # 再刷新不会重复记录
        self.assertEqual(self.rules.refresh_age(roster, changes, date(2030, 3, 11)), [])
        self.assertEqual(len(changes), 2)

    def test_removed_child_age_frozen(self):
        roster, changes = self.rules.build_initial(
            [{"name": "孩子", "relationship": "child", "birth_date": "2015-01-01"}], date(2030, 1, 1)
        )
        self.rules.remove(roster, changes, "P1", "退出", date(2030, 1, 2))
        self.rules.refresh_age(roster, changes, date(2035, 1, 1))
        self.assertTrue(roster[0]["minor"])  # 退出者状态冻结，不产生成年事件
        self.assertTrue(all(c["type"] != "turned_adult" for c in changes))


class IsMinorTest(unittest.TestCase):
    def test_calendar_age(self):
        birth = date(2012, 3, 10)
        self.assertTrue(is_minor(birth, date(2030, 3, 9)))
        self.assertFalse(is_minor(birth, date(2030, 3, 10)))


class MaterialCalculatorTest(unittest.TestCase):
    def setUp(self):
        self.calc = MaterialCalculator()
        self.roster_rules = RosterRules()

    def test_minor_child_requires_guardianship(self):
        roster, _ = self.roster_rules.build_initial(
            [{"name": "娃", "relationship": "child", "birth_date": "2020-01-01"},
             {"name": "偶", "relationship": "spouse", "birth_date": "1990-01-01"}],
            date(2030, 1, 1),
        )
        result = self.calc.reconcile(["passport"], roster, {})
        self.assertIn("guardianship_declaration", result["required_by_person"]["P1"])
        self.assertNotIn("guardianship_declaration", result["required_by_person"]["P2"])
        # 缺项逐人记录
        self.assertEqual(result["missing_by_person"][PRIMARY_PERSON_ID], ["passport"])
        self.assertIn("guardianship_declaration", result["missing_by_person"]["P1"])
        # 待补区是并集且去重（passport 在多人名下只出现一次）
        self.assertEqual(result["pending_documents"].count("passport"), 1)
        self.assertIn("guardianship_declaration", result["pending_documents"])

    def test_documents_attached_per_person_clear_missing(self):
        roster, _ = self.roster_rules.build_initial(
            [{"name": "娃", "relationship": "child", "birth_date": "2020-01-01"}], date(2030, 1, 1)
        )
        child_required = self.calc.required_for(roster[0])
        person_documents = {
            PRIMARY_PERSON_ID: ["passport"],
            "P1": child_required,
        }
        result = self.calc.reconcile(["passport"], roster, person_documents)
        self.assertEqual(result["missing_by_person"]["P1"], [])
        self.assertEqual(result["pending_documents"], [])
        self.assertEqual(sorted(result["submitted_documents_all"]),
                         sorted(set(["passport"] + child_required)))

    def test_removed_person_kept_out_of_pending_but_history_remains(self):
        roster, _ = self.roster_rules.build_initial(
            [{"name": "娃", "relationship": "child", "birth_date": "2020-01-01"},
             {"name": "偶", "relationship": "spouse", "birth_date": "1990-01-01"}],
            date(2030, 1, 1),
        )
        person_documents = {PRIMARY_PERSON_ID: ["passport"], "P2": ["id_copy", "marriage_certificate", "passport"]}
        self.roster_rules.remove(roster, [], "P2", "退出", date(2030, 1, 2))
        result = self.calc.reconcile(["passport"], roster, person_documents)
        # 退出者的历史材料仍在，且不再出现在缺项/待补区
        self.assertNotIn("P2", result["missing_by_person"])
        pending_p2 = [doc for doc in result["pending_documents"]]
        self.assertNotIn("marriage_certificate", pending_p2)
        self.assertIn("guardianship_declaration", result["pending_documents"])  # 仍在册的未成年子女

    def test_adult_child_guardianship_dropped_but_submitted_kept(self):
        roster, changes = self.roster_rules.build_initial(
            [{"name": "娃", "relationship": "child", "birth_date": "2012-03-10"}], date(2030, 3, 9)
        )
        person_documents = {"P1": ["id_copy", "birth_certificate", "passport", "guardianship_declaration"]}
        self.roster_rules.refresh_age(roster, changes, date(2030, 3, 10))
        result = self.calc.reconcile(["passport"], roster, person_documents)
        self.assertNotIn("guardianship_declaration", result["required_by_person"]["P1"])
        self.assertEqual(result["missing_by_person"]["P1"], [])
        # 已交的监护声明仍挂在本人名下作为历史
        self.assertIn("guardianship_declaration", person_documents["P1"])


if __name__ == "__main__":
    unittest.main()
