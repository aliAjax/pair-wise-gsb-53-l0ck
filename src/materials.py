"""材料清单计算：按当前名单逐人生成要求材料并核对缺项。

纯函数式规则，不读写数据库；输入是名单和已交材料，输出是逐人材料状态。
"""
from typing import Any, Dict, List

# 每名随行家属按关系要求的基础材料；未成年子女额外要求监护声明
PERSON_RELATION_DOCS = {
    "spouse": ["id_copy", "marriage_certificate", "passport"],
    "child": ["id_copy", "birth_certificate", "passport"],
    "parent": ["id_copy", "kinship_certificate", "passport"],
}
GUARDIANSHIP_DOC = "guardianship_declaration"
PRIMARY_PERSON_ID = "primary"

DOC_LABELS = {
    "passport": "护照",
    "id_copy": "身份证明",
    "marriage_certificate": "结婚证明",
    "birth_certificate": "出生证明",
    "kinship_certificate": "亲属关系证明",
    "guardianship_declaration": "监护声明",
}


def doc_label(code: str) -> str:
    return DOC_LABELS.get(code, code)


class MaterialCalculator:
    """根据当前人员名单计算材料，缺项留在待补区，退出者保留历史。"""

    def required_for(self, entry: Dict[str, Any]) -> List[str]:
        docs = list(PERSON_RELATION_DOCS.get(entry["relationship"], ["id_copy", "passport"]))
        # 只有未成年子女附监护声明；成年后自动移出要求，但已交材料仍挂在其名下
        if entry["relationship"] == "child" and entry.get("minor"):
            docs.append(GUARDIANSHIP_DOC)
        return docs

    @staticmethod
    def person_documents(person_documents: Dict[str, Any], person_id: str) -> List[str]:
        docs = person_documents.get(person_id, []) if person_documents else []
        return list(docs) if isinstance(docs, list) else []

    def reconcile(
        self,
        primary_required: List[str],
        roster: List[Dict[str, Any]],
        person_documents: Dict[str, Any],
    ) -> Dict[str, Any]:
        """名单变动/材料更新后，按当前人员重算每个人的要求、已交和缺项。

        退出者不再参与缺项计算，其要求清单和已交材料原样保留为历史。
        """
        required_by_person: Dict[str, List[str]] = {PRIMARY_PERSON_ID: list(primary_required)}
        missing_by_person: Dict[str, List[str]] = {}
        for entry in roster:
            # 退出者的关系与未成年标记在退出时已冻结，按冻结属性重算即等于历史清单，
            # 已交材料也仍挂在本人名下；下面缺项计算跳过退出者，即“保留历史”。
            required_by_person[entry["person_id"]] = self.required_for(entry)

        primary_docs = self.person_documents(person_documents, PRIMARY_PERSON_ID)
        missing_by_person[PRIMARY_PERSON_ID] = [doc for doc in required_by_person[PRIMARY_PERSON_ID] if doc not in primary_docs]
        for entry in roster:
            pid = entry["person_id"]
            if entry["status"] != "active":
                continue
            submitted = self.person_documents(person_documents, pid)
            missing_by_person[pid] = [doc for doc in required_by_person[pid] if doc not in submitted]

        # 待补区：当前在案人员（主申请人+在册家属）缺项的并集，去重保序
        pending: List[str] = []
        for pid, docs in missing_by_person.items():
            for doc in docs:
                if doc not in pending:
                    pending.append(doc)

        required_union: List[str] = []
        for entry in roster:
            if entry["status"] != "active":
                continue
            for doc in required_by_person[entry["person_id"]]:
                if doc not in required_union:
                    required_union.append(doc)
        required_union = list(primary_required) + [doc for doc in required_union if doc not in primary_required]

        submitted_union = list(primary_docs)
        for entry in roster:
            if entry["status"] != "active":
                continue
            for doc in self.person_documents(person_documents, entry["person_id"]):
                if doc not in submitted_union:
                    submitted_union.append(doc)

        return {
            "required_by_person": required_by_person,
            "missing_by_person": missing_by_person,
            "pending_documents": pending,
            "required_documents_all": required_union,
            "submitted_documents_all": submitted_union,
        }


def missing_summary(missing_by_person: Dict[str, List[str]], roster: List[Dict[str, Any]]) -> List[str]:
    """决定前逐人核对：生成“姓名：缺项”的可读列表。"""
    names = {entry["person_id"]: entry["name"] for entry in roster}
    lines: List[str] = []
    primary_docs = missing_by_person.get(PRIMARY_PERSON_ID, [])
    if primary_docs:
        lines.append("主申请人缺少：" + ", ".join(doc_label(doc) for doc in primary_docs))
    for pid, docs in missing_by_person.items():
        if pid == PRIMARY_PERSON_ID or not docs:
            continue
        lines.append("%s缺少：%s" % (names.get(pid, pid), ", ".join(doc_label(doc) for doc in docs)))
    return lines
