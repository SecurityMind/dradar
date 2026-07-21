import argparse
import json
import subprocess
from types import SimpleNamespace
from pathlib import Path

from dradar import image_cache, local_config, runloop


PROJECT = "some-task__abc1234"
MAIN_REF = f"{PROJECT}-main:latest"
PROXY_REF = f"{PROJECT}-pier-egress-proxy:latest"


def _inspect(reference=MAIN_REF, *, project=PROJECT, service="main", image_id="sha256:abc"):
    return {
        "Id": image_id,
        "Created": "2026-07-20T00:00:00Z",
        "Size": 2 * image_cache.GIB,
        "RepoTags": [reference],
        "Config": {"Labels": {
            "com.docker.compose.project": project,
            "com.docker.compose.service": service,
            "com.docker.compose.version": "2.0",
        }},
    }


def _image(reference=MAIN_REF, *, project=PROJECT, service="main",
           image_id="sha256:abc", size=2 * image_cache.GIB, containers=0):
    return image_cache.DockerImage(
        reference, image_id, project, service, size, containers,
        "2026-07-20T00:00:00Z",
    )


def test_discovery_requires_matching_compose_labels_and_exact_tag(monkeypatch):
    bad_ref = "unrelated-main:latest"
    monkeypatch.setattr(image_cache, "_inventory_rows", lambda: {
        MAIN_REF: {"ID": "sha256:abc", "UniqueSize": "2GB", "Containers": "0"},
        bad_ref: {"ID": "sha256:def", "UniqueSize": "9GB", "Containers": "0"},
    })
    monkeypatch.setattr(image_cache, "_inspect", lambda _refs: {
        MAIN_REF: _inspect(),
        bad_ref: _inspect(bad_ref, project="unrelated", image_id="sha256:def"),
    })

    found = image_cache.discover_pier_images()

    assert set(found) == {MAIN_REF}
    assert found[MAIN_REF].unique_size == 2_000_000_000


def test_record_trial_images_persists_only_valid_current_refs(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(image_cache.shutil, "which", lambda _name: "/usr/bin/docker")

    def fake_run(cmd, **kwargs):
        reference = cmd[-1]
        if reference == MAIN_REF:
            return subprocess.CompletedProcess(cmd, 0, json.dumps([_inspect()]), "")
        return subprocess.CompletedProcess(cmd, 1, "", "not found")

    monkeypatch.setattr(image_cache.subprocess, "run", fake_run)

    count = image_cache.record_trial_images(
        tmp_path, assignment_id="a1", task_id="some-task", trial_name=PROJECT,
    )

    assert count == 1
    records = image_cache.load(tmp_path)
    assert records[MAIN_REF]["image_id"] == "sha256:abc"
    assert records[MAIN_REF]["assignment_id"] == "a1"
    assert records[MAIN_REF]["task_id"] == "some-task"


def test_invalid_trial_name_never_queries_docker(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        image_cache, "_run_docker",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not query Docker")),
    )
    assert image_cache.record_trial_images(
        tmp_path, assignment_id="a1", task_id="t", trial_name="test-fixture",
    ) == 0


def test_cleanup_plan_protects_active_and_container_images_and_legacy_is_opt_in(
    tmp_path: Path, monkeypatch,
):
    active_ref = MAIN_REF
    safe_ref = "other-task__def5678-main:latest"
    container_ref = "third-task__ghi9012-main:latest"
    legacy_ref = "legacy-task__jkl3456-main:latest"
    images = {
        active_ref: _image(active_ref, image_id="sha256:a"),
        safe_ref: _image(safe_ref, project="other-task__def5678", image_id="sha256:b"),
        container_ref: _image(container_ref, project="third-task__ghi9012",
                              image_id="sha256:c", containers=1),
        legacy_ref: _image(legacy_ref, project="legacy-task__jkl3456", image_id="sha256:d"),
    }
    records = {
        active_ref: {"image_id": "sha256:a", "assignment_id": "active", "last_used_at": "1"},
        safe_ref: {"image_id": "sha256:b", "assignment_id": "settled", "last_used_at": "2"},
        container_ref: {"image_id": "sha256:c", "assignment_id": "settled", "last_used_at": "3"},
    }
    with image_cache._ledger_lock(tmp_path):
        image_cache._save_unlocked(tmp_path, records)
    monkeypatch.setattr(image_cache, "discover_pier_images", lambda: images)

    normal = image_cache.plan_cleanup(
        tmp_path, protected_assignment_ids={"active"}, include_legacy=False,
    )
    legacy = image_cache.plan_cleanup(
        tmp_path, protected_assignment_ids={"active"}, include_legacy=True,
    )

    assert [item.reference for item in normal.candidates] == [safe_ref]
    assert {item.reference for item in legacy.candidates} == {safe_ref, legacy_ref}
    assert normal.protected == 2
    assert normal.legacy_count == 1


def test_legacy_job_without_checkpoint_still_protects_active_assignment_image(
    tmp_path: Path, monkeypatch,
):
    assignment_id = "a" * 32
    trial_dir = tmp_path / "work" / "jobs" / f"a{assignment_id}" / PROJECT
    trial_dir.mkdir(parents=True)
    monkeypatch.setattr(
        image_cache, "discover_pier_images", lambda: {MAIN_REF: _image()},
    )

    plan = image_cache.plan_cleanup(
        tmp_path,
        protected_assignment_ids={assignment_id},
        include_legacy=True,
    )

    assert plan.candidates == []
    assert plan.protected == 1


def test_remove_prunes_only_matching_ledger_entry(tmp_path: Path, monkeypatch):
    image = _image()
    records = {
        MAIN_REF: {"image_id": image.image_id},
        PROXY_REF: {"image_id": "sha256:proxy"},
    }
    with image_cache._ledger_lock(tmp_path):
        image_cache._save_unlocked(tmp_path, records)
    monkeypatch.setattr(image_cache, "_remove_one", lambda _image: True)

    removed, reclaimed = image_cache.remove_images(tmp_path, [image])

    assert removed == 1 and reclaimed == image.unique_size
    assert set(image_cache.load(tmp_path)) == {PROXY_REF}


def test_remove_revalidates_id_and_never_uses_force(monkeypatch):
    image = _image()
    calls = []
    monkeypatch.setattr(image_cache, "_inventory_rows", lambda: {
        MAIN_REF: {"ID": image.image_id, "UniqueSize": "2GB", "Containers": "0"},
    })
    monkeypatch.setattr(image_cache, "_inspect", lambda _refs: {MAIN_REF: _inspect()})
    monkeypatch.setattr(
        image_cache, "_run_docker",
        lambda cmd, **_kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""),
    )

    assert image_cache._remove_one(image)
    assert calls == [["image", "rm", MAIN_REF]]
    assert "--force" not in calls[0] and "-f" not in calls[0]


