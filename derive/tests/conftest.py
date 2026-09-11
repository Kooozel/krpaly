"""Keep the suite's records out of the committed `derive/manifests/`.

Every stage writes a record beside its working manifest, and nearly every test
runs a stage. Session-scoped rather than per test, because the module-scoped
fixtures that run a stage once for many tests are set up before any
function-scoped fixture is: a per-test patch would arrive after they had
already written into the real tree. A record directory is named after its
`--out`, and most of the suite's are called `kraj-1`, so a test that asserts on
a record passes its own `record=` rather than reading one back from here.
"""

from __future__ import annotations

import pytest

from krpaly_derive import record


@pytest.fixture(scope="session", autouse=True)
def record_root(tmp_path_factory: pytest.TempPathFactory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(record, "RECORD_ROOT", tmp_path_factory.mktemp("manifests"))
        yield
