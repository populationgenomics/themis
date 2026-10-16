"""What a decoded protobuf message holds that its schema does not name, found before anything reads it."""

from __future__ import annotations

from google.protobuf import message, unknown_fields


def undeclared(msg: message.Message) -> list[str]:
    """Each place within `msg`, itself included, holding a field or an enum number its schema does not name.

    Either is what a newer writer leaves for these stubs, or what a field or member retired after the
    message was written leaves behind. Decoding keeps both, but `json_format` drops an unknown field
    silently, so a reader of the message's JSON would never learn of it.

    Returns:
        One description per message holding unknown field numbers, and per field holding enum numbers
        its enum does not name; empty where the schema names everything `msg` holds.
    """
    found: list[str] = []
    numbers = sorted({field.field_number for field in unknown_fields.UnknownFieldSet(msg)})
    if numbers:
        found.append(f'{msg.DESCRIPTOR.full_name} field number(s) {numbers}')
    for field, value in msg.ListFields():
        if field.message_type is not None and field.message_type.GetOptions().map_entry:
            held, items = field.message_type.fields_by_name['value'], list(value.values())
        else:
            held, items = field, list(value) if field.is_repeated else [value]
        if held.enum_type is not None:
            unnamed = sorted({item for item in items if item not in held.enum_type.values_by_number})
            if unnamed:
                found.append(f'{field.full_name} enum number(s) {unnamed}, which {held.enum_type.full_name} lacks')
        elif held.message_type is not None:
            for item in items:
                found += undeclared(item)
    return found
