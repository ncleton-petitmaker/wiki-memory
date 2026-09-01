from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .config import load_registry, load_vault


def _tokens(value: Any) -> set[str]:
    if isinstance(value, list):
        value = " ".join(str(item) for item in value)
    return set(re.findall(r"[\w-]+", str(value).lower(), flags=re.UNICODE))


def recommend_vault(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    request_tokens = _tokens(
        [
            request.get("title", ""),
            request.get("purpose", ""),
            request.get("audience", []),
            request.get("keywords", []),
        ]
    )
    requested_confidentiality = request.get("confidentiality")
    requested_lifecycle = request.get("lifecycle")
    ranked = []
    for entry in load_registry(root).get("vaults", []):
        _, vault = load_vault(root, entry["slug"])
        include = _tokens(vault.get("routing", {}).get("include", []))
        exclude = _tokens(vault.get("routing", {}).get("exclude", []))
        keywords = _tokens(vault.get("routing", {}).get("keywords", []))
        purpose = _tokens(vault.get("purpose", ""))
        audience = _tokens(vault.get("audience", []))
        blocked = sorted(request_tokens & exclude)
        score = 0
        reasons = []
        if blocked:
            score -= 100
            reasons.append("excluded:" + ",".join(blocked))
        overlap = request_tokens & (include | keywords | purpose)
        score += len(overlap) * 3
        if overlap:
            reasons.append("topic:" + ",".join(sorted(overlap)))
        audience_overlap = _tokens(request.get("audience", [])) & audience
        score += len(audience_overlap) * 2
        if audience_overlap:
            reasons.append("audience:" + ",".join(sorted(audience_overlap)))
        if requested_confidentiality:
            if requested_confidentiality == vault.get("confidentiality"):
                score += 3
                reasons.append("confidentiality-match")
            else:
                score -= 5
                reasons.append("confidentiality-boundary")
        if requested_lifecycle:
            if requested_lifecycle == vault.get("lifecycle"):
                score += 2
            else:
                score -= 2
                reasons.append("lifecycle-boundary")
        ranked.append({"slug": entry["slug"], "score": score, "reasons": reasons})
    ranked.sort(key=lambda item: (-item["score"], item["slug"]))
    positive = [item for item in ranked if item["score"] > 0]
    if not positive:
        decision = "new_vault"
    elif len(positive) > 1 and positive[0]["score"] - positive[1]["score"] <= 2:
        decision = "ask"
    else:
        decision = "existing_vault"
    return {"decision": decision, "ranked": ranked}
