from pathlib import Path

from rk.config import KernelConfig


def test_config_resolves_relative_paths_from_injected_base(tmp_path: Path) -> None:
    cfg = KernelConfig.from_mapping(
        {
            "workspace_root": "state",
            "spec_root": "specification",
            "inbox_roots": ["incoming-a", "incoming-b"],
        },
        base=tmp_path,
    )

    assert cfg.workspace_root == (tmp_path / "state").resolve()
    assert cfg.db_path == (tmp_path / "state" / "rk.sqlite").resolve()
    assert cfg.schema_path == (tmp_path / "specification" / "schema.sql").resolve()
    assert cfg.inbox_roots == (
        (tmp_path / "incoming-a").resolve(),
        (tmp_path / "incoming-b").resolve(),
    )


def test_environment_overrides_are_explicit(tmp_path: Path) -> None:
    cfg = KernelConfig.load(
        environ={
            "RK_WORKSPACE_ROOT": "custom",
            "RK_BUSY_TIMEOUT_MS": "1200",
            "RK_INBOX_ROOTS": f"one{__import__('os').pathsep}two",
        },
        base=tmp_path,
    )

    assert cfg.busy_timeout_ms == 1200
    assert cfg.workspace_root == (tmp_path / "custom").resolve()
    assert len(cfg.inbox_roots) == 2
