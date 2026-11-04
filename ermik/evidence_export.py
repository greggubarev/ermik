"""Read-only export and live verification of one exact-HEAD PR record."""

from __future__ import annotations

import hashlib
import json
import re

from . import state
from .config import Config
from .evidence import make_manifest, validate_manifest
from .evidence_store import read_evidence
from .github import GitHub, GitHubError
from .policy import parse_issue, validate_changed_paths
from .review import _checks_sha256
from .runtime import (_approved_on_github, _resolve_image_id, main_sha,
                      published_image_id, WorkflowError)


_BRANCH = re.compile(r"work/issue-(\d+)-[a-z0-9-]+\Z")
_LINK = re.compile(r"(?im)\bCloses\s+#(\d+)\b")


def _scope_sha256(paths: tuple[str, ...]) -> str:
    encoded = json.dumps(paths, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _github(cfg: Config) -> GitHub:
    return (GitHub(cfg.repo, cfg.publish_repo, cfg.publish_token_env)
            if cfg.publish_repo else GitHub(cfg.repo))


def _live(cfg: Config, gh: GitHub, pr_number: int) -> tuple[dict, int, object, list[str]]:
    """Check remote identity and admission without reading controller state."""
    if type(pr_number) is not int or pr_number <= 0:
        raise WorkflowError("invalid PR number")
    pr = gh.pr(pr_number)
    branch = pr.get("headRefName")
    match = _BRANCH.fullmatch(branch) if isinstance(branch, str) else None
    if not match or _LINK.findall(pr.get("body") or "") != [match.group(1)]:
        raise WorkflowError("PR issue link or task branch changed")
    issue_number = int(match.group(1))
    try:
        gh.validate_pr_identity(pr, branch=branch, base_branch=cfg.base_branch,
                                number=pr_number, state="OPEN")
    except GitHubError as exc:
        raise WorkflowError("PR identity changed") from exc
    if pr.get("baseRefOid") != main_sha(gh, cfg):
        raise WorkflowError("PR base is stale")
    files = pr.get("files")
    if (not isinstance(files, list) or not files or len(files) > 100
            or pr.get("changedFiles") != len(files)
            or any(not isinstance(row, dict) or not isinstance(row.get("path"), str)
                   for row in files)):
        raise WorkflowError("complete PR paths unavailable")
    paths = sorted(row["path"] for row in files)
    if len(paths) != len(set(paths)):
        raise WorkflowError("duplicate PR path")
    issue = gh.issue(issue_number)
    labels = {row["name"] for row in issue.get("labels", [])}
    if not {"queue:worker", "state:in-progress"}.issubset(labels):
        raise WorkflowError("issue queue state changed")
    spec = parse_issue(issue.get("body") or "")
    if not _approved_on_github(gh, cfg, issue_number, spec):
        raise WorkflowError("issue admission changed")
    validate_changed_paths(paths, spec.allowed_paths)
    return pr, issue_number, spec, paths


def _usage(conn, issue: int, attempts: int) -> tuple[dict, bool]:
    rows = conn.execute("SELECT complete,input_tokens,output_tokens,reasoning_tokens,"
                        "cache_read_tokens,cache_write_tokens,total_tokens,estimated_cost "
                        "FROM usage_events WHERE issue=?", (issue,)).fetchall()
    if attempts < 1 or len(rows) != attempts or not all(row["complete"] == 1 for row in rows):
        return {"observation": "unknown"}, False
    columns = ("input_tokens", "output_tokens", "reasoning_tokens",
               "cache_read_tokens", "cache_write_tokens", "total_tokens")
    if any(any(type(row[column]) is not int or row[column] < 0 for column in columns)
           or type(row["estimated_cost"]) not in (int, float)
           or row["estimated_cost"] < 0 for row in rows):
        return {"observation": "unknown"}, False
    return {"observation": "measured",
            **{key: sum(row[column] for row in rows)
               for key, column in zip(("input", "output", "reasoning", "cache_read",
                                       "cache_write", "total"), columns)},
            "estimated_cost": sum(row["estimated_cost"] for row in rows)}, True


def export_manifest(cfg: Config, pr_number: int) -> dict:
    """Export a validated observation from saved controller checks and live refs."""
    gh = _github(cfg)
    with state.exclusive(cfg.state_dir):
        pr, issue_number, spec, paths = _live(cfg, gh, pr_number)
        with state.connect(cfg.state_dir) as conn:
            job = state.job(conn, issue_number)
            admitted = state.admission(conn, issue_number)
            if (job is None or admitted is None or job["status"] != "awaiting_review"
                    or job["pr"] != pr_number or job["branch"] != pr["headRefName"]
                    or job["head_sha"] != pr["headRefOid"]
                    or job["base_sha"] != pr["baseRefOid"]
                    or job["image_id"] != published_image_id(pr.get("body") or "")
                    or admitted["body_sha"] != spec.body_sha256
                    or tuple(json.loads(admitted["allowed_json"])) != spec.allowed_paths
                    or tuple(json.loads(job["allowed_json"])) != spec.allowed_paths):
                raise WorkflowError("controller evidence identity is stale")
            attempts = job["attempts_count"]
            usage, measured = _usage(conn, issue_number, attempts)
            model = job["result_model"]
        evidence_path = (cfg.state_dir / "evidence" / str(issue_number)
                         / f"{pr['headRefOid']}.json")
        saved = read_evidence(evidence_path)
        proof = saved.get("ci")
        if (saved.get("issue") != issue_number or saved.get("pr") != pr_number
                or saved.get("branch") != pr["headRefName"]
                or saved.get("head") != pr["headRefOid"]
                or saved.get("base") != pr["baseRefOid"]
                or saved.get("body_sha256") != spec.body_sha256
                or saved.get("paths") != paths or not isinstance(proof, dict)
                or proof.get("head") != pr["headRefOid"]
                or proof.get("committed_diff_base") != pr["baseRefOid"]
                or proof.get("result") != "passed"
                or proof.get("image_id") != job["image_id"]):
            raise WorkflowError("saved check evidence is stale")
        local_checks = proof.get("checks")
        if not isinstance(local_checks, list) or len(local_checks) != len(cfg.checks):
            raise WorkflowError("saved check policy changed")
        checks = []
        for index, (observed, command) in enumerate(zip(local_checks, cfg.checks), 1):
            if (not isinstance(observed, dict) or observed.get("command") != list(command)
                    or observed.get("exit_code") != 0):
                raise WorkflowError("saved check policy changed")
            checks.append({"index": index, "result": "passed",
                           "log_sha256": observed.get("log_sha256")})
        unknowns = []
        if not isinstance(model, str) or not model:
            model = None
            unknowns.append("model")
        if not measured:
            unknowns.append("usage")
        manifest = make_manifest(repo=cfg.repo, issue=issue_number, pr=pr_number,
                                 branch=pr["headRefName"],
                                 head_repository=cfg.publish_repo or cfg.repo,
                                 base=pr["baseRefOid"], head=pr["headRefOid"],
                                 body_sha256=spec.body_sha256,
                                 scope_sha256=_scope_sha256(spec.allowed_paths),
                                 checks_sha256=_checks_sha256(cfg), paths=paths,
                                 checks=checks, image=job["image_id"], model=model,
                                 attempts=attempts, usage=usage, unknowns=unknowns)
        current, _, current_spec, current_paths = _live(cfg, gh, pr_number)
        if (current["headRefOid"] != pr["headRefOid"]
                or current["baseRefOid"] != pr["baseRefOid"]
                or current_paths != paths
                or current_spec.body_sha256 != spec.body_sha256
                or current_spec.allowed_paths != spec.allowed_paths
                or published_image_id(current.get("body") or "") != job["image_id"]):
            raise WorkflowError("PR changed during evidence export")
        return manifest


def verify_manifest(cfg: Config, pr_number: int, manifest: dict) -> dict:
    """Verify a portable observation against current GitHub and local policy."""
    record = validate_manifest(manifest)
    gh = _github(cfg)
    try:
        pr, issue_number, spec, paths = _live(cfg, gh, pr_number)
    except WorkflowError:
        return {"status": "stale", "reason": "remote_identity"}
    try:
        published_image = published_image_id(pr.get("body") or "")
    except WorkflowError:
        return {"status": "stale", "reason": "remote_identity"}
    expected = {"repo": cfg.repo, "issue": issue_number, "pr": pr_number,
                "branch": pr["headRefName"],
                "head_repository": cfg.publish_repo or cfg.repo,
                "base": pr["baseRefOid"], "head": pr["headRefOid"],
                "body_sha256": spec.body_sha256,
                "scope_sha256": _scope_sha256(spec.allowed_paths),
                "checks_sha256": _checks_sha256(cfg), "paths": paths,
                "image": published_image}
    for key, value in expected.items():
        if record.get(key) != value:
            return {"status": "stale", "reason": "identity_changed"}
    try:
        image_id = _resolve_image_id(cfg.image)
    except WorkflowError:
        return {"status": "stale", "reason": "image_unavailable"}
    if image_id != record["image"]:
        return {"status": "stale", "reason": "image_changed"}
    return {"status": "current", "repo": cfg.repo, "issue": issue_number,
            "pr": pr_number, "head": record["head"], "sha256": record["sha256"],
            "observations": "controller_claim_not_independently_verified"}
