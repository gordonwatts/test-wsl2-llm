import numpy as np
import pytest
import uproot
from test_report import sample_result

from test_wsl2_llm.models import CopiedBackFile, ValidatorConfig
from test_wsl2_llm.validation import apply_validators, validate_configuration


def run_check(tmp_path, **arguments):
    path = tmp_path / "result.root"
    with uproot.recreate(path) as root:
        root.mktree("events", {"pt": "float64", "eta": "float64"})
        root["events"].extend({"pt": np.array([1.]), "eta": np.array([2.])})
        root["hist"] = np.histogram(np.array([1., 2.]))
    result = sample_result()
    result.copied_back = [CopiedBackFile(
        source="result.root", destination=str(path), type="file", size=path.stat().st_size,
    )]
    spec = ValidatorConfig(name="root_tree", arguments={
        "file": "result.root", "tree": "events", **arguments,
    })
    return apply_validators(result, [spec]).validation[0]


@pytest.mark.parametrize("arguments,passed,diagnostic", [
    ({"must_have": ["pt"], "can_have": ["eta"], "no_other_leaves": True}, True, "match"),
    ({"must_have": ["missing"]}, False, "missing"),
    ({"cannot_have": ["eta"]}, False, "forbidden"),
    ({"must_have": ["pt"], "no_other_leaves": True}, False, "unexpected"),
    ({"tree": "absent"}, False, "does not exist"),
    ({"tree": "hist"}, False, "not a tree"),
    ({"file": "unreturned.root"}, False, "copied-back"),
])
def test_real_root_tree(tmp_path, arguments, passed, diagnostic):
    check = run_check(tmp_path, **arguments)
    assert check.passed is passed
    assert diagnostic in check.message


def test_conflicting_leaf_configuration():
    with pytest.raises(ValueError):
        validate_configuration([ValidatorConfig(name="root_tree", arguments={
            "file": "a.root", "tree": "events", "must_have": ["x"], "cannot_have": ["x"],
        })])


def test_invalid_local_root_file(tmp_path):
    path = tmp_path / "broken.root"
    path.write_text("not ROOT")
    result = sample_result()
    result.copied_back = [CopiedBackFile(
        source="broken.root", destination=str(path), type="file", size=8,
    )]
    check = apply_validators(result, [ValidatorConfig(name="root_tree", arguments={
        "file": "broken.root", "tree": "events",
    })]).validation[0]
    assert not check.passed
    assert "Cannot read" in check.message
