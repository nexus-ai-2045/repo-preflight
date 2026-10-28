"""公開generic正本をprivate workspaceなしで検査する合成ケース。"""

import importlib.util
from pathlib import Path
import pytest
from synthetic_paths import synthetic_home_path

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
    encoded_lengths = []
    segment_lengths = []
    original_utf8 = ENGINE._source_line_utf8
    original_segment = ast.get_source_segment

    def observe_utf8(line):
        encoded_lengths.append(len(line))
        return original_utf8(line)

    def observe_segment(fragment, node, **kwargs):
        segment_lengths.append(len(fragment))
        return original_segment(fragment, node, **kwargs)

    monkeypatch.setattr(ENGINE, "_source_line_utf8", observe_utf8)
    monkeypatch.setattr(ast, "get_source_segment", observe_segment)
    views = ENGINE.static_string_views(source)
    assert len(views) == 2000
    assert [value for value, _original in views] == [
        f"token_{index}" for index in range(2000)
    ]
    # 1行nodeは全文をget_source_segmentへ渡さない。符号化は行ごとに一度。
    assert segment_lengths == []
    assert len(encoded_lengths) == 2000
    assert max(encoded_lengths) < 100
    assert sum(encoded_lengths) <= len(source) + 2000


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


def test_prose_skips_full_quoted_literal_scan(monkeypatch):
    calls = []
    original = ENGINE.re.finditer

    def wrapped(pattern, text, *args, **kwargs):
        calls.append(pattern if isinstance(pattern, str) else "compiled")
        return original(pattern, text, *args, **kwargs)

    monkeypatch.setattr(ENGINE.re, "finditer", wrapped)
    prose = "# 見出し\n" + ('本文には "public brand" と [link](url) がある。\n' * 2000)
    assert (
        ENGINE.scan_private_text(prose, ["fixture-private-operator"])["status"]
        == "pass"
    )
    assert calls == []


def test_prose_without_concatenation_skips_the_character_lexer(monkeypatch):
    called = False
    original = ENGINE._literal_plus_chains

    def wrapped(fragment):
        nonlocal called
        called = True
        return original(fragment)

    monkeypatch.setattr(ENGINE, "_literal_plus_chains", wrapped)
    prose = "# 見出し\n" + ("これは普通の文章です。\"public\" と 'brand'。\n" * 2000)
    assert ENGINE.static_string_views(prose) == []
    assert called is False
    assert (
        ENGINE.scan_private_text(prose + DENIED, [DENIED])["literal_match_count"] == 1
    )


