import gpu_gate


def test_version_is_a_semver_string():
    assert gpu_gate.VERSION == "0.1.0"
