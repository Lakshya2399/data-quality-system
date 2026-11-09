import json

import pytest

import generate_data as gd
from app.agents.model_agent import build_data_model
from app.data_io import load_csv
from app.profiling import profile_tables
from fakes import make_llm
from fixtures import enrichment


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("agent_data")
    gd.generate(d, seed=42)
    return d


@pytest.fixture(scope="session")
def sample_df(data_dir):
    return load_csv(data_dir / "sample_orders.csv")


@pytest.fixture(scope="session")
def regions_df(data_dir):
    return load_csv(data_dir / "valid_regions.csv")


@pytest.fixture(scope="session")
def profile(sample_df, regions_df):
    return profile_tables({"sample_orders": sample_df, "valid_regions": regions_df})


@pytest.fixture(scope="session")
def data_model(profile):
    return build_data_model(make_llm([json.dumps(enrichment(profile))]), profile)


# ---- Phase 4: isolated database + working directory per test ---------------------------------
from types import SimpleNamespace  # noqa: E402


@pytest.fixture
def env(tmp_path):
    return SimpleNamespace(db=tmp_path / "dq.sqlite", work=tmp_path / "work")
