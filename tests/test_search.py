from scraper.search import split_locations, split_terms


def test_split_terms_accepts_a_numbered_company_list():
    assert split_terms("1 McDonald's\n2. Burger King\n3 - KFC\nBurger King") == (
        "McDonald's", "Burger King", "KFC",
    )


def test_split_terms_accepts_plus_separated_batch_input_and_preserves_cplusplus():
    assert split_terms("altenheim+ambulante+C+++hotel+Restaurant") == (
        "altenheim", "ambulante", "C++", "hotel", "Restaurant",
    )


def test_split_locations_accepts_plus_separated_cities():
    assert split_locations("Bonn + Köln, Berlin\nBonn") == ("Bonn", "Köln", "Berlin")
