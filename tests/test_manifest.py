from pathlib import Path

from dradar.manifest import task_content_hash


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
