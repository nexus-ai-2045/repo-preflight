import json

import ci_test_witness


def test_collection_fingerprint_keeps_duplicates_and_redacts_test_names(tmp_path):
    path = tmp_path / "results.xml"
    marker = "synthetic-private-test-identifier"
    path.write_text(
        '<testsuites><testsuite><testcase classname="suite" name="'
        + marker
        + '"/><testcase classname="suite" name="'
        + marker
        + '"><skipped/></testcase></testsuite></testsuites>'
    )
    facts = ci_test_witness.collection_facts(path)
    assert facts["status"] == "complete"
    assert facts["case_count"] == 2 and facts["skipped"] == 1
    assert marker not in json.dumps(facts)
    first = facts["case_sha256"]
    path.write_text('<testsuite><testcase classname="suite" name="other"/></testsuite>')
    assert ci_test_witness.collection_facts(path)["case_sha256"] != first


def test_collection_fingerprint_ignores_duration_and_execution_order(tmp_path):
    path = tmp_path / "results.xml"
    path.write_text(
        '<testsuite><testcase classname="s" name="a" time="1"/>'
        '<testcase classname="s" name="b" time="2"/></testsuite>'
    )
    first = ci_test_witness.collection_facts(path)["case_sha256"]
    path.write_text(
        '<testsuite><testcase classname="s" name="b" time="9"/>'
        '<testcase classname="s" name="a" time="8"/></testsuite>'
    )
    assert ci_test_witness.collection_facts(path)["case_sha256"] == first


def test_missing_or_malformed_collection_never_fabricates_success(tmp_path):
    path = tmp_path / "results.xml"
    assert ci_test_witness.collection_facts(path)["status"] == "not_created"
    path.write_text("<not-closed")
    assert ci_test_witness.collection_facts(path)["status"] == "unavailable"
