"""公開generic正本をprivate workspaceなしで検査する合成ケース。"""

import importlib.util
from pathlib import Path
import pytest

SOURCE = Path(__file__).resolve().parents[1] / "scripts/identity_normalization.py"
SPEC = importlib.util.spec_from_file_location("identity_normalization_tested", SOURCE)
ENGINE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ENGINE)
DENIED = "fixture-private-identity@example.invalid"


@pytest.mark.parametrize(
    "text",
    [
        'CONTACT = ("fixture-private-" "identity@example.invalid")',
        'CONTACT = "fixture-private-identity\\x40example.invalid"',
        'CONTACT = "fixture-private-" + "identity@example.invalid"',
        '```python\nCONTACT = "fixture-private-" + "identity@example.invalid"\n```',
        '~~~python\nCONTACT = "fixture-private-" + "identity@example.invalid"\n~~~',
        '```python\nCONTACT = "fixture-private-" + "identity@example.invalid"',
    ],
)
def test_static_literal_and_fence_cases_reject_without_executing(text):
    result = ENGINE.scan_private_text(text, [DENIED])
    assert result["status"] == "fail"
    assert result["literal_match_count"] == 1


def test_reconstructed_path_has_value_free_evidence():
    result = ENGINE.scan_private_text(
        'PATH = "/" + "Users/operator-fixture/work"', [DENIED]
    )
    assert result == {
        "status": "fail",
        "literal_match_count": 0,
        "local_path_match": True,
        "reconstructed_path_match": True,
    }


def test_public_brand_and_synthetic_contact_pass():
    assert (
        ENGINE.scan_private_text("Public Brand / synthetic@example.invalid", [DENIED])[
            "status"
        ]
        == "pass"
    )


def test_target_text_is_never_executed(tmp_path):
    marker = tmp_path / "must-not-exist"
    text = "__import__('pathlib').Path(" + repr(str(marker)) + ").write_text('bad')"
    assert ENGINE.scan_private_text(text, [DENIED])["literal_match_count"] == 0
    assert not marker.exists()


def test_policy_input_is_explicit_nonempty_snapshot():
    assert ENGINE.parse_denylist_text(
        "# comment\nfixture-private\nfixture-private\n"
    ) == ["fixture-private"]
    with pytest.raises(ValueError, match="private_identity_policy_empty"):
        ENGINE.parse_denylist_text("# empty\n")


@pytest.mark.parametrize(
    "text",
    [
        '["fixture-", "private-", "operator"]',
        'const privateMarkers = [["fixture-", "private-", "operator"], ["other"]].map(parts => parts.join(""));',
        '"".join(["fixture-", "private-", "operator"])',
        '["fixture-", "private-", "operator"].join("")',
        '_join_chars("f i x t u r e - p r i v a t e - o p e r a t o r")',
    ],
)
def test_reconstructable_known_identity_array_and_spaced_literal(text):
    result = ENGINE.scan_private_text(text, ["fixture-private-operator"])
    assert result["status"] == "fail"
    assert result["literal_match_count"] == 1


def test_unrelated_arrays_and_spaced_brand_do_not_match_policy():
    result = ENGINE.scan_private_text(
        '["public-", "brand"] + _join_chars("p u b l i c")',
        ["fixture-private-operator"],
    )
    assert result["status"] == "pass"


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize(
    "expression", ['("al" + "pha")', '("al" "pha")', '"".join(["al", "pha"])']
)
def test_source_segments_keep_stdlib_unicode_and_newline_semantics(newline, expression):
    import ast

    source = "π = 1" + newline + "\fTARGET = " + expression + newline
    node = ast.parse(source).body[-1].value
    expected = ast.get_source_segment(source, node)
    assert ENGINE.static_string_views(source) == [("alpha", expected)]


def test_multiline_segment_keeps_unicode_byte_columns_and_raw_newlines():
    import ast

    source = 'π = "π"; TARGET = ("al" +\r\n    "pha")\r\n'
    node = ast.parse(source).body[-1].value
    assert ENGINE.static_string_views(source)[-1] == (
        "alpha",
        ast.get_source_segment(source, node),
    )


def test_many_literals_do_not_resplit_the_whole_module(monkeypatch):
    import ast

    source = "\n".join(f'VALUE_{index} = "token_{index}"' for index in range(2000))
    observed_lengths = []
    original = ast.get_source_segment

    def observe(fragment, node, **kwargs):
        observed_lengths.append(len(fragment))
        return original(fragment, node, **kwargs)

    monkeypatch.setattr(ast, "get_source_segment", observe)
    views = ENGINE.static_string_views(source)
    assert len(views) == 2000
    assert len(observed_lengths) == 2000
    assert max(observed_lengths) < 100
    assert sum(observed_lengths) <= len(source) + 2000


