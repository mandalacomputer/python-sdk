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
    with pytest.raises(SystemExit, match="declared 2 times"):
        check_surface.constant(source, name, Path("fixture").with_suffix(suffix))


def test_an_ordinary_division_in_a_constants_module_is_not_refused(
    check_surface: ModuleType,
) -> None:
    """The modules constants are read out of are source, not tables.

    A slash after a `)` is undecidable to the strict reader the table walkers use,
    and `Date.now() / 1000` is exactly that shape — ordinary arithmetic, in a file
    this has to get a constant out of. Blanking comments with the tolerant
    predicate is what keeps a legal division from taking the comparison down; it
    cannot read one as a regex, because a regex cannot begin after a value.
    """
    source = "const seconds = Date.now() / 1000;\nexport const SAMPLE_LIMIT = 36;\n"
    assert check_surface.constant(source, "SAMPLE_LIMIT", Path("fixture").with_suffix(".ts")) == 36
