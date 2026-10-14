
from ah_paths import ABSENT, describe_op, diff_paths, get_at, is_absent, normalize, plan_revert, pointer, shape
from fakes.jsonpatch import apply_json_patch, merge_patch

AFFINITY = {"spec": {"template": {"spec": {"affinity": {"nodeAffinity": {
    "requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [
        {"matchExpressions": [{"key": "gke-nodepool", "operator": "NotIn",
                               "values": ["infra-pool"]}]}]}}}}}}}


def ds():
    return {
        "apiVersion": "apps/v1", "kind": "DaemonSet",
        "metadata": {"name": "d", "namespace": "m", "resourceVersion": "1",
                     "annotations": {"a/b": "1"}},
        "spec": {"minReadySeconds": 5, "replicas": 3,
                 "template": {"spec": {
                     "containers": [{"name": "c", "image": "i:1"}],
                     "tolerations": [{"key": "x", "operator": "Exists"}]}}},
        "status": {"numberReady": 3},
    }


def roundtrip(before, patch):
    """Apply a merge patch, diff, then replay the inverse; return (paths, ops, restored)."""
    after = merge_patch(before, patch)
    paths = diff_paths(before, after)
    ops, conflicts = plan_revert(paths, after)
    assert conflicts == []
    return paths, ops, apply_json_patch(after, ops)


def test_adding_a_field_inverse_removes_it():
    before = ds()
    paths, ops, restored = roundtrip(before, AFFINITY)
    assert [p["path"] for p in paths] == [["spec", "template", "spec", "affinity"]]
    assert is_absent(paths[0]["before"])
    assert [o["op"] for o in ops] == ["test", "remove"]
    assert normalize(restored) == normalize(before)


def test_changing_a_value_inverse_restores_it():
    before = ds()
    paths, ops, restored = roundtrip(before, {"spec": {"replicas": 1}})
    assert paths == [{"path": ["spec", "replicas"], "before": 3, "held": 1}]
    assert [o["op"] for o in ops] == ["test", "replace"]
    assert normalize(restored) == normalize(before)


def test_replacing_an_array_restores_the_whole_array():
    before = ds()
    patch = {"spec": {"template": {"spec": {"tolerations": [{"key": "y"}, {"key": "z"}]}}}}
    paths, ops, restored = roundtrip(before, patch)
    assert [p["path"] for p in paths] == [["spec", "template", "spec", "tolerations"]]
    assert ops[1]["value"] == [{"key": "x", "operator": "Exists"}]
    assert normalize(restored) == normalize(before)


def test_removed_key_is_restored_with_add():
    before = ds()
    paths, ops, restored = roundtrip(before, {"spec": {"minReadySeconds": None}})
    assert is_absent(paths[0]["held"])
    assert [o["op"] for o in ops] == ["add"]          # absence cannot be `test`ed
    assert normalize(restored) == normalize(before)


def test_volatile_fields_are_ignored():
    before, after = ds(), ds()
    after["metadata"]["resourceVersion"] = "99"
    after["metadata"]["managedFields"] = [{"manager": "x"}]
    after["metadata"]["generation"] = 7
    after["status"] = {"numberReady": 0}
    assert diff_paths(before, after) == []


def test_change_by_someone_else_is_a_conflict_not_clobbered():
    before = ds()
    after = merge_patch(before, {"spec": {"replicas": 1}})
    paths = diff_paths(before, after)
    live = merge_patch(after, {"spec": {"replicas": 9}})   # someone else changed it
    ops, conflicts = plan_revert(paths, live)
    assert ops == []
    assert conflicts == [{"path": ["spec", "replicas"], "live_shape": "int"}]


def test_force_overrides_a_conflict_without_a_test_guard():
    before = ds()
    after = merge_patch(before, {"spec": {"replicas": 1}})
    paths = diff_paths(before, after)
    live = merge_patch(after, {"spec": {"replicas": 9}})
    ops, conflicts = plan_revert(paths, live, force=True)
    assert conflicts == []
    assert [o["op"] for o in ops] == ["replace"]
    assert apply_json_patch(live, ops)["spec"]["replicas"] == 3


def test_already_restored_is_a_noop():
    before = ds()
    after = merge_patch(before, {"spec": {"replicas": 1}})
    ops, conflicts = plan_revert(diff_paths(before, after), before)
    assert (ops, conflicts) == ([], [])


def test_pointer_escapes_slash_and_tilde():
    assert pointer(["metadata", "annotations", "a/b"]) == "/metadata/annotations/a~1b"
    assert pointer(["a~b"]) == "/a~0b"


def test_get_at_and_shape_never_expose_values():
    obj = ds()
    assert get_at(obj, ["spec", "replicas"]) == 3
    assert get_at(obj, ["spec", "nope"]) == ABSENT
    assert shape(ABSENT) == "absent"
    assert shape({"k": "secret"}) == "object(1 keys)"
    assert shape(["secret"]) == "array(1)"
    assert shape("secret") == "str"


def test_describe_op():
    assert describe_op({"before": ABSENT, "held": 1}) == "add"
    assert describe_op({"before": 1, "held": ABSENT}) == "remove"
    assert describe_op({"before": 1, "held": 2}) == "replace"
