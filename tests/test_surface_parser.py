"""What ``check_surface`` reads out of an ``apidoc.ts`` it does not control.

The route half of this gate has its own tests; this is the parameter half,
which is the half that fails quietly. A list this reader misses is not an error
— the other routes still count parameters, so the guard for a scan that found
nothing stays silent — and the route's genuine parameters come back as ones the
mirror invented, which sends whoever reads the report to the wrong file to
delete entries that are correct.

A port of the TypeScript SDK's ``test/surface-parser.test.ts`` cases for the
same four fixes (OPL-4483, OPL-4511, OPL-4513), against this reader instead:
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
        # Flush left: `shared_query` reads a declaration only at the start of a
        # line, which is how it is spelled upstream and is not what this test is
        # about. That it is a spelling — `export const`, or an indent — is a
        # divergence from the TypeScript reader that this ticket left alone.
        "const X_KEY: Query = { name: 'X-Model-Key', description: 'x' };\n"
        "export const DOCS: Record<string, Doc> = {\n"
        "  'GET sizes': { headers: [X_KEY] },\n"
        "};\n",
    )
    assert found == {"header:X-Model-Key"}


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