def test_foreign_recovery_never_executes_dynamic_expression(tmp_path, monkeypatch):
    marker = tmp_path / "not-executed"
    monkeypatch.setenv("REPO_PREFLIGHT_DYNAMIC_EXEC_MARKER", str(marker))
    source = (
        'const VALUE = "safe" + __import__("pathlib").Path('
        '__import__("os").environ["REPO_PREFLIGHT_DYNAMIC_EXEC_MARKER"]'
        ').write_text("bad");'
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


@pytest.mark.parametrize(
    "source",
    [
        'const VALUE = ("fixture-" + "private-") + "identity@example.invalid";',
        'const VALUE = "fixture-" + ("private-" + "identity@example.invalid");',
        'const VALUE: string = (("fixture-" + "private-")) + ("identity@example.invalid");',
        'const VALUE = ("fixture-" /* inner */ + "private-") /* outer */ + "identity@example.invalid";',
        'const VALUE = ("fixture-" + // inner\r\n "private-") + /* outer */ ("identity@example.invalid");',
        'function value() { return ("fixture-" + "private-") + "identity@example.invalid"; }',
        'emit(("fixture-" + "private-") + "identity@example.invalid"); const END = true;',
    ],
)
def test_foreign_grouped_static_concat_is_recovered(source):
    result = ENGINE.scan_private_text(source, [DENIED])
    assert result["status"] == "fail"
    assert result["literal_match_count"] == 1


@pytest.mark.parametrize(
    "source",
    [
        'const VALUE = fn("fixture-" + "private-") + "identity@example.invalid";',
        'const VALUE = fn(("fixture-" + "private-")) + "identity@example.invalid";',
        'const VALUE = !("fixture-" + "private-") + "identity@example.invalid";',
        'const VALUE = +("fixture-" + "private-") + "identity@example.invalid";',
        'const VALUE = ("fixture-" + dynamic) + "private-identity@example.invalid";',
        'const VALUE = ("fixture-" + "private-").transform() + "identity@example.invalid";',
        'const VALUE = ("fixture-" + "private-")[0] + "identity@example.invalid";',
        'const VALUE = obj.return("fixture-" + "private-") + "identity@example.invalid";',
        'const VALUE = yield ("fixture-" + "private-") + "identity@example.invalid";',
        'const VALUE = dynamic * ("fixture-" + "private-") + "identity@example.invalid";',
    ],
)
def test_foreign_grouping_does_not_join_across_dynamic_boundaries(source):
    assert ENGINE.scan_private_text(source, [DENIED])["status"] == "pass"


def test_foreign_unclosed_static_group_is_incomplete_not_pass():
    with pytest.raises(ValueError, match="private_identity_normalization_incomplete"):
        ENGINE.scan_private_text('const VALUE = (("public" + "brand");', [DENIED])


def test_foreign_deep_group_failure_is_incomplete_not_pass():
    source = "const VALUE = " + "(" * 2000 + '"public" + "brand"' + ")" * 2000
    with pytest.raises(ValueError, match="private_identity_normalization_incomplete"):
        ENGINE.scan_private_text(source, [DENIED])


def test_foreign_grouped_chains_keep_bounded_ast_work(monkeypatch):
    import ast

    source = "\n".join(
        f'const V{index} = (("public" + "-")) + ("brand-{index}");'
        for index in range(2000)
    )
    lengths = []
    original = ast.parse

    def observe(fragment, *args, **kwargs):
        lengths.append(len(fragment))
        return original(fragment, *args, **kwargs)

    monkeypatch.setattr(ast, "parse", observe)
    assert len(ENGINE.static_string_views(source)) == 2000
    assert len(lengths) == 2001
    assert max(lengths[1:]) < 100
    assert sum(lengths[1:]) <= len(source)


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


def _one_line_records(count, body_length):
    records = []
    for index in range(count):
        records.append(
            '["rec-%04d","synthetic","%s","public","example.invalid"]'
            % (index, "x" * body_length)
        )
    return 'DATA = ["schema",' + ",".join(records) + "]"


def test_oracle_does_not_read_git_head():
    """履歴の無い実行でもoracleが自己比較やskipにならない。"""
    source = Path(__file__).read_text(encoding="utf-8")
    assert "git" + " show" not in source
    assert "HEAD" + ":" not in source
    assert "pytest" + ".skip" not in source
    assert "import " + "subprocess" not in source


def _stdlib_string_constants(source):
    """連結やjoinを解釈しない。平文の定数だけをstdlibの原文と比較する。"""
    import ast

    found = []

    def walk(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.append((node.value, ast.get_source_segment(source, node)))
            return
        for child in ast.iter_child_nodes(node):
            walk(child)

    walk(ast.parse(source))
    return found


@pytest.mark.parametrize(
    "source,value,segment",
    [
        ('πππ = "alpha"', "alpha", '"alpha"'),
        ('π = "α" + "β"', "αβ", '"α" + "β"'),
        ('VALUE = "al" "pha"', "alpha", '"al" "pha"'),
        ('VALUE = ("al" + "pha")', "alpha", '"al" + "pha"'),
        ('VALUE = ("al" +\n    "pha")', "alpha", '"al" +\n    "pha"'),
        ('VALUE = "".join(["al", "pha"])', "alpha", '"".join(["al", "pha"])'),
        ('VALUE = ["al", "pha"].join("")', "alpha", '["al", "pha"].join("")'),
        ('VALUE = r"a\\n" + "b"', "a\\nb", 'r"a\\n" + "b"'),
        ('VALUE = "\\u03c0"', "π", '"\\u03c0"'),
        ('VALUE = """alpha"""', "alpha", '"""alpha"""'),
        ('VALUE = """al\npha"""', "al\npha", '"""al\npha"""'),
        ('VALUE = "a\\\nb"', "ab", '"a\\\nb"'),
    ],
)
def test_static_expression_matches_fixed_segment_and_stdlib(source, value, segment):
    import ast

    node = ast.parse(source).body[0].value
    assert ast.get_source_segment(source, node) == segment
    assert ENGINE.static_string_views(source) == [(value, segment)]


def test_multiline_unicode_segments_match_stdlib_nodes():
    import ast

    source = 'π = "π"; TARGET = ("al" +\r\n    "pha")\r\n'
    tree = ast.parse(source)
    first = tree.body[0].value
    second = tree.body[1].value
    assert ENGINE.static_string_views(source) == [
        (first.value, ast.get_source_segment(source, first)),
        ("alpha", ast.get_source_segment(source, second)),
    ]
    assert ast.get_source_segment(source, second) == '"al" +\r\n    "pha"'


@pytest.mark.parametrize(
    "source,segment",
    [
        ("π = 1\n\fTARGET = " + '("al" + "pha")' + "\n", '"al" + "pha"'),
        ("π = 1\r\n\fTARGET = " + '("al" "pha")' + "\r\n", '"al" "pha"'),
        (
            "π = 1\rTARGET = " + '"".join(["al", "pha"])' + "\r",
            '"".join(["al", "pha"])',
        ),
    ],
)
def test_second_statement_segment_matches_stdlib(source, segment):
    import ast

    node = ast.parse(source).body[-1].value
    assert ast.get_source_segment(source, node) == segment
    assert ENGINE.static_string_views(source) == [("alpha", segment)]


def test_dynamic_parts_match_stdlib_constants_and_stay_unjoined():
    import ast

    source = 'VALUE = "fixture-private-" + dynamic + "identity@example.invalid"'
    binop = ast.parse(source).body[0].value
    nodes = [binop.left.left, binop.right]
    assert ENGINE.static_string_views(source) == [
        (node.value, ast.get_source_segment(source, node)) for node in nodes
    ]
    assert [node.value for node in nodes] == [
        "fixture-private-",
        "identity@example.invalid",
    ]


def test_fstring_parts_follow_runtime_stdlib_segments():
    import ast

    joined = 'VALUE = f"pre {dynamic} post"'
    parts = [
        node
        for node in ast.parse(joined).body[0].value.values
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert ENGINE.static_string_views(joined) == [
        (node.value, ast.get_source_segment(joined, node)) for node in parts
    ]
    assert [node.value for node in parts] == ["pre ", " post"]

    added = 'VALUE = f"{dynamic}" + "identity@example.invalid"'
    right = ast.parse(added).body[0].value.right
    assert ENGINE.static_string_views(added) == [
        (right.value, ast.get_source_segment(added, right))
    ]


@pytest.mark.parametrize(
    "source,views",
    [
        (
            '```python\nCONTACT = "fixture-private-" + "identity@example.invalid"\n```',
            [
                (
                    "fixture-private-identity@example.invalid",
                    '"fixture-private-" + "identity@example.invalid"',
                ),
                (
                    "fixture-private-identity@example.invalid",
                    '"fixture-private-" + "identity@example.invalid"',
                ),
            ],
        ),
        (
            '~~~python\nπ = "α" + "β"\n~~~',
            [("αβ", '"α" + "β"'), ("αβ", '"α" + "β"')],
        ),
        (
            '```python\nCONTACT = "fixture-private-" + "identity@example.invalid"',
            [
                (
                    "fixture-private-identity@example.invalid",
                    '"fixture-private-" + "identity@example.invalid"',
                ),
                (
                    "fixture-private-identity@example.invalid",
                    '"fixture-private-" + "identity@example.invalid"',
                ),
            ],
        ),
        (
            'const VALUE = ("fixture-" + "private-") + "identity@example.invalid";',
            [
                (
                    "fixture-private-identity@example.invalid",
                    '("fixture-" + "private-") + "identity@example.invalid"',
                )
            ],
        ),
        (
            'const CONTACT = "fixture-private-" /* note */ + "identity@example.invalid";',
            [
                (
                    "fixture-private-identity@example.invalid",
                    '"fixture-private-" /* note */ + "identity@example.invalid"',
                )
            ],
        ),
        (
            'if broken(:\nCONTACT = ("fixture-private-" + # note\n "identity@example.invalid")',
            [
                (
                    "fixture-private-identity@example.invalid",
                    '("fixture-private-" + # note\n "identity@example.invalid")',
                )
            ],
        ),
    ],
)
def test_fence_and_foreign_views_match_fixed_segments(source, views):
    assert ENGINE.static_string_views(source) == views


def test_one_line_records_match_stdlib_constant_segments():
    source = _one_line_records(40, 24)
    assert "\n" not in source
    views = ENGINE.static_string_views(source)
    assert views == _stdlib_string_constants(source)
    assert views[0] == ("schema", '"schema"')
    assert len(views) == 1 + 5 * 40


FAIL_EXACT = {
    "status": "fail",
    "literal_match_count": 1,
    "local_path_match": False,
    "reconstructed_path_match": False,
}
PASS_EXACT = {
    "status": "pass",
    "literal_match_count": 0,
    "local_path_match": False,
    "reconstructed_path_match": False,
}
PATH_EXACT = {
    "status": "fail",
    "literal_match_count": 0,
    "local_path_match": True,
    "reconstructed_path_match": True,
}
SCAN_EXPECTATIONS = [
    ('CONTACT = "fixture-private-" + "identity@example.invalid"', FAIL_EXACT),
    (
        '```python\nCONTACT = "fixture-private-" + "identity@example.invalid"\n```',
        FAIL_EXACT,
    ),
    (
        'const VALUE = ("fixture-" + "private-") + "identity@example.invalid";',
        FAIL_EXACT,
    ),
    (
        'const CONTACT = "fixture-private-" /* note */ + "identity@example.invalid";',
        FAIL_EXACT,
    ),
    ('PATH = "/" + "Users/operator-fixture/work"', PATH_EXACT),
    (
        'VALUE = "fixture-private-" + dynamic + "identity@example.invalid"',
        PASS_EXACT,
    ),
    ('VALUE = f"{dynamic}" + "identity@example.invalid"', PASS_EXACT),
    ('const PUBLIC: string = "public-" + "brand";', PASS_EXACT),
    ("Public Brand / synthetic@example.invalid", PASS_EXACT),
]


def test_detection_oracle_includes_positive_and_negative_examples():
    statuses = {expected["status"] for _, expected in SCAN_EXPECTATIONS}
    assert statuses == {"fail", "pass"}
    assert any(item["literal_match_count"] == 1 for _, item in SCAN_EXPECTATIONS)
    assert any(item["reconstructed_path_match"] for _, item in SCAN_EXPECTATIONS)
    assert any(
        item["status"] == "pass"
        and item["literal_match_count"] == 0
        and item["local_path_match"] is False
        for _, item in SCAN_EXPECTATIONS
    )


@pytest.mark.parametrize("text,expected", SCAN_EXPECTATIONS)
def test_scan_positive_and_negative_detection(text, expected):
    assert ENGINE.scan_private_text(text, [DENIED]) == expected


def test_non_ascii_single_line_segment_matches_stdlib_byte_offsets():
    import ast

    source = ("π" * 40) + ' = "α" + "β"'
    node = ast.parse(source).body[0].value
    assert ENGINE.static_string_views(source) == [
        ("αβ", ast.get_source_segment(source, node))
    ]


def test_dynamic_expression_stays_unjoined():
    source = 'VALUE = "fixture-private-" + dynamic + "identity@example.invalid"'
    values = [value for value, _original in ENGINE.static_string_views(source)]
    assert "fixture-private-identity@example.invalid" not in values
    assert values == ["fixture-private-", "identity@example.invalid"]


def test_parenthesized_concat_array_and_join_keep_static_values():
    assert ENGINE.static_string_views('("al" + "pha")')[0][0] == "alpha"
    assert ENGINE.static_string_views('"".join(["al", "pha"])')[0][0] == "alpha"
    assert ENGINE.static_string_views('["al", "pha"].join("")')[0][0] == "alpha"


def test_multiline_segment_does_not_rescan_unrelated_lines(monkeypatch):
    import ast

    prefix = "\n".join(f"OTHER_{index} = {index}" for index in range(500))
    source = prefix + '\nTARGET = ("al" +\n    "pha")\n'
    lengths = []
    original = ast.get_source_segment

    def observe(fragment, node, **kwargs):
        lengths.append(len(fragment))
        return original(fragment, node, **kwargs)

    monkeypatch.setattr(ast, "get_source_segment", observe)
    views = ENGINE.static_string_views(source)
    assert views[-1][0] == "alpha"
    assert lengths
    assert max(lengths) < 80
    assert sum(lengths) < len(source) / 10


def test_one_line_encode_work_does_not_grow_with_view_count(monkeypatch):
    """合否は処理した文字数。wall timeは使わない。"""
    import ast

    measured = []
    original_utf8 = ENGINE._source_line_utf8
    original_segment = ast.get_source_segment

    def observe_utf8(line):
        measured.append(len(line))
        return original_utf8(line)

    def observe_segment(fragment, node, **kwargs):
        measured.append(len(fragment))
        return original_segment(fragment, node, **kwargs)

    monkeypatch.setattr(ENGINE, "_source_line_utf8", observe_utf8)
    monkeypatch.setattr(ast, "get_source_segment", observe_segment)

    def work(count):
        measured.clear()
        source = _one_line_records(count, 48)
        views = ENGINE.static_string_views(source)
        return sum(measured), len(views), len(source)

    small_work, small_views, small_source = work(100)
    large_work, large_views, large_source = work(400)
    assert small_views == 1 + 5 * 100
    assert large_views == 1 + 5 * 400
    assert small_work <= small_source
    assert large_work <= large_source
    assert large_work < large_views * large_source / 10
    assert large_source > small_source * 3
    assert large_work < small_work * 6


@pytest.mark.parametrize(
    "text",
    [
        'const VALUE: string = "f i x \\x74 u r e - p r i v a t e - o p e r a t o r";',
        'const VALUE: string = "f i x \\u0074 u r e - p r i v a t e - o p e r a t o r";',
        'const VALUE: string = "f i x \\164 u r e - p r i v a t e - o p e r a t o r";',
        'const VALUE: string = "f i x \\N{LATIN SMALL LETTER T} u r e - p r i v a t e - o p e r a t o r";',
        'const VALUE: string = "f\\x20i\\x20x\\x20t\\x20u\\x20r\\x20e\\x20-\\x20p\\x20r\\x20i\\x20v\\x20a\\x20t\\x20e\\x20-\\x20o\\x20p\\x20e\\x20r\\x20a\\x20t\\x20o\\x20r";',
    ],
)
def test_spaced_literal_with_escaped_characters_is_reconstructed(text):
    # 一文字区切りの引用を探す前段の絞り込みが、後段の復元で読めるエスケープを取りこぼすと
    # 符号化した非公開の識別子が素通りする (PR #15 のレビュー指摘)。
    result = ENGINE.scan_private_text(text, ["fixture-private-operator"])
    assert result["status"] == "fail"


@pytest.mark.parametrize(
    "text",
    [
        'const VALUE: string = "' + chr(0xA0).join("fixture-private-operator") + '";',
        'const VALUE: string = "' + chr(0x2003).join("fixture-private-operator") + '";',
        'const VALUE: string = "' + chr(0x3000).join("fixture-private-operator") + '";',
    ],
)
def test_spaced_literal_with_unicode_whitespace_is_reconstructed(text):
    # 後段の split() は Unicode の空白でも区切るので、前段の絞り込みも同じ空白を受ける
    # (PR #15 のレビュー指摘)。
    result = ENGINE.scan_private_text(text, ["fixture-private-operator"])
    assert result["status"] == "fail"


@pytest.mark.parametrize(
    ("fragment", "expected"),
    [
        ('x = "fixture-private-" + "operator"', True),
        ('x = ("fixture-private-")\n  /* c */ + "operator"', True),
        ('C++ の記事で "引用" と書き、別の段落で a + b と書く', False),
        ('"".join(parts) と書く記事', False),
        ("+ だけで引用の無い文章", False),
    ],
)
def test_recovery_runs_only_for_a_literal_followed_by_plus(fragment, expected):
    assert ENGINE._needs_foreign_literal_recovery(fragment) is expected


@pytest.mark.parametrize(
    ("text", "pattern"),
    [
        ('const x: string = "o \' c o n n o r";', "o'connor"),
        ("const x: string = 'o \" c o n n o r';", 'o"connor'),
        ("const x: string = \"a ' b ' c ' d\";", "a'b'c'd"),
    ],
)
def test_spaced_literal_with_the_opposite_quote_is_reconstructed(text, pattern):
    # 反対側の引用符も 1 文字の欠片になる (PR #15 のレビュー指摘)。
    result = ENGINE.scan_private_text(text, [pattern])
    assert result["status"] == "fail"


def test_spaced_literal_filter_never_misses_what_reconstruction_finds():
    # 前段の絞り込みは後段の復元の必要条件でなければならない。手書きの絞り込みが
    # 後段とずれて穴があく型を、ランダムに組んだ引用で機械的に見張る (ラチェット)。
    import ast
    import random

    rng = random.Random(15)
    pieces = [
        "a",
        "b",
        "x",
        "-",
        "@",
        " ",
        "\t",
        chr(0xA0),
        chr(0x2003),
        chr(0x3000),
        "'",
        '\\"',
        "\\'",
        "\\x74",
        "\\x20",
        "\\164",
        "\\N{SPACE}",
        "\\t",
        "\\U00000074",
        "\\\\",
        "\\\n",
        chr(0x0B),
        chr(0x1C),
        chr(0x1F),
        chr(0x85),
        '"',
    ]
    for _ in range(3000):
        body = "".join(rng.choice(pieces) for _ in range(rng.randint(4, 14)))
        quote = rng.choice(['"', "'"])
        prefix = rng.choice(["", "r", "u", "b"])
        text = "const x = " + prefix + quote + body + quote + ";"
        expected = False
        for match in ENGINE.re.finditer(ENGINE._QUOTED_LITERAL, text):
            try:
                value = ast.literal_eval(match.group())
            except (SyntaxError, ValueError):
                continue
            parts = value.split() if isinstance(value, str) else []
            if len(parts) >= 4 and all(len(part) == 1 for part in parts):
                expected = True
        if expected:
            assert any(
                len(ENGINE._SPACED_SEPARATOR_HINT.findall(m.group())) >= 3
                for m in ENGINE.re.finditer(ENGINE._QUOTED_LITERAL, text)
            ), text


def test_recovery_gate_stays_linear_with_unclosed_block_comments():
    # 閉じない /* が多い文章で、判定がどの位置からも終端まで探し直すと二乗時間になる
    # (PR #15 のレビュー指摘)。
    import time

    fragment = "}} " + ('"a" /* x\n' * 20000) + " + 1"
    started = time.perf_counter()
    ENGINE._needs_foreign_literal_recovery(fragment)
    assert time.perf_counter() - started < 2.0


def test_recovery_gate_never_misses_a_chain_the_lexer_recovers():
    # 回復解析の入口は、_literal_plus_chains が連結を戻す形の必要条件でなければならない。
    # 乱数の種を固定したトークン列で、戻る連結があるのに入口が偽の入力が無いことを見張る。
    import random

    rng = random.Random(1523)
    tokens = [
        '"a"',
        "'b'",
        'r"c"',
        "u'd'",
        "+",
        "(",
        ")",
        " ",
        "\n",
        "/* x */",
        "/* y",
        "// z\n",
        "# w\n",
        "}",
        ";",
        "x",
        ".join",
        "[",
        "]",
        ",",
        '"e\\"f"',
    ]
    for _ in range(4000):
        fragment = "".join(rng.choice(tokens) for _ in range(rng.randint(3, 18)))
        try:
            chains = ENGINE._literal_plus_chains(fragment)
        except ValueError:
            chains = ["incomplete"]
        if chains:
            assert ENGINE._needs_foreign_literal_recovery(fragment), fragment


def test_recovery_tokens_preserve_existing_lexer_on_malformed_quotes():
    import random

    rng = random.Random(20261024)
    pieces = [
        '"',
        "'",
        "\\",
        "\n",
        "\r",
        "r",
        "u",
        "B",
        "x",
        "$",
        " ",
        "+",
        "(",
        ")",
        "/*",
        "*/",
        "//",
        "#",
        "é",
    ]
    for _ in range(4000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 50)))
        expected = [
            (m.lastgroup, m.span(), m.group())
            for m in ENGINE._RECOVERY_TOKEN.finditer(text)
        ]
        actual = [
            (m.lastgroup, m.span(), m.group()) for m in ENGINE._recovery_tokens(text)
        ]
        assert actual == expected


def test_quote_only_iteration_preserves_literals_inside_comments():
    import random

    rng = random.Random(2410)
    pieces = ['"', "'", "\\", "\n", "\r", "r", "x", " ", "/*", "*/", "//", "#"]
    for _ in range(4000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 50)))
        expected = [
            (m.span(), m.group())
            for m in ENGINE.re.finditer(ENGINE._QUOTED_LITERAL, text)
        ]
        actual = [(m.span(), m.group()) for m in ENGINE._quoted_literals(text)]
        assert actual == expected


