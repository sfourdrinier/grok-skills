# wrapper/scripts/tests/test_grokcli_output.py

import unittest

from groklib import GrokWrapperError, grokcli_output


class ParseGrokJsonTests(unittest.TestCase):
    """parse_grok_json fails closed as output-malformed on anything but a JSON object."""

    def test_parses_valid_object(self) -> None:
        parsed = grokcli_output.parse_grok_json('{"text": "PONG", "stopReason": "EndTurn"}')
        self.assertEqual(parsed["text"], "PONG")

    def test_empty_stdout_is_output_malformed(self) -> None:
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli_output.parse_grok_json("   \n")
        self.assertEqual(caught.exception.error_class, "output-malformed")

    def test_non_json_is_output_malformed(self) -> None:
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli_output.parse_grok_json("not json {")
        self.assertEqual(caught.exception.error_class, "output-malformed")

    def test_json_array_is_output_malformed(self) -> None:
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli_output.parse_grok_json("[1, 2, 3]")
        self.assertEqual(caught.exception.error_class, "output-malformed")


class StopReasonPredicateTests(unittest.TestCase):
    """is_cancelled / is_turn_exhaustion normalize the stop reason robustly."""

    def test_cancelled_matches_case_insensitively(self) -> None:
        self.assertTrue(grokcli_output.is_cancelled("Cancelled"))
        self.assertTrue(grokcli_output.is_cancelled("CANCELED"))
        self.assertFalse(grokcli_output.is_cancelled("EndTurn"))
        self.assertFalse(grokcli_output.is_cancelled(None))

    def test_turn_exhaustion_matches_max_turn_tokens(self) -> None:
        self.assertTrue(grokcli_output.is_turn_exhaustion("MaxTurns", 30, 30))
        self.assertTrue(grokcli_output.is_turn_exhaustion("max_turns_reached", 5, 5))
        self.assertFalse(grokcli_output.is_turn_exhaustion("EndTurn", 1, 30))
        # Grok reports turn-cap as Cancelled when num_turns hits the budget.
        self.assertTrue(grokcli_output.is_turn_exhaustion("Cancelled", 30, 30))
        # Cancelled well under budget is not turn-exhaustion.
        self.assertFalse(grokcli_output.is_turn_exhaustion("Cancelled", 1, 30))
        # Unlimited (no max_turns): plain Cancelled is not turn-exhaustion.
        self.assertFalse(grokcli_output.is_turn_exhaustion("Cancelled", 1, None))
        self.assertFalse(grokcli_output.is_turn_exhaustion("Cancelled", None, None))

    def test_turn_exhaustion_num_turns_fallback(self) -> None:
        # Unknown non-terminal stop reason but the turn budget is spent.
        self.assertTrue(grokcli_output.is_turn_exhaustion("Aborting", 30, 30))
        # EndTurn at the budget is a clean finish, not exhaustion.
        self.assertFalse(grokcli_output.is_turn_exhaustion("EndTurn", 30, 30))
        # Cancelled with missing num_turns but an explicit max_turns: treat as budget.
        self.assertTrue(grokcli_output.is_turn_exhaustion("Cancelled", None, 30))

    def test_has_usable_model_output(self) -> None:
        self.assertTrue(grokcli_output.has_usable_model_output({"final_text": "findings here"}))
        self.assertTrue(grokcli_output.has_usable_model_output({"structured": {"findings": []}}))
        self.assertFalse(grokcli_output.has_usable_model_output({"final_text": "  ", "structured": None}))
        self.assertFalse(grokcli_output.has_usable_model_output({"structured": {}}))

    def test_error_stop_at_turn_cap_is_not_turn_exhaustion(self) -> None:
        # Grok dogfood #13: an explicit error/refusal stop that coincides with the
        # turn cap must NOT be reclassified as turn-exhaustion (it stays a
        # cli-failure); only non-error unknown stops at the cap fall back.
        for error_stop in ("ToolError", "ExecutionFailed", "Refused", "PermissionDenied", "InvalidRequest"):
            with self.subTest(stop=error_stop):
                self.assertFalse(grokcli_output.is_turn_exhaustion(error_stop, 30, 30))
        # A genuinely turn-shaped stop is still caught regardless of the cap.
        self.assertTrue(grokcli_output.is_turn_exhaustion("MaxTurnsReached", 30, 30))


