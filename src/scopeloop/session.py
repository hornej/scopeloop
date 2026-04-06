"""Session Manager - Artifact storage and event timeline for replayability.

Every autonomous run captures a complete audit trail including:
- Git state (commit, branch, dirty files)
- Config snapshot
- Build/flash logs
- Serial logs (timestamped)
- Waveform captures
- Measurements
- Test results

This enables debugging "why did it pass/fail?" without manual investigation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

import aiofiles

logger = logging.getLogger(__name__)


class SessionState(Enum):
    """State of a session."""

    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class EventSource(Enum):
    """Source of an event."""

    SYSTEM = "system"
    SERIAL = "serial"
    SCOPE = "scope"
    LOGIC = "logic"
    BUILD = "build"
    FLASH = "flash"
    TEST = "test"
    USER = "user"
    AGENT = "agent"


@dataclass
class TimelineEvent:
    """A single event in the session timeline."""

    ts: float  # Unix timestamp
    source: str  # EventSource value
    event: str  # Event type (e.g., "log", "capture", "build_start")
    data: dict[str, Any] = field(default_factory=dict)

    def to_json_line(self) -> str:
        """Convert to JSON Lines format."""
        return json.dumps({
            "ts": self.ts,
            "source": self.source,
            "event": self.event,
            "data": self.data,
        })

    @classmethod
    def from_json_line(cls, line: str) -> TimelineEvent:
        """Parse from JSON Lines format."""
        data = json.loads(line)
        return cls(
            ts=data["ts"],
            source=data["source"],
            event=data["event"],
            data=data.get("data", {}),
        )


@dataclass
class GitState:
    """Git repository state."""

    commit_hash: str | None = None
    branch: str | None = None
    dirty_files: list[str] = field(default_factory=list)
    has_uncommitted_changes: bool = False

    @classmethod
    async def capture(cls, repo_path: Path | None = None) -> GitState:
        """Capture current git state."""
        try:
            cwd = str(repo_path) if repo_path else None

            # Get current commit hash
            result = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "HEAD",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
            )
            stdout, _ = await result.communicate()
            commit_hash = stdout.decode().strip() if result.returncode == 0 else None

            # Get current branch
            result = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "--abbrev-ref", "HEAD",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
            )
            stdout, _ = await result.communicate()
            branch = stdout.decode().strip() if result.returncode == 0 else None

            # Get dirty files
            result = await asyncio.create_subprocess_exec(
                "git", "status", "--porcelain",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
            )
            stdout, _ = await result.communicate()
            dirty_files = []
            if result.returncode == 0:
                for line in stdout.decode().strip().split("\n"):
                    if line.strip():
                        # Format: "XY filename" or "XY filename -> newname"
                        parts = line[3:].split(" -> ")
                        dirty_files.append(parts[0])

            return cls(
                commit_hash=commit_hash,
                branch=branch,
                dirty_files=dirty_files,
                has_uncommitted_changes=len(dirty_files) > 0,
            )
        except Exception as e:
            logger.warning(f"Could not capture git state: {e}")
            return cls()


@dataclass
class SessionMetadata:
    """Metadata for a session."""

    session_id: str
    created_at: str  # ISO format
    started_at: str | None = None
    ended_at: str | None = None
    state: str = SessionState.CREATED.value
    project_name: str | None = None
    description: str | None = None
    iteration_count: int = 0
    outcome: str | None = None  # "success", "failure", "interrupted"


@dataclass
class IterationMetadata:
    """Metadata for a single iteration within a session."""

    iteration_number: int
    started_at: str
    ended_at: str | None = None
    build_success: bool | None = None
    flash_success: bool | None = None
    boot_success: bool | None = None
    test_results: dict[str, bool] = field(default_factory=dict)


class Session:
    """A single autonomous development session.

    Captures all artifacts and events for a development/testing run.

    Usage:
        session = Session.create(sessions_dir, project_name="my_project")

        # Start an iteration
        iteration = session.start_iteration()

        # Log events
        await session.log_event(EventSource.BUILD, "build_start", {"target": "esp32"})
        await session.log_event(EventSource.SERIAL, "log", {"line": "Boot complete"})

        # Store artifacts
        await session.store_artifact(iteration, "build.log", build_log_content)
        await session.store_waveform(iteration, "scope_ch1", samples, metadata)

        # End iteration
        await session.end_iteration(iteration, success=True)

        # End session
        await session.end(outcome="success")
    """

    def __init__(
        self,
        session_dir: Path,
        metadata: SessionMetadata,
        git_state: GitState | None = None,
    ):
        self._session_dir = session_dir
        self._metadata = metadata
        self._git_state = git_state
        self._timeline_file: Path = session_dir / "timeline.jsonl"
        self._current_iteration: int = 0
        self._timeline_lock = asyncio.Lock()

    @property
    def session_id(self) -> str:
        return self._metadata.session_id

    @property
    def session_dir(self) -> Path:
        return self._session_dir

    @property
    def metadata(self) -> SessionMetadata:
        return self._metadata

    @property
    def git_state(self) -> GitState | None:
        return self._git_state

    @property
    def current_iteration(self) -> int:
        return self._current_iteration

    @classmethod
    async def create(
        cls,
        sessions_dir: Path,
        project_name: str | None = None,
        description: str | None = None,
        config_content: str | None = None,
        repo_path: Path | None = None,
    ) -> Session:
        """Create a new session.

        Args:
            sessions_dir: Directory to store sessions.
            project_name: Name of the project.
            description: Session description.
            config_content: Content of scopeloop.yaml to snapshot.
            repo_path: Path to git repository.

        Returns:
            New Session instance.
        """
        # Generate session ID
        now = datetime.now()
        session_id = f"{now.strftime('%Y-%m-%d_%H-%M-%S')}_{_generate_short_id()}"

        # Create session directory
        session_dir = sessions_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "iterations").mkdir(exist_ok=True)

        # Create metadata
        metadata = SessionMetadata(
            session_id=session_id,
            created_at=now.isoformat(),
            project_name=project_name,
            description=description,
        )

        # Capture git state
        git_state = await GitState.capture(repo_path)

        # Save initial files
        async with aiofiles.open(session_dir / "session.json", "w") as f:
            await f.write(json.dumps(asdict(metadata), indent=2))

        async with aiofiles.open(session_dir / "git_state.json", "w") as f:
            await f.write(json.dumps(asdict(git_state), indent=2))

        if config_content:
            async with aiofiles.open(session_dir / "config_snapshot.yaml", "w") as f:
                await f.write(config_content)

        # Create empty timeline
        (session_dir / "timeline.jsonl").touch()

        session = cls(session_dir, metadata, git_state)

        # Log session creation event
        await session.log_event(
            EventSource.SYSTEM,
            "session_created",
            {"session_id": session_id, "project_name": project_name},
        )

        logger.info(f"Created session: {session_id}")
        return session

    @classmethod
    async def load(cls, session_dir: Path) -> Session:
        """Load an existing session.

        Args:
            session_dir: Path to session directory.

        Returns:
            Loaded Session instance.
        """
        # Load metadata
        async with aiofiles.open(session_dir / "session.json", "r") as f:
            metadata_dict = json.loads(await f.read())
            metadata = SessionMetadata(**metadata_dict)

        # Load git state
        git_state = None
        git_state_path = session_dir / "git_state.json"
        if git_state_path.exists():
            async with aiofiles.open(git_state_path, "r") as f:
                git_state_dict = json.loads(await f.read())
                git_state = GitState(**git_state_dict)

        session = cls(session_dir, metadata, git_state)

        # Determine current iteration from existing directories
        iterations_dir = session_dir / "iterations"
        if iterations_dir.exists():
            existing = [int(d.name) for d in iterations_dir.iterdir() if d.name.isdigit()]
            session._current_iteration = max(existing) if existing else 0

        return session

    async def start(self) -> None:
        """Start the session."""
        self._metadata.started_at = datetime.now().isoformat()
        self._metadata.state = SessionState.RUNNING.value
        await self._save_metadata()
        await self.log_event(EventSource.SYSTEM, "session_started", {})

    async def end(self, outcome: str = "success") -> None:
        """End the session.

        Args:
            outcome: Session outcome ("success", "failure", "interrupted").
        """
        self._metadata.ended_at = datetime.now().isoformat()
        self._metadata.state = SessionState.COMPLETED.value
        self._metadata.outcome = outcome
        await self._save_metadata()
        await self.log_event(
            EventSource.SYSTEM, "session_ended", {"outcome": outcome}
        )
        logger.info(f"Ended session {self.session_id} with outcome: {outcome}")

    async def start_iteration(self) -> int:
        """Start a new iteration.

        Returns:
            Iteration number.
        """
        self._current_iteration += 1
        self._metadata.iteration_count = self._current_iteration

        # Create iteration directory
        iteration_dir = self._session_dir / "iterations" / f"{self._current_iteration:03d}"
        iteration_dir.mkdir(parents=True, exist_ok=True)

        # Create iteration metadata
        iteration_metadata = IterationMetadata(
            iteration_number=self._current_iteration,
            started_at=datetime.now().isoformat(),
        )
        async with aiofiles.open(iteration_dir / "iteration.json", "w") as f:
            await f.write(json.dumps(asdict(iteration_metadata), indent=2))

        await self._save_metadata()
        await self.log_event(
            EventSource.SYSTEM,
            "iteration_started",
            {"iteration": self._current_iteration},
        )

        logger.info(f"Started iteration {self._current_iteration}")
        return self._current_iteration

    async def end_iteration(
        self,
        iteration: int,
        build_success: bool | None = None,
        flash_success: bool | None = None,
        boot_success: bool | None = None,
        test_results: dict[str, bool] | None = None,
    ) -> None:
        """End an iteration.

        Args:
            iteration: Iteration number.
            build_success: Whether build succeeded.
            flash_success: Whether flash succeeded.
            boot_success: Whether boot succeeded.
            test_results: Dict of test name -> success.
        """
        iteration_dir = self._session_dir / "iterations" / f"{iteration:03d}"

        # Update iteration metadata
        iteration_path = iteration_dir / "iteration.json"
        if iteration_path.exists():
            async with aiofiles.open(iteration_path, "r") as f:
                iteration_data = json.loads(await f.read())

            iteration_data["ended_at"] = datetime.now().isoformat()
            iteration_data["build_success"] = build_success
            iteration_data["flash_success"] = flash_success
            iteration_data["boot_success"] = boot_success
            iteration_data["test_results"] = test_results or {}

            async with aiofiles.open(iteration_path, "w") as f:
                await f.write(json.dumps(iteration_data, indent=2))

        await self.log_event(
            EventSource.SYSTEM,
            "iteration_ended",
            {
                "iteration": iteration,
                "build_success": build_success,
                "flash_success": flash_success,
                "boot_success": boot_success,
            },
        )

        logger.info(f"Ended iteration {iteration}")

    async def log_event(
        self,
        source: EventSource | str,
        event: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Log an event to the timeline.

        Args:
            source: Event source.
            event: Event type.
            data: Event data.
        """
        if isinstance(source, EventSource):
            source = source.value

        timeline_event = TimelineEvent(
            ts=time.time(),
            source=source,
            event=event,
            data=data or {},
        )

        async with self._timeline_lock:
            async with aiofiles.open(self._timeline_file, "a") as f:
                await f.write(timeline_event.to_json_line() + "\n")

    async def store_artifact(
        self,
        iteration: int,
        filename: str,
        content: str | bytes,
    ) -> Path:
        """Store an artifact for an iteration.

        Args:
            iteration: Iteration number.
            filename: Artifact filename.
            content: Artifact content.

        Returns:
            Path to stored artifact.
        """
        iteration_dir = self._session_dir / "iterations" / f"{iteration:03d}"
        iteration_dir.mkdir(parents=True, exist_ok=True)

        artifact_path = iteration_dir / filename
        mode = "wb" if isinstance(content, bytes) else "w"

        async with aiofiles.open(artifact_path, mode) as f:
            await f.write(content)

        logger.debug(f"Stored artifact: {artifact_path}")
        return artifact_path

    async def store_waveform(
        self,
        iteration: int,
        name: str,
        samples: Any,  # numpy array
        metadata: dict[str, Any],
    ) -> tuple[Path, Path]:
        """Store waveform data for an iteration.

        Args:
            iteration: Iteration number.
            name: Waveform name (e.g., "scope_ch1").
            samples: Numpy array of samples.
            metadata: Waveform metadata (sample rate, settings, etc.).

        Returns:
            Tuple of (data_path, metadata_path).
        """
        import numpy as np

        iteration_dir = self._session_dir / "iterations" / f"{iteration:03d}"
        iteration_dir.mkdir(parents=True, exist_ok=True)

        # Store samples as .npy
        data_path = iteration_dir / f"{name}.npy"
        np.save(data_path, samples)

        # Store metadata as JSON
        metadata_path = iteration_dir / f"{name}_meta.json"
        metadata["stored_at"] = datetime.now().isoformat()
        async with aiofiles.open(metadata_path, "w") as f:
            await f.write(json.dumps(metadata, indent=2))

        logger.debug(f"Stored waveform: {data_path}")
        return data_path, metadata_path

    async def get_timeline(
        self,
        source: str | None = None,
        event: str | None = None,
        since_ts: float | None = None,
    ) -> list[TimelineEvent]:
        """Get timeline events, optionally filtered.

        Args:
            source: Filter by source.
            event: Filter by event type.
            since_ts: Only events after this timestamp.

        Returns:
            List of matching events.
        """
        events = []
        async with aiofiles.open(self._timeline_file, "r") as f:
            async for line in f:
                line = line.strip()
                if not line:
                    continue
                evt = TimelineEvent.from_json_line(line)

                if source is not None and evt.source != source:
                    continue
                if event is not None and evt.event != event:
                    continue
                if since_ts is not None and evt.ts < since_ts:
                    continue

                events.append(evt)

        return events

    async def _save_metadata(self) -> None:
        """Save session metadata to disk."""
        async with aiofiles.open(self._session_dir / "session.json", "w") as f:
            await f.write(json.dumps(asdict(self._metadata), indent=2))

    def get_iteration_dir(self, iteration: int) -> Path:
        """Get the directory for an iteration."""
        return self._session_dir / "iterations" / f"{iteration:03d}"