@pytest.mark.parametrize("group", ["(group)", "[slug]"])
@pytest.mark.parametrize("filename", ["page.tsx", "daily-quests.tsx"])
def test_repository_name_context_distinguishes_relative_route_groups(group, filename):
    from path_input_fixture import fixture_relative

    name = fixture_relative("web", "app", group, "home", filename)
    assert ENGINE.scan_private_text(name, [DENIED])["local_path_match"]
    assert ENGINE.scan_private_repository_name(name, [DENIED])["status"] == "pass"


@pytest.mark.parametrize(
    "prefix", ["", "prefix ", "artifacts/file://", "prefix(", "--include"]
)
def test_repository_name_context_keeps_absolute_and_embedded_host_paths(prefix):
    from path_input_fixture import fixture_path

    name = prefix + fixture_path("linux", "fixture-host", "work")
    assert ENGINE.scan_private_repository_name(name, [DENIED])["status"] == "fail"


def test_repository_name_context_keeps_embedded_windows_host_path():
    from path_input_fixture import fixture_path

    name = "artifacts/" + fixture_path("windows", "fixture-host", "work")
    assert ENGINE.scan_private_repository_name(name, [DENIED])["status"] == "fail"


def test_repository_name_context_keeps_identity_literals():
    from path_input_fixture import fixture_relative

    name = fixture_relative("web", "app", "(group)", "home", DENIED)
    result = ENGINE.scan_private_repository_name(name, [DENIED])
    assert result["status"] == "fail" and result["literal_match_count"] == 1