@pytest.mark.parametrize("flavour", ["macos", "linux"])
@pytest.mark.parametrize("username", ['"Alice Smith"', "'Alice Smith'", "山田", "Åsa"])
def test_shared_grammar_detects_quoted_and_unicode_reconstruction(flavour, username):
    from path_input_fixture import fixture_path, split_path_source

    source = split_path_source(fixture_path(flavour, username, "work"))
    result = ENGINE.scan_private_text(source, [DENIED])
    assert result["status"] == "fail"
    assert result["local_path_match"] is True
    assert result["reconstructed_path_match"] is True


@pytest.mark.parametrize("forward", [False, True])
@pytest.mark.parametrize("repeated", [False, True])
def test_windows_root_requires_username_but_preserves_repeated_separators(
    forward, repeated
):
    from path_input_fixture import fixture_root

    root = fixture_root("windows", forward=forward)
    if repeated:
        separator = "/" if forward else "\\"
        root = root.replace(separator, separator * 2)
    assert ENGINE.scan_private_text(root, [DENIED])["status"] == "pass"
    assert (
        ENGINE.scan_private_text(root + "fictional-user", [DENIED])["status"] == "fail"
    )


@pytest.mark.parametrize(
    "text", ["annual", "annotation", "Annette", "x_ann", "ann-x", "a.ann", "ann.name"]
)
def test_identity_requires_both_full_username_alphabet_boundaries(text):
    assert ENGINE.hit_patterns(text, ["ann"]) == []


@pytest.mark.parametrize(
    "text", ["ann", "ANN", "ann@example.invalid", "path/ann/work", "contact: ann;"]
)
def test_identity_preserves_complete_token_path_and_email_delimiters(text):
    assert ENGINE.hit_patterns(text, ["ann"]) == ["ann"]


@pytest.mark.parametrize("flavour", ["macos", "linux"])
def test_shared_grammar_keeps_relative_paths_out_and_uri_cli_paths_in(flavour):
    from path_input_fixture import fixture_path, fixture_relative

    folder = "Users" if flavour == "macos" else "home"
    assert not ENGINE.hit_local_absolute_path(
        fixture_relative("src", folder, "alice", "work")
    )
    absolute = fixture_path(flavour, "alice", "work")
    assert ENGINE.hit_local_absolute_path("file://" + absolute)
    assert ENGINE.hit_local_absolute_path("--include" + absolute)


@pytest.mark.parametrize(
    "source",
    [
        'const CONTACT = "fixture-private-" + "identity@example.invalid";',
        'const CONTACT: string = "fixture-private-" + "identity@example.invalid";',
        'function broken( {\nCONTACT = "fixture-private-" + "identity@example.invalid"',
        'if broken(:\nCONTACT = "fixture-private-" + "identity@example.invalid"',
        'const CONTACT = (\n "fixture-" +\n "private-" +\n "identity@example.invalid"\n);',
        '```typescript\nconst CONTACT: string = "fixture-private-" + "identity@example.invalid";\n```',
        'const CONTACT = "fixture-private-identity\\x40" + "example.invalid";',
    ],
)
def test_foreign_or_broken_source_recovers_static_concat(source):
    result = ENGINE.scan_private_text(source, [DENIED])
    assert result["status"] == "fail"
    assert result["literal_match_count"] == 1


def test_foreign_concat_uses_shared_path_grammar():
    from path_input_fixture import fixture_path

    path = fixture_path("macos", "fixture-person", "work")
    source = "const ROOT: string = " + repr(path[:2]) + " + " + repr(path[2:]) + ";"
    result = ENGINE.scan_private_text(source, [DENIED])
    assert result["local_path_match"] and result["reconstructed_path_match"]


@pytest.mark.parametrize(
    "source",
    [
        'const PUBLIC: string = "public-" + "brand";',
        '# 普通のMarkdown\n文章には "public" と "brand" を使う。',
        'const VALUE = "fixture-private-" + dynamic + "identity@example.invalid";',
        'const VALUE = f"{dynamic}" + "identity@example.invalid";',
    ],
)
def test_foreign_recovery_does_not_invent_dynamic_or_prose_values(source):
    assert ENGINE.scan_private_text(source, [DENIED])["status"] == "pass"


def test_foreign_recovery_never_executes_dynamic_expression(tmp_path):
    marker = tmp_path / "not-executed"
    source = (
        'const VALUE = "safe" + __import__("pathlib").Path('
        + repr(str(marker))
        + ').write_text("bad");'
    )
    assert ENGINE.scan_private_text(source, [DENIED])["status"] == "pass"
    assert not marker.exists()


def test_foreign_unparseable_literal_chain_is_incomplete_not_pass():
    with pytest.raises(ValueError, match="private_identity_normalization_incomplete"):
        ENGINE.scan_private_text(r'const VALUE = "broken\xZZ" + "literal";', [DENIED])


