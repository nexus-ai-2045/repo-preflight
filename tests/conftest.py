"""公開前gateのprivate入力をテストごとに明示供給する。"""

import pytest
from identity_provider_fixture import configure_private_provider


@pytest.fixture
def private_identity_inputs(tmp_path, monkeypatch):
    return configure_private_provider(tmp_path, monkeypatch)
