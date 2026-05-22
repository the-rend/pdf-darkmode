from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    ContentStream,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
)
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn

DEVICE_GRAY = NameObject("/DeviceGray")
DEVICE_RGB = NameObject("/DeviceRGB")
DEVICE_CMYK = NameObject("/DeviceCMYK")
RESOURCES = NameObject("/Resources")
XOBJECT = NameObject("/XObject")
SUBTYPE = NameObject("/Subtype")
FORM = NameObject("/Form")
CONTENTS = NameObject("/Contents")
FILTER = NameObject("/Filter")
DECODE_PARMS = NameObject("/DecodeParms")
LENGTH = NameObject("/Length")

DEFAULT_DARKMODE_COLOR = b"1 1 1 rg\n1 1 1 RG\n"


@dataclass
class ColorState:
    stroking_space: Any = DEVICE_GRAY
    nonstroking_space: Any = DEVICE_GRAY


def invert_pdf(
    input_path: Path | str,
    *,
    add_dark_background: bool = True,
    text_gray_factor: float = 0.9,
) -> None:
    reader = PdfReader(str(input_path))
    visited: set[Any] = set()

    with Progress(
        TextColumn("[bold white]{task.percentage:>3.0f}%"),
        BarColumn(bar_width=24, style="red", complete_style="green"),
        TextColumn("{task.completed}/{task.total} pages"),
    ) as progress:
        task = progress.add_task("", total=len(reader.pages))
        for page in reader.pages:
            _invert_page_stream(
                page,
                reader,
                visited,
                add_dark_background=add_dark_background,
                text_gray_factor=text_gray_factor,
            )
            progress.advance(task)

    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    output_path = _default_output_path(Path(input_path))
    with output_path.open("wb") as handle:
        writer.write(handle)


def _invert_page_stream(
    page: Any,
    pdf: PdfReader,
    visited: set[Any],
    *,
    add_dark_background: bool,
    text_gray_factor: float,
) -> None:
    if CONTENTS in page:
        page[CONTENTS] = _invert_contents(page[CONTENTS], pdf, visited, text_gray_factor=text_gray_factor)

    if add_dark_background:
        _prepend_dark_background(page)

    resources = page.get(RESOURCES)
    if isinstance(resources, DictionaryObject):
        _invert_resource_xobjects(resources, pdf, visited, text_gray_factor=text_gray_factor)


def _invert_resource_xobjects(
    resources: DictionaryObject,
    pdf: PdfReader,
    visited: set[Any],
    *,
    text_gray_factor: float,
) -> None:
    xobjects = resources.get(XOBJECT)
    if not isinstance(xobjects, DictionaryObject):
        return

    for name, xobject_ref in list(xobjects.items()):
        xobject = xobject_ref.get_object() if hasattr(xobject_ref, "get_object") else xobject_ref
        if not isinstance(xobject, DictionaryObject) or xobject.get(SUBTYPE) != FORM:
            continue
        xobjects[name] = _invert_stream_object(
            xobject_ref,
            pdf,
            visited,
            text_gray_factor=text_gray_factor,
        )


def _invert_contents(contents: Any, pdf: PdfReader, visited: set[Any], *, text_gray_factor: float) -> Any:
    resolved = contents.get_object() if hasattr(contents, "get_object") else contents
    if isinstance(resolved, ArrayObject):
        for item in resolved:
            _invert_stream_object(item, pdf, visited, text_gray_factor=text_gray_factor)
        return contents
    _invert_stream_object(contents, pdf, visited, text_gray_factor=text_gray_factor)
    return contents


def _prepend_dark_background(page: Any) -> None:
    media_box = page.mediabox
    x = float(media_box.left)
    y = float(media_box.bottom)
    width = float(media_box.right - media_box.left)
    height = float(media_box.top - media_box.bottom)

    background_data = (
        "q\n"
        "0 0 0 rg\n"
        f"{x:g} {y:g} {width:g} {height:g} re\n"
        "f\n"
        "Q\n"
    ).encode("ascii")

    existing = page.get(CONTENTS)
    if existing is None:
        return

    resolved = existing.get_object() if hasattr(existing, "get_object") else existing
    if isinstance(resolved, ArrayObject):
        if not resolved:
            return
        first_stream = resolved[0]
        first_resolved = first_stream.get_object() if hasattr(first_stream, "get_object") else first_stream
        _set_stream_data(first_resolved, background_data + _stream_data(first_resolved))
        return

    _set_stream_data(resolved, background_data + _stream_data(resolved))


