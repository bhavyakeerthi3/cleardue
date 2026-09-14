"""Exhaustive bounded comparison of supported candidate operations, not a global optimum claim."""
from itertools import combinations


def select_minimum(actions, proposal):
    if len(actions) > 8:
        raise ValueError("too many candidate actions for a bounded plan")
    required = set()
    for condition in proposal.condition_evaluations:
        if condition.status not in {"UNMET", "UNKNOWN"}:
            continue
        quotes = " ".join(r.quote for r in condition.support_refs + condition.conflict_refs).lower()
        if condition.kind == "ACCEPTANCE" and "failed" in quotes:
            required.add((condition.condition_id, "correct_failure"))
        if condition.kind in {"ACCEPTANCE", "CHANGE_ORDER", "BILLING_AUTHORITY", "DOCUMENTATION"}:
            required.add((condition.condition_id, "request_evidence"))
    def coverage(action):
        task = {"jira": "correct_failure", "gmail": "request_evidence"}.get(action.app)
        return {(cid, task) for cid in action.condition_ids} & required
    choices = []
    for count in range(len(actions)+1):
        for subset in combinations(range(len(actions)), count):
            covered = set().union(*(coverage(actions[i]) for i in subset))
            choices.append({"indices": list(subset), "writes": count, "covers_required_work": required <= covered})
    valid = [choice for choice in choices if choice["covers_required_work"]]
    if not valid:
        return [], [{"selected": False, "reason": "No candidate plan covers all required work; human review required", "writes": 0}]
    best = min(valid, key=lambda c: (c["writes"], c["indices"]))
    # Keep only the useful comparison: no action, selected, and full proposed plan.
    shown = [c for c in choices if not c["indices"] or c is best or len(c["indices"]) == len(actions)]
    alternatives = [{"actions": [actions[i].action_type.value for i in c["indices"]], "writes": c["writes"], "selected": c is best,
        "reason": "Fewest writes among supplied candidates covering required work" if c is best else "Missing required work" if not c["covers_required_work"] else "Additional write is unnecessary to address the blocker"} for c in shown]
    alternatives += [{"actions": ["Change invoice amount", "Force payable"], "writes": None, "selected": False, "reason": "Unavailable: outside the action allowlist and cannot establish acceptance"}]
    return [actions[i] for i in best["indices"]], alternatives
