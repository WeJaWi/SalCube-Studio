"""Disposable state and a network tripwire for tests of the running studio."""
from contextlib import ExitStack, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


@contextmanager
def isolated_runtime():
    # Keep read-only profiles, branding and reference images in the repo, but
    # redirect every shared mutable path before any test invokes app helpers.
    import app
    from pipeline import config, jobs, llm, stats, storage

    with ExitStack() as stack:
        root = Path(stack.enter_context(TemporaryDirectory(prefix="dmiqwi-tests-")))
        for folder in ("output", "logs", "generated", "uploads"):
            (root / folder).mkdir()
        paths = {
            "OUTPUT_DIR": root / "output",
            "SETTINGS_FILE": root / "output/settings.json",
            "GDRIVE_TOKEN_FILE": root / "output/gdrive_token.json",
            "GDRIVE_CLIENT_SECRET_FILE": root / "client.json",
        }
        for name, path in paths.items():
            stack.enter_context(patch.object(config, name, path))
        stack.enter_context(patch.object(app, "QUEUE_STATE_FILE", root / "output/queue_state.json"))
        stack.enter_context(patch.object(app, "UPDATE_LOCK_FILE", root / "output/.southcube-update.lock"))
        stack.enter_context(patch.object(app, "UPDATE_STATUS_FILE", root / "output/update_status.json"))
        stack.enter_context(patch.object(app, "THUMB_OUT_DIR", root / "generated"))
        stack.enter_context(patch.object(app, "THUMB_UPLOADS_DIR", root / "uploads"))
        stack.enter_context(patch.object(stats, "STATS_FILE", root / "output/render_stats.json"))
        stack.enter_context(patch.object(jobs, "_logs_root", return_value=root / "logs"))
        stack.enter_context(patch.object(app, "DRIVE", storage.GoogleDriveStorage()))
        stack.enter_context(patch.object(app, "JOBS", {}))
        stack.enter_context(patch.object(app, "FLEET_WORKERS", {}))
        stack.enter_context(patch("requests.sessions.Session.request", side_effect=AssertionError(
            "Unmocked network request: tests must never call live providers")))
        stack.enter_context(patch.object(llm, "_claude_cli_chat", side_effect=AssertionError(
            "Unmocked paid CLI request in test")))
        yield root