def test_balanced_maintenance_removes_old_owned_images_over_limit(tmp_path: Path, monkeypatch):
    first = _image(size=8 * image_cache.GIB)
    second = _image("other-task__def5678-main:latest", project="other-task__def5678",
                    image_id="sha256:def", size=8 * image_cache.GIB)
    plan = image_cache.CleanupPlan(
        [first, second], {first.reference, second.reference}, 0,
        16 * image_cache.GIB, 16 * image_cache.GIB,
    )
    policy = image_cache.CachePolicy(
        "balanced", 10 * image_cache.GIB, 7 * image_cache.GIB,
        25 * image_cache.GIB, True,
    )
    monkeypatch.setattr(image_cache, "effective_policy", lambda *_a: policy)
    monkeypatch.setattr(image_cache, "plan_cleanup", lambda *_a, **_k: plan)
    monkeypatch.setattr(
        image_cache.shutil, "disk_usage",
        lambda _p: SimpleNamespace(total=500 * image_cache.GIB,
                                   used=400 * image_cache.GIB,
                                   free=100 * image_cache.GIB),
    )
    removed = []
    monkeypatch.setattr(
        image_cache, "remove_images",
        lambda _home, images: (removed.extend(images) or len(images),
                               sum(item.unique_size for item in images)),
    )

    result = image_cache.automatic_maintenance(
        tmp_path, {}, protected_assignment_ids=set(),
    )

    assert result.removed == 2
    assert [item.reference for item in removed] == [first.reference, second.reference]
    assert result.allow_new_claims


def test_metered_mode_never_auto_deletes_and_blocks_claims_under_disk_floor(
    tmp_path: Path, monkeypatch,
):
    image = _image()
    plan = image_cache.CleanupPlan(
        [image], {image.reference}, 0, image.unique_size, 60 * image_cache.GIB,
    )
    policy = image_cache.CachePolicy(
        "metered", 50 * image_cache.GIB, 40 * image_cache.GIB,
        25 * image_cache.GIB, False,
    )
    monkeypatch.setattr(image_cache, "effective_policy", lambda *_a: policy)
    monkeypatch.setattr(image_cache, "plan_cleanup", lambda *_a, **_k: plan)
    monkeypatch.setattr(
        image_cache.shutil, "disk_usage",
        lambda _p: SimpleNamespace(total=500 * image_cache.GIB,
                                   used=490 * image_cache.GIB,
                                   free=10 * image_cache.GIB),
    )
    monkeypatch.setattr(
        image_cache, "remove_images",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must preserve cache")),
    )

    result = image_cache.automatic_maintenance(
        tmp_path, {}, protected_assignment_ids=set(),
    )

    assert not result.allow_new_claims
    assert result.removed == 0
    assert "metered" in result.note


