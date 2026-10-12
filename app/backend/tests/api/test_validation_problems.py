import pytest
from pydantic import BaseModel, Field, ValidationError

from rdscommander.api.validation_problems import option_problems, sentence_for_error
from rdscommander.catalogue.options import EnvironmentImportOptions, TerraformAdoptionOptions


def _option_problems_for(model: type[BaseModel], options: dict) -> list[str]:
    with pytest.raises(ValidationError) as caught:
        model.model_validate(options)
    return option_problems(caught.value.errors(), model)


def test_a_pattern_without_help_is_described_by_the_pattern_itself() -> None:
    class Unhelpful(BaseModel):
        code: str = Field(pattern=r"^[A-Z]{3}$")

    assert _option_problems_for(Unhelpful, {"code": "abcd"}) == ["code: must match ^[A-Z]{3}$"]


def test_a_field_validator_message_loses_the_pydantic_prefix_and_keeps_its_own_words() -> None:
    assert _option_problems_for(
        EnvironmentImportOptions, {"slug": "compare", "display_name": "S", "simulated": True}
    ) == [
        "slug: 'compare' is reserved for a page of the console; choose another slug, for example compare-env"
    ]


def test_an_empty_selection_and_a_nested_value_read_as_sentences() -> None:
    assert _option_problems_for(
        TerraformAdoptionOptions, {"target_key": "sandbox/000000000000/eu-west-2", "resource_arns": []}
    ) == ["resource_arns: must have at least one entry"]
    assert _option_problems_for(
        TerraformAdoptionOptions, {"target_key": "sandbox/000000000000/eu-west-2", "resource_arns": [7]}
    ) == ["resource_arns -> 0: must be text"]


def test_help_opening_with_an_acronym_keeps_its_capitals_and_pydantic_openings_become_sentences() -> None:
    error = {"type": "string_pattern_mismatch", "loc": ("region",), "msg": "x", "ctx": {"pattern": "^x$"}}
    assert sentence_for_error(error, {"help": "AWS region names such as eu-west-2. More."}) == (
        "must match AWS region names such as eu-west-2"
    )
    assert sentence_for_error(error, {"help": "A version number such as 17 or 17.2. More."}) == (
        "must match a version number such as 17 or 17.2"
    )
    unusual = {"type": "url_parsing", "loc": ("link",), "msg": "Input should be a valid URL, empty host"}
    assert sentence_for_error(unusual) == "must be a valid URL, empty host"


def test_an_error_whose_context_lacks_what_its_sentence_needs_falls_back_to_the_reworded_message() -> None:
    error = {"type": "too_long", "loc": ("regions",), "msg": "List should have at most 3 items", "ctx": {}}
    assert sentence_for_error(error) == "must have at most 3 items"
