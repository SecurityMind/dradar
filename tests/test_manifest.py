import hashlib
from pathlib import Path

from dradar.manifest import task_content_hash


def _legacy_raw_hash(root: Path, task_id: str) -> str:
    task = root / task_id
    digest = hashlib.sha256()
    for name in ("instruction.md", "task.toml"):
        path = task / name
        if path.is_file():
            digest.update(name.encode())
            digest.update(path.read_bytes())
    env = task / "environment"
    for rel in sorted(p.relative_to(env).as_posix()
                      for p in env.rglob("*") if p.is_file()):
        digest.update(rel.encode())
        digest.update((env / rel).read_bytes())
    return digest.hexdigest()


def _write_task(root: Path, task_id: str, instruction="hi", toml="a=1",
                 env_files: dict[str, str] | None = None,
                 solution: str | None = "the solution", tests: str | None = "the tests") -> None:
    task_dir = root / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "instruction.md").write_text(instruction)
    (task_dir / "task.toml").write_text(toml)
    env_dir = task_dir / "environment"
    env_dir.mkdir(exist_ok=True)
    for rel, content in (env_files or {"Dockerfile": "FROM python"}).items():
        path = env_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    if solution is not None:
        sol_dir = task_dir / "solution"
        sol_dir.mkdir(exist_ok=True)
        (sol_dir / "solution.patch").write_text(solution)
    if tests is not None:
        tests_dir = task_dir / "tests"
        tests_dir.mkdir(exist_ok=True)
        (tests_dir / "grader.py").write_text(tests)


def test_hash_stable_across_calls(tmp_path: Path):
    _write_task(tmp_path, "t1")
    h1 = task_content_hash(tmp_path, "t1")
    h2 = task_content_hash(tmp_path, "t1")
    assert h1 == h2


def test_lf_checkout_keeps_the_legacy_hash(tmp_path: Path):
    _write_task(tmp_path, "t1", instruction="one\ntwo\n",
                env_files={"Dockerfile": "FROM python\nRUN true\n"})
    assert task_content_hash(tmp_path, "t1") == _legacy_raw_hash(tmp_path, "t1")


def test_hash_ignores_solution_and_tests(tmp_path: Path):
    _write_task(tmp_path, "t1", solution="solution A", tests="tests A")
    h1 = task_content_hash(tmp_path, "t1")
    _write_task(tmp_path, "t1", solution="solution B (totally different)", tests="tests B (also different)")
    h2 = task_content_hash(tmp_path, "t1")
    assert h1 == h2


def test_hash_changes_when_instruction_changes(tmp_path: Path):
    _write_task(tmp_path, "t1", instruction="version one")
    h1 = task_content_hash(tmp_path, "t1")
    _write_task(tmp_path, "t1", instruction="version two")
    h2 = task_content_hash(tmp_path, "t1")
    assert h1 != h2


def test_hash_changes_when_environment_file_changes(tmp_path: Path):
    _write_task(tmp_path, "t1", env_files={"Dockerfile": "FROM python:3.11"})
    h1 = task_content_hash(tmp_path, "t1")
    _write_task(tmp_path, "t1", env_files={"Dockerfile": "FROM python:3.12"})
    h2 = task_content_hash(tmp_path, "t1")
    assert h1 != h2


def test_hash_changes_when_environment_file_added(tmp_path: Path):
    _write_task(tmp_path, "t1", env_files={"Dockerfile": "FROM python"})
    h1 = task_content_hash(tmp_path, "t1")
    _write_task(tmp_path, "t1", env_files={"Dockerfile": "FROM python", "setup.sh": "echo hi"})
    h2 = task_content_hash(tmp_path, "t1")
    assert h1 != h2


def test_hash_same_content_different_task_dir_matches(tmp_path: Path):
    # Simulates server checkout vs. volunteer's separate clone of the same repo.
    root_a, root_b = tmp_path / "server", tmp_path / "volunteer"
    _write_task(root_a, "t1", solution="server-only solution")
    _write_task(root_b, "t1", solution=None, tests=None)  # volunteer lacks solution/tests locally too, still matches
    assert task_content_hash(root_a, "t1") == task_content_hash(root_b, "t1")


def test_hash_treats_lf_and_crlf_text_checkouts_as_identical(tmp_path: Path):
    lf, crlf = tmp_path / "lf", tmp_path / "crlf"
    content = {
        "instruction": "first line\nsecond line\n",
        "toml": "name = 'example'\nenabled = true\n",
        "env_files": {"Dockerfile": "FROM python\nRUN echo ready\n"},
    }
    _write_task(lf, "t1", **content)
    _write_task(crlf, "t1", **content)
    task = crlf / "t1"
    for path in (task / "instruction.md", task / "task.toml",
                 task / "environment" / "Dockerfile"):
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))

    assert task_content_hash(lf, "t1") == task_content_hash(crlf, "t1")


def test_hash_keeps_binary_line_endings_byte_exact(tmp_path: Path):
    left, right = tmp_path / "left", tmp_path / "right"
    _write_task(left, "t1")
    _write_task(right, "t1")
    (left / "t1" / "environment" / "payload.bin").write_bytes(b"\0row\r\n")
    (right / "t1" / "environment" / "payload.bin").write_bytes(b"\0row\n")

    assert task_content_hash(left, "t1") != task_content_hash(right, "t1")
