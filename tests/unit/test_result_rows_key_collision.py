"""Two keys that read the same once stringified must not overwrite each other.

The normaliser turns every dict key into text for JSON. Before the fix the text was the output
key, so the int 1 and the str "1" (or None and "None") landed on one key and the later value
silently replaced the earlier one, after the billed query had run.
"""

from cost_guard_mcp.tools.result_rows import _normalise_rows


def _row(row: dict) -> dict:
    return _normalise_rows([row]).rows[0]


def test_int_and_str_keys_both_survive_and_the_first_keeps_the_plain_text():
    assert _row({1: "int", "1": "str"}) == {"1": "int", "1__type_str__": "str"}
    assert _row({"1": "str", 1: "int"}) == {"1": "str", "1__type_int__": "int"}


def test_nested_dict_keys_are_kept_apart():
    assert _row({"data": {1: "int", "1": "str"}}) == {"data": {"1": "int", "1__type_str__": "str"}}


def test_none_and_the_text_none_are_kept_apart():
    assert _row({None: "null", "None": "text"}) == {"None": "null", "None__type_str__": "text"}


def test_a_taken_suffix_gets_a_counter():
    # "1__type_int__" is already a key, so the int 1 cannot take it and moves to the _2 form.
    assert _row({"1__type_int__": "c", "1": "a", 1: "b"}) == {
        "1__type_int__": "c",
        "1": "a",
        "1__type_int_2__": "b",
    }


def test_keys_that_do_not_collide_are_unchanged():
    assert _row({"a": "x", "b": "y", 1: "z"}) == {"a": "x", "b": "y", "1": "z"}


def test_the_counter_keeps_counting_while_names_are_taken():
    row = {"1__type_int__": "c", "1__type_int_2__": "d", "1": "a", 1: "b"}
    assert _row(row) == {
        "1__type_int__": "c",
        "1__type_int_2__": "d",
        "1": "a",
        "1__type_int_3__": "b",
    }


def test_a_genuine_key_equal_to_a_made_name_is_itself_renamed():
    # Documented consequence of "the first key with a text keeps it": the third key reads the same as
    # the name the second one was given, so it is the one that moves.
    row = {"1": "a", 1: "b", "1__type_int__": "c"}
    assert _row(row) == {"1": "a", "1__type_int__": "b", "1__type_int____type_str__": "c"}
