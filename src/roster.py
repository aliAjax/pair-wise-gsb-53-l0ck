"""随行家属名单规则：人员校验、未成年判定与名单变动。

本模块只处理名单本身的规则（谁在名单上、是否未成年、变动流水），
不计算材料清单，也不接触数据库。
"""
from datetime import date, datetime
from typing import Any, Dict, List, Tuple

from .domain import ValidationError, choice, text

# 收案登记时支持的家属关系
RELATIONSHIPS = ("spouse", "child", "parent")
RELATIONSHIP_LABELS = {"spouse": "配偶", "child": "子女", "parent": "父母"}

ADULT_AGE = 18
ACTIVE = "active"
REMOVED = "removed"


def parse_date(value: Any, field: str = "date") -> date:
    if not isinstance(value, str):
        raise ValidationError("%s必须是YYYY-MM-DD格式日期" % field)
    try:
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValidationError("%s必须是YYYY-MM-DD格式日期" % field) from exc
    return parsed


def is_minor(birth_date: date, as_of: date) -> bool:
    """按日历年龄判断：未满18周岁为未成年（生日当天视为成年）。"""
    age = as_of.year - birth_date.year - ((as_of.month, as_of.day) < (birth_date.month, birth_date.day))
    return age < ADULT_AGE


class RosterRules:
    RELATIONSHIPS = RELATIONSHIPS
    ACTIVE = ACTIVE
    REMOVED = REMOVED

    def validate_person(self, data: Dict[str, Any]) -> Dict[str, str]:
        if not isinstance(data, dict):
            raise ValidationError("家属信息必须是对象")
        name = text(data, "name")
        relationship = choice(data, "relationship", list(RELATIONSHIPS))
        birth = parse_date(data.get("birth_date"), "birth_date")
        return {"name": name, "relationship": relationship, "birth_date": birth.isoformat()}

    def validate_persons(self, raw: Any) -> List[Dict[str, str]]:
        if raw is None:
            return []
        if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
            raise ValidationError("dependents必须是家属对象列表")
        persons = [self.validate_person(item) for item in raw]
        seen = set()
        for person in persons:
            key = (person["name"], person["birth_date"])
            if key in seen:
                raise ValidationError("名单中不能重复登记%s" % person["name"])
            seen.add(key)
        return persons

    @staticmethod
    def label(relationship: str) -> str:
        return RELATIONSHIP_LABELS.get(relationship, relationship)

    @staticmethod
    def next_person_id(roster: List[Dict[str, Any]]) -> str:
        # 退出者编号也不再复用，保证历史材料始终能挂回同一个人
        maximum = 0
        for entry in roster:
            pid = str(entry.get("person_id", ""))
            if pid.startswith("P") and pid[1:].isdigit():
                maximum = max(maximum, int(pid[1:]))
        return "P%d" % (maximum + 1)

    @staticmethod
    def _change(changes: List[Dict[str, Any]], change_type: str, entry: Dict[str, Any], as_of: date, actor_id: str, detail: str) -> Dict[str, Any]:
        event = {
            "seq": len(changes) + 1,
            "date": as_of.isoformat(),
            "type": change_type,
            "person_id": entry["person_id"],
            "name": entry["name"],
            "actor_id": actor_id,
            "detail": detail,
        }
        changes.append(event)
        return event

    def build_initial(
        self, persons: List[Dict[str, str]], as_of: date, actor_id: str = ""
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """收案登记：生成初始名单和登记流水。"""
        roster: List[Dict[str, Any]] = []
        changes: List[Dict[str, Any]] = []
        for person in persons:
            birth = parse_date(person["birth_date"], "birth_date")
            if birth > as_of:
                raise ValidationError("%s的出生日期不能晚于登记日期" % person["name"])
            entry = {
                "person_id": self.next_person_id(roster),
                "name": person["name"],
                "relationship": person["relationship"],
                "birth_date": person["birth_date"],
                "status": ACTIVE,
                "joined_date": as_of.isoformat(),
                "minor": is_minor(birth, as_of),
                "removed_date": None,
                "remove_reason": None,
            }
            roster.append(entry)
            self._change(
                changes,
                "registered",
                entry,
                as_of,
                actor_id,
                "收案登记%s%s（出生日期%s）" % (self.label(entry["relationship"]), entry["name"], entry["birth_date"]),
            )
        return roster, changes

    def add(
        self,
        roster: List[Dict[str, Any]],
        changes: List[Dict[str, Any]],
        data: Dict[str, Any],
        as_of: date,
        actor_id: str = "",
    ) -> str:
        """名单新增；同一人（姓名+出生日期）曾退出的，重新激活并保留历史。"""
        person = self.validate_person(data)
        birth = parse_date(person["birth_date"], "birth_date")
        if birth > as_of:
            raise ValidationError("%s的出生日期不能晚于当前日期" % person["name"])
        key = (person["name"], person["birth_date"])
        for entry in roster:
            if (entry["name"], entry["birth_date"]) == key and entry["status"] == ACTIVE:
                raise ValidationError("%s已在随行名单中" % person["name"])
        for entry in roster:
            if (entry["name"], entry["birth_date"]) == key and entry["status"] == REMOVED:
                entry["status"] = ACTIVE
                entry["removed_date"] = None
                entry["remove_reason"] = None
                entry["minor"] = is_minor(birth, as_of)
                self._change(
                    changes, "reactivated", entry, as_of, actor_id,
                    "%s重新加入随行名单" % entry["name"],
                )
                return changes[-1]["detail"]
        entry = {
            "person_id": self.next_person_id(roster),
            "name": person["name"],
            "relationship": person["relationship"],
            "birth_date": person["birth_date"],
            "status": ACTIVE,
            "joined_date": as_of.isoformat(),
            "minor": is_minor(birth, as_of),
            "removed_date": None,
            "remove_reason": None,
        }
        roster.append(entry)
        self._change(
            changes, "added", entry, as_of, actor_id,
            "新增%s%s（出生日期%s）" % (self.label(entry["relationship"]), entry["name"], entry["birth_date"]),
        )
        return changes[-1]["detail"]

    def remove(
        self,
        roster: List[Dict[str, Any]],
        changes: List[Dict[str, Any]],
        person_id: str,
        reason: str,
        as_of: date,
        actor_id: str = "",
    ) -> str:
        entry = self.find(roster, person_id)
        if entry is None:
            raise ValidationError("名单中没有人员%s" % person_id)
        if entry["status"] == REMOVED:
            raise ValidationError("%s已退出随行名单" % entry["name"])
        entry["status"] = REMOVED
        entry["removed_date"] = as_of.isoformat()
        entry["remove_reason"] = reason
        self._change(
            changes, "removed", entry, as_of, actor_id,
            "%s退出随行名单：%s" % (entry["name"], reason),
        )
        return changes[-1]["detail"]

    def refresh_age(
        self, roster: List[Dict[str, Any]], changes: List[Dict[str, Any]], as_of: date, actor_id: str = ""
    ) -> List[str]:
        """按当前日期重算成年状态；未成年子女成年时自动留变动记录。退出者状态冻结。"""
        adult_ids: List[str] = []
        for entry in roster:
            if entry["status"] != ACTIVE or entry["relationship"] != "child" or not entry.get("minor"):
                continue
            birth = parse_date(entry["birth_date"], "birth_date")
            if not is_minor(birth, as_of):
                entry["minor"] = False
                adult_ids.append(entry["person_id"])
                self._change(
                    changes, "turned_adult", entry, as_of, actor_id,
                    "%s已成年，监护声明不再要求" % entry["name"],
                )
        return adult_ids

    @staticmethod
    def find(roster: List[Dict[str, Any]], person_id: str):
        for entry in roster:
            if entry["person_id"] == person_id:
                return entry
        return None

    @staticmethod
    def active(roster: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [entry for entry in roster if entry["status"] == ACTIVE]