def regex_source(expression, *, before="", after=""):
    return "import re\n" + before + "PATTERN = re.compile(" + expression + ")\n" + after


def regex_home_root():
    return "/".join(("", "Users", ""))


@pytest.mark.parametrize("separator", ["/", "\\"])
def test_compiled_generic_username_class_has_source_role(separator):
    root = (
        ("C:" + separator + "Users" + separator)
        if separator == "\\"
        else regex_home_root()
    )
    # Backslashes represent regex escapes, not a concrete host account.
    pattern = root.replace("\\", "\\\\") + (
        r"[^\\\s]+" if separator == "\\" else r"[^/\s]+"
    )
    source = regex_source(repr(pattern))
    assert ENGINE.scan_private_text(source, [DENIED])["status"] == "pass"
    assert ENGINE.static_string_views(source)[0][0] == pattern


def test_compiled_static_join_of_original_operating_regex_templates_passes():
    roots = [
        "C:" + "\\\\" + "Users" + "\\\\",
        "C:" + regex_home_root(),
        regex_home_root(),
    ]
    patterns = [
        root + (r"[^\\\s]+" if index == 0 else r"[^/\s]+")
        for index, root in enumerate(roots)
    ]
    expression = repr("(") + ' + "|".join(' + repr(patterns) + ") + " + repr(")")
    assert (
        ENGINE.scan_private_text(regex_source(expression), [DENIED])["status"] == "pass"
    )


