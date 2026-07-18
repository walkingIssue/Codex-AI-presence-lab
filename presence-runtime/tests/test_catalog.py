from __future__ import annotations

import json

import pytest

from presence_runtime.catalog import Catalog
from presence_runtime.errors import CatalogReferenceError, ConflictError


def test_catalog_revisions_profiles_and_presets_monotonically(
    tmp_path,
    higan_pack: dict,
) -> None:
    catalog = Catalog(tmp_path / "catalog")
    avatar_ref = catalog.register_avatar(higan_pack)
    profile_v1 = catalog.put_profile(
        {
            "profile_id": "higan-default",
            "voice_id": "af_heart",
            "avatar_ref": avatar_ref,
        }
    )
    profile_v2 = catalog.put_profile(
        {
            "profile_id": "higan-default",
            "voice_id": "bf_isabella",
            "avatar_ref": avatar_ref,
        },
        expected_revision=1,
    )
    preset = catalog.put_preset(
        {
            "preset_id": "plain",
            "compatible_model_fingerprints": [higan_pack["model_fingerprint"]],
            "semantic": {"slots": {"accessory.shoulders": [], "body.legs": []}},
        }
    )

    assert avatar_ref == "higan@3"
    assert profile_v1["revision"] == 1
    assert profile_v2["revision"] == 2
    assert catalog.get_profile("higan-default")["voice_id"] == "bf_isabella"
    assert catalog.get_profile("higan-default@1")["voice_id"] == "af_heart"
    assert preset["revision"] == 1
    with pytest.raises(ConflictError):
        catalog.put_profile(
            {"profile_id": "higan-default", "voice_id": "af_heart"},
            expected_revision=1,
        )


def test_avatar_assets_are_copied_into_immutable_fingerprint_directory(
    tmp_path,
    higan_pack: dict,
) -> None:
    source = tmp_path / "source-model"
    source.mkdir()
    (source / "Higan.model3.json").write_text("owned by user", encoding="utf-8")
    catalog = Catalog(tmp_path / "catalog")
    catalog.register_avatar(higan_pack, assets=source)
    source.joinpath("Higan.model3.json").write_text("changed later", encoding="utf-8")

    key = higan_pack["model_fingerprint"].removeprefix("sha256:")
    copied = catalog.root / "avatars" / key / "assets" / "Higan.model3.json"
    assert copied.read_text(encoding="utf-8") == "owned by user"
    assert (catalog.root / "avatars" / key / "renderer" / "index.html").is_file()


def test_existing_avatar_refreshes_only_derived_renderer_code(
    tmp_path,
    higan_pack: dict,
) -> None:
    source = tmp_path / "source-model"
    source.mkdir()
    (source / "Higan.model3.json").write_text("owned by user", encoding="utf-8")
    catalog = Catalog(tmp_path / "catalog")
    catalog.register_avatar(higan_pack, assets=source)
    key = higan_pack["model_fingerprint"].removeprefix("sha256:")
    target = catalog.root / "avatars" / key
    renderer = target / "renderer" / "renderer.js"
    renderer.write_text("stale derived code", encoding="utf-8")

    catalog.register_avatar(higan_pack)

    metadata = json.loads(
        (target / "renderer" / "catalog-renderer.json").read_text(encoding="utf-8")
    )
    assert renderer.read_text(encoding="utf-8") != "stale derived code"
    assert metadata["renderer_template_fingerprint"].startswith("sha256:")
    assert (target / "assets" / "Higan.model3.json").read_text(encoding="utf-8") == "owned by user"


def test_portable_export_refuses_overwrite_and_removal_checks_references(
    tmp_path,
) -> None:
    catalog = Catalog(tmp_path / "catalog")
    saved = catalog.put_profile(
        {"profile_id": "shared", "voice_id": "af_heart"}
    )
    output = tmp_path / "shared.json"
    catalog.export("profile", "shared", output)
    assert json.loads(output.read_text(encoding="utf-8"))["revision"] == 1
    with pytest.raises(ConflictError):
        catalog.export("profile", "shared", output)
    with pytest.raises(CatalogReferenceError):
        catalog.remove(
            "profile",
            f"shared@{saved['revision']}",
            references=["binding-1"],
        )
    catalog.remove("profile", "shared", references=["binding-1"], force=True)
    assert catalog.list_profiles() == []