class SchemaWalkerTests(unittest.TestCase):
    """validate_structured_output enforces the required/properties/type subset with JSON pointers."""

    def test_valid_object_passes(self) -> None:
        schema = {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}}
        grokcli_output.validate_structured_output({"answer": "PONG"}, schema)

    def test_missing_required_property_reports_pointer(self) -> None:
        schema = {
            "type": "object",
            "required": ["answer", "count"],
            "properties": {"answer": {"type": "string"}, "count": {"type": "number"}},
        }
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli_output.validate_structured_output({"answer": "PONG"}, schema)
        self.assertEqual(caught.exception.error_class, "schema-mismatch")
        self.assertEqual(caught.exception.detail.get("pointer"), "/count")

    def test_wrong_scalar_type_reports_pointer(self) -> None:
        schema = {"type": "object", "required": ["count"], "properties": {"count": {"type": "number"}}}
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli_output.validate_structured_output({"count": "not-a-number"}, schema)
        self.assertEqual(caught.exception.error_class, "schema-mismatch")
        self.assertEqual(caught.exception.detail.get("pointer"), "/count")

    def test_nested_array_of_objects_pointer(self) -> None:
        schema = {
            "type": "object",
            "required": ["items"],
            "properties": {
                "items": {
                    "type": "array",
                    "items": {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}},
                }
            },
        }
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli_output.validate_structured_output({"items": [{"id": "a"}, {"nope": "b"}]}, schema)
        self.assertEqual(caught.exception.error_class, "schema-mismatch")
        self.assertEqual(caught.exception.detail.get("pointer"), "/items/1/id")

    def test_boolean_true_is_not_a_number(self) -> None:
        schema = {"type": "object", "required": ["n"], "properties": {"n": {"type": "number"}}}
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli_output.validate_structured_output({"n": True}, schema)
        self.assertEqual(caught.exception.error_class, "schema-mismatch")

    def test_integer_true_is_not_an_integer(self) -> None:
        schema = {"type": "object", "required": ["n"], "properties": {"n": {"type": "integer"}}}
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli_output.validate_structured_output({"n": False}, schema)
        self.assertEqual(caught.exception.error_class, "schema-mismatch")

    def test_enum_rejects_value_outside_the_enum(self) -> None:
        # Grok dogfood #5: a value outside a declared enum must fail closed at the
        # failing pointer, not be silently accepted as a plain string.
        schema = {
            "type": "object",
            "required": ["verdict"],
            "properties": {"verdict": {"type": "string", "enum": ["pass", "fail"]}},
        }
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli_output.validate_structured_output({"verdict": "maybe"}, schema)
        self.assertEqual(caught.exception.error_class, "schema-mismatch")
        self.assertEqual(caught.exception.detail.get("pointer"), "/verdict")

    def test_enum_accepts_conforming_value(self) -> None:
        schema = {
            "type": "object",
            "required": ["verdict"],
            "properties": {"verdict": {"type": "string", "enum": ["pass", "fail", "inconclusive"]}},
        }
        grokcli_output.validate_structured_output({"verdict": "inconclusive"}, schema)

    def test_enum_only_node_without_type_is_honored(self) -> None:
        # An enum-only node (no "type") validates by membership and must not fall
        # through to the missing-type mismatch.
        schema = {"type": "object", "required": ["color"], "properties": {"color": {"enum": ["red", "green"]}}}
        grokcli_output.validate_structured_output({"color": "green"}, schema)
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli_output.validate_structured_output({"color": "blue"}, schema)
        self.assertEqual(caught.exception.error_class, "schema-mismatch")

    def test_enum_boolean_is_not_accepted_for_integer_enum(self) -> None:
        # Python's True == 1 must not let a boolean satisfy an integer enum.
        schema = {"type": "object", "required": ["n"], "properties": {"n": {"enum": [0, 1, 2]}}}
        with self.assertRaises(GrokWrapperError):
            grokcli_output.validate_structured_output({"n": True}, schema)

    def test_enum_not_a_list_is_mismatch(self) -> None:
        schema = {"type": "object", "required": ["v"], "properties": {"v": {"enum": "pass"}}}
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli_output.validate_structured_output({"v": "pass"}, schema)
        self.assertEqual(caught.exception.error_class, "schema-mismatch")

    def test_unsupported_schema_type_fails_closed(self) -> None:
        schema = {"type": "object", "required": ["x"], "properties": {"x": {"type": "geopoint"}}}
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli_output.validate_structured_output({"x": {}}, schema)
        self.assertEqual(caught.exception.error_class, "schema-mismatch")

    def test_pointer_escapes_rfc6901_special_characters(self) -> None:
        schema = {"type": "object", "required": ["a/b"], "properties": {}}
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli_output.validate_structured_output({}, schema)
        self.assertEqual(caught.exception.detail.get("pointer"), "/a~1b")

    def test_schema_required_not_a_list_is_mismatch(self) -> None:
        # A malformed "required" keyword (operator --schema typo: a string
        # instead of a list) at a nested schema node must fail loudly as a
        # classified schema-mismatch pointing at that node, never silently
        # skip the required-property check.
        schema = {
            "type": "object",
            "required": ["nested"],
            "properties": {"nested": {"type": "object", "required": "oops", "properties": {}}},
        }
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli_output.validate_structured_output({"nested": {}}, schema)
        self.assertEqual(caught.exception.error_class, "schema-mismatch")
        self.assertEqual(caught.exception.detail.get("pointer"), "/nested")

    def test_schema_required_entry_not_a_string_is_mismatch(self) -> None:
        # A malformed "required" ENTRY (a non-string list item, e.g. an operator
        # --schema typo of 123 instead of "name") must fail loudly rather than be
        # silently ignored -- otherwise {"required":[123]} would validate {} as OK.
        schema = {"type": "object", "required": [123], "properties": {}}
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli_output.validate_structured_output({}, schema)
        self.assertEqual(caught.exception.error_class, "schema-mismatch")
        self.assertEqual(
            caught.exception.detail.get("reason"), "schema-required-entry-not-a-string"
        )
        self.assertEqual(caught.exception.detail.get("entryType"), "int")

    def test_schema_properties_not_a_dict_is_mismatch(self) -> None:
        # A malformed "properties" keyword (a list instead of an object) at a
        # nested schema node must likewise fail loudly with a pointer to that
        # node, never silently skip property validation.
        schema = {
            "type": "object",
            "required": ["nested"],
            "properties": {
                "nested": {"type": "object", "required": [], "properties": ["not", "a", "dict"]}
            },
        }
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli_output.validate_structured_output({"nested": {}}, schema)
        self.assertEqual(caught.exception.error_class, "schema-mismatch")
        self.assertEqual(caught.exception.detail.get("pointer"), "/nested")

    def test_array_items_tuple_form_is_mismatch(self) -> None:
        # PR968 codex array-items: a tuple-form "items" ([...]) is an item
        # constraint this walker does not implement; it must fail closed rather
        # than silently accept every array element.
        schema = {"type": "array", "items": [{"type": "string"}, {"type": "number"}]}
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli_output.validate_structured_output(["a", 1], schema)
        self.assertEqual(caught.exception.error_class, "schema-mismatch")
        self.assertEqual(caught.exception.detail.get("reason"), "schema-items-not-an-object")

    def test_array_items_false_is_mismatch(self) -> None:
        # items:false forbids/constrains elements; an unimplemented form must
        # reject, never bless the array by returning success.
        schema = {"type": "array", "items": False}
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli_output.validate_structured_output(["anything"], schema)
        self.assertEqual(caught.exception.error_class, "schema-mismatch")
        self.assertEqual(caught.exception.detail.get("reason"), "schema-items-not-an-object")

    def test_array_without_items_accepts_any_elements(self) -> None:
        # An array schema with NO "items" keyword places no element constraint;
        # it still validates (the fail-closed branch fires only when "items" is
        # present but not an object schema).
        grokcli_output.validate_structured_output(["a", 1, {"k": "v"}], {"type": "array"})