def test_note_compiled_username_class_with_placeholder_lookahead_passes():
    pattern = regex_home_root() + r"(?!USER\b|<)[A-Za-z0-9._-]+/"
    assert (
        ENGINE.scan_private_text(regex_source(repr(pattern)), [DENIED])["status"]
        == "pass"
    )


@pytest.mark.parametrize("placement", ["branch", "source", "comment"])
def test_regex_role_keeps_separate_concrete_host_paths(placement):
    from path_input_fixture import fixture_path

    path = fixture_path("macos", "regex-fixture-host", "work")
    pattern = regex_home_root() + r"[^/\s]+"
    source = regex_source(repr(pattern))
    if placement == "branch":
        source = regex_source(repr(pattern + "|" + path))
    elif placement == "source":
        source += "ACTUAL = " + repr(path)
    else:
        source = regex_source("(" + repr(pattern) + "\n# " + path + "\n)")
    assert ENGINE.scan_private_text(source, [DENIED])["status"] == "fail"


@pytest.mark.parametrize("suffix", ["", "[0-9]+"])
def test_compiled_regex_concrete_username_is_still_detected(suffix):
    from path_input_fixture import fixture_path

    pattern = fixture_path("macos", "regex-fixture-host", "work") + suffix
    assert (
        ENGINE.scan_private_text(regex_source(repr(pattern)), [DENIED])["status"]
        == "fail"
    )


