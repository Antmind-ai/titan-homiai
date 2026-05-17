from __future__ import annotations

from pathlib import Path
import uuid

import pytest

from app.workers import tasks


@pytest.mark.asyncio
async def test_cleanup_user_data_task_removes_user_owned_r2_and_local_files(
    monkeypatch,
    tmp_path: Path,
):
    user_id = uuid.uuid4()
    user_id_str = str(user_id)

    user_dir = tmp_path / user_id_str
    (user_dir / "nested").mkdir(parents=True)
    local_file_1 = user_dir / "input-a.jpg"
    local_file_2 = user_dir / "nested" / "input-b.png"
    local_file_1.write_bytes(b"a")
    local_file_2.write_bytes(b"b")

    monkeypatch.setattr(tasks.settings, "design_upload_dir", str(tmp_path))

    async def _fake_list_objects_with_prefix(prefix: str) -> list[str]:
        if prefix == f"{user_id_str}/":
            return [
                f"{user_id_str}/input-a.jpg",
                f"{user_id_str}/input-b.png",
            ]
        if prefix == f"object-replace/{user_id_str}/":
            return [
                f"object-replace/{user_id_str}/mask-a.png",
                f"{user_id_str}/input-a.jpg",  # duplicated across prefixes
            ]
        return []

    deleted_keys: list[str] = []

    async def _fake_delete_object(key: str) -> bool:
        deleted_keys.append(key)
        return True

    monkeypatch.setattr(tasks, "list_objects_with_prefix_async", _fake_list_objects_with_prefix)
    monkeypatch.setattr(tasks, "delete_object_async", _fake_delete_object)

    result = await tasks.cleanup_user_data_task({}, user_id_str)

    assert result["status"] == "completed"
    assert set(result["deleted_r2_keys"]) == {
        f"{user_id_str}/input-a.jpg",
        f"{user_id_str}/input-b.png",
        f"object-replace/{user_id_str}/mask-a.png",
    }
    assert len(deleted_keys) == 3
    assert user_dir.exists() is False
    assert set(result["deleted_local_files"]) == {
        str(local_file_1),
        str(local_file_2),
    }


@pytest.mark.asyncio
async def test_cleanup_user_data_task_is_idempotent_when_no_artifacts(
    monkeypatch,
    tmp_path: Path,
):
    user_id = uuid.uuid4()

    monkeypatch.setattr(tasks.settings, "design_upload_dir", str(tmp_path))

    async def _fake_list_objects_with_prefix(_prefix: str) -> list[str]:
        return []

    async def _unexpected_delete_object(_key: str) -> bool:
        raise AssertionError("delete_object_async should not be called")

    monkeypatch.setattr(tasks, "list_objects_with_prefix_async", _fake_list_objects_with_prefix)
    monkeypatch.setattr(tasks, "delete_object_async", _unexpected_delete_object)

    result = await tasks.cleanup_user_data_task({}, str(user_id))

    assert result["status"] == "completed"
    assert result["deleted_r2_keys"] == []
    assert result["deleted_local_files"] == []
    assert result["deleted_count"] == 0


@pytest.mark.asyncio
async def test_cleanup_user_data_task_continues_when_single_r2_delete_fails(
    monkeypatch,
    tmp_path: Path,
):
    user_id = uuid.uuid4()
    user_id_str = str(user_id)

    monkeypatch.setattr(tasks.settings, "design_upload_dir", str(tmp_path))

    async def _fake_list_objects_with_prefix(prefix: str) -> list[str]:
        if prefix == f"{user_id_str}/":
            return [
                f"{user_id_str}/broken.jpg",
                f"{user_id_str}/ok.jpg",
            ]
        return []

    async def _fake_delete_object(key: str) -> bool:
        if key.endswith("broken.jpg"):
            raise RuntimeError("simulated delete failure")
        return True

    monkeypatch.setattr(tasks, "list_objects_with_prefix_async", _fake_list_objects_with_prefix)
    monkeypatch.setattr(tasks, "delete_object_async", _fake_delete_object)

    result = await tasks.cleanup_user_data_task({}, user_id_str)

    assert result["status"] == "completed"
    assert result["deleted_r2_keys"] == [f"{user_id_str}/ok.jpg"]
    assert result["deleted_count"] == 1