class UnsupportedSchemaKeywordTests(unittest.TestCase):
    """PR968 codex #6: constraint keywords the walker does not enforce fail closed."""

    def test_additional_properties_false_rejects_nonconforming_object(self) -> None:
        # additionalProperties:false is a real constraint the walker cannot enforce.
        # A value with an extra property would slip past a walker that only checked
        # required/properties -- so the schema keyword itself is a fail-closed mismatch.
        schema = {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "additionalProperties": False,
        }
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli_output.validate_structured_output({"answer": "PONG", "sneaky": 1}, schema)
        self.assertEqual(caught.exception.error_class, "schema-mismatch")
        self.assertEqual(caught.exception.detail.get("reason"), "unsupported-schema-keyword")
        self.assertIn("additionalProperties", caught.exception.detail.get("unsupportedKeywords", []))

    def test_various_unenforced_constraints_fail_closed(self) -> None:
        for keyword, value in (
            ("minProperties", 1),
            ("patternProperties", {"^x": {"type": "string"}}),
            ("minLength", 3),
            ("pattern", "^a"),
            ("oneOf", [{"type": "string"}]),
        ):
            with self.subTest(keyword=keyword):
                schema = {"type": "object", "properties": {}, keyword: value}
                with self.assertRaises(GrokWrapperError) as caught:
                    grokcli_output.validate_structured_output({}, schema)
                self.assertEqual(caught.exception.error_class, "schema-mismatch")
                self.assertIn(keyword, caught.exception.detail.get("unsupportedKeywords", []))

    def test_nested_unsupported_keyword_fails_with_pointer(self) -> None:
        schema = {
            "type": "object",
            "required": ["nested"],
            "properties": {"nested": {"type": "string", "minLength": 5}},
        }
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli_output.validate_structured_output({"nested": "ok"}, schema)
        self.assertEqual(caught.exception.error_class, "schema-mismatch")
        self.assertEqual(caught.exception.detail.get("pointer"), "/nested")

    def test_inert_annotation_keywords_are_allowed(self) -> None:
        # Pure annotation keywords never constrain validation, so they must NOT be
        # rejected -- a schema carrying title/description still validates normally.
        schema = {
            "type": "object",
            "title": "Answer",
            "description": "the reply",
            "required": ["answer"],
            "properties": {"answer": {"type": "string", "description": "text"}},
        }
        grokcli_output.validate_structured_output({"answer": "PONG"}, schema)