def test_regex_role_keeps_all_original_and_reconstructed_private_literals():
    pattern = regex_home_root() + r"[^/\s]+"
    expression = (
        repr(pattern + "|") + " + " + repr(DENIED[:12]) + " + " + repr(DENIED[12:])
    )
    result = ENGINE.scan_private_text(regex_source(expression), [DENIED])
    assert result["status"] == "fail" and result["literal_match_count"] == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "no-import",
        "other-call",
        "name-shadow",
        "argument-shadow",
        "attribute-replacement",
        "dictionary-replacement",
        "setattr",
        "alias",
        "dynamic",
        "invalid-regex",
        "invalid-source",
    ],
)
def test_ambiguous_or_nonregex_context_preserves_original_path_checks(mutation):
    pattern = regex_home_root() + r"[^/\s]+"
    source = regex_source(repr(pattern))
    if mutation == "no-import":
        source = source.replace("import re\n", "")
    elif mutation == "other-call":
        source = source.replace("re.compile", "other.compile")
    elif mutation == "name-shadow":
        source = regex_source(repr(pattern), before="re = other\n")
    elif mutation == "argument-shadow":
        source = (
            "import re\ndef function(re):\n    return re.compile("
            + repr(pattern)
            + ")\n"
        )
    elif mutation == "attribute-replacement":
        source = regex_source(repr(pattern), before="re.compile = other\n")
    elif mutation == "dictionary-replacement":
        source = regex_source(repr(pattern), before='re.__dict__["compile"] = other\n')
    elif mutation == "setattr":
        source = regex_source(repr(pattern), before='setattr(re, "compile", other)\n')
    elif mutation == "alias":
        source = regex_source(
            repr(pattern), before="alias = re\nalias.compile = other\n"
        )
    elif mutation == "dynamic":
        source = regex_source(repr(pattern) + " + unknown")
    elif mutation == "invalid-regex":
        source = regex_source(repr(pattern + "("))
    else:
        source += "if (\n"
    assert ENGINE.scan_private_text(source, [DENIED])["status"] == "fail"


@pytest.mark.parametrize("use_private_branch", [False, True])
def test_regex_join_evaluates_static_expression_items_without_changing_public_views(
    use_private_branch,
):
    from path_input_fixture import fixture_path

    expression_items = [
        repr("C:")
        + " + "
        + repr("\\\\")
        + " + "
        + repr("Users")
        + " + "
        + repr(r"\\[^\\\s]+"),
        repr("/") + " + " + repr("Users") + " + " + repr(r"/[^/\s]+"),
    ]
    if use_private_branch:
        expression_items.append(
            repr(fixture_path("macos", "regex-fixture-host", "work"))
        )
    expression = (
        repr("(") + ' + "|".join([' + ", ".join(expression_items) + "]) + " + repr(")")
    )
    source = regex_source(expression)
    before = ENGINE.static_string_views(source)
    result = ENGINE.scan_private_text(source, [DENIED])
    assert result["status"] == ("fail" if use_private_branch else "pass")
    assert ENGINE.static_string_views(source) == before


def test_regex_dunder_attribute_mutation_preserves_original_path_checks():
    pattern = regex_home_root() + r"[^/\s]+"
    source = regex_source(repr(pattern), before='re.__setattr__("compile", other)\n')
    assert ENGINE.scan_private_text(source, [DENIED])["status"] == "fail"


@pytest.mark.parametrize(
    "prefix",
    [
        "from unknown import *\n",
        "import re as other_re\nother_re.compile = other\n",
        'globals()["re"] = other\n',
    ],
)
def test_regex_ambiguous_module_bindings_are_not_a_template_role(prefix):
    pattern = regex_home_root() + r"[^/\s]+"
    assert (
        ENGINE.scan_private_text(regex_source(repr(pattern), before=prefix), [DENIED])[
            "status"
        ]
        == "fail"
    )


@pytest.mark.parametrize("suffix", ["actual-name", "(?:actual-name)"])
def test_regex_generic_class_followed_by_username_literal_is_not_exempted(suffix):
    pattern = regex_home_root() + r"[^/\s]+" + suffix + "/work"
    assert (
        ENGINE.scan_private_text(regex_source(repr(pattern)), [DENIED])["status"]
        == "fail"
    )


def test_regex_source_spans_do_not_hide_identical_nonregex_literal():
    pattern = regex_home_root() + r"[^/\s]+"
    source = regex_source(repr(pattern), after="TEXT = " + repr(pattern))
    assert ENGINE.scan_private_text(source, [DENIED])["status"] == "fail"


@pytest.mark.parametrize("line_break", ["\n", "\r\n", "\r"])
def test_regex_source_offsets_preserve_non_ascii_prefix_and_actual_comment(line_break):
    from path_input_fixture import fixture_path

    pattern = regex_home_root() + r"[^/\s]+"
    path = fixture_path("macos", "regex-fixture-host", "work")
    source = regex_source(
        repr(pattern), before="説明 = '本文'; ", after="# " + path + "\n"
    )
    source = source.replace("\n", line_break)
    assert ENGINE.scan_private_text(source, [DENIED])["status"] == "fail"
    clean = regex_source(repr(pattern), before="説明 = '本文'; ").replace(
        "\n", line_break
    )
    assert ENGINE.scan_private_text(clean, [DENIED])["status"] == "pass"


def test_regex_source_role_preserves_nonregex_synthetic_home_fixture():
    from path_input_fixture import fixture_path

    source = "HOME = " + repr(fixture_path("macos", "fixture-host", "work"))
    assert ENGINE.scan_private_text(source, [DENIED])["status"] == "fail"


@pytest.mark.parametrize(
    "kind", ["private-literal", "encoded-private-literal", "concrete-path"]
)
def test_complete_regex_expression_keeps_detections_across_join_item_boundaries(kind):
    pattern = regex_home_root() + r"[^/\s]+"
    items = [repr(pattern) + " + " + repr("|")]
    if kind == "concrete-path":
        items.extend([repr(regex_home_root()), repr("fixture-host/work")])
    elif kind == "encoded-private-literal":
        encoded = DENIED.replace("@", "%40")
        middle = encoded.index("%") + 1
        items.extend([repr(encoded[:middle]), repr(encoded[middle:])])
    else:
        items.extend([repr(DENIED[:12]), repr(DENIED[12:])])
    source = regex_source('"".join([' + ", ".join(items) + "])")
    result = ENGINE.scan_private_text(source, [DENIED])
    assert result["status"] == "fail"
    if kind != "concrete-path":
        assert result["literal_match_count"] == 1


