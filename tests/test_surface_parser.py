"""What ``check_surface`` reads out of an ``apidoc.ts`` it does not control.

The route half of this gate has its own tests; this is the parameter half,
which is the half that fails quietly. A list this reader misses is not an error
— the other routes still count parameters, so the guard for a scan that found
nothing stays silent — and the route's genuine parameters come back as ones the
mirror invented, which sends whoever reads the report to the wrong file to
delete entries that are correct.

A port of the TypeScript SDK's ``test/surface-parser.test.ts`` cases for the
same fixes (OPL-4483, OPL-4511, OPL-4513, OPL-4514), against this reader instead:
``parameters()`` is called directly rather than through a subprocess, so what
each test asserts is the reader's answer rather than a diff line about it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def check_surface() -> ModuleType:
    """Import the standalone script without leaving its directory on sys.path."""
    scripts = str(Path(__file__).resolve().parent.parent / "scripts")
    sys.path.insert(0, scripts)
    try:
        import check_surface
    finally:
        sys.path.pop(0)
    return check_surface


def scan(check_surface: ModuleType, tmp_path: Path, apidoc: str) -> set[str]:
    """What the reader finds for ``GET sizes`` in an ``apidoc.ts`` spelled this way."""
    path = tmp_path / check_surface.APIDOC
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(apidoc)
    return check_surface.parameters(tmp_path)["GET sizes"]


def test_a_shared_constant_is_recorded_under_the_list_that_cites_it(
    check_surface: ModuleType, tmp_path: Path
) -> None:
    """``Query`` is the type of both lists upstream — ``headers?: Query[]``.

    So a shared constant cited in a ``headers`` list is a header. The loop bound
    ``kind`` and then wrote ``query:`` anyway, which is one missing parameter and
    one extra, both naming the same thing and neither of them true: the operator
    is sent to add a query nobody serves and delete a header that is correct.
    """
    found = scan(
        check_surface,
        tmp_path,
        "const X_KEY: Query = { name: 'X-Model-Key', description: 'x' };\n"
        "export const DOCS: Record<string, Doc> = {\n"
        "  'GET sizes': { headers: [X_KEY] },\n"
        "};\n",
    )
    assert found == {"header:X-Model-Key"}


def test_a_shared_constant_is_resolved_however_it_is_declared(
    check_surface: ModuleType, tmp_path: Path
) -> None:
    """``export``, an indent and the whitespace around ``:`` and ``=`` are spellings.

    None of them changes what the declaration means, and each one made the
    constant invisible. A route citing it then read as taking no parameters at
    all — so the parameters go missing from the upstream side, and the report
    tells whoever reads it to delete mirror entries that are correct.
    """
    for decl in (
        "export const PARTIAL: Query = {",
        "  const PARTIAL: Query = {",
        "const PARTIAL:Query = {",
        "const PARTIAL: Query =\n{",
    ):
        found = scan(
            check_surface,
            tmp_path,
            f"{decl} name: 'allow_partial', description: 'x' }};\n"
            "export const DOCS: Record<string, Doc> = {\n"
            "  'GET sizes': { query: [PARTIAL] },\n"
            "};\n",
        )
        assert found == {"query:allow_partial"}, decl


def test_a_shared_constant_does_not_resolve_to_a_copy_quoted_in_a_comment(
    check_surface: ModuleType, tmp_path: Path
) -> None:
    """Allowing an indent is what puts the scan inside block comments.

    A superseded copy of a declaration is indented under its ``*``, which is how
    ``apidoc.ts`` explains itself. The quoted copy comes last and wins the map,
    so every route citing the identifier reports one missing parameter and one
    extra, both naming a name nobody serves. The live declaration wins.
    """
    found = scan(
        check_surface,
        tmp_path,
        "const PARTIAL: Query = { name: 'allow_partial', description: 'x' };\n"
        "/* Superseded, kept for the reader:\n"
        "  const PARTIAL: Query = { name: 'stale_old_name', description: 'x' };\n"
        "*/\n"
        "export const DOCS: Record<string, Doc> = {\n"
        "  'GET sizes': { query: [PARTIAL] },\n"
        "};\n",
    )
    assert found == {"query:allow_partial"}


def test_a_query_list_whose_bracket_the_formatter_wrapped_is_still_read(
    check_surface: ModuleType, tmp_path: Path
) -> None:
    """The one space after the colon is a spelling, not a shape.

    Missed, the route's parameters go unread with nothing said, because a full
    table still counts parameters elsewhere.
    """
    found = scan(
        check_surface,
        tmp_path,
        """
        export const DOCS: Record<string, Doc> = {
          'GET sizes': {
            query:
              [{ name: 'limit', description: 'x' }],
          },
        };
        """,
    )
    assert found == {"query:limit"}


def test_the_entrys_own_query_list_is_read_not_one_nested_in_its_prose(
    check_surface: ModuleType, tmp_path: Path
) -> None:
    """``str.find`` answers with the first ``query: [`` at any depth.

    A response example nesting one of its own then supplies the parameters, and
    the route's real list — further down, at the entry's own depth — is never
    read at all.
    """
    found = scan(
        check_surface,
        tmp_path,
        """
        export const DOCS: Record<string, Doc> = {
          'GET sizes': {
            responses: { 200: { example: { query: [{ name: 'ghost' }] } } },
            query: [{ name: 'real', description: 'x' }],
          },
        };
        """,
    )
    assert found == {"query:real"}


def test_a_body_whose_object_call_is_spelled_with_other_whitespace_is_read(
    check_surface: ModuleType, tmp_path: Path
) -> None:
    """``body: object(`` is a spelling too: wrapped, it yielded no fields at all."""
    found = scan(
        check_surface,
        tmp_path,
        """
        export const DOCS: Record<string, Doc> = {
          'GET sizes': {
            body:
              object({ name: str('Name'), size: str('Size') }, { title: 'Sizes' }),
          },
        };
        """,
    )
    assert found == {"body:name", "body:size"}


def test_a_body_in_a_shape_the_reader_cannot_read_is_refused_not_reported_as_none(
    check_surface: ModuleType, tmp_path: Path
) -> None:
    """Reported as no fields, the route reads as documenting no body at all.

    And the mirror lists none for a route it cannot see either, so the two agree
    over a body neither of them looked at.
    """
    with pytest.raises(SystemExit) as exit_info:
        scan(
            check_surface,
            tmp_path,
            "export const DOCS: Record<string, Doc> = { 'GET sizes': { body: SHARED_BODY } };\n",
        )
    assert "documents a body in a form this" in str(exit_info.value)


def test_a_nested_body_literal_does_not_vouch_for_the_entrys_own(
    check_surface: ModuleType, tmp_path: Path
) -> None:
    """The refusal above, skipped by a ``body: {`` in a response example.

    The route then reports no fields, which matches a mirror that lists none —
    the vacuous all-clear the guard exists to refuse, arriving by way of the
    guard.
    """
    with pytest.raises(SystemExit) as exit_info:
        scan(
            check_surface,
            tmp_path,
            """
            export const DOCS: Record<string, Doc> = {
              'GET sizes': { responses: { 200: { body: { ok: true } } }, body: SHARED_BODY },
            };
            """,
        )
    assert "'GET sizes'" in str(exit_info.value)


def test_the_route_is_named_when_an_object_body_is_spelled_with_an_identifier(
    check_surface: ModuleType, tmp_path: Path
) -> None:
    """``object(SHARED_FIELDS)`` has no literal for the field walk to start at.

    Unchecked, the missing ``{`` came back as ``ValueError: substring not
    found`` — naming neither the route nor the file it is in. It is the same
    unreadable shape as the case beside it and belongs in the same sentence.
    """
    with pytest.raises(SystemExit) as exit_info:
        scan(
            check_surface,
            tmp_path,
            "export const DOCS: Record<string, Doc> = "
            "{ 'GET sizes': { body: object(SHARED_FIELDS) } };\n",
        )
    said = str(exit_info.value)
    assert "'GET sizes'" in said
    assert str(check_surface.APIDOC) in said
    assert "substring not found" not in said


def test_a_raw_schema_body_is_no_fields_rather_than_a_refusal(
    check_surface: ModuleType, tmp_path: Path
) -> None:
    """The file upload's own bytes: a body with nothing in it to name.

    The boundary of the refusal above, and the reason it tests the shape rather
    than the field count.
    """
    found = scan(
        check_surface,
        tmp_path,
        "export const DOCS: Record<string, Doc> = "
        "{ 'GET sizes': { body: { type: 'string', format: 'binary' } } };\n",
    )
    assert found == set()


def test_route_comments_do_not_contribute_entries(check_surface: ModuleType) -> None:
    source = """export const ROUTES: Route[] = [
      { method: 'GET', pattern: 'widgets' },
      // { method: 'POST', pattern: 'retired' },
    ];"""
    assert check_surface.table(source, "ROUTES") == {("GET", "widgets")}


@pytest.mark.parametrize(
    "extra",
    [
        "// ] } a comment must not close the list\n{ method: 'POST', pattern: 'widgets' },",
        '/* ] } */ { method: "POST", pattern: "widgets" },',
        '{ pattern: "widgets", method: "POST" },',
        (
            "{ method: 'POST', pattern: 'widgets', roles: ['reader', 'writer'], "
            "description: 'keep ] and } inside this string' },"
        ),
    ],
)
def test_every_literal_route_is_compared(check_surface: ModuleType, extra: str) -> None:
    source = (
        "export const ROUTES: Route[] = [{ method: 'GET', pattern: 'widgets' },\n" + extra + "\n];"
    )
    assert check_surface.table(source, "ROUTES") == {("GET", "widgets"), ("POST", "widgets")}


def test_a_commented_declaration_cannot_replace_the_live_table(check_surface: ModuleType) -> None:
    source = """/* export const ROUTES: Route[] = [
      { method: 'POST', pattern: 'retired' },
    ]; */
    export const ROUTES: Route[] = [
      { 'pattern': "widgets", "method": 'GET' },
    ];"""
    assert check_surface.table(source, "ROUTES") == {("GET", "widgets")}


@pytest.mark.parametrize(
    "tail",
    [
        "OTHER_ROUTE];",
        "...OTHER_ROUTES];",
        "{ method: METHOD, pattern: 'widgets' }];",
        "{ method: 'POST' }];",
        "{ method: 'POST', method: 'DELETE', pattern: 'widgets' }];",
        "{ method: 'POST', pattern: 'widgets' ];",
        "{ method: 'POST', pattern: 'widgets' }",
        "{ method: 'POST', pattern: 'unterminated }];",
        "{ method: 'POST', pattern: 'widgets', roles: [) }];",
        ",];",
    ],
)
def test_a_supported_route_does_not_hide_an_unreadable_entry(
    check_surface: ModuleType, tail: str
) -> None:
    source = "export const ROUTES: Route[] = [{ method: 'GET', pattern: 'widgets' },\n" + tail
    with pytest.raises(SystemExit, match="cannot read ROUTES"):
        check_surface.table(source, "ROUTES")


def test_quoted_body_keys_are_fields_but_nested_keys_are_not(
    check_surface: ModuleType, tmp_path: Path
) -> None:
    found = scan(
        check_surface,
        tmp_path,
        """export const DOCS: Record<string, Doc> = {
          'GET sizes': { 'body': object({
            'name': str('Name'), "size": str('Size'),
            nested: object({ 'child': str('Child') }),
          }) },
        };""",
    )
    assert found == {"body:name", "body:size", "body:nested"}


def test_double_quoted_routes_and_parameter_names_are_inventoried(
    check_surface: ModuleType, tmp_path: Path
) -> None:
    path = tmp_path / check_surface.APIDOC
    path.parent.mkdir(parents=True)
    path.write_text(
        """const SHARED: Query = { "name": "shared", description: 'Example' };
        export const DOCS: Record<string, Doc> = {
          'GET widgets': { query: [] },
          "POST widgets": {
            "query": [{ 'name': "limit", description: "ignore name: 'ghost'" }, SHARED],
            'headers': [SHARED, { name: 'X-Example' }],
          },
        };"""
    )
    assert check_surface.parameters(tmp_path) == {
        "GET widgets": set(),
        "POST widgets": {"query:limit", "query:shared", "header:shared", "header:X-Example"},
    }


@pytest.mark.parametrize("key", ["query", "headers"])
@pytest.mark.parametrize(
    "value",
    [
        "SHARED_ARRAY",
        "[UNKNOWN_ENTRY]",
        "[{ name: 'known' }, UNKNOWN_ENTRY]",
        "[...SHARED_ARRAY]",
        "[makeEntry()]",
        "[{ description: 'name omitted' }]",
        "[{ name: NAME }]",
        "[{ name: 'known' }] || SHARED_ARRAY",
    ],
)
def test_unreadable_parameter_declarations_do_not_become_empty_inventories(
    check_surface: ModuleType, tmp_path: Path, key: str, value: str
) -> None:
    with pytest.raises(SystemExit) as exc:
        scan(
            check_surface,
            tmp_path,
            f"export const DOCS: Record<string, Doc> = {{ 'GET sizes': {{ {key}: {value} }} }};",
        )
    assert "GET sizes" in str(exc.value)
    assert key in str(exc.value)


@pytest.mark.parametrize(
    "body",
    [
        "object(SHARED_FIELDS, { title: 'This is not the schema' })",
        "object({ known: str('Known'), ...SHARED_FIELDS })",
        "object({ [FIELD]: str('Name') })",
        "object({ 'unterminated: str('Name') })",
        "object({ known: str('Known') }) || SHARED_BODY",
    ],
)
def test_unreadable_body_fields_are_not_a_partial_inventory(
    check_surface: ModuleType, tmp_path: Path, body: str
) -> None:
    with pytest.raises(SystemExit):
        scan(
            check_surface,
            tmp_path,
            f"export const DOCS: Record<string, Doc> = {{ 'GET sizes': {{ body: {body} }} }};",
        )


def test_empty_lists_and_a_raw_body_schema_remain_valid(
    check_surface: ModuleType, tmp_path: Path
) -> None:
    assert (
        scan(
            check_surface,
            tmp_path,
            """export const DOCS: Record<string, Doc> = {
          'GET sizes': { query: [], headers: [], body: { type: 'string' } },
        };""",
        )
        == set()
    )


@pytest.mark.parametrize("suffix", [".concat(OTHER_ROUTES)", "\n.concat(OTHER_ROUTES)"])
def test_route_initializer_continuations_are_refused(
    check_surface: ModuleType, suffix: str
) -> None:
    source = "export const ROUTES: Route[] = [{ method: 'GET', pattern: 'widgets' }]" + suffix + ";"
    with pytest.raises(SystemExit, match="cannot read ROUTES"):
        check_surface.table(source, "ROUTES")


@pytest.mark.parametrize("kind", ["docs", "shared"])
def test_parameter_initializer_continuations_are_refused(
    check_surface: ModuleType, tmp_path: Path, kind: str
) -> None:
    shared = "const SHARED: Query = { name: 'known' }"
    docs = "export const DOCS: Record<string, Doc> = { 'GET sizes': { query: [SHARED] } }"
    source = shared + (" && OTHER_ENTRY" if kind == "shared" else "") + ";\n"
    source += docs + (" && OTHER_DOCS" if kind == "docs" else "") + ";"
    with pytest.raises(SystemExit, match="cannot read DOCS"):
        scan(check_surface, tmp_path, source)


def test_a_template_string_cannot_supply_the_route_declaration(check_surface: ModuleType) -> None:
    source = """const example = `
    export const ROUTES: Route[] = [{ method: 'GET', pattern: 'retired' }];
    `;
    export const ROUTES: Route[] = [{ method: 'GET', pattern: 'widgets' }];
    """
    assert check_surface.table(source, "ROUTES") == {("GET", "widgets")}


def test_a_template_string_cannot_supply_parameter_declarations(
    check_surface: ModuleType, tmp_path: Path
) -> None:
    found = scan(
        check_surface,
        tmp_path,
        """const example = `
        export const DOCS: Record<string, Doc> = { 'GET sizes': { query: [] } };
        `;
        const SHARED: Query = { name: 'known' };
        const another = `
        const SHARED: Query = { name: 'retired' };
        `;
        export const DOCS: Record<string, Doc> = { 'GET sizes': { query: [SHARED] } };
        """,
    )
    assert found == {"query:known"}


def test_a_function_local_parameter_does_not_replace_the_module_entry(
    check_surface: ModuleType, tmp_path: Path
) -> None:
    found = scan(
        check_surface,
        tmp_path,
        """const SHARED: Query = { name: 'known' };
        function example() {
          const SHARED: Query = { name: 'retired' };
          return SHARED;
        }
        export const DOCS: Record<string, Doc> = { 'GET sizes': { query: [SHARED] } };
        """,
    )
    assert found == {"query:known"}


def test_nested_template_interpolations_cannot_supply_declarations(
    check_surface: ModuleType, tmp_path: Path
) -> None:
    source = """const example = `outer ${(() => {
      const SHARED: Query = { name: 'local' };
      return `inner ${`deep
        export const ROUTES: Route[] = [{ method: 'POST', pattern: 'retired' }];
        export const DOCS: Record<string, Doc> = { 'GET sizes': { query: [] } };
      `}`;
    })()}
    const SHARED: Query = { name: 'quoted' };
    `;
    const SHARED: Query = { name: 'known' };
    export const ROUTES: Route[] = [{ method: 'GET', pattern: 'widgets' }];
    export const DOCS: Record<string, Doc> = { 'GET sizes': { query: [SHARED] } };
    """
    assert check_surface.table(source, "ROUTES") == {("GET", "widgets")}
    assert scan(check_surface, tmp_path, source) == {"query:known"}


@pytest.mark.parametrize("kind", ["docs", "shared"])
def test_a_declaration_available_only_in_a_template_is_refused(
    check_surface: ModuleType, tmp_path: Path, kind: str
) -> None:
    shared = "const SHARED: Query = { name: 'known' };"
    docs = "export const DOCS: Record<string, Doc> = { 'GET sizes': { query: [SHARED] } };"
    source = (
        shared + "\nconst example = `\n" + docs + "\n`;"
        if kind == "docs"
        else "const example = `\n" + shared + "\n`;\n" + docs
    )
    with pytest.raises(SystemExit, match="cannot read"):
        scan(check_surface, tmp_path, source)


def test_a_local_entry_without_a_module_declaration_is_unresolved(
    check_surface: ModuleType, tmp_path: Path
) -> None:
    with pytest.raises(SystemExit, match="unresolved parameter entry"):
        scan(
            check_surface,
            tmp_path,
            """function example() {
              const SHARED: Query = { name: 'local' };
            }
            export const DOCS: Record<string, Doc> = { 'GET sizes': { query: [SHARED] } };
            """,
        )


def test_duplicate_module_declarations_are_ambiguous(check_surface: ModuleType) -> None:
    source = "export const ROUTES: Route[] = [{ method: 'GET', pattern: 'widgets' }];\n"
    with pytest.raises(SystemExit, match="ambiguous"):
        check_surface.table(source + source, "ROUTES")


def test_a_literal_declaration_may_end_at_eof(check_surface: ModuleType, tmp_path: Path) -> None:
    assert check_surface.table(
        "export const ROUTES: Route[] = [{ method: 'GET', pattern: 'widgets' }]", "ROUTES"
    ) == {("GET", "widgets")}
    assert (
        scan(
            check_surface,
            tmp_path,
            "export const DOCS: Record<string, Doc> = { 'GET sizes': { query: [] } }",
        )
        == set()
    )


@pytest.mark.parametrize("real", ["", "buildRoutes()", "[{ method: 'GET', pattern: 'widgets' }]"])
def test_a_control_statement_regex_cannot_supply_routes(
    check_surface: ModuleType, real: str
) -> None:
    source = (
        "if (enabled) /export const ROUTES: Route[] = [{method:'GET',pattern:'fake'}];/.test(text);"
    )
    if real:
        source += "\nexport const ROUTES: Route[] = " + real + ";"
    with pytest.raises(SystemExit, match="cannot read ROUTES.*slash"):
        check_surface.table(source, "ROUTES")


@pytest.mark.parametrize("real", ["", "buildDocs()", "{ 'GET sizes': { query: [] } }"])
def test_a_control_statement_regex_cannot_supply_documented_parameters(
    check_surface: ModuleType, tmp_path: Path, real: str
) -> None:
    source = (
        "if (enabled) /export const DOCS: Record<string, Doc> = "
        "{'GET sizes': { query: [] }};/.test(text);"
    )
    if real:
        source += "\nexport const DOCS: Record<string, Doc> = " + real + ";"
    with pytest.raises(SystemExit, match="cannot read DOCS.*slash"):
        scan(check_surface, tmp_path, source)


@pytest.mark.parametrize(
    "prefix",
    [
        "function example() { if (enabled) /fake/.test(text); }\n",
        "const example = (value() / 2);\n",
        "const example = { value: value() / 2 };\n",
    ],
)
def test_an_ambiguous_slash_is_refused_inside_nested_scopes(
    check_surface: ModuleType, prefix: str
) -> None:
    source = prefix + "export const ROUTES: Route[] = [{ method: 'GET', pattern: 'widgets' }];"
    with pytest.raises(SystemExit, match="cannot read ROUTES.*slash"):
        check_surface.table(source, "ROUTES")


def test_a_supported_regex_position_still_hides_its_contents(check_surface: ModuleType) -> None:
    source = """const pattern = /export const ROUTES: Route[] = [{method:'GET',pattern:'fake'}];/;
    function example() { return /[{}]/; }
    export const ROUTES: Route[] = [{ method: 'GET', pattern: 'widgets' }];
    """
    assert check_surface.table(source, "ROUTES") == {("GET", "widgets")}


def test_an_unreadable_regex_cannot_supply_a_declaration(check_surface: ModuleType) -> None:
    source = """const example = /unterminated
    export const ROUTES: Route[] = [{ method: 'GET', pattern: 'widgets' }];
    """
    with pytest.raises(SystemExit, match="cannot read ROUTES.*regex"):
        check_surface.table(source, "ROUTES")


def _interpolated_regex_prose(declaration: str) -> str:
    return (
        "const prose = `${(() => { if (enabled) /}}`; " + declaration + " `/.test(text); })()}`;\n"
    )


def test_an_interpolated_regex_cannot_supply_routes(check_surface: ModuleType) -> None:
    source = (
        _interpolated_regex_prose("export const ROUTES: Route[] = [{method:'GET',pattern:'fake'}];")
        + "export const ROUTES: Route[] = buildRoutes();"
    )
    with pytest.raises(SystemExit, match="cannot read ROUTES.*slash"):
        check_surface.table(source, "ROUTES")


def test_an_interpolated_regex_cannot_supply_documentation(
    check_surface: ModuleType, tmp_path: Path
) -> None:
    source = (
        _interpolated_regex_prose(
            "export const DOCS: Record<string, Doc> = {'GET sizes': {query: []}};"
        )
        + "export const DOCS: Record<string, Doc> = buildDocs();"
    )
    with pytest.raises(SystemExit, match="cannot read DOCS.*slash"):
        scan(check_surface, tmp_path, source)


def test_an_interpolated_regex_cannot_supply_a_shared_entry(
    check_surface: ModuleType, tmp_path: Path
) -> None:
    source = _interpolated_regex_prose("const SHARED: Query = {name: 'fake'};") + (
        "export const DOCS: Record<string, Doc> = {'GET sizes': {query: [SHARED]}};"
    )
    with pytest.raises(SystemExit, match="cannot read DOCS.*slash"):
        scan(check_surface, tmp_path, source)


@pytest.mark.parametrize(
    "reader",
    [
        "quoted_end",
        "strip_comments",
        "balanced",
        "split_items",
        "top_level_keys",
        "literal_contents",
    ],
)
def test_interpolation_boundaries_are_checked_through_each_reader(
    check_surface: ModuleType, reader: str
) -> None:
    surface_text = sys.modules["surface_text"]
    template = (
        _interpolated_regex_prose("const HIDDEN = {}; ")
        .removeprefix("const prose = ")
        .removesuffix(";\n")
    )
    with pytest.raises(ValueError, match="ambiguous slash"):
        if reader == "quoted_end":
            surface_text.quoted_end(template, 0)
        elif reader == "balanced":
            surface_text.balanced("{value: " + template + "}", 0, "{", "}")
        elif reader == "literal_contents":
            surface_text.literal_contents("[" + template + "]", "[", "]")
        else:
            getattr(surface_text, reader)("value: " + template)


def test_a_recognized_interpolation_regex_hides_its_delimiters(check_surface: ModuleType) -> None:
    source = _interpolated_regex_prose(
        "export const ROUTES: Route[] = [{method:'GET',pattern:'fake'}];"
    ).replace("if (enabled)", "return")
    source += "export const ROUTES: Route[] = [{method:'GET',pattern:'widgets'}];"
    assert check_surface.table(source, "ROUTES") == {("GET", "widgets")}


@pytest.mark.parametrize("property_name", ["delay", "return"])
def test_member_division_inside_interpolation_preserves_the_inventory(
    check_surface: ModuleType, property_name: str
) -> None:
    source = "const prose = `${Math.ceil(result." + property_name + " / 1000)}`;\n"
    source += "export const ROUTES: Route[] = [{method:'GET',pattern:'widgets'}];"
    assert check_surface.table(source, "ROUTES") == {("GET", "widgets")}


@pytest.mark.parametrize("divisor", ["3600 / 2", "WINDOW_S / 2", "window.size / 2"])
def test_ordinary_division_beside_a_table_is_read_as_arithmetic(
    check_surface: ModuleType, divisor: str
) -> None:
    """Legal arithmetic upstream must not cost the whole comparison.

    The run that enforces this is the platform's CI, so a refusal here is a red
    build over source this reader has no interest in — and a slash after a
    value cannot open a regex, so reading it as division is not a guess.
    """
    source = "const HALF = " + divisor + ";\n"
    source += "export const ROUTES: Route[] = [{method:'GET',pattern:'widgets'}];"
    assert check_surface.table(source, "ROUTES") == {("GET", "widgets")}


def test_a_slash_after_a_keyword_is_not_read_as_division(check_surface: ModuleType) -> None:
    """A keyword is not a value, so the slash after one is still a regex.

    `_DIVISION_OPERAND` matches any trailing word, and a word that can precede
    a regex has to stay out of that reading: `return /}/` divides nothing, and
    treating its literal as an operator would put the table's own delimiters
    back inside a regex nobody read.
    """
    surface_text = sys.modules["surface_text"]
    source = "const f = () => { return /}}/.test(x); };"
    assert (
        surface_text.checked_slash_end(source, source.index("/}}/")) == source.index("/.test") + 1
    )


@pytest.mark.parametrize(
    "prelude",
    [
        "export default /a; export const ROUTES: Route[] = [{method:'GET',pattern:'fake'}]; z/;",
        "class C extends /}/.constructor {}",
        "const t = `${class extends /}[`]/.constructor {}}`;",
    ],
)
def test_a_regex_after_a_keyword_cannot_supply_a_table(
    check_surface: ModuleType, prelude: str
) -> None:
    """Every reserved word, not only the ones that can lead a regex.

    `export default /…/` and `extends /…/` are legal TypeScript, and reading
    either slash as division walks the reader into the regex: the first carried
    a whole fake route table inside one, and the other two close a scope and
    end a template with delimiters nobody wrote.
    """
    source = prelude + "export const ROUTES: Route[] = [{method:'GET',pattern:'widgets'}];"
    with pytest.raises(SystemExit, match="cannot read ROUTES.*slash"):
        check_surface.table(source, "ROUTES")


def test_a_keyword_this_reader_cannot_classify_is_still_refused(
    check_surface: ModuleType,
) -> None:
    """The division reading is for values only; the rest stays fail-closed."""
    surface_text = sys.modules["surface_text"]
    source = "const f = () => { typeof /}}/; };"
    with pytest.raises(ValueError, match="ambiguous slash"):
        surface_text.checked_slash_end(source, source.index("/}}/"))


def test_an_undecidable_slash_is_refused_with_a_line_and_column(
    check_surface: ModuleType,
) -> None:
    """The offset it used to name is not a place anybody can go and look."""
    surface_text = sys.modules["surface_text"]
    source = "const a = 1;\nconst b = size() /* x */;\n"
    with pytest.raises(ValueError, match=r"ambiguous slash at line 2 column 18"):
        surface_text.checked_slash_end(source.replace("/* x */", "/[a]"), source.index("/*"))


@pytest.mark.parametrize("line_ending", ["\n", "\r\n"])
def test_an_interpolation_comment_cannot_supply_a_division_operand(
    check_surface: ModuleType, line_ending: str
) -> None:
    source = _interpolated_regex_prose(
        "export const ROUTES: Route[] = [{method:'GET',pattern:'fake'}];"
    ).replace("if (enabled) /", "if (enabled) // prose.member" + line_ending + "/")
    source += "export const ROUTES: Route[] = buildRoutes();"
    with pytest.raises(SystemExit, match="cannot read ROUTES.*slash"):
        check_surface.table(source, "ROUTES")


def _line_split_division(prelude: str, value: str) -> str:
    """A file whose only real route table is reachable ONLY across two line breaks.

    Deliberately adversarial, and spelled out because no formatter produces it: two
    divisions are wrapped so the slash opens its line, and each of the reader's two
    readings of such a slash ends at the next slash on that line. Read as regex
    literals they eat one backtick each — the opening and the closing one of the
    template holding the fake table — which keeps the reader's backtick parity even,
    so nothing downstream refuses. The fake table inside the template then reads as
    code and the real one, swallowed by the second reading, is not there at all.

    What JavaScript sees is ``value / `…` + 1``: a division by a template,
    the fake table being that template's TEXT, and one route table in the file.
    """
    return (
        prelude + f"const ratio = {value}\n"
        " / ` / 2;\n"
        "export const ROUTES: Route[] = [{method:'GET',pattern:'fake'}];\n"
        f"const tail = {value}\n"
        " / ` + 1; export const ROUTES: Route[] = "
        "[{method:'GET',pattern:'widgets'}]; const z = 4 / 5;\n"
    )


@pytest.mark.parametrize(
    ("prelude", "value"),
    [
        ("const obj = {return: 1};\n", "obj.return"),
        ("let x = 2;\n", "x++"),
        ("const count = 2;\n", "count"),
    ],
    ids=["member-access", "postfix-update", "plain-name"],
)
def test_a_line_split_division_cannot_expose_a_table_inside_a_template(
    check_surface: ModuleType, prelude: str, value: str
) -> None:
    """The strict reader's operand tests stopped at the end of the line.

    So a division the source wrapped left it reading a regex where there was an
    operator — for a plain name it refused, which is honest, and for the other two
    it walked into the "regex" and came back with a route table it had read out of a
    template's prose while the real one was inside a literal that reading had
    shifted. That is the fail-open OPL-4805 closed in the lenient reader, in the
    strict one (OPL-4824).

    The same three spellings as the lenient reader's own line-break test, because
    the slash predicate places them differently: a member access whose property is
    a reserved word and a postfix update both read as regex positions, and a plain
    name reads as neither.
    """
    assert check_surface.table(_line_split_division(prelude, value), "ROUTES") == {
        ("GET", "widgets")
    }


def test_a_line_split_value_in_raw_source_is_refused_rather_than_read(
    check_surface: ModuleType,
) -> None:
    """Inside a template interpolation the strict reader is handed RAW source.

    Its comments are not blanked there, so a name on the line above the slash may
    have come out of a line comment and the operand cannot be believed. The answer
    is the refusal a gate is for: it used to read the slash as opening a regex and
    carry on with the wrong state, silently.
    """
    source = (
        "const prose = `${(() => { let x = 2; const r = x++\n / 2 / 3; return r; })()}`;\n"
        "export const ROUTES: Route[] = [{method:'GET',pattern:'widgets'}];"
    )
    with pytest.raises(SystemExit, match="cannot read ROUTES.*slash after a value"):
        check_surface.table(source, "ROUTES")


def test_the_two_slash_readers_differ_only_in_what_the_caller_promised(
    check_surface: ModuleType,
) -> None:
    """One position, two answers, and the difference is a promise about comments.

    Blanked source cannot hide an operand in a comment, so the value one line up is
    the operand it looks like and the slash divides. Raw source can, so the same
    position refuses. Pinned together because a caller that passes the flag without
    blanking would get the division reading over a comment's token.
    """
    surface_text = sys.modules["surface_text"]
    source = "const ratio = obj.return\n / 2;"
    at = source.index("/ 2")
    assert surface_text.checked_slash_end(source, at, comments_blanked=True) == at + 1
    with pytest.raises(ValueError, match="unreadable slash after a value at line 2 column 2"):
        surface_text.checked_slash_end(source, at)


@pytest.mark.parametrize("contents", ["echo ${value/path}", "echo ${unfinished", "echo \\"])
def test_go_raw_strings_do_not_invoke_template_interpolation(
    check_surface: ModuleType, contents: str
) -> None:
    source = "package example\nconst script = `" + contents + "`\nconst sampleLimit = 12 * 3\n"
    assert check_surface.constant(source, "sampleLimit", Path("fixture").with_suffix(".go")) == 36


def test_typescript_constant_scanning_keeps_strict_interpolation_boundaries(
    check_surface: ModuleType,
) -> None:
    source = _interpolated_regex_prose("export const SAMPLE_LIMIT = 99;")
    source += "export const SAMPLE_LIMIT = 36;"
    with pytest.raises(ValueError, match="ambiguous slash"):
        check_surface.constant(source, "SAMPLE_LIMIT", Path("fixture").with_suffix(".ts"))


# --- a constant read out of prose (OPL-4805) -------------------------------
#
# Comment blanking closed one half of this: a commented-out declaration cannot
# be read as the declaration. A QUOTED one still could, and the first match in
# the file won, so a snippet in a template or in a Go raw string decided what
# the platform's constant was — and a number read out of prose that happens to
# equal the mirror is a comparison that passes over real drift.


def test_a_quoted_typescript_declaration_does_not_win_over_the_real_one(
    check_surface: ModuleType,
) -> None:
    source = (
        "export const snippet = `\nexport const SAMPLE_LIMIT = 99;\n`;\n"
        "export const SAMPLE_LIMIT = 36;\n"
    )
    assert check_surface.constant(source, "SAMPLE_LIMIT", Path("fixture").with_suffix(".ts")) == 36


def test_a_go_raw_string_declaration_does_not_win_over_the_real_one(
    check_surface: ModuleType,
) -> None:
    source = (
        "package example\n"
        "var script = `\nconst sampleLimit = 99\n`\n"
        "const (\n\tsampleLimit = 12 * 3\n)\n"
    )
    assert check_surface.constant(source, "sampleLimit", Path("fixture").with_suffix(".go")) == 36


@pytest.mark.parametrize(
    ("source", "name", "suffix"),
    [
        ("export const A = 1;\nexport const A = 2;\n", "A", ".ts"),
        ("package example\nconst a = 1\nconst (\n\ta = 2\n)\n", "a", ".go"),
    ],
    ids=["typescript", "go"],
)
def test_two_declarations_of_one_name_are_refused_rather_than_ordered(
    check_surface: ModuleType, source: str, name: str, suffix: str
) -> None:
    """Position is not evidence. Picking the first is how it agrees with the wrong one."""
    with pytest.raises(SystemExit, match="declared more than once"):
        check_surface.constant(source, name, Path("fixture").with_suffix(suffix))


def test_a_regex_this_reader_cannot_place_refuses_rather_than_picking_a_side(
    check_surface: ModuleType,
) -> None:
    """The attack the blanking alone did not stop (adversarial review, OPL-4805).

    A regex literal after a `)` is a position this reader cannot decide, and a
    backtick inside one moves a template's boundary: read as an operator, the real
    declaration lands inside the "template" and is blanked, leaving the snippet as
    the only declaration in the file. Both readings are taken and the disagreement
    is the answer — the value this would otherwise have reported is the prose one.
    """
    source = (
        'if (true) /`/.test("");\n'
        "export const SAMPLE_LIMIT = 99;\n"
        "const snippet = `\nexport const SAMPLE_LIMIT = 36;\n`;\n"
        'if (true) /`/.test("");\n'
    )
    with pytest.raises(SystemExit, match="undecidable slash"):
        check_surface.constant(source, "SAMPLE_LIMIT", Path("fixture").with_suffix(".ts"))


def test_a_typed_go_declaration_is_not_invisible_to_a_local_of_the_same_name(
    check_surface: ModuleType,
) -> None:
    """`const name int = 99` is still the declaration (adversarial review, OPL-4805).

    A pattern blind to the type read the file as if the constant were declared
    somewhere else, and a function-local of the same name was where it then found
    it — so a local `36` answered for an exported `99`.
    """
    source = (
        "package example\n"
        "const sampleLimit int = 99\n\n"
        "func f() {\n\tconst sampleLimit = 36\n\t_ = sampleLimit\n}\n"
    )
    assert check_surface.constant(source, "sampleLimit", Path("fixture").with_suffix(".go")) == 99


def test_a_declaration_only_inside_a_function_is_not_the_platforms_constant(
    check_surface: ModuleType,
) -> None:
    """Nothing mirrors a local, so finding one is finding nothing."""
    source = "package example\nfunc f() {\n\tconst sampleLimit = 36\n}\n"
    with pytest.raises(SystemExit, match="not found"):
        check_surface.constant(source, "sampleLimit", Path("fixture").with_suffix(".go"))


@pytest.mark.parametrize(
    "division",
    ["Date.now() / 1000", "x++ / 2", "obj.return / 2"],
    ids=["after-a-call", "after-a-postfix", "after-a-reserved-property"],
)
def test_an_ordinary_division_still_does_not_refuse_a_constant(
    check_surface: ModuleType, division: str
) -> None:
    """Three divisions the slash predicate places differently, none of which may cost
    the comparison: the first it cannot decide, and the other two it reads as the
    start of a regex. A regex cannot cross a line break, so neither reading moves a
    boundary here, and the two policies agree."""
    source = f"const value = {division};\nexport const SAMPLE_LIMIT = 36;\n"
    assert check_surface.constant(source, "SAMPLE_LIMIT", Path("fixture").with_suffix(".ts")) == 36


def test_a_division_after_a_property_named_like_a_keyword_is_not_a_regex(
    check_surface: ModuleType,
) -> None:
    """The agreement of two readings is worth nothing where both guess the same way.

    `obj.return / …` is a division, and the slash predicate calls it a regex
    position because the word before it is `return`. Both policies then consumed a
    backtick as part of that "regex", which moved a template's boundary: the
    declaration inside the template was left standing and the real one was blanked.
    A value a member access decides is not a guess, so it is settled before either
    policy is consulted (adversarial review, OPL-4805).
    """
    source = (
        "const obj = {return: 1};\n"
        "const ratio = obj.return / `/\nexport const SAMPLE_LIMIT = 36;\n` / 2;\n"
        "export const SAMPLE_LIMIT = 99;\n"
        "const marker = /`/;\n"
    )
    assert check_surface.constant(source, "SAMPLE_LIMIT", Path("fixture").with_suffix(".ts")) == 99


def test_a_brace_inside_a_regex_does_not_move_a_declarations_scope(
    check_surface: ModuleType,
) -> None:
    """A regex body is a literal, and one left standing is counted as code.

    A `}` in a regex closed a scope nobody opened, so the module's own declaration
    read as nested and a nested one read as top level — the exported 99 lost to a
    namespace's 36. An unmatched `{` did the mirror image and reported a real
    declaration missing (adversarial review, OPL-4805).
    """
    hidden = (
        "const pattern = /[}]/;\n"
        "export const SAMPLE_LIMIT = 99;\n"
        "namespace Example {\nexport const SAMPLE_LIMIT = 36;\n}\n"
    )
    ts = Path("fixture").with_suffix(".ts")
    assert check_surface.constant(hidden, "SAMPLE_LIMIT", ts) == 99
    unmatched = "const pattern = /[{]/;\nexport const SAMPLE_LIMIT = 36;\n"
    assert check_surface.constant(unmatched, "SAMPLE_LIMIT", ts) == 36


def test_a_prefix_update_of_a_regex_property_is_not_a_division(
    check_surface: ModuleType,
) -> None:
    """`++ /re/.lastIndex` increments a property OF a regex, so the slash opens one.

    The postfix test was written as "a `++` before the slash" and matched this,
    which turned a regex into a division and let its backtick move a template's
    boundary. A postfix update has an operand in front of it; a prefix one does not
    (adversarial review, OPL-4805).
    """
    source = (
        "++ /`/.lastIndex;\n"
        "export const SAMPLE_LIMIT = 99;\n"
        "const snippet = `\nexport const SAMPLE_LIMIT = 36;\n`;\n"
        "++ /`/.lastIndex;\n"
    )
    assert check_surface.constant(source, "SAMPLE_LIMIT", Path("fixture").with_suffix(".ts")) == 99


@pytest.mark.parametrize("value", ["obj.return", "x++"], ids=["member-access", "postfix-update"])
def test_a_line_break_between_a_value_and_its_slash_is_still_a_division(
    check_surface: ModuleType, value: str
) -> None:
    """A value on the line above divides just the same, and JavaScript agrees:
    a name, a line break and a slash is one expression, not a new statement.

    The operand tests reached only as far as a space or a tab, so both divisions
    came back as regex positions the moment the line wrapped (adversarial review,
    OPL-4805).
    """
    source = (
        "const obj = {return: 1};\nlet x = 2;\n"
        f"const ratio = {value}\n / `/\nexport const SAMPLE_LIMIT = 36;\n` / 2;\n"
        "export const SAMPLE_LIMIT = 99;\n"
        "const marker = /`/;\n"
    )
    assert check_surface.constant(source, "SAMPLE_LIMIT", Path("fixture").with_suffix(".ts")) == 99


def test_a_hashbang_is_not_code_and_its_braces_are_not_scope(
    check_surface: ModuleType,
) -> None:
    """Every tool that reads one of these files treats a `#!` line as a comment.

    This reader did not, so a brace in one closed a scope nobody opened — which hid
    the module's own declaration and promoted a namespace's (adversarial review,
    OPL-4805).
    """
    source = (
        "#!/usr/bin/env node --title=}\n"
        "export const SAMPLE_LIMIT = 99;\n"
        "namespace Example {\nexport const SAMPLE_LIMIT = 36;\n}\n"
    )
    assert check_surface.constant(source, "SAMPLE_LIMIT", Path("fixture").with_suffix(".ts")) == 99


def test_braces_that_do_not_balance_refuse_rather_than_placing_a_declaration(
    check_surface: ModuleType,
) -> None:
    """The depth rule is a count, and a count that has gone negative is evidence of
    text this reader is not seeing as text. Whether the declaration is at the top
    level is then exactly what cannot be established, so it is refused."""
    with pytest.raises(SystemExit, match="do not balance"):
        check_surface.constant(
            "}\nexport const SAMPLE_LIMIT = 36;\n",
            "SAMPLE_LIMIT",
            Path("fixture").with_suffix(".ts"),
        )


#: A file whose real declaration is 99 and whose template carries a 36, with the
#: attack construct substituted in. Every case below is the same question: did the
#: reader place the template's boundary where JavaScript places it?
_HIDDEN_BY_A_TEMPLATE = (
    "const obj = {{return: 1}};\nlet x = 1;\n"
    "{attack}\n"
    "export const SAMPLE_LIMIT = 99;\n"
    "const snippet = `\nexport const SAMPLE_LIMIT = 36;\n`;\n"
    "const marker = /`/;\n"
)


@pytest.mark.parametrize(
    "attack",
    [
        "if (true) ++ /`/.lastIndex;",
        "x\n++ /`/.lastIndex;",
        "const pad = obj.return" + " " * 260 + "/ 2;",
    ],
    ids=["prefix-update-after-a-condition", "prefix-update-after-a-line-break", "padded-operand"],
)
def test_a_slash_this_reader_cannot_place_never_reports_the_templates_number(
    check_surface: ModuleType, attack: str
) -> None:
    """Three ways the previous round's certainties were false, and one rule for all.

    A `)` is not something a postfix update can be applied to, a line break before
    `++` is where JavaScript ends the statement, and an operand does not stop being
    one because 260 spaces follow it. Each of them read a regex as a division or the
    reverse, moved a template's boundary, and reported the number inside the
    template. Reading the value correctly is the good outcome and refusing is an
    acceptable one; reporting the template's 36 is not (adversarial review, OPL-4805).
    """
    source = _HIDDEN_BY_A_TEMPLATE.format(attack=attack)
    try:
        assert (
            check_surface.constant(source, "SAMPLE_LIMIT", Path("fixture").with_suffix(".ts")) == 99
        )
    except SystemExit as refusal:
        assert "SAMPLE_LIMIT" in str(refusal)


def test_a_comments_punctuation_cannot_make_a_slash_certain(
    check_surface: ModuleType,
) -> None:
    """Blanking comments protects every slash decision, not only the first.

    A `?` at the end of a line comment was read as the character before the slash
    below it, which made an undecidable division certainly a regex — and both
    policies then guessed it the same way, so the disagreement that is supposed to
    catch a guess never happened (adversarial review, OPL-4805).
    """
    source = (
        "const ratio = (1) // why?\n / `/\nexport const SAMPLE_LIMIT = 36;\n` / 2;\n"
        "export const SAMPLE_LIMIT = 99;\n"
        "const marker = /`/;\n"
    )
    with pytest.raises(SystemExit, match="undecidable slash"):
        check_surface.constant(source, "SAMPLE_LIMIT", Path("fixture").with_suffix(".ts"))


def test_a_brace_count_that_recovers_is_still_not_a_placement(
    check_surface: ModuleType,
) -> None:
    """A closing brace this reader cannot see, and the count back at zero afterwards.

    The `}` inside a regex closed a scope nobody opened, and the next real `{`
    brought the count back to zero — so a declaration inside a namespace read as one
    at the top level. Checking the number at the declaration is not enough; the
    whole prefix has to have stayed non-negative (adversarial review, OPL-4805).
    """
    source = (
        'export {};\nlet unused\n/[}]/.test("");\n'
        "namespace Example {\nexport const SAMPLE_LIMIT = 36;\n}\n"
    )
    with pytest.raises(SystemExit, match="do not balance|not found"):
        check_surface.constant(source, "SAMPLE_LIMIT", Path("fixture").with_suffix(".ts"))
