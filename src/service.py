"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, ValidationError, text
from .repository import Repository
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )

    def change_roster(self, actor: Actor, record_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        """随行家属新增或退出；变动后按当前人员重算材料。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_roster(actor.role):
            raise PermissionDenied("角色无权调整随行名单")
        record = self.repository.get(record_id)
        self._check_version(record, expected_version)
        new_payload, detail = self.rules.change_roster(record, data or {}, actor.user_id)
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=record["state"],
            payload=new_payload,
            actor_id=actor.user_id,
            action="roster_change",
            details={"summary": detail, "input": data or {}, "state": record["state"]},
        )

    def submit_person_documents(self, actor: Actor, record_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        """材料挂在对应人员名下（主申请人或在册家属），并重算缺项。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_documents(actor.role):
            raise PermissionDenied("角色无权登记材料")
        record = self.repository.get(record_id)
        self._check_version(record, expected_version)
        new_payload, detail = self.rules.submit_person_documents(record, data or {}, actor.user_id)
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=record["state"],
            payload=new_payload,
            actor_id=actor.user_id,
            action="person_documents",
            details={"summary": detail, "input": data or {}, "state": record["state"]},
        )

    @staticmethod
    def _check_version(record: Dict[str, Any], expected_version) -> None:
        if not isinstance(expected_version, int):
            raise ValidationError("expected_version必须是整数")
        if int(record["version"]) != int(expected_version):
            raise Conflict("版本冲突，请刷新后重试")

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
