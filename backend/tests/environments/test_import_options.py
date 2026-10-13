import pytest
from pydantic import ValidationError

from rdscommander.catalogue.options import EnvironmentImportOptions


@pytest.mark.parametrize("reserved_slug", ["compare", "import"])
def test_a_slug_that_would_shadow_a_console_page_is_rejected(reserved_slug: str) -> None:
    with pytest.raises(ValidationError, match=f"'{reserved_slug}' is reserved for a page of the console"):
        EnvironmentImportOptions(slug=reserved_slug, display_name="X", simulated=True)


@pytest.mark.parametrize("slug", ["compare-prod", "importer", "staging"])
def test_slugs_that_merely_contain_a_reserved_word_are_accepted(slug: str) -> None:
    assert EnvironmentImportOptions(slug=slug, display_name="X", simulated=True).slug == slug