def test_docker_failure_still_blocks_new_claims_when_disk_is_low(
    tmp_path: Path, monkeypatch,
):
    policy = image_cache.CachePolicy(
        "balanced", 50 * image_cache.GIB, 37 * image_cache.GIB,
        25 * image_cache.GIB, True,
    )
    unavailable = image_cache.CleanupPlan(
        [], set(), 0, 0, 0, docker_available=False,
        note="Docker socket unavailable",
    )
    monkeypatch.setattr(image_cache, "effective_policy", lambda *_a: policy)
    monkeypatch.setattr(image_cache, "plan_cleanup", lambda *_a, **_k: unavailable)
    monkeypatch.setattr(
        image_cache.shutil, "disk_usage",
        lambda _p: SimpleNamespace(total=500 * image_cache.GIB,
                                   used=490 * image_cache.GIB,
                                   free=10 * image_cache.GIB),
    )

    result = image_cache.automatic_maintenance(
        tmp_path, {}, protected_assignment_ids=set(),
    )

    assert not result.allow_new_claims
    assert "no new task" in result.note


def test_server_state_failure_never_deletes_but_keeps_disk_claim_guard(
    monkeypatch, capsys,
):
    monkeypatch.setattr(
        runloop, "_active_by_id",
        lambda _client: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    monkeypatch.setattr(runloop, "_disk_allows_refill", lambda _cfg: False)
    monkeypatch.setattr(
        image_cache, "automatic_maintenance",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("must not delete without authoritative server state")
        ),
    )

    assert not runloop._maintain_image_cache(object(), {}, phase="before run")
    output = capsys.readouterr().out
    assert "no Docker image was deleted" in output
    assert "no new task will be claimed" in output


def test_default_policy_is_adaptive_and_bounded(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        image_cache.shutil, "disk_usage",
        lambda _p: SimpleNamespace(total=2_000 * image_cache.GIB,
                                   used=0, free=2_000 * image_cache.GIB),
    )
    large = image_cache.effective_policy(tmp_path, {})
    assert large.mode == "balanced"
    assert large.limit_bytes == 50 * image_cache.GIB
    assert large.target_bytes == int(37.5 * image_cache.GIB)

    monkeypatch.setattr(
        image_cache.shutil, "disk_usage",
        lambda _p: SimpleNamespace(total=128 * image_cache.GIB,
                                   used=0, free=128 * image_cache.GIB),
    )
    small = image_cache.effective_policy(tmp_path, {})
    assert small.limit_bytes == 20 * image_cache.GIB


def test_config_set_preserves_identity_token(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(local_config, "HOME", tmp_path)
    monkeypatch.setattr(local_config, "CONFIG_PATH", tmp_path / "config.json")
    local_config._save_config({"server": "https://deng.example", "token": "secret-token"})

    assert image_cache.cmd_config_set(argparse.Namespace(
        key="image-cache-mode", value="metered",
    )) == 0

    cfg = local_config._load_config()
    assert cfg["token"] == "secret-token"
    assert cfg["image_cache_mode"] == "metered"


def test_cleanup_docker_dry_run_never_removes_images(tmp_path: Path, monkeypatch, capsys):
    image = _image()
    plan = image_cache.CleanupPlan(
        [image], {image.reference}, 0, image.unique_size, image.unique_size,
    )
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(runloop, "_load_config", lambda: {})
    monkeypatch.setattr(runloop, "_client", lambda _cfg: object())
    monkeypatch.setattr(runloop, "_active_by_id", lambda _client: {})
    monkeypatch.setattr(image_cache, "plan_cleanup", lambda *_a, **_k: plan)
    monkeypatch.setattr(
        image_cache, "remove_images",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("dry-run must not delete")),
    )

    args = argparse.Namespace(
        dry_run=True, include_kept=False, docker=True,
        all_task_images=False, yes=True,
    )
    assert runloop.cmd_cleanup(args) == 0
    out = capsys.readouterr().out
    assert MAIN_REF in out and "would remove" in out


def test_cleanup_requires_explicit_docker_flag_for_legacy_sweep(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    args = argparse.Namespace(
        dry_run=True, include_kept=False, docker=False,
        all_task_images=True, yes=True,
    )
    assert runloop.cmd_cleanup(args) == 1