def test_text_delta_ignores_only_unchanged_body():
    previous = (
        "CONTACT = "
        + repr(DENIED)
        + "\nPATH = "
        + repr(synthetic_home_path("macos", "operator-fixture/work"))
        + "\n"
    )
    current = previous + 'SAFE = "public"\n'
    assert (
        ENGINE.scan_private_text_delta(previous, current, [DENIED])["status"] == "pass"
    )
    assert ENGINE.scan_private_text(current, [DENIED])["status"] == "fail"


@pytest.mark.parametrize(
    "addition",
    [
        'CONTACT = "fixture-private-identity@example.invalid"\n',
        "PATH = " + repr(synthetic_home_path("macos", "new-operator/work")) + "\n",
    ],
)
def test_text_delta_new_values_are_not_excused_by_old_findings(addition):
    previous = "OLD = " + repr(synthetic_home_path("macos", "old-operator/work")) + "\n"
    assert (
        ENGINE.scan_private_text_delta(previous, previous + addition, [DENIED])[
            "status"
        ]
        == "fail"
    )


def test_text_delta_reconstructs_old_prefix_plus_new_piece():
    previous = 'PATH = (\n    "/" + "Users/"\n)\n'
    current = previous.replace("\n)", '\n    + "operator-fixture/work"\n)')
    result = ENGINE.scan_private_text_delta(previous, current, [DENIED])
    assert result["local_path_match"] is True
    assert result["reconstructed_path_match"] is True


def test_text_delta_same_value_elsewhere_does_not_hide_new_reconstruction():
    expression = '"fixture-private-" + "identity@example.invalid"'
    previous = "OLD = " + expression + '\nNEW = (\n    "fixture-private-"\n)\n'
    current = previous.replace("\n)", '\n    + "identity@example.invalid"\n)')
    assert (
        ENGINE.scan_private_text_delta(previous, current, [DENIED])[
            "literal_match_count"
        ]
        == 1
    )


def test_text_delta_generic_regex_role_is_preserved_for_added_lines():
    previous = "import re\n"
    current = (
        previous
        + "PATTERN = re.compile("
        + repr(synthetic_home_path("macos", r"[^/\s]+/work"))
        + ")\n"
    )
    assert (
        ENGINE.scan_private_text_delta(previous, current, [DENIED])["status"] == "pass"
    )


def test_text_delta_removed_regex_binding_rechecks_new_path_role():
    previous = (
        "import re\nPATTERN = re.compile("
        + repr(synthetic_home_path("macos", r"[^/\s]+/work"))
        + ")\n"
    )
    current = previous.removeprefix("import re\n")
    assert (
        ENGINE.scan_private_text_delta(previous, current, [DENIED])["local_path_match"]
        is True
    )


def test_text_delta_repetitive_input_has_bounded_full_fallback():
    previous = "SAFE = 1\n" * 4001 + "CONTACT = " + repr(DENIED) + "\n"
    result = ENGINE.scan_private_text_delta(previous, previous + "SAFE = 2\n", [DENIED])
    assert result["status"] == "fail"
    assert result["delta_full_fallback"] is True


@pytest.mark.parametrize(
    ("expression", "value"),
    [
        (
            "('c:' + '"
            + synthetic_home_path("windows", r"fixture\vault", separator="\\").lower()[
                2:
            ]
            + "')",
            synthetic_home_path("windows", r"fixture\vault", separator="\\").lower(),
        ),
        (r"('fixture-\t' + 'private-operator')", r"fixture-\tprivate-operator"),
        (r"('fixture-\n' + 'private-operator')", r"fixture-\nprivate-operator"),
        ("('fixture-''private-' + 'operator')", "fixture-'private-operator"),
        (r"('fixture-\' + 'private-operator')", "fixture-\\private-operator"),
        (
            "(('fixture-private-') # comment\n + ('operator'))",
            "fixture-private-operator",
        ),
    ],
)
@pytest.mark.parametrize("prefix", ["", "$value = "])
def test_powershell_single_quoted_concatenation_preserves_literal_values(
    expression, value, prefix
):
    text = prefix + expression
    result = ENGINE.scan_private_text(text, [value])
    assert result["status"] == "fail"
    assert result["literal_match_count"] == 1


def test_powershell_split_home_path_is_detected_without_python_escape_decoding():
    path = synthetic_home_path("windows", r"fixture-operator\vault", separator="\\")
    expression = (
        "$path = ("
        + " + ".join("'" + piece + "'" for piece in (path[:2], path[2:5], path[5:]))
        + ")"
    )
    result = ENGINE.scan_private_text(expression, [DENIED])
    assert result["status"] == "fail"
    assert result["local_path_match"] is True
    assert result["reconstructed_path_match"] is True


@pytest.mark.parametrize(
    "text",
    [
        "$x = 'fixture-private-' + dynamic + 'operator'",
        "$x = call('fixture-private-') + 'operator'",
        "$x = ('fixture-private-' + 'operator').replace('x', 'y') + 'suffix'",
    ],
)
def test_powershell_recovery_does_not_cross_dynamic_or_call_boundaries(text):
    views = ENGINE.static_string_views(text)
    assert not any(value == "fixture-private-operatorsuffix" for value, _ in views)
    if ".replace" not in text:
        assert not any(value == "fixture-private-operator" for value, _ in views)


@pytest.mark.parametrize(
    "text",
    [
        "$x = ('fixture-private-' + 'unterminated)",
        '$x = ("fixture-private-" + "\\u{6f}perator")',
        "$x = ('fixture-private-' + 'operator';)",
    ],
)
def test_unsupported_or_malformed_foreign_chain_remains_fail_closed(text):
    with pytest.raises(ValueError, match="private_identity_normalization_incomplete"):
        ENGINE.scan_private_text(text, [DENIED])


@pytest.mark.parametrize("with_powershell_values", [False, True])
def test_many_fences_do_not_recopy_all_accumulated_views(
    monkeypatch, with_powershell_values
):
    copied = []

    class MeasuredSet(set):
        def __init__(self, values=()):
            values = list(values)
            copied.append(len(values))
            super().__init__(values)

        def update(self, values):
            values = list(values)
            copied.append(len(values))
            super().update(values)

    monkeypatch.setattr(ENGINE, "set", MeasuredSet, raising=False)

    def work(count):
        copied.clear()
        expressions = [
            (
                (r"'public-\t' + 'brand-" + str(index) + "'")
                if with_powershell_values
                else repr("public-brand-" + str(index))
            )
            for index in range(count)
        ]
        source = "\n".join("```\n" + value + "\n```" for value in expressions)
        views = ENGINE.static_string_views(source)
        values = {value for value, _ in views}
        expected = {
            (r"public-\tbrand-" if with_powershell_values else "public-brand-")
            + str(index)
            for index in range(count)
        }
        assert expected <= values
        assert sum(copied) <= len(views) * 2
        return sum(copied)

    small = work(100)
    large = work(400)
    assert large <= max(1, small) * 5