class SessionManager:
    """Manage multiple sessions.

    Usage:
        manager = SessionManager(Path("./sessions"))

        # Create a new session
        session = await manager.create_session(project_name="my_project")

        # List all sessions
        sessions = await manager.list_sessions()

        # Load a specific session
        session = await manager.load_session(session_id)
    """

    def __init__(self, sessions_dir: Path):
        self._sessions_dir = sessions_dir
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        self._active_session: Session | None = None

    @property
    def sessions_dir(self) -> Path:
        return self._sessions_dir

    @property
    def active_session(self) -> Session | None:
        return self._active_session

    async def create_session(
        self,
        project_name: str | None = None,
        description: str | None = None,
        config_content: str | None = None,
        repo_path: Path | None = None,
    ) -> Session:
        """Create a new session.

        Args:
            project_name: Name of the project.
            description: Session description.
            config_content: Content of scopeloop.yaml to snapshot.
            repo_path: Path to git repository.

        Returns:
            New Session instance.
        """
        session = await Session.create(
            self._sessions_dir,
            project_name=project_name,
            description=description,
            config_content=config_content,
            repo_path=repo_path,
        )
        self._active_session = session
        return session

    async def load_session(self, session_id: str) -> Session:
        """Load an existing session.

        Args:
            session_id: Session ID to load.

        Returns:
            Loaded Session instance.
        """
        session_dir = self._sessions_dir / session_id
        if not session_dir.exists():
            raise FileNotFoundError(f"Session not found: {session_id}")

        session = await Session.load(session_dir)
        self._active_session = session
        return session

    async def list_sessions(self, limit: int | None = None) -> list[SessionMetadata]:
        """List all sessions.

        Args:
            limit: Maximum number of sessions to return (most recent first).

        Returns:
            List of session metadata, sorted by creation time (newest first).
        """
        sessions = []

        for session_dir in self._sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue

            metadata_path = session_dir / "session.json"
            if not metadata_path.exists():
                continue

            try:
                async with aiofiles.open(metadata_path, "r") as f:
                    metadata_dict = json.loads(await f.read())
                    sessions.append(SessionMetadata(**metadata_dict))
            except Exception as e:
                logger.warning(f"Could not load session {session_dir}: {e}")

        # Sort by creation time (newest first)
        sessions.sort(key=lambda s: s.created_at, reverse=True)

        if limit:
            sessions = sessions[:limit]

        return sessions

    async def delete_session(self, session_id: str) -> None:
        """Delete a session and all its artifacts.

        Args:
            session_id: Session ID to delete.
        """
        session_dir = self._sessions_dir / session_id
        if not session_dir.exists():
            raise FileNotFoundError(f"Session not found: {session_id}")

        shutil.rmtree(session_dir)
        logger.info(f"Deleted session: {session_id}")

        if self._active_session and self._active_session.session_id == session_id:
            self._active_session = None

    async def cleanup_old_sessions(self, max_age_days: int = 30) -> int:
        """Delete sessions older than the specified age.

        Args:
            max_age_days: Maximum age in days.

        Returns:
            Number of sessions deleted.
        """
        from datetime import timedelta

        cutoff = datetime.now() - timedelta(days=max_age_days)
        deleted = 0

        sessions = await self.list_sessions()
        for session in sessions:
            created = datetime.fromisoformat(session.created_at)
            if created < cutoff:
                await self.delete_session(session.session_id)
                deleted += 1

        logger.info(f"Cleaned up {deleted} old sessions")
        return deleted


def _generate_short_id() -> str:
    """Generate a short random ID."""
    import secrets

    return secrets.token_hex(3)
