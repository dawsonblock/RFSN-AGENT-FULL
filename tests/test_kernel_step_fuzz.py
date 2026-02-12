import os

from hypothesis import (  # type: ignore[import-not-found]
    given,
    settings,
    strategies as st,
    HealthCheck,
)

from rfsn_kernel.kernel import HardKernel
from rfsn_kernel.state import Outcome
from services.tool_gateway.policy import validate_repo_path


def _ok_exec(_step):
    return Outcome(
        success=True,
        exit_code=0,
        logs="ok",
    )


@st.composite
def step_dicts(draw):
    step_type = draw(st.text(min_size=0, max_size=30))
    step = {
        "id": draw(st.text(min_size=0, max_size=20)),
        "type": step_type,
    }
    if draw(st.booleans()):
        step["pattern"] = draw(
            st.text(min_size=0, max_size=200),
        )
    if draw(st.booleans()):
        step["path"] = draw(st.text(min_size=0, max_size=120))
    if draw(st.booleans()):
        step["line_start"] = draw(
            st.integers(min_value=-5, max_value=1000),
        )
        step["line_end"] = draw(
            st.integers(min_value=-5, max_value=1000),
        )
    if draw(st.booleans()):
        step["patch"] = draw(
            st.text(min_size=0, max_size=1000),
        )
    if draw(st.booleans()):
        step["template_id"] = draw(
            st.text(min_size=0, max_size=30),
        )
    if draw(st.booleans()):
        step["timeout_s"] = draw(
            st.integers(min_value=-10, max_value=5000),
        )
    return step


@settings(
    max_examples=700,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(step_dicts())
def test_kernel_step_fuzz_never_crashes(step):
    ledger_path = "/tmp/kernel_fuzz_ledger.jsonl"
    try:
        os.remove(ledger_path)
    except FileNotFoundError:
        pass
    k = HardKernel(
        ledger_path=ledger_path,
        policy={
            "risk_max": 1.0,
            "success_min": 0.0,
            "loop_max": 1.0,
            "drift_max": 1.0,
            "rng_seed": 1,
        },
    )
    k.reset_for_run(run_id="fuzz")
    r = k.kernel_step(
        step,
        execute_fn=_ok_exec,
        run_id="fuzz",
    )
    assert r is not None
    assert r.phase in {
        "VALIDATE", "DECIDE", "COMMIT",
    }


PATH_STR = st.text(min_size=0, max_size=160)


@settings(
    max_examples=700,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(PATH_STR)
def test_validate_repo_path_blocks_traversal_like_inputs(path):
    ok = validate_repo_path(
        path,
        ["**"],
        ["**/.git/**"],
        repo_root_required=True,
        repo_root="/tmp/repo",
    )
    if (
        path.startswith("/")
        or path.startswith("~")
        or "\x00" in path
        or ".." in path.split("/")
    ):
        assert not ok
