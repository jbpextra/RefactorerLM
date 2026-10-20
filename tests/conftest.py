from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """Base path for static test fixtures."""
    return Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def config_dir(fixtures_dir: Path) -> Path:
    return fixtures_dir / "config"


@pytest.fixture(scope="session")
def report_dir(fixtures_dir: Path) -> Path:
    return fixtures_dir / "reports"


@pytest.fixture(scope="session")
def cyclic_fixture_dir(fixtures_dir: Path) -> Path:
    return fixtures_dir / "cyclic_pkg"


@pytest.fixture(scope="session")
def redundant_fixture_dir(fixtures_dir: Path) -> Path:
    return fixtures_dir / "redundant_pkg"