def _invert_stream_object(stream_obj: Any, pdf: PdfReader, visited: set[Any], *, text_gray_factor: float) -> Any:
    resolved = stream_obj.get_object() if hasattr(stream_obj, "get_object") else stream_obj
    key = _stream_identity(resolved)
    if key in visited:
        return stream_obj

    visited.add(key)

    content = ContentStream(resolved, pdf)
    content.operations = _rewrite_operations(content.operations, text_gray_factor=text_gray_factor)

    _set_stream_data(resolved, DEFAULT_DARKMODE_COLOR + content.get_data())

    resources = resolved.get(RESOURCES)
    if isinstance(resources, DictionaryObject):
        _invert_resource_xobjects(resources, pdf, visited, text_gray_factor=text_gray_factor)

    return stream_obj


def _rewrite_operations(
    operations: list[tuple[list[Any], bytes]],
    *,
    text_gray_factor: float,
) -> list[tuple[list[Any], bytes]]:
    state = ColorState()
    state_stack: list[ColorState] = []
    rewritten: list[tuple[list[Any], bytes]] = []
    in_text_object = False
    text_gray_factor = max(0.0, min(1.0, text_gray_factor))

    for operands, operator in operations:
        if operator == b"q":
            state_stack.append(ColorState(state.stroking_space, state.nonstroking_space))
            rewritten.append((operands, operator))
            continue

        if operator == b"Q":
            if state_stack:
                state = state_stack.pop()
            rewritten.append((operands, operator))
            continue

        if operator == b"BT":
            in_text_object = True
            rewritten.append((operands, operator))
            rewritten.extend(_text_default_color_operations(text_gray_factor))
            state.stroking_space = DEVICE_GRAY
            state.nonstroking_space = DEVICE_GRAY
            continue

        if operator == b"ET":
            in_text_object = False
            rewritten.append((operands, operator))
            continue

        if operator in {b"g", b"G", b"rg", b"RG", b"k", b"K"}:
            _invert_basic_color_operator(operands, operator)
            if in_text_object:
                _dim_operands(operands, operator, text_gray_factor)
            if operator == b"g":
                state.nonstroking_space = DEVICE_GRAY
            elif operator == b"G":
                state.stroking_space = DEVICE_GRAY
            elif operator == b"rg":
                state.nonstroking_space = DEVICE_RGB
            elif operator == b"RG":
                state.stroking_space = DEVICE_RGB
            elif operator == b"k":
                state.nonstroking_space = DEVICE_CMYK
            elif operator == b"K":
                state.stroking_space = DEVICE_CMYK
            rewritten.append((operands, operator))
            continue

        if operator in {b"cs", b"CS"}:
            _update_color_space(state, operands, operator)
            rewritten.append((operands, operator))
            continue

        if operator in {b"sc", b"scn", b"SC", b"SCN"}:
            if _invert_current_color(state, operands, operator):
                if in_text_object:
                    color_space = state.nonstroking_space if operator in {b"sc", b"scn"} else state.stroking_space
                    _dim_operands_by_space(operands, color_space, text_gray_factor)
                rewritten.append((operands, operator))
                continue

        rewritten.append((operands, operator))

    return rewritten


def _text_default_color_operations(text_gray_factor: float) -> list[tuple[list[Any], bytes]]:
    gray_value = _number_like(FloatObject(text_gray_factor), text_gray_factor)
    return [([gray_value], b"g"), ([gray_value], b"G")]


def _invert_basic_color_operator(operands: list[Any], operator: bytes) -> None:
    if operator in {b"g", b"G"}:
        if operands:
            operands[0] = _number_like(operands[0], _invert_component(float(operands[0])))
        return

    if operator in {b"rg", b"RG"}:
        for index in range(min(3, len(operands))):
            operands[index] = _number_like(operands[index], _invert_component(float(operands[index])))
        return

    if operator in {b"k", b"K"}:
        for index in range(min(4, len(operands))):
            operands[index] = _number_like(operands[index], _invert_component(float(operands[index])))