def test_foreign_chain_recovery_parses_only_each_bounded_expression(monkeypatch):
    import ast

    source = "\n".join(
        f'const VALUE_{index}: string = "public-" + "brand-{index}";'
        for index in range(2000)
    )
    lengths = []
    original = ast.parse

    def observe(fragment, *args, **kwargs):
        lengths.append(len(fragment))
        return original(fragment, *args, **kwargs)

    monkeypatch.setattr(ast, "parse", observe)
    views = ENGINE.static_string_views(source)
    assert len(views) == 2000
    assert lengths[0] == len(source)
    assert len(lengths) == 2001
    assert max(lengths[1:]) < 100
    assert sum(lengths[1:]) <= len(source)


@pytest.mark.parametrize(
    "source",
    [
        'const CONTACT = "fixture-private-" /* note */ + "identity@example.invalid";',
        'const CONTACT = "fixture-private-" + /* note */ "identity@example.invalid";',
        'const CONTACT = "fixture-private-" + // note\n "identity@example.invalid";',
        'if broken(:\nCONTACT = ("fixture-private-" + # note\n "identity@example.invalid")',
    ],
)
def test_comment_separated_static_chain_is_not_silently_unchecked(source):
    result = ENGINE.scan_private_text(source, [DENIED])
    assert result["status"] == "fail" and result["literal_match_count"] == 1


@pytest.mark.parametrize("marker", ["// literal", "/* literal */", "# literal"])
def test_recovery_preserves_comment_symbols_inside_literals_and_source_segment(marker):
    expression = (
        repr("https://public.invalid/" + marker) + " /* comment */ + " + repr(" brand")
    )
    source = "const VALUE: string = " + expression + ";"
    assert ENGINE.static_string_views(source) == [
        ("https://public.invalid/" + marker + " brand", expression)
    ]
    assert ENGINE.scan_private_text(source, [DENIED])["status"] == "pass"


def test_comment_recovery_keeps_escaped_quote_and_newline_segments():
    expression = (
        r'"https://public.invalid/\"/*literal*/"'
        + " + // real comment\r\n "
        + repr(" brand")
    )
    source = "const VALUE = " + expression + ";"
    views = ENGINE.static_string_views(source)
    assert views == [('https://public.invalid/"/*literal*/ brand', expression)]


def test_comment_does_not_bridge_a_dynamic_operand():
    source = 'const VALUE = "fixture-private-" + /* note */ dynamic + "identity@example.invalid";'
    assert ENGINE.scan_private_text(source, [DENIED])["status"] == "pass"


def test_unterminated_comment_in_selected_chain_is_incomplete_not_pass():
    with pytest.raises(ValueError, match="private_identity_normalization_incomplete"):
        ENGINE.scan_private_text('const VALUE = "public" + /* unfinished', [DENIED])


@pytest.mark.parametrize("tail", ["Projects/source.py", "notes", "123", "_cache"])
@pytest.mark.parametrize("drive", ["C", "d"])
def test_explicit_encoded_home_policy_is_a_prefix_not_an_alias(tail, drive):
    prefix = drive + "-Users-fixture-operator-"
    result = ENGINE.scan_private_text(prefix + tail, [prefix])
    assert result["status"] == "fail" and result["literal_match_count"] == 1


def test_encoded_home_prefix_cannot_use_username_span_exception():
    prefix = "C-Users-fixture-operator-"
    text = prefix + "Projects"
    assert ENGINE.hit_patterns(
        text, [prefix], ignored_username_spans=((0, len(text)),)
    ) == [prefix]


@pytest.mark.parametrize(
    "needle,text",
    [
        ("ann", "annual"),
        ("ann", "Annette"),
        ("zqhandle", "zqhandle123"),
        ("C-Users-fixture-operator", "C-Users-fixture-operatorProjects"),
        ("fake-C-Users-fixture-operator-", "fake-C-Users-fixture-operator-Projects"),
        ("C-Users--", "C-Users--Projects"),
    ],
)
def test_nonprefix_policy_keeps_whole_alias_boundaries(needle, text):
    assert ENGINE.hit_patterns(text, [needle]) == []


def test_encoded_home_prefix_policy_casefold_and_static_recovery():
    prefix = "C-Users-fixture-operator-"
    text = 'const VALUE = "c-users-fixture-" /* note */ + "operator-Projects";'
    result = ENGINE.scan_private_text(text, [prefix])
    assert result["status"] == "fail" and result["literal_match_count"] == 1


def test_ordinary_alias_still_honours_ignored_username_span():
    assert ENGINE.hit_patterns("ann", ["ann"], ignored_username_spans=((0, 3),)) == []