@pytest.mark.parametrize(
    ("prefix", "expected_status"),
    [("safe", "pass"), ("fixture-private-", "fail")],
)
def test_valid_python_survives_incompatible_alternate_quote_grammar(
    prefix, expected_status
):
    import ast

    tail = "escaped" if expected_status == "pass" else "operator"
    source = "TEXT = '" + prefix + "' + '" + tail + "\\'+'"
    assert isinstance(ast.parse(source), ast.Module)
    with pytest.raises(ValueError, match="private_identity_normalization_incomplete"):
        ENGINE._powershell_single_string_views(source)
    result = ENGINE.scan_private_text(source, ["fixture-private-operator"])
    assert result["status"] == expected_status
    assert result["literal_match_count"] == (1 if expected_status == "fail" else 0)


def test_unexpected_alternate_decoder_error_is_not_suppressed(monkeypatch):
    def unexpected_error(text):
        raise ValueError("unexpected-decoder-error")

    monkeypatch.setattr(ENGINE, "_powershell_single_chain_value", unexpected_error)
    with pytest.raises(ValueError, match="unexpected-decoder-error"):
        ENGINE.scan_private_text("TEXT = 'safe' + 'public'", [DENIED])


@pytest.mark.parametrize(
    ("expression", "needle"),
    [
        ("TEXT = 'fixture''private-' + 'operator'", "fixture'private-operator"),
        (r"TEXT = 'fixture-\t' + 'private-operator'", r"fixture-\tprivate-operator"),
    ],
)
@pytest.mark.parametrize("insert_before", [False, True])
def test_unrelated_python_expression_cannot_erase_an_alternate_private_value(
    expression, needle, insert_before
):
    import ast

    unrelated = "OTHER = 'safe' + 'escaped\\'+'"
    source = "\n".join(
        [unrelated, expression] if insert_before else [expression, unrelated]
    )
    assert isinstance(ast.parse(source), ast.Module)
    assert ENGINE.scan_private_text(expression, [needle])["status"] == "fail"
    assert ENGINE.scan_private_text(source, [needle])["status"] == "fail"
    assert needle in {value for value, _ in ENGINE.static_string_views(source)}


@pytest.mark.parametrize("item", ["''", "'escaped\\'+'"])
def test_static_call_parent_does_not_hide_an_inner_alternate_value(item):
    import ast

    source = "TEXT = ('fixture''private-' + 'operator').join([" + item + "])"
    needle = "fixture'private-operator"
    assert isinstance(ast.parse(source), ast.Module)
    assert ENGINE.scan_private_text(source, [needle])["status"] == "fail"
    assert needle in {value for value, _ in ENGINE.static_string_views(source)}


def test_nested_static_add_is_decoded_only_at_disjoint_maximal_roots(monkeypatch):
    measured = []
    original = ENGINE._powershell_single_chain_value

    def observe(expression):
        measured.append(len(expression))
        return original(expression)

    monkeypatch.setattr(ENGINE, "_powershell_single_chain_value", observe)
    for count in (100, 400):
        measured.clear()
        expression = " + ".join([r"'public-\t'"] * count)
        source = "TEXT = (" + expression + ").join([''])"
        values = {value for value, _ in ENGINE.static_string_views(source)}
        assert r"public-\t" * count in values
        assert len(measured) == 1
        assert sum(measured) <= len(source)


@pytest.mark.parametrize("fenced", [False, True])
@pytest.mark.parametrize("comment", ["", " /* comment */"])
@pytest.mark.parametrize("other_first", [False, True])
@pytest.mark.parametrize("separator", ["\n", " "])
def test_foreign_python_compatible_quotes_remain_independent(
    fenced, comment, other_first, separator
):
    safe = "const 説明: string = 'safe'" + comment + " + 'escaped\\'+';"
    other = "const other: string = 'public' + 'brand';"
    source = separator.join([other, safe] if other_first else [safe, other])
    if fenced:
        source = "```typescript\n" + source + "\n```"
    assert ENGINE.scan_private_text(source, [DENIED])["status"] == "pass"


def test_fenced_python_compatible_quote_is_not_vetoed_by_alternate_grammar():
    source = "```python\nTEXT = 'safe' + 'escaped\\'+'\n```"
    assert ENGINE.scan_private_text(source, [DENIED])["status"] == "pass"


@pytest.mark.parametrize("other_first", [False, True])
@pytest.mark.parametrize("separator", ["\n", "; "])
def test_foreign_recovered_python_does_not_erase_overlapping_alternate_chain(
    other_first, separator
):
    private = "const x = 'fixture''private-' + 'operator';"
    other = "const y = 'safe' + 'escaped\\'+';"
    source = separator.join([other, private] if other_first else [private, other])
    needle = "fixture'private-operator"
    assert ENGINE.scan_private_text(private, [needle])["status"] == "fail"
    assert ENGINE.scan_private_text(source, [needle])["status"] == "fail"


@pytest.mark.parametrize("other_first", [False, True])
@pytest.mark.parametrize(
    ("private", "needle"),
    [
        ("TEXT = 'fixture''private-' + 'operator'", "fixture'private-operator"),
        (r"TEXT = 'fixture-\t' + 'private-operator'", r"fixture-\tprivate-operator"),
    ],
)
def test_fenced_private_alternate_survives_unrelated_python_quote(
    other_first, private, needle
):
    other = "OTHER = 'safe' + 'escaped\\'+'"
    body = "\n".join([other, private] if other_first else [private, other])
    source = "```python\n" + body + "\n```"
    assert ENGINE.scan_private_text(source, [needle])["status"] == "fail"
