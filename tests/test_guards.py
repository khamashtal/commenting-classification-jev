"""Rules about the codebase itself, enforced so they cannot quietly lapse.

These are cheap static checks. They exist because every one of them encodes a decision
that was expensive to make and would be easy to undo by accident.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
TESTS = Path(__file__).resolve().parent
PY_FILES = sorted(SRC.rglob("*.py"))


def _module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


class TestNoLiveCalls:
    """The suite must never cost money or depend on a network."""

    def test_no_test_constructs_the_real_jev_client(self) -> None:
        for path in sorted(TESTS.glob("test_*.py")):
            source = path.read_text(encoding="utf-8")
            for match in re.finditer(r"\bJevClient\(", source):
                line = source[: match.start()].count("\n") + 1
                # Constructing it is fine; entering it would open a connection.
                context = source[match.start() : match.start() + 400]
                assert "async with" not in context.split("\n")[0], (
                    f"{path.name}:{line} enters a real JevClient"
                )

    def test_no_test_imports_the_typesafe_sdk_client(self) -> None:
        # This file names the forbidden symbols in order to forbid them.
        for path in sorted(TESTS.glob("test_*.py")):
            if path.name == "test_guards.py":
                continue
            source = path.read_text(encoding="utf-8")
            assert "AsyncTypeSafeClient" not in source, (
                f"{path.name} references the real SDK client"
            )

    def test_no_hardcoded_service_urls_in_tests(self) -> None:
        for path in sorted(TESTS.glob("*.py")):
            if path.name == "test_guards.py":
                continue
            source = path.read_text(encoding="utf-8")
            for host in (
                "api.typesafe.ai",
                "comments-mcp.viafoura.co",
                "livecomments.viafoura.co",
            ):
                assert host not in source, f"{path.name} names a live host: {host}"


class TestNoScraping:
    """Decision: everything about an article comes from CAPI. See CLAUDE.md."""

    def test_nothing_parses_html(self) -> None:
        for path in PY_FILES:
            source = path.read_text(encoding="utf-8")
            for banned in ("BeautifulSoup", "html.parser", "lxml", "<meta"):
                assert banned not in source, f"{path.name} looks like it parses HTML"

    def test_no_module_requests_text_html(self) -> None:
        for path in PY_FILES:
            assert "text/html" not in path.read_text(encoding="utf-8"), (
                f"{path.name} asks a server for HTML"
            )


class TestSecrets:
    """Decision: settings are read once, in one place, and never logged."""

    def test_environment_is_only_read_where_it_should_be(self) -> None:
        allowed = {"settings.py", "log_config.py"}
        for path in PY_FILES:
            if path.name in allowed:
                continue
            source = path.read_text(encoding="utf-8")
            assert "os.environ" not in source, (
                f"{path.name} reads os.environ directly; use Settings"
            )
            assert "getenv" not in source, (
                f"{path.name} reads getenv directly; use Settings"
            )

    def test_no_secret_is_ever_formatted_into_a_string(self) -> None:
        pattern = re.compile(
            r"[\"'{][^\"'}]*\b(api_key|apikey|token|secret|password)\b",
            re.IGNORECASE,
        )
        for path in PY_FILES:
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(),
                1,
            ):
                stripped = line.strip()
                if stripped.startswith("#") or '"""' in stripped:
                    continue
                if ("logger." in stripped or "print(" in stripped) and pattern.search(
                    stripped,
                ):
                    pytest.fail(f"{path.name}:{number} may log a secret: {stripped}")

    def test_dotenv_is_never_read_by_the_processing_layer(self) -> None:
        for path in (SRC / "processing").rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            if path.name == "settings.py":
                continue
            assert "load_dotenv" not in source, f"{path.name} loads .env itself"


class TestAsyncHygiene:
    """This pipeline is IO-bound; a blocking call stalls every task, not one."""

    BLOCKING = {
        "time.sleep": "use asyncio.sleep",
        "requests.get": "use aiohttp or httpx",
        "requests.post": "use aiohttp or httpx",
        "urllib.request.urlopen": "use an async client",
    }

    def test_no_blocking_calls_inside_async_functions(self) -> None:
        for path in PY_FILES:
            tree = _module(path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.AsyncFunctionDef):
                    continue
                body = ast.unparse(node)
                for call, advice in self.BLOCKING.items():
                    assert call not in body, (
                        f"{path.name}: {node.name} calls {call} — {advice}"
                    )

    def test_file_writes_in_async_code_go_through_a_thread(self) -> None:
        """Disk IO is blocking, so every such call must be offloaded to a thread."""
        tree = _module(SRC / "processing" / "workflow.py")
        offloaded: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if ast.unparse(node.func) != "asyncio.to_thread" or not node.args:
                continue
            offloaded.add(ast.unparse(node.args[0]))
        for helper in ("_write_report", "load_store", "save_store"):
            assert helper in offloaded, (
                f"{helper} touches the disk and must go through asyncio.to_thread"
            )

    def test_every_gather_over_comments_is_bounded(self) -> None:
        """Fanning out over a 10,000-comment thread needs a bound somewhere."""
        source = (SRC / "processing" / "classification.py").read_text(encoding="utf-8")
        assert "asyncio.gather" in source
        client = (SRC / "clients" / "jev.py").read_text(encoding="utf-8")
        assert "Semaphore" in client, "the fan-out is only safe if the client bounds it"

    def test_shared_mutable_rate_state_is_locked(self) -> None:
        source = (SRC / "clients" / "jev.py").read_text(encoding="utf-8")
        assert "asyncio.Lock" in source
        assert "async with self._lock" in source


class TestTypingDiscipline:
    def test_every_module_uses_future_annotations(self) -> None:
        for path in PY_FILES:
            if path.name == "__init__.py":
                continue
            source = path.read_text(encoding="utf-8")
            assert "from __future__ import annotations" in source, path.name

    def test_public_functions_are_annotated(self) -> None:
        for path in PY_FILES:
            for node in ast.walk(_module(path)):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.name.startswith("_"):
                    continue
                assert node.returns is not None, (
                    f"{path.name}: {node.name} has no return annotation"
                )
