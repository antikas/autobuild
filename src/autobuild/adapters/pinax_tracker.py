"""Pinax tracker adapter. Pinax commits every mutation itself and publishes it
from the checked-out remote default branch."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from autobuild.domain import (
    AdapterError,
    AdapterIdentity,
    CampaignRef,
    ClaimReceipt,
    ClaimTaken,
    CloseEvidence,
    EvidenceError,
    ProbeResult,
    Proposal,
    ProposalRef,
    TrackerPublication,
    TrackerStop,
    WorkItem,
    WorkspaceRef,
)
from autobuild.adapters.tracker_commits import (
    head_on_remote,
    require_committed,
    require_no_foreign_changes,
)

_TRACKER_PATHS = (".ergon",)

# Pinax exit codes for a mutating command. 0 is committed and 3 on a claim is
# a superseded claim; both are handled by the adapter. 4 is an environment
# stop and the rest are refusals; every stop carries Pinax's own explanation.
_ENVIRONMENT_EXITS = frozenset({4})
_REFUSED_EXITS = frozenset({1, 2, 5, 6, 7})


class PinaxTrackerAdapter:
    # Pinax commits every mutation itself; the workspace adapter verifies the
    # tracker commit instead of creating one.
    commits_itself = True

    def __init__(
        self,
        repository: Path,
        proposal_prefix: str = "abp",
        accept_unpublished_claims: bool = False,
    ) -> None:
        """``accept_unpublished_claims`` accepts a claim Pinax committed but
        did not publish because the checked-out branch is not the remote
        default branch; the claim then travels with the branch's pull request.
        Every other unpublished claim stops the campaign."""

        self._repository = repository.resolve(strict=False)
        self._proposal_prefix = proposal_prefix
        self._accept_unpublished_claims = accept_unpublished_claims

    def probe(self) -> ProbeResult:
        executable = shutil.which("pinax")
        if executable is None:
            return ProbeResult.unavailable("pinax executable was not found")
        if not (self._repository / ".ergon").is_dir():
            return ProbeResult.unavailable(f"Pinax is not initialised at {self._repository}")
        completed = subprocess.run(
            [executable, "--help"], capture_output=True, text=True, check=False
        )
        if completed.returncode != 0:
            return ProbeResult.unavailable(completed.stderr.strip() or "pinax probe failed")
        return ProbeResult.ready(
            AdapterIdentity(
                "pinax-tracker",
                "cli",
                frozenset({"queue", "claim", "close", "park", "proposal-gate"}),
            ),
            str(self._repository),
        )

    def next_item(self, campaign: CampaignRef) -> WorkItem | None:
        root = campaign.repository.resolve(strict=False)
        if root != self._repository:
            raise AdapterError("campaign repository does not match the bound Pinax repository")
        completed = self._run(root, "next", "--actor", "coordinator@autobuild", "--json")
        if completed.returncode != 0:
            combined = f"{completed.stdout}\n{completed.stderr}".casefold()
            if "no ready" in combined or "queue" in combined and "empty" in combined:
                return None
            raise AdapterError(completed.stderr.strip() or completed.stdout.strip() or "pinax next failed")
        if not completed.stdout.strip():
            return None
        payload = json.loads(completed.stdout)
        if payload.get("item_id") is None:
            return None
        item_id = str(payload["item_id"])
        title = str(payload["title"])
        brief_ref, caption = self._brief_note(root, item_id)
        return WorkItem(
            item_id,
            title,
            brief_ref,
            (caption or f"Measurable acceptance is defined by {brief_ref}",),
        )

    def ready_items(self, campaign: CampaignRef) -> tuple[WorkItem, ...]:
        root = campaign.repository.resolve(strict=False)
        if root != self._repository:
            raise AdapterError("campaign repository does not match the bound Pinax repository")
        completed = self._run(
            root, "ready", "--actor", "coordinator@autobuild", "--json"
        )
        if completed.returncode != 0:
            combined = f"{completed.stdout}\n{completed.stderr}".casefold()
            if "no ready" in combined or ("queue" in combined and "empty" in combined):
                return ()
            raise AdapterError(
                completed.stderr.strip() or completed.stdout.strip() or "pinax ready failed"
            )
        if not completed.stdout.strip():
            return ()
        payload = json.loads(completed.stdout)
        rows = payload.get("items", payload) if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise AdapterError("pinax ready returned an unexpected JSON shape")
        titles = self._titles(root)
        items: list[WorkItem] = []
        for row in rows:
            if isinstance(row, dict):
                item_id = str(row.get("item_id") or "")
                title = str(row.get("title") or titles.get(item_id, "") or item_id)
            elif isinstance(row, str):
                item_id = row
                title = titles.get(item_id, "") or item_id
            else:
                continue
            if not item_id:
                continue
            brief_ref, caption = self._brief_note(root, item_id)
            items.append(
                WorkItem(
                    item_id,
                    title,
                    brief_ref,
                    (caption or f"Measurable acceptance is defined by {brief_ref}",),
                )
            )
        return tuple(items)

    @staticmethod
    def _titles(root: Path) -> dict[str, str]:
        titles: dict[str, str] = {}
        for log in (root / ".ergon" / "log").glob("*.jsonl"):
            for line in log.read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") != "item.created":
                    continue
                payload = event.get("payload", {})
                item_id = str(payload.get("item_id", ""))
                title = str(payload.get("title", ""))
                if item_id and title:
                    titles[item_id] = title
        return titles

    def resumable_claims(self, campaign: CampaignRef) -> tuple[WorkItem, ...]:
        root = campaign.repository.resolve(strict=False)
        if root != self._repository:
            raise AdapterError("campaign repository does not match the bound Pinax repository")
        items_dir = root / ".ergon" / "items"
        if not items_dir.is_dir():
            return ()
        terminal = {"done", "shipped", "parked", "cancelled", "blocked"}
        result: list[WorkItem] = []
        for path in sorted(items_dir.glob("*.md")):
            front = self._frontmatter(path)
            item_id = front.get("id", "").strip()
            owner = front.get("owner", "").strip()
            status = front.get("status", "").strip().casefold()
            if not item_id or not owner.casefold().startswith("builder"):
                continue
            if status in terminal:
                continue
            try:
                brief_ref, caption = self._brief_note(root, item_id)
            except EvidenceError:
                continue
            result.append(
                WorkItem(
                    item_id,
                    front.get("title", "").strip() or item_id,
                    brief_ref,
                    (caption or f"Measurable acceptance is defined by {brief_ref}",),
                )
            )
        return tuple(result)

    @staticmethod
    def _frontmatter(path: Path) -> dict[str, str]:
        front: dict[str, str] = {}
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return front
        if not lines or lines[0].strip() != "---":
            return front
        for line in lines[1:]:
            if line.strip() == "---":
                break
            key, sep, value = line.partition(":")
            if sep:
                front[key.strip()] = value.strip()
        return front

    def claim(self, item: WorkItem, actor: str) -> ClaimReceipt:
        if self._git(
            self._repository, "status", "--porcelain", "--untracked-files=all"
        ).strip():
            raise AdapterError("primary checkout must be clean before a tracker claim")
        _, publication = self._mutate(
            self._repository, "claim", item.item_id, "--actor", actor, "--json"
        )
        return ClaimReceipt(
            item.item_id,
            actor,
            datetime.now(UTC).isoformat(),
            published=publication.published,
            publication_note=publication.note,
        )

    def close(
        self,
        evidence: CloseEvidence,
        item_commit: str,
        workspace: WorkspaceRef,
        actor: str,
    ) -> None:
        briefing = workspace.root / f".autobuild-{evidence.item_id}-briefing.md"
        briefing.write_text(self._briefing(evidence, item_commit), encoding="utf-8")
        try:
            self._mutate(
                workspace.root,
                "done",
                evidence.item_id,
                "--briefing",
                str(briefing),
                "--actor",
                actor,
                "--json",
            )
        finally:
            briefing.unlink(missing_ok=True)

    def park(
        self, item_id: str, reason: str, actor: str, workspace: WorkspaceRef | None = None
    ) -> TrackerPublication | None:
        if workspace is not None:
            self._mutate(
                workspace.root, "park", item_id, "--reason", reason, "--actor", actor, "--json"
            )
            return None
        require_no_foreign_changes(self._git, self._repository, _TRACKER_PATHS)
        _, publication = self._mutate(
            self._repository, "park", item_id, "--reason", reason, "--actor", actor, "--json"
        )
        return publication

    def propose(self, proposal: Proposal, actor: str) -> ProposalRef:
        self.validate_proposal(proposal)
        require_no_foreign_changes(self._git, self._repository, _TRACKER_PATHS)
        created, added = self._mutate(
            self._repository,
            "add",
            "--title",
            proposal.title,
            "--prefix",
            self._proposal_prefix,
            "--allow-new-prefix",
            "--actor",
            actor,
            "--json",
        )
        proposal_id = str(created.get("item_id") or created.get("id") or "")
        if not proposal_id:
            raise AdapterError("pinax add did not return a proposal id")
        _, blocked = self._mutate(
            self._repository,
            "block",
            proposal_id,
            "--gate",
            "proposal",
            "--actor",
            actor,
            "--json",
        )
        caption = f"{proposal.question} {proposal.rationale}"[:200]
        _, noted = self._mutate(
            self._repository,
            "note",
            "add",
            proposal_id,
            "--ref",
            proposal.brief_ref,
            "--caption",
            caption,
            "--actor",
            actor,
            "--json",
        )
        writes = (added, blocked, noted)
        return ProposalRef(
            proposal_id,
            runnable=False,
            published=all(write.published for write in writes),
            publication_note=next((write.note for write in writes if not write.published), ""),
        )

    @staticmethod
    def validate_proposal(proposal: Proposal) -> None:
        reference = proposal.brief_ref.strip()
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", reference):
            return
        normalised = reference.replace("\\", "/")
        path = PurePosixPath(normalised)
        unsafe = (
            normalised.startswith(("/", "~"))
            or re.match(r"^[A-Za-z]:/", normalised) is not None
            or any(part in {".", ".."} for part in path.parts)
        )
        if unsafe:
            raise EvidenceError(
                "proposal brief_ref must be a repository-relative path or durable URI"
            )

    @staticmethod
    def _brief_note(root: Path, item_id: str) -> tuple[str, str]:
        notes: list[tuple[str, str, str]] = []
        for log in (root / ".ergon" / "log").glob("*.jsonl"):
            for line in log.read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = event.get("payload", {})
                if event.get("type") == "note.added" and payload.get("item_id") == item_id:
                    notes.append(
                        (
                            str(event.get("ts", "")),
                            str(payload.get("ref", "")),
                            str(payload.get("caption", "")),
                        )
                    )
        if not notes:
            raise EvidenceError(f"Pinax item {item_id} has no approved brief reference")
        _, ref, caption = max(notes)
        if not ref:
            raise EvidenceError(f"Pinax item {item_id} has an empty brief reference")
        return ref, caption

    @staticmethod
    def _briefing(evidence: CloseEvidence, item_commit: str) -> str:
        changed = "\n".join(f"- {entry.kind.value}: `{entry.path.as_posix()}`" for entry in evidence.diff.changed_paths)
        return (
            f"## Outcome\n\nAccepted and delivered by AutoBuild.\n\n"
            f"## Evidence\n\n"
            f"- Item commit: `{item_commit}`\n"
            f"- Workspace revision: `{evidence.workspace_revision}`\n"
            f"- Validator: `{evidence.validation.validator_id}`\n"
            f"- Validator output: `{evidence.validation.command.stdout_ref}`\n"
            f"- Review verdict: `{evidence.verdict.evidence_ref}`\n"
            f"- Trajectory: `{evidence.trajectory_ref}`\n\n"
            f"## Changed paths\n\n{changed or '- None'}\n"
        )

    def _mutate(
        self, root: Path, *args: str
    ) -> tuple[dict[str, object], TrackerPublication]:
        """Run one mutating Pinax command and confirm it committed its event.

        Pinax appends, commits and, on the checked-out remote default branch,
        pushes in the one command, so the adapter never stages or commits
        tracker files. A nonzero exit is mapped to a typed stop that carries
        Pinax's own report. Returns the command's JSON and its publication."""

        command = args[0]
        before = self._git(root, "rev-parse", "HEAD")
        completed = self._run(root, *args)
        code = completed.returncode
        report = self._last_json(completed.stdout)
        notes = completed.stderr.strip()
        cause = "; ".join(
            part for part in (str(report.get("message", "")).strip(), notes) if part
        ) or completed.stdout.strip()
        if code == 3 and command == "claim":
            raise ClaimTaken(args[1], cause or "claim superseded")
        if code == 4 and command == "claim" and self._branch_local_claim(report):
            require_committed(self._git, root, before, _TRACKER_PATHS)
            return report, TrackerPublication(False, cause)
        status = str(report.get("status", "")).strip()
        label = f"pinax {command} exit {code}" + (f" ({status})" if status else "")
        if code in _ENVIRONMENT_EXITS:
            raise TrackerStop(f"{label}: {cause}", code=code, environment=True)
        if code in _REFUSED_EXITS:
            raise TrackerStop(f"{label}: {cause}", code=code, environment=False)
        if code != 0:
            raise AdapterError(f"{label}: {cause}")
        require_committed(self._git, root, before, _TRACKER_PATHS)
        payload = self._parse(command, completed.stdout)
        pushed = payload.get("pushed")
        published = pushed if isinstance(pushed, bool) else head_on_remote(self._git, root)
        return payload, TrackerPublication(published, "" if published else notes)

    def _branch_local_claim(self, report: dict[str, object]) -> bool:
        """A claim Pinax committed and did not push only because the checked-out
        branch is not the remote default branch, in a mode that accepts it."""

        head_branch = str(report.get("head_branch") or "")
        return (
            self._accept_unpublished_claims
            and report.get("status") == "committed_local"
            and report.get("committed") is True
            and bool(head_branch)
            and head_branch != str(report.get("remote_branch") or "")
        )

    @staticmethod
    def _last_json(stdout: str) -> dict[str, object]:
        for line in reversed(stdout.strip().splitlines()):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            return payload if isinstance(payload, dict) else {}
        return {}

    @staticmethod
    def _parse(command: str, stdout: str) -> dict[str, object]:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise AdapterError(f"pinax returned invalid JSON for {command}") from exc
        if not isinstance(payload, dict):
            raise AdapterError(f"pinax returned a non-object JSON payload for {command}")
        return payload

    @staticmethod
    def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["pinax", "--root", str(root), *args],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    @staticmethod
    def _git(root: Path, *args: str) -> str:
        attempts = 5 if args and args[0] in {"add", "commit", "push"} else 1
        for attempt in range(attempts):
            completed = subprocess.run(
                ["git", "-C", str(root), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            detail = completed.stderr.strip() or completed.stdout.strip()
            transient = "permission denied" in detail.casefold() or "unpacker error" in detail.casefold()
            if completed.returncode == 0 or not transient or attempt == attempts - 1:
                break
            time.sleep(0.2 * (attempt + 1))
        if completed.returncode != 0:
            raise AdapterError(f"git {' '.join(args)} failed: {detail}")
        return completed.stdout.rstrip("\r\n")