def _update_color_space(state: ColorState, operands: list[Any], operator: bytes) -> None:
    color_space = operands[0] if operands else None
    if operator == b"cs":
        state.nonstroking_space = color_space
    else:
        state.stroking_space = color_space


def _invert_current_color(state: ColorState, operands: list[Any], operator: bytes) -> bool:
    color_space = state.nonstroking_space if operator in {b"sc", b"scn"} else state.stroking_space

    if color_space == DEVICE_GRAY and operands:
        operands[0] = _number_like(operands[0], _invert_component(float(operands[0])))
        return True

    if color_space == DEVICE_RGB and len(operands) >= 3:
        for index in range(3):
            operands[index] = _number_like(operands[index], _invert_component(float(operands[index])))
        return True

    if color_space == DEVICE_CMYK and len(operands) >= 4:
        for index in range(4):
            operands[index] = _number_like(operands[index], _invert_component(float(operands[index])))
        return True

    return False


def _invert_component(value: float) -> float:
    return max(0.0, min(1.0, 1.0 - value))


def _dim_component(value: float, factor: float) -> float:
    return max(0.0, min(1.0, value * factor))


def _dim_operands(operands: list[Any], operator: bytes, factor: float) -> None:
    if operator in {b"g", b"G"} and operands:
        operands[0] = _number_like(operands[0], _dim_component(float(operands[0]), factor))
        return

    if operator in {b"rg", b"RG"}:
        for index in range(min(3, len(operands))):
            operands[index] = _number_like(operands[index], _dim_component(float(operands[index]), factor))
        return

    if operator in {b"k", b"K"}:
        for index in range(min(4, len(operands))):
            operands[index] = _number_like(operands[index], _dim_component(float(operands[index]), factor))


def _dim_operands_by_space(operands: list[Any], color_space: Any, factor: float) -> None:
    if color_space == DEVICE_GRAY and operands:
        operands[0] = _number_like(operands[0], _dim_component(float(operands[0]), factor))
        return

    if color_space == DEVICE_RGB and len(operands) >= 3:
        for index in range(3):
            operands[index] = _number_like(operands[index], _dim_component(float(operands[index]), factor))
        return

    if color_space == DEVICE_CMYK and len(operands) >= 4:
        for index in range(4):
            operands[index] = _number_like(operands[index], _dim_component(float(operands[index]), factor))


def _number_like(original: Any, value: float) -> Any:
    if isinstance(original, NumberObject) and not isinstance(original, FloatObject):
        if abs(value - round(value)) > 1e-9:
            return FloatObject(value)
    try:
        return type(original)(value)
    except Exception:
        return FloatObject(value)


def _stream_identity(stream_obj: Any) -> Any:
    reference = getattr(stream_obj, "indirect_reference", None)
    if reference is not None:
        return (reference.idnum, reference.generation)
    return id(stream_obj)


def _stream_data(stream_obj: Any) -> bytes:
    if hasattr(stream_obj, "get_data"):
        return stream_obj.get_data()
    raise TypeError(f"Unsupported stream object: {type(stream_obj)!r}")


def _set_stream_data(stream_obj: Any, data: bytes) -> None:
    if not hasattr(stream_obj, "set_data"):
        raise TypeError(f"Unsupported stream object: {type(stream_obj)!r}")

    try:
        stream_obj.set_data(data)
    except Exception:
        stream_obj._data = data
    if isinstance(stream_obj, DictionaryObject):
        if FILTER in stream_obj:
            del stream_obj[FILTER]
        if DECODE_PARMS in stream_obj:
            del stream_obj[DECODE_PARMS]
        stream_obj[LENGTH] = NumberObject(len(data))


def _default_output_path(input_path: Path) -> Path:
    return input_path.with_name(f"{input_path.stem} - darkmode{input_path.suffix}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Invert PDF colors without rasterizing the document.")
    parser.add_argument("input_pdf", type=Path, help="Path to the source PDF")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    input_path: Path = args.input_pdf
    invert_pdf(input_path)


if __name__ == "__main__":
    main()
