from hypothesis import (  # type: ignore[import-not-found]
    given,
    strategies as st,
    settings,
    HealthCheck,
)
from services.orchestrator.kernel import (
    Kernel,
)  # type: ignore[import-not-found]

k = Kernel(
    "shared/bundle_schema.json",
    "policies/tool_allowlist.yaml",
)
STEP_TYPE = st.text(
    st.characters(
        min_codepoint=32, max_codepoint=126,
    ),
    min_size=0, max_size=40,
)


@settings(
    max_examples=1500,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(STEP_TYPE)
def test_kernel_rejects_non_allowlisted_step_types(step_type):
    bundle = {
        "intent": "fuzz",
        "bundle_id": "b-fuzz0001",
        "steps": [{"id": "s1", "type": step_type}],
        "acceptance": {"tests_green": True, "no_new_failures": True},
    }
    errs = k.validate_bundle(bundle)
    allow = {
        "repo_search", "repo_read_range",
        "apply_patch", "ensure_deps",
        "run_tests",
    }
    if step_type not in allow:
        assert errs
