"""移民案件期限与材料管理领域规则与状态转换。"""
from datetime import date
from typing import Any, Dict, Tuple

from .domain import Conflict, ValidationError, boolean, choice, integer, text, text_list
from .materials import PRIMARY_PERSON_ID, MaterialCalculator, doc_label, missing_summary
from .roster import RosterRules, parse_date

INITIAL_STATE = "draft"
CREATE_ROLES = {'intake_officer'}
ACTION_ROLES = {'submit': {'legal_rep', 'case_officer'}, 'request_evidence': {'case_officer'}, 'respond': {'legal_rep'}, 'decide': {'case_officer', 'supervisor'}, 'appeal': {'legal_rep'}, 'close': {'supervisor'}, 'reopen': {'supervisor'}}
TRANSITIONS = {'submit': {'draft': 'submitted'}, 'request_evidence': {'submitted': 'evidence_requested'}, 'respond': {'evidence_requested': 'response_received'}, 'decide': {'submitted': 'decided', 'response_received': 'decided'}, 'appeal': {'decided': 'appealed'}, 'close': {'decided': 'closed', 'appealed': 'closed'}, 'reopen': {'decided': 'submitted'}}

ROSTER_ROLES = {'intake_officer', 'legal_rep', 'case_officer', 'supervisor'}
DOCUMENT_ROLES = {'legal_rep', 'case_officer', 'supervisor'}
ROSTER_STATES = {'draft', 'submitted', 'evidence_requested', 'response_received'}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def __init__(self, roster_rules: RosterRules = None, materials: MaterialCalculator = None) -> None:
        self.roster = roster_rules or RosterRules()
        self.materials = materials or MaterialCalculator()

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        all_roles.update(ROSTER_ROLES)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def role_can_roster(self, role: str) -> bool:
        return role == "admin" or role in ROSTER_ROLES

    def role_can_documents(self, role: str) -> bool:
        return role == "admin" or role in DOCUMENT_ROLES

    @staticmethod
    def as_of_date(data: Dict[str, Any], key: str = "as_of_date", default: date = None) -> date:
        if not data.get(key):
            return default or date.today()
        return parse_date(data.get(key), key)

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "applicant_id")
        choice(p, "case_type", ["asylum", "family", "work"])
        integer(p, "received_day", 0)
        integer(p, "deadline_days", 1)
        integer(p, "response_day", 0)
        boolean(p, "representation_active")
        text_list(p, "required_documents", 1)
        # 收案即登记随行家属关系和出生日期
        p["_dependents"] = self.roster.validate_persons(p.get("dependents"))
        return p

    def reconcile(self, p: Dict[str, Any], as_of: date, actor_id: str = "") -> Dict[str, Any]:
        """名单或材料变化后的统一重算入口：年龄状态 + 逐人材料 + 汇总字段。"""
        roster = p.get("roster") or []
        changes = p.get("roster_changes") or []
        self.roster.refresh_age(roster, changes, as_of, actor_id)
        result = self.materials.reconcile(p["required_documents"], roster, p.get("person_documents") or {})
        p["roster"] = roster
        p["roster_changes"] = changes
        p["required_by_person"] = result["required_by_person"]
        p["missing_by_person"] = result["missing_by_person"]
        p["pending_documents"] = result["pending_documents"]
        p["required_documents_all"] = result["required_documents_all"]
        p["submitted_documents_all"] = result["submitted_documents_all"]
        # 兼容旧字段：主申请人维度保留原始要求清单
        p["missing_documents"] = list(p["missing_by_person"].get(PRIMARY_PERSON_ID, []))
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        p["deadline_day"] = int(p["received_day"]) + int(p["deadline_days"])
        p["days_remaining"] = int(p["deadline_day"]) - int(p["response_day"])
        p["overdue"] = p["days_remaining"] < 0
        dependents = p.pop("_dependents")
        as_of = self.as_of_date(p)
        roster, roster_changes = self.roster.build_initial(dependents, as_of, actor_id="intake")
        p["roster"] = roster
        p["roster_changes"] = roster_changes
        p["person_documents"] = {}
        p["submitted_documents"] = []
        self.reconcile(p, as_of)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing) -> None:
        for item in existing:
            if item["state"] not in {"closed", "decided"} and item["payload"].get("applicant_id") == payload.get("applicant_id") and item["payload"].get("case_type") == payload.get("case_type"):
                raise Conflict("同一申请人同类型案件仍在处理中")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise ValidationError("当前状态不允许执行%s" % action)
        return allowed

    def require_roster_state(self, record: Dict[str, Any]) -> None:
        if record["state"] not in ROSTER_STATES:
            raise Conflict("案件已结束决定流程，不能再调整名单；如需调整请先重开案件")

    def change_roster(self, record: Dict[str, Any], data: Dict[str, Any], actor_id: str = "") -> Tuple[Dict[str, Any], str]:
        self.require_roster_state(record)
        data = data or {}
        p = dict(record["payload"])
        p.setdefault("roster", [])
        p.setdefault("roster_changes", [])
        p.setdefault("person_documents", {})
        as_of = self.as_of_date(data)
        op = choice(data, "op", ["add", "remove", "refresh"])
        if op == "add":
            detail = self.roster.add(p["roster"], p["roster_changes"], data.get("person", {}), as_of, actor_id)
        elif op == "remove":
            person_id = text(data, "person_id")
            reason = text(data, "reason")
            detail = self.roster.remove(p["roster"], p["roster_changes"], person_id, reason, as_of, actor_id)
        else:
            adults = self.roster.refresh_age(p["roster"], p["roster_changes"], as_of, actor_id)
            detail = "名单已按%s核对，%s" % (as_of.isoformat(),
                                           "成年：" + "、".join(adults) if adults else "无新增成年家属")
        # 名单变动后按当前人员重算材料，缺项留在待补区
        self.reconcile(p, as_of, actor_id)
        return p, detail

    def submit_person_documents(self, record: Dict[str, Any], data: Dict[str, Any], actor_id: str = "") -> Tuple[Dict[str, Any], str]:
        self.require_roster_state(record)
        data = data or {}
        p = dict(record["payload"])
        roster = p.get("roster") or []
        person_documents = dict(p.get("person_documents") or {})
        docs = text_list(data, "documents", 1)
        person_id = data.get("person_id", PRIMARY_PERSON_ID)
        if not isinstance(person_id, str) or not person_id.strip():
            raise ValidationError("person_id必须是文本")
        person_id = person_id.strip()
        if person_id != PRIMARY_PERSON_ID:
            entry = self.roster.find(roster, person_id)
            if entry is None:
                raise ValidationError("名单中没有人员%s" % person_id)
            if entry["status"] != "active":
                raise ValidationError("%s已退出名单，不能再补交材料" % entry["name"])
            name = entry["name"]
        else:
            name = "主申请人"
        merged = list(person_documents.get(person_id, []))
        for doc in docs:
            if doc not in merged:
                merged.append(doc)
        person_documents[person_id] = merged
        p["person_documents"] = person_documents
        if person_id == PRIMARY_PERSON_ID:
            p["submitted_documents"] = list(merged)
        self.reconcile(p, self.as_of_date(data), actor_id)
        return p, "%s材料已登记：%s" % (name, ", ".join(doc_label(doc) for doc in docs))

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = data or {}
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "submit":
            # 材料可在收案后逐人登记，submit时documents可省略；如携带则并入主申请人名下
            docs = text_list(data, "documents", 0)
            as_of = self.as_of_date(data)
            person_documents = dict(p.get("person_documents") or {})
            person_documents[PRIMARY_PERSON_ID] = list(dict.fromkeys(list(person_documents.get(PRIMARY_PERSON_ID, [])) + docs))
            p["person_documents"] = person_documents
            self.reconcile(p, as_of)
            missing = list(p["pending_documents"])
            waiver = boolean(data, "supervisor_waiver")
            if missing and not waiver:
                # 决定前提交流程逐人核对，直接报出每个人名下的缺项
                raise ValidationError("缺少材料：" + "；".join(missing_summary(p["missing_by_person"], p.get("roster") or [])))
            if p["overdue"] and not waiver:
                raise ValidationError("案件已超过提交期限")
            changes["submitted_documents"] = docs
            changes["missing_documents"] = list(p["missing_by_person"].get(PRIMARY_PERSON_ID, []))
            changes["waiver_used"] = waiver
            summary = "申请材料已提交"
        elif action == "request_evidence":
            request_day = integer(data, "evidence_request_day", p["response_day"])
            allowed_days = integer(data, "allowed_days", 1)
            changes["evidence_request_day"] = request_day
            changes["evidence_due_day"] = request_day + allowed_days
            changes["evidence_request"] = text(data, "evidence_request")
            summary = "补件要求已发出"
        elif action == "respond":
            docs = text_list(data, "documents", 1)
            if int(data.get("response_day", p["response_day"])) > int(p["evidence_due_day"]):
                raise ValidationError("补件回应超过期限")
            changes["response_day"] = int(data["response_day"])
            changes["evidence_documents"] = docs
            summary = "补件已回应"
        elif action == "decide":
            as_of = self.as_of_date(data)
            self.reconcile(p, as_of)
            # 决定前逐人核完：当前在案的每个人都不能有缺项
            pending = list(p["pending_documents"])
            if pending and not boolean(data, "supervisor_waiver"):
                raise ValidationError("决定前材料未齐：" + "；".join(missing_summary(p["missing_by_person"], p.get("roster") or [])))
            changes["decision"] = choice(data, "decision", ["granted", "denied", "withdrawn"])
            changes["decision_reason"] = text(data, "decision_reason")
            summary = "案件已作出决定"
        elif action == "appeal":
            appeal_day = integer(data, "appeal_day", 0)
            if appeal_day > int(p["deadline_day"]) + 30:
                raise ValidationError("上诉窗口已关闭")
            changes["appeal_day"] = appeal_day
            changes["appeal_reason"] = text(data, "appeal_reason")
            summary = "上诉已登记"
        elif action == "close":
            changes["closure_note"] = text(data, "closure_note")
            summary = "案件归档"
        elif action == "reopen":
            changes["reopen_reason"] = text(data, "reopen_reason")
            summary = "案件已重开，可重新核对名单与材料"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