class ExtractResultFieldsStructuredTests(unittest.TestCase):
    """PR968 codex #5: non-object structuredOutput is preserved, not discarded."""

    def test_array_root_structured_output_is_preserved(self) -> None:
        fields = grokcli_output.extract_result_fields({"structuredOutput": [1, 2, 3]})
        self.assertEqual(fields["structured"], [1, 2, 3])

    def test_scalar_root_structured_output_is_preserved(self) -> None:
        for value in ("hello", 7, 0, False, True):
            with self.subTest(value=value):
                fields = grokcli_output.extract_result_fields({"structuredOutput": value})
                self.assertEqual(fields["structured"], value)

    def test_absent_or_null_structured_output_is_none(self) -> None:
        # Both the absent key and an explicit JSON null mean "no structured output",
        # which the schema-run missing check treats as structured-output-missing.
        self.assertIsNone(grokcli_output.extract_result_fields({})["structured"])
        self.assertIsNone(grokcli_output.extract_result_fields({"structuredOutput": None})["structured"])

    def test_preserved_array_validates_against_array_root_schema(self) -> None:
        # The end-to-end point: a valid array-root result is returned and validates,
        # NOT reported as structured-output-missing.
        fields = grokcli_output.extract_result_fields({"structuredOutput": ["a", "b"]})
        schema = {"type": "array", "items": {"type": "string"}}
        grokcli_output.validate_structured_output(fields["structured"], schema)


class ModelsOutputTests(unittest.TestCase):
    """parse_models_output classifies login state and extracts the model list."""

    _LOGGED_IN = (
        "You are logged in with grok.com.\n"
        "\n"
        "Default model: grok-4.5\n"
        "\n"
        "Available models:\n"
        "  * grok-4.5 (default)\n"
        "  - grok-composer-2.5-fast\n"
    )

    def test_logged_in_extracts_default_and_models(self) -> None:
        result = grokcli_output.parse_models_output(self._LOGGED_IN)
        self.assertTrue(result["loggedIn"])
        self.assertEqual(result["defaultModel"], "grok-4.5")
        self.assertEqual(result["models"], ["grok-4.5", "grok-composer-2.5-fast"])

    def test_not_logged_in_raises_auth_missing(self) -> None:
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli_output.parse_models_output("You are not logged in.\nRun grok login.\n")
        self.assertEqual(caught.exception.error_class, "auth-missing")

    def test_logged_in_without_model_list_is_output_malformed(self) -> None:
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli_output.parse_models_output("You are logged in with grok.com.\n")
        self.assertEqual(caught.exception.error_class, "output-malformed")


if __name__ == "__main__":
    unittest.main()
